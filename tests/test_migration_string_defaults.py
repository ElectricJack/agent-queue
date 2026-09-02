# tests/test_migration_string_defaults.py
"""A string server default must not carry its own SQL quotes.

SQLAlchemy renders a plain-string ``server_default`` as a SQL literal, so
``server_default="'system'"`` emits::

    scope TEXT DEFAULT '''system''' NOT NULL

and the stored default becomes the 8-character string ``'system'`` — quotes
included.  Under a CHECK constraint (``ck_playbook_artifacts_scope``,
``ck_playbook_activations_health``) an insert that omits the column fails
outright; everywhere else it silently writes quote characters into the data.

The correct forms are ``server_default="system"`` (SQLAlchemy adds the
quotes) or ``server_default=sa.text("'system'")``.

Three checks, cheapest first: an AST scan that names the offending file and
line, a DDL-compile scan that catches the same mistake spelled any other way,
and a live insert against the two tables that actually shipped broken.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from src.database import Database
from src.database.tables import metadata, playbook_activations, playbook_artifacts
from tests.pg_dsn import ensure_worker_postgres_dsn

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Files whose ``Column(...)`` declarations reach real DDL.
SOURCES = sorted((ROOT / "migrations" / "versions").glob("*.py")) + [
    ROOT / "src" / "database" / "tables.py"
]

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

#: ``DEFAULT <string literal>`` in emitted DDL, with SQL's doubled-quote escaping.
_DDL_DEFAULT = re.compile(r"DEFAULT\s+'((?:''|[^'])*)'")


def _name_of(node: ast.AST) -> str | None:
    """Trailing identifier of a Name/Attribute/Call, e.g. ``sa.Text()`` -> Text."""
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _self_quoted(value: str) -> bool:
    """True when ``value`` is itself wrapped in SQL single quotes."""
    return len(value) >= 2 and value.startswith("'") and value.endswith("'")


def test_no_string_server_default_carries_its_own_quotes():
    """Source scan: ``server_default="'x'"`` in any Column declaration."""
    offenders: list[str] = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or _name_of(call) != "Column":
                continue
            for keyword in call.keywords:
                if keyword.arg != "server_default":
                    continue
                node = keyword.value
                is_string = isinstance(node, ast.Constant) and isinstance(node.value, str)
                if is_string and _self_quoted(node.value):
                    rel = path.relative_to(ROOT)
                    offenders.append(f"{rel}:{node.lineno}: server_default={node.value!r}")

    assert not offenders, (
        "String server defaults that quote themselves — SQLAlchemy quotes them again, "
        'so the emitted DDL is DEFAULT \'\'\'x\'\'\'. Drop the inner quotes ("x") or '
        'use sa.text("\'x\'"):\n  ' + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("dialect_factory", [sqlite.dialect, postgresql.dialect], ids=["sqlite", "postgresql"])
def test_emitted_ddl_has_no_doubly_quoted_default(dialect_factory):
    """Compile scan: no ``CREATE TABLE`` names a default that is itself a quoted literal.

    Catches the mistake however it is spelled — a raw string, ``sa.text``, or
    a ``DefaultClause`` — because it looks at what the database will actually
    be told.
    """
    dialect = dialect_factory()
    offenders: list[str] = []
    for table in metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        for match in _DDL_DEFAULT.finditer(ddl):
            literal = match.group(1).replace("''", "'")
            if _self_quoted(literal):
                offenders.append(f"{table.name}: {match.group(0)}")

    assert not offenders, (
        f"Emitted {dialect.name} DDL contains defaults that store quote characters:\n  "
        + "\n  ".join(offenders)
    )


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "string-defaults.db"))
        await database.initialize()
    yield database
    await database.close()


async def test_playbook_artifact_insert_may_rely_on_column_defaults(db):
    """The original repro: an insert that omits every defaulted column.

    ``scope`` sits under ``ck_playbook_artifacts_scope``, so a malformed
    default fails the insert rather than merely writing junk.
    """
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256="sha256:" + "a" * 64,
                playbook_id="task-review",
                source_digest="sha256:" + "b" * 64,
                contract_fingerprint="sha256:" + "c" * 64,
                compiler_build="test-build",
                path="/tmp/task-review.json",
                created_at=0.0,
            )
        )
        row = (await conn.execute(select(playbook_artifacts))).mappings().one()

    assert row["scope"] == "system"
    assert row["scope_identifier"] == ""
    assert row["profile_fingerprint"] == ""
    assert row["validation"] == "{}"


async def test_playbook_activation_insert_may_rely_on_column_defaults(db):
    """``health`` sits under ``ck_playbook_activations_health``."""
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(playbook_activations).values(
                activation_id="act-1",
                playbook_id="task-review",
                updated_at=0.0,
            )
        )
        row = (await conn.execute(select(playbook_activations))).mappings().one()

    assert row["scope"] == "system"
    assert row["scope_identifier"] == ""
    assert row["health"] == "disabled"
    assert row["reasons"] == "[]"
