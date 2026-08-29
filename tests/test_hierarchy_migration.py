# tests/test_hierarchy_migration.py
"""Revisions A (DDL) and B (canonicalise) — spec §17."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy import text as sqltext

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database, hierarchy_migration as hm
from src.models import Project
from src.orchestrator import Orchestrator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ID = "proj"


def _alembic(db_path: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENT_QUEUE_DB_URL=f"sqlite+aiosqlite:///{db_path}")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "mig.db")


class TestRevisionA:
    def test_upgrade_adds_columns_and_table(self, db_path):
        res = _alembic(db_path, "upgrade", "a1b2c3d4e5f6")
        assert res.returncode == 0, res.stderr
        insp = inspect(create_engine(f"sqlite:///{db_path}"))
        task_cols = {c["name"] for c in insp.get_columns("tasks")}
        assert {
            "next_child_ordinal",
            "created_by_kind",
            "created_by_id",
            "claim_epoch",
            "filed_count",
        } <= task_cols
        sess_cols = {c["name"] for c in insp.get_columns("sessions")}
        assert {
            "claims",
            "agent_id",
            "claim_phase",
            "claim_phase_at",
            "last_claim_epoch",
            "last_claim_result",
        } <= sess_cols
        prof_cols = {c["name"] for c in insp.get_columns("agent_profiles")}
        assert {"min_active", "max_active", "max_claims_per_session"} <= prof_cols
        assert "hierarchy_migration_rejects" in insp.get_table_names()
        idx = {i["name"] for i in insp.get_indexes("tasks")}
        assert "idx_tasks_ready_by_profile" in idx

    def test_downgrade_round_trips(self, db_path):
        assert _alembic(db_path, "upgrade", "a1b2c3d4e5f6").returncode == 0
        res = _alembic(db_path, "downgrade", "-1")
        assert res.returncode == 0, res.stderr
        insp = inspect(create_engine(f"sqlite:///{db_path}"))
        assert "next_child_ordinal" not in {c["name"] for c in insp.get_columns("tasks")}
        assert "hierarchy_migration_rejects" not in insp.get_table_names()


def _seed(engine, rows, edges):
    """rows: (id, project, parent_col, status); edges: (task, parent)."""
    with engine.begin() as c:
        for pid in {r[1] for r in rows}:
            c.execute(
                sqltext("INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (:i, :i, 0)"),
                {"i": pid},
            )
        for tid, proj, parent_col, status in rows:
            c.execute(
                sqltext(
                    "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
                    "status, created_at, updated_at) "
                    "VALUES (:i, :p, :pc, :i, :i, :s, :t, :t)"
                ),
                {"i": tid, "p": proj, "pc": parent_col, "s": status, "t": time.time()},
            )
        for t, p in edges:
            c.execute(
                sqltext(
                    "INSERT INTO task_dependencies (task_id, depends_on_task_id, dep_type) "
                    "VALUES (:t, :p, 'parent-child')"
                ),
                {"t": t, "p": p},
            )


@pytest.fixture
def engine_at_a(db_path):
    assert _alembic(db_path, "upgrade", "a1b2c3d4e5f6").returncode == 0
    return create_engine(f"sqlite:///{db_path}")


class TestCanonicalise:
    def test_column_breaks_duplicate_edge_tie(self, engine_at_a):
        _seed(
            engine_at_a,
            [
                ("p1", "x", None, "IN_PROGRESS"),
                ("p2", "x", None, "IN_PROGRESS"),
                ("c", "x", "p2", "READY"),
            ],
            [("c", "p1"), ("c", "p2")],
        )
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        assert plan.parents["c"] == "p2"
        assert [r.reason for r in plan.rejects] == ["duplicate"]
        assert plan.rejects[0].parent_id == "p1"

    def test_column_only_becomes_edge(self, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "x", "p", "READY")], [])
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        assert plan.parents == {"c": "p"}
        assert plan.rejects == []

    def test_cross_project_parent_is_rejected(self, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "y", "p", "READY")], [])
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        assert "c" not in plan.parents
        assert plan.rejects[0].reason == "cross_project"

    def test_cycle_and_depth_rejected(self, engine_at_a):
        _seed(
            engine_at_a,
            [
                ("a", "x", None, "IN_PROGRESS"),
                ("b", "x", None, "IN_PROGRESS"),
                ("d1", "x", None, "IN_PROGRESS"),
                ("d2", "x", None, "IN_PROGRESS"),
                ("d3", "x", None, "IN_PROGRESS"),
                ("d4", "x", None, "READY"),
            ],
            [("a", "b"), ("b", "a"), ("d2", "d1"), ("d3", "d2"), ("d4", "d3")],
        )
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        reasons = {(r.task_id, r.reason) for r in plan.rejects}
        assert ("d4", "depth") in reasons
        # Both members of the a<->b cycle lose their parent, not just one.
        assert ("a", "cycle") in reasons
        assert ("b", "cycle") in reasons

    def test_depth_severs_shallowest_violator_first(self, engine_at_a):
        # d1 <- d2 <- d3 <- d4 <- d5 (depths 1..5, MAX_STRUCTURAL_DEPTH=3).
        # Severing d4 (the shallowest violator) turns it into a root and
        # brings d5 down to depth 2 along with it, so d5 is never rejected.
        _seed(
            engine_at_a,
            [
                ("d1", "x", None, "IN_PROGRESS"),
                ("d2", "x", None, "IN_PROGRESS"),
                ("d3", "x", None, "IN_PROGRESS"),
                ("d4", "x", None, "IN_PROGRESS"),
                ("d5", "x", None, "READY"),
            ],
            [("d2", "d1"), ("d3", "d2"), ("d4", "d3"), ("d5", "d4")],
        )
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        assert [r.task_id for r in plan.rejects if r.reason == "depth"] == ["d4"]
        assert "d4" not in plan.parents
        assert plan.parents["d5"] == "d4"

    def test_ordinals_backfill_by_id_prefix_across_archive(self, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("p.3", "x", None, "READY")], [])
        with engine_at_a.begin() as c:
            c.execute(
                sqltext(
                    "INSERT INTO archived_tasks (id, project_id, title, description, status, "
                    "created_at, updated_at, archived_at) "
                    "VALUES ('p.7', 'x', 'a', 'a', 'COMPLETED', 0, 0, 0)"
                )
            )
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
            hm.apply(conn, plan)
        with engine_at_a.begin() as conn:
            n = conn.execute(sqltext("SELECT next_child_ordinal FROM tasks WHERE id='p'")).scalar()
        assert n == 8


class TestRevisionB:
    def test_fails_on_rejects_but_keeps_report(self, db_path, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "y", "p", "READY")], [])
        res = _alembic(db_path, "upgrade", "b2c3d4e5f6a7")
        assert res.returncode != 0
        with engine_at_a.begin() as conn:
            rows = conn.execute(
                sqltext("SELECT reason FROM hierarchy_migration_rejects")
            ).fetchall()
        assert rows == [("cross_project",)]
        # Schema unchanged: no unique index yet.
        insp = inspect(engine_at_a)
        assert not any(
            i["name"] == "uq_task_deps_single_parent" for i in insp.get_indexes("task_dependencies")
        )

    def test_allow_rejects_env_proceeds(self, db_path, engine_at_a, monkeypatch):
        _seed(
            engine_at_a,
            [
                ("p", "x", None, "IN_PROGRESS"),
                ("c", "y", "p", "READY"),
                ("p2", "x", None, "IN_PROGRESS"),
                ("c2", "x", None, "READY"),
            ],
            [("c2", "p2")],
        )
        monkeypatch.setenv("AQ_MIGRATION_ALLOW_REJECTS", "1")
        res = _alembic(db_path, "upgrade", "b2c3d4e5f6a7")
        assert res.returncode == 0, res.stderr
        with engine_at_a.begin() as conn:
            # The rejected cross-project pointer never lands...
            assert (
                conn.execute(sqltext("SELECT parent_task_id FROM tasks WHERE id='c'")).scalar()
                is None
            )
            # ...but apply() still ran: the valid edge was rewritten.
            assert (
                conn.execute(sqltext("SELECT parent_task_id FROM tasks WHERE id='c2'")).scalar()
                == "p2"
            )
        insp = inspect(engine_at_a)
        assert any(
            i["name"] == "uq_task_deps_single_parent" for i in insp.get_indexes("task_dependencies")
        )
        # The index must be partial (parent-child rows only), not a
        # blanket unique-per-task_id constraint over every dep_type.
        with engine_at_a.begin() as conn:
            idx_sql = conn.execute(
                sqltext(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    "AND name='uq_task_deps_single_parent'"
                )
            ).scalar()
        assert "dep_type = 'parent-child'" in idx_sql

    def test_clean_data_migrates_and_flags_containers(self, db_path, engine_at_a):
        _seed(
            engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "x", None, "READY")], [("c", "p")]
        )
        assert _alembic(db_path, "upgrade", "b2c3d4e5f6a7").returncode == 0
        with engine_at_a.begin() as conn:
            assert (
                conn.execute(sqltext("SELECT parent_task_id FROM tasks WHERE id='c'")).scalar()
                == "p"
            )
            assert (
                conn.execute(
                    sqltext("SELECT value FROM task_metadata WHERE task_id='p' AND key='container'")
                ).scalar()
                == "true"
            )


class TestPreflightCommand:
    @pytest.fixture
    async def db(self, tmp_path):
        database = Database(str(tmp_path / "test.db"))
        await database.initialize()
        await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
        yield database
        await database.close()

    @pytest.fixture
    def config(self, tmp_path):
        return AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path / "workspaces"),
            database_path=str(tmp_path / "test.db"),
            data_dir=str(tmp_path / "data"),
        )

    @pytest.fixture
    def handler(self, db, config):
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = MagicMock()
        orchestrator.complete_session_task = AsyncMock(return_value={"status": "COMPLETED"})
        return CommandHandler(orchestrator, config)

    async def test_reports_rejects_and_persists_them(self, db, handler):
        await db.create_project(Project(id="other", name="Other Project"))
        now = time.time()
        async with db._engine.begin() as conn:
            await conn.execute(
                sqltext(
                    "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
                    "status, created_at, updated_at) "
                    "VALUES ('p', :pid, NULL, 'p', 'p', 'IN_PROGRESS', :t, :t)"
                ),
                {"pid": PROJECT_ID, "t": now},
            )
            await conn.execute(
                sqltext(
                    "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
                    "status, created_at, updated_at) "
                    "VALUES ('c', 'other', 'p', 'c', 'c', 'READY', :t, :t)"
                ),
                {"t": now},
            )

        res = await handler._cmd_db_preflight_hierarchy({})

        assert set(res) == {"success", "run_id", "parents_resolved", "rejects", "report_path"}
        assert res["success"] is False
        assert len(res["rejects"]) == 1
        assert res["rejects"][0]["reason"] == "cross_project"

        assert os.path.exists(res["report_path"])
        with open(res["report_path"], encoding="utf-8") as fh:
            report = json.load(fh)
        assert report["run_id"] == res["run_id"]

        async with db._engine.begin() as conn:
            rows = (
                await conn.execute(
                    sqltext("SELECT reason FROM hierarchy_migration_rejects WHERE run_id = :r"),
                    {"r": res["run_id"]},
                )
            ).fetchall()
        assert [r[0] for r in rows] == ["cross_project"]

    async def test_clean_db_succeeds_with_no_rejects(self, db, handler):
        res = await handler._cmd_db_preflight_hierarchy({})
        assert res["success"] is True
        assert res["rejects"] == []


class TestEnvTransactionPerMigration:
    """Revision B's preflight needs revision A committed before it opens its
    second connection — that only holds with one transaction per migration."""

    def test_online_configure_sets_transaction_per_migration(self):
        import ast

        src = open(os.path.join(ROOT, "migrations", "env.py")).read()
        tree = ast.parse(src)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_do_run_migrations"
        )
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "configure"
        ]
        assert calls, "env.py._do_run_migrations must call context.configure()"
        kwargs = {k.arg: k.value for k in calls[0].keywords}
        assert "transaction_per_migration" in kwargs, (
            "migrations/env.py must pass transaction_per_migration=True "
            "(revision b2c3d4e5f6a7's preflight opens a second connection)"
        )
        assert kwargs["transaction_per_migration"].value is True
