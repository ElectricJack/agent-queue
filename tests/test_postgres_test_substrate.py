"""Parallel test workers must not destroy one another's databases."""

import asyncio
import uuid

import pytest

from tests import db_fixtures
from tests.db_fixtures import LeasePool, lease_dsn


def test_separate_test_runs_have_distinct_lease_names():
    dsn = lease_dsn("base")
    first = LeasePool(dsn, "gw0")
    second = LeasePool(dsn, "gw0")
    assert first._name(0) != second._name(0)


async def test_clone_refuses_to_drop_an_existing_unowned_database(monkeypatch):
    executed: list[str] = []

    class Connection:
        async def execute(self, statement):
            executed.append(statement)

        async def close(self):
            return None

    async def _connect(_dsn):
        return Connection()

    async def _exists(_conn, _name):
        return True

    monkeypatch.setattr(db_fixtures, "ensure_template", lambda _dsn: _async_value("template"))
    monkeypatch.setattr(db_fixtures, "_connect_admin", _connect)
    monkeypatch.setattr(db_fixtures, "_database_exists", _exists)

    with pytest.raises(RuntimeError, match="not owned by this lease pool"):
        await db_fixtures.clone_database("postgresql://u:p@h/postgres", "foreign")

    assert executed == []


async def test_template_creation_coordinates_across_worker_databases(monkeypatch):
    name = "aq_tmpl_race_" + uuid.uuid4().hex[:12]
    monkeypatch.setattr(db_fixtures, "template_name", lambda: name)
    monkeypatch.setattr(db_fixtures, "_TEMPLATE_READY", None)
    first, second = lease_dsn("first"), lease_dsn("second")
    try:
        results = await asyncio.gather(
            db_fixtures.ensure_template(first),
            db_fixtures.ensure_template(second),
        )
        assert results == [name, name]
    finally:
        conn = await db_fixtures._connect_admin(first)
        try:
            await conn.execute(
                "UPDATE pg_database SET datistemplate = false WHERE datname = $1", name
            )
        finally:
            await conn.close()
        await db_fixtures.drop_database(first, name)


async def _relfilenodes(conn) -> dict[int, int]:
    rows = await conn.fetch(
        "SELECT oid, relfilenode FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relfilenode <> 0"
    )
    return {row["oid"]: row["relfilenode"] for row in rows}


async def test_reset_empties_guarded_tables_without_new_files():
    """The per-test reset must not hand the next checkpoint new files (bold-harbor).

    ``TRUNCATE ... CASCADE`` gave the whole foreign-key closure new
    relfilenodes -- 279 files after a test that wrote one ``projects`` row --
    and every ``DROP DATABASE`` on the shared server then waited for their
    fsyncs.  The rollout row sits behind a ``RESTRICT`` key and a
    DELETE-refusing guard trigger, so it also proves the reset needs neither
    a delete order nor the guard's permission.
    """
    dsn = lease_dsn("reset")
    conn = await db_fixtures._connect_admin(dsn)
    try:
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('p1', 'p1', 0)")
        await conn.execute(
            "INSERT INTO integration_rollout_transitions (id, project_id, generation, "
            "old_effective_mode, new_effective_mode, old_desired_mode, new_desired_mode, "
            "operator_id, reason, blocker_digest, old_legacy_policy, new_legacy_policy, "
            "created_at) VALUES ('t1', 'p1', 1, 'disabled', 'observe', 'disabled', "
            "'observe', 'op', 'r', $1, '{}', '{}', 0)",
            "sha256:" + "0" * 64,
        )
        before = await _relfilenodes(conn)
    finally:
        await conn.close()

    await db_fixtures.reset_all(dsn)

    conn = await db_fixtures._connect_admin(dsn)
    try:
        assert await db_fixtures._non_empty_tables(conn) == []
        assert await _relfilenodes(conn) == before
        # VACUUM hands the emptied heap back at zero pages -- the physical
        # state a truncate left, which the perf suites' buffer counts assume.
        assert await conn.fetchval("SELECT pg_relation_size('projects')") == 0
    finally:
        await conn.close()


async def test_reset_restarts_used_identity_sequences():
    dsn = lease_dsn("sequences")
    insert = "INSERT INTO events (event_type, timestamp) VALUES ('probe', 0) RETURNING id"
    conn = await db_fixtures._connect_admin(dsn)
    try:
        await conn.fetchval(insert)
        assert await conn.fetchval(insert) == 2
    finally:
        await conn.close()

    await db_fixtures.reset_all(dsn)

    conn = await db_fixtures._connect_admin(dsn)
    try:
        assert await conn.fetchval(insert) == 1
    finally:
        await conn.close()


async def test_reset_leaves_planner_stats_as_a_truncate_did():
    """Emptied tables must read as never vacuumed, as a truncate left them.

    VACUUM records them as vacuumed-empty (``reltuples = 0``), which turns off
    the planner's minimum-size guess for fresh tables: a table the next test
    fills looks tiny, a cached foreign-key check on ``tasks`` became a
    sequential scan per row, and a 20k-edge bulk load took 18s instead of 2s.
    """
    dsn = lease_dsn("stats")
    stats = (
        "SELECT relname, relpages, reltuples FROM pg_class "
        "WHERE relname IN ('tasks', 'tasks_pkey', 'idx_tasks_claim_frontier') ORDER BY relname"
    )
    conn = await db_fixtures._connect_admin(dsn)
    try:
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('p1', 'p1', 0)")
        await conn.execute(
            "INSERT INTO tasks (id, project_id, title, description, created_at, updated_at) "
            "SELECT 't' || g, 'p1', 't', '', 0, 0 FROM generate_series(1, 500) g"
        )
    finally:
        await conn.close()

    await db_fixtures.reset_all(dsn)

    conn = await db_fixtures._connect_admin(dsn)
    try:
        rows = [tuple(row) for row in await conn.fetch(stats)]
    finally:
        await conn.close()
    assert rows == [
        ("idx_tasks_claim_frontier", 0, -1.0),
        ("tasks", 0, -1.0),
        ("tasks_pkey", 0, -1.0),
    ]


@pytest.mark.parametrize(
    ("has_clear_stats", "reason"),
    [
        (False, "pg_clear_relation_stats"),
        (True, "session_replication_role"),
    ],
)
async def test_reset_falls_back_to_truncate_when_the_row_reset_is_unavailable(
    monkeypatch, has_clear_stats, reason
):
    import asyncpg

    executed: list[str] = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class Connection:
        async def fetchval(self, query):
            assert "pg_clear_relation_stats" in query
            return has_clear_stats

        async def fetch(self, query):
            if "pg_tables" in query:
                return [{"tablename": "projects"}]
            return [{"t": "projects"}]

        def transaction(self):
            return Transaction()

        async def execute(self, statement, *_args):
            if "session_replication_role" in statement:
                raise asyncpg.exceptions.InsufficientPrivilegeError(
                    'permission denied to set parameter "session_replication_role"'
                )
            executed.append(statement)

        async def close(self):
            return None

    async def _connect(_dsn):
        return Connection()

    monkeypatch.setattr(db_fixtures, "_connect_admin", _connect)
    monkeypatch.setattr(db_fixtures, "_ROW_RESET", None)

    with pytest.warns(RuntimeWarning, match=reason):
        await db_fixtures.reset_all("postgresql://u:p@h/leased")
    await db_fixtures.reset_all("postgresql://u:p@h/leased")

    assert executed == [db_fixtures._TRUNCATE_ALL, db_fixtures._TRUNCATE_ALL]


async def test_pool_dispose_drops_its_clones_together(monkeypatch):
    """Four clones dropped in turn waited for four server-wide checkpoints."""
    import sys
    from types import SimpleNamespace

    started: list[str] = []
    all_started = asyncio.Event()
    pool = LeasePool("postgresql+asyncpg://u:p@h/worker", "gw0")
    pool._created = {pool._name(index) for index in range(4)}

    class Connection:
        async def execute(self, statement):
            started.append(statement)
            if len(started) == 4:
                all_started.set()
            await all_started.wait()

        async def close(self):
            return None

    async def _connect(_dsn):
        return Connection()

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=_connect))

    await asyncio.wait_for(pool.dispose(), timeout=5)

    assert len(started) == 4
    assert pool._created == set()


async def _async_value(value):
    return value
