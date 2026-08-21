"""Work-graph WG-2: the typed-edge and label command surface.

Covers docs/specs/implementation/work-graph.md §5 for the parts WG-2 owns:
``dep_type`` on ``add_dependency`` / ``remove_dependency`` / ``create_task``,
the ``waits-for`` deadlock rule at the command boundary, ``labels`` on
create and list, and the ``label.*`` / ``dependency.*`` audit events.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import DepType, Project, Task, TaskStatus
from src.orchestrator import Orchestrator


PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid


# ── add_dependency ───────────────────────────────────────────────────────


class TestAddDependency:
    async def test_defaults_to_blocks(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        res = await handler._cmd_add_dependency({"task_id": "b", "depends_on": "a"})
        assert res["ok"] is True
        assert res["dep_type"] == "blocks"
        assert await db.get_typed_dependencies("b") == [("a", "blocks")]

    async def test_accepts_an_explicit_type(self, handler, db):
        await mktask(db, "parent")
        await mktask(db, "child")
        res = await handler._cmd_add_dependency(
            {"task_id": "child", "depends_on": "parent", "dep_type": "parent-child"}
        )
        assert res["dep_type"] == "parent-child"

    async def test_rejects_an_unknown_type(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        res = await handler._cmd_add_dependency(
            {"task_id": "b", "depends_on": "a", "dep_type": "sometimes-blocks"}
        )
        assert "Invalid dep_type" in res["error"]

    async def test_duplicates_are_per_pair_and_type(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        await handler._cmd_add_dependency({"task_id": "b", "depends_on": "a"})
        # Same pair, different type: allowed.
        res = await handler._cmd_add_dependency(
            {"task_id": "b", "depends_on": "a", "dep_type": "discovered-from"}
        )
        assert res["ok"] is True
        # Same pair, same type: rejected.
        res = await handler._cmd_add_dependency({"task_id": "b", "depends_on": "a"})
        assert "already exists" in res["error"]

    async def test_cycles_are_rejected_for_blocking_types(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        await handler._cmd_add_dependency({"task_id": "b", "depends_on": "a"})
        res = await handler._cmd_add_dependency(
            {"task_id": "a", "depends_on": "b", "dep_type": "parent-child"}
        )
        assert "Cannot add dependency" in res["error"]

    async def test_provenance_edges_may_point_backwards(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        await handler._cmd_add_dependency({"task_id": "b", "depends_on": "a"})
        res = await handler._cmd_add_dependency(
            {"task_id": "a", "depends_on": "b", "dep_type": "discovered-from"}
        )
        assert res["ok"] is True

    async def test_waits_for_rejects_a_descendant_waiter(self, handler, db):
        await mktask(db, "container")
        await mktask(db, "child")
        await handler._cmd_add_dependency(
            {"task_id": "child", "depends_on": "container", "dep_type": "parent-child"}
        )
        res = await handler._cmd_add_dependency(
            {"task_id": "child", "depends_on": "container", "dep_type": "waits-for"}
        )
        assert "fan in over itself" in res["error"]

    async def test_waits_for_allows_an_outside_waiter(self, handler, db):
        await mktask(db, "container")
        await mktask(db, "child")
        await mktask(db, "finalize")
        await handler._cmd_add_dependency(
            {"task_id": "child", "depends_on": "container", "dep_type": "parent-child"}
        )
        res = await handler._cmd_add_dependency(
            {"task_id": "finalize", "depends_on": "container", "dep_type": "waits-for"}
        )
        assert res["ok"] is True

    async def test_it_logs_dependency_added(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        await handler._cmd_add_dependency({"task_id": "b", "depends_on": "a"})
        events = await db.get_recent_events(limit=20, task_id="b")
        assert "dependency.added" in [e["event_type"] for e in events]


# ── remove_dependency ────────────────────────────────────────────────────


class TestRemoveDependency:
    async def test_removes_every_type_by_default(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        await db.add_dependency("b", "a", DepType.BLOCKS.value)
        await db.add_dependency("b", "a", DepType.RELATED.value)
        res = await handler._cmd_remove_dependency({"task_id": "b", "depends_on": "a"})
        assert sorted(res["removed_dep_types"]) == ["blocks", "related"]
        assert await db.get_typed_dependencies("b") == []

    async def test_can_target_one_type(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        await db.add_dependency("b", "a", DepType.BLOCKS.value)
        await db.add_dependency("b", "a", DepType.RELATED.value)
        res = await handler._cmd_remove_dependency(
            {"task_id": "b", "depends_on": "a", "dep_type": "blocks"}
        )
        assert res["removed_dep_types"] == ["blocks"]
        assert await db.get_typed_dependencies("b") == [("a", "related")]

    async def test_missing_edge_of_that_type_errors(self, handler, db):
        await mktask(db, "a")
        await mktask(db, "b")
        await db.add_dependency("b", "a", DepType.BLOCKS.value)
        res = await handler._cmd_remove_dependency(
            {"task_id": "b", "depends_on": "a", "dep_type": "related"}
        )
        assert "No dependency found" in res["error"]


# ── create_task ──────────────────────────────────────────────────────────


class TestCreateTaskGraph:
    async def test_labels_are_attached(self, handler, db):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "t", "labels": ["hold:me", "area:api"]}
        )
        assert await db.get_task_labels(res["created"]) == ["area:api", "hold:me"]

    async def test_labels_accept_a_comma_string(self, handler, db):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "t", "labels": "a, b ,,a"}
        )
        assert await db.get_task_labels(res["created"]) == ["a", "b"]

    async def test_depends_on_accepts_bare_ids(self, handler, db):
        await mktask(db, "up", status=TaskStatus.READY)
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "t", "depends_on": ["up"]}
        )
        created = await db.get_task(res["created"])
        assert await db.get_typed_dependencies(created.id) == [("up", "blocks")]
        # A task created with a blocking edge starts DEFINED, not READY.
        assert created.status == TaskStatus.DEFINED
        assert created.is_blocked is True

    async def test_depends_on_accepts_typed_dicts(self, handler, db):
        await mktask(db, "origin", status=TaskStatus.READY)
        res = await handler._cmd_create_task(
            {
                "project_id": PROJECT_ID,
                "title": "t",
                "depends_on": [{"task_id": "origin", "dep_type": "discovered-from"}],
            }
        )
        created = await db.get_task(res["created"])
        # Provenance only — the task is runnable immediately.
        assert created.status == TaskStatus.READY
        assert created.is_blocked is False

    async def test_parent_id_creates_the_container_edge(self, handler, db):
        await mktask(db, "container", status=TaskStatus.DEFINED)
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "t", "parent_id": "container"}
        )
        created = await db.get_task(res["created"])
        assert created.parent_task_id == "container"
        assert ("container", "parent-child") in await db.get_typed_dependencies(created.id)
        # A DEFINED container withholds its children.
        assert created.is_blocked is True

    async def test_unknown_dependency_is_rejected(self, handler, db):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "t", "depends_on": ["nope"]}
        )
        assert "not found" in res["error"]

    async def test_unknown_parent_is_rejected(self, handler, db):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "t", "parent_id": "nope"}
        )
        assert "not found" in res["error"]


# ── list_tasks label filters ─────────────────────────────────────────────


class TestListTaskLabelFilters:
    async def test_all_of_and_any_of(self, handler, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await mktask(db, "b", status=TaskStatus.READY)
        await db.add_task_label("a", "x")
        await db.add_task_label("a", "y")
        await db.add_task_label("b", "x")

        res = await handler._cmd_list_tasks({"project_id": PROJECT_ID, "labels": ["x", "y"]})
        assert [t["id"] for t in res["tasks"]] == ["a"]

        res = await handler._cmd_list_tasks({"project_id": PROJECT_ID, "any_label": ["x"]})
        assert sorted(t["id"] for t in res["tasks"]) == ["a", "b"]

    @pytest.mark.parametrize("display_mode", ["tree", "compact"])
    async def test_hierarchical_modes_honour_the_filter(self, handler, db, display_mode):
        """``--labels x --display-mode tree`` used to return everything: the
        kwargs were built and then dropped on the floor."""
        await mktask(db, "a", status=TaskStatus.READY)
        await mktask(db, "b", status=TaskStatus.READY)
        await db.add_task_label("a", "x")

        res = await handler._cmd_list_tasks(
            {"project_id": PROJECT_ID, "labels": ["x"], "display_mode": display_mode}
        )
        assert [t["root"]["id"] for t in res["trees"]] == ["a"]
        assert res["label_filter_scope"] == "root"

    async def test_hierarchical_modes_are_unchanged_without_a_filter(self, handler, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await mktask(db, "b", status=TaskStatus.READY)
        res = await handler._cmd_list_tasks({"project_id": PROJECT_ID, "display_mode": "tree"})
        assert sorted(t["root"]["id"] for t in res["trees"]) == ["a", "b"]
        assert "label_filter_scope" not in res


# ── task_set label events ────────────────────────────────────────────────


class TestTaskSetLabelEvents:
    async def test_add_and_remove_are_audited(self, handler, db):
        await mktask(db, "t", status=TaskStatus.READY)
        await handler._cmd_task_set({"task_id": "t", "labels_add": ["hold:me"]})
        await handler._cmd_task_set({"task_id": "t", "labels_remove": ["hold:me"]})
        types = [e["event_type"] for e in await db.get_recent_events(limit=20, task_id="t")]
        assert "label.added" in types
        assert "label.removed" in types


# ── gate commands (WG-3) ─────────────────────────────────────────────────


class TestGateCommands:
    async def test_gate_create_happy(self, handler, db):
        await mktask(db, "t1")
        res = await handler._cmd_gate_create(
            {
                "project_id": PROJECT_ID,
                "gate_type": "human",
                "title": "review",
                "waiter_task_ids": ["t1"],
            }
        )
        assert res["success"] is True
        assert res["gate_id"].startswith("gate-")
        # Task blocked by the gate.
        assert (await db.get_task("t1")).is_blocked is True

    async def test_gate_create_missing_fields(self, handler):
        res = await handler._cmd_gate_create({"project_id": PROJECT_ID, "title": "x"})
        assert res["success"] is False
        assert "gate_type" in res["error"]

    async def test_gate_list_and_show(self, handler, db):
        await mktask(db, "t1")
        create = await handler._cmd_gate_create(
            {
                "project_id": PROJECT_ID,
                "gate_type": "human",
                "title": "r",
                "waiter_task_ids": ["t1"],
            }
        )
        gid = create["gate_id"]
        listed = await handler._cmd_gate_list({"project_id": PROJECT_ID})
        assert any(g["id"] == gid for g in listed["gates"])
        shown = await handler._cmd_gate_show({"gate_id": gid})
        assert shown["success"] is True
        assert shown["gate"]["id"] == gid
        assert "t1" in shown["waiters"]

    async def test_gate_resolve_unknown(self, handler):
        res = await handler._cmd_gate_resolve(
            {"gate_id": "gate-nonexistent", "resolved_by": "u"}
        )
        assert res["success"] is False

    async def test_gate_resolve_happy(self, handler, db):
        await mktask(db, "t1")
        create = await handler._cmd_gate_create(
            {
                "project_id": PROJECT_ID,
                "gate_type": "human",
                "title": "r",
                "waiter_task_ids": ["t1"],
            }
        )
        gid = create["gate_id"]
        res = await handler._cmd_gate_resolve({"gate_id": gid, "resolved_by": "jack"})
        assert res["success"] is True
        assert "t1" in res["unblocked_task_ids"]
        assert (await db.get_task("t1")).is_blocked is False

    async def test_gate_resolve_idempotent(self, handler, db):
        await mktask(db, "t1")
        create = await handler._cmd_gate_create(
            {
                "project_id": PROJECT_ID,
                "gate_type": "human",
                "title": "r",
                "waiter_task_ids": ["t1"],
            }
        )
        gid = create["gate_id"]
        first = await handler._cmd_gate_resolve({"gate_id": gid, "resolved_by": "a"})
        second = await handler._cmd_gate_resolve({"gate_id": gid, "resolved_by": "b"})
        assert first["success"] is True
        assert second["success"] is True
        assert second["unblocked_task_ids"] == []
