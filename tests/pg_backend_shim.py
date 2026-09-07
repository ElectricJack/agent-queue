"""Run the existing SQLite-shaped test suite against PostgreSQL, unmodified.

Opt in with ``AQ_TEST_BACKEND=postgres``.  Every ``Database(str(tmp_path /
"x.db"))`` in the suite then yields a :class:`PostgreSQLDatabaseAdapter` bound
to a database leased from this worker's pool, and the same path inside one
test always resolves to the same database (tests that reopen a path to check
persistence keep working).

Why a shim rather than a codemod: 194 test files construct a SQLite database
directly.  Converting them is T6 of the SQLite removal, and it is much safer to
do *after* the Postgres substrate is proven than before — with the shim you can
flip one env var, see exactly which tests break on Postgres semantics, and fix
those, instead of landing a 194-file rewrite on faith.  It is also how the
substrate gets an apples-to-apples benchmark against the SQLite baseline.

The mechanism: ``src.database.Database`` *is* ``SQLiteDatabaseAdapter``, and
``PostgreSQLDatabaseAdapter`` is a sibling class rather than a subclass.
Patching ``SQLiteDatabaseAdapter.__new__`` to return a Postgres adapter means
Python skips ``__init__`` (the returned object is not an instance of ``cls``),
so the SQLite path argument is simply discarded.  Patching the class object
itself — not a module attribute — is what makes this work regardless of whether
a test did ``from src.database import Database`` at module import time.

See ``docs/superpowers/specs/2026-09-07-sqlite-removal-implementation.md`` §T0.
"""

from __future__ import annotations

import os

from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
from src.database.adapters.sqlite import SQLiteDatabaseAdapter


def enabled() -> bool:
    return os.environ.get("AQ_TEST_BACKEND", "").lower() in {"postgres", "postgresql", "pg"}


class _Router:
    """Maps the SQLite paths one test asks for onto leased Postgres databases."""

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

_ORIGINAL_NEW = SQLiteDatabaseAdapter.__new__


def _routed_new(cls, *args, **kwargs):
    path = args[0] if args else kwargs.get("path", "")
    return PostgreSQLDatabaseAdapter(ROUTER.dsn_for(path))


def install() -> None:
    SQLiteDatabaseAdapter.__new__ = _routed_new  # type: ignore[assignment]


def uninstall() -> None:
    SQLiteDatabaseAdapter.__new__ = _ORIGINAL_NEW  # type: ignore[assignment]
