"""Run-owned PostgreSQL databases for pytest and pytest-xdist.

``POSTGRES_TEST_DSN`` names a maintenance database on a disposable PostgreSQL
server. The suite never runs against that database itself. Each pytest process
creates an exclusively owned database whose name includes a fresh owner token,
the run token and the xdist worker id, and migration tests create further
unique scratch databases. A graceful session teardown removes only names this
process successfully created.

SIGTERM, SIGKILL or the OOM killer can end a process before that teardown. The
owner token is therefore also a PostgreSQL advisory lock, held on a dedicated
connection from before the process's first ``CREATE DATABASE`` until its
teardown has finished. The server drops the lock when that connection closes,
however the process ended, so a free lock proves the owner is gone. A lock
lost while the process lives on is taken back at once, and no database is
created until it is. Every new worker database starts one bounded background
sweep of ``aq_test_ownv2_*`` names whose owner lock is free. Operator
databases, schema templates, lease-pool clones (``tests/db_fixtures.py`` reaps
those) and older unversioned ``aq_test_*`` names never match it: they carry no
lock that could prove anything.

An unexpected existing target is treated as an ownership collision. Its
Alembic state is inspected read-only for an actionable stale/unknown-revision
diagnostic; this process never reuses, drops, migrates, or stamps it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import uuid
import warnings
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_WORKER_ENV = "PYTEST_XDIST_WORKER"
_RUN_ENV = "AQ_TEST_RUN_ID"
_MAX_DATABASE_NAME = 63
_IDENT_RE = re.compile(r"[^a-zA-Z0-9_]")

# Sentinels distinguish "not resolved" from a cached ``None`` DSN.
_UNSET = object()
_CACHED_DSN: object | str | None = _UNSET
_CACHED_RUN_ID: object | str = _UNSET

# Creation order is ownership proof. Teardown walks it backwards so scratch
# children disappear before the process's worker database.
_OWNED_DATABASES: list[tuple[str, str]] = []

# Every database this process creates is named ``aq_test_ownv2_<token>_...``,
# and the process holds the owner lock for ``<token>`` from before the CREATE
# until teardown, so a free lock means an orphan. The version keeps unlocked
# names from older checkouts out of the sweep.
_OWNED_PREFIX = "aq_test_ownv2"
_OWNED_NAME_RE = re.compile(r"aq_test_ownv2_([0-9a-f]{12})_[a-zA-Z0-9_]+\Z")
# Next to tests/db_fixtures.py's lease-pool keys (0x5170..., 0x5171...).
# Advisory locks are scoped to the connected database, so every holder and
# sweeper connects to the same maintenance database (_maintenance_dsn).
_OWNER_LOCK_PREFIX = 0x5172000000000000
_SWEEP_LOCK_KEY = 0x5173000000000000
_MAX_REAP_PER_SWEEP = 8
# The sweep runs in the background, so a DROP may wait out a slow checkpoint.
_REAP_STATEMENT_TIMEOUT_MS = 30_000
_CLOSE_TIMEOUT_S = 10
# How long a lost owner lock is retried, e.g. across a test-server restart.
_RELOCK_DEADLINE_S = 120

_OWNER_TOKEN: str | None = None
_OWNER: _OwnerLease | None = None


def _worker_id() -> str:
    """Sanitised xdist worker id, or ``master`` for a serial pytest run."""
    raw = os.environ.get(_WORKER_ENV, "master")
    return _IDENT_RE.sub("_", raw) or "master"


def _run_id() -> str:
    """The run token: readable in names, shared by xdist workers under ``aq test``."""
    global _CACHED_RUN_ID
    if _CACHED_RUN_ID is _UNSET:
        raw = os.environ.get(_RUN_ENV) or uuid.uuid4().hex[:16]
        _CACHED_RUN_ID = (_IDENT_RE.sub("_", raw) or uuid.uuid4().hex[:16])[:32]
    return str(_CACHED_RUN_ID)


def _unique_token() -> str:
    return uuid.uuid4().hex[:12]


def _owner_token() -> str:
    """Random per process: names its databases and keys its owner lock."""
    global _OWNER_TOKEN
    if _OWNER_TOKEN is None:
        _OWNER_TOKEN = uuid.uuid4().hex[:12]
    return _OWNER_TOKEN


def _owner_lock_key(token: str) -> int:
    return _OWNER_LOCK_PREFIX | int(token, 16)


def _database_name(dsn: str) -> str:
    name = urlsplit(dsn).path.lstrip("/")
    if not name:
        raise RuntimeError("POSTGRES_TEST_DSN must include a maintenance database name")
    return name


def _replace_database(dsn: str, name: str, *, asyncpg: bool = False) -> str:
    parts = urlsplit(dsn)
    scheme = parts.scheme.replace("+asyncpg", "") if asyncpg else parts.scheme
    return urlunsplit((scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def _maintenance_dsn(dsn: str) -> str:
    """A plain-asyncpg URL independent of every database this run owns."""
    return _replace_database(dsn, "postgres", asyncpg=True)


def _owned_name(*parts: str) -> str:
    """Readable, collision-resistant PostgreSQL identifier of at most 63 bytes.

    Only the tail is ever shortened, so the owner token stays readable.
    """
    raw = "_".join(parts)
    slug = _IDENT_RE.sub("_", raw).strip("_") or "run"
    candidate = f"{_OWNED_PREFIX}_{_owner_token()}_{slug}"
    if len(candidate) <= _MAX_DATABASE_NAME:
        return candidate
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    return f"{candidate[: _MAX_DATABASE_NAME - len(digest) - 1]}_{digest}"


async def _database_exists(conn, name: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name))


@lru_cache(maxsize=1)
def _known_revisions() -> tuple[frozenset[str], frozenset[str]]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    return (
        frozenset(revision.revision for revision in script.walk_revisions()),
        frozenset(script.get_heads()),
    )


async def _revision_diagnostic(base_dsn: str, target: str) -> str:
    """Describe an existing target's stamp without changing it."""
    import asyncpg

    try:
        conn = await asyncpg.connect(_replace_database(base_dsn, target, asyncpg=True))
    except Exception as exc:  # pragma: no cover - server-specific denial text
        return f"Alembic state unavailable ({type(exc).__name__}: {exc})"
    try:
        table = await conn.fetchval("SELECT to_regclass('public.alembic_version')")
        if not table:
            return "database is unstamped"
        rows = await conn.fetch("SELECT version_num FROM alembic_version ORDER BY version_num")
        current = frozenset(str(row["version_num"]) for row in rows)
        known, heads = _known_revisions()
        unknown = sorted(current - known)
        if unknown:
            return f"alembic_version contains unknown revision(s) {unknown!r}"
        if current != heads:
            return (
                f"alembic_version is stale at {sorted(current)!r}; "
                f"this checkout's head(s) are {sorted(heads)!r}"
            )
        return f"alembic_version is at this checkout's head(s) {sorted(current)!r}"
    except Exception as exc:  # pragma: no cover - malformed foreign database
        return f"Alembic state unreadable ({type(exc).__name__}: {exc})"
    finally:
        await conn.close()


class _OwnerTokenHeld(RuntimeError):
    """Another session holds this process's owner lock."""


class _OwnerLease:
    """This process's owner lock, held on a dedicated connection until closed.

    The connection lives on its own daemon thread and event loop, because
    pytest runs each test (and each ``asyncio.run``) on a loop that closes.
    The orphan sweep runs on that loop too, so it never delays test startup.
    """

    def __init__(self, admin_dsn: str, token: str):
        self.admin_dsn = admin_dsn
        self.token = token
        self.sweep_failures: list[str] = []
        self.lost: str | None = None
        self._conn = None
        self._closing = False
        self._relock: asyncio.Task | None = None
        self._sweep: asyncio.Task | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="aq-test-db-owner", daemon=True
        )

    def _submit(self, coro):
        return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    async def acquire(self) -> None:
        self._thread.start()
        try:
            await self._submit(self._lock())
        except BaseException:
            self._stop()
            raise

    async def _lock(self) -> None:
        import asyncpg

        conn = await asyncpg.connect(
            self.admin_dsn, server_settings={"application_name": "aq-test-db-owner"}
        )
        try:
            held = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", _owner_lock_key(self.token)
            )
        except BaseException:
            conn.terminate()
            raise
        if not held:
            await conn.close(timeout=_CLOSE_TIMEOUT_S)
            raise _OwnerTokenHeld(
                f"PostgreSQL test owner token {self.token} is already held by another "
                "session; refusing to create test databases under it"
            )
        conn.add_termination_listener(self._on_terminated)
        self._conn = conn

    def _on_terminated(self, conn) -> None:
        # The lock went with the connection (a server restart, a reset socket)
        # while this process lives on. Until it is back, a sweep elsewhere may
        # take this process's databases for orphans, so re-take it at once.
        if self._closing or conn is not self._conn:
            return
        self._conn = None
        self._relock = self._loop.create_task(self._relock_after_loss())

    async def _relock_after_loss(self) -> None:
        deadline = self._loop.time() + _RELOCK_DEADLINE_S
        delay = 0.2
        while not self._closing:
            try:
                await self._lock()
                return
            except _OwnerTokenHeld as exc:
                # A sweeper claimed the token: this process's databases may
                # already be gone. Nothing more is created under it.
                self.lost = str(exc)
                return
            except Exception as exc:  # noqa: BLE001 - the server may still be restarting
                if self._loop.time() >= deadline:
                    self.lost = f"could not reconnect: {type(exc).__name__}: {exc}"
                    return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)

    async def ensure_held(self) -> None:
        """Refuse a new database while the owner lock is not provably held."""
        await self._submit(self._await_held())

    async def _await_held(self) -> None:
        if self._conn is not None and self._conn.is_closed():
            self._on_terminated(self._conn)
        if self._relock is not None:
            await self._relock
        if self._conn is None:
            raise RuntimeError(
                "lost this process's PostgreSQL test owner lock and could not take it "
                f"back ({self.lost}); refusing to create a database that a sweep could "
                "take for an orphan"
            )

    async def start_sweep(self) -> None:
        await self._submit(self._spawn_sweep())

    async def _spawn_sweep(self) -> None:
        self._sweep = self._loop.create_task(
            _reap_orphaned_databases(self.admin_dsn, self.token, self.sweep_failures)
        )

    async def aclose(self) -> list[str]:
        """Release the lock; returns orphans the sweep failed to drop."""
        try:
            await self._submit(self._release())
        finally:
            self._stop()
        return self.sweep_failures

    async def _release(self) -> None:
        self._closing = True
        # Teardown never waits on a sweep: what it did not reach is still an
        # orphan for the next run. cancel() is a no-op on a finished task.
        for task in (self._relock, self._sweep):
            if task is None:
                continue
            task.cancel()
            try:
                await asyncio.wait_for(task, _CLOSE_TIMEOUT_S)
            except (asyncio.CancelledError, TimeoutError):
                pass
            except Exception as exc:  # noqa: BLE001 - reported at teardown, never raised
                self.sweep_failures.append(f"{type(exc).__name__}: {exc}")
        if self._conn is not None:
            conn, self._conn = self._conn, None
            try:
                await conn.close(timeout=_CLOSE_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - the socket closes either way
                conn.terminate()
        # asyncpg's query-cancel requests outlive a cancelled DROP briefly.
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        if pending:
            await asyncio.wait(pending, timeout=_CLOSE_TIMEOUT_S)

    def _stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=_CLOSE_TIMEOUT_S)
        if not self._thread.is_alive():
            self._loop.close()


async def _hold_owner_lock(admin_dsn: str) -> None:
    """Take this process's owner lock before its first ``CREATE DATABASE``."""
    global _OWNER
    if _OWNER is None:
        lease = _OwnerLease(admin_dsn, _owner_token())
        await lease.acquire()
        _OWNER = lease
    elif _OWNER.admin_dsn != admin_dsn:
        # The lock proves liveness only on the server that holds it.
        raise RuntimeError(
            "refusing to create a test database on a second server: this process's "
            "owner lock can vouch for databases on one PostgreSQL server only"
        )
    else:
        await _OWNER.ensure_held()


async def _orphan_groups(conn, own_token: str) -> dict[str, list[str]]:
    """Versioned owned databases of other processes, grouped by owner token."""
    rows = await conn.fetch(
        r"SELECT datname FROM pg_database WHERE datname LIKE 'aq\_test\_ownv2\_%'"
    )
    groups: dict[str, list[str]] = {}
    for row in rows:
        match = _OWNED_NAME_RE.fullmatch(row["datname"])
        if match and match[1] != own_token:
            groups.setdefault(match[1], []).append(row["datname"])
    return groups


async def _claim_orphans(conn, groups: dict[str, list[str]], budget: int) -> list[str]:
    """Up to *budget* names whose owner lock *conn* could take: owner gone.

    The locks stay with *conn* until it closes, so no other sweeper races it.
    """
    claimed: list[str] = []
    for token in sorted(groups):
        if len(claimed) >= budget:
            break
        if await conn.fetchval("SELECT pg_try_advisory_lock($1)", _owner_lock_key(token)):
            claimed.extend(sorted(groups[token])[: budget - len(claimed)])
    return claimed


async def _reap_orphaned_databases(admin_dsn: str, own_token: str, failures: list[str]) -> None:
    """One bounded pass over dead owners' databases; one sweeper at a time.

    A DROP waits on a checkpoint, which a busy server can hold for minutes, so
    each has a statement timeout and a pass attempts only a few. There is no
    ``WITH (FORCE)``: an orphan somebody is still attached to stays put.
    """
    import asyncpg

    conn = await asyncpg.connect(
        admin_dsn, server_settings={"application_name": "aq-test-db-sweep"}
    )
    try:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _SWEEP_LOCK_KEY):
            return
        groups = await _orphan_groups(conn, own_token)
        await conn.execute(f"SET statement_timeout = {_REAP_STATEMENT_TIMEOUT_MS}")
        for name in await _claim_orphans(conn, groups, _MAX_REAP_PER_SWEEP):
            try:
                await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
            except Exception as exc:  # noqa: BLE001 - one orphan must not end the pass
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
    finally:
        # Closing releases the sweep lock and every owner lock claimed above.
        await conn.close(timeout=_CLOSE_TIMEOUT_S)


async def _create_owned_database(base_dsn: str, target: str) -> None:
    """Create *target* under this process's owner lock and register it for cleanup."""
    import asyncpg

    match = _OWNED_NAME_RE.fullmatch(target)
    if not match or match[1] != _owner_token():
        raise ValueError(f"{target!r} is not named under this process's owner token")
    admin_dsn = _maintenance_dsn(base_dsn)
    await _hold_owner_lock(admin_dsn)
    conn = await asyncpg.connect(admin_dsn)
    collision = False
    try:
        collision = await _database_exists(conn, target)
        if not collision:
            try:
                await conn.execute(f'CREATE DATABASE "{target}"')
            except asyncpg.exceptions.DuplicateDatabaseError:
                collision = True
    finally:
        await conn.close()

    if collision:
        state = await _revision_diagnostic(base_dsn, target)
        raise RuntimeError(
            f"Refusing to reuse PostgreSQL test database {target!r}: it already exists; "
            f"{state}. It was not created by this test process and was not dropped or "
            "stamped. Retry with a fresh AQ_TEST_RUN_ID, or remove it explicitly only "
            "after confirming that no other run owns it."
        )

    _OWNED_DATABASES.append((admin_dsn, target))


async def dispose_owned_databases() -> None:
    """Drop only databases created by this process, then release its owner lock."""
    global _OWNER
    try:
        failures = await _drop_owned_databases()
    finally:
        # Anything left behind is an orphan now; the next run's sweep may take it.
        lease, _OWNER = _OWNER, None
        stale: list[str] = []
        if lease is not None:
            try:
                stale = await lease.aclose()
            except Exception as exc:  # noqa: BLE001 - must not hide the drop failures
                stale = [f"owner lock release: {type(exc).__name__}: {exc}"]
    if stale:
        warnings.warn(
            f"could not reap {len(stale)} orphaned PostgreSQL test database(s); a later "
            "run retries: " + "; ".join(stale),
            RuntimeWarning,
            stacklevel=2,
        )
    if failures:
        raise RuntimeError(
            "could not clean owned PostgreSQL test databases: " + "; ".join(failures)
        )


async def _drop_owned_databases() -> list[str]:
    """Drop this process's databases in reverse creation order; returns failures."""
    failures: list[str] = []
    if not _OWNED_DATABASES:
        return failures

    import asyncpg

    while _OWNED_DATABASES:
        admin_dsn, target = _OWNED_DATABASES[-1]
        conn = None
        try:
            conn = await asyncpg.connect(admin_dsn)
            await conn.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
        except Exception as exc:  # pragma: no cover - teardown server failure
            failures.append(f"{target}: {type(exc).__name__}: {exc}")
            _OWNED_DATABASES.pop()
        else:
            _OWNED_DATABASES.pop()
        finally:
            if conn is not None:
                await conn.close()
    return failures


async def create_scratch_database(suffix: str) -> str:
    """Create a unique empty database for a schema-mutating test."""
    base = ensure_worker_postgres_dsn()
    if not base:
        raise RuntimeError("create_scratch_database requires POSTGRES_TEST_DSN")
    target = _owned_name("scratch", suffix, _unique_token())
    await _create_owned_database(base, target)
    return _replace_database(base, target)


async def _provision_worker_database(base_dsn: str, target: str) -> None:
    await _create_owned_database(base_dsn, target)
    if _OWNER is not None:
        await _OWNER.start_sweep()


def ensure_worker_postgres_dsn() -> str | None:
    """Point this process at a fresh, exclusively owned PostgreSQL database.

    The derived DSN is cached so every importing test module agrees. The base
    DSN is used only to reach the disposable server's maintenance database.
    """
    global _CACHED_DSN
    if _CACHED_DSN is not _UNSET:
        return _CACHED_DSN  # type: ignore[return-value]
    base = os.environ.get("POSTGRES_TEST_DSN", "").strip()
    if not base:
        if os.environ.get("AQ_REQUIRE_POSTGRES_TESTS") == "1":
            from src.cli.test_runner import postgres_test_dsn_error

            raise RuntimeError(postgres_test_dsn_error() or "POSTGRES_TEST_DSN is required")
        _CACHED_DSN = None
        return None
    _database_name(base)  # validates the DSN names a maintenance database
    target = _owned_name(_run_id(), _worker_id())
    asyncio.run(_provision_worker_database(base, target))
    worker_dsn = _replace_database(base, target)
    os.environ["POSTGRES_TEST_DSN"] = worker_dsn
    _CACHED_DSN = worker_dsn
    return worker_dsn
