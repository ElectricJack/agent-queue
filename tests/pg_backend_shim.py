"""Route tests that still construct a database by *file path* onto Postgres.

SQLite is gone: ``src.database.Database`` is now ``PostgreSQLDatabaseAdapter``.
But ~190 test modules still say ``Database(str(tmp_path / "test.db"))``, which
would hand a filesystem path to a Postgres adapter.

This shim intercepts construction and, when the argument is not a PostgreSQL
DSN, substitutes a database leased from this worker's template-cloned pool.
The same path within one test always resolves to the same database, so tests
that reopen a path to check persistence keep working.

It is deliberately **self-eliminating**: it only fires for a non-DSN argument,
so as T6's codemod converts call sites to the ``temp_db`` fixture the shim goes
quiet on its own, and it can be deleted once nothing triggers it.

See ``docs/superpowers/specs/2026-09-07-sqlite-removal-implementation.md`` §T0.
"""

from __future__ import annotations

import importlib

import src.database
from src.config import is_postgres_url
from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter


class _Router:
    """Maps the paths one test asks for onto leased Postgres databases."""

    def __init__(self) -> None:
        self.free: list[str] = []
        self.by_path: dict[str, str] = {}
        self.leased: list[str] = []

    def reset(self, free: list[str]) -> None:
        self.free = list(free)
        self.by_path = {}
        self.leased = []

    def dsn_for(self, path: str) -> str:
        key = str(path)
        if key in self.by_path:
            return self.by_path[key]
        if not self.free:
            raise RuntimeError(
                "pg shim: this test asked for more distinct databases than the "
                f"lease pool holds ({len(self.leased)} already leased). Raise "
                "AQ_TEST_DB_POOL_SIZE."
            )
        dsn = self.free.pop()
        self.by_path[key] = dsn
        self.leased.append(dsn)
        return dsn


ROUTER = _Router()

_ORIGINAL_INIT = PostgreSQLDatabaseAdapter.__init__
_ORIGINAL_CREATE = src.database.create_database


def _routed_init(self, dsn: str, pool_min: int = 2, pool_max: int = 10):
    if not is_postgres_url(dsn):
        dsn = ROUTER.dsn_for(dsn)
    _ORIGINAL_INIT(self, dsn, pool_min, pool_max)


def _routed_create_database(config):
    """``create_database`` for a config that still names a path, not a DSN.

    Production refuses that outright — a non-DSN url used to fall through to
    SQLite and bring the daemon up on an empty file.  Tests still build
    ``AppConfig(database_path=...)``, so here it leases instead of raising.
    """
    url = getattr(config.database, "url", "") or getattr(config, "database_path", "")
    if is_postgres_url(url):
        return _ORIGINAL_CREATE(config)
    return PostgreSQLDatabaseAdapter(
        ROUTER.dsn_for(url or "default"),
        config.database.pool_min_size,
        config.database.pool_max_size,
    )


#: Modules that bound ``create_database`` by name at import time, so patching
#: ``src.database.create_database`` alone would not reach them.
_CREATE_BINDINGS = ("src.database", "src.orchestrator.core")


def install() -> None:
    PostgreSQLDatabaseAdapter.__init__ = _routed_init  # type: ignore[method-assign]
    for mod in _CREATE_BINDINGS:
        module = importlib.import_module(mod)
        if hasattr(module, "create_database"):
            setattr(module, "create_database", _routed_create_database)


def uninstall() -> None:
    PostgreSQLDatabaseAdapter.__init__ = _ORIGINAL_INIT  # type: ignore[method-assign]
    for mod in _CREATE_BINDINGS:
        module = importlib.import_module(mod)
        if hasattr(module, "create_database"):
            setattr(module, "create_database", _ORIGINAL_CREATE)


def enabled() -> bool:
    """Always on: there is no other backend for a path-shaped argument."""
    return True
