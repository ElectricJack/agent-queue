"""Regression coverage for the native sub-agent telemetry revision."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn


_MIGRATION = (
    Path(__file__).parents[1]
    / "migrations/versions/33bdb059ceff_subagent_events_and_session_hooks_.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("subagent_events_migration", _MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hooks_provisioned_default_compiles_for_postgresql(monkeypatch):
    """The revision must add a Boolean false default PostgreSQL accepts."""
    migration = _load_migration()
    added_columns = []

    class Batch:
        def __init__(self, table_name: str):
            self.table_name = table_name

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def create_index(self, *_args, **_kwargs):
            pass

        def add_column(self, column):
            added_columns.append((self.table_name, column))

    class Operations:
        def create_table(self, *_args, **_kwargs):
            pass

        def batch_alter_table(self, table_name, **_kwargs):
            return Batch(table_name)

    monkeypatch.setattr(migration, "op", Operations())
    migration.upgrade()

    column = next(column for table, column in added_columns if table == "sessions")
    sql = str(CreateColumn(column).compile(dialect=postgresql.dialect())).lower()
    assert "boolean default false not null" in sql
