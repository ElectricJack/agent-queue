"""Phases — ordered containers that gate implicitly (graph-visibility A1).

A phase is an ordinary container task carrying ``task_metadata.phase`` and one
``blocks`` edge onto the previous sibling phase.  Nothing else gates: the
persisted ``is_blocked`` projection keeps the later phase DEFINED, and a
DEFINED parent withholds every descendant through the ``parent-child`` rule.
These tests pin that the claim frontier needs no phase-specific clause.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import insert, select, update

from src.database.queries.claim_queries import _frontier_where
from src.database.queries.hierarchy_queries import ProjectIntegrationMode
from src.database.tables import task_branch_origins, tasks
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, TaskStatus

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
    async def test_phase_two_work_is_withheld_until_phase_one_completes(self, handler, orch):
        db = orch.db
        first = await phase(handler, "Phase 1")
        second = await phase(handler, "Phase 2")
        assert first["phase"]["order"] == 1
        assert first["phase"]["blocked_by"] is None
        assert second["phase"]["order"] == 2
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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known hazard: the §7 settlement predicate's 'no non-COMPLETED child' "
            "clause is vacuously true for a container with zero children, so an "
            "empty phase is swallowed the moment the cascade releases it.  The "
            "EXISTS(child) guard that would fix it also stops an *emptied* "
            "container settling, which "
            "tests/test_hierarchy_settlement.py::TestSettlement::"
            "test_emptied_container_settles_on_reparent pins as required "
            "behaviour.  Reported to the plan owner rather than restricted to "
            "phase-flagged containers unilaterally — see task-4-report.md."
        ),
    )
    async def test_a_childless_phase_is_not_settled_by_the_cascade(self, handler, orch):
        db = orch.db
        created = await phase(handler, "Phase 1")
        phase_id = created["phase"]["id"]

        await cascade(orch)
        assert (await db.get_task(phase_id)).status != TaskStatus.COMPLETED

        async with db._engine.begin() as conn:
            result = await db.settle_containers({phase_id}, conn=conn)
        assert result.settled == []
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
