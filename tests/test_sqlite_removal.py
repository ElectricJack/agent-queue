"""Ratchet: SQLite stays removed.

Modelled on ``tests/test_v1_removal.py``.  The removal landed on 2026-09-07
(see ``docs/superpowers/specs/2026-09-07-sqlite-removal-implementation.md``);
these assertions are what stop it growing back one convenience import at a
time.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

#: The one module allowed to know SQLite exists: the one-way importer behind
#: ``aq db import-sqlite``, for operators upgrading from a pre-PostgreSQL
#: release.  It is scheduled for deletion — see its module docstring.
ALLOWED = {SRC / "database" / "legacy_sqlite_import.py"}

#: SQLite *usage*, not the word.  Prose that explains why the backend is gone
#: is wanted, not forbidden — the ratchet exists to stop the code coming back,
#: not the history of it.
_SQLITE_USE = re.compile(
    r"""
      import\s+sqlite3            # the stdlib driver
    | import\s+aiosqlite          # the async driver
    | from\s+sqlite               # ... in any from-import form
    | sqlite\+aiosqlite           # a SQLAlchemy URL
    | sqlite:///                   # ... or a bare one
    | SQLiteDatabaseAdapter        # the deleted adapter
    | create_sqlite_engine         # the deleted engine factory
    | (?<!milvus)\.db["']          # a file-shaped database path literal
    """,
    re.VERBOSE,
)
_DIALECT_BRANCH = re.compile(r"dialect\.name\s*[=!]=")


def _python_sources() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_nothing_uses_sqlite_outside_the_legacy_importer():
    offenders = {}
    for path in _python_sources():
        if path in ALLOWED:
            continue
        hits = [
            f"{path.relative_to(ROOT)}:{n}"
            for n, line in enumerate(path.read_text().splitlines(), 1)
            if _SQLITE_USE.search(line)
        ]
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"SQLite crept back into src/: {offenders}"


def test_the_sqlite_adapter_and_connection_shim_are_gone():
    assert not (SRC / "database" / "adapters" / "sqlite.py").exists()
    assert not (SRC / "database" / "connection.py").exists()


def test_no_dialect_branching_in_the_query_layer():
    """One backend means no code may ask which one it is."""
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _python_sources()
        if p not in ALLOWED
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if _DIALECT_BRANCH.search(line)
    ]
    assert not offenders, f"dialect branching is back: {offenders}"


def test_aiosqlite_is_not_a_runtime_dependency():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    core = " ".join(data["project"]["dependencies"])
    assert "aiosqlite" not in core, "aiosqlite must stay an optional extra"
    assert "asyncpg" in core, "asyncpg is the runtime driver now"
    extras = data["project"]["optional-dependencies"]
    assert "aiosqlite" in " ".join(extras["sqlite-import"])


def test_migration_history_stays_squashed():
    """New revisions must not reintroduce SQLite-shaped migrations.

    ``batch_alter_table`` exists only because SQLite cannot ``ALTER TABLE``;
    on PostgreSQL it is a no-op wrapper that hides what a revision really does.
    """
    import ast

    versions = ROOT / "migrations" / "versions"
    offenders = []
    for path in versions.glob("*.py"):
        tree = ast.parse(path.read_text())
        # Prose may discuss batch mode (the squashed baseline explains why it
        # is gone); only calls and comparisons count.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and "batch_alter_table" in ast.unparse(node.func):
                offenders.append(f"{path.name}: batch_alter_table")
            if isinstance(node, ast.Compare) and "dialect.name" in ast.unparse(node.left):
                offenders.append(f"{path.name}: dialect branch")
    assert not offenders, f"SQLite-shaped migrations: {offenders}"


@pytest.mark.parametrize("url", ["", "~/aq.db", "/var/lib/aq/aq.db", "sqlite:///x.db"])
def test_a_non_postgres_url_is_refused_rather_than_treated_as_a_file(url):
    """The fail-silent path this replaced is the reason the check exists.

    A non-DSN url used to be read as a SQLite file path, so a typo brought the
    daemon up healthy on an empty database while the real one sat untouched.
    """
    from src.config import AppConfig, DatabaseConfig

    errors = AppConfig(database=DatabaseConfig(url=url)).validate()
    assert any("PostgreSQL DSN is required" in e.message for e in errors)


def test_the_alembic_environment_has_no_sqlite_left():
    """``migrations/env.py`` is outside ``src/`` and had its own SQLite arms.

    It kept a ``sqlite+aiosqlite://`` default URL, a ``dialect.name ==
    'sqlite'`` batch-mode branch and a StaticPool swap for months after the
    cutover, so it gets the same ratchet the package sources get.
    """
    env = ROOT / "migrations" / "env.py"
    offenders = [
        f"env.py:{n}: {line.strip()}"
        for n, line in enumerate(env.read_text().splitlines(), 1)
        if _SQLITE_USE.search(line) or _DIALECT_BRANCH.search(line) or "render_as_batch" in line
    ]
    assert not offenders, f"SQLite crept back into the alembic env: {offenders}"
    assert "sqlite" not in (ROOT / "alembic.ini").read_text().lower()


def _alembic_without_a_url(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "AGENT_QUEUE_DB_URL"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("args", [("current",), ("upgrade", "head")])
def test_alembic_with_no_url_configured_refuses_instead_of_making_a_database(args):
    """The footgun this closes: a bare ``alembic upgrade head`` used to succeed.

    With no URL set it resolved ``sqlite+aiosqlite:///~/.agent-queue/agent-queue.db``
    and created or migrated that file, while the real PostgreSQL database sat
    untouched.  It must now fail loudly and touch nothing.
    """
    result = _alembic_without_a_url(*args)
    assert result.returncode != 0
    assert "no database URL configured" in result.stderr + result.stdout
