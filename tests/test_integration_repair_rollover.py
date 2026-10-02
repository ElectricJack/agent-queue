"""Natural deadline/drain/restart handoff with real PostgreSQL and Git origins."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_revisions,
    integration_repair_stages,
    integration_review_evidence,
    playbook_artifacts,
    tasks,
)
from src.git.manager import GitError
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.integration.repair import RepairService
from src.integration.service import IntegrationService
from src.models import Agent, AgentState, TaskStatus
from src.orchestrator.core import Orchestrator
from tests import test_integration_owner_recovery as owner_tests
from tests.test_integration_owner_recovery import git, remote_sha
from tests.test_integration_repair import _artifact, _policy

env = owner_tests.env


async def _batch_writer(env, *, dirty=False, on_exhausted="continue"):
    db = env.db
    branch = "aq/integration/batch"
    partial = env.branch(branch)
    sources = [env.branch(f"source-{n}") for n in range(2)]
    artifact = _artifact()
    policy = _policy()
    policy["root"]["repair"].update(conflict_scope="batch", on_exhausted=on_exhausted)
    await db.update_project(
        "p", hierarchical_integration_mode="train", hierarchical_integration_policy=policy
    )
    async with db.immediate() as conn:
        await conn.execute(insert(playbook_artifacts).values(
            **artifact.model_dump(mode="json"), scope="project", scope_identifier="p",
            profile_fingerprint="", path="/tmp/artifact", size_bytes=1,
            validation="{}", created_at=1.0,
        ))
        await conn.execute(insert(integration_batches).values(
            id="batch", project_id="p", repository_id="r", request_id="request",
            source_manifest_digest="frozen-manifest", base_sha=partial,
            lifecycle="sealing", current_revision=0, integration_branch=branch,
            policy_snapshot=policy, artifact_snapshot=artifact.model_dump(mode="json"),
            cleanup_state="pending", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_candidate_revisions).values(
            batch_id="batch", revision=0, construction_base_sha=partial,
            head_sha=partial, state="constructing", created_at=1.0, updated_at=1.0,
        ))
        for n, source in enumerate(sources):
            tree = git(env.base, "rev-parse", f"{source}^{{tree}}")
            await conn.execute(insert(integration_review_evidence).values(
                id=f"review-{n}", source_task_id=f"source-{n}", repository_id="r",
                source_base=partial, reviewed_head_sha=source, reviewed_tree_sha=tree,
                review_kind="code", generation=0, verdict="approved",
                evidence={}, created_at=1.0,
            ))
            await conn.execute(insert(integration_batch_members).values(
                batch_id="batch", ordinal=n, task_id=f"source-{n}", repository_id="r",
                source_base_sha=partial, reviewed_head_sha=source, reviewed_tree_sha=tree,
                review_evidence_id=f"review-{n}", review_evidence={},
            ))
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "batch"
        ).values(lifecycle="repairing"))
    recovery = env.service(clock=lambda: 131.0)
    repair = RepairService(db, owner_recovery=recovery, clock=lambda: 131.0)
    async with db.immediate() as conn:
        operation = await repair.reserve_batch_operation_on(conn, "batch", now=100.0)
        await conn.execute(insert(integration_candidate_member_results).values(
            batch_id="batch", revision=0, member_ordinal=0, input_head_sha=partial,
            input_tree_sha=git(env.base, "rev-parse", f"{partial}^{{tree}}"),
            result="conflict", conflict_evidence={
                "operation_id": operation["id"], "batch_id": "batch", "revision": 0,
                "ordinal": 0, "partial_head_sha": partial,
                "source_base_sha": partial, "source_head_sha": sources[0],
            }, created_at=1.0, updated_at=1.0,
        ))
    target = BranchKey(repository_id="r", branch=branch)
    await BranchOwnership(db).acquire(target, operation["id"], "collector")
    assert (await repair.start(operation["id"], partial, "batch", now=100.0))["outcome"] == "started"
    primary = (await repair.dispatch(operation["id"], 0))["repair_task_id"]
    await db.create_agent(Agent(
        id="old-agent", name="Old writer", profile_id="worker",
        state=AgentState.BUSY, current_task_id=primary,
    ))
    slot = await env.slot(
        "old-slot", branch, locked_by_task_id=primary, locked_by_agent_id="old-agent"
    )
    git(slot, "merge", "--no-ff", "-m", "first frozen merge", sources[0])
    git(slot, "merge", "--no-ff", "-m", "second frozen merge", sources[1])
    head = git(slot, "rev-parse", "HEAD")
    if dirty:
        (slot / "base.txt").write_text("unfinished repair\n")
        (slot / "new.txt").write_text("untracked work\n")
    await env.session(
        "old-session", work_dir=slot, task_id=primary, agent_id="old-agent",
        state="draining", desired_state="stopped", claim_phase="active", last_claim_epoch=1,
    )
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == primary).values(
            status="IN_PROGRESS", assigned_agent_id="old-agent", claim_epoch=1
        ))
        await conn.execute(update(integration_branch_owners).where(
            integration_branch_owners.c.repository_id == "r",
            integration_branch_owners.c.ref == branch,
        ).values(handoff_state="attached", session_id="old-session", workspace_id="ws-slot"))
    owner = await BranchOwnership(db).get_owner(target)
    return SimpleNamespace(
        db=db, repair=repair, recovery=recovery, operation=operation["id"],
        primary=primary, slot=slot, head=head, partial=partial, sources=sources,
        target=target, owner=owner,
    )


async def _stage(case, ordinal):
    async with case.db._engine.connect() as conn:
        return dict((await conn.execute(select(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == case.operation,
            integration_repair_stages.c.ordinal == ordinal,
        ))).mappings().one())


@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("crash", ["none", "before_release", "after_release", "after_fence", "after_ready"])
async def test_deadline_rollover_preserves_and_resumes_exact_progress(env, monkeypatch, dirty, crash):
    case = await _batch_writer(env, dirty=dirty)
    repair = case.repair

    async def dispatch_due(_now):
        for row in await repair.pending_dispatches():
            await repair.dispatch(row["operation_id"], row["ordinal"])

    service = IntegrationService(
        case.db, SimpleNamespace(mark_due=AsyncMock()), repair,
        SimpleNamespace(dispatch_due=AsyncMock()),
        repair_dispatch_handler=dispatch_due,
        clock=lambda: 130.0,
    )
    # Natural persisted deadline pass and reservation retry, with no CI event.
    await service.tick(130.0)
    before = await _stage(case, 1)
    next_id = before["repair_task_id"]
    assert before["state"] == "active" and before["deadline_at"] == 190.0
    assert (await case.db.get_task(next_id)).status is TaskStatus.PAUSED
    assert (await BranchOwnership(case.db).get_owner(case.target))["owner_id"] == case.primary
    assert remote_sha(env.origin, f"aq/preserved/{case.owner['id']}") is None

    await case.db.update_session("old-session", state="stopped", desired_state="stopped")
    await case.db.update_task(case.primary, status=TaskStatus.BLOCKED)
    push = AsyncMock(wraps=case.recovery.git.apush_validated_ref)
    monkeypatch.setattr(case.recovery.git, "apush_validated_ref", push)
    if crash != "none":
        obj, method = {
            "before_release": (case.recovery, "_release"),
            "after_release": (case.recovery, "recover"),
            "after_fence": (repair._ownership, "acquire"),
            "after_ready": (case.db, "_notify_ready"),
        }[crash]
        original = getattr(obj, method)

        async def interrupted(*args, **kwargs):
            if crash != "before_release":
                await original(*args, **kwargs)
            raise RuntimeError("injected response loss")

        monkeypatch.setattr(obj, method, interrupted)
        with pytest.raises(RuntimeError, match="injected response loss"):
            await repair.dispatch(case.operation, 1)
        monkeypatch.setattr(obj, method, original)

    # Reconstruct the repair service as on daemon restart. The audit is the
    # progress source even when the old owner was already released/reassigned.
    restarted = RepairService(case.db, owner_recovery=case.recovery, clock=lambda: 132.0)
    result = await restarted.dispatch(case.operation, 1)
    assert result["outcome"] in {"dispatched", "already_dispatched"}
    after = await _stage(case, 1)
    progress = after["dossier"]["preserved_progress"]
    tip = progress["sha"]
    assert remote_sha(env.origin, progress["ref"]) == tip
    assert tip == case.head if not dirty else git(env.origin, "rev-parse", f"{tip}^") == case.head
    assert progress["completed_member_ordinals"] == [0, 1]
    assert progress["repair_commits"] == git(
        case.slot, "rev-list", "--first-parent", "--reverse", f"{case.partial}..{tip}"
    ).splitlines()
    assert after["starting_sha"] == tip
    for key in ("started_at", "deadline_at", "attempts", "policy", "current_subject", "deadline_event_id"):
        assert after[key] == before[key]
    assert (await case.db.get_task(next_id)).status is TaskStatus.READY
    description = (await case.db.get_task(next_id)).description
    assert tip in description and "Continue at the preserved tip" in description
    assert case.partial in description and "Candidate CI must pass" in description
    assert (await case.db.get_task(case.primary)).status is TaskStatus.BLOCKED
    assert (await case.db.get_session("old-session")).claim_phase is None
    assert push.await_count == 1
    owner = await BranchOwnership(case.db).get_owner(case.target)
    assert owner["owner_id"] == next_id and owner["handoff_state"] == "reserved"
    assert owner["fence_token"] == case.owner["fence_token"] + 2

    # The real origin/fence resolver and checkout-start path must choose the
    # preservation ref instead of the unchanged partial on canonical origin.
    orch = Orchestrator.__new__(Orchestrator)
    orch.db, orch.git = case.db, case.recovery.git
    task = await case.db.get_task(next_id)
    project = await case.db.get_project("p")
    origin, fence, role = await orch._hierarchy_origin_and_fence(task, project)
    assert role == "repair" and fence.token == owner["fence_token"]
    start = await orch._hierarchy_repair_start(
        str(env.base), origin, fence, repository_url=str(env.origin)
    )
    successor = env.tmp / "successor"
    git(env.base, "worktree", "add", "-b", "resume-check", str(successor), start)
    assert git(successor, "rev-parse", "HEAD") == tip
    if dirty:
        assert (successor / "base.txt").read_text() == "unfinished repair\n"
        assert (successor / "new.txt").read_text() == "untracked work\n"
        assert (await case.db.get_workspace("ws-slot")).enabled is False
    for _ in range(2):
        assert (await restarted.dispatch(case.operation, 1))["outcome"] == "already_dispatched"
        await restarted.reconcile_delegate_reservations(133.0)
    assert push.await_count == 1
    assert len([r for r in await env.audits(case.owner["id"])
                if r["outcome"] == "preserved_and_released"]) == 1
    assert await _stage(case, 1) == after
    assert remote_sha(env.origin, case.target.branch) == case.partial


@pytest.mark.parametrize("on_exhausted", ["human", "continue"])
async def test_service_retries_a_busy_successor_dispatch_without_any_event(env, on_exhausted):
    """The lost ``repair_exhausted`` run and a ``busy`` refusal both still advance.

    No playbook runs here: the deadline pass allocates the debug stage, the
    service's dispatch source finds it under either policy, the attached
    predecessor makes the first dispatch answer ``busy``, and the paced retry
    hands the stage to its delegate once the old writer has stopped.
    """
    import src.integration.service as service_module

    case = await _batch_writer(env, on_exhausted=on_exhausted)
    repair = case.repair
    outcomes = []

    async def dispatcher(row):
        result = await repair.dispatch(row["operation_id"], int(row["ordinal"]))
        outcomes.append(result["outcome"])
        return result

    now = [130.0]
    # Only the deadline and dispatch sources: the reservation reconciler would
    # also hand a stopped writer's branch over and hide the retry under test.
    service = IntegrationService(
        case.db, SimpleNamespace(mark_due=AsyncMock()),
        SimpleNamespace(expire=repair.expire, pending_dispatches=repair.pending_dispatches),
        SimpleNamespace(dispatch_due=AsyncMock()),
        repair_dispatcher=dispatcher, clock=lambda: now[0],
    )
    await service.tick(130.0)
    stage = await _stage(case, 1)
    assert stage["state"] == "active" and outcomes == ["busy"]
    assert (await case.db.get_task(stage["repair_task_id"])).status is TaskStatus.PAUSED

    await case.db.update_session("old-session", state="stopped", desired_state="stopped")
    await case.db.update_task(case.primary, status=TaskStatus.BLOCKED)
    now[0] = 131.0
    await service.tick(131.0)
    assert outcomes == ["busy"]  # paced: the retry is not due yet

    now[0] = 130.0 + service_module.DISPATCH_RETRY_BASE_SECONDS
    await service.tick(now[0])
    assert outcomes == ["busy", "dispatched"]
    assert (await case.db.get_task(stage["repair_task_id"])).status is TaskStatus.READY
    owner = await BranchOwnership(case.db).get_owner(case.target)
    assert owner["owner_id"] == stage["repair_task_id"] and owner["handoff_state"] == "reserved"

    now[0] += 1000.0
    await service.tick(now[0])
    assert outcomes == ["busy", "dispatched"]  # a launched writer leaves the selection


@pytest.mark.parametrize("corruption", ["manifest", "ref", "lineage"])
async def test_preserved_rollover_corruption_remains_an_explicit_blocker(env, corruption):
    case = await _batch_writer(env)
    await case.repair.expire(case.operation, 0, now=130.0)
    await case.db.update_session("old-session", state="stopped", desired_state="stopped")
    await case.db.update_task(case.primary, status=TaskStatus.BLOCKED)
    recovered = await case.recovery.recover(case.owner["id"], principal="sweep")
    assert recovered.outcome == "preserved_and_released"
    if corruption == "manifest":
        stage = await _stage(case, 1)
        dossier = dict(stage["dossier"])
        dossier["manifest"] = dict(dossier["manifest"]) | {"source_manifest_digest": "changed"}
        async with case.db.immediate() as conn:
            await conn.execute(update(integration_repair_stages).where(
                integration_repair_stages.c.operation_id == case.operation,
                integration_repair_stages.c.ordinal == 1,
            ).values(dossier=dossier))
    elif corruption == "ref":
        git(env.origin, "update-ref", f"refs/heads/{recovered.evidence['preserved_ref']}", case.partial)
    else:
        stage = await _stage(case, 1)
        subject = dict(stage["current_subject"]) | {"candidate_sha": case.sources[1]}
        async with case.db.immediate() as conn:
            await conn.execute(update(integration_repair_stages).where(
                integration_repair_stages.c.operation_id == case.operation,
                integration_repair_stages.c.ordinal == 1,
            ).values(current_subject=subject))
    result = await case.repair.dispatch(case.operation, 1)
    assert result["outcome"] == "human_required"
    stage = await _stage(case, 1)
    assert "preserved_progress_blocker" in stage["dossier"]
    assert (await case.db.get_task(stage["repair_task_id"])).status is TaskStatus.PAUSED
    assert stage["deadline_at"] == 190.0 and stage["attempts"] == 0


async def test_preserved_checkout_ref_movement_fails_without_resetting_work(env):
    case = await _batch_writer(env)
    await case.repair.expire(case.operation, 0, now=130.0)
    await case.db.update_session("old-session", state="stopped", desired_state="stopped")
    await case.db.update_task(case.primary, status=TaskStatus.BLOCKED)
    result = await case.repair.dispatch(case.operation, 1)
    orch = Orchestrator.__new__(Orchestrator)
    orch.db, orch.git = case.db, case.recovery.git
    origin, fence, _ = await orch._hierarchy_origin_and_fence(
        await case.db.get_task(result["repair_task_id"]), await case.db.get_project("p")
    )
    git(env.origin, "update-ref", f"refs/heads/{origin['preserved_progress']['ref']}", case.partial)
    with pytest.raises(GitError, match="preserved repair tip"):
        await orch._hierarchy_repair_start(str(env.base), origin, fence, repository_url=str(env.origin))
    assert git(case.slot, "rev-parse", "HEAD") == case.head


async def test_accepted_new_revision_supersedes_historical_preservation(env):
    """A later accepted candidate must not be rewound by the old recovery audit."""
    case = await _batch_writer(env)
    await case.repair.expire(case.operation, 0, now=130.0)
    await case.db.update_session("old-session", state="stopped", desired_state="stopped")
    await case.db.update_task(case.primary, status=TaskStatus.BLOCKED)
    first = await case.repair.dispatch(case.operation, 1)
    prior = await _stage(case, 1)
    # Model the constructor's durable accepted revision and normal subject bind.
    git(env.base, "push", "origin", f"{case.head}:refs/heads/{case.target.branch}")
    async with case.db.immediate() as conn:
        await conn.execute(insert(integration_candidate_revisions).values(
            batch_id="batch", revision=1, construction_base_sha=case.partial,
            head_sha=case.head, state="testing", created_at=150.0, updated_at=150.0,
        ))
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "batch"
        ).values(current_revision=1, lifecycle="testing"))
        await case.repair.bind_current_batch_subject_on(conn, case.operation, now=150.0)
    assert (await case.repair.expire(case.operation, 1, now=190.0))["stage"] == 2
    second = await case.repair.dispatch(case.operation, 2)
    assert second["outcome"] == "dispatched"
    assert first["repair_task_id"] != second["repair_task_id"]
    stage = await _stage(case, 2)
    assert stage["starting_sha"] == case.head
    assert "preserved_progress" not in stage["dossier"]
    assert stage["dossier"]["preserved_progress_history"] == [prior["dossier"]["preserved_progress"]]
    assert stage["deadline_at"] == 250.0 and stage["attempts"] == 0
    orch = Orchestrator.__new__(Orchestrator)
    orch.db, orch.git = case.db, case.recovery.git
    origin, fence, _ = await orch._hierarchy_origin_and_fence(
        await case.db.get_task(second["repair_task_id"]), await case.db.get_project("p")
    )
    assert origin["preserved_progress"] is None
    assert await orch._hierarchy_repair_start(
        str(env.base), origin, fence, repository_url=str(env.origin)
    ) == case.head
