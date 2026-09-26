"""A completed child of a collecting parent is assembled, or says why not.

Regression for vivid-ridge: after automatic per-task reviews were retired
(cf0002b9c), nothing wrote the approved review evidence the collector requires
of a completed child, so train epics stalled with every sibling that needed a
finished child held out of the claim frontier (sharp-impact).  The collector now
proves the child's published head from Git and records completion evidence;
``aq integration redrive-child`` is the supervisor's dry-run-first handle, and
``integration.stuck_children`` reports what is still waiting.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database.queries.hierarchy_queries import (
    delivered_same_parent_prerequisites_when_hierarchical,
)
from src.database.tables import (
    events,
    integration_outbox,
    integration_review_evidence,
    playbook_artifacts,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_dependencies,
    task_integration_checkpoints,
    tasks,
)
from src.doctor.integration_checks import run_check
from src.doctor.task_checks import run_check as run_task_check
from src.git.manager import GitManager
from src.integration.child_delivery import (
    COMPLETION_IDENTITY_PREFIX,
    REDRIVE_EVENT,
    ChildDelivery,
)
from src.integration.collection import CollectionService
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
from src.integration.promotion import PromotionService
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, Task, TaskStatus

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


def _policy_and_artifact() -> tuple[dict, ArtifactSnapshot]:
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
        repair=RepairPolicy(debug_intelligence_class="high"),
        route=PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            artifact=artifact,
        ),
    )
    policy = HierarchicalIntegrationPolicy(
        parent=boundary, root=boundary, branchless_parent="verifier", on_failed_child="block"
    )
    return policy.model_dump(mode="json"), artifact


@pytest.fixture
async def case(tmp_path, reuse_database):
    """Train epic ``epic`` collecting its COMPLETED child ``epic.1``.

    The child's branch is published at ``head``, one commit past the origin
    base both branches were cut from; no reviewer ever looked at it.  Its
    sibling ``epic.2`` needs it, exactly as sharp-impact.2 needed .1.
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
    _git(["switch", "-c", "aq/epic.1"], work)
    (work / "child.txt").write_text("child work\n")
    _git(["add", "child.txt"], work)
    _git(["commit", "-m", "child work"], work)
    head = _git(["rev-parse", "HEAD"], work)
    tree = _git(["rev-parse", "HEAD^{tree}"], work)
    _git(["push", "origin", "aq/epic.1"], work)

    db = await reuse_database("child-delivery.db")
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
    policy, artifact = _policy_and_artifact()
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
        default_head_resolver=lambda _repo, _branch: base,
        checkpoint_verifier=lambda _task, _repo, head_sha: head_sha,
    )
    await db.create_task(
        Task(
            id="epic", project_id="p", repo_id="repo", title="epic", description="epic",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await hierarchy.file_children("epic", [{"title": "child"}, {"title": "sibling"}], 0)
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_dependencies).values(
                task_id="epic.2", depends_on_task_id="epic.1", dep_type="blocks"
            )
        )
        await conn.execute(
            update(task_branch_origins).values(materialized=True, materialized_at=2.0)
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "epic.1").values(status="COMPLETED", updated_at=3.0)
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "epic.1")
            .values(checkpoint_sha=head, updated_at=3.0)
        )
    bootstrapped = await hierarchy.bootstrap_container_collection("epic")
    assert bootstrapped["outcome"] == "checkpointed"
    promotion = PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager())
    yield SimpleNamespace(
        db=db, hierarchy=hierarchy, promotion=promotion, base=base, head=head, tree=tree,
        work=work,
    )


def _collector(case, delivery=None):
    return CollectionService(
        case.db,
        hierarchy_service_factory=lambda: case.hierarchy,
        child_delivery=delivery or ChildDelivery(case.db, case.promotion),
    )


async def _evidence(db, task_id="epic.1") -> list[dict]:
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(integration_review_evidence).where(
                    integration_review_evidence.c.source_task_id == task_id
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def _delivery_ready(db) -> list[dict]:
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "delivery.ready"
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def _open_reviewer(db) -> None:
    await db.create_profile(AgentProfile(id="reviewer", name="Reviewer", harness="claude"))
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="review-1", project_id="p", title="Review epic.1", description="",
                status="READY", profile_id="reviewer", created_at=4.0, updated_at=4.0,
            )
        )
        await conn.execute(
            insert(task_dependencies).values(
                task_id="review-1", depends_on_task_id="epic.1", dep_type="discovered-from"
            )
        )


async def _reject_head(case) -> None:
    await case.db.append_integration_review_evidence(
        {
            "id": "rejection", "source_task_id": "epic.1", "repository_id": "repo",
            "source_base": case.base, "reviewed_head_sha": case.head,
            "reviewed_tree_sha": case.tree, "reviewer_task_id": "review-1",
            "reviewer_session_attempt_id": None, "review_kind": "leaf", "generation": 0,
            "verdict": "rejected", "evidence": {}, "created_at": 4.0,
        }
    )


async def _sibling_admitted(db) -> bool:
    async with db._engine.connect() as conn:
        return (
            await conn.execute(
                select(tasks.c.id).where(
                    tasks.c.id == "epic.2",
                    delivered_same_parent_prerequisites_when_hierarchical(),
                )
            )
        ).scalar_one_or_none() == "epic.2"


# ---------------------------------------------------------------------------
# Collector: the durable path
# ---------------------------------------------------------------------------


async def test_unreviewed_completed_child_is_receipted_after_one_collector_tick(case):
    """vivid-ridge: the child is assembled and its sibling enters the frontier."""
    await _receipt_completed_child(case)


async def _receipt_completed_child(case):
    assert await _evidence(case.db) == []
    assert not await _sibling_admitted(case.db)

    await _collector(case).tick(10.0)

    [ready] = await _delivery_ready(case.db)
    payload = ready["payload"]
    request = PromotionInput(
        operation_key=payload["operation_key"],
        source_task_id=payload["source_task_id"],
        source_head=payload["source_head"],
        source_base=payload["source_base"],
        expected_target=payload["expected_target"],
        fence=Fence.model_validate(payload["fence"]),
    )
    prepared = await case.promotion.prepare(request)
    await case.promotion.push(prepared.intent_id, request.fence)
    await case.promotion.reconcile(prepared.intent_id)

    async with case.db._engine.connect() as conn:
        [receipt] = (
            await conn.execute(select(task_delivery_receipts))
        ).mappings().all()
    assert (receipt["source_task_id"], receipt["target_task_id"]) == ("epic.1", "epic")
    assert receipt["target_branch"] == "aq/epic"
    assert receipt["reviewed_head_sha"] == case.head
    assert await _sibling_admitted(case.db)


async def test_collector_proves_an_unreviewed_child_and_queues_its_delivery(case):
    await _collector(case).tick(10.0)

    [evidence] = await _evidence(case.db)
    assert evidence["verdict"] == "approved"
    assert evidence["review_kind"] == "leaf"
    assert evidence["reviewer_task_id"] is None
    assert evidence["reviewer_identity"] == f"{COMPLETION_IDENTITY_PREFIX}epic.1"
    assert (evidence["source_base"], evidence["reviewed_head_sha"]) == (case.base, case.head)
    # The exact tree promotion recomputes before it pushes.
    assert evidence["reviewed_tree_sha"] == case.tree
    assert evidence["evidence"]["decision_path"] == "completion_proof"
    [event] = await _delivery_ready(case.db)
    assert event["payload"]["source_task_id"] == "epic.1"
    assert event["payload"]["source_head"] == case.head
    assert event["payload"]["source_base"] == case.base

    # Idempotent: the next pass finds the evidence and records nothing new.
    await _collector(case).tick(11.0)
    assert len(await _evidence(case.db)) == 1


async def test_collector_without_child_delivery_still_waits_for_a_reviewer(case):
    collector = CollectionService(case.db, hierarchy_service_factory=lambda: case.hierarchy)

    await collector.tick(10.0)

    assert await _evidence(case.db) == []
    assert await _delivery_ready(case.db) == []


async def test_collector_never_overrides_a_reviewer_rejection(case):
    await _reject_head(case)

    await _collector(case).tick(10.0)

    assert [row["verdict"] for row in await _evidence(case.db)] == ["rejected"]
    assert await _delivery_ready(case.db) == []


async def test_collector_waits_for_an_open_reviewer(case):
    await _open_reviewer(case.db)

    await _collector(case).tick(10.0)

    assert await _evidence(case.db) == []
    assert await _delivery_ready(case.db) == []


async def test_collector_backs_off_a_child_whose_branch_moved(case):
    (case.work / "child.txt").write_text("unreviewed follow-up\n")
    _git(["commit", "-am", "pushed after close"], case.work)
    _git(["push", "origin", "aq/epic.1"], case.work)
    fetches = []
    original = case.promotion._fetch_all_heads

    async def counting_fetch(*args, **kwargs):
        fetches.append(args)
        return await original(*args, **kwargs)

    case.promotion._fetch_all_heads = counting_fetch
    delivery = ChildDelivery(case.db, case.promotion, retry_seconds=60.0)
    collector = _collector(case, delivery)

    await collector.tick(100.0)
    await collector.tick(130.0)  # inside the 60s backoff: no second fetch
    assert len(fetches) == 1
    await collector.tick(161.0)
    assert len(fetches) == 2

    assert await _evidence(case.db) == []
    assert await _delivery_ready(case.db) == []


# ---------------------------------------------------------------------------
# Redrive: the supervisor's control
# ---------------------------------------------------------------------------


async def test_redrive_dry_run_reports_the_proven_head_and_writes_nothing(case):
    result = await ChildDelivery(case.db, case.promotion).run("epic.1")

    assert result["outcome"] == "would_advance"
    assert result["parent_task_id"] == "epic"
    assert result["branch"] == "aq/epic.1"
    assert result["parent_branch"] == "aq/epic"
    assert result["head_sha"] == result["remote_head_sha"] == case.head
    assert result["base_sha"] == case.base
    assert result["tree_sha"] == case.tree
    assert result["checkpoint"]["state"] == "working"
    assert await _evidence(case.db) == []


async def test_redrive_apply_records_evidence_and_queues_the_parent(case):
    collector = _collector(case)
    delivery = ChildDelivery(
        case.db,
        case.promotion,
        collect=lambda parent_id: collector.collect_parent(parent_id, 20.0),
        clock=lambda: 20.0,
    )

    result = await delivery.run(
        "epic.1", dry_run=False, expected_head_sha=case.head,
        reason="sharp-impact stalled", operator_id="supervisor session:s1",
    )

    assert result["outcome"] == "advanced"
    assert result["collection"] == "queued"
    [evidence] = await _evidence(case.db)
    assert evidence["id"] == result["evidence_id"]
    assert evidence["reviewer_identity"] == "supervisor session:s1"
    assert evidence["evidence"]["decision_path"] == "operator_redrive"
    assert evidence["evidence"]["reason"] == "sharp-impact stalled"
    [ready] = await _delivery_ready(case.db)
    assert ready["payload"]["source_head"] == case.head
    async with case.db._engine.connect() as conn:
        row = (
            await conn.execute(select(events).where(events.c.event_type == REDRIVE_EVENT))
        ).mappings().one()
    payload = json.loads(row["payload"])
    assert row["task_id"] == "epic.1"
    assert payload["operator_id"] == "supervisor session:s1"
    assert payload["head_sha"] == case.head
    assert payload["evidence_created"] is True

    # Re-applying reuses the evidence rather than writing a second verdict.
    again = await delivery.run(
        "epic.1", dry_run=False, expected_head_sha=case.head, reason="again",
        operator_id="supervisor session:s1",
    )
    assert again["outcome"] == "advanced"
    assert again["evidence_id"] == evidence["id"]
    assert len(await _evidence(case.db)) == 1


async def test_redrive_apply_refuses_a_head_the_dry_run_did_not_report(case):
    result = await ChildDelivery(case.db, case.promotion).run(
        "epic.1", dry_run=False, expected_head_sha="d" * 40, reason="stuck"
    )

    assert result["outcome"] == "changed"
    assert await _evidence(case.db) == []


async def test_redrive_blocks_a_moved_branch_a_rejection_and_an_open_reviewer(case):
    delivery = ChildDelivery(case.db, case.promotion)
    await _open_reviewer(case.db)
    reviewer = await delivery.run("epic.1")
    assert reviewer["outcome"] == "blocked"
    assert "review-1" in reviewer["reason"]

    await _reject_head(case)
    rejected = await delivery.run("epic.1")
    assert rejected["outcome"] == "blocked"
    assert "rejected" in rejected["reason"]


async def test_redrive_blocks_a_branch_that_moved_after_the_close(case):
    (case.work / "child.txt").write_text("moved\n")
    _git(["commit", "-am", "moved"], case.work)
    moved = _git(["rev-parse", "HEAD"], case.work)
    _git(["push", "origin", "aq/epic.1"], case.work)

    result = await ChildDelivery(case.db, case.promotion).run("epic.1")

    assert result["outcome"] == "blocked"
    assert result["remote_head_sha"] == moved
    assert "moved" in result["reason"]


async def test_redrive_reports_a_delivered_child_and_a_no_code_child(case):
    delivery = ChildDelivery(case.db, case.promotion)
    async with case.db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="receipt", domain_key="receipt", source_task_id="epic.1",
                target_task_id="epic", repository_id="repo", target_branch="aq/epic",
                reviewed_head_sha=case.head, disposition="code", created_at=5.0,
            )
        )
    delivered = await delivery.run("epic.1")
    assert delivered["outcome"] == "nothing_to_redrive"
    assert "receipt" in delivered["reason"]

    async with case.db.immediate() as conn:
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "epic.1")
            .values(checkpoint_sha=case.base)
        )
    no_code = await delivery.run("epic.1")
    assert no_code["outcome"] == "blocked"
    assert "record-noop" in no_code["reason"]


async def test_redrive_reissues_a_receipt_for_a_newer_same_head_completion(case):
    await _collector(case).tick(10.0)
    [ready] = await _delivery_ready(case.db)
    payload = ready["payload"]
    request = PromotionInput(
        operation_key=payload["operation_key"],
        source_task_id=payload["source_task_id"],
        source_head=payload["source_head"],
        source_base=payload["source_base"],
        expected_target=payload["expected_target"],
        fence=Fence.model_validate(payload["fence"]),
    )
    prepared = await case.promotion.prepare(request)
    await case.promotion.push(prepared.intent_id, request.fence)
    await case.promotion.reconcile(prepared.intent_id)
    await case.db.update_task("epic.1", status=TaskStatus.READY)
    await case.db.update_task("epic.1", status=TaskStatus.COMPLETED)
    async with case.db.immediate() as conn:
        [old] = (await conn.execute(select(task_delivery_receipts))).mappings().all()
        await conn.execute(insert(task_completion_records).values(
            id="new-completion", task_id="epic.1", outcome="pass",
            completed_at=old["created_at"] + 1,
        ))
    assert not await _sibling_admitted(case.db)
    delivery = ChildDelivery(case.db, case.promotion)
    diagnosis = await delivery.run("epic.1")
    assert diagnosis["outcome"] == "would_advance"
    assert diagnosis["redrive_kind"] == "receipt_reissue"
    applied = await delivery.run("epic.1", dry_run=False, expected_head_sha=case.head)
    assert applied["outcome"] == "advanced"
    assert applied["receipt_id"] != old["id"]
    assert await _sibling_admitted(case.db)
    readiness = await case.hierarchy.readiness("epic")
    assert not any(row["task_id"] == "epic.1" for row in readiness["blockers"])
    assert (await delivery.run("epic.1"))["outcome"] == "nothing_to_redrive"


async def test_doctor_explains_ready_sibling_excluded_from_claim_frontier(case):
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic.2").values(
            status="READY", is_blocked=False,
        ))
    result = await run_task_check(case.db, "tasks.ready_frontier_exclusions")
    assert result.severity.value == "warn"
    sibling = next(row for row in result.data["tasks"] if row["task_id"] == "epic.2")
    assert "sibling_prerequisite_not_delivered" in sibling["reasons"]


async def test_redrive_classifies_tasks_it_cannot_advance(case):
    delivery = ChildDelivery(case.db, case.promotion)

    assert (await delivery.run("missing"))["outcome"] == "not_found"
    root = await delivery.run("epic")
    assert root["outcome"] == "not_eligible"
    assert "redrive-root" in root["reason"]

    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic").values(status="READY"))
    parent_moved_on = await delivery.run("epic.1")
    assert parent_moved_on["outcome"] == "blocked"
    assert "not PAUSED" in parent_moved_on["reason"]

    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic.1").values(status="READY"))
    reopened = await delivery.run("epic.1")
    assert reopened["outcome"] == "not_eligible"


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


async def test_doctor_flags_a_child_its_parent_never_assembled(case):
    result = await run_check(case.db, "integration.stuck_children")

    assert result.severity.value == "warn"
    [child] = result.data["children"]
    assert child["task_id"] == "epic.1"
    assert child["parent_task_id"] == "epic"
    assert child["head_sha"] == case.head
    assert child["evidence"] == "none"
    assert "redrive-child" in result.detail


async def test_doctor_ignores_fresh_and_delivered_children(case):
    import time

    async with case.db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "epic.1").values(updated_at=time.time())
        )
    fresh = await run_check(case.db, "integration.stuck_children")
    assert fresh.severity.value == "ok"

    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic.1").values(updated_at=3.0))
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="receipt", domain_key="receipt", source_task_id="epic.1",
                target_task_id="epic", repository_id="repo", target_branch="aq/epic",
                reviewed_head_sha=case.head, disposition="code", created_at=5.0,
            )
        )
    delivered = await run_check(case.db, "integration.stuck_children")
    assert delivered.severity.value == "ok"


async def test_removing_manual_hold_preserves_collecting_parent(case):
    """Resume removes the hold; it cannot start an unverified parent worker."""
    before = await case.db.get_integration_checkpoint("epic")
    snapshot = await case.db.pause_task("epic")
    await case.db.finish_task_pause("epic", snapshot)

    resumed = await case.db.resume_task("epic")

    assert resumed.status == TaskStatus.PAUSED
    assert resumed.resume_after is None
    assert await case.db.get_task_meta("epic", "manual_pause") is None
    assert await case.db.get_integration_checkpoint("epic") == before
    assert await case.db.recover_orphaned_pause("epic") is None
    await _collector(case).tick(10.0)
    assert len(await _delivery_ready(case.db)) == 1


@pytest.mark.parametrize("status", [TaskStatus.READY, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS])
async def test_collecting_parent_requires_verifier_for_every_worker_wake(case, status):
    from src.database.queries.hierarchy_queries import HierarchyError

    with pytest.raises(HierarchyError, match="guarded verifier wake"):
        await case.db.transition_task(
            "epic", status, context="manual_resume", force=True, _manual_pause_control=True,
        )
    assert (await case.db.get_task("epic")).status == TaskStatus.PAUSED


async def _block_collector(case):
    # Reproduce the old resume path, then the stopped-session orphan verdict.
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic").values(status="IN_PROGRESS"))
    await case.db.transition_task("epic", TaskStatus.BLOCKED, context="session_not_live")
    await case.db.set_task_meta("epic", "needs_attention", "session_not_live")


async def test_redrive_root_restores_blocked_collection_and_receipts_the_child(case):
    from src.integration.root_pull_requests import RootDeliveryRedrive

    await _block_collector(case)
    redrive = RootDeliveryRedrive(case.db, case.promotion.git)
    diagnosis = await redrive.run("epic")
    assert diagnosis["outcome"] == "would_collect"
    assert diagnosis["head_sha"] == case.base
    assert (await case.db.get_task("epic")).status == TaskStatus.BLOCKED
    assert await _delivery_ready(case.db) == []

    restored = await redrive.run(
        "epic", dry_run=False, expected_head_sha=diagnosis["head_sha"],
        reason="restore displaced collector", operator_id="supervisor:p",
    )
    assert restored["outcome"] == "collecting"
    assert (await case.db.get_task("epic")).status == TaskStatus.PAUSED
    assert await case.db.get_task_meta("epic", "needs_attention") is None
    async with case.db._engine.connect() as conn:
        audit = (await conn.execute(select(events).where(
            events.c.event_type == "integration.collection_redriven",
        ))).mappings().one()
    assert json.loads(audit["payload"])["reason"] == "restore displaced collector"
    # Exercise the real Git promotion and prove that the dependent re-enters
    # the claim frontier, not merely that a status column changed.
    await _receipt_completed_child(case)


async def test_redrive_collection_refuses_a_different_head(case):
    from src.integration.collecting_parent_recovery import CollectingParentRecovery

    await _block_collector(case)
    result = await CollectingParentRecovery(case.db).run(
        "epic", dry_run=False, expected_head_sha="f" * 40, reason="wrong head",
    )
    assert result["outcome"] == "changed"
    assert (await case.db.get_task("epic")).status == TaskStatus.BLOCKED


@pytest.mark.parametrize("blocker", ["manual_pause", "terminal", "human_required", "owner", "episode"])
async def test_redrive_collection_does_not_override_real_blockers(case, blocker):
    from src.database.tables import integration_branch_owners, integration_repair_operations
    from src.integration.collecting_parent_recovery import CollectingParentRecovery

    await _block_collector(case)
    if blocker == "manual_pause":
        await case.db.set_task_meta("epic", "manual_pause", {"status": "BLOCKED"})
    elif blocker == "terminal":
        await case.db.set_task_meta("epic", "blocked_terminal", "integration_repair_exhausted")
    else:
        async with case.db.immediate() as conn:
            if blocker == "human_required":
                await conn.execute(update(integration_repair_operations).values(state="human_required"))
            elif blocker == "owner":
                await conn.execute(update(integration_branch_owners).values(owner_role="repair"))
            else:
                await conn.execute(update(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == "epic",
                ).values(episode_id=None))
    result = await CollectingParentRecovery(case.db).run(
        "epic", dry_run=False, expected_head_sha=case.base, reason="must refuse",
    )
    assert result["outcome"] == "blocked"
    assert (await case.db.get_task("epic")).status == TaskStatus.BLOCKED


async def test_doctor_reports_collectors_hidden_by_blocked_status(case):
    await _block_collector(case)
    result = await run_check(case.db, "integration.blocked_collectors")
    assert result.severity.value == "warn"
    [parent] = result.data["parents"]
    assert parent["task_id"] == "epic"
    assert parent["operation_state"] == "active"
    assert parent["episode_id"] is not None

    # Even a missing episode must be visible; the child alarm only scans
    # parents with live operations and would otherwise report a clean bill.
    async with case.db.immediate() as conn:
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "epic",
        ).values(episode_id=None))
    missing = await run_check(case.db, "integration.blocked_collectors")
    assert missing.data["parents"][0]["operation_id"] is None
    assert missing.severity.value == "warn"


async def test_resume_collecting_parent_survives_stopped_session_orphan_sweep(case):
    from src.config import AppConfig
    from src.models import SessionRecord
    from src.sessions.reconciler import SessionReconciler

    await case.db.create_session(SessionRecord(
        id="old-parent-writer", project_id="p", profile_id="worker", harness="codex",
        provider="fake", name="old-parent-writer", lifecycle="task", work_dir=str(case.work),
        epoch="old-daemon", instance_token="old-writer", started_at=1.0, task_id="epic",
        state="stopped", desired_state="stopped", ended_at=2.0,
    ))
    snapshot = await case.db.pause_task("epic")
    await case.db.finish_task_pause("epic", snapshot)
    await case.db.resume_task("epic")
    # This is the restart path that used to turn IN_PROGRESS into BLOCKED.
    await SessionReconciler(case.db, AppConfig(), providers=None)._step_orphans([], 10.0)
    assert (await case.db.get_task("epic")).status == TaskStatus.PAUSED
    assert await case.db.get_task_meta("epic", "needs_attention") is None
    await _collector(case).tick(10.0)
    assert len(await _delivery_ready(case.db)) == 1


async def test_removing_hold_does_not_restore_a_blocked_parent_to_collection(case):
    await _block_collector(case)
    snapshot = await case.db.pause_task("epic")
    await case.db.finish_task_pause("epic", snapshot)
    resumed = await case.db.resume_task("epic")
    assert resumed.status == TaskStatus.BLOCKED


async def test_collection_redrive_rechecks_a_later_hold_before_mutating(case, monkeypatch):
    from src.integration.collecting_parent_recovery import CollectingParentRecovery

    await _block_collector(case)
    recovery = CollectingParentRecovery(case.db)
    diagnose = recovery.diagnose

    async def hold_after_diagnosis(task_id):
        result = await diagnose(task_id)
        await case.db.set_task_meta(task_id, "manual_pause", {"status": "BLOCKED"})
        return result

    monkeypatch.setattr(recovery, "diagnose", hold_after_diagnosis)
    result = await recovery.run(
        "epic", dry_run=False, expected_head_sha=case.base, reason="race a hold",
    )
    assert result["outcome"] == "changed"
    assert (await case.db.get_task("epic")).status == TaskStatus.BLOCKED
    assert await case.db.get_task_meta("epic", "manual_pause") == {"status": "BLOCKED"}


async def test_collection_redrive_refuses_a_live_parent_session(case):
    from src.integration.collecting_parent_recovery import CollectingParentRecovery
    from src.models import SessionRecord

    await _block_collector(case)
    await case.db.create_session(SessionRecord(
        id="live-parent", project_id="p", profile_id="worker", harness="codex",
        provider="fake", name="live-parent", lifecycle="pool", work_dir=str(case.work),
        epoch="daemon", instance_token="live-writer", started_at=1.0, task_id="epic",
        state="running",
    ))
    result = await CollectingParentRecovery(case.db).run(
        "epic", dry_run=False, expected_head_sha=case.base, reason="must refuse live writer",
    )
    assert result["outcome"] == "blocked"
    assert "session or workspace" in result["reason"]
    assert (await case.db.get_task("epic")).status == TaskStatus.BLOCKED
