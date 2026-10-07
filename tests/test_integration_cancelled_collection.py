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
historical cancellation fixtures and current recovery paths against a real Git origin.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.database.tables import (
    agents,
    archived_tasks,
    events,
    gates,
    integration_branch_owners,
    integration_check_evidence,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    playbook_artifacts,
    projects,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_gates,
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
from src.integration.failed_verification_recovery import RECOVERY_EVENT, FailedVerificationRecovery
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
from src.integration.ownership import BranchOwnership, StaleFence
from src.integration.promotion_contracts import PromotionConflict
from src.integration.promotion import PromotionService
from src.integration.repair import RepairService
from src.integration.verifier_subject import latest_red_parent_evidence
from src.models import (
    Agent, AgentState, Project, RepoConfig, RepoSourceType, SessionRecord, Task, TaskStatus, Workspace,
)

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
        verifier_intelligence_class="high",
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


async def _failed_aggregate(case):
    """Two receipted children, a settled red verifier and one completed new fix."""
    head_two = _commit_on(case.work, "aq/two-clean", case.base, "two.txt", "two\n")
    child_two = await case.db.get_task("epic.2")
    _git(["push", "origin", f"{head_two}:refs/heads/{child_two.branch_name}", "--force"], case.work)
    async with case.db.immediate() as conn:
        await conn.execute(update(task_integration_checkpoints)
                           .where(task_integration_checkpoints.c.task_id == "epic.2")
                           .values(checkpoint_sha=head_two))
    assert await _promote_next(case, 10.0) is not None
    assert await _promote_next(case, 11.0) is not None
    red_head = _remote_tip(case)
    verifier_id = f"verify-{case.operation_id}"
    await case.db.create_task(Task(
        id=verifier_id, project_id="p", repo_id="repo", branch_name="aq/epic",
        title="failed aggregate verifier", description="found a real scope-contract failure",
        status=TaskStatus.FAILED,
    ))
    async with case.db.immediate() as conn:
        await conn.execute(update(task_integration_checkpoints)
                           .where(task_integration_checkpoints.c.task_id == "epic")
                           .values(state="verifying", checkpoint_sha=red_head))
        await conn.execute(update(integration_repair_operations)
                           .where(integration_repair_operations.c.id == case.operation_id)
                           .values(state="escalated", verifier_task_id=verifier_id))
        await conn.execute(update(integration_branch_owners)
                           .where(integration_branch_owners.c.ref == "aq/epic")
                           .values(owner_id=verifier_id, owner_role="verifier"))
        await conn.execute(insert(task_completion_records).values(
            id="failed-completion", task_id=verifier_id, outcome="fail", branch="aq/epic",
            commits=json.dumps([red_head]), summary="two scope failures", completed_at=2e10,
        ))
    # The fix becomes complete only after aggregate verification failed.
    _git(["fetch", "origin", "aq/epic"], case.work)
    fix = _commit_on(case.work, "aq/fix", red_head, "fix.txt", "scope fix\n")
    child_three = await case.db.get_task("epic.3")
    _git(["push", "origin", f"{fix}:refs/heads/{child_three.branch_name}"], case.work)
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic.3")
                           .values(status="COMPLETED"))
        await conn.execute(update(task_integration_checkpoints)
                           .where(task_integration_checkpoints.c.task_id == "epic.3")
                           .values(checkpoint_sha=fix))
    return red_head, verifier_id


async def _held_red_aggregate(case, orchestrator, *, evidence_changes=None):
    """Keep the verifier's actual pool claim/checkout while trusted exact CI is red."""
    red_head, verifier = await _failed_aggregate(case)
    checkpoint = await case.db.get_integration_checkpoint("epic")
    owner = await _owner(case)
    checkout = case.tmp_path / "held-verifier"
    _git(["clone", "--branch", "aq/epic", str(case.origin), str(checkout)])
    _git(["config", "user.name", "Test"], checkout)
    _git(["config", "user.email", "test@example.com"], checkout)
    await case.db.create_agent(Agent(
        id="held-agent", name="held verifier", profile_id="worker",
        state=AgentState.BUSY, current_task_id=verifier,
    ))
    await case.db.create_workspace(Workspace(
        id="held-workspace", project_id="p", workspace_path=str(checkout),
        source_type=RepoSourceType.CLONE, locked_by_task_id=verifier,
        locked_by_agent_id="held-agent",
    ))
    await case.db.create_session(SessionRecord(
        id="held-session", task_id=verifier, project_id="p", agent_id="held-agent",
        profile_id="worker", harness="codex", provider="fake", name="held-verifier",
        lifecycle="pool", work_dir=str(checkout), epoch="epoch", instance_token="instance",
        started_at=time.time(), state="running", desired_state="running",
        claim_phase="active", last_claim_epoch=1,
    ))
    subject = {
        "project_id": "p", "operation_id": case.operation_id, "task_id": "epic",
        "episode_id": checkpoint["episode_id"], "generation": checkpoint["generation"],
        "head_sha": red_head, "verifier_task_id": verifier,
        "target": {"repository_id": "repo", "branch": "aq/epic"},
        "expected_token": owner["fence_token"] - 1,
        "next_owner_id": verifier, "next_role": "verifier",
    }
    async with case.db.immediate() as conn:
        await conn.execute(delete(task_completion_records))
        await conn.execute(update(tasks).where(tasks.c.id == verifier).values(
            status="IN_PROGRESS", assigned_agent_id="held-agent", claim_epoch=1, retry_count=2,
        ))
        await conn.execute(update(integration_branch_owners)
                           .where(integration_branch_owners.c.id == owner["id"])
                           .values(handoff_state="attached", session_id="held-session",
                                   workspace_id="held-workspace"))
        await conn.execute(insert(integration_outbox).values(
            id="held-subject", dedup_key="held-subject", project_id="p",
            event_type="task.integration_ready", payload=subject,
            created_at=time.time(), available_at=time.time(),
        ))
        evidence = dict(
            id="held-red", operation_id=case.operation_id, parent_task_id="epic",
            parent_generation=checkpoint["generation"], parent_head_sha=red_head,
            producer_id="forge", workflow_id="aggregate", run_id="red-run", attempt=1,
            required_check_version="test", checks={"unit": "failure"},
            conclusion="failure", classification="conclusive", observed_at=time.time(),
        )
        await conn.execute(insert(integration_check_evidence).values(
            **{**evidence, **(evidence_changes or {})}
        ))
    provider = SimpleNamespace(stop=AsyncMock(), confirm_stopped=AsyncMock(return_value=True))
    orchestrator.db, orchestrator.git = case.db, GitManager()
    orchestrator.session_providers.create = lambda *_args: provider
    return red_head, verifier, checkout, provider


@pytest.mark.parametrize("failed_check", ["failure", "skipped", "neutral"])
async def test_held_red_verifier_recovery_preserves_evidence_and_detach_proof(
    case, orchestrator_factory, failed_check,
):
    orchestrator = await orchestrator_factory()
    red_head, verifier, checkout, provider = await _held_red_aggregate(
        case, orchestrator, evidence_changes={"checks": {"unit": failed_check}},
    )
    [red] = await _rows(case.db, integration_check_evidence)
    # Sibling suites share the observation time; a green sibling does not
    # supersede this observation's failed required check.
    async with case.db.immediate() as conn:
        await conn.execute(insert(integration_check_evidence).values(**dict(
            red, id="sibling-suite", workflow_id="other", run_id="sibling-run",
            checks={"other": "success"},
        )))
    original_evidence = await _rows(case.db, integration_check_evidence)
    original_receipts = await _rows(case.db, task_delivery_receipts)
    original_owner = await _owner(case)
    recovery = FailedVerificationRecovery(
        case.db, case.promotion, confirm_handoff=orchestrator.aconfirm_integration_owner_handoff,
    )
    diagnosis = await recovery.run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    provider.stop.assert_not_awaited()
    assert await _owner(case) == original_owner
    assert (await case.db.get_session("held-session")).claim_phase == "active"
    result = await recovery.run("epic", dry_run=False, expected_head_sha=red_head,
                                reason="collect completed fix")
    assert result["outcome"] == "reopened", result
    provider.stop.assert_awaited_once()
    provider.confirm_stopped.assert_awaited_once()
    [handle] = provider.stop.await_args.args
    assert handle.instance_token == "instance"
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], checkout) == "HEAD"
    session = await case.db.get_session("held-session")
    assert session.state == "stopped" and session.claim_phase is None and session.task_id is None
    assert (await case.db.get_workspace("held-workspace")).locked_by_task_id is None
    failed = await case.db.get_task(verifier)
    assert failed.status == TaskStatus.FAILED and failed.assigned_agent_id is None
    assert failed.retry_count == 2
    [completion] = await _rows(case.db, task_completion_records)
    assert completion["outcome"] == "fail" and json.loads(completion["commits"]) == [red_head]
    assert json.loads(completion["verification"]) == red
    assert await _rows(case.db, integration_check_evidence) == original_evidence
    assert await _rows(case.db, task_delivery_receipts) == original_receipts
    [audit] = await _rows(case.db, events, events.c.event_type == RECOVERY_EVENT)
    payload = json.loads(audit["payload"])
    assert payload["ci_failure_evidence_id"] == "held-red"
    assert payload["previous_attachment"] == original_owner
    assert payload["failure_subject"]["outbox_id"] == "held-subject"
    assert (await _owner(case))["fence_token"] == original_owner["fence_token"] + 1
    # A replay neither appends another completion nor touches the stopped process.
    assert (await recovery.run("epic", dry_run=False, expected_head_sha=red_head,
                               reason="replay"))["outcome"] == "not_eligible"
    assert len(await _rows(case.db, task_completion_records)) == 1
    provider.stop.assert_awaited_once()


def _red_unit_row(*, observed_at: float) -> dict:
    """A conclusive red parent-evidence row for the ``unit`` required check."""
    return {
        "id": "red-unit",
        "operation_id": "op",
        "parent_task_id": "epic",
        "parent_generation": 1,
        "parent_head_sha": "f" * 40,
        "producer_id": "forge",
        "workflow_id": "suite-a",
        "run_id": "run-a",
        "attempt": 1,
        "required_check_version": "v1",
        "checks": {"unit": "failure"},
        "conclusion": "failure",
        "classification": "conclusive",
        "observed_at": observed_at,
    }


def test_latest_red_parent_evidence_survives_a_later_green_sibling_suite() -> None:
    """A green sibling observed *after* a red sibling keeps the red visible.

    Regression: required checks can span multiple suites; ``ci.py`` stamps one
    evidence row per suite with ``observed_at=self.clock()``.  When the green
    sibling's row is written a fraction later, only the group-by-suite rule sees
    the red; a naive ``max(observed_at)`` picks the green sibling and drops it.
    """
    operation = {
        "id": "op",
        "parent_task_id": "epic",
        "required_check_version": "v1",
        "policy_snapshot": {"parent": {
            "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        }},
    }
    checkpoint = {"generation": 1, "checkpoint_sha": "f" * 40}
    red = _red_unit_row(observed_at=100.0)
    green_sibling = dict(
        _red_unit_row(observed_at=100.0005),
        id="sibling-green",
        workflow_id="suite-b",
        run_id="run-b",
        checks={"deploy": "success"},
        conclusion="success",
    )
    for order in ([red, green_sibling], [green_sibling, red]):
        found = latest_red_parent_evidence(
            order, operation=operation, checkpoint=checkpoint
        )
        assert found is not None and found["id"] == "red-unit", (
            f"red must surface for input order {order!r}"
        )


def test_latest_red_parent_evidence_still_superseded_by_later_green_same_suite() -> None:
    """A later green of the *same* suite supersedes its prior red (no regression)."""
    operation = {
        "id": "op",
        "parent_task_id": "epic",
        "required_check_version": "v1",
        "policy_snapshot": {"parent": {
            "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        }},
    }
    checkpoint = {"generation": 1, "checkpoint_sha": "f" * 40}
    red = _red_unit_row(observed_at=100.0)
    green_rerun = dict(
        _red_unit_row(observed_at=101.0),
        id="green-rerun",
        checks={"unit": "success"},
        conclusion="success",
    )
    found = latest_red_parent_evidence(
        [red, green_rerun], operation=operation, checkpoint=checkpoint
    )
    assert found is None


def test_latest_red_parent_evidence_empty_and_no_required_red() -> None:
    operation = {
        "id": "op",
        "parent_task_id": "epic",
        "required_check_version": "v1",
        "policy_snapshot": {"parent": {
            "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        }},
    }
    checkpoint = {"generation": 1, "checkpoint_sha": "f" * 40}
    assert latest_red_parent_evidence([], operation=operation, checkpoint=checkpoint) is None
    green_only = [dict(_red_unit_row(observed_at=100.0),
                       checks={"deploy": "success"}, conclusion="success")]
    assert latest_red_parent_evidence(
        green_only, operation=operation, checkpoint=checkpoint
    ) is None


@pytest.mark.parametrize("mismatch", [
    "head", "generation", "producer", "version", "classification", "subject", "fence",
    "newer_green", "before_handoff",
])
async def test_held_verifier_requires_current_trusted_exact_red_ci(
    case, orchestrator_factory, mismatch,
):
    orchestrator = await orchestrator_factory()
    changes = {
        "head": {"parent_head_sha": case.base}, "generation": {"parent_generation": 999},
        "producer": {"producer_id": "untrusted"}, "version": {"required_check_version": "old"},
        "classification": {"classification": "full_suite_fallback"},
        "before_handoff": {"observed_at": 0},
    }
    head, verifier, _, provider = await _held_red_aggregate(
        case, orchestrator, evidence_changes=changes.get(mismatch),
    )
    async with case.db.immediate() as conn:
        if mismatch == "subject":
            await conn.execute(delete(integration_outbox)
                               .where(integration_outbox.c.id == "held-subject"))
        elif mismatch == "fence":
            await conn.execute(update(integration_branch_owners)
                               .values(fence_token=999))
        elif mismatch == "newer_green":
            [red] = await _rows(case.db, integration_check_evidence)
            await conn.execute(insert(integration_check_evidence).values(**dict(
                red, id="newer-green", conclusion="success", checks={"unit": "success"},
                run_id="rerun", observed_at=red["observed_at"] + 1,
            )))
    recovery = FailedVerificationRecovery(
        case.db, case.promotion, confirm_handoff=orchestrator.aconfirm_integration_owner_handoff,
    )
    result = await recovery.run("epic", dry_run=False, expected_head_sha=head, reason="fix")
    assert result["outcome"] in {"blocked", "ambiguous"}, result
    provider.stop.assert_not_awaited()
    assert (await case.db.get_task(verifier)).status == TaskStatus.IN_PROGRESS
    assert not await _rows(case.db, task_completion_records)
    assert (await case.db.get_integration_checkpoint("epic"))["state"] == "verifying"


@pytest.mark.parametrize("unproved", ["running", "dirty", "unpublished", "no_callback"])
async def test_red_verifier_cannot_reopen_without_process_and_checkout_proof(
    case, orchestrator_factory, unproved,
):
    orchestrator = await orchestrator_factory()
    head, verifier, checkout, provider = await _held_red_aggregate(case, orchestrator)
    if unproved == "running":
        provider.confirm_stopped.return_value = False
    elif unproved in {"dirty", "unpublished"}:
        (checkout / "work.txt").write_text("unfinished verifier fix\n")
        if unproved == "unpublished":
            _git(["add", "work.txt"], checkout)
            _git(["commit", "-m", "unpublished verifier fix"], checkout)
    callback = None if unproved == "no_callback" else orchestrator.aconfirm_integration_owner_handoff
    result = await FailedVerificationRecovery(
        case.db, case.promotion, confirm_handoff=callback,
    ).run("epic", dry_run=False, expected_head_sha=head, reason="fix")
    assert result["outcome"] == "blocked", result
    assert (await _owner(case))["handoff_state"] in {"attached", "handoff_pending"}
    assert (await case.db.get_workspace("held-workspace")).locked_by_task_id == verifier
    assert (await case.db.get_task(verifier)).status == TaskStatus.IN_PROGRESS
    assert not await _rows(case.db, task_completion_records)
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], checkout) == "aq/epic"
    if unproved in {"dirty", "unpublished"}:
        assert (checkout / "work.txt").read_text() == "unfinished verifier fix\n"


@pytest.mark.parametrize("holder", ["parent_hold", "verifier_hold", "another_workspace"])
async def test_held_red_verifier_keeps_operator_holds_and_other_holders(
    case, orchestrator_factory, holder,
):
    orchestrator = await orchestrator_factory()
    head, verifier, _, provider = await _held_red_aggregate(case, orchestrator)
    if holder == "another_workspace":
        await case.db.create_workspace(Workspace(
            id="another", project_id="p", workspace_path=str(case.tmp_path / "another"),
            source_type=RepoSourceType.CLONE, locked_by_task_id=verifier,
        ))
    else:
        subject = "epic" if holder == "parent_hold" else verifier
        async with case.db.immediate() as conn:
            await case.db._upsert_meta(subject, "manual_pause", {"reason": "operator hold"}, conn=conn)
    result = await FailedVerificationRecovery(
        case.db, case.promotion, confirm_handoff=orchestrator.aconfirm_integration_owner_handoff,
    ).run("epic", dry_run=False, expected_head_sha=head, reason="fix")
    assert result["outcome"] == "blocked", result
    provider.stop.assert_not_awaited()
    assert (await _owner(case))["handoff_state"] == "attached"
    assert not await _rows(case.db, task_completion_records)


async def test_held_red_verifier_rechecks_ci_after_external_handoff(case, orchestrator_factory):
    orchestrator = await orchestrator_factory()
    head, verifier, _, provider = await _held_red_aggregate(case, orchestrator)

    async def handoff_then_green(owner):
        assert await orchestrator.aconfirm_integration_owner_handoff(owner)
        [red] = await _rows(case.db, integration_check_evidence)
        async with case.db.immediate() as conn:
            await conn.execute(insert(integration_check_evidence).values(**dict(
                red, id="green-after-stop", conclusion="success", checks={"unit": "success"},
                run_id="rerun", observed_at=red["observed_at"] + 1,
            )))
        return True

    result = await FailedVerificationRecovery(
        case.db, case.promotion, confirm_handoff=handoff_then_green,
    ).run("epic", dry_run=False, expected_head_sha=head, reason="fix")
    assert result["outcome"] == "blocked", result
    provider.stop.assert_awaited_once()
    assert (await case.db.get_task(verifier)).status == TaskStatus.PAUSED
    assert (await _owner(case))["handoff_state"] == "released"
    assert not await _rows(case.db, task_completion_records)
    assert (await case.db.get_integration_checkpoint("epic"))["state"] == "verifying"


async def test_failed_verification_reopens_then_receipts_fix_and_wakes_fresh_verifier(case):
    red_head, old_verifier = await _failed_aggregate(case)
    recovery = FailedVerificationRecovery(case.db, case.promotion)
    old_receipts = await _rows(case.db, task_delivery_receipts)
    old_owner = await _owner(case)
    old_checkpoint = await case.db.get_integration_checkpoint("epic")
    blocked = await ChildDelivery(case.db, case.promotion).diagnose("epic.3")
    assert blocked["outcome"] == "blocked"
    assert "not awaiting children" in blocked["reason"]
    diagnosis = await recovery.run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    assert diagnosis["head_sha"] == red_head
    assert await _owner(case) == old_owner
    applied = await recovery.run("epic", dry_run=False, expected_head_sha=red_head,
                                 reason="collect completed scope fix", operator_id="operator")
    assert applied["outcome"] == "reopened", applied
    checkpoint = await case.db.get_integration_checkpoint("epic")
    assert checkpoint["generation"] == old_checkpoint["generation"] + 1
    assert checkpoint["episode_id"] == old_checkpoint["episode_id"]
    assert checkpoint["checkpoint_sha"] == red_head
    assert checkpoint["state"] == "awaiting_children"
    assert checkpoint["current_verification_id"] is None
    assert (await _owner(case))["fence_token"] == old_owner["fence_token"] + 1
    assert (await _operation(case))["verifier_task_id"] is None
    assert await _rows(case.db, task_delivery_receipts) == old_receipts
    [audit] = await _rows(case.db, events, events.c.event_type == RECOVERY_EVENT)
    payload = json.loads(audit["payload"])
    assert payload["previous_verifier_task_id"] == old_verifier
    assert payload["failure_completion_id"] == "failed-completion"
    assert payload["receipts"] == [r["id"] for r in old_receipts]
    # Old generation and old writer can no longer certify or mutate the aggregate.
    stale = await case.hierarchy.parent_completion.verify_parent(
        "epic", old_checkpoint["generation"], red_head, ["old-evidence"])
    assert stale["outcome"] == "stale_generation"
    with pytest.raises(StaleFence):
        async with case.db.immediate() as conn:
            await BranchOwnership(case.db).transfer_detached_on(
                conn, Fence(target={"repository_id": "repo", "branch": "aq/epic"},
                            owner_id=old_verifier, token=old_owner["fence_token"]),
                "old-writer", "verifier")
    assert await _promote_next(case, 30.0) is not None
    receipts = await _rows(case.db, task_delivery_receipts)
    assert len(receipts) == 3
    fix_receipt = next(r for r in receipts if r["source_task_id"] == "epic.3")
    assert fix_receipt["parent_operation_id"] == case.operation_id
    assert fix_receipt["parent_episode_id"] == old_checkpoint["episode_id"]
    new_head = _remote_tip(case)
    assert new_head != red_head
    async with case.db.immediate() as conn:
        ready = await case.hierarchy.parent_completion.mark_ready_on(conn, "epic")
    assert ready["outcome"] == "ready", ready
    new_verifier = (await _operation(case))["verifier_task_id"]
    assert new_verifier != old_verifier
    assert (await case.db.get_task(old_verifier)).status == TaskStatus.FAILED
    owner = await _owner(case)
    async with case.db.immediate() as conn:
        fence = await BranchOwnership(case.db).transfer_detached_on(
            conn, Fence(target={"repository_id": "repo", "branch": "aq/epic"},
                        owner_id=owner["owner_id"], token=owner["fence_token"]),
            new_verifier, "verifier")
    await case.hierarchy.parent_completion.wake_verifier("epic", fence)
    assert (await case.db.get_task(new_verifier)).status == TaskStatus.READY
    checkpoint = await case.db.get_integration_checkpoint("epic")
    assert checkpoint["state"] == "verifying"
    assert checkpoint["checkpoint_sha"] == new_head
    assert checkpoint["verified_sha"] is None
    async with case.db.immediate() as conn:
        await conn.execute(insert(integration_check_evidence).values(
            id="fresh-green", operation_id=case.operation_id, parent_task_id="epic",
            parent_generation=checkpoint["generation"], parent_head_sha=new_head,
            producer_id="forge", workflow_id="aggregate", run_id="fresh", attempt=1,
            required_check_version="test", checks={"unit": "success"},
            conclusion="success", classification="conclusive", observed_at=2e10,
        ))
    verified = await case.hierarchy.parent_completion.verify_parent(
        "epic", checkpoint["generation"], new_head, ["fresh-green"])
    assert verified["outcome"] == "verified", verified
    completed = await case.hierarchy.parent_completion.complete_parent(
        "epic", checkpoint["generation"], new_head)
    assert completed["outcome"] == "completed", completed


@pytest.mark.parametrize("owner_state", ["reserved", "released"])
async def test_failed_verification_recovers_settled_repair_fence(case, owner_state):
    red_head, _ = await _failed_aggregate(case)
    repair_id = "settled-repair"
    await case.db.create_task(Task(
        id=repair_id, project_id="p", repo_id="repo", branch_name="aq/epic",
        title="settled repair", description="no remaining writer", status=TaskStatus.COMPLETED,
    ))
    async with case.db.immediate() as conn:
        await conn.execute(insert(integration_repair_stages).values(
            operation_id=case.operation_id, ordinal=0, intelligence_class="high",
            policy={}, trigger_id="red", starting_sha=red_head, current_subject={}, attempts=1,
            deadline_at=20.0, deadline_event_id="deadline", state="expired",
            repair_task_id=repair_id, writer_kind="existing_verifier",
        ))
        await conn.execute(update(integration_branch_owners)
                           .where(integration_branch_owners.c.ref == "aq/epic")
                           .values(owner_id=repair_id, owner_role="repair", handoff_state=owner_state))
    old_stages = await _rows(case.db, integration_repair_stages)
    old_owner = await _owner(case)
    recovery = FailedVerificationRecovery(case.db, case.promotion)
    assert (await recovery.run("epic"))["outcome"] == "would_reopen"
    result = await recovery.run("epic", dry_run=False, expected_head_sha=red_head,
                                reason="collect fix after settled repair", operator_id="operator")
    assert result["outcome"] == "reopened", result
    owner = await _owner(case)
    assert owner["owner_role"] == "collector"
    assert owner["fence_token"] > old_owner["fence_token"]
    assert await _rows(case.db, integration_repair_stages) == old_stages


async def test_failed_verification_refuses_unrelated_repair_fence(case):
    await _failed_aggregate(case)
    async with case.db.immediate() as conn:
        await conn.execute(update(integration_branch_owners)
                           .where(integration_branch_owners.c.ref == "aq/epic")
                           .values(owner_id="unrelated-repair", owner_role="repair"))
    result = await FailedVerificationRecovery(case.db, case.promotion).run("epic")
    assert result["outcome"] == "blocked"
    assert "detached fence" in result["reason"]


@pytest.mark.parametrize("blocker", [
    "attached_owner", "wrong_owner", "remote_moved", "failure_missing", "failure_head",
    "manual_hold", "no_fix", "mutation", "repair_writer",
])
async def test_failed_verification_recovery_refuses_ambiguous_state(case, blocker):
    red_head, verifier = await _failed_aggregate(case)
    async with case.db.immediate() as conn:
        if blocker in {"attached_owner", "wrong_owner"}:
            values = ({"session_id": "writer", "handoff_state": "attached"}
                      if blocker == "attached_owner" else {"owner_id": "another-operation"})
            await conn.execute(update(integration_branch_owners).values(**values))
        elif blocker == "failure_missing":
            await conn.execute(delete(task_completion_records))
        elif blocker == "failure_head":
            await conn.execute(update(task_completion_records).values(commits=json.dumps([case.base])))
        elif blocker == "manual_hold":
            from src.database.tables import task_metadata

            await conn.execute(insert(task_metadata).values(
                task_id="epic", key="manual_pause", value="true"))
        elif blocker == "no_fix":
            await conn.execute(update(tasks).where(tasks.c.id == "epic.3").values(status="READY"))
        elif blocker == "mutation":
            await conn.execute(update(integration_promotion_intents)
                               .where(integration_promotion_intents.c.source_task_id == "epic.2")
                               .values(state="prepared"))
        elif blocker == "repair_writer":
            await conn.execute(insert(integration_repair_stages).values(
                operation_id=case.operation_id, ordinal=0, intelligence_class="high",
                policy={}, trigger_id="red", starting_sha=red_head, current_subject={}, attempts=1,
                deadline_at=2e10, deadline_event_id="deadline", state="active",
                repair_task_id=verifier, writer_kind="existing_verifier",
            ))
    if blocker == "remote_moved":
        _git(["push", "origin", f"{case.heads['epic.1']}:refs/heads/aq/epic", "--force"], case.work)
    owner = await _owner(case)
    checkpoint = await case.db.get_integration_checkpoint("epic")
    result = await FailedVerificationRecovery(case.db, case.promotion).run(
        "epic", dry_run=False, expected_head_sha=red_head, reason="attempt recovery")
    assert result["outcome"] in {"blocked", "ambiguous"}, result
    assert await _owner(case) == owner
    assert await case.db.get_integration_checkpoint("epic") == checkpoint


@pytest.mark.parametrize("missing_head", [False, True])
async def test_failed_verification_recovery_preserves_stage_budgets_and_human_gates(case, missing_head):
    if missing_head:
        red_head, _, _ = await _empty_failure_subject(case)
    else:
        red_head, _ = await _failed_aggregate(case)
    async with case.db.immediate() as conn:
        await conn.execute(insert(integration_repair_stages).values(
            operation_id=case.operation_id, ordinal=1, policy={"attempt_limit": 3},
            intelligence_class="high", starting_sha=red_head, attempts=3,
            started_at=20.0, deadline_at=30.0, deadline_event_id="original-deadline",
            state="passed", completed_at=29.0, dossier={"original": "evidence"},
        ))
        await conn.execute(update(integration_repair_operations).values(active_stage=1))
        await conn.execute(insert(gates).values(
            id="rollout-gate", project_id="p", gate_type="human", title="Phase 2 rollout",
            status="open", created_at=20.0,
        ))
        await conn.execute(insert(task_gates).values(task_id="epic", gate_id="rollout-gate"))
    stages = await _rows(case.db, integration_repair_stages)
    frozen_policy = (await _operation(case))["policy_snapshot"]
    recovery = FailedVerificationRecovery(case.db, case.promotion)
    diagnosis = await recovery.run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    assert diagnosis["human_gates"] == ["rollout-gate"]
    result = await recovery.run("epic", dry_run=False, expected_head_sha=red_head, reason="fix")
    assert result["outcome"] == "reopened"
    assert await _rows(case.db, integration_repair_stages) == stages
    operation = await _operation(case)
    assert operation["active_stage"] == 1
    assert operation["policy_snapshot"] == frozen_policy
    [gate] = await _rows(case.db, gates)
    assert gate["status"] == "open"
    [audit] = await _rows(case.db, events, events.c.event_type == RECOVERY_EVENT)
    assert json.loads(audit["payload"])["stages"] == stages


@pytest.mark.parametrize("context", ["session_close_hard_failure", "max_retries"])
async def test_failed_verification_recovery_accepts_terminal_failed_close_without_reset(case, context):
    red_head, verifier = await _failed_aggregate(case)
    await case.db.transition_task(verifier, TaskStatus.BLOCKED, context=context, force=True)
    await case.db.update_task(verifier, retry_count=3)
    original = await case.db.get_task(verifier)
    result = await FailedVerificationRecovery(case.db, case.promotion).run(
        "epic", dry_run=False, expected_head_sha=red_head, reason="collect scope fix")
    assert result["outcome"] == "reopened", result
    assert await case.db.get_task(verifier) == original
    assert await case.db.get_task_meta(verifier, "blocked_terminal") == context


async def test_failed_verification_cannot_reverify_unchanged_red_head(case):
    red_head, _ = await _failed_aggregate(case)
    recovery = FailedVerificationRecovery(case.db, case.promotion)
    applied = await recovery.run("epic", dry_run=False, expected_head_sha=red_head, reason="fix")
    assert applied["outcome"] == "reopened"
    # A child delivery that changes no parent commit cannot re-arm the red aggregate.
    receipts = await _rows(case.db, task_delivery_receipts)
    child = await case.db.get_integration_checkpoint("epic.3")
    receipt = dict(receipts[-1], id="unchanged-fix", domain_key="unchanged-fix",
                   source_task_id="epic.3", reviewed_head_sha=child["checkpoint_sha"],
                   before_sha=red_head, squash_sha=red_head, after_sha=red_head,
                   created_at=max(r["created_at"] for r in receipts) + 1)
    async with case.db.immediate() as conn:
        await conn.execute(insert(task_delivery_receipts).values(**receipt))
        result = await case.hierarchy.parent_completion.mark_ready_on(conn, "epic")
    assert result["outcome"] == "waiting", result
    assert {"task_id": "epic", "reason": "failed_aggregate_head_unchanged"} in result["blockers"]
    checkpoint = await case.db.get_integration_checkpoint("epic")
    result = await case.hierarchy.parent_completion.verify_parent(
        "epic", checkpoint["generation"], red_head, ["new-check-evidence"])
    assert result["outcome"] == "waiting"
    assert (await _operation(case))["verifier_task_id"] is None
    assert await case.db.get_integration_checkpoint("epic") == checkpoint


async def test_failed_verification_rechecks_remote_after_diagnosis(case, monkeypatch):
    red_head, _ = await _failed_aggregate(case)
    recovery = FailedVerificationRecovery(case.db, case.promotion)
    original = recovery._diagnose

    async def move_after_proof(task_id):
        result = await original(task_id)
        _git(["push", "origin", f"{case.heads['epic.1']}:refs/heads/aq/epic", "--force"], case.work)
        return result

    monkeypatch.setattr(recovery, "_diagnose", move_after_proof)
    owner = await _owner(case)
    result = await recovery.run("epic", dry_run=False, expected_head_sha=red_head, reason="recover")
    assert result["outcome"] == "changed", result
    assert await _owner(case) == owner


async def _empty_failure_subject(case):
    """Legacy g3 failure while the mutable checkpoint has already reached g4."""
    red_head, verifier_id = await _failed_aggregate(case)
    checkpoint = await case.db.get_integration_checkpoint("epic")
    owner = await _owner(case)
    payload = {
        "project_id": "p", "operation_id": case.operation_id, "task_id": "epic",
        "episode_id": checkpoint["episode_id"], "generation": 3, "head_sha": red_head,
        "verifier_task_id": verifier_id, "target": {"repository_id": "repo", "branch": "aq/epic"},
        "expected_token": owner["fence_token"], "next_owner_id": verifier_id,
        "next_role": "verifier",
    }
    async with case.db.immediate() as conn:
        await conn.execute(update(task_completion_records).values(
            commits="[]", summary="prose is not trusted head evidence"))
        await conn.execute(update(task_integration_checkpoints)
                           .where(task_integration_checkpoints.c.task_id == "epic")
                           .values(generation=4))
        await conn.execute(insert(integration_outbox).values(
            id="immutable-g3", dedup_key="immutable-g3", project_id="p",
            event_type="task.integration_ready", payload=payload, available_at=20.0,
            created_at=time.time(),
        ))
    return red_head, verifier_id, payload


async def test_empty_failure_binds_immutable_subject_preserving_history_and_fresh_verifier(case):
    red_head, verifier_id, subject = await _empty_failure_subject(case)
    await case.db.update_task(verifier_id, retry_count=3)
    await case.db.transition_task(verifier_id, TaskStatus.BLOCKED,
                                  context="session_close_hard_failure", force=True)
    completion = await _rows(case.db, task_completion_records)
    verifier = await case.db.get_task(verifier_id)
    stages = await _rows(case.db, integration_repair_stages)
    outbox = await _rows(case.db, integration_outbox)
    recovery = FailedVerificationRecovery(case.db, case.promotion)
    diagnosis = await recovery.run("epic")
    assert diagnosis["outcome"] == "would_reopen", diagnosis
    binding = diagnosis["delegates"][0]["failure_subject"]
    assert binding["outbox_id"] == "immutable-g3"
    assert binding["subject"] == subject
    assert await _rows(case.db, integration_outbox) == outbox
    assert await _rows(case.db, events, events.c.event_type == RECOVERY_EVENT) == []
    applied = await recovery.run("epic", dry_run=False, expected_head_sha=red_head,
                                 reason="immutable g3 proof for empty failure", operator_id="operator")
    assert applied["outcome"] == "reopened", applied
    assert await _rows(case.db, task_completion_records) == completion
    assert await case.db.get_task(verifier_id) == verifier
    assert await _rows(case.db, integration_repair_stages) == stages
    [audit] = await _rows(case.db, events, events.c.event_type == RECOVERY_EVENT)
    audit = json.loads(audit["payload"])
    assert audit["failure_completion_id"] == completion[0]["id"]
    assert audit["failure_subject"] == binding
    assert (audit["previous_generation"], audit["generation"]) == (4, 5)
    assert await _promote_next(case, 30.0) is not None
    async with case.db.immediate() as conn:
        ready = await case.hierarchy.parent_completion.mark_ready_on(conn, "epic")
    assert ready["outcome"] == "ready", ready
    assert ready["head_sha"] != red_head
    assert (await _operation(case))["verifier_task_id"] != verifier_id


@pytest.mark.parametrize("mismatch", [
    "project_id", "operation_id", "task_id", "episode_id", "repository", "branch", "head_sha",
    "next_owner_id", "next_role", "future_generation", "boolean_generation", "before_episode",
    "early_event", "late_event", "missing_event", "duplicate_event", "malformed_commits", "nonempty_commits",
    "attached_owner", "manual_hold", "remote_moved",
])
async def test_empty_failure_subject_refuses_mismatch_and_unsettled_writer(case, mismatch):
    from src.database.tables import task_metadata

    red_head, verifier_id, subject = await _empty_failure_subject(case)
    subject = dict(subject)
    async with case.db.immediate() as conn:
        if mismatch in {"project_id", "operation_id", "task_id", "episode_id", "next_owner_id", "next_role"}:
            subject[mismatch] = "unrelated"
        elif mismatch in {"repository", "branch"}:
            subject["target"] = {**subject["target"],
                                 "repository_id" if mismatch == "repository" else "branch": "other"}
        elif mismatch == "head_sha":
            subject["head_sha"] = case.base
        elif mismatch in {"future_generation", "boolean_generation"}:
            subject["generation"] = 5 if mismatch == "future_generation" else True
        elif mismatch == "before_episode":
            subject["generation"] = -1
        elif mismatch in {"early_event", "late_event"}:
            await conn.execute(update(integration_outbox)
                               .where(integration_outbox.c.id == "immutable-g3")
                               .values(created_at=3e10 if mismatch == "late_event" else 1.0))
        elif mismatch == "missing_event":
            await conn.execute(delete(integration_outbox)
                               .where(integration_outbox.c.id == "immutable-g3"))
        elif mismatch == "duplicate_event":
            await conn.execute(insert(integration_outbox).values(
                id="another-subject", dedup_key="another-subject", project_id="p",
                event_type="task.integration_ready", payload=subject, available_at=21.0,
                created_at=time.time(),
            ))
        elif mismatch in {"malformed_commits", "nonempty_commits"}:
            await conn.execute(update(task_completion_records).values(
                commits="invalid" if mismatch == "malformed_commits" else json.dumps([case.base])))
        elif mismatch == "attached_owner":
            await conn.execute(update(integration_branch_owners).values(
                handoff_state="attached", session_id="still-running"))
        elif mismatch == "manual_hold":
            await conn.execute(insert(task_metadata).values(
                task_id=verifier_id, key="manual_pause", value="true"))
        await conn.execute(update(integration_outbox)
                           .where(integration_outbox.c.id == "immutable-g3").values(payload=subject))
    if mismatch == "remote_moved":
        _git(["push", "origin", f"{case.base}:refs/heads/aq/epic", "--force"], case.work)
    owner = await _owner(case)
    checkpoint = await case.db.get_integration_checkpoint("epic")
    completion = await _rows(case.db, task_completion_records)
    result = await FailedVerificationRecovery(case.db, case.promotion).run(
        "epic", dry_run=False, expected_head_sha=red_head, reason="must not accept ambiguous evidence")
    assert result["outcome"] in {"blocked", "ambiguous"}, result
    assert await _owner(case) == owner
    assert await case.db.get_integration_checkpoint("epic") == checkpoint
    assert await _rows(case.db, task_completion_records) == completion
    assert await _rows(case.db, events, events.c.event_type == RECOVERY_EVENT) == []


async def test_empty_failure_rechecks_frozen_subject_after_diagnosis(case, monkeypatch):
    red_head, _, subject = await _empty_failure_subject(case)
    recovery = FailedVerificationRecovery(case.db, case.promotion)
    original = recovery._diagnose

    async def change_after_proof(task_id):
        result = await original(task_id)
        async with case.db.immediate() as conn:
            await conn.execute(update(integration_outbox)
                               .where(integration_outbox.c.id == "immutable-g3")
                               .values(payload={**subject, "head_sha": case.base}))
        return result

    monkeypatch.setattr(recovery, "_diagnose", change_after_proof)
    owner = await _owner(case)
    result = await recovery.run("epic", dry_run=False, expected_head_sha=red_head, reason="recover")
    assert result["outcome"] == "changed", result
    assert await _owner(case) == owner


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
    """Seed a historical cancellation, whose public creation control is retired."""
    from src.integration.delegate_release import release_delegates

    now = time.time()
    async with case.db.immediate() as conn:
        delegates = list((await conn.execute(select(integration_repair_stages.c.repair_task_id).where(
            integration_repair_stages.c.operation_id == case.operation_id,
            integration_repair_stages.c.repair_task_id.is_not(None),
        ))).scalars())
        await conn.execute(update(integration_branch_owners).where(
            integration_branch_owners.c.owner_id.in_([case.operation_id, *delegates]),
            integration_branch_owners.c.handoff_state == "reserved",
            integration_branch_owners.c.session_id.is_(None),
            integration_branch_owners.c.workspace_id.is_(None),
        ).values(handoff_state="released", updated_at=now))
        await conn.execute(update(integration_repair_operations).where(
            integration_repair_operations.c.id == case.operation_id,
        ).values(state="cancelled", updated_at=now))
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == case.operation_id,
            integration_repair_stages.c.state.not_in(["passed", "cancelled"]),
        ).values(state="cancelled", completed_at=now))
    await release_delegates(
        case.db, operation_ids=[case.operation_id], now=now, released_by="historical_fixture",
    )
    return {"outcome": "cancelled", "operation_id": case.operation_id, "reason": reason}


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


async def _no_progress_collection(case):
    """A later real child conflict is stranded behind a terminal debug incident."""
    conflict = await _deliver_first_and_conflict_second(case)
    operation = await _operation(case)
    subject = {"kind": "parent", "generation": 3, "head_sha": conflict["expected_target"]}
    for ordinal, status in ((0, TaskStatus.COMPLETED), (1, TaskStatus.FAILED)):
        await case.db.create_task(
            Task(
                id=f"old-repair-{ordinal}",
                project_id="p",
                repo_id="repo",
                branch_name="aq/epic",
                title="Ended repair",
                description="",
                status=status,
                created_by_kind="integration_repair",
                created_by_id=case.operation_id,
            )
        )
    async with case.db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(
                integration_repair_operations.c.id == case.operation_id,
            )
            .values(state="escalated", active_stage=1)
        )
        for ordinal, state in ((0, "passed"), (1, "expired")):
            incident = {
                "incident_id": f"repair-no-progress:{case.operation_id}:1",
                "stage": 1,
                "subject": subject,
                "attempts": 0,
                "deadline_at": 20.0,
                "repair_task_id": "old-repair-1",
                "recorded_at": 20.0,
            }
            await conn.execute(
                insert(integration_repair_stages).values(
                    operation_id=case.operation_id,
                    ordinal=ordinal,
                    policy=operation["policy_snapshot"]["parent"]["repair"],
                    starting_sha=conflict["expected_target"],
                    trigger_id=f"stage-exhausted:{case.operation_id}:{ordinal - 1}",
                    current_subject=subject,
                    repair_task_id=f"old-repair-{ordinal}",
                    writer_kind="repair_delegate",
                    state=state,
                    attempts=0,
                    started_at=12.0,
                    deadline_at=20.0,
                    completed_at=20.0,
                    deadline_event_id=f"old-deadline-{ordinal}",
                    dossier={"supervisor_recovery": incident} if ordinal == 1 else {},
                )
            )
    return conflict


@pytest.mark.parametrize("confirmed_workspace", [False, True])
async def test_no_progress_collection_reopens_only_its_later_conflict(case, confirmed_workspace):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.database.tables import workspaces

    conflict = await _no_progress_collection(case)
    if confirmed_workspace:
        _git(["fetch", "origin", "aq/epic"], case.work)
        _git(["switch", "-c", "aq/epic", "origin/aq/epic"], case.work)
        async with case.db.immediate() as conn:
            await conn.execute(
                insert(workspaces).values(
                    id="former-repair",
                    project_id="p",
                    workspace_path=str(case.work),
                    source_type="link",
                    enabled=True,
                    created_at=1.0,
                )
            )
            await conn.execute(
                update(integration_branch_owners).values(
                    confirmed_workspace_id="former-repair",
                )
            )
    old_stages = await _rows(case.db, integration_repair_stages)
    receipts = await _rows(case.db, task_delivery_receipts)
    checkpoint = await case.db.get_integration_checkpoint("epic")
    original_owner = await _owner(case)
    recovery = _recovery(case, RepairService(case.db).dispatch)
    handler = IntegrationCommandsMixin()
    handler.db = case.db
    handler.orchestrator = SimpleNamespace(
        promotion_service=case.promotion,
        repair_service=RepairService(case.db),
    )
    preview = await handler._cmd_integration_reopen_collection({"task_id": "epic"})
    assert preview["outcome"] == "would_reopen", preview
    assert preview["conflict"]["intent_id"] == conflict["id"]
    assert preview["stage"]["ordinal"] == 2
    assert await _rows(case.db, integration_repair_stages) == old_stages
    assert await _owner(case) == original_owner

    result = await handler._cmd_integration_reopen_collection(
        {
            "task_id": "epic",
            "dry_run": False,
            "expected_head_sha": preview["head_sha"],
            "reason": "Resume a later conflict stranded by no-progress",
        }
    )
    assert result["outcome"] == "reopened", result
    assert result["dispatch"]["outcome"] == "dispatched"
    new_delegate = f"repair-{case.operation_id}-2"
    assert result["dispatch"]["repair_task_id"] == new_delegate
    assert (await case.db.get_task(new_delegate)).status is TaskStatus.READY
    assert (await _owner(case))["fence_token"] == original_owner["fence_token"] + 2
    assert await case.db.get_integration_checkpoint("epic") == checkpoint
    assert await _rows(case.db, task_delivery_receipts) == receipts
    assert (
        await _rows(case.db, integration_repair_stages, integration_repair_stages.c.ordinal < 2)
        == old_stages
    )
    assert _remote_tip(case) == conflict["expected_target"]
    scope = await case.db.get_repair_filing_scope(new_delegate)
    assert scope["trigger_id"] == conflict["id"]
    assert PromotionService._repair_subject_matches_intent(scope, conflict)
    assert (await recovery.run("epic"))["outcome"] == "nothing_to_reopen"
    assert len(await _rows(case.db, integration_repair_stages)) == 3


@pytest.mark.parametrize(
    "blocker",
    [
        "missing_incident",
        "same_conflict",
        "wrong_fence",
        "moved_child",
        "live_holder",
        "two_conflicts",
        "dirty_checkout",
        "unpublished_ref",
        "remote_moved",
        "gate",
    ],
)
async def test_no_progress_collection_keeps_proofs_and_rejects_retrying_old_work(case, blocker):
    from src.database.tables import sessions, workspaces

    conflict = await _no_progress_collection(case)
    async with case.db.immediate() as conn:
        if blocker == "missing_incident":
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.ordinal == 1,
                )
                .values(dossier={})
            )
        elif blocker == "same_conflict":
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.ordinal == 1,
                )
                .values(trigger_id=conflict["id"])
            )
        elif blocker == "wrong_fence":
            await conn.execute(
                update(integration_branch_owners).values(
                    fence_token=conflict["fence_token"] + 1,
                )
            )
        elif blocker == "moved_child":
            await conn.execute(
                update(task_integration_checkpoints)
                .where(
                    task_integration_checkpoints.c.task_id == "epic.2",
                )
                .values(checkpoint_sha="e" * 40)
            )
        elif blocker == "live_holder":
            await conn.execute(
                insert(sessions).values(
                    id="live-old-repair",
                    task_id="old-repair-1",
                    project_id="p",
                    profile_id="debugger",
                    work_dir=str(case.work),
                    state="running",
                    harness="fake",
                    provider="fake",
                    name="live-old-repair",
                    lifecycle="task",
                    instance_token="live",
                    epoch="epoch",
                    started_at=21.0,
                )
            )
        elif blocker == "two_conflicts":
            await conn.execute(
                insert(integration_promotion_intents).values(
                    **{
                        key: value
                        for key, value in conflict.items()
                        if value is not None and key not in {"id", "domain_key", "receipt_id"}
                    },
                    id="other-conflict",
                    domain_key="other-conflict",
                    receipt_id="other-receipt",
                )
            )
        elif blocker == "gate":
            await conn.execute(
                insert(gates).values(
                    id="human-gate",
                    project_id="p",
                    gate_type="human",
                    title="Held",
                    status="open",
                    created_at=1.0,
                )
            )
            await conn.execute(insert(task_gates).values(task_id="epic", gate_id="human-gate"))
        elif blocker in {"dirty_checkout", "unpublished_ref"}:
            _git(["fetch", "origin", "aq/epic"], case.work)
            _git(["switch", "-c", "aq/epic", "origin/aq/epic"], case.work)
            await conn.execute(
                insert(workspaces).values(
                    id="former-repair",
                    project_id="p",
                    workspace_path=str(case.work),
                    source_type="link",
                    enabled=True,
                    created_at=1.0,
                )
            )
            await conn.execute(
                update(integration_branch_owners).values(
                    confirmed_workspace_id="former-repair",
                )
            )
            (case.work / "unpublished.txt").write_text("preserve me\n")
            if blocker == "unpublished_ref":
                _git(["add", "unpublished.txt"], case.work)
                _git(["commit", "-m", "unpublished parent work"], case.work)
        elif blocker == "remote_moved":
            _git(["push", "origin", f"{case.base}:refs/heads/aq/epic", "--force"], case.work)
    stages = await _rows(case.db, integration_repair_stages)
    receipts = await _rows(case.db, task_delivery_receipts)
    owner = await _owner(case)
    operation = await _operation(case)
    recovery = _recovery(case)
    for apply in (False, True):
        refused = await recovery.run(
            "epic",
            dry_run=not apply,
            expected_head_sha=conflict["expected_target"],
            reason="Must not bypass evidence",
            operator_id="supervisor:p",
        )
        assert refused["outcome"] in {"blocked", "ambiguous", "nothing_to_reopen"}, refused
    assert await _rows(case.db, integration_repair_stages) == stages
    assert await _rows(case.db, task_delivery_receipts) == receipts
    assert await _owner(case) == owner
    assert await _operation(case) == operation


async def test_no_progress_collection_rechecks_remote_under_apply_lock(case, monkeypatch):
    conflict = await _no_progress_collection(case)
    stages = await _rows(case.db, integration_repair_stages)
    owner = await _owner(case)
    recovery = _recovery(case)
    original_proof = recovery._prove
    calls = 0

    async def moved_remote(facts):
        nonlocal calls
        calls += 1
        if calls == 2:
            _git(["push", "origin", f"{case.base}:refs/heads/aq/epic", "--force"], case.work)
        return await original_proof(facts)

    monkeypatch.setattr(recovery, "_prove", moved_remote)
    result = await recovery.run(
        "epic",
        dry_run=False,
        expected_head_sha=conflict["expected_target"],
        reason="A concurrent remote change must refuse",
    )
    assert result["outcome"] == "changed", result
    assert await _owner(case) == owner
    assert await _rows(case.db, integration_repair_stages) == stages


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
