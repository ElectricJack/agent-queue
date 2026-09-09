"""``GET /api/providers/usage`` --- newest reading per series, staleness included.

The route itself is a thin fold over ``latest_provider_usage``; what is worth
testing is the judgement it adds.  ``stale`` is computed here so the card, the
doctor check and any other client agree, and the two horizons are deliberately
different (a Codex number only advances while a Codex session is live), so the
tests below pin the horizon per provider rather than one shared number.

The staleness clock reads ``last_seen_at``, not ``observed_at`` --- see spec
amendment A3 and ``test_staleness_is_measured_from_last_seen_at_not_observed_at``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.providers import (
    DEFAULT_CLAUDE_STALE_AFTER,
    DEFAULT_CODEX_STALE_AFTER,
    build_providers_router,
    is_stale,
    series_key,
)
from src.config import AppConfig
from src.database import Database
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("p.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def client_factory(db):
    def _make(config=None) -> AsyncClient:
        app = FastAPI()
        app.include_router(build_providers_router(db=db, config=config))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    return _make


def snapshot(**overrides) -> dict:
    row = {
        "provider": "claude",
        "account_label": "pro",
        "window": "week",
        "scope": "all models",
        "used_percent": 46.0,
        "resets_at": NOW + 3600.0,
        "observed_at": NOW,
        "source": "probe",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# the empty case
# ---------------------------------------------------------------------------


async def test_an_empty_table_returns_an_empty_list_rather_than_an_error(client_factory):
    """Every install starts here; a 404 would render as a broken feature."""
    async with client_factory() as client:
        response = await client.get("/api/providers/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["snapshots"] == []
    assert body["series"] == {}
    assert body["now"] > 0


# ---------------------------------------------------------------------------
# latest-per-series
# ---------------------------------------------------------------------------


async def test_only_the_newest_reading_of_each_series_is_returned(db, client_factory):
    await db.record_provider_usage(
        [
            snapshot(used_percent=40.0, observed_at=NOW - 600),
            snapshot(used_percent=46.0, observed_at=NOW - 300),
            snapshot(window="session", used_percent=6.0, scope="", observed_at=NOW - 60),
            snapshot(provider="codex", window="primary", scope="", used_percent=88.0),
        ]
    )
    async with client_factory() as client:
        body = (await client.get("/api/providers/usage")).json()

    by_key = {series_key(row): row for row in body["snapshots"]}
    assert set(by_key) == {"claude/week/all models", "claude/session/", "codex/primary/"}
    # The 40% reading is superseded, not merged: a card shows the latest value.
    assert by_key["claude/week/all models"]["used_percent"] == 46.0


async def test_the_provider_filter_narrows_to_one_provider(db, client_factory):
    await db.record_provider_usage(
        [snapshot(), snapshot(provider="codex", window="primary", scope="")]
    )
    async with client_factory() as client:
        body = (await client.get("/api/providers/usage?provider=codex")).json()
    assert [row["provider"] for row in body["snapshots"]] == ["codex"]


async def test_a_null_resets_at_survives_the_response_model(db, client_factory):
    """A percentage without a clock still beats no reading at all."""
    await db.record_provider_usage([snapshot(resets_at=None)])
    async with client_factory() as client:
        body = (await client.get("/api/providers/usage")).json()
    assert body["snapshots"][0]["resets_at"] is None


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------


async def test_a_seeded_stale_row_reports_stale_true(db, client_factory):
    import time

    old = time.time() - (DEFAULT_CLAUDE_STALE_AFTER + 60)
    await db.record_provider_usage([snapshot(observed_at=old)])
    async with client_factory() as client:
        body = (await client.get("/api/providers/usage")).json()
    row = body["snapshots"][0]
    assert row["stale"] is True
    assert row["age_seconds"] >= DEFAULT_CLAUDE_STALE_AFTER


async def test_a_fresh_row_reports_stale_false(db, client_factory):
    import time

    await db.record_provider_usage([snapshot(observed_at=time.time() - 30)])
    async with client_factory() as client:
        body = (await client.get("/api/providers/usage")).json()
    assert body["snapshots"][0]["stale"] is False


def test_codex_gets_a_longer_horizon_than_claude():
    """A Codex number only advances while a Codex session is live.

    Holding it to the probe's horizon would mark every Codex card stale
    overnight and teach the operator to ignore the word.
    """
    age = DEFAULT_CLAUDE_STALE_AFTER + 60
    assert is_stale(snapshot(last_seen_at=NOW - age), NOW) is True
    codex = snapshot(provider="codex", window="primary", last_seen_at=NOW - age)
    assert is_stale(codex, NOW) is False
    older = snapshot(provider="codex", window="primary", last_seen_at=NOW - DEFAULT_CODEX_STALE_AFTER - 60)
    assert is_stale(older, NOW) is True


def test_staleness_is_measured_from_last_seen_at_not_observed_at():
    """Spec amendment A3.

    The writer drops a reading identical to the newest stored row, so a window
    steady at 81% all afternoon keeps an ``observed_at`` hours old while the
    probe confirms it every ten minutes.  Measuring from ``observed_at`` would
    mute a live card.
    """
    row = snapshot(
        observed_at=NOW - 6 * 3600,
        last_seen_at=NOW - 120,
    )
    assert is_stale(row, NOW) is False


def test_last_seen_at_falls_back_to_observed_at_when_the_column_is_absent():
    """Rows written before ``last_seen_at`` existed still report an honest age.

    Defaulting the missing value to 0 would date every such row to 1970 and
    call the whole table stale.
    """
    row = snapshot(observed_at=NOW - 60)
    row.pop("last_seen_at", None)
    assert is_stale(row, NOW) is False


def test_a_reading_from_the_future_is_fresh_not_stale():
    """Clock skew between the writer's box and ours must not wrap the verdict."""
    assert is_stale(snapshot(last_seen_at=NOW + 300), NOW) is False


def test_the_horizons_come_from_config_when_one_is_supplied():
    config = AppConfig()
    config.providers.claude.stale_after_seconds = 60.0
    row = snapshot(last_seen_at=NOW - 120)
    assert is_stale(row, NOW, config) is True
    config.providers.claude.stale_after_seconds = 600.0
    assert is_stale(row, NOW, config) is False


async def test_age_seconds_never_goes_negative(db, client_factory):
    import time

    await db.record_provider_usage([snapshot(observed_at=time.time() + 600)])
    async with client_factory() as client:
        body = (await client.get("/api/providers/usage")).json()
    assert body["snapshots"][0]["age_seconds"] == 0.0


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


async def test_history_is_returned_only_when_since_is_given(db, client_factory):
    await db.record_provider_usage(
        [
            snapshot(used_percent=40.0, observed_at=NOW - 600),
            snapshot(used_percent=46.0, observed_at=NOW - 300),
        ]
    )
    async with client_factory() as client:
        default = (await client.get("/api/providers/usage")).json()
        with_history = (await client.get(f"/api/providers/usage?since={NOW - 900}")).json()

    assert default["series"] == {}
    assert [row["used_percent"] for row in with_history["series"]["claude/week/all models"]] == [
        40.0,
        46.0,
    ]


async def test_history_is_clipped_at_since(db, client_factory):
    await db.record_provider_usage(
        [
            snapshot(used_percent=40.0, observed_at=NOW - 600),
            snapshot(used_percent=46.0, observed_at=NOW - 300),
        ]
    )
    async with client_factory() as client:
        body = (await client.get(f"/api/providers/usage?since={NOW - 400}")).json()
    assert [row["used_percent"] for row in body["series"]["claude/week/all models"]] == [46.0]


async def test_a_negative_since_is_rejected(client_factory):
    async with client_factory() as client:
        response = await client.get("/api/providers/usage?since=-1")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_the_route_is_registered_on_the_real_app():
    """A router that only tests include is a route nobody can call."""
    from src.api.spec import build_openapi_spec

    assert "/api/providers/usage" in build_openapi_spec()["paths"]
