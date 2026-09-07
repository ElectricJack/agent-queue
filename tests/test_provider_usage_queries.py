# tests/test_provider_usage_queries.py
"""ProviderUsageQueryMixin — implementation spec T1.

The dedup rule is the reason this table needs a query layer at all, so most
of what follows pins it down: an unchanged reading must not write a row, and
a changed one always must.  Everything runs on SQLite and, when
``POSTGRES_TEST_DSN`` is set, on PostgreSQL too — ``window`` is a reserved
word there, and the grouped-max used by ``latest_provider_usage`` is the kind
of statement that quietly differs per dialect.
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database import Database
from src.database.tables import provider_usage_snapshots
from src.models import ProviderUsageSnapshot
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def any_db(request, tmp_path):
    """SQLite always; PostgreSQL when ``POSTGRES_TEST_DSN`` is set (CI)."""
    if request.param == "postgres":
        if not POSTGRES_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "provider_usage.db"))
        await database.initialize()
    yield database
    await database.close()


def snap(
    *,
    provider="codex",
    window="primary",
    scope="",
    used_percent=88.0,
    observed_at=1_000.0,
    resets_at=2_000.0,
    account_label="pro",
    source="transcript",
) -> ProviderUsageSnapshot:
    return ProviderUsageSnapshot(
        provider=provider,
        window=window,
        scope=scope,
        used_percent=used_percent,
        observed_at=observed_at,
        resets_at=resets_at,
        account_label=account_label,
        source=source,
    )


async def count_rows(db) -> int:
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(provider_usage_snapshots.c.id))).all()
    return len(rows)


# --- writing ---------------------------------------------------------------


async def test_record_writes_a_first_reading_and_reads_it_back(any_db):
    written = await any_db.record_provider_usage([snap()])

    assert written == 1
    rows = await any_db.latest_provider_usage()
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "codex"
    assert row["window"] == "primary"
    assert row["scope"] == ""
    assert row["account_label"] == "pro"
    assert row["used_percent"] == 88.0
    assert row["resets_at"] == 2_000.0
    assert row["observed_at"] == 1_000.0
    assert row["source"] == "transcript"


async def test_record_of_nothing_is_a_no_op(any_db):
    assert await any_db.record_provider_usage([]) == 0
    assert await count_rows(any_db) == 0


async def test_an_unchanged_repeat_of_the_newest_row_is_dropped(any_db):
    await any_db.record_provider_usage([snap(observed_at=1_000.0)])

    # Same reading, later clock: the whole point -- an idle fleet re-reporting
    # 88% every couple of seconds must not fill the table.
    written = await any_db.record_provider_usage([snap(observed_at=1_060.0)])

    assert written == 0
    assert await count_rows(any_db) == 1


async def test_a_changed_percent_always_writes(any_db):
    await any_db.record_provider_usage([snap(used_percent=88.0)])

    written = await any_db.record_provider_usage(
        [snap(used_percent=89.0, observed_at=1_060.0)]
    )

    assert written == 1
    assert await count_rows(any_db) == 2
    assert (await any_db.latest_provider_usage())[0]["used_percent"] == 89.0


async def test_a_changed_reset_clock_writes_even_at_the_same_percent(any_db):
    """The window rolled over: same percentage, new deadline, real news."""
    await any_db.record_provider_usage([snap(used_percent=5.0, resets_at=2_000.0)])

    written = await any_db.record_provider_usage(
        [snap(used_percent=5.0, resets_at=9_000.0, observed_at=1_060.0)]
    )

    assert written == 1
    assert (await any_db.latest_provider_usage())[0]["resets_at"] == 9_000.0


async def test_a_reading_that_regains_a_reset_clock_writes(any_db):
    """``None`` and a float are different readings in both directions."""
    await any_db.record_provider_usage([snap(resets_at=None)])
    assert await any_db.record_provider_usage([snap(resets_at=None, observed_at=1_060.0)]) == 0

    written = await any_db.record_provider_usage(
        [snap(resets_at=2_000.0, observed_at=1_120.0)]
    )

    assert written == 1


async def test_dedup_applies_within_one_batch(any_db):
    written = await any_db.record_provider_usage(
        [snap(observed_at=1_000.0 + i) for i in range(100)]
    )

    assert written == 1
    assert await count_rows(any_db) == 1


async def test_a_batch_out_of_order_folds_oldest_first(any_db):
    """Handed 88 → 89 → 88 shuffled, the stored series still reads 88, 89, 88."""
    written = await any_db.record_provider_usage(
        [
            snap(used_percent=89.0, observed_at=1_060.0),
            snap(used_percent=88.0, observed_at=1_120.0),
            snap(used_percent=88.0, observed_at=1_000.0),
        ]
    )

    assert written == 3
    series = await any_db.provider_usage_series("codex", "primary")
    assert [row["used_percent"] for row in series] == [88.0, 89.0, 88.0]


async def test_each_series_dedups_independently(any_db):
    await any_db.record_provider_usage(
        [
            snap(provider="claude", window="week", scope="all models", used_percent=45.0),
            snap(provider="claude", window="week", scope="Fable", used_percent=81.0),
        ]
    )

    # "all models" holds; "Fable" moves.  One write, not zero and not two.
    written = await any_db.record_provider_usage(
        [
            snap(
                provider="claude",
                window="week",
                scope="all models",
                used_percent=45.0,
                observed_at=1_600.0,
            ),
            snap(
                provider="claude",
                window="week",
                scope="Fable",
                used_percent=82.0,
                observed_at=1_600.0,
            ),
        ]
    )

    assert written == 1
    assert await count_rows(any_db) == 3


async def test_dedup_ignores_the_plan_label_and_the_producer(any_db):
    """A plan rename, or the same window arriving from the other producer, is
    not new information about the quota."""
    await any_db.record_provider_usage([snap(account_label="pro", source="transcript")])

    written = await any_db.record_provider_usage(
        [snap(account_label="max", source="probe", observed_at=1_060.0)]
    )

    assert written == 0


async def test_a_mapping_is_accepted_in_place_of_a_dataclass(any_db):
    written = await any_db.record_provider_usage(
        [
            {
                "provider": "codex",
                "window": "primary",
                "used_percent": 88.0,
                "observed_at": 1_000.0,
                "source": "transcript",
            }
        ]
    )

    assert written == 1
    row = (await any_db.latest_provider_usage())[0]
    assert row["scope"] == ""
    assert row["account_label"] == ""
    assert row["resets_at"] is None


async def test_an_unknown_source_is_refused_by_the_check_constraint(any_db):
    with pytest.raises((IntegrityError, DBAPIError)):
        await any_db.record_provider_usage([snap(source="guessed")])


# --- latest ----------------------------------------------------------------


async def test_latest_returns_exactly_one_row_per_series(any_db):
    for observed_at, pct in ((1_000.0, 1.0), (1_100.0, 2.0), (1_200.0, 3.0)):
        await any_db.record_provider_usage(
            [
                snap(used_percent=pct, observed_at=observed_at),
                snap(
                    provider="claude",
                    window="session",
                    used_percent=pct,
                    observed_at=observed_at,
                ),
                snap(
                    provider="claude",
                    window="week",
                    scope="Fable",
                    used_percent=pct,
                    observed_at=observed_at,
                ),
            ]
        )

    rows = await any_db.latest_provider_usage()

    assert len(rows) == 3
    assert {(r["provider"], r["window"], r["scope"]) for r in rows} == {
        ("codex", "primary", ""),
        ("claude", "session", ""),
        ("claude", "week", "Fable"),
    }
    assert {r["used_percent"] for r in rows} == {3.0}
    assert {r["observed_at"] for r in rows} == {1_200.0}


async def test_latest_filters_by_provider(any_db):
    await any_db.record_provider_usage(
        [snap(), snap(provider="claude", window="session", used_percent=5.0)]
    )

    rows = await any_db.latest_provider_usage(provider="claude")

    assert [r["provider"] for r in rows] == ["claude"]


async def test_latest_on_an_empty_table_is_an_empty_list(any_db):
    assert await any_db.latest_provider_usage() == []


async def test_latest_breaks_an_observed_at_tie_on_the_later_insert(any_db):
    """Two rows can share a timestamp when one transcript line yields both."""
    async with any_db._engine.begin() as conn:
        await conn.execute(
            insert(provider_usage_snapshots),
            [
                {
                    "provider": "codex",
                    "account_label": "pro",
                    "window": "primary",
                    "scope": "",
                    "used_percent": pct,
                    "resets_at": None,
                    "observed_at": 1_000.0,
                    "source": "transcript",
                }
                for pct in (10.0, 20.0)
            ],
        )

    rows = await any_db.latest_provider_usage()

    assert len(rows) == 1
    assert rows[0]["used_percent"] == 20.0


# --- series ----------------------------------------------------------------


async def test_series_returns_one_series_oldest_first_from_since(any_db):
    await any_db.record_provider_usage(
        [snap(used_percent=float(i), observed_at=1_000.0 + i) for i in range(5)]
    )
    await any_db.record_provider_usage(
        [snap(provider="claude", window="session", used_percent=99.0)]
    )

    rows = await any_db.provider_usage_series("codex", "primary", since=1_002.0)

    assert [r["observed_at"] for r in rows] == [1_002.0, 1_003.0, 1_004.0]
    assert {r["provider"] for r in rows} == {"codex"}


async def test_series_of_an_unknown_key_is_empty(any_db):
    await any_db.record_provider_usage([snap()])

    assert await any_db.provider_usage_series("codex", "secondary") == []


# --- purge -----------------------------------------------------------------


async def test_purge_drops_only_rows_older_than_the_horizon(any_db):
    await any_db.record_provider_usage(
        [snap(used_percent=float(i), observed_at=1_000.0 + i) for i in range(5)]
    )

    dropped = await any_db.purge_provider_usage(1_003.0)

    assert dropped == 3
    remaining = await any_db.provider_usage_series("codex", "primary")
    assert [r["observed_at"] for r in remaining] == [1_003.0, 1_004.0]


async def test_purge_is_bounded_by_limit_and_takes_the_oldest_first(any_db):
    await any_db.record_provider_usage(
        [snap(used_percent=float(i), observed_at=1_000.0 + i) for i in range(5)]
    )

    assert await any_db.purge_provider_usage(9_999.0, limit=2) == 2
    remaining = await any_db.provider_usage_series("codex", "primary")
    assert [r["observed_at"] for r in remaining] == [1_002.0, 1_003.0, 1_004.0]

    # The caller re-runs until it returns 0 -- that is the whole contract.
    assert await any_db.purge_provider_usage(9_999.0, limit=2) == 2
    assert await any_db.purge_provider_usage(9_999.0, limit=2) == 1
    assert await any_db.purge_provider_usage(9_999.0, limit=2) == 0
