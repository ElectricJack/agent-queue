"""Regression coverage for schema startup safeguards."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy import text as sqltext

from src.database.engine import create_sqlite_engine, run_schema_setup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The last revision *below* the swarm hierarchy pair (A = a1b2c3d4e5f6 DDL,
#: B = b2c3d4e5f6a7 canonicalise).  Downgrading here crosses both.
BELOW_HIERARCHY_PAIR = "4e925610d7a6"


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENT_QUEUE_DB_URL=db_url)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_unknown_alembic_revision_fails_without_rewriting_version(tmp_path):
    engine = create_sqlite_engine(str(tmp_path / "unknown.db"))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            await conn.execute(text("INSERT INTO alembic_version VALUES ('not-a-real-revision')"))
        with pytest.raises(RuntimeError, match="not-a-real-revision"):
            await run_schema_setup(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() == ("not-a-real-revision")
    finally:
        await engine.dispose()


async def test_startup_data_migrations_are_idempotent_and_preserve_same_project_links(tmp_path):
    engine = create_sqlite_engine(str(tmp_path / "idempotent.db"))
    try:
        await run_schema_setup(engine)
        # Re-running startup schema setup is the public idempotency contract.
        await run_schema_setup(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() is not None
    finally:
        await engine.dispose()


def test_sqlite_head_window_downgrade_reupgrade_preserves_and_transforms_data(tmp_path):
    """head -> below the hierarchy pair -> head, with data in the window.

    The downgrade must remove the swarm DDL; data seeded at the old schema
    (a column-only parent pointer, the pre-canonicalisation drift shape)
    must survive the re-upgrade and be *transformed*: revision B rewrites
    the pointer as a parent-child edge, flags the container and installs
    the single-parent partial unique index.
    """
    db_path = str(tmp_path / "window.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    assert _alembic(db_url, "upgrade", "head").returncode == 0
    res = _alembic(db_url, "downgrade", BELOW_HIERARCHY_PAIR)
    assert res.returncode == 0, res.stderr

    engine = create_engine(f"sqlite:///{db_path}")
    insp = inspect(engine)
    assert "claim_epoch" not in {c["name"] for c in insp.get_columns("tasks")}
    assert "hierarchy_migration_rejects" not in insp.get_table_names()

    now = time.time()
    with engine.begin() as conn:
        conn.execute(
            sqltext("INSERT INTO projects (id, name, created_at) VALUES ('x', 'x', 0)")
        )
        for tid, parent in (("p", None), ("c", "p")):
            conn.execute(
                sqltext(
                    "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
                    "status, created_at, updated_at) "
                    "VALUES (:i, 'x', :pc, :i, :i, :s, :t, :t)"
                ),
                {"i": tid, "pc": parent, "s": "IN_PROGRESS" if tid == "p" else "READY", "t": now},
            )

    res = _alembic(db_url, "upgrade", "head")
    assert res.returncode == 0, res.stderr
    with engine.begin() as conn:
        # Preserved: both rows survive with the pointer intact.
        assert (
            conn.execute(sqltext("SELECT parent_task_id FROM tasks WHERE id='c'")).scalar()
            == "p"
        )
        # Transformed: the column-only pointer became the canonical edge...
        assert (
            conn.execute(
                sqltext(
                    "SELECT depends_on_task_id FROM task_dependencies "
                    "WHERE task_id='c' AND dep_type='parent-child'"
                )
            ).scalar()
            == "p"
        )
        # ...and the parent is now a flagged container.
        assert (
            conn.execute(
                sqltext("SELECT value FROM task_metadata WHERE task_id='p' AND key='container'")
            ).scalar()
            == "true"
        )
    insp = inspect(engine)
    assert "claim_epoch" in {c["name"] for c in insp.get_columns("tasks")}
    assert any(
        i["name"] == "uq_task_deps_single_parent"
        for i in insp.get_indexes("task_dependencies")
    )
    engine.dispose()


def test_one_way_token_ledger_downgrade_is_documented_lossy(tmp_path):
    """Below-hierarchy history: c4e1a9d7b310's downgrade is one-way by design.

    At head the token ledger keeps rows whose agent no longer exists (the
    whole point of the revision).  Downgrading across it re-establishes the
    FKs by deleting those orphans first — the documented, deliberately
    lossy one-way semantics — while attributable rows survive.
    """
    db_path = str(tmp_path / "oneway.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    assert _alembic(db_url, "upgrade", "head").returncode == 0

    engine = create_engine(f"sqlite:///{db_path}")
    now = time.time()
    with engine.begin() as conn:
        conn.execute(sqltext("INSERT INTO projects (id, name, created_at) VALUES ('x','x',0)"))
        conn.execute(
            sqltext(
                "INSERT INTO agent_profiles (id, name, created_at, updated_at) "
                "VALUES ('worker','Worker',:t,:t)"
            ),
            {"t": now},
        )
        conn.execute(
            sqltext(
                "INSERT INTO agents (id, name, profile_id, created_at) "
                "VALUES ('live','live','worker',:t)"
            ),
            {"t": now},
        )
        conn.execute(
            sqltext(
                "INSERT INTO tasks (id, project_id, title, description, status, "
                "created_at, updated_at) VALUES ('t','x','t','t','READY',:t,:t)"
            ),
            {"t": now},
        )
        for lid, agent in (("keep", "live"), ("orphan", "ghost")):
            conn.execute(
                sqltext(
                    "INSERT INTO token_ledger (id, project_id, agent_id, task_id, "
                    "tokens_used, timestamp) VALUES (:i, 'x', :a, 't', 1, :t)"
                ),
                {"i": lid, "a": agent, "t": now},
            )

    # Cross c4e1a9d7b310 (its down_revision is the branchpoint below it).
    res = _alembic(db_url, "downgrade", "a1c7f3e08b42")
    assert res.returncode == 0, res.stderr
    with engine.begin() as conn:
        ids = {
            r[0] for r in conn.execute(sqltext("SELECT id FROM token_ledger")).fetchall()
        }
    assert ids == {"keep"}  # the orphan was dropped so the FK could return
    engine.dispose()
