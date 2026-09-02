"""Who may run Alembic migrations, and against which database.

The incident this module exists to prevent (2026-09-02): worker sessions
running in worktree slots executed ``alembic upgrade`` — directly, through a
pytest fixture, and through an ``aq start`` — against the DB URL in
``~/.agent-queue/config.yaml``.  That URL is the operator's *production*
database, so a branch that was never merged stamped ``alembic_version`` with
revisions ``main`` does not contain (``e7a2b9c41d05``, then
``f2a4c6e8b0d2``).  The daemon then refused to start with "Alembic preflight
failed: alembic_version references unknown revision(s)" and the operator had
to hand-write a merge revision to recover.

The invariant
-------------

**Only the daemon, or an operator who typed ``aq db upgrade``, may migrate
the production database.**  Everything else — the CLI, pytest, worker
sessions — may migrate its own scratch databases as much as it likes and
must never touch production's schema.

That is deliberately two orthogonal facts, not a privilege ladder:

* :func:`current_scope` answers *who is asking*.
* :func:`is_production_database` answers *what they are pointing at*.

:func:`migration_decision` combines them.  A non-daemon process pointed at
production gets :data:`VERIFY` — read the stamped revision, compare it to the
code's head, and fail loudly when it is behind ("schema behind code; ask the
operator to upgrade") instead of quietly migrating.  Everything else gets
:data:`MIGRATE`, which is the historical behaviour and keeps every
``tmp_path`` SQLite database in the test suite working unchanged.

Scope resolution order matters
------------------------------

``AQ_DB_SCOPE`` in the environment beats the process-wide scope a daemon sets
for itself with :func:`set_process_scope`.  That is the whole point: a worker
session carries ``AQ_DB_SCOPE=worker`` (see
:func:`src.sessions.env.build_session_env`), so an ``aq start`` *inside a
worktree slot* still resolves to ``worker`` and still refuses, even though
the daemon it boots declares itself the daemon.  An operator who genuinely
needs to migrate from an unusual place sets ``AQ_DB_SCOPE=operator``
explicitly, which is a decision they can be held to rather than an accident.
"""

from __future__ import annotations

import logging
import os
import sys
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

__all__ = [
    "CLI",
    "DAEMON",
    "MIGRATE",
    "OPERATOR",
    "SCOPES",
    "SCOPE_ENV",
    "TEST",
    "VERIFY",
    "WORKER",
    "MigrationRefused",
    "SchemaBehindCode",
    "assert_not_production_database",
    "current_scope",
    "is_production_database",
    "migration_decision",
    "normalize_database_url",
    "process_scope",
    "production_database_url",
    "same_database",
    "set_process_scope",
]

#: The daemon process itself — the only long-lived owner of the schema.
DAEMON = "daemon"
#: An operator who explicitly asked for a migration (``aq db upgrade``).
OPERATOR = "operator"
#: A session launched into a worktree slot.  Never migrates production.
WORKER = "worker"
#: A human or script at a terminal that did not ask for a migration.
CLI = "cli"
#: pytest.  Free to migrate its own scratch databases, never production.
TEST = "test"

SCOPES: frozenset[str] = frozenset({DAEMON, OPERATOR, WORKER, CLI, TEST})

#: Environment override, and the marker every worker session carries.
SCOPE_ENV = "AQ_DB_SCOPE"

#: :func:`migration_decision` outcomes.
MIGRATE = "migrate"
VERIFY = "verify"

#: Where the daemon's configuration lives.  Read directly rather than through
#: :func:`src.config.load_config` so that resolving "what is production?"
#: never depends on a config that parses, and never costs a full AppConfig.
_CONFIG_PATH = os.path.join(os.path.expanduser("~/.agent-queue"), "config.yaml")

_POSTGRES_SCHEMES = ("postgresql", "postgres")

# Process-wide scope declared in code (``set_process_scope``).  ``None`` means
# "nobody has declared one", which is the normal state for the CLI and tests.
_process_scope: str | None = None


class MigrationRefused(RuntimeError):
    """Raised when a process may not migrate the database it is pointed at."""


class SchemaBehindCode(MigrationRefused):
    """The production schema is older than this checkout, and we may not fix it."""


def set_process_scope(scope: str | None) -> str | None:
    """Declare this process's scope; returns the previous value.

    Called once by the daemon at startup and by ``aq db upgrade``.  An
    ``AQ_DB_SCOPE`` value in the environment still wins (see the module
    docstring), so this cannot be used to escape a worker session.
    """
    global _process_scope
    if scope is not None and scope not in SCOPES:
        raise ValueError(f"unknown database scope {scope!r}; expected one of {sorted(SCOPES)}")
    previous, _process_scope = _process_scope, scope
    return previous


class process_scope:
    """Context manager form of :func:`set_process_scope`."""

    def __init__(self, scope: str) -> None:
        self._scope = scope
        self._previous: str | None = None

    def __enter__(self) -> str:
        self._previous = set_process_scope(self._scope)
        return self._scope

    def __exit__(self, *exc: object) -> None:
        set_process_scope(self._previous)


def current_scope() -> str:
    """Resolve who is asking for a migration.

    Order: ``AQ_DB_SCOPE`` → a scope declared by :func:`set_process_scope` →
    a worker session marker in the environment → pytest → plain CLI.
    """
    override = (os.environ.get(SCOPE_ENV) or "").strip().lower()
    if override in SCOPES:
        return override
    if override:
        logger.warning("Ignoring unknown %s=%r", SCOPE_ENV, override)
    if _process_scope is not None:
        return _process_scope
    if os.environ.get("AQ_SESSION_ID"):
        return WORKER
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return TEST
    return CLI


def normalize_database_url(url: str | None) -> str:
    """A canonical, comparable spelling of *url*.

    SQLite paths become resolved absolute paths; PostgreSQL DSNs lose their
    driver suffix, credentials and query string and keep only
    ``postgresql://host:port/dbname``.  Two URLs that normalize equal address
    the same database, which is the only question this module asks of them.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    scheme = raw.split("://", 1)[0].split("+", 1)[0].lower() if "://" in raw else ""
    if scheme in _POSTGRES_SCHEMES:
        parts = urlsplit(raw)
        host = (parts.hostname or "localhost").lower()
        if host in {"127.0.0.1", "::1", "localhost"}:
            host = "localhost"
        port = parts.port or 5432
        name = parts.path.lstrip("/")
        return f"postgresql://{host}:{port}/{name}"
    if scheme in {"sqlite", "sqlite3"}:
        # SQLAlchemy spells an absolute path with four slashes and a relative
        # one with three, so exactly one leading slash is the separator and
        # the rest is the path.
        raw = raw.split("://", 1)[1].split("?", 1)[0]
        raw = raw.removeprefix("/")
    if not raw or raw == ":memory:" or "mode=memory" in raw:
        return ""
    return os.path.realpath(os.path.abspath(os.path.expanduser(raw)))


def same_database(left: str | None, right: str | None) -> bool:
    """Whether two URLs address the same database.  Empty never matches."""
    a, b = normalize_database_url(left), normalize_database_url(right)
    return bool(a) and a == b


def production_database_url(config_path: str | None = None) -> str:
    """The daemon's configured database URL, read straight from config.yaml.

    Deliberately ignores ``AGENT_QUEUE_DB`` and ``AQ_DATABASE_URL``: those are
    the *overrides* worker sessions are given to point somewhere harmless, and
    letting an override redefine "production" would disarm the guard exactly
    where it matters.  Returns ``""`` when there is no config file, no
    database URL in it, or it cannot be parsed — an install with no configured
    production database has nothing to protect.
    """
    path = config_path or os.environ.get("AQ_CONFIG_PATH") or _CONFIG_PATH
    import yaml

    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("Could not read production database URL from %s: %s", path, exc)
        return ""
    if not isinstance(raw, dict):
        return ""
    section = raw.get("database")
    url = section.get("url") if isinstance(section, dict) else None
    return str(url or raw.get("database_path") or "")


def is_production_database(url: str | None, config_path: str | None = None) -> bool:
    """Whether *url* is the database the daemon is configured to own."""
    return same_database(url, production_database_url(config_path))


def migration_decision(url: str | None, *, scope: str | None = None) -> str:
    """:data:`MIGRATE` or :data:`VERIFY` for *url* under the current scope."""
    if not is_production_database(url):
        return MIGRATE
    if (scope or current_scope()) in {DAEMON, OPERATOR}:
        return MIGRATE
    return VERIFY


def refusal_message(url: str | None, *, detail: str, scope: str | None = None) -> str:
    """The one message every refusal shares, plus a case-specific *detail*."""
    from src.database import redact_dsn

    return (
        f"{detail} This process ({SCOPE_ENV}={scope or current_scope()}) may not run "
        f"Alembic migrations against the production database "
        f"({redact_dsn(normalize_database_url(url))}). Only the daemon, or an operator "
        "running `aq db upgrade`, may change the production schema. Worker sessions must "
        "use a scratch database — see docs/guides/migrations.md."
    )


def assert_not_production_database(url: str | None, *, actor: str) -> None:
    """Raise unless *url* is something other than the production database.

    Used by ``tests/conftest.py`` so the suite can never be pointed at the
    daemon's real database, whatever the environment says.
    """
    if is_production_database(url):
        from src.database import redact_dsn

        raise MigrationRefused(
            f"{actor} refused: {redact_dsn(normalize_database_url(url))} is the daemon's "
            "configured production database (~/.agent-queue/config.yaml). Point it at a "
            "scratch database instead — the test suite runs migrations, and stamping "
            "production with an unmerged branch's revisions is what broke the daemon on "
            "2026-09-02."
        )
