"""PostgreSQL test substrate — template database, lease pool, truncate reset.

Replaces the SQLite template cache in :mod:`src.database.engine` (which
byte-copies a fully migrated ``.db`` file per test) with the PostgreSQL
equivalent.  That cache is the only reason the suite runs in minutes rather
than days: without a template, every fresh database replays the full Alembic
chain.  Deleting SQLite deletes it, so this module has to exist *first*.

Three tiers, by what a test actually needs:

* **Tier 1 — lease pool (the default).**  Each xdist worker owns a small pool
  of databases cloned from the template.  A test leases one per distinct
  database it asks for and the lease is truncated on release.  Truncate costs
  milliseconds; this is the path ~95% of tests take.
* **Tier 2 — :func:`clone_database`.**  A fresh clone of the template, for a
  test that mutates schema and cannot hand the database back to the pool.
* **Tier 3 — ``tests.pg_dsn.create_scratch_database``.**  An empty database for
  tests that drive ``alembic upgrade``/``downgrade`` themselves.

See ``docs/superpowers/specs/2026-09-07-sqlite-removal-implementation.md`` §T0.
"""

from __future__ import annotations

import os
import re
import uuid
import warnings

from src.database.schema_key import schema_key_slug
from tests.pg_dsn import ensure_worker_postgres_dsn

#: Advisory-lock key guarding template construction across xdist workers.
#: Arbitrary but fixed; scoped to the maintenance database it is taken on.
_TEMPLATE_LOCK_KEY = 0x41515F54_454D504C  # "AQ_TEMPL"

#: How many databases a worker keeps in its lease pool.  Tests almost always
#: want one; the conftest factories occasionally build two (a handler DB and a
#: separate orchestrator DB), and a handful of integration tests want three.
POOL_SIZE = int(os.environ.get("AQ_TEST_DB_POOL_SIZE", "4"))

#: Set once this process has confirmed the template exists, so repeated
#: clones skip even the existence query.
_TEMPLATE_READY: str | None = None

_IDENT_RE = re.compile(r"[^a-zA-Z0-9_]")

# A pool holds this lock on the maintenance database from before its first
# clone until disposal.  A dead process loses the lock even if pytest cannot
# run fixture teardown.  The versioned name keeps old, unlocked pool databases
# out of the automatic sweep.
_POOL_NAME_RE = re.compile(r"aq_test_poolv2_([0-9a-f]{12})_[a-zA-Z0-9_]{1,20}_[0-9]{1,8}\Z")
_POOL_LOCK_PREFIX = 0x5170000000000000
_MAX_REAP_PER_START = 8
_REAP_STATEMENT_TIMEOUT_MS = 5000


def _pool_lock_key(run_id: str) -> int:
    return _POOL_LOCK_PREFIX | int(run_id, 16)


def template_name() -> str:
    """``aq_tmpl_<schema slug>`` — one template per distinct migrated schema."""
    return f"aq_tmpl_{schema_key_slug()}"


def _split(dsn: str) -> tuple[str, str]:
    prefix, _, dbname = dsn.rpartition("/")
    return prefix, dbname


def _admin_dsn(dsn: str) -> str:
    """asyncpg wants the plain scheme, not SQLAlchemy's ``+asyncpg`` suffix."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


async def _connect_admin(base_dsn: str):
    import asyncpg

    return await asyncpg.connect(_admin_dsn(base_dsn))


async def _database_exists(conn, name: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name))


async def ensure_template(base_dsn: str) -> str:
    """Build ``aq_tmpl_<slug>`` if absent; return its name.

    Concurrency-safe across xdist workers via a session advisory lock on the
    maintenance database.  The template is built under a temporary name and
    renamed into place, so a crashed build never leaves a half-migrated
    database that a later run would happily clone — the same temp-then-rename
    discipline ``engine.py``'s SQLite template cache uses.

    Once built it is marked ``datistemplate`` / ``NOT datallowconn``: nothing
    can connect to it, which is also what makes ``CREATE DATABASE ... TEMPLATE``
    reliable (that statement fails while any session is attached to the source).
    """
    global _TEMPLATE_READY
    name = template_name()
    if _TEMPLATE_READY == name:
        return name

    # Fast path: the template almost always already exists, and taking the
    # advisory lock to discover that serialises every worker behind every
    # other worker's clone.  Check first, lock only to build.
    # Advisory locks are database-scoped. Every worker must coordinate through
    # the same maintenance database, even though its test DSN is worker-local.
    prefix, _ = _split(base_dsn)
    maintenance_dsn = f"{prefix}/postgres"
    conn = await _connect_admin(maintenance_dsn)
    try:
        if await _database_exists(conn, name):
            _TEMPLATE_READY = name
            return name
    finally:
        await conn.close()

    conn = await _connect_admin(maintenance_dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _TEMPLATE_LOCK_KEY)
        try:
            if await _database_exists(conn, name):
                return name
            building = f"{name}_building"
            await conn.execute(f'DROP DATABASE IF EXISTS "{building}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{building}"')
            # Hold the lock until publication: releasing it during migration
            # lets another worker drop the database we are still building.
            await _migrate(f"{prefix}/{building}")
            await conn.execute(f'ALTER DATABASE "{name}_building" RENAME TO "{name}"')
            await conn.execute(
                "UPDATE pg_database SET datistemplate = true WHERE datname = $1", name
            )
            await conn.execute(
                "UPDATE pg_database SET datallowconn = false WHERE datname = $1", name
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _TEMPLATE_LOCK_KEY)
    finally:
        await conn.close()
    _TEMPLATE_READY = name
    return name


async def _migrate(dsn: str) -> None:
    """Run the Alembic chain to head against *dsn*, then dispose the engine."""
    from src.database.engine import create_postgres_engine, run_schema_setup

    engine = create_postgres_engine(dsn)
    try:
        await run_schema_setup(engine)
    finally:
        await engine.dispose()


async def clone_database(base_dsn: str, name: str) -> str:
    """``CREATE DATABASE <name> TEMPLATE <template>``; return the DSN.

    A server-side file copy — far cheaper than replaying migrations, and the
    reason this substrate can stand in for the SQLite template cache.
    """
    template = await ensure_template(base_dsn)
    prefix, _ = _split(base_dsn)
    import asyncpg

    conn = await _connect_admin(base_dsn)
    try:
        if await _database_exists(conn, name):
            raise RuntimeError(
                f"refusing to replace existing PostgreSQL test database {name!r}; "
                "it is not owned by this lease pool"
            )
        try:
            await conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
        except asyncpg.exceptions.DuplicateDatabaseError as exc:
            raise RuntimeError(
                f"refusing to replace concurrently created PostgreSQL test database {name!r}; "
                "retry with a fresh test-run token"
            ) from exc
    finally:
        await conn.close()
    return f"{prefix}/{name}"


async def drop_database(base_dsn: str, name: str) -> None:
    conn = await _connect_admin(base_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


#: Wipe every row but leave the schema (and ``alembic_version``) alone.
#:
#: Two optimisations over the obvious "TRUNCATE each table in a loop", which
#: cost 2.6s per call against this schema's 92 tables and made the per-test
#: teardown dominate the whole suite:
#:
#: * one ``TRUNCATE a, b, c ...`` statement instead of 92 separate ones, and
#: * skip tables that are already empty — a test typically touches under ten
#:   of the 92, and ``EXISTS`` against an empty table is essentially free.
#:
#: Measured on this schema: 2619ms (loop) -> 210ms (single statement) ->
#: 60-140ms (this).
_TRUNCATE_ALL = """
DO $$
DECLARE r RECORD; tbls text := ''; has boolean;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables
           WHERE schemaname = 'public' AND tablename <> 'alembic_version' LOOP
    EXECUTE format('SELECT EXISTS(SELECT 1 FROM %I)', r.tablename) INTO has;
    IF has THEN tbls := tbls || quote_ident(r.tablename) || ','; END IF;
  END LOOP;
  IF tbls <> '' THEN
    EXECUTE 'TRUNCATE TABLE ' || rtrim(tbls, ',') || ' RESTART IDENTITY CASCADE';
  END IF;
END $$;
"""


async def truncate_all(dsn: str) -> None:
    """Wipe every row but leave the schema (and ``alembic_version``) alone."""
    conn = await _connect_admin(dsn)
    try:
        await conn.execute(_TRUNCATE_ALL)
    finally:
        await conn.close()


#: Rows the migration chain itself inserts, captured from the template once.
#: ``truncate_all`` would otherwise delete them and every test after the first
#: in a leased database would run without them -- which is exactly what broke
#: six workspace-heavy tests on the first substrate run: the built-in
#: ``workspace_kinds`` (``project-repo``, ``vault``, ``readonly-dir``) vanished
#: after the first truncate, and the failures reproduced only under xdist
#: because each passed in isolation.
_SEED: dict[str, list[dict]] | None = None


async def capture_seed(dsn: str) -> dict[str, list[dict]]:
    """Snapshot every non-empty table of a pristine template clone."""
    conn = await _connect_admin(dsn)
    try:
        tables = [
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ]
        if not tables:
            return {}
        # One round trip to find the non-empty tables rather than 92 -- the
        # migration chain seeds a handful of rows in one table, so fetching
        # each table's contents individually is almost all wasted latency.
        probe = " UNION ALL ".join(
            f"SELECT '{t}' AS t WHERE EXISTS (SELECT 1 FROM \"{t}\")" for t in tables
        )
        seed: dict[str, list[dict]] = {}
        for row in await conn.fetch(probe):
            name = row["t"]
            seed[name] = [dict(r) for r in await conn.fetch(f'SELECT * FROM "{name}"')]
        return seed
    finally:
        await conn.close()


async def restore_seed(dsn: str, seed: dict[str, list[dict]]) -> None:
    """Re-insert the captured migration seed rows after a truncate."""
    if not seed:
        return
    conn = await _connect_admin(dsn)
    try:
        for name, rows in seed.items():
            columns = list(rows[0].keys())
            collist = ", ".join(f'"{c}"' for c in columns)
            params = ", ".join(f"${i + 1}" for i in range(len(columns)))
            stmt = f'INSERT INTO "{name}" ({collist}) VALUES ({params}) ON CONFLICT DO NOTHING'
            await conn.executemany(stmt, [[r[c] for c in columns] for r in rows])
    finally:
        await conn.close()


async def seed_task_session_attempt(
    db,
    *,
    task_id: str,
    project_id: str,
    task_status=None,
    session_id: str | None = None,
    agent_id: str | None = "a1",
    agent_name: str = "Worker",
    profile_id: str = "worker",
    session_state: str = "running",
    attempt_state: str = "running",
    heartbeat_age: float = 30.0,
    now: float | None = None,
    session_started_at: float | None = None,
    attempt_started_at: float | None = None,
    session_ended_at: float | None = None,
    attempt_ended_at: float | None = None,
    create_task: bool = True,
) -> dict[str, str]:
    """Create the task/session/live-attempt shape used by marker tests.

    Graph markers are derived from a live session attempt, never from
    ``Agent.current_task_id``.  Centralising this otherwise verbose setup
    keeps endpoint, query, and doctor tests honest while leaving the relevant
    liveness controls explicit: status, both states, heartbeat age, and end
    times all remain fixture arguments.
    """
    import time

    from sqlalchemy import insert

    from src.database.tables import task_session_attempts
    from src.models import SessionRecord, Task, TaskStatus

    if task_status is None:
        task_status = TaskStatus.IN_PROGRESS
    if create_task:
        await db.create_task(
            Task(id=task_id, project_id=project_id, title=task_id, description="", status=task_status)
        )

    timestamp = time.time() if now is None else now
    session_started_at = timestamp - 60 if session_started_at is None else session_started_at
    attempt_started_at = session_started_at if attempt_started_at is None else attempt_started_at
    session_id = session_id or f"session-{agent_id or 'unowned'}-{task_id}"
    attempt_id = uuid.uuid4().hex
    await db.create_session(
        SessionRecord(
            id=session_id,
            project_id=project_id,
            profile_id=profile_id,
            harness="claude",
            provider="tmux",
            name=session_id,
            lifecycle="pool",
            work_dir="/w",
            epoch="e",
            instance_token=session_id,
            started_at=session_started_at,
            task_id=task_id,
            state=session_state,
            last_activity=timestamp - heartbeat_age,
            ended_at=session_ended_at,
        )
    )
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(task_session_attempts).values(
                id=attempt_id,
                session_id=session_id,
                task_id=task_id,
                project_id=project_id,
                agent_id=agent_id,
                agent_name=agent_name,
                profile_id=profile_id,
                name=agent_name,
                lifecycle="pool",
                model="claude-opus-5",
                harness="claude",
                provider="tmux",
                state=attempt_state,
                work_dir="/w",
                started_at=attempt_started_at,
                session_started_at=session_started_at,
                ended_at=attempt_ended_at,
            )
        )
    return {"task_id": task_id, "session_id": session_id, "attempt_id": attempt_id}


class LeasePool:
    """Per-worker pool of template-cloned databases, truncated on release.

    Cloning is ~100-300ms; truncating is single-digit milliseconds.  With one
    clone per pool slot amortised over the whole session, the per-test cost is
    the truncate, which is what makes this competitive with byte-copying a
    SQLite file.
    """

    def __init__(self, base_dsn: str, worker: str, size: int = POOL_SIZE):
        self._base = base_dsn
        self._worker = (_IDENT_RE.sub("_", worker) or "master")[:20]
        self._run_id = uuid.uuid4().hex[:12]
        self._size = size
        self._free: list[str] = []
        self._created: set[str] = set()
        self._next = 0
        self._guard = None

    def _name(self, index: int) -> str:
        return f"aq_test_poolv2_{self._run_id}_{self._worker}_{index}"

    async def _start(self) -> None:
        if self._guard is not None:
            return
        prefix, _ = _split(self._base)
        conn = await _connect_admin(f"{prefix}/postgres")
        try:
            await conn.execute("SELECT pg_advisory_lock($1)", _pool_lock_key(self._run_id))
            self._guard = conn
            await self._reap_stale(conn)
        except BaseException:
            self._guard = None
            await conn.close()
            raise

    async def _reap_stale(self, conn) -> None:
        """Drop only versioned pool databases whose owner lock is gone."""
        rows = await conn.fetch(
            "SELECT datname FROM pg_database WHERE datname LIKE 'aq_test_poolv2_%'"
        )
        groups: dict[str, list[str]] = {}
        for row in rows:
            name = row["datname"]
            match = _POOL_NAME_RE.fullmatch(name)
            if match and match[1] != self._run_id:
                groups.setdefault(match[1], []).append(name)

        # Cleanup is opportunistic: a DROP can wait on a busy checkpointer.
        # Bound the work so it cannot hold the test suite's startup hostage.
        attempted = 0
        await conn.execute(f"SET statement_timeout = {_REAP_STATEMENT_TIMEOUT_MS}")
        try:
            for run_id, names in groups.items():
                if attempted >= _MAX_REAP_PER_START:
                    break
                key = _pool_lock_key(run_id)
                if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", key):
                    continue
                try:
                    for name in sorted(names):
                        if attempted >= _MAX_REAP_PER_START:
                            break
                        attempted += 1
                        try:
                            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                        except Exception as exc:
                            warnings.warn(
                                f"could not reap stale test database {name}: {exc}",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                finally:
                    await conn.execute("SELECT pg_advisory_unlock($1)", key)
        finally:
            await conn.execute("RESET statement_timeout")

    async def acquire(self) -> str:
        """Lease a clean database; returns its DSN."""
        await self._start()
        if self._free:
            return self._free.pop()
        global _SEED
        index, self._next = self._next, self._next + 1
        name = self._name(index)
        dsn = await clone_database(self._base, name)
        self._created.add(name)
        if _SEED is None:
            _SEED = await capture_seed(dsn)
        return dsn

    async def release(self, dsn: str) -> None:
        await truncate_all(dsn)
        if _SEED:
            await restore_seed(dsn, _SEED)
        self._free.append(dsn)

    async def dispose(self) -> None:
        try:
            for name in sorted(self._created):
                await drop_database(self._base, name)
                self._created.remove(name)
            self._free.clear()
        finally:
            if self._guard is not None:
                await self._guard.close()
                self._guard = None


def base_dsn() -> str | None:
    """This worker's Postgres DSN, or ``None`` when the suite has no Postgres."""
    return ensure_worker_postgres_dsn()


# ── Explicit per-test database leases ───────────────────────────────────────
#: Set by the ``_pg_backend`` fixture in conftest for the duration of a test.
_LEASES: dict[str, str] = {}
_FREE: list[str] = []
_TAKEN: list[str] = []


def begin_test(pool_dsns: list[str]) -> None:
    """Arm :func:`lease_dsn` with this test's pool of leasable databases."""
    _LEASES.clear()
    _FREE[:] = list(pool_dsns)
    _TAKEN.clear()


def leased() -> list[str]:
    """The databases this test actually took, for the teardown reset."""
    return list(_TAKEN)


def lease_dsn(name: str = "test") -> str:
    """A Postgres database for this test, keyed by *name*.

    Replaces the ``Database(lease_dsn("test.db"))`` idiom the suite grew
    under SQLite.  Asking twice for the same *name* within one test returns
    the same database, so tests that reopen a path to check persistence keep
    working; different names get different databases.
    """
    if name in _LEASES:
        return _LEASES[name]
    if not _FREE:
        raise RuntimeError(
            f"this test asked for more distinct databases than the pool holds "
            f"({len(_TAKEN)} already leased). Raise AQ_TEST_DB_POOL_SIZE."
        )
    dsn = _FREE.pop()
    _LEASES[name] = dsn
    _TAKEN.append(dsn)
    return dsn
