"""The baseline emits a native PostgreSQL boolean default for session hooks."""

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn

from src.database.tables import sessions


@pytest.fixture(autouse=True)
def _pg_backend():
    """Compiling PostgreSQL DDL never opens a database connection."""


def test_hooks_provisioned_default_compiles_for_postgresql():
    sql = str(
        CreateColumn(sessions.c.hooks_provisioned).compile(dialect=postgresql.dialect())
    ).lower()
    assert "boolean default false not null" in sql
