"""Phases — ordered containers that gate implicitly (graph-visibility A1).

A phase is an ordinary container task carrying ``task_metadata.phase`` and one
``blocks`` edge onto the previous sibling phase.  Nothing else gates: the
persisted ``is_blocked`` projection keeps the later phase DEFINED, and a
DEFINED parent withholds every descendant through the ``parent-child`` rule.
These tests pin that the claim frontier needs no phase-specific clause.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import insert, select, update

from src.database.queries.claim_queries import _frontier_where
from src.database.queries.hierarchy_queries import HierarchyError, ProjectIntegrationMode
from src.database.tables import task_branch_origins, task_dependencies, tasks
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    DepType,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
)

PROJECT_ID = "proj"


@pytest.fixture
async def orch(orchestrator_factory):
    orchestrator = await orchestrator_factory()
    await orchestrator.db.create_project(Project(id=PROJECT_ID, name="Phases"))
    await orchestrator.db.create_profile(
        AgentProfile(id="coder", name="Coder", lifecycle="task")
    )
    await orchestrator.db.update_project(PROJECT_ID, default_profile_id="coder")
    yield orchestrator
    await orchestrator.db.close()


@pytest.fixture
def handler(orch):
    return orch.command_handler


async def phase(handler, title, *, label=None, parent_id=None):
    args = {"project_id": PROJECT_ID, "title": title}
    if label is not None:
        args["label"] = label
    if parent_id is not None:
        args["parent_id"] = parent_id
    result = await handler._cmd_phase_create(args)
    assert result["success"] is True, result
    return result


async def work(handler, title, parent_id):
    result = await handler._cmd_create_task(
        {"project_id": PROJECT_ID, "title": title, "parent_id": parent_id}
    )
    assert result.get("success") is True, result
    return result["created"]


async def cascade(orch, passes=3):
    """Run the deterministic promotion cascade *passes* times.

    One pass promotes a DEFINED container and releases it to IN_PROGRESS;
    only the *next* pass sees its children unblocked.  Three passes is the
    depth of the deepest arrangement these tests build.
    """
    for _ in range(passes):
        await orch._check_defined_tasks()


async def frontier(db, mode=None):
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(select(tasks.c.id).where(_frontier_where(PROJECT_ID, mode)))
        ).scalars().all()
    return set(rows)


async def blocks_edges(db, task_id):
    """The ids *task_id* carries a ``blocks`` edge onto, straight from the table."""
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(task_dependencies.c.depends_on_task_id).where(
                    task_dependencies.c.task_id == task_id,
                    task_dependencies.c.dep_type == DepType.BLOCKS.value,
                )
            )
        ).scalars().all()
    return set(rows)


@pytest.mark.parametrize(
    "mode, expected_repo_id",
    [("development", "phase-repo"), ("observe", "phase-repo"), ("disabled", None)],
)
async def test_standalone_phase_uses_designated_repository(
    handler, orch, tmp_path, mode, expected_repo_id
):
    db = orch.db
    await db.create_repo(
        RepoConfig(
            id="phase-repo",
            project_id=PROJECT_ID,
            source_type=RepoSourceType.LINK,
            source_path=str(tmp_path / "repo"),
        )
    )
    await db.update_project(
        PROJECT_ID,
        hierarchical_integration_mode=mode,
        integration_repository_id="phase-repo",
    )

    result = await phase(handler, "Phase 1")
    assert (await db.get_task(result["phase"]["id"])).repo_id == expected_repo_id


async def planner_session(db, tmp_path, held_id, sid="planner-1", agent_id="planner-agent"):
    """A live, non-elevated planner session holding *held_id*.

    The planner runs `lifecycle: task` like any other worker, so its
    `create_task` calls go down the worker-filing path — which is exactly
    what ``phase_create`` has to place explicitly.
    """
    work_dir = tmp_path / sid
    work_dir.mkdir(parents=True, exist_ok=True)
    await db.create_agent(
        Agent(id=agent_id, name=agent_id, profile_id="planner", state=AgentState.BUSY)
    )
    now = time.time()
    await db.create_session(
        SessionRecord(
            id=sid,
            task_id=held_id,
            project_id=PROJECT_ID,
            profile_id="planner",
            harness="claude",
            provider="fake",
            name=f"n-planner--{sid}",
            lifecycle="task",
            state="running",
            work_dir=str(work_dir),
            epoch="e",
            instance_token=sid,
            started_at=now,
            last_activity=now,
            agent_id=agent_id,
        )
    )
    return sid


def scoped(handler, sid, held_id):
    """Put *handler* in the same non-elevated session scope the API would."""
    handler._current_scope = {
        "kind": "session",
        "session_id": sid,
        "task_id": held_id,
        "project_id": PROJECT_ID,
        "elevated": False,
    }
    return handler


async def claimable(db):
    async with db._engine.begin() as conn:
        return await db.select_ready_for_profile(
            conn,
            project_id=PROJECT_ID,
            profile_id="coder",
            default_profile_id="coder",
            agent_id="agent-1",
        )


# ---------------------------------------------------------------------------
# Step 1 — (a)…(f)
# ---------------------------------------------------------------------------


class TestPhaseGating:
    async def test_concurrent_phase_creation_allocates_distinct_orders(self, handler):
        """The hierarchy lock covers sibling order allocation and metadata write."""
        first, second = await asyncio.gather(
            handler._cmd_phase_create({"project_id": PROJECT_ID, "title": "Phase 1"}),
            handler._cmd_phase_create({"project_id": PROJECT_ID, "title": "Phase 2"}),
        )

        assert first["success"] is True, first
        assert second["success"] is True, second
        assert sorted((first["phase"]["order"], second["phase"]["order"])) == [1, 2]

    async def test_gate_write_failure_removes_the_created_phase(self, handler, orch, monkeypatch):
        await phase(handler, "Phase 1")

        async def fail_last_gate(*_args, **_kwargs):
            raise HierarchyError("cycle", "injected final phase gate failure")

        monkeypatch.setattr(orch.db, "add_dependency", fail_last_gate)
        result = await handler._cmd_phase_create({"project_id": PROJECT_ID, "title": "Phase 2"})

        assert result == {
            "success": False,
            "code": "hierarchy.cycle",
            "error": (
                "hierarchy.cycle: injected final phase gate failure; "
                "phase creation was rolled back"
            ),
        }
        assert [task.title for task in await orch.db.list_tasks(project_id=PROJECT_ID)] == ["Phase 1"]

    async def test_phase_two_work_is_withheld_until_phase_one_completes(self, handler, orch):
        db = orch.db
        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")
        assert first["phase"]["order"] == 1
        assert first["phase"]["previous_phase_id"] is None
        assert first["phase"]["blocked_by"] is None
        assert second["phase"]["order"] == 2
        assert second["phase"]["previous_phase_id"] == first["phase"]["id"]
        assert second["phase"]["blocked_by"] == first["phase"]["id"]

        early = await work(handler, "early", first["phase"]["id"])
        late = await work(handler, "late", second["phase"]["id"])

        await cascade(orch)

        # (a) phase 1's work is on the frontier; phase 2's is not, and no
        # per-task edge was ever written between them.
        assert (await db.get_task(early)).status == TaskStatus.READY
        assert (await db.get_task(early)).is_blocked is False
        assert (await db.get_task(late)).is_blocked is True
        assert await frontier(db) == {early}
        assert await claimable(db) == early

        # (b) completing phase 1's only child settles phase 1 and releases
        # phase 2 and its work.
        await db.transition_task(early, TaskStatus.COMPLETED)
        assert (await db.get_task(first["phase"]["id"])).status == TaskStatus.COMPLETED

        await cascade(orch)
        assert (await db.get_task(second["phase"]["id"])).status == TaskStatus.IN_PROGRESS
        assert (await db.get_task(late)).status == TaskStatus.READY
        assert await frontier(db) == {late}
        assert await claimable(db) == late

    async def test_a_nested_epic_inside_a_gated_phase_is_withheld_too(self, handler, orch):
        db = orch.db
        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")
        early = await work(handler, "early", first["phase"]["id"])
        epic = await work(handler, "epic", second["phase"]["id"])
        deep = await work(handler, "deep", epic)

        await cascade(orch)
        # (c) the grandchild is withheld while its phase is gated.
        assert await frontier(db) == {early}
        assert (await db.get_task(epic)).is_blocked is True
        assert (await db.get_task(deep)).is_blocked is True

        await db.transition_task(early, TaskStatus.COMPLETED)
        await cascade(orch)
        assert await frontier(db) == {deep}
        assert (await db.get_task(epic)).status == TaskStatus.IN_PROGRESS

    async def test_a_failed_child_holds_the_gate(self, handler, orch):
        db = orch.db
        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")
        early = await work(handler, "early", first["phase"]["id"])
        late = await work(handler, "late", second["phase"]["id"])

        await cascade(orch)
        await db.transition_task(early, TaskStatus.FAILED)
        await cascade(orch)

        # (d) a FAILED child never settles its phase, so phase 2 stays shut.
        assert (await db.get_task(first["phase"]["id"])).status == TaskStatus.IN_PROGRESS
        assert (await db.get_task(second["phase"]["id"])).status == TaskStatus.DEFINED
        assert (await db.get_task(late)).is_blocked is True
        assert await frontier(db) == set()

    async def test_failed_phase_hold_is_bounded_and_explained_with_guarded_remedies(
        self, handler, orch
    ):
        db = orch.db
        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")
        failed_ids = [await work(handler, f"failed-{n:02d}", first["phase"]["id"]) for n in range(21)]
        await work(handler, "later", second["phase"]["id"])
        await cascade(orch)
        for task_id in failed_ids[:20]:
            await db.transition_task(task_id, TaskStatus.FAILED)
        await db.transition_task(
            failed_ids[20], TaskStatus.BLOCKED, context="session_close_hard_failure"
        )

        listed = await handler._cmd_phase_list({"project_id": PROJECT_ID})
        hold = listed["phases"][0]["phase_hold"]
        assert hold["phase_id"] == first["phase"]["id"]
        assert hold["failed_children_total"] == 21
        assert len(hold["failed_children"]) == 20
        assert hold["failed_children"][-1]["status"] == TaskStatus.FAILED.value
        assert hold["descendant_blocker_count"] == 21
        assert [remedy["code"] for remedy in hold["remedies"]] == ["retry_or_reopen", "delete"]

        explained = await handler._cmd_explain_task({"task_id": first["phase"]["id"]})
        assert explained["phase_hold"] == hold
        assert "phase_failed_work" in explained["reason_codes"]
        assert "Waiting for failed work" in next(
            reason["detail"] for reason in explained["reasons"] if reason["code"] == "phase_failed_work"
        )

    async def test_phase_explanations_keep_nonfailure_holds_distinct(self, handler, orch):
        db = orch.db
        empty = await phase(handler, "Empty")
        active = await phase(handler, "Active")
        ordinary = await phase(handler, "Ordinary")
        paused = await phase(handler, "Paused")
        active_child = await work(handler, "active child", active["phase"]["id"])
        ordinary_child = await work(handler, "ordinary child", ordinary["phase"]["id"])
        paused_child = await work(handler, "paused child", paused["phase"]["id"])
        await cascade(orch)
        await db.transition_task(ordinary_child, TaskStatus.BLOCKED)
        await db.transition_task(paused_child, TaskStatus.PAUSED)

        expected = {
            empty["phase"]["id"]: "phase_empty",
            active["phase"]["id"]: "phase_active_children",
            ordinary["phase"]["id"]: "phase_child_blocked",
            paused["phase"]["id"]: "phase_manual_pause",
        }
        for task_id, code in expected.items():
            explained = await handler._cmd_explain_task({"task_id": task_id})
            assert code in explained["reason_codes"]
            assert explained["phase_hold"] is None
        assert (await db.get_task(active_child)).status is not TaskStatus.COMPLETED

    async def test_phase_list_reports_order_labels_and_counts(self, handler, orch):
        first = await phase(handler, "Phase 1", label="Foundations")
        second = await phase(handler, "Phase 2", label="Delivery")
        done = await work(handler, "done", first["phase"]["id"])
        await work(handler, "todo", first["phase"]["id"])
        await work(handler, "later", second["phase"]["id"])

        await cascade(orch)
        await orch.db.transition_task(done, TaskStatus.COMPLETED)

        listed = await handler._cmd_phase_list({"project_id": PROJECT_ID})
        assert listed["success"] is True
        # (e) ordered by ``order``, with labels and child counts.
        assert [p["order"] for p in listed["phases"]] == [1, 2]
        assert [p["label"] for p in listed["phases"]] == ["Foundations", "Delivery"]
        assert [p["id"] for p in listed["phases"]] == [
            first["phase"]["id"],
            second["phase"]["id"],
        ]
        assert [p["title"] for p in listed["phases"]] == ["Phase 1", "Phase 2"]
        assert [(p["total"], p["done"]) for p in listed["phases"]] == [(2, 1), (1, 0)]
        assert listed["phases"][0]["status"] == "IN_PROGRESS"
        assert listed["phases"][1]["is_blocked"] is True

    async def test_phase_list_uses_the_scoped_join_not_per_task_metadata_reads(
        self, handler, orch, monkeypatch
    ):
        first = await phase(handler, "Phase 1")
        await phase(handler, "Phase 2")

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("phase list must use its scoped task/metadata join")

        monkeypatch.setattr(orch.db, "list_tasks", unexpected)
        monkeypatch.setattr(orch.db, "get_task_meta", unexpected)
        listed = await handler._cmd_phase_list({"project_id": PROJECT_ID})

        assert [row["id"] for row in listed["phases"]] == [
            first["phase"]["id"],
            listed["phases"][1]["id"],
        ]

    async def test_adding_work_to_a_completed_phase_is_refused(self, handler, orch):
        db = orch.db
        first = await phase(handler, "Phase 1")
        early = await work(handler, "early", first["phase"]["id"])
        await cascade(orch)
        await db.transition_task(early, TaskStatus.COMPLETED)
        assert (await db.get_task(first["phase"]["id"])).status == TaskStatus.COMPLETED

        # (f) the existing container-close rule refuses late work.
        refused = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "too late", "parent_id": first["phase"]["id"]}
        )
        assert refused.get("code") == "hierarchy.container_closed"

    async def test_a_phase_may_sit_under_an_epic(self, handler, orch):
        db = orch.db
        epic = await handler._cmd_create_task({"project_id": PROJECT_ID, "title": "epic"})
        epic_id = epic["created"]
        first = await phase(handler, "Phase 1", parent_id=epic_id)
        second = await phase(handler, "Phase 2", parent_id=epic_id)
        assert first["phase"]["parent_id"] == epic_id
        assert second["phase"]["order"] == 2
        assert second["phase"]["blocked_by"] == first["phase"]["id"]

        early = await work(handler, "early", first["phase"]["id"])
        await cascade(orch)
        assert await frontier(db) == {early}

        # Scoped listing: only the epic's own phases.
        listed = await handler._cmd_phase_list({"project_id": PROJECT_ID, "parent_id": epic_id})
        assert [p["id"] for p in listed["phases"]] == [
            first["phase"]["id"],
            second["phase"]["id"],
        ]
        assert (await handler._cmd_phase_list({"project_id": PROJECT_ID}))["phases"] == []

    async def test_phase_metadata_and_container_flag_are_written_at_creation(self, handler, orch):
        db = orch.db
        created = await phase(handler, "Phase 1", label="Foundations")
        task = await db.get_task(created["phase"]["id"])
        assert task.status == TaskStatus.DEFINED
        assert task.task_type.value == "plan"
        assert task.description == "Foundations"
        assert await db.get_task_meta(created["phase"]["id"], "container") is True
        assert await db.get_task_meta(created["phase"]["id"], "phase") == {
            "order": 1,
            "label": "Foundations",
        }

    async def test_refusals_name_their_cause(self, handler):
        assert (await handler._cmd_phase_create({"title": "x"}))["code"] == "phase.project_required"
        assert (await handler._cmd_phase_create({"project_id": PROJECT_ID}))["code"] == (
            "phase.title_required"
        )
        missing = await handler._cmd_phase_create({"project_id": "nope", "title": "x"})
        assert missing["code"] == "phase.project_not_found"
        bad_parent = await handler._cmd_phase_create(
            {"project_id": PROJECT_ID, "title": "x", "parent_id": "ghost"}
        )
        assert bad_parent["code"] == "phase.parent_not_found"
        assert (await handler._cmd_phase_list({}))["code"] == "phase.project_required"


# ---------------------------------------------------------------------------
# Step 2 — a childless phase must survive the cascade
# ---------------------------------------------------------------------------


class TestChildlessPhase:
    async def test_a_childless_phase_is_never_claimable(self, handler, orch):
        created = await phase(handler, "Phase 1")
        await cascade(orch)
        assert await frontier(orch.db) == set()
        assert await claimable(orch.db) is None
        assert created["phase"]["id"] not in await frontier(orch.db)

    async def test_a_childless_phase_is_not_settled_by_the_cascade(self, handler, orch):
        """A phase is created *before* its work, so it must survive empty.

        The §7 predicate's "no un-COMPLETED child" clause is vacuously true of
        zero children; without ``childless_held_open_container`` the cascade would release
        the phase to IN_PROGRESS and settle it COMPLETED in the same pass,
        after which ``container_closed`` refuses the work it was created for.
        """
        db = orch.db
        created = await phase(handler, "Phase 1")
        phase_id = created["phase"]["id"]

        await cascade(orch)
        assert (await db.get_task(phase_id)).status == TaskStatus.IN_PROGRESS

        async with db._engine.begin() as conn:
            result = await db.settle_containers({phase_id}, conn=conn)
        assert result.settled == []
        assert phase_id not in await db.settle_candidates()

        # And the work it was waiting for is still accepted.
        late = await work(handler, "late", phase_id)
        await cascade(orch)
        assert (await db.get_task(late)).status == TaskStatus.READY

    async def test_a_phase_that_had_children_settles_normally(self, handler, orch):
        """The carve-out is childlessness, not phase-ness.

        Once a phase has held work, it settles on exactly the ordinary §7
        terms — which is what makes the next phase's gate open at all.
        """
        db = orch.db
        created = await phase(handler, "Phase 1")
        phase_id = created["phase"]["id"]
        one = await work(handler, "one", phase_id)
        two = await work(handler, "two", phase_id)

        await cascade(orch)
        await db.transition_task(one, TaskStatus.COMPLETED)
        assert (await db.get_task(phase_id)).status == TaskStatus.IN_PROGRESS
        await db.transition_task(two, TaskStatus.COMPLETED)
        assert (await db.get_task(phase_id)).status == TaskStatus.COMPLETED

    async def test_a_phase_emptied_by_reparenting_stops_settling(self, handler, orch):
        """A phase emptied again is a childless phase again — by design.

        ``test_emptied_container_settles_on_reparent`` pins the opposite for an
        ordinary container: an epic whose last child moves away must complete
        rather than hang.  A phase deliberately does not, because "empty" is
        its normal starting state and more work is expected to arrive.
        """
        db = orch.db
        created = await phase(handler, "Phase 1")
        phase_id = created["phase"]["id"]
        only = await work(handler, "only", phase_id)
        elsewhere = await handler._cmd_create_task(
            {"project_id": PROJECT_ID, "title": "elsewhere"}
        )
        await cascade(orch)
        assert (await db.get_task(phase_id)).status == TaskStatus.IN_PROGRESS

        async with db._engine.begin() as conn:
            await db.set_parent(only, elsewhere["created"], conn=conn)

        assert (await db.get_task(phase_id)).status == TaskStatus.IN_PROGRESS
        assert phase_id not in await db.settle_candidates()


# ---------------------------------------------------------------------------
# Step 4 — the same arrangement under a hierarchy-mode project
# ---------------------------------------------------------------------------


class TestHierarchyMode:
    """The phase ``blocks`` edge joins root siblings that share no parent.

    ``delivered_same_parent_prerequisites_when_hierarchical`` only withholds a
    task whose ``blocks`` prerequisite shares its ``parent_task_id``, so the
    phase edge is invisible to it and phase 2's work is admitted on exactly
    the ordinary hierarchy terms (a materialized origin preserving its
    parent's ref).
    """

    async def _enable_hierarchy(self, db):
        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id=PROJECT_ID,
                source_type=RepoSourceType.LINK,
                source_path="/tmp/phases-repo",
            )
        )
        async with db.immediate() as conn:
            from src.database.tables import projects

            await conn.execute(
                update(projects)
                .where(projects.c.id == PROJECT_ID)
                .values(
                    hierarchical_integration_mode="hierarchy",
                    integration_repository_id="repo",
                )
            )

    async def _materialize(self, db, task_id, parent_id):
        parent_branch = (await db.get_task(parent_id)).branch_name or f"aq/{parent_id}"
        async with db.immediate() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == parent_id).values(branch_name=parent_branch)
            )
            await conn.execute(
                update(tasks).where(tasks.c.id == task_id).values(branch_name=f"aq/{task_id}")
            )
            await conn.execute(
                insert(task_branch_origins).values(
                    id=f"origin-{task_id}",
                    task_id=task_id,
                    repository_id="repo",
                    parent_task_id=parent_id,
                    parent_repository_id="repo",
                    parent_ref=parent_branch,
                    base_sha="a" * 40,
                    creation_generation=0,
                    reserved=True,
                    materialized=True,
                    materialized_at=1.0,
                    created_at=time.time(),
                )
            )

    async def test_phase_gating_is_unchanged_under_hierarchy_mode(self, handler, orch):
        db = orch.db
        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")
        early = await work(handler, "early", first["phase"]["id"])
        late = await work(handler, "late", second["phase"]["id"])

        await self._enable_hierarchy(db)
        await self._materialize(db, early, first["phase"]["id"])
        await self._materialize(db, late, second["phase"]["id"])
        mode = ProjectIntegrationMode.of(await db.get_project(PROJECT_ID))
        assert mode.hierarchical is True

        await cascade(orch)
        assert await frontier(db, mode) == {early}

        await db.transition_task(early, TaskStatus.COMPLETED)
        await cascade(orch)
        assert (await db.get_task(second["phase"]["id"])).status == TaskStatus.IN_PROGRESS
        # Phase 2's work is admitted: the phase edge is between root siblings
        # with no shared parent, so the sibling-delivery predicate ignores it.
        assert await frontier(db, mode) == {late}
        assert await db.hierarchy_runnable_task_ids([late]) == {late}


# ---------------------------------------------------------------------------
# Placement under a real session principal (review finding 1)
# ---------------------------------------------------------------------------


class TestSessionPlacement:
    """A planner holds a task, so its filings default *under that task*.

    `create_task`'s worker-filing path (swarm-work-model §12) reads an omitted
    `parent_id` as "a child of the task I hold".  `phase_create` therefore has
    to say `root` out loud, and must order the phase against the parent the
    database actually recorded.
    """

    async def _held(self, orch):
        await orch.db.create_task(
            Task(
                id="held",
                project_id=PROJECT_ID,
                title="planning",
                description="planning",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        return "held"

    async def test_a_root_phase_lands_at_the_root_not_under_the_held_task(
        self, handler, orch, tmp_path
    ):
        db = orch.db
        held = await self._held(orch)
        sid = await planner_session(db, tmp_path, held)
        scoped(handler, sid, held)

        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")

        # The response says root, and the database agrees.
        assert first["phase"]["parent_id"] is None
        assert second["phase"]["parent_id"] is None
        assert (await db.get_task(first["phase"]["id"])).parent_task_id is None
        assert (await db.get_task(second["phase"]["id"])).parent_task_id is None

        # Order and the gate edge are computed against that same root.
        assert first["phase"]["order"] == 1
        assert first["phase"]["blocked_by"] is None
        assert second["phase"]["order"] == 2
        assert second["phase"]["blocked_by"] == first["phase"]["id"]
        assert await blocks_edges(db, second["phase"]["id"]) == {first["phase"]["id"]}
        assert await blocks_edges(db, first["phase"]["id"]) == set()

        # And phase_list — which reads the root — sees both of them.
        listed = await handler._cmd_phase_list({"project_id": PROJECT_ID})
        assert [p["id"] for p in listed["phases"]] == [
            first["phase"]["id"],
            second["phase"]["id"],
        ]

    async def test_a_phase_with_an_in_scope_parent_lands_there(
        self, handler, orch, tmp_path
    ):
        db = orch.db
        held = await self._held(orch)
        sid = await planner_session(db, tmp_path, held)
        scoped(handler, sid, held)

        first = await phase(handler, "Phase 1", parent_id=held)
        second = await phase(handler, "Phase 2", parent_id=held)

        assert first["phase"]["parent_id"] == held
        assert (await db.get_task(first["phase"]["id"])).parent_task_id == held
        assert (await db.get_task(second["phase"]["id"])).parent_task_id == held
        # Ordered against the held task, not the root.
        assert second["phase"]["order"] == 2
        assert second["phase"]["blocked_by"] == first["phase"]["id"]
        assert await blocks_edges(db, second["phase"]["id"]) == {first["phase"]["id"]}
        # The root has no phases at all, so the two placements cannot be confused.
        assert (await handler._cmd_phase_list({"project_id": PROJECT_ID}))["phases"] == []
        under_held = await handler._cmd_phase_list(
            {"project_id": PROJECT_ID, "parent_id": held}
        )
        assert [p["id"] for p in under_held["phases"]] == [
            first["phase"]["id"],
            second["phase"]["id"],
        ]

    async def test_an_out_of_scope_parent_surfaces_the_filing_refusal(
        self, handler, orch, tmp_path
    ):
        db = orch.db
        held = await self._held(orch)
        await db.create_task(
            Task(
                id="stranger",
                project_id=PROJECT_ID,
                title="stranger",
                description="stranger",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        sid = await planner_session(db, tmp_path, held)
        scoped(handler, sid, held)

        refused = await handler._cmd_phase_create(
            {"project_id": PROJECT_ID, "title": "nope", "parent_id": "stranger"}
        )
        assert refused["success"] is False
        # The filing path's own words, verbatim — the scope refusal carries no
        # code of its own, and none is invented to paper over that.
        assert refused["error"] == (
            "parent must be the held task, one of its descendants, "
            "or the held task's own parent"
        )
        assert refused.get("code") != "phase.create_failed"
        assert await handler._cmd_phase_list({"project_id": PROJECT_ID}) == {
            "success": True,
            "phases": [],
        }

    async def test_an_idle_session_gets_the_filing_paths_own_code(
        self, handler, orch, tmp_path
    ):
        db = orch.db
        sid = await planner_session(db, tmp_path, None, sid="idle-1", agent_id="idle-agent")
        scoped(handler, sid, None)

        refused = await handler._cmd_phase_create({"project_id": PROJECT_ID, "title": "x"})
        assert refused["success"] is False
        assert refused["code"] == "idle_session_cannot_file"


# ---------------------------------------------------------------------------
# The escape hatch for an abandoned empty phase (review finding 2)
# ---------------------------------------------------------------------------


class TestAbandonedPhaseRecovery:
    async def test_deleting_an_abandoned_empty_phase_releases_the_next(
        self, handler, orch
    ):
        """An empty phase never settles, so deletion is the only way out.

        Documented on ``childless_held_open_container()``: because the phase holds its
        ``blocks`` edge shut indefinitely, an operator who abandons one must
        delete it rather than leave it in place.
        """
        db = orch.db
        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")
        late = await work(handler, "late", second["phase"]["id"])

        await cascade(orch)
        assert (await db.get_task(late)).is_blocked is True
        assert await frontier(db) == set()

        deleted = await handler._cmd_delete_task({"task_id": first["phase"]["id"]})
        assert deleted.get("success") is not False, deleted
        assert await db.get_task(first["phase"]["id"]) is None

        await cascade(orch)
        assert (await db.get_task(second["phase"]["id"])).status == TaskStatus.IN_PROGRESS
        assert (await db.get_task(late)).status == TaskStatus.READY
        assert await frontier(db) == {late}
        assert await claimable(db) == late

    async def test_deleting_an_empty_middle_phase_keeps_the_later_gates(
        self, handler, orch
    ):
        """Each phase gates behind EVERY earlier open phase, not just one.

        With a single edge onto the immediate predecessor, deleting an empty
        phase 2 dropped the only edge phase 3 had and released it while
        phase 1 was still open — an operator tidying an abandoned phase
        silently un-gated the rest of the plan.
        """
        db = orch.db
        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")
        third = await phase(handler, "Phase 3")
        assert third["phase"]["previous_phase_id"] == second["phase"]["id"]
        assert third["phase"]["blocked_by"] == second["phase"]["id"]
        assert third["phase"]["blocked_by_all"] == [
            first["phase"]["id"], second["phase"]["id"]
        ]
        early = await work(handler, "early", first["phase"]["id"])
        late = await work(handler, "late", third["phase"]["id"])

        await cascade(orch)
        assert await frontier(db) == {early}

        deleted = await handler._cmd_delete_task({"task_id": second["phase"]["id"]})
        assert deleted.get("success") is not False, deleted

        await cascade(orch)
        # Phase 3 is still gated: phase 1 has not completed.
        assert (await db.get_task(third["phase"]["id"])).is_blocked is True
        assert (await db.get_task(late)).status != TaskStatus.READY
        assert await frontier(db) == {early}

        await db.transition_task(early, TaskStatus.COMPLETED, force=True)
        await cascade(orch)
        assert (await db.get_task(late)).status == TaskStatus.READY

    async def test_a_new_phase_does_not_gate_behind_a_completed_one(self, handler, orch):
        """A completed phase gates nothing; only open earlier phases do."""
        db = orch.db
        first = await phase(handler, "Phase 1")
        await db.transition_task(first["phase"]["id"], TaskStatus.COMPLETED, force=True)
        second = await phase(handler, "Phase 2")
        third = await phase(handler, "Phase 3")

        assert second["phase"]["previous_phase_id"] == first["phase"]["id"]
        assert second["phase"]["blocked_by_all"] == []
        assert third["phase"]["previous_phase_id"] == second["phase"]["id"]
        assert third["phase"]["blocked_by_all"] == [second["phase"]["id"]]
        assert await blocks_edges(db, second["phase"]["id"]) == set()
        assert await blocks_edges(db, third["phase"]["id"]) == {second["phase"]["id"]}


# ---------------------------------------------------------------------------
# Creation through the hierarchy filing service (brief Step 4)
# ---------------------------------------------------------------------------


class TestHierarchyModeCreation:
    """Phases are refused in `hierarchy` and `train` mode, at both doors.

    There a phase container owns a branch and its children deliver *to it*:
    phase *N+1* could open on a base that lacks phase *N*'s work, and one
    FAILED child strands the whole stage's delivery — the same hazard
    ``hierarchy.parent_key_unsupported_mode`` already bars for standing
    parents (design §3.1).  One check, one code, both doors."""

    async def _enable(self, orch, tmp_path, mode="hierarchy"):
        from src.integration.hierarchy import HierarchyIntegration

        db = orch.db
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
        orch.hierarchy_integration = HierarchyIntegration(
            db,
            default_head_resolver=lambda _repo, _branch: "a" * 40,
            checkpoint_verifier=lambda _task, _repo, head_sha: head_sha,
        )

    @pytest.mark.parametrize("mode", ["hierarchy", "train"])
    async def test_phase_create_is_refused_and_writes_nothing(
        self, handler, orch, tmp_path, mode
    ):
        db = orch.db
        await self._enable(orch, tmp_path, mode=mode)

        result = await handler._cmd_phase_create(
            {"project_id": PROJECT_ID, "title": "Phase 1", "label": "Foundations"}
        )

        assert result["success"] is False
        assert result["code"] == "hierarchy.phases_unsupported_mode"
        assert "phase" in result["error"]
        # Refused before ``_cmd_create_task``: no task row at all, not a
        # created-then-unflagged one.
        assert await db.list_tasks(project_id=PROJECT_ID) == []

    @pytest.mark.parametrize("mode", ["hierarchy", "train"])
    async def test_a_phased_graph_is_refused_with_the_same_code(
        self, handler, orch, tmp_path, mode
    ):
        """The graph door reports the one shared refusal, and creates nothing."""
        db = orch.db
        await self._enable(orch, tmp_path, mode=mode)

        result = await handler._cmd_create_task_graph(
            {
                "project_id": PROJECT_ID,
                "graph": {
                    "version": 1,
                    "parent": {"title": "Epic"},
                    "phases": [{"key": "one", "title": "Phase 1"}],
                    "nodes": [
                        {"key": "a", "title": "A", "acceptance": ["x"], "phase": "one"}
                    ],
                },
            }
        )

        assert result["code"] == "hierarchy.phases_unsupported_mode"
        assert await db.list_tasks(project_id=PROJECT_ID) == []

    async def test_an_unphased_graph_is_still_created(self, handler, orch, tmp_path):
        """The refusal is about phases only — hierarchical graphs still file."""
        await self._enable(orch, tmp_path)

        result = await handler._cmd_create_task_graph(
            {
                "project_id": PROJECT_ID,
                "graph": {
                    "version": 1,
                    "parent": {"title": "Epic"},
                    "nodes": [{"key": "a", "title": "A", "acceptance": ["x"]}],
                },
            }
        )

        assert "error" not in result, result
        assert len(result["task_ids"]) == 1


# ---------------------------------------------------------------------------
# Development mode — the gate the operator's projects actually run on
# ---------------------------------------------------------------------------


class TestDevelopmentModeGate:
    """A phased graph gates correctly in ``development`` mode.

    This pins an escape that nothing else covers.  A ``blocks`` edge is
    unsatisfied while the prerequisite is not COMPLETED **or**
    ``_development_delivery_pending(prerequisite)`` is true, and that
    predicate requires ``task.branch_name IS NOT NULL``
    (``src/database/queries/blocked_state.py:146``).  A phase container never
    owns a branch, so the delivery half is always false for an inter-phase
    edge and the gate releases on COMPLETED alone — which for a container
    means every child COMPLETED.  Give a container a ``branch_name`` in
    development mode and every inter-phase gate silently stops releasing.
    """

    async def _enable(self, orch, tmp_path):
        db = orch.db
        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id=PROJECT_ID,
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path / "repo"),
            )
        )
        # With a repository configured, ``_development_delivery_pending``'s
        # join resolves — so the only thing making it false is the container's
        # NULL ``branch_name``.
        await db.update_project(
            PROJECT_ID,
            hierarchical_integration_mode="development",
            integration_repository_id="repo",
        )

    @staticmethod
    def _graph() -> dict:
        return {
            "version": 1,
            "parent": {"title": "Epic"},
            "phases": [
                {"key": "schema", "title": "Phase 1"},
                {"key": "engine", "title": "Phase 2"},
            ],
            "nodes": [
                {"key": "tables", "title": "Tables", "acceptance": ["x"], "phase": "schema"},
                {"key": "cascade", "title": "Cascade", "acceptance": ["x"], "phase": "engine"},
            ],
        }

    async def test_phase_two_releases_only_when_phase_one_settles(
        self, handler, orch, tmp_path
    ):
        db = orch.db
        await self._enable(orch, tmp_path)

        report = await handler._cmd_create_task_graph(
            {"project_id": PROJECT_ID, "graph": self._graph()}
        )
        assert "error" not in report, report
        phases = {p["key"]: p["task_id"] for p in report["phases"]}
        nodes = {n["key"]: n["task_id"] for n in report["nodes"]}

        await cascade(orch)
        assert (await db.get_task(phases["engine"])).is_blocked is True
        assert (await db.get_task(nodes["cascade"])).is_blocked is True
        assert (await db.get_task(nodes["tables"])).status == TaskStatus.READY
        assert await claimable(db) == nodes["tables"]

        await db.transition_task(nodes["tables"], TaskStatus.COMPLETED)
        assert (await db.get_task(phases["schema"])).status == TaskStatus.COMPLETED

        await cascade(orch)
        assert (await db.get_task(phases["engine"])).is_blocked is False
        assert (await db.get_task(phases["engine"])).status == TaskStatus.IN_PROGRESS
        assert (await db.get_task(nodes["cascade"])).status == TaskStatus.READY
        assert await claimable(db) == nodes["cascade"]
