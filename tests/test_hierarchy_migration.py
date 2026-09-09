"""Hierarchy canonicalisation and preflight, independent of retired revisions."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text as sqltext

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.database import hierarchy_migration as hm
from src.models import Project
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"


def _seed(monkeypatch, rows, edges, archived_ids=()):
    """Supply legacy snapshots, including drift prohibited by today's schema."""
    tasks = {tid: (project, parent) for tid, project, parent, _status in rows}
    by_child = {}
    for ordinal, (child, parent) in enumerate(edges):
        by_child.setdefault(child, []).append((parent, float(ordinal)))
    monkeypatch.setattr(hm, "_snapshot", lambda conn: (tasks, by_child, list(archived_ids)))


class TestCanonicalise:
    def test_column_breaks_duplicate_edge_tie(self, monkeypatch):
        _seed(
            monkeypatch,
            [
                ("p1", "x", None, "IN_PROGRESS"),
                ("p2", "x", None, "IN_PROGRESS"),
                ("c", "x", "p2", "READY"),
            ],
            [("c", "p1"), ("c", "p2")],
        )
        plan = hm.canonicalise(None)
        assert plan.parents["c"] == "p2"
        assert [r.reason for r in plan.rejects] == ["duplicate"]
        assert plan.rejects[0].parent_id == "p1"

    def test_column_only_becomes_edge(self, monkeypatch):
        _seed(monkeypatch, [("p", "x", None, "IN_PROGRESS"), ("c", "x", "p", "READY")], [])
        plan = hm.canonicalise(None)
        assert plan.parents == {"c": "p"}
        assert plan.rejects == []

    def test_orphan_edge_is_rejected_not_dropped(self, monkeypatch):
        """``apply`` deletes every parent-child edge and reinserts only the
        plan's, so an edge whose task row is gone must be recorded."""
        _seed(monkeypatch, [("p", "x", None, "IN_PROGRESS")], [("ghost", "p")])
        plan = hm.canonicalise(None)
        orphans = [r for r in plan.rejects if r.task_id == "ghost"]
        assert len(orphans) == 1
        assert (orphans[0].parent_id, orphans[0].source, orphans[0].reason) == (
            "p",
            "edge",
            "not_found",
        )
        assert "ghost" not in plan.parents

    def test_cross_project_parent_is_rejected(self, monkeypatch):
        _seed(monkeypatch, [("p", "x", None, "IN_PROGRESS"), ("c", "y", "p", "READY")], [])
        plan = hm.canonicalise(None)
        assert "c" not in plan.parents
        assert plan.rejects[0].reason == "cross_project"

    def test_cycle_and_depth_rejected(self, monkeypatch):
        _seed(
            monkeypatch,
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
        plan = hm.canonicalise(None)
        reasons = {(r.task_id, r.reason) for r in plan.rejects}
        assert ("d4", "depth") in reasons
        # Both members of the a<->b cycle lose their parent, not just one.
        assert ("a", "cycle") in reasons
        assert ("b", "cycle") in reasons

    def test_depth_severs_shallowest_violator_first(self, monkeypatch):
        # d1 <- d2 <- d3 <- d4 <- d5 (depths 1..5, MAX_STRUCTURAL_DEPTH=3).
        # Severing d4 (the shallowest violator) turns it into a root and
        # brings d5 down to depth 2 along with it, so d5 is never rejected.
        _seed(
            monkeypatch,
            [
                ("d1", "x", None, "IN_PROGRESS"),
                ("d2", "x", None, "IN_PROGRESS"),
                ("d3", "x", None, "IN_PROGRESS"),
                ("d4", "x", None, "IN_PROGRESS"),
                ("d5", "x", None, "READY"),
            ],
            [("d2", "d1"), ("d3", "d2"), ("d4", "d3"), ("d5", "d4")],
        )
        plan = hm.canonicalise(None)
        assert [r.task_id for r in plan.rejects if r.reason == "depth"] == ["d4"]
        assert "d4" not in plan.parents
        assert plan.parents["d5"] == "d4"

    def test_ordinals_backfill_by_id_prefix_across_archive(self, monkeypatch):
        _seed(
            monkeypatch,
            [("p", "x", None, "IN_PROGRESS"), ("p.3", "x", None, "READY")],
            [],
            ["p.7"],
        )
        assert hm.canonicalise(None).ordinals["p"] == 8


class TestPreflightCommand:
    @pytest.fixture
    async def db(self, tmp_path):
        database = Database(lease_dsn("test.db"))
        await database.initialize()
        await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
        yield database
        await database.close()

    @pytest.fixture
    def config(self, tmp_path):
        return AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path / "workspaces"),
            database=DatabaseConfig(url=lease_dsn("test.db")),
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
        report = json.loads(Path(res["report_path"]).read_text(encoding="utf-8"))
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

    async def test_apply_persists_canonical_edges_containers_and_ordinals(self, db):
        async with db._engine.begin() as conn:
            await conn.execute(
                sqltext(
                    "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
                    "status, created_at, updated_at) VALUES "
                    "('p', :pid, NULL, 'Parent', '', 'IN_PROGRESS', 0, 0), "
                    "('p.3', :pid, 'p', 'Child', '', 'READY', 0, 0)"
                ),
                {"pid": PROJECT_ID},
            )
            await conn.execute(
                sqltext(
                    "INSERT INTO archived_tasks (id, project_id, title, description, status, "
                    "created_at, updated_at, archived_at) "
                    "VALUES ('p.7', :pid, 'Archived', '', 'COMPLETED', 0, 0, 0)"
                ),
                {"pid": PROJECT_ID},
            )
            plan = await conn.run_sync(hm.canonicalise)
            assert plan.parents == {"p.3": "p"}
            assert plan.rejects == []
            await conn.run_sync(lambda sync: hm.apply(sync, plan))
            assert (
                await conn.execute(
                    sqltext(
                        "SELECT depends_on_task_id FROM task_dependencies "
                        "WHERE task_id='p.3' AND dep_type='parent-child'"
                    )
                )
            ).scalar_one() == "p"
            assert (
                await conn.execute(
                    sqltext("SELECT value FROM task_metadata WHERE task_id='p' AND key='container'")
                )
            ).scalar_one() == "true"
            assert (
                await conn.execute(sqltext("SELECT next_child_ordinal FROM tasks WHERE id='p'"))
            ).scalar_one() == 8
