"""``sessions.hooks_provisioned`` must default to a *boolean* false.

Revision ``33bdb059ceff`` originally wrote ``server_default=sa.text('0')``,
which SQLite accepts and PostgreSQL rejects outright::

    ALTER TABLE sessions ADD COLUMN hooks_provisioned BOOLEAN DEFAULT 0 NOT NULL
    asyncpg.exceptions.DatatypeMismatchError: column "hooks_provisioned" is of
    type boolean but default expression is of type integer

A fresh PostgreSQL database therefore could not be migrated at all, so the
daemon could not start — found while bringing up the swarm E2E environment.
``sa.false()`` renders as ``false`` on PostgreSQL and ``0`` on SQLite, which
is what every other boolean column in the tree uses.
"""

from __future__ import annotations

import asyncio
import ast
import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from tests.pg_dsn import ensure_worker_postgres_dsn

HOOKS_REVISION = "33bdb059ceff"
PRIOR_REVISION = "009793fbb800"
POSTGRES_DSN = ensure_worker_postgres_dsn()

pytestmark = pytest.mark.migration


def _alembic_config(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _offline_sql(capsys, dialect_url: str) -> str:
    cfg = _alembic_config(dialect_url)
    command.upgrade(cfg, f"{PRIOR_REVISION}:{HOOKS_REVISION}", sql=True)
    return capsys.readouterr().out


def test_postgres_gets_a_boolean_literal_default(capsys):
    sql = _offline_sql(capsys, "postgresql://user:pw@localhost/db")

    assert "ADD COLUMN hooks_provisioned BOOLEAN DEFAULT false NOT NULL" in sql
    assert "hooks_provisioned BOOLEAN DEFAULT 0" not in sql


def test_sqlite_backfills_existing_sessions_as_not_provisioned():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
        engine = create_engine(f"sqlite:///{db_path}")

        command.upgrade(cfg, PRIOR_REVISION)
        with engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO projects (id, name, created_at) VALUES ('p1', 'P1', 1.0)")
            )
            conn.execute(
                sa.text(
                    "INSERT INTO sessions (id, project_id, profile_id, harness, provider, "
                    "name, lifecycle, state, work_dir, epoch, instance_token, started_at) "
                    "VALUES ('s1', 'p1', 'pr', 'claude', 'tmux', 'n-s1', 'named', "
                    "'running', '/wd', 'e', 'tok', 1.0)"
                )
            )

        command.upgrade(cfg, HOOKS_REVISION)

        with engine.connect() as conn:
            got = conn.execute(
                sa.text("SELECT hooks_provisioned FROM sessions WHERE id = 's1'")
            ).scalar()
        assert not got

        command.downgrade(cfg, PRIOR_REVISION)
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(sessions)"))}
        assert "hooks_provisioned" not in columns


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_upgrade_and_downgrade_use_a_boolean_default():
    """Run this revision's real DDL against PostgreSQL, not just offline SQL."""
    import asyncpg

    from tests.pg_dsn import create_scratch_database

    dsn = await create_scratch_database("session_hooks_migration")
    cfg = _alembic_config(dsn)

    # migrations/env.py uses asyncio.run() for its async engine.  Running the
    # Alembic commands in a thread keeps that loop separate from pytest's loop.
    await asyncio.to_thread(command.upgrade, cfg, PRIOR_REVISION)
    await asyncio.to_thread(command.upgrade, cfg, HOOKS_REVISION)

    plain_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(plain_dsn)
    try:
        assert await conn.fetchval(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'sessions' AND column_name = 'hooks_provisioned'"
        ) == "false"
    finally:
        await conn.close()

    await asyncio.to_thread(command.downgrade, cfg, PRIOR_REVISION)
    conn = await asyncpg.connect(plain_dsn)
    try:
        assert await conn.fetchval(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'sessions' AND column_name = 'hooks_provisioned'"
        ) is None
    finally:
        await conn.close()


def _numeric_boolean_defaults(path: Path) -> list[str]:
    """Return ``file:line`` for every Boolean Column with a numeric server_default.

    Handles both the migration spelling (``sa.Column(..., sa.Boolean(), ...)``)
    and the ``tables.py`` spelling (``Column(..., Boolean, ...)``).
    """

    def _is_named(node: ast.expr, name: str) -> bool:
        if isinstance(node, ast.Name):
            return node.id == name
        if isinstance(node, ast.Attribute):
            return node.attr == name
        if isinstance(node, ast.Call):
            return _is_named(node.func, name)
        return False

    offenders: list[str] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_named(node.func, "Column")):
            continue
        if not any(_is_named(arg, "Boolean") for arg in node.args):
            continue
        for kw in node.keywords:
            if kw.arg != "server_default":
                continue
            default = kw.value
            literal = None
            if isinstance(default, ast.Constant):
                literal = default.value
            elif (
                isinstance(default, ast.Call)
                and _is_named(default.func, "text")
                and default.args
                and isinstance(default.args[0], ast.Constant)
            ):
                literal = default.args[0].value
            if isinstance(literal, str) and literal.strip().strip("'\"") in {"0", "1"}:
                offenders.append(f"{path.name}:{default.lineno}")
    return offenders


def test_no_migration_gives_a_boolean_column_a_numeric_default():
    """The same mistake anywhere else would break PostgreSQL the same way."""
    offenders: list[str] = []
    for path in sorted(Path("migrations/versions").glob("*.py")):
        offenders.extend(_numeric_boolean_defaults(path))

    assert offenders == [], (
        "Boolean server_default must be sa.true()/sa.false(), not a numeric "
        f"literal (PostgreSQL rejects it): {offenders}"
    )


def test_tables_metadata_has_no_numeric_boolean_defaults():
    """``tables.py`` is what autogenerate diffs against.

    A numeric default there re-introduces the bug into the *next* generated
    migration even after the current one is fixed, so the metadata must carry
    the same ``false()``/``true()`` the migrations emit.
    """
    offenders = _numeric_boolean_defaults(Path("src/database/tables.py"))

    assert offenders == [], (
        "Boolean server_default in tables.py must be false()/true(), not a "
        f"numeric literal: {offenders}"
    )
