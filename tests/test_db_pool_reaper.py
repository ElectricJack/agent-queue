"""Crash recovery for the template-cloned PostgreSQL lease pool."""

from __future__ import annotations

import pytest

from tests import db_fixtures


class _AdminConnection:
    def __init__(
        self,
        names: list[str],
        live_tokens: set[str] = frozenset(),
        *,
        sweep_busy: bool = False,
    ):
        self.names = names
        self.live_tokens = live_tokens
        self.sweep_busy = sweep_busy
        self.held: set[int] = set()
        self.dropped: list[str] = []
        self.closed = False

    async def execute(self, statement: str, *args):
        if statement == "SELECT pg_advisory_lock($1)":
            self.held.add(args[0])
        elif statement == "SELECT pg_advisory_unlock($1)":
            self.held.remove(args[0])
        elif statement in ("SET statement_timeout = 5000", "RESET statement_timeout"):
            pass
        elif statement.startswith('DROP DATABASE IF EXISTS "'):
            self.dropped.append(statement.split('"')[1])
        else:
            raise AssertionError(statement)

    async def fetch(self, statement: str):
        assert "pg_database" in statement
        return [{"datname": name} for name in self.names]

    async def fetchval(self, statement: str, key: int):
        assert statement == "SELECT pg_try_advisory_lock($1)"
        if key == db_fixtures._REAP_LOCK_KEY and self.sweep_busy:
            return False
        if any(key == db_fixtures._pool_lock_key(token) for token in self.live_tokens):
            return False
        self.held.add(key)
        return True

    async def close(self):
        self.held.clear()
        self.closed = True


@pytest.mark.asyncio
async def test_pool_reaps_only_unlocked_versioned_names(monkeypatch):
    stale = "abc123def456"
    live = "123abc456def"
    names = [
        f"aq_test_poolv2_{stale}_gw0_0",
        f"aq_test_poolv2_{stale}_gw0_1",
        f"aq_test_poolv2_{live}_master_0",
        "aq_test_deadbeef1234_master_0",  # legacy: no liveness lock
        "aq_tmpl_schema_key",
        f"aq_test_poolv2_{stale}_gw0_bad",  # malformed index
    ]
    conn = _AdminConnection(names, {live})

    async def connect(_dsn):
        return conn

    monkeypatch.setattr(db_fixtures, "_connect_admin", connect)
    pool = db_fixtures.LeasePool("postgresql://u:p@h/worker", "gw1")
    conn.names.append(pool._name(0))  # pg_try_advisory_lock is reentrant on our own connection
    await pool._start()

    assert conn.dropped == names[:2]
    assert conn.held == {db_fixtures._pool_lock_key(pool._run_id)}
    assert not conn.closed
    await pool.dispose()
    assert conn.closed


@pytest.mark.asyncio
async def test_pool_skips_cleanup_when_another_worker_is_sweeping(monkeypatch):
    name = "aq_test_poolv2_abc123def456_gw0_0"
    conn = _AdminConnection([name], sweep_busy=True)

    async def connect(_dsn):
        return conn

    monkeypatch.setattr(db_fixtures, "_connect_admin", connect)
    pool = db_fixtures.LeasePool("postgresql://u:p@h/worker", "gw1")
    await pool._start()

    assert conn.dropped == []
    assert conn.held == {db_fixtures._pool_lock_key(pool._run_id)}
    await pool.dispose()


@pytest.mark.asyncio
async def test_pool_locks_before_clone_and_releases_after_drop(monkeypatch):
    conn = _AdminConnection([])
    events: list[str] = []

    async def connect(_dsn):
        return conn

    async def clone(_dsn, name):
        assert db_fixtures._pool_lock_key(pool._run_id) in conn.held
        events.append("clone")
        return f"postgresql://u:p@h/{name}"

    async def drop(_dsn, name):
        assert db_fixtures._pool_lock_key(pool._run_id) in conn.held
        events.append(f"drop:{name}")

    monkeypatch.setattr(db_fixtures, "_connect_admin", connect)
    monkeypatch.setattr(db_fixtures, "clone_database", clone)
    monkeypatch.setattr(db_fixtures, "drop_database", drop)
    monkeypatch.setattr(db_fixtures, "_SEED", {})
    pool = db_fixtures.LeasePool("postgresql://u:p@h/worker", "worker name")
    name = pool._name(0)
    assert db_fixtures._POOL_NAME_RE.fullmatch(name)

    await pool.acquire()
    await pool.dispose()

    assert events == ["clone", f"drop:{name}"]
    assert conn.closed


@pytest.mark.asyncio
async def test_failed_reap_warns_and_keeps_pool_usable(monkeypatch):
    conn = _AdminConnection(["aq_test_poolv2_abc123def456_gw0_0"])

    async def connect(_dsn):
        return conn

    async def fail_drop(statement: str, *args):
        if statement.startswith("DROP DATABASE"):
            raise RuntimeError("drop failed")
        await _AdminConnection.execute(conn, statement, *args)

    monkeypatch.setattr(db_fixtures, "_connect_admin", connect)
    monkeypatch.setattr(conn, "execute", fail_drop)
    pool = db_fixtures.LeasePool("postgresql://u:p@h/worker", "gw1")

    with pytest.warns(RuntimeWarning, match="drop failed"):
        await pool._start()

    assert pool._guard is conn
    assert conn.held == {db_fixtures._pool_lock_key(pool._run_id)}
    await pool.dispose()
    assert conn.closed
