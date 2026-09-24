"""Run ownership and collision safety for the PostgreSQL test substrate."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import pg_dsn

_TOKEN = "0123456789ab"
_ADMIN = "postgresql://u:p@h:5432/postgres"


@pytest.fixture(autouse=True)
def _isolated_ownership(monkeypatch):
    """Never touch this pytest process's real owner lock or owned databases."""
    monkeypatch.setattr(pg_dsn, "_OWNER", None)
    monkeypatch.setattr(pg_dsn, "_OWNER_TOKEN", _TOKEN)
    monkeypatch.setattr(pg_dsn, "_OWNED_DATABASES", [])


def _reset_derivation(monkeypatch, *, run_id: str = "run_abc") -> None:
    monkeypatch.setattr(pg_dsn, "_CACHED_DSN", pg_dsn._UNSET)
    monkeypatch.setattr(pg_dsn, "_CACHED_RUN_ID", pg_dsn._UNSET)
    monkeypatch.setenv("POSTGRES_TEST_DSN", "postgresql://u:p@h:5432/aqtest")
    monkeypatch.setenv("AQ_TEST_RUN_ID", run_id)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")


class _FakeServer:
    """Just enough PostgreSQL for ownership: databases and session advisory locks."""

    def __init__(self, databases=()):
        self.databases = set(databases)
        self.locks: dict[int, _FakeConnection] = {}
        self.log: list[str] = []
        self.drop_errors: dict[str, Exception] = {}
        self.stuck: set[str] = set()  # DROPs held behind a busy checkpointer
        self.connections: list[_FakeConnection] = []

    async def connect(self, dsn, **_kwargs):
        conn = _FakeConnection(self, dsn)
        self.connections.append(conn)
        return conn

    def drop_connection(self, conn: _FakeConnection, *, lock_taken_by=None) -> None:
        """The server ends *conn* (a restart, a reset socket); its locks go with it.

        *lock_taken_by* models a sweeper that takes a freed lock first.
        """
        conn.closed = True
        for key in [key for key, holder in self.locks.items() if holder is conn]:
            del self.locks[key]
            if lock_taken_by is not None:
                self.locks[key] = lock_taken_by
        for listener in conn.listeners:
            conn.loop.call_soon_threadsafe(listener, conn)

    def module(self):
        return SimpleNamespace(
            connect=self.connect,
            exceptions=SimpleNamespace(DuplicateDatabaseError=type("Dup", (Exception,), {})),
        )


class _FakeConnection:
    def __init__(self, server: _FakeServer, dsn: str):
        self.server = server
        self.dsn = dsn
        self.closed = False
        self.listeners: list = []
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    def add_termination_listener(self, listener) -> None:
        self.listeners.append(listener)

    def is_closed(self) -> bool:
        return self.closed

    def terminate(self) -> None:
        self.closed = True

    async def fetchval(self, statement: str, *args):
        if statement == "SELECT pg_try_advisory_lock($1)":
            holder = self.server.locks.setdefault(args[0], self)
            if holder is self:
                self.server.log.append(f"lock:{args[0]:x}")
                return True
            return False
        if statement == "SELECT 1 FROM pg_database WHERE datname = $1":
            return 1 if args[0] in self.server.databases else None
        raise AssertionError(statement)

    async def fetch(self, statement: str):
        assert "pg_database" in statement
        return [{"datname": name} for name in sorted(self.server.databases)]

    async def execute(self, statement: str, *_args):
        self.server.log.append(statement)
        name = statement.split('"')[1] if '"' in statement else None
        if statement.startswith("CREATE DATABASE"):
            self.server.databases.add(name)
        elif statement.startswith("DROP DATABASE"):
            if name in self.server.stuck:
                await asyncio.Event().wait()
            if name in self.server.drop_errors:
                raise self.server.drop_errors[name]
            self.server.databases.discard(name)
        elif not statement.startswith("SET statement_timeout"):
            raise AssertionError(statement)

    async def close(self, timeout=None):
        self.closed = True
        for key in [key for key, holder in self.server.locks.items() if holder is self]:
            del self.server.locks[key]
            self.server.log.append(f"unlock:{key:x}")
        for listener in self.listeners:  # asyncpg notifies on a deliberate close too
            asyncio.get_running_loop().call_soon(listener, self)


def _hold(server: _FakeServer, token: str) -> _FakeConnection:
    """Model a live owner: another session holding *token*'s lock."""
    other = _FakeConnection(server, _ADMIN)
    server.locks[pg_dsn._owner_lock_key(token)] = other
    return other


# ── naming ─────────────────────────────────────────────────────────────────


def test_repeated_calls_return_the_same_run_owned_dsn(monkeypatch):
    _reset_derivation(monkeypatch)
    created: list[tuple[str, str]] = []

    async def _fake_create(base_dsn, target_db):
        created.append((base_dsn, target_db))

    monkeypatch.setattr(pg_dsn, "_create_owned_database", _fake_create)

    first = pg_dsn.ensure_worker_postgres_dsn()
    second = pg_dsn.ensure_worker_postgres_dsn()

    assert first == f"postgresql://u:p@h:5432/aq_test_ownv2_{_TOKEN}_run_abc_gw3"
    assert first == second
    assert pg_dsn.os.environ["POSTGRES_TEST_DSN"] == first
    assert created == [("postgresql://u:p@h:5432/aqtest", f"aq_test_ownv2_{_TOKEN}_run_abc_gw3")]


def test_concurrent_runs_derive_distinct_worker_databases(monkeypatch):
    created: list[str] = []

    async def _fake_create(_base_dsn, target_db):
        created.append(target_db)

    monkeypatch.setattr(pg_dsn, "_create_owned_database", _fake_create)
    for run_id, token in (("run_one", "aaaaaaaaaaaa"), ("run_two", "bbbbbbbbbbbb")):
        _reset_derivation(monkeypatch, run_id=run_id)
        monkeypatch.setattr(pg_dsn, "_OWNER_TOKEN", token)  # each process draws its own
        pg_dsn.ensure_worker_postgres_dsn()

    assert created == [
        "aq_test_ownv2_aaaaaaaaaaaa_run_one_gw3",
        "aq_test_ownv2_bbbbbbbbbbbb_run_two_gw3",
    ]


def test_owner_token_is_fresh_per_process_and_names_keep_it_through_truncation(monkeypatch):
    monkeypatch.setattr(pg_dsn, "_OWNER_TOKEN", None)
    token = pg_dsn._owner_token()
    assert token == pg_dsn._owner_token()
    assert len(token) == 12 and int(token, 16) >= 0

    long_name = pg_dsn._owned_name("scratch", "x" * 80, "token_one")
    short_name = pg_dsn._owned_name("run_abc", "gw3")
    for name in (long_name, short_name):
        assert len(name) <= 63
        match = pg_dsn._OWNED_NAME_RE.fullmatch(name)
        assert match and match[1] == token
    assert long_name != pg_dsn._owned_name("scratch", "x" * 80, "token_two")


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
        f"aq_test_ownv2_{_TOKEN}_scratch_migration_token_one",
        f"aq_test_ownv2_{_TOKEN}_scratch_migration_token_two",
    ]


# ── collisions ─────────────────────────────────────────────────────────────


async def test_existing_stale_database_is_diagnosed_without_mutation(monkeypatch):
    class Connection:
        def __init__(self):
            self.executed: list[str] = []

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
    monkeypatch.setitem(__import__("sys").modules, "asyncpg", fake_asyncpg)

    class _HeldLease:
        admin_dsn = _ADMIN

        async def ensure_held(self):
            return None

    monkeypatch.setattr(pg_dsn, "_OWNER", _HeldLease())

    async def _unknown_revision(_base_dsn, _target):
        return "alembic_version contains unknown revision(s) ['foreign_rev']"

    monkeypatch.setattr(pg_dsn, "_revision_diagnostic", _unknown_revision)

    with pytest.raises(RuntimeError, match="unknown revision.*foreign_rev") as excinfo:
        await pg_dsn._create_owned_database(
            "postgresql://u:p@h:5432/base", pg_dsn._owned_name("claimed")
        )

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


async def test_a_name_outside_this_process_token_is_never_created(monkeypatch):
    server = _FakeServer()
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())

    for target in ("aq_test_ownv2_ffffffffffff_run_gw0", "aq_test_aqtest_run_gw0", "claimed"):
        with pytest.raises(ValueError, match="owner token"):
            await pg_dsn._create_owned_database("postgresql://u:p@h:5432/base", target)

    assert server.log == [] and server.connections == []
    assert pg_dsn._OWNER is None


# ── the owner lock ─────────────────────────────────────────────────────────


async def test_owner_lock_is_taken_before_the_first_create_and_kept(monkeypatch):
    server = _FakeServer()
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())
    worker = pg_dsn._owned_name("run_abc", "gw3")
    scratch = pg_dsn._owned_name("scratch", "migration", "t1")

    await pg_dsn._create_owned_database("postgresql://u:p@h:5432/aqtest", worker)
    await pg_dsn._create_owned_database(f"postgresql://u:p@h:5432/{worker}", scratch)
    lease = pg_dsn._OWNER
    try:
        key = pg_dsn._owner_lock_key(_TOKEN)
        assert server.log == [
            f"lock:{key:x}",
            f'CREATE DATABASE "{worker}"',
            f'CREATE DATABASE "{scratch}"',
        ]
        holder = server.locks[key]
        assert holder.dsn == _ADMIN and not holder.closed
        # Every holder and sweeper must meet on one database: advisory locks
        # are scoped to the database a session is connected to.
        assert {conn.dsn for conn in server.connections} == {_ADMIN}
    finally:
        await lease.aclose()


async def test_a_held_owner_token_refuses_to_create_anything(monkeypatch):
    server = _FakeServer()
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())
    _hold(server, _TOKEN)

    with pytest.raises(RuntimeError, match="already held"):
        await pg_dsn._create_owned_database(
            "postgresql://u:p@h:5432/aqtest", pg_dsn._owned_name("run_abc", "gw3")
        )

    assert not any(entry.startswith("CREATE") for entry in server.log)
    assert pg_dsn._OWNER is None
    assert all(conn.closed for conn in server.connections)


async def test_one_process_keeps_its_databases_on_one_server(monkeypatch):
    server = _FakeServer()
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())
    await pg_dsn._create_owned_database("postgresql://u:p@h:5432/a", pg_dsn._owned_name("one"))
    lease = pg_dsn._OWNER
    try:
        with pytest.raises(RuntimeError, match="one PostgreSQL server only"):
            await pg_dsn._create_owned_database(
                "postgresql://u:p@other:5432/a", pg_dsn._owned_name("two")
            )
    finally:
        await lease.aclose()


async def _eventually(predicate, what: str) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.01)


async def test_a_lost_owner_lock_is_taken_back_before_anything_else_is_created(monkeypatch):
    server = _FakeServer()
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())
    monkeypatch.setattr(pg_dsn, "_RELOCK_DEADLINE_S", 5)
    worker = pg_dsn._owned_name("run_abc", "gw3")
    await pg_dsn._create_owned_database("postgresql://u:p@h:5432/aqtest", worker)
    lease = pg_dsn._OWNER
    key = pg_dsn._owner_lock_key(_TOKEN)
    first = server.locks[key]
    try:
        server.drop_connection(first)  # e.g. the test server restarted
        await _eventually(lambda: key in server.locks, "owner lock was not taken back")

        scratch = pg_dsn._owned_name("scratch", "after_loss")
        await pg_dsn._create_owned_database(f"postgresql://u:p@h:5432/{worker}", scratch)

        second = server.locks[key]
        assert second is not first and not second.closed
        assert scratch in server.databases
    finally:
        await lease.aclose()
    assert key not in server.locks


async def test_an_owner_lock_a_sweeper_took_first_refuses_new_databases(monkeypatch):
    server = _FakeServer()
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())
    worker = pg_dsn._owned_name("run_abc", "gw3")
    await pg_dsn._create_owned_database("postgresql://u:p@h:5432/aqtest", worker)
    lease = pg_dsn._OWNER
    key = pg_dsn._owner_lock_key(_TOKEN)
    sweeper = _FakeConnection(server, _ADMIN)
    try:
        server.drop_connection(server.locks[key], lock_taken_by=sweeper)

        scratch = pg_dsn._owned_name("scratch", "after_loss")
        with pytest.raises(RuntimeError, match="lost this process's PostgreSQL test owner lock"):
            await pg_dsn._create_owned_database(f"postgresql://u:p@h:5432/{worker}", scratch)
        assert scratch not in server.databases
        assert "already held" in lease.lost
    finally:
        await lease.aclose()
    assert server.locks == {key: sweeper}


async def test_cleanup_drops_only_registered_databases_in_reverse_order(monkeypatch):
    executed: list[str] = []

    class Connection:
        async def execute(self, statement):
            executed.append(statement)

        async def close(self):
            return None

    async def _connect(_dsn):
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


async def test_teardown_releases_the_owner_lock_only_after_its_drops(monkeypatch):
    server = _FakeServer()
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())
    worker = pg_dsn._owned_name("run_abc", "gw3")
    scratch = pg_dsn._owned_name("scratch", "s")
    await pg_dsn._create_owned_database("postgresql://u:p@h:5432/aqtest", worker)
    await pg_dsn._create_owned_database(f"postgresql://u:p@h:5432/{worker}", scratch)
    server.drop_errors[scratch] = RuntimeError("checkpoint stall")
    server.log.clear()

    with pytest.raises(RuntimeError, match="checkpoint stall"):
        await pg_dsn.dispose_owned_databases()

    key = pg_dsn._owner_lock_key(_TOKEN)
    # A database teardown could not drop is an orphan from here on: the lock
    # goes anyway, so the next run's sweep may take it.
    assert server.log == [
        f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)',
        f'DROP DATABASE IF EXISTS "{worker}" WITH (FORCE)',
        f"unlock:{key:x}",
    ]
    assert pg_dsn._OWNER is None and key not in server.locks


# ── the orphan sweep ───────────────────────────────────────────────────────


async def _sweep(server: _FakeServer) -> list[str]:
    failures: list[str] = []
    await pg_dsn._reap_orphaned_databases(_ADMIN, _TOKEN, failures)
    return failures


def _dropped(server: _FakeServer) -> list[str]:
    return [entry.split('"')[1] for entry in server.log if entry.startswith("DROP")]


async def test_sweep_drops_only_databases_whose_owner_lock_is_free(monkeypatch):
    dead, live = "abc123def456", "123abc456def"
    orphans = [f"aq_test_ownv2_{dead}_run_gw0", f"aq_test_ownv2_{dead}_scratch_mig_t1"]
    keep = [
        f"aq_test_ownv2_{live}_run_gw1",  # owner alive
        f"aq_test_ownv2_{_TOKEN}_run_gw3",  # this process
        "aq_test_aqtest_run_abc_gw0",  # legacy: no owner lock to prove anything
        "aq_test_aq_test_aqtest_run_gw0_scratch_mig_t1",  # legacy scratch
        f"aq_test_poolv2_{dead}_gw0_0",  # lease pool: tests/db_fixtures.py sweeps it
        "aq_tmpl_0123abcd",  # schema template
        "agent_queue",  # operator database
        "postgres",
        "aq_test_ownv2_NOTHEX123456_run_gw0",  # malformed token
        f"aq_test_ownv2_{dead}",  # no tail
    ]
    server = _FakeServer(orphans + keep)
    _hold(server, live)
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())

    assert await _sweep(server) == []

    assert _dropped(server) == sorted(orphans)
    assert server.databases == set(keep)
    # No WITH (FORCE): an orphan somebody is still attached to stays put.
    assert all("FORCE" not in entry for entry in server.log if entry.startswith("DROP"))
    assert f"SET statement_timeout = {pg_dsn._REAP_STATEMENT_TIMEOUT_MS}" in server.log
    # Every lock the sweep took went away with its connection.
    assert set(server.locks) == {pg_dsn._owner_lock_key(live)}
    assert all(conn.closed for conn in server.connections)


async def test_sweep_is_bounded_per_pass(monkeypatch):
    names = [f"aq_test_ownv2_{token * 12}_scratch_{index}" for token in "abc" for index in range(4)]
    server = _FakeServer(names)
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())

    await _sweep(server)

    assert len(_dropped(server)) == pg_dsn._MAX_REAP_PER_SWEEP == 8
    assert _dropped(server) == sorted(names)[:8]


async def test_only_one_process_sweeps_at_a_time(monkeypatch):
    server = _FakeServer(["aq_test_ownv2_abc123def456_run_gw0"])
    server.locks[pg_dsn._SWEEP_LOCK_KEY] = _FakeConnection(server, _ADMIN)
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())

    await _sweep(server)

    assert _dropped(server) == []
    assert all(conn.closed for conn in server.connections)


async def test_a_failed_drop_is_recorded_and_the_pass_continues(monkeypatch):
    first, second = "aq_test_ownv2_abc123def456_a", "aq_test_ownv2_abc123def456_b"
    server = _FakeServer([first, second])
    server.drop_errors[first] = RuntimeError("canceling statement due to statement timeout")
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())

    failures = await _sweep(server)

    assert _dropped(server) == [first, second]
    assert server.databases == {first}
    assert len(failures) == 1 and first in failures[0] and "statement timeout" in failures[0]


async def test_provisioning_sweeps_in_the_background_and_teardown_does_not_wait(monkeypatch):
    orphan = "aq_test_ownv2_abc123def456_run_gw0"
    server = _FakeServer([orphan])
    server.stuck.add(orphan)
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())
    _reset_derivation(monkeypatch)

    started = time.monotonic()
    worker_dsn = await asyncio.to_thread(pg_dsn.ensure_worker_postgres_dsn)
    assert time.monotonic() - started < 5
    assert worker_dsn.endswith(f"/aq_test_ownv2_{_TOKEN}_run_abc_gw3")

    deadline = time.monotonic() + 5
    while not any(entry.startswith("DROP") for entry in server.log):
        assert time.monotonic() < deadline, server.log
        await asyncio.sleep(0.01)

    started = time.monotonic()
    await pg_dsn.dispose_owned_databases()
    assert time.monotonic() - started < 5
    assert orphan in server.databases  # interrupted; the next run tries again
    assert server.locks == {} and all(conn.closed for conn in server.connections)


async def test_teardown_reports_orphans_the_sweep_could_not_drop(monkeypatch):
    orphan = "aq_test_ownv2_abc123def456_run_gw0"
    server = _FakeServer([orphan])
    server.drop_errors[orphan] = RuntimeError("database is being accessed by other users")
    monkeypatch.setitem(sys.modules, "asyncpg", server.module())
    _reset_derivation(monkeypatch)

    await asyncio.to_thread(pg_dsn.ensure_worker_postgres_dsn)
    deadline = time.monotonic() + 5
    # connections: the owner lease, the worker CREATE, then the sweep.
    while not (len(server.connections) >= 3 and server.connections[2].closed):
        assert time.monotonic() < deadline, server.log
        await asyncio.sleep(0.01)

    with pytest.warns(RuntimeWarning, match="being accessed by other users"):
        await pg_dsn.dispose_owned_databases()


# ── against a real server ──────────────────────────────────────────────────

_PROBE = """
import asyncio, os, sys
sys.path.insert(0, os.environ["PROBE_ROOT"])
from tests import pg_dsn

name = pg_dsn._owned_name("sigterm_probe")
asyncio.run(pg_dsn._create_owned_database(os.environ["PROBE_BASE_DSN"], name))
print(pg_dsn._owner_token(), name, flush=True)
sys.stdin.read()
"""


async def test_a_sigterm_owner_leaves_databases_the_sweep_can_prove_dead():
    """The pytest_sessionfinish that SIGTERM skips is no longer the only cleanup."""
    import asyncpg

    base = pg_dsn.ensure_worker_postgres_dsn()
    if not base:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    env = {**os.environ, "PROBE_ROOT": str(Path(__file__).resolve().parent.parent)}
    env["PROBE_BASE_DSN"] = base
    probe = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PROBE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=env,
    )
    conn = name = None
    try:
        line = await asyncio.wait_for(probe.stdout.readline(), 60)
        token, name = line.decode().split()
        assert pg_dsn._OWNED_NAME_RE.fullmatch(name)[1] == token

        conn = await asyncpg.connect(pg_dsn._maintenance_dsn(base))
        groups = await pg_dsn._orphan_groups(conn, own_token=pg_dsn._owner_token())
        assert groups[token] == [name]
        # Alive: its lock is held, so no sweeper may claim the database.
        assert await pg_dsn._claim_orphans(conn, {token: [name]}, budget=8) == []

        probe.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(probe.wait(), 30) == -signal.SIGTERM

        # Dead: the server released the lock with the probe's connection.
        claimed: list[str] = []
        deadline = time.monotonic() + 60
        while not claimed:
            assert time.monotonic() < deadline, "owner lock outlived its process"
            claimed = await pg_dsn._claim_orphans(conn, {token: [name]}, budget=8)
            if not claimed:
                await asyncio.sleep(0.1)
        assert claimed == [name]
    finally:
        if probe.returncode is None:
            probe.kill()
            await probe.wait()
        if conn is not None:
            # Best effort: a busy checkpointer can hold DROP DATABASE for
            # minutes, and whatever stays is exactly what later sweeps reap.
            with contextlib.suppress(asyncpg.PostgresError):
                await conn.execute("SET statement_timeout = 5000")
                await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
            await conn.close()


async def _async_value(value):
    return value
