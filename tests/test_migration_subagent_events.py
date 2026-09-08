"""The baseline emits a native PostgreSQL boolean default for session hooks."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn

from src.database.tables import sessions


def test_hooks_provisioned_default_compiles_for_postgresql():
    sql = str(
        CreateColumn(sessions.c.hooks_provisioned).compile(dialect=postgresql.dialect())
    ).lower()
    assert "boolean default false not null" in sql
