"""Construct the configured PostgreSQL backend without loading it on import."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.config import is_postgres_url
from src.database.dsn import redact_dsn

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.database.base import DatabaseBackend

# Preserve the public factory's logging category.
logger = logging.getLogger("src.database")


def create_database(config: AppConfig) -> DatabaseBackend:
    """Create the PostgreSQL backend from application config.

    Raises when ``database.url`` is not a PostgreSQL DSN.  This used to fall
    through to SQLite for anything unrecognised, which meant a typo'd DSN
    brought the daemon up healthy on an empty file while the real database
    sat untouched.  There is one backend now, so the mistake is a hard error.

    The returned object is not yet initialized — callers must
    ``await db.initialize()`` before use.
    """
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.migration_guard import SchemaAheadPolicy

    db_url = config.database.url
    if not is_postgres_url(db_url):
        raise ValueError(
            "database.url must be a PostgreSQL DSN "
            f"(got {redact_dsn(db_url) or 'empty'}). SQLite is no longer supported; "
            "run `aq db import-sqlite <path>` to carry an old database over."
        )
    logger.info("database url=%s", redact_dsn(db_url))
    return PostgreSQLDatabaseAdapter(
        db_url,
        config.database.pool_min_size,
        config.database.pool_max_size,
        pre_ping=config.database.pre_ping,
        pool_recycle=config.database.pool_recycle_seconds,
        schema_ahead_policy=SchemaAheadPolicy(
            config.database.schema_ahead_migrations,
            config.database.schema_ahead_max_revisions,
        ),
    )
