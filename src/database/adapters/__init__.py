"""Database backend adapters.  PostgreSQL is the only supported backend."""

from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

__all__ = ["PostgreSQLDatabaseAdapter"]
