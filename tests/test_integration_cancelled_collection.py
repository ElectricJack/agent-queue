"""A parent collection cancelled mid-episode is reopened without stranding receipts.

Regression for calm-grove-25 and azure-vault-92 (2026-10-02): ``aq integration
cancel-preserving`` on an expired parent repair cancelled the parent's only
collection operation.  The checkpoint stayed ``awaiting_children`` in its
episode, completed children's receipts stayed bound to that episode, the
collector's fence was released, the stage delegates were archived, and a later
child's conflict intent had no repair.  ``aq integration redrive-child``
refused ("no live collection operation") and ``reserve_episode_on`` returned
the cancelled operation.  ``aq integration reopen-collection`` reactivates it
in place behind a dry run; these tests drive the real promotion, repair,
cancellation and archive paths against a real Git origin.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    agents,
    archived_tasks,
    events,
    integration_branch_owners,
    integration_check_evidence,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    playbook_artifacts,
    projects,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.cancelled_collection_recovery import (
    REOPEN_EVENT,
    CancelledCollectionRecovery,
)
from src.integration.child_delivery import ChildDelivery
from src.integration.collection import CollectionService
from src.integration.delegate_release import archive_obsolete_delegates
from src.integration.development import DevelopmentIntegration
from src.integration.hierarchy import HierarchyIntegration
from src.integration.models import (
    ArtifactSnapshot,
    Fence,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    PromotionInput,
    RepairPolicy,
    RequiredCheckSet,
)
from src.integration.promotion import PromotionConflict, PromotionService
from src.integration.repair import RepairService
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus

_AMBIENT_IDENTITY_KEYS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_DATE",
)


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = {key: value for key, value in os.environ.items() if key not in _AMBIENT_IDENTITY_KEYS}
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _policy() -> tuple[dict, ArtifactSnapshot]:
    artifact = ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test",
        version=1,
    )
    boundary = IntegrationBoundaryPolicy(
        required_checks=RequiredCheckSet(version="test", names=("unit",), producer_id="forge"),
        repair=RepairPolicy(debug_intelligence_class="high", on_exhausted="continue"),
        route=PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            artifact=artifact,
        ),
        primary_intelligence_class="standard",
    )
    policy = HierarchicalIntegrationPolicy(
        parent=boundary, root=boundary, branchless_parent="verifier", on_failed_child="block"
    )
    return policy.model_dump(mode="json"), artifact


def _commit_on(work: Path, branch: str, base: str, path: str, text: str) -> str:
    _git(["switch", "-c", branch, base], work)
    (work / path).write_text(text)
    _git(["add", path], work)
    _git(["commit", "-m", f"{branch} work"], work)
    _git(["push", "origin", branch], work)
    return _git(["rev-parse", "HEAD"], work)


@pytest.fixture
async def case(tmp_path, reuse_database):
    """Train epic ``epic`` with two COMPLETED children that edit the same line.

    ``epic.1`` delivers cleanly; ``epic.2`` then conflicts with it, exactly as
    calm-grove-25.3 conflicted after .1 and .2 were receipted.  ``epic.3`` is
    still to do.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "--initial-branch=main", str(origin)])
    _git(["clone", str(origin), str(work)])
    _git(["config", "user.name", "Seed"], work)
    _git(["config", "user.email", "seed@example.test"], work)
    (work / "shared.txt").write_text("base\n")
    _git(["add", "shared.txt"], work)
    _git(["commit", "-m", "base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "main"], work)
    _git(["push", "origin", f"{base}:refs/heads/aq/epic"], work)
    heads = {
        "epic.1": _commit_on(work, "aq/epic.1", base, "shared.txt", "one\n"),
        "epic.2": _commit_on(work, "aq/epic.2", base, "shared.txt", "two\n"),
    }

    db = await reuse_database("cancelled-collection.db")
    await db.create_project(Project(id="p", name="train project"))
    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(origin),
            default_branch="main",
        )
    )
    policy, artifact = _policy()
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/hierarchy-artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    await db.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        hierarchical_integration_policy=policy,
    )
    hierarchy = HierarchyIntegration(
        db,
        # The collector observes the live parent tip, as production does.
        default_head_resolver=lambda _repo, branch: _git(
            ["ls-remote", str(origin), f"refs/heads/{branch.removeprefix('refs/heads/')}"]
        ).split()[0],
        checkpoint_verifier=lambda _task, _repo, head_sha: head_sha,
    )
    await db.create_task(
        Task(
            id="epic", project_id="p", repo_id="repo", title="epic", description="epic",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await hierarchy.file_children(
        "epic", [{"title": "one"}, {"title": "two"}, {"title": "three"}], 0
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins).values(materialized=True, materialized_at=2.0)
        )
        for child, head in heads.items():
            await conn.execute(
                update(tasks).where(tasks.c.id == child).values(status="COMPLETED", updated_at=3.0)
            )
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == child)
                .values(checkpoint_sha=head, updated_at=3.0)
            )
    bootstrapped = await hierarchy.bootstrap_container_collection("epic")
    assert bootstrapped["outcome"] == "checkpointed"
    promotion = PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager())
    async with db._engine.connect() as conn:
        operation_id = (
            await conn.execute(select(integration_repair_operations.c.id))
        ).scalar_one()
    yield SimpleNamespace(
        db=db, hierarchy=hierarchy, promotion=promotion, base=base, heads=heads,
        work=work, origin=origin, tmp_path=tmp_path, operation_id=operation_id,
    )


def _collector(case):
    return CollectionService(
        case.db,
        hierarchy_service_factory=lambda: case.hierarchy,
        child_delivery=ChildDelivery(case.db, case.promotion),
    )


async def _promote_next(case, now: float):
    """Queue the next approved child and run its promotion; return its intent."""
    before = {row["id"] for row in await _rows(case.db, integration_outbox)}
    assert await _collector(case).collect_parent("epic", now) == "queued"
    [ready] = [
        row for row in await _rows(case.db, integration_outbox)
        if row["id"] not in before and row["event_type"] == "delivery.ready"
    ]
    payload = ready["payload"]
    request = PromotionInput(
        operation_key=payload["operation_key"],
        source_task_id=payload["source_task_id"],
        source_head=payload["source_head"],
        source_base=payload["source_base"],
        expected_target=payload["expected_target"],
        fence=Fence.model_validate(payload["fence"]),
    )
    try:
        prepared = await case.promotion.prepare(request)
    except PromotionConflict:
        return None
    await case.promotion.push(prepared.intent_id, request.fence)
    await case.promotion.reconcile(prepared.intent_id)
    return prepared.intent_id


async def _rows(db, table, *where) -> list[dict]:
    async with db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(table).where(*where))).mappings()]


async def _operation(case) -> dict:
    [row] = await _rows(
        case.db, integration_repair_operations,
        integration_repair_operations.c.id == case.operation_id,
    )
    return row


async def _owner(case) -> dict:
    [row] = await _rows(
        case.db, integration_branch_owners, integration_branch_owners.c.ref == "aq/epic"
    )
    return row


def _remote_tip(case) -> str:
    return _git(["ls-remote", str(case.origin), "refs/heads/aq/epic"]).split()[0]


async def _deliver_first_and_conflict_second(case) -> dict:
    assert await _promote_next(case, 10.0) is not None
    assert await _promote_next(case, 11.0) is None
    [conflict] = await _rows(
        case.db, integration_promotion_intents,
        integration_promotion_intents.c.state == "conflict",
    )
    assert conflict["source_task_id"] == "epic.2"
    assert conflict["expected_target"] == _remote_tip(case)
    return conflict


async def _cancel(case, reason="obsolete expired repair") -> dict:
    development = DevelopmentIntegration(case.db, data_dir=case.tmp_path, git=GitManager())
    result = await development.cancel_preserving(case.operation_id, reason=reason)
    assert result["outcome"] == "cancelled"
    return result


async def _expired_repair_then_cancel(case, conflict) -> str:
    """Stage 0 repairs the conflict, then the operator cancels and the delegate archives."""
    repair = RepairService(case.db)
    started = await repair.start(case.operation_id, conflict["expected_target"], case.operation_id)
    assert started["outcome"] == "started"
    dispatched = await repair.dispatch(case.operation_id, 0)
    assert dispatched["outcome"] == "dispatched"
    delegate = dispatched["repair_task_id"]
    await _cancel(case)
    archived = await archive_obsolete_delegates(case.db, operation_ids=[case.operation_id])
    assert archived["archived_delegates"] == [delegate]
    return delegate


def _recovery(case, dispatch=None):
    return CancelledCollectionRecovery(case.db, case.promotion, dispatch=dispatch)


# ---------------------------------------------------------------------------
# The production shape: receipts, one later conflict, an archived delegate
# ---------------------------------------------------------------------------


async def test_cancelled_collection_strands_every_existing_path(case):
    """The gap itself: nothing could continue the parent before this control."""
    conflict = await _deliver_first_and_conflict_second(case)
    await _expired_repair_then_cancel(case, conflict)

    assert await _collector(case).collect_parent("epic", 20.0) == "waiting"
    redrive = await ChildDelivery(case.db, case.promotion).diagnose("epic.2")
    assert redrive["outcome"] == "blocked"
    assert "no live collection operation" in redrive["reason"]
    assert "aq integration reopen-collection epic" in redrive["reason"]
    async with case.db.immediate() as conn:
        parent = (await conn.execute(select(tasks).where(tasks.c.id == "epic"))).mappings().one()
        project = (await conn.execute(select(projects).where(projects.c.id == "p"))).mappings().one()
        [checkpoint] = (await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "epic"
        ))).mappings().all()
        same = await case.hierarchy.parent_completion.reserve_episode_on(
            conn, parent=dict(parent), project=dict(project), checkpoint=dict(checkpoint),
            pre_collection_sha=checkpoint["checkpoint_sha"],
        )
    assert (same["id"], same["state"]) == (case.operation_id, "cancelled")


async def test_reopen_resumes_the_episode_with_a_fresh_repair_for_the_conflict(case):
    conflict = await _deliver_first_and_conflict_second(case)
    old_delegate = await _expired_repair_then_cancel(case, conflict)
    [receipt] = await _rows(case.db, task_delivery_receipts)
    released = await _owner(case)
    assert released["handoff_state"] == "released"
    dispatched = []

    async def dispatch(operation_id, stage):
        result = await RepairService(case.db).dispatch(operation_id, stage)
        dispatched.append(result)
        return result

    recovery = _recovery(case, dispatch)
    diagnosis = await recovery.run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    assert diagnosis["head_sha"] == conflict["expected_target"] == _remote_tip(case)
    assert diagnosis["operation_id"] == case.operation_id
    assert diagnosis["conflict"]["intent_id"] == conflict["id"]
    assert diagnosis["stage"] == {
        "ordinal": 1, "intelligence_class": "high", "timeout_seconds": 3600,
        "attempt_limit": 3,
    }
    assert diagnosis["delegates"] == [
        {"task_id": old_delegate, "status": "FAILED", "archived": True}
    ]
    assert [row["id"] for row in diagnosis["receipts"]] == [receipt["id"]]
    # A dry run writes nothing.
    assert (await _operation(case))["state"] == "cancelled"
    assert await _owner(case) == released

    result = await recovery.run(
        "epic", dry_run=False, expected_head_sha=diagnosis["head_sha"],
        reason="cancel stopped the whole collection, not just the expired repair",
        operator_id="supervisor:p",
    )
    assert result["outcome"] == "reopened", result
    assert result["collector_fence_token"] == released["fence_token"] + 1

    operation = await _operation(case)
    assert (operation["state"], operation["active_stage"]) == ("escalated", 1)
    stages = {
        row["ordinal"]: row for row in await _rows(
            case.db, integration_repair_stages,
            integration_repair_stages.c.operation_id == case.operation_id,
        )
    }
    # The cancelled stage stays cancelled; the new one is bound to the conflict.
    assert stages[0]["state"] == "cancelled"
    assert stages[1]["state"] == "active"
    assert stages[1]["trigger_id"] == conflict["id"]
    assert stages[1]["starting_sha"] == conflict["expected_target"]
    assert stages[1]["dossier"]["current_conflict"]["intent_id"] == conflict["id"]
    assert stages[1]["dossier"]["reopened"]["operator_id"] == "supervisor:p"

    # Dispatch filed a new writer; the archived one was never restored.
    [handoff] = dispatched
    assert handoff["outcome"] == "dispatched"
    new_delegate = f"repair-{case.operation_id}-1"
    assert handoff["repair_task_id"] == new_delegate
    assert result["dispatch"]["repair_task_id"] == new_delegate
    assert (await case.db.get_task(new_delegate)).status == TaskStatus.READY
    assert await case.db.get_task(old_delegate) is None
    assert await _rows(case.db, archived_tasks, archived_tasks.c.id == old_delegate)
    owner = await _owner(case)
    assert (owner["owner_id"], owner["owner_role"], owner["handoff_state"]) == (
        new_delegate, "repair", "reserved",
    )
    assert owner["fence_token"] == released["fence_token"] + 2
    # Once attached, the new writer's resolution of exactly this conflict is in scope.
    scope = await case.db.get_repair_filing_scope(new_delegate)
    assert (scope["operation_id"], scope["stage"], scope["trigger_id"]) == (
        case.operation_id, 1, conflict["id"],
    )
    assert scope["writer_kind"] == "repair_delegate"
    assert PromotionService._repair_subject_matches_intent(scope, conflict)

    # Receipts and the episode are exactly as recorded.
    assert await _rows(case.db, task_delivery_receipts) == [receipt]
    [checkpoint] = await _rows(
        case.db, task_integration_checkpoints,
        task_integration_checkpoints.c.task_id == "epic",
    )
    assert checkpoint["episode_id"] == operation["episode_id"]
    assert checkpoint["state"] == "awaiting_children"
    assert (await case.db.get_task("epic")).status == TaskStatus.PAUSED
    [audit] = await _rows(case.db, events, events.c.event_type == REOPEN_EVENT)
    payload = json.loads(audit["payload"])
    assert payload["reason"].startswith("cancel stopped")
    assert payload["conflict_intent_id"] == conflict["id"]
    assert payload["previous_owner"]["fence_token"] == released["fence_token"]
    assert payload["receipts"] == [receipt["id"]]

    # The child no longer reports a missing collection; the repair owns it.
    redrive = await ChildDelivery(case.db, case.promotion).diagnose("epic.2")
    assert redrive["outcome"] == "nothing_to_redrive"
    assert conflict["id"] in redrive["reason"]

    again = await recovery.run("epic")
    assert again["outcome"] == "nothing_to_reopen"


async def test_reopen_without_a_prior_stage_opens_the_primary_stage(case):
    conflict = await _deliver_first_and_conflict_second(case)
    await _cancel(case)

    diagnosis = await _recovery(case).run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    assert diagnosis["stage"]["ordinal"] == 0
    assert diagnosis["stage"]["intelligence_class"] == "standard"
    result = await _recovery(case).run(
        "epic", dry_run=False, expected_head_sha=diagnosis["head_sha"], reason="reopen",
    )
    assert result["outcome"] == "reopened"
    assert result["dispatch"] is None
    operation = await _operation(case)
    assert (operation["state"], operation["active_stage"]) == ("active", 0)
    # The orchestrator's continuation sweep picks the writerless stage up.
    assert await RepairService(case.db).pending_dispatches() == [
        {"operation_id": case.operation_id, "ordinal": 0}
    ]
    dispatched = await RepairService(case.db).dispatch(case.operation_id, 0)
    assert dispatched["outcome"] == "dispatched"
    [stage] = await _rows(case.db, integration_repair_stages)
    assert stage["trigger_id"] == conflict["id"]


async def test_reopen_without_a_conflict_resumes_collection_under_a_new_fence(case):
    assert await _promote_next(case, 10.0) is not None
    async with case.db.immediate() as conn:
        # epic.2 has not finished yet when the operator cancels.
        await conn.execute(update(tasks).where(tasks.c.id == "epic.2").values(status="READY"))
    await _cancel(case)
    released = await _owner(case)

    diagnosis = await _recovery(case).run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    assert diagnosis["stage"] is None
    [receipt] = await _rows(case.db, task_delivery_receipts)
    assert diagnosis["head_sha"] == receipt["after_sha"]
    result = await _recovery(case).run(
        "epic", dry_run=False, expected_head_sha=diagnosis["head_sha"], reason="reopen",
    )
    assert result["outcome"] == "reopened"
    owner = await _owner(case)
    assert (owner["owner_id"], owner["owner_role"], owner["handoff_state"]) == (
        case.operation_id, "collector", "reserved",
    )
    assert owner["fence_token"] == released["fence_token"] + 1
    assert (await _operation(case))["state"] == "active"

    # The collector delivers the next child under the new fence, receipted to
    # the same operation and episode as the first.
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic.2").values(status="COMPLETED"))
    assert await _promote_next(case, 30.0) is None  # epic.2 still conflicts with epic.1
    [conflict] = await _rows(
        case.db, integration_promotion_intents,
        integration_promotion_intents.c.state == "conflict",
    )
    assert conflict["fence_token"] == owner["fence_token"]
    assert conflict["operation_key"] == case.operation_id


async def test_a_reopened_collection_repairs_its_next_conflict_past_a_cancelled_stage(case):
    """Review finding: reopening with no conflict left the cancelled stage active.

    ``RepairService.start`` only continued a live stage, so the reopened
    collection's next conflict answered ``stale`` and stranded the parent again.
    """
    assert await _promote_next(case, 10.0) is not None
    head = _remote_tip(case)
    [checkpoint] = await _rows(
        case.db, task_integration_checkpoints, task_integration_checkpoints.c.task_id == "epic"
    )
    # Stage 0 repairs a failed parent check, then the operator cancels.
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic.2").values(status="READY"))
        await conn.execute(insert(integration_check_evidence).values(
            id="ci-red", operation_id=case.operation_id, parent_task_id="epic",
            parent_generation=checkpoint["generation"], parent_head_sha=head,
            producer_id="forge", workflow_id="ci", run_id="1", attempt=1,
            required_check_version="test", checks=[{"name": "unit", "conclusion": "failure"}],
            conclusion="failure", classification="code", observed_at=12.0,
        ))
    started = await RepairService(case.db).start(case.operation_id, head, "ci-red")
    assert started["outcome"] == "started"
    await _cancel(case)

    diagnosis = await _recovery(case).run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    assert diagnosis["stage"] is None
    result = await _recovery(case).run(
        "epic", dry_run=False, expected_head_sha=head, reason="reopen",
    )
    assert result["outcome"] == "reopened"
    assert (await _operation(case))["active_stage"] == 0

    # The next child conflicts, and the shipped policy's start + literal
    # stage-zero dispatch reach a fresh stage and a new writer.
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic.2").values(status="COMPLETED"))
    assert await _promote_next(case, 30.0) is None
    [conflict] = await _rows(
        case.db, integration_promotion_intents,
        integration_promotion_intents.c.state == "conflict",
    )
    repair = RepairService(case.db)
    again = await repair.start(case.operation_id, conflict["expected_target"], case.operation_id)
    assert (again["outcome"], again["stage"]) == ("started", 1)
    replay = await repair.start(case.operation_id, conflict["expected_target"], case.operation_id)
    assert replay["outcome"] == "already_started"
    operation = await _operation(case)
    assert (operation["state"], operation["active_stage"]) == ("escalated", 1)
    dispatched = await repair.dispatch(case.operation_id, 0)
    assert dispatched["outcome"] == "dispatched", dispatched
    assert dispatched["repair_task_id"] == f"repair-{case.operation_id}-1"
    stages = {
        row["ordinal"]: row for row in await _rows(
            case.db, integration_repair_stages,
            integration_repair_stages.c.operation_id == case.operation_id,
        )
    }
    assert stages[0]["state"] == "cancelled"
    assert stages[1]["trigger_id"] == conflict["id"]
    assert stages[1]["dossier"]["previous_stage"]["state"] == "cancelled"


async def test_a_failed_reopen_dispatch_is_retried_whatever_the_policy(case):
    """Review finding: only ``continue`` policies were swept for a writerless stage."""
    await _deliver_first_and_conflict_second(case)
    await _cancel(case)
    async with case.db.immediate() as conn:
        operation = (await conn.execute(select(integration_repair_operations).where(
            integration_repair_operations.c.id == case.operation_id
        ))).mappings().one()
        policy = dict(operation["policy_snapshot"])
        policy["parent"] = dict(policy["parent"])
        policy["parent"]["repair"] = dict(policy["parent"]["repair"], on_exhausted="human")
        await conn.execute(update(integration_repair_operations).values(policy_snapshot=policy))

    async def broken(_operation_id, _stage):
        raise RuntimeError("daemon restarted mid-dispatch")

    head = _remote_tip(case)
    result = await _recovery(case, broken).run(
        "epic", dry_run=False, expected_head_sha=head, reason="reopen",
    )
    assert result["outcome"] == "reopened"
    assert result["dispatch"]["outcome"] == "runtime_error"
    assert await RepairService(case.db).pending_dispatches() == [
        {"operation_id": case.operation_id, "ordinal": 0}
    ]


async def test_reopen_reclaims_a_kept_collector_reservation_at_the_next_fence(case):
    await _deliver_first_and_conflict_second(case)
    await _cancel(case)
    async with case.db.immediate() as conn:
        # A legacy cancellation that kept the detached collector reservation.
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.ref == "aq/epic")
            .values(handoff_state="reserved", owner_id=case.operation_id, owner_role="collector")
        )
    kept = await _owner(case)
    diagnosis = await _recovery(case).run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    result = await _recovery(case).run(
        "epic", dry_run=False, expected_head_sha=diagnosis["head_sha"], reason="reopen",
    )
    assert result["collector_fence_token"] == kept["fence_token"] + 1
    assert (await _owner(case))["fence_token"] == kept["fence_token"] + 1


async def test_reopen_restores_a_parent_the_exhausted_repair_blocked(case):
    conflict = await _deliver_first_and_conflict_second(case)
    await _expired_repair_then_cancel(case, conflict)
    await case.db.transition_task(
        "epic", TaskStatus.BLOCKED, context="integration_repair_exhausted", force=True,
        _manual_pause_control=True,
    )
    await case.db.set_task_meta("epic", "blocked_terminal", "integration_repair_exhausted")

    diagnosis = await _recovery(case).run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    result = await _recovery(case).run(
        "epic", dry_run=False, expected_head_sha=diagnosis["head_sha"], reason="reopen",
    )
    assert result["outcome"] == "reopened"
    assert (await case.db.get_task("epic")).status == TaskStatus.PAUSED


# ---------------------------------------------------------------------------
# Refusals: nothing ambiguous, live, held or moved is ever overridden
# ---------------------------------------------------------------------------


async def test_apply_needs_the_reported_head_and_a_reason(case):
    await _deliver_first_and_conflict_second(case)
    await _cancel(case)
    diagnosis = await _recovery(case).run("epic")

    wrong = await _recovery(case).run(
        "epic", dry_run=False, expected_head_sha="f" * 40, reason="wrong head",
    )
    assert wrong["outcome"] == "changed"
    unexplained = await _recovery(case).run(
        "epic", dry_run=False, expected_head_sha=diagnosis["head_sha"], reason=" ",
    )
    assert unexplained["outcome"] == "changed"
    assert (await _operation(case))["state"] == "cancelled"


async def test_apply_rechecks_state_that_changed_after_the_dry_run(case, monkeypatch):
    await _deliver_first_and_conflict_second(case)
    await _cancel(case)
    recovery = _recovery(case)
    diagnose = recovery._diagnose

    async def hold_after_diagnosis(task_id):
        result = await diagnose(task_id)
        await case.db.set_task_meta(task_id, "manual_pause", {"status": "PAUSED"})
        return result

    monkeypatch.setattr(recovery, "_diagnose", hold_after_diagnosis)
    head = _remote_tip(case)
    result = await recovery.run("epic", dry_run=False, expected_head_sha=head, reason="race")
    assert result["outcome"] == "changed"
    assert (await _operation(case))["state"] == "cancelled"
    assert (await _owner(case))["handoff_state"] == "released"
    assert await _rows(case.db, events, events.c.event_type == REOPEN_EVENT) == []


async def test_refuses_when_the_parent_branch_moved(case):
    await _deliver_first_and_conflict_second(case)
    await _cancel(case)
    _git(["fetch", "origin", "aq/epic"], case.work)
    _git(["switch", "-c", "stray", _remote_tip(case)], case.work)
    (case.work / "stray.txt").write_text("unrecorded\n")
    _git(["add", "stray.txt"], case.work)
    _git(["commit", "-m", "unrecorded push"], case.work)
    _git(["push", "origin", "HEAD:refs/heads/aq/epic"], case.work)

    result = await _recovery(case).run("epic")
    assert result["outcome"] == "blocked"
    assert result["remote_head_sha"] == _remote_tip(case)
    assert "not the recorded collection head" in result["reason"]


async def test_refuses_an_ambiguous_promotion(case):
    conflict = await _deliver_first_and_conflict_second(case)
    await _cancel(case)
    async with case.db.immediate() as conn:
        await conn.execute(
            update(integration_promotion_intents)
            .where(integration_promotion_intents.c.id == conflict["id"])
            .values(state="pushed")
        )
    result = await _recovery(case).run("epic")
    assert result["outcome"] == "ambiguous"
    assert [blocker["ref"] for blocker in result["blockers"]] == ["promotion"]


@pytest.mark.parametrize(
    ("blocker", "because"),
    [
        ("manual_pause", "operator hold"),
        ("attached_owner", "holds the branch (attached"),
        ("unsettled_delegate", "is PAUSED; settle it"),
        ("two_conflicts", "more than one conflict"),
        ("moved_child", "no longer names epic.2's completed head"),
        ("verifier", "re-arm a settled verifier"),
        ("draining", "draining"),
        ("assigned_parent", "assigned to an agent"),
    ],
)
async def test_refuses_what_it_cannot_prove_quiet(case, blocker, because):
    conflict = await _deliver_first_and_conflict_second(case)
    delegate = await _expired_repair_then_cancel(case, conflict)
    async with case.db.immediate() as conn:
        if blocker == "attached_owner":
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.ref == "aq/epic")
                .values(handoff_state="attached", session_id="s", workspace_id="w")
            )
        elif blocker == "unsettled_delegate":
            [archived] = (await conn.execute(select(archived_tasks).where(
                archived_tasks.c.id == delegate
            ))).mappings().all()
            await conn.execute(insert(tasks).values(
                id=delegate, project_id="p", title=archived["title"], description="",
                status="PAUSED", repo_id="repo", branch_name="aq/epic",
                created_by_kind="integration_repair", created_by_id=case.operation_id,
                created_at=1.0, updated_at=1.0,
            ))
        elif blocker == "two_conflicts":
            await conn.execute(insert(integration_promotion_intents).values(
                **{
                    key: value for key, value in conflict.items()
                    if value is not None and key not in {"id", "domain_key", "receipt_id"}
                },
                id="intent-other", domain_key="other-domain", receipt_id="receipt-other",
            ))
        elif blocker == "moved_child":
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == "epic.2")
                .values(checkpoint_sha="e" * 40)
            )
        elif blocker == "verifier":
            await conn.execute(
                update(integration_repair_operations).values(verifier_task_id="epic")
            )
        elif blocker == "draining":
            await conn.execute(
                update(projects).values(hierarchical_integration_draining=True)
            )
        elif blocker == "assigned_parent":
            await conn.execute(insert(agents).values(
                id="agent-1", name="a", profile_id="w", created_at=1.0,
            ))
            await conn.execute(
                update(tasks).where(tasks.c.id == "epic").values(assigned_agent_id="agent-1")
            )
    if blocker == "manual_pause":
        await case.db.set_task_meta("epic", "manual_pause", {"status": "PAUSED"})

    result = await _recovery(case).run(
        "epic", dry_run=False, expected_head_sha=conflict["expected_target"],
        reason="must refuse",
    )
    assert result["outcome"] == "blocked", result
    assert because in result["reason"]
    assert (await _operation(case))["state"] == "cancelled"
    assert (await _owner(case))["handoff_state"] != "reserved"


@pytest.mark.parametrize(
    ("state", "outcome"),
    [("active", "nothing_to_reopen"), ("human_required", "not_eligible"),
     ("completed", "not_eligible")],
)
async def test_only_a_cancelled_operation_is_reopened(case, state, outcome):
    async with case.db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(state=state))
    result = await _recovery(case).run("epic")
    assert result["outcome"] == outcome


async def test_unknown_and_non_collecting_tasks(case):
    assert (await _recovery(case).run("missing"))["outcome"] == "not_found"
    leaf = await _recovery(case).run("epic.3")
    assert leaf["outcome"] == "not_eligible"
