"""Run ownership and collision safety for the PostgreSQL test substrate."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

import tests.pg_dsn as pg_dsn


def _reset_derivation(monkeypatch, *, run_id: str = "run_abc") -> None:
    monkeypatch.setattr(pg_dsn, "_CACHED_DSN", pg_dsn._UNSET)
    monkeypatch.setattr(pg_dsn, "_CACHED_RUN_ID", pg_dsn._UNSET)
    monkeypatch.setenv("POSTGRES_TEST_DSN", "postgresql://u:p@h:5432/aqtest")
    monkeypatch.setenv("AQ_TEST_RUN_ID", run_id)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")


def test_repeated_calls_return_the_same_run_owned_dsn(monkeypatch):
    _reset_derivation(monkeypatch)
    created: list[tuple[str, str]] = []

    async def _fake_create(base_dsn, target_db):
        created.append((base_dsn, target_db))

    monkeypatch.setattr(pg_dsn, "_create_owned_database", _fake_create)

    first = pg_dsn.ensure_worker_postgres_dsn()
    second = pg_dsn.ensure_worker_postgres_dsn()

    assert first == "postgresql://u:p@h:5432/aq_test_aqtest_run_abc_gw3"
    assert first == second
    assert pg_dsn.os.environ["POSTGRES_TEST_DSN"] == first
    assert created == [("postgresql://u:p@h:5432/aqtest", "aq_test_aqtest_run_abc_gw3")]


def test_concurrent_runs_derive_distinct_worker_databases(monkeypatch):
    created: list[str] = []

    async def _fake_create(_base_dsn, target_db):
        created.append(target_db)

    monkeypatch.setattr(pg_dsn, "_create_owned_database", _fake_create)
    for run_id in ("run_one", "run_two"):
        _reset_derivation(monkeypatch, run_id=run_id)
        pg_dsn.ensure_worker_postgres_dsn()

    assert created == ["aq_test_aqtest_run_one_gw3", "aq_test_aqtest_run_two_gw3"]


def test_unset_dsn_is_cached_as_none(monkeypatch):
    monkeypatch.setattr(pg_dsn, "_CACHED_DSN", pg_dsn._UNSET)
    monkeypatch.delenv("POSTGRES_TEST_DSN", raising=False)
    monkeypatch.delenv("AQ_REQUIRE_POSTGRES_TESTS", raising=False)
    assert pg_dsn.ensure_worker_postgres_dsn() is None
    assert pg_dsn.ensure_worker_postgres_dsn() is None


def test_required_dsn_guard_remains_actionable_outside_pytest_preflight(monkeypatch):
    monkeypatch.setattr(pg_dsn, "_CACHED_DSN", pg_dsn._UNSET)
    monkeypatch.delenv("POSTGRES_TEST_DSN", raising=False)
    monkeypatch.setenv("AQ_REQUIRE_POSTGRES_TESTS", "1")

    with pytest.raises(RuntimeError, match="docker compose up -d postgres"):
        pg_dsn.ensure_worker_postgres_dsn()


async def test_scratch_databases_are_unique_even_for_the_same_suffix(monkeypatch):
    monkeypatch.setattr(pg_dsn, "_CACHED_DSN", "postgresql://u:p@h:5432/worker")
    tokens = iter(("token_one", "token_two"))
    monkeypatch.setattr(pg_dsn, "_unique_token", lambda: next(tokens))
    created: list[str] = []

    async def _fake_create(_base_dsn, target_db):
        created.append(target_db)

    monkeypatch.setattr(pg_dsn, "_create_owned_database", _fake_create)

    first = await pg_dsn.create_scratch_database("migration")
    second = await pg_dsn.create_scratch_database("migration")

    assert first != second
    assert created == [
        "aq_test_worker_scratch_migration_token_one",
        "aq_test_worker_scratch_migration_token_two",
    ]


async def test_existing_stale_database_is_diagnosed_without_mutation(monkeypatch):
    class Connection:
        executed: list[str] = []

        async def fetchval(self, *_args):
            return 1

        async def execute(self, statement):
            self.executed.append(statement)

        async def close(self):
            return None

    connection = Connection()
    fake_asyncpg = SimpleNamespace(
        connect=lambda _dsn: _async_value(connection),
        exceptions=SimpleNamespace(DuplicateDatabaseError=RuntimeError),
    )
    monkeypatch.setitem(pg_dsn.__dict__, "_OWNED_DATABASES", [])
    monkeypatch.setitem(__import__("sys").modules, "asyncpg", fake_asyncpg)

    async def _unknown_revision(_base_dsn, _target):
        return "alembic_version contains unknown revision(s) ['foreign_rev']"

    monkeypatch.setattr(pg_dsn, "_revision_diagnostic", _unknown_revision)

    with pytest.raises(RuntimeError, match="unknown revision.*foreign_rev") as excinfo:
        await pg_dsn._create_owned_database("postgresql://u:p@h/base", "claimed_name")

    assert connection.executed == []
    assert pg_dsn._OWNED_DATABASES == []
    assert "was not dropped or stamped" in str(excinfo.value)


async def test_revision_diagnostic_distinguishes_stale_and_unknown(monkeypatch):
    class Connection:
        def __init__(self, revision):
            self.revision = revision

        async def fetchval(self, *_args):
            return "alembic_version"

        async def fetch(self, *_args):
            return [{"version_num": self.revision}]

        async def close(self):
            return None

    revisions = iter(("known_old", "foreign_rev"))

    async def _connect(_dsn):
        return Connection(next(revisions))

    monkeypatch.setitem(
        __import__("sys").modules,
        "asyncpg",
        SimpleNamespace(connect=_connect),
    )
    monkeypatch.setattr(
        pg_dsn,
        "_known_revisions",
        lambda: (frozenset({"known_old", "head"}), frozenset({"head"})),
    )

    stale = await pg_dsn._revision_diagnostic("postgresql://u:p@h/base", "target")
    unknown = await pg_dsn._revision_diagnostic("postgresql://u:p@h/base", "target")

    assert "stale" in stale and "known_old" in stale
    assert "unknown revision" in unknown and "foreign_rev" in unknown


async def test_cleanup_drops_only_registered_databases_in_reverse_order(monkeypatch):
    executed: list[str] = []

    class Connection:
        async def execute(self, statement):
            executed.append(statement)

        def terminate(self):
            return None

    async def _connect(_dsn, **_kwargs):
        return Connection()

    owned = [("postgresql://u:p@h/postgres", "worker"), ("postgresql://u:p@h/postgres", "scratch")]
    monkeypatch.setattr(pg_dsn, "_OWNED_DATABASES", owned)
    monkeypatch.setitem(
        __import__("sys").modules,
        "asyncpg",
        SimpleNamespace(connect=_connect),
    )

    await pg_dsn.dispose_owned_databases()

    assert executed == [
        'DROP DATABASE IF EXISTS "scratch" WITH (FORCE)',
        'DROP DATABASE IF EXISTS "worker" WITH (FORCE)',
    ]
    assert owned == []
    assert all("unowned" not in statement for statement in executed)


async def test_cleanup_issues_every_drop_at_once_so_they_share_a_checkpoint(monkeypatch):
    """``DROP DATABASE`` waits for a server-wide checkpoint (bold-harbor).

    Issued in turn, a process owning N databases waits for N checkpoints --
    each owing every file the whole server dirtied since the last.  Issued
    together they share one.  Each fake drop blocks until every drop has
    started, so a serial teardown never finishes.
    """
    names = [f"db{i}" for i in range(5)]
    started: list[str] = []
    all_started = asyncio.Event()

    class Connection:
        async def execute(self, statement):
            started.append(statement)
            if len(started) == len(names):
                all_started.set()
            await all_started.wait()

        def terminate(self):
            return None

    async def _connect(_dsn, **_kwargs):
        return Connection()

    owned = [("postgresql://u:p@h/postgres", name) for name in names]
    monkeypatch.setattr(pg_dsn, "_OWNED_DATABASES", owned)
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=_connect))

    await asyncio.wait_for(pg_dsn.dispose_owned_databases(), timeout=5)

    assert sorted(started) == sorted(
        f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)' for name in names
    )
    assert owned == []


async def test_concurrent_drops_are_bounded(monkeypatch):
    in_flight = 0
    peak = 0

    class Connection:
        async def execute(self, _statement):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1

        def terminate(self):
            return None

    async def _connect(_dsn, **_kwargs):
        return Connection()

    monkeypatch.setattr(pg_dsn, "_DROP_CONCURRENCY", 3)
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=_connect))

    failures = await pg_dsn.drop_databases(
        [("postgresql://u:p@h/postgres", f"db{i}") for i in range(7)]
    )

    assert failures == []
    assert peak == 3


async def test_cleanup_attempts_every_drop_and_reports_each_failure(monkeypatch):
    executed: list[str] = []

    class Connection:
        async def execute(self, statement):
            executed.append(statement)
            if '"broken"' in statement:
                raise OSError("server went away")

        def terminate(self):
            return None

    async def _connect(_dsn, **_kwargs):
        return Connection()

    owned = [("postgresql://u:p@h/postgres", name) for name in ("first", "broken", "last")]
    monkeypatch.setattr(pg_dsn, "_OWNED_DATABASES", owned)
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=_connect))

    with pytest.raises(RuntimeError, match="broken: OSError: server went away"):
        await pg_dsn.dispose_owned_databases()

    assert len(executed) == 3
    assert owned == []


async def test_cleanup_deadline_cancels_a_checkpoint_wait(monkeypatch):
    connection = None

    class Connection:
        terminated = False

        async def execute(self, _statement):
            await asyncio.sleep(30)

        def terminate(self):
            self.terminated = True

    async def _connect(_dsn, **_kwargs):
        nonlocal connection
        connection = Connection()
        return connection

    monkeypatch.setattr(pg_dsn, "CLEANUP_TOTAL_SECONDS", 0.05)
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=_connect))
    started = asyncio.get_running_loop().time()

    failures = await pg_dsn.drop_databases([("postgresql://u:p@h/postgres", "aq_test_owned")])

    assert asyncio.get_running_loop().time() - started < 1
    assert failures == ["aq_test_owned: PostgreSQL test database cleanup deadline exceeded"]
    assert connection is not None and connection.terminated


async def _async_value(value):
    return value
