"""Leaf imports and pytest collection must not initialize the application graph."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "src.playbooks", "src.commands.command_inventory", "src.commands.contracts",
    "src.plugins.registry",
)


def _run(source: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        env={**os.environ, "AQ_TEST_PG_MODE": "pure"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("statement", [
    "import src.database",
    "from src.database import redact_dsn",
    "from src.database import create_database",
    "from src.database import DatabaseBackend",
    "from src.database.queries import ProjectQueryMixin",
    "from src.database.queries import TaskQueryMixin",
    "import tests.db_fixtures",
    "import tests.reuse_databases",
    "from src.database.dsn import redact_dsn",
    "import tests.conftest",
])
def test_leaf_import_does_not_load_application(statement):
    _run(f"""
        import sys
        {statement}
        forbidden = {FORBIDDEN!r}
        loaded = [name for name in sys.modules
                  if any(name == p or name.startswith(p + '.') for p in forbidden)]
        assert not loaded, loaded
        assert 'src.database.adapters.postgresql' not in sys.modules
        assert 'src.database.queries.job_queries' not in sys.modules
        if {statement!r} == 'import tests.conftest':
            assert 'tests.db_fixtures' not in sys.modules
    """)


def test_root_lease_forwarder_uses_the_current_pool(monkeypatch):
    from tests import conftest, db_fixtures

    monkeypatch.setattr(db_fixtures, "lease_dsn", lambda name: "leased:" + name)
    assert conftest.lease_dsn("fixture") == "leased:fixture"


def test_public_database_exports_preserve_identity():
    _run("""
        import src.database as database
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
        from src.database.base import DatabaseBackend
        from src.database.factory import create_database
        from src.database.dsn import redact_dsn

        assert database.Database is PostgreSQLDatabaseAdapter
        assert database.PostgreSQLDatabaseAdapter is PostgreSQLDatabaseAdapter
        assert database.DatabaseBackend is DatabaseBackend
        assert database.create_database is create_database
        assert database.redact_dsn is redact_dsn
        assert set(database.__all__) <= set(dir(database))
        try:
            database.missing_export
        except AttributeError:
            pass
        else:
            raise AssertionError('unknown database export was accepted')
    """)


def test_query_exports_preserve_identity():
    _run("""
        import importlib
        import src.database.queries as queries
        assert set(queries.__all__) <= set(dir(queries))
        for name in queries.__all__:
            exported = getattr(queries, name)
            module = importlib.import_module(exported.__module__)
            assert exported is getattr(module, name)
        try:
            queries.missing_export
        except AttributeError:
            pass
        else:
            raise AssertionError('unknown query export was accepted')
    """)


def test_leaf_collection_does_not_load_application():
    _run(f"""
        import sys
        import pytest

        result = pytest.main(['--collect-only', '-q', '-p', 'no:xdist',
                              'tests/test_knowledge_budget.py'])
        assert result == 0, result
        forbidden = {FORBIDDEN!r}
        loaded = [name for name in sys.modules
                  if any(name == p or name.startswith(p + '.') for p in forbidden)]
        assert not loaded, loaded
        assert 'src.database.adapters.postgresql' not in sys.modules
    """)
