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
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.models import DepType, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn


PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
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
            {
                "project_id": PROJECT_ID,
                "title": "t",
                "parent_id": "container",
                "reason": "Split out so the container can track delivery",
            }
        )
        created = await db.get_task(res["created"])
        assert created.parent_task_id == "container"
        assert ("container", "parent-child") in await db.get_typed_dependencies(created.id)
        assert (await db.get_typed_dependencies_detailed(created.id))[0]["description"] == (
            "Split out so the container can track delivery"
        )
        # A DEFINED container withholds its children.
        assert created.is_blocked is True

    async def test_parent_id_yields_hierarchical_dotted_child_id(
        self, handler, db
    ):
        """Nested creates get dotted ids ({parent}.{n}) — hierarchical id
        generation was dead code until ``_cmd_create_task`` was wired to
        pass ``parent_id`` down to ``child_task_id``.
        """
        await mktask(db, "root-x", status=TaskStatus.DEFINED)
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "c1", "parent_id": "root-x"}
        )
        assert res["created"] == "root-x.1"
        res2 = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "c2", "parent_id": "root-x"}
        )
        assert res2["created"] == "root-x.2"

    async def test_depth_cap_creates_root_id_with_discovered_from_edge(
        self, handler, db
    ):
        """A create under a depth-3 parent falls back to a fresh root id
        and gets a ``discovered-from`` edge so provenance survives
        without extending the depth chain past the cap.
        """
        # Depth 3 chain: a → a.1 → a.1.2 (parent already at cap).
        await mktask(db, "a")
        await mktask(db, "a.1", parent_task_id="a")
        await mktask(db, "a.1.2", parent_task_id="a.1")

        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "deep", "parent_id": "a.1.2"}
        )
        cid = res["created"]
        # Fresh root id — no dotted lineage, adjective-noun form.
        assert "." not in cid
        assert "-" in cid
        # discovered-from edge points at the notional parent.
        deps = await db.get_typed_dependencies(cid)
        assert ("a.1.2", "discovered-from") in deps
        # And NOT a parent-child edge (would falsely extend the chain).
        assert ("a.1.2", "parent-child") not in deps
        # parent_task_id is *not* set on the capped fallback — the task
        # is a fresh root, provenance carried by the edge only.
        created = await db.get_task(cid)
        assert created.parent_task_id is None

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
        filtered = await handler._cmd_gate_list({"project_id": PROJECT_ID, "task_id": "t1"})
        assert [g["id"] for g in filtered["gates"]] == [gid]
        assert (await handler._cmd_gate_list({"project_id": PROJECT_ID, "task_id": "missing"}))["gates"] == []
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

    async def test_gate_resolve_emits_task_unblocked_on_bus(self, handler, db):
        """The operator resolve path must emit ``task.unblocked`` on the
        bus for flipped waiters — playbooks that subscribe to blocked-flip
        events would otherwise never fire for gates resolved via
        ``aq gate resolve`` (only for gates resolved via the sweep).
        """
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

        captured: list[tuple[str, dict]] = []

        async def _spy(data):
            captured.append((data.get("_event_type"), data))

        handler.orchestrator.bus.subscribe("task.unblocked", _spy)

        res = await handler._cmd_gate_resolve({"gate_id": gid, "resolved_by": "op"})
        assert res["success"] is True
        assert "t1" in res["unblocked_task_ids"]
        # The command path fires task.unblocked just like _sweep_gates does.
        types = [t for (t, _) in captured]
        assert "task.unblocked" in types
        # And carries the flipped task id.
        payloads = [p for (t, p) in captured if t == "task.unblocked"]
        assert any(p.get("task_id") == "t1" for p in payloads)


# ── parent_key: the keyed standing parent (graph-visibility A2) ──────────


async def standing(db, key="maintenance"):
    """Every task in the project carrying the standing-parent dedup key."""
    from src.database.tables import tasks as tasks_table

    async with db._engine.begin() as conn:
        rows = (
            await conn.execute(
                tasks_table.select().where(
                    tasks_table.c.project_id == PROJECT_ID,
                    tasks_table.c.dedup_key == f"parent:{key}",
                )
            )
        ).mappings().fetchall()
    return [row["id"] for row in rows]


class TestParentKey:
    """``create_task``'s ``parent_key`` — resolve-or-create a standing container.

    Mechanism only: which key an automated creator uses is playbook policy.
    """

    async def test_first_call_creates_the_container_and_files_under_it(self, handler, db):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "CI repair", "parent_key": "maintenance"}
        )
        assert res.get("success") is True, res
        container_ids = await standing(db)
        assert len(container_ids) == 1
        container_id = container_ids[0]
        assert res["parent_id"] == container_id

        container = await db.get_task(container_id)
        assert container is not None
        # Titled from the key, flagged a container at creation, and born
        # DEFINED so nothing can claim it and settlement cannot reach it.
        assert container.title == "Maintenance"
        assert container.parent_task_id is None
        assert container.task_type is not None and container.task_type.value == "chore"
        assert container.status == TaskStatus.DEFINED
        assert await db.get_task_meta(container_id, "container") is True
        assert (await db.get_task(res["created"])).parent_task_id == container_id

    async def test_explicit_parent_title_wins(self, handler, db):
        await handler._cmd_create_task(
            {
                "project_id": PROJECT_ID,
                "title": "CI repair",
                "parent_key": "maintenance",
                "parent_title": "Routine maintenance",
            }
        )
        container_id = (await standing(db))[0]
        assert (await db.get_task(container_id)).title == "Routine maintenance"

    async def test_second_call_reuses_the_same_container(self, handler, db):
        first = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance"}
        )
        second = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "two", "parent_key": "maintenance"}
        )
        assert second["parent_id"] == first["parent_id"]
        assert len(await standing(db)) == 1

    async def test_a_settled_container_is_never_reused(self, handler, db):
        first = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance"}
        )
        old = first["parent_id"]
        await db.transition_task(old, TaskStatus.COMPLETED, force=True)
        second = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "two", "parent_key": "maintenance"}
        )
        assert second["parent_id"] != old
        # The settled container is left exactly as it was — it leaves through
        # normal archival, not by being re-opened.
        assert (await db.get_task(old)).status == TaskStatus.COMPLETED
        assert (await db.get_children(old))[0].id == first["created"]
        assert len(await standing(db)) == 2

    async def test_a_failed_container_is_never_reused(self, handler, db):
        first = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance"}
        )
        await db.transition_task(first["parent_id"], TaskStatus.FAILED, force=True)
        second = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "two", "parent_key": "maintenance"}
        )
        assert second["parent_id"] != first["parent_id"]

    async def test_a_blocked_container_is_still_reused(self, handler, db):
        first = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance"}
        )
        await db.transition_task(first["parent_id"], TaskStatus.BLOCKED, force=True)
        second = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "two", "parent_key": "maintenance"}
        )
        assert second["parent_id"] == first["parent_id"]

    async def test_concurrent_creators_make_exactly_one_container(self, handler, db):
        import asyncio

        results = await asyncio.gather(*[
            handler._cmd_create_task(
                {"project_id": PROJECT_ID, "title": f"t{n}", "parent_key": "maintenance"}
            )
            for n in range(5)
        ])
        assert all(r.get("success") is True for r in results), results
        container_ids = await standing(db)
        assert len(container_ids) == 1, container_ids
        assert {r["parent_id"] for r in results} == set(container_ids)
        # Five children, one container, and no moment at which an empty
        # container was a settle candidate.
        assert len(await db.get_children(container_ids[0])) == 5
        assert await db.settle_candidates() == []

    async def test_a_refused_child_leaves_one_reusable_empty_container(self, handler, db):
        """The container commits before the child, so the child can still be
        refused (a bad profile, an unknown class, a crash) — and the cascade
        runs every five seconds in that window.  The orphan must survive it
        and be reused, not settled and not duplicated."""
        refused = await handler._cmd_create_task({
            "project_id": PROJECT_ID,
            "title": "one",
            "parent_key": "maintenance",
            "profile_id": "no-such-profile",
        })
        assert refused.get("success") is not True
        assert "no-such-profile" in refused["error"]

        container_ids = await standing(db)
        assert len(container_ids) == 1, container_ids
        container_id = container_ids[0]
        assert await db.get_children(container_id) == []

        # The real promotion cascade: DEFINED -> READY -> IN_PROGRESS for a
        # flagged container, then settlement seeds off it.
        for _ in range(3):
            await handler.orchestrator._check_defined_tasks()
        await handler.orchestrator._sweep_container_completion()

        container = await db.get_task(container_id)
        assert container.status != TaskStatus.COMPLETED, container.status
        assert container_id not in await db.settle_candidates()

        # And the next call reuses it rather than creating a second one.
        second = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "two", "parent_key": "maintenance"}
        )
        assert second["parent_id"] == container_id
        assert len(await standing(db)) == 1

    async def test_a_childless_standing_container_is_not_settled(self, handler, db):
        """Straight at the §7 predicate, with a hand-made IN_PROGRESS
        container: neither the backstop sweep nor the event path may
        complete it while it has no children."""
        from src.database.queries.hierarchy_queries import STANDING_PARENT_KEY

        await mktask(db, "standing", status=TaskStatus.IN_PROGRESS)
        async with db.immediate() as conn:
            await db.mark_container("standing", conn=conn)
            await db._upsert_meta("standing", STANDING_PARENT_KEY, {"key": "maintenance"},
                                  conn=conn)

        assert "standing" not in await db.settle_candidates()
        async with db._engine.begin() as conn:
            await db.settle_containers({"standing"}, conn=conn)
        assert (await db.get_task("standing")).status == TaskStatus.IN_PROGRESS

        # It settles as soon as it has held work and that work completed —
        # the standing parent is held open, not immortal.
        await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "child", "parent_id": "standing"}
        )
        child = (await db.get_children("standing"))[0]
        await db.transition_task(child.id, TaskStatus.COMPLETED, force=True)
        assert (await db.get_task("standing")).status == TaskStatus.COMPLETED

    async def test_a_waiter_holds_no_pooled_connection(self, config, tmp_path):
        """The lock is polled, not blocked on.

        A creator that parks inside ``pg_advisory_xact_lock`` keeps its
        pooled connection for the whole wait, so as many concurrent keyed
        creators as the pool has slots occupy every slot and then time out
        together.  With a pool of two, four concurrent creators on one key
        would be that deadlock; they must all succeed instead.
        """
        from unittest.mock import MagicMock

        from src.commands.handler import CommandHandler
        from src.database import Database
        from src.models import Project
        from src.orchestrator import Orchestrator
        from tests.db_fixtures import lease_dsn

        tiny = Database(lease_dsn("tinypool.db"), 1, 1)
        await tiny.initialize()
        try:
            await tiny.create_project(Project(id=PROJECT_ID, name="Tiny pool"))
            orchestrator = Orchestrator(config)
            orchestrator.db = tiny
            orchestrator.git = MagicMock()
            handler = CommandHandler(orchestrator, config)

            import asyncio

            results = await asyncio.gather(*[
                handler._cmd_create_task(
                    {"project_id": PROJECT_ID, "title": f"t{n}", "parent_key": "maintenance"}
                )
                for n in range(4)
            ])
            assert all(r.get("success") is True for r in results), results
            assert len({r["parent_id"] for r in results}) == 1
        finally:
            await tiny.close()

    async def test_the_container_is_flagged_and_marked_standing(self, handler, db):
        from src.database.queries.hierarchy_queries import STANDING_PARENT_KEY

        created = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance"}
        )
        container_id = created["parent_id"]
        assert await db.get_task_meta(container_id, "container") is True
        assert await db.get_task_meta(container_id, STANDING_PARENT_KEY) == {
            "key": "maintenance"
        }

    async def test_reuse_re_asserts_the_marks_on_an_orphaned_container(self, handler, db):
        """A crash between the container row and its marks leaves a bare
        task; the next keyed call must heal it before filing a child."""
        from src.database.tables import task_metadata

        from sqlalchemy import delete

        first = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance"}
        )
        container_id = first["parent_id"]
        async with db._engine.begin() as conn:
            await conn.execute(
                delete(task_metadata).where(task_metadata.c.task_id == container_id)
            )
        assert await db.get_task_meta(container_id, "container") is None

        second = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "two", "parent_key": "maintenance"}
        )
        assert second["parent_id"] == container_id
        assert await db.get_task_meta(container_id, "container") is True

    async def test_the_callers_args_dict_is_not_mutated(self, handler, db):
        args = {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance",
                "parent_title": "Maintenance"}
        before = dict(args)
        await handler._cmd_create_task(args)
        assert args == before

    async def test_parent_key_conflicts_with_parent_id(self, handler, db):
        await mktask(db, "other")
        res = await handler._cmd_create_task(
            {
                "project_id": PROJECT_ID,
                "title": "x",
                "parent_key": "maintenance",
                "parent_id": "other",
            }
        )
        assert res["success"] is False
        assert res["code"] == "hierarchy.parent_conflict"
        assert await standing(db) == []

    async def test_parent_key_conflicts_with_root(self, handler, db):
        res = await handler._cmd_create_task(
            {
                "project_id": PROJECT_ID,
                "title": "x",
                "parent_key": "maintenance",
                "root": True,
            }
        )
        assert res["success"] is False
        assert res["code"] == "hierarchy.parent_conflict"
        assert await standing(db) == []

    async def test_parent_key_is_refused_for_session_principals(self, handler, db):
        handler._current_scope = {
            "kind": "session",
            "session_id": "s1",
            "project_id": PROJECT_ID,
            "elevated": False,
        }
        try:
            res = await handler._cmd_create_task(
                {"project_id": PROJECT_ID, "title": "x", "parent_key": "maintenance"}
            )
        finally:
            handler._current_scope = None
        assert res["success"] is False
        assert res["code"] == "hierarchy.parent_key_not_for_sessions"
        assert await standing(db) == []

    async def test_two_keys_get_two_containers(self, handler, db):
        one = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance"}
        )
        two = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "two", "parent_key": "sentinel"}
        )
        assert one["parent_id"] != two["parent_id"]
        assert len(await standing(db, "maintenance")) == 1
        assert len(await standing(db, "sentinel")) == 1


class TestParentKeyUnderHierarchyMode:
    """A standing parent must not own delivery (final review C1).

    In a hierarchy/train project the child is filed through
    ``file_prepared_child_on``, which bases it on the standing container's
    checkpoint and delivers it to the container's branch — so work that has
    to reach main (the CI main sentinel's repair, say) reaches it only when
    the container settles, which one FAILED sibling prevents forever.  The
    mechanism is therefore refused outright in those projects, before any
    write.
    """

    async def _enable(self, db, tmp_path, mode="hierarchy"):
        from src.models import RepoConfig, RepoSourceType

        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id=PROJECT_ID,
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path / "repo"),
            )
        )
        await db.update_project(
            PROJECT_ID,
            hierarchical_integration_mode=mode,
            integration_repository_id="repo",
        )

    @pytest.mark.parametrize("mode", ["hierarchy", "train"])
    async def test_create_task_is_refused(self, handler, db, tmp_path, mode):
        await self._enable(db, tmp_path, mode)
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "CI repair", "parent_key": "maintenance"}
        )
        assert res["success"] is False
        assert res["code"] == "hierarchy.parent_key_unsupported_mode"
        assert "parent_id" in res["error"]
        # Nothing written: no container, no child.
        assert await standing(db) == []
        assert await db.list_tasks(project_id=PROJECT_ID) == []

    @pytest.mark.parametrize("mode", ["hierarchy", "train"])
    async def test_ensure_task_is_refused(self, handler, db, tmp_path, mode):
        await self._enable(db, tmp_path, mode)
        res = await handler._cmd_ensure_task({
            "project_id": PROJECT_ID,
            "dedup_key": "ci-baseline:abc:1",
            "title": "CI repair",
            "parent_key": "maintenance",
        })
        assert res["success"] is False
        assert res["code"] == "hierarchy.parent_key_unsupported_mode"
        assert await standing(db) == []
        assert await db.list_tasks(project_id=PROJECT_ID) == []

    async def test_a_disabled_project_still_accepts_it(self, handler, db):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "one", "parent_key": "maintenance"}
        )
        assert res.get("success") is True, res
        assert len(await standing(db)) == 1


class TestParentKeyValidation:
    """``parent_key`` is a durable dedup key, so it is bounded like one."""

    @pytest.mark.parametrize(
        "key", ["Maintenance", "-lead", "_lead", "has space", "a" * 65, "sentinel!"]
    )
    async def test_a_malformed_key_is_refused(self, handler, db, key):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "x", "parent_key": key}
        )
        assert res["success"] is False
        assert res["code"] == "hierarchy.parent_key_invalid"
        assert await db.list_tasks(project_id=PROJECT_ID) == []

    @pytest.mark.parametrize("key", ["a", "maintenance", "ci-main-2", "a" * 64])
    async def test_a_well_formed_key_is_accepted(self, handler, db, key):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "x", "parent_key": key}
        )
        assert res.get("success") is True, res


class TestReservedDedupKey:
    """Nothing but the standing-parent path may write a ``parent:`` key.

    A caller that could supply one would adopt (or pre-empt) a standing
    container, which is control-plane state the mechanism owns.
    """

    async def test_create_task_refuses_a_reserved_dedup_key(self, handler, db):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "x", "dedup_key": "parent:maintenance"}
        )
        assert res["success"] is False
        assert res["code"] == "hierarchy.reserved_dedup_key"
        assert "parent_key" in res["error"]
        assert await db.list_tasks(project_id=PROJECT_ID) == []

    async def test_ensure_task_refuses_a_reserved_dedup_key(self, handler, db):
        res = await handler._cmd_ensure_task({
            "project_id": PROJECT_ID,
            "dedup_key": "parent:maintenance",
            "title": "x",
        })
        assert res["success"] is False
        assert res["code"] == "hierarchy.reserved_dedup_key"
        assert await db.list_tasks(project_id=PROJECT_ID) == []

    async def test_the_mechanism_itself_still_writes_one(self, handler, db):
        res = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "x", "parent_key": "maintenance"}
        )
        assert res.get("success") is True, res
        assert len(await standing(db)) == 1
