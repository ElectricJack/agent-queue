"""Run-owned PostgreSQL databases for pytest and pytest-xdist.

``POSTGRES_TEST_DSN`` names a maintenance database on a disposable PostgreSQL
server. The suite never runs against that database itself. Each pytest process
creates an exclusively owned database whose name includes a fresh run token and
the xdist worker id, and migration tests create further unique scratch
databases. A graceful session teardown removes only names this process
successfully created.

An unexpected existing target is treated as an ownership collision. Its
Alembic state is inspected read-only for an actionable stale/unknown-revision
diagnostic; it is never reused, dropped, migrated, or stamped.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
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


def _worker_id() -> str:
    """Sanitised xdist worker id, or ``master`` for a serial pytest run."""
    raw = os.environ.get(_WORKER_ENV, "master")
    return _IDENT_RE.sub("_", raw) or "master"


def _run_id() -> str:
    """One ownership token per pytest process (shared when ``aq test`` sets it)."""
    global _CACHED_RUN_ID
    if _CACHED_RUN_ID is _UNSET:
        raw = os.environ.get(_RUN_ENV) or uuid.uuid4().hex[:16]
        _CACHED_RUN_ID = (_IDENT_RE.sub("_", raw) or uuid.uuid4().hex[:16])[:32]
    return str(_CACHED_RUN_ID)


def _unique_token() -> str:
    return uuid.uuid4().hex[:12]


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
    """Readable, collision-resistant PostgreSQL identifier of at most 63 bytes."""
    raw = "_".join(parts)
    slug = _IDENT_RE.sub("_", raw).strip("_") or "run"
    candidate = f"aq_test_{slug}"
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


async def _create_owned_database(base_dsn: str, target: str) -> None:
    """Create *target* exclusively and register it for owner-only cleanup."""
    import asyncpg

    admin_dsn = _maintenance_dsn(base_dsn)
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
    """Drop only databases created by this process, in reverse creation order."""
    if not _OWNED_DATABASES:
        return

    import asyncpg

    failures: list[str] = []
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
    if failures:
        raise RuntimeError(
            "could not clean owned PostgreSQL test databases: " + "; ".join(failures)
        )


async def create_scratch_database(suffix: str) -> str:
    """Create a unique empty database for a schema-mutating test."""
    base = ensure_worker_postgres_dsn()
    if not base:
        raise RuntimeError("create_scratch_database requires POSTGRES_TEST_DSN")
    target = _owned_name(
        _database_name(base),
        "scratch",
        suffix,
        _unique_token(),
    )
    await _create_owned_database(base, target)
    return _replace_database(base, target)


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
    target = _owned_name(_database_name(base), _run_id(), _worker_id())
    asyncio.run(_create_owned_database(base, target))
    worker_dsn = _replace_database(base, target)
    os.environ["POSTGRES_TEST_DSN"] = worker_dsn
    _CACHED_DSN = worker_dsn
    return worker_dsn
