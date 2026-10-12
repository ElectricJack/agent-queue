"""Subject writers: replay, expiry, unknown stops and real-Git preservation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select, update

from src.database.tables import (
    integration_branch_owners,
    integration_outbox,
    integration_subjects,
    playbook_artifacts,
    projects,
    tasks,
    workspaces,
)
from src.git.manager import GitManager
from src.integration.models import BranchKey, Fence
from src.integration.owner_recovery import OwnerRecovery
from src.integration.ownership import BranchOwnership, StaleFence
from src.integration.runtime_contracts import (
    PolicyArtifactPin,
    Primitive,
    PrimitivePorts,
    Subject,
    SubjectEngine,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    WriterFileArgs,
    WriterLeaseArgs,
    WriterRole,
    WriterStatus,
    WriterStopProofArgs,
)
from src.integration.writers import WriterPrimitives
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.test_integration_owner_recovery import Env, git, remote_sha

ARTIFACT = "sha256:" + "1" * 64


@pytest.fixture
async def env(tmp_path, reuse_database):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    base = tmp_path / "base"
    git(tmp_path, "clone", str(origin), str(base))
    git(base, "config", "user.name", "Tester")
    git(base, "config", "user.email", "tester@example.test")
    (base / "base.txt").write_text("base\n")
    git(base, "add", ".")
    git(base, "commit", "-m", "base")
    git(base, "push", "origin", "main")
    db = await reuse_database("subject-writers")
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(
        RepoConfig(
            id="r",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(origin),
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                integration_repository_id="r",
                hierarchical_integration_mode="development",
                hierarchical_integration_desired_mode="development",
            )
        )
        await conn.execute(
            insert(workspaces).values(
                id="ws-base",
                project_id="p",
                workspace_path=str(base),
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=ARTIFACT,
                playbook_id="policy",
                source_digest="sha256:" + "c" * 64,
                contract_fingerprint="sha256:" + "d" * 64,
                compiler_build="test",
                path="/artifacts/policy.json",
                created_at=1.0,
            )
        )
    result = Env(db=db, origin=origin, base=base, tmp=tmp_path)
    result.now = 1000.0
    result.probe = AsyncMock(return_value=True)
    result.recovery = OwnerRecovery(
        db, GitManager(), None, confirm_stopped=result.probe, clock=lambda: result.now
    )
    result.writers = WriterPrimitives(db, owner_recovery=result.recovery, clock=lambda: result.now)
    yield result


async def make_subject(env, *, kind=SubjectKind.ROOT_BATCH, subject_id="s", branch="candidate"):
    if kind is not SubjectKind.ROOT_BATCH:
        await env.db.create_task(Task(id="source", project_id="p", title="Source", description=""))
    subject = Subject(
        id=subject_id,
        project_id="p",
        repository_id="r",
        kind=kind,
        subject_key=f"{kind}:{subject_id}",
        task_id=None if kind is SubjectKind.ROOT_BATCH else "source",
        engine=SubjectEngine.RECONCILER,
        phase=SubjectPhase.REPAIRING,
        policy=PolicyArtifactPin(playbook_id="policy", artifact_sha256=ARTIFACT),
        target_ref=f"refs/heads/{branch}",
        head_sha=git(env.base, "rev-parse", "main"),
        generation=2,
        schedule=SubjectSchedule.progress(now=env.now, max_wait_seconds=3600),
        created_at=env.now,
        updated_at=env.now,
    )
    await env.db.ensure_integration_subject(subject.to_row())
    return subject


async def current(env, subject):
    return Subject.from_row(await env.db.get_integration_subject(subject.id))


def filing(*, role=WriterRole.REPAIR, ordinal=0, **kwargs):
    return WriterFileArgs(
        role=role,
        ordinal=ordinal,
        intelligence_class="standard-high",
        budget_seconds=300,
        attempt_limit=2,
        brief="Fix the exact subject.",
        **kwargs,
    )


def leasing(subject, **kwargs):
    return WriterLeaseArgs(
        ref=subject.target_ref, owner_task_id=subject.writer.task_id, ttl_seconds=60, **kwargs
    )


async def file_and_lease(env, subject):
    assert (await env.writers.file(subject, filing())).outcome == "filed"
    subject = await current(env, subject)
    result = await env.writers.lease(subject, leasing(subject))
    assert result.outcome == "leased"
    return await current(env, subject), Fence.model_validate(result.detail["fence"])


async def test_managed_writer_expiry_allows_successor_without_stop_or_workspace_proof(env):
    from src.integration.lock import BranchLock

    env.writers.managed_leases = True
    subject, fence = await file_and_lease(env, await make_subject(env))
    async with env.db.immediate() as conn:
        # Stranded compatibility bindings are not managed eligibility proofs.
        await conn.execute(
            update(integration_branch_owners).values(
                handoff_state="handoff_pending", workspace_id="stranded", session_id="dead"
            )
        )
    env.now += 61
    assert (await env.writers.lease(subject, leasing(subject))).outcome == "stale"
    successor = await BranchLock(env.db, clock=lambda: env.now).acquire(
        fence.target, "successor", role="repair"
    )
    assert successor.token > fence.token
    with pytest.raises(StaleFence):
        await env.writers.ownership.assert_current(fence)
    env.probe.assert_not_awaited()


async def test_managed_writer_refuses_legacy_stop_saga_and_releases_by_identity(env):
    from src.integration.lock import BranchLock

    env.writers.managed_leases = True
    subject, fence = await file_and_lease(env, await make_subject(env))
    result = await env.writers.stop_proof(
        subject, WriterStopProofArgs(task_id=fence.owner_id, fence_token=fence.token)
    )
    assert result.outcome == "unknown"
    assert result.reason == "managed_ref_uses_lease_release"
    env.probe.assert_not_awaited()
    lock = BranchLock(env.db, clock=lambda: env.now)
    assert await lock.release(fence)
    successor = await lock.acquire(fence.target, "successor")
    assert not await lock.release(fence)
    assert (await lock.get(fence.target)).grant() == successor


@pytest.mark.parametrize(
    "kind,role",
    [
        (SubjectKind.ROOT_BATCH, WriterRole.REPAIR),
        (SubjectKind.PARENT_EPISODE, WriterRole.VERIFIER),
        (SubjectKind.SOURCE, WriterRole.SOURCE_REPAIR),
        (SubjectKind.SOURCE, WriterRole.REPAIR),  # development repair uses the same path
    ],
)
async def test_concurrent_filing_and_restart_keep_one_task_and_budget(env, kind, role):
    subject = await make_subject(env, kind=kind)
    args = filing(role=role)
    results = await asyncio.gather(*(env.writers.file(subject, args) for _ in range(6)))
    assert sorted(result.outcome for result in results) == ["exists"] * 5 + ["filed"]
    task_ids = {result.detail["task_id"] for result in results}
    assert len(task_ids) == 1
    subject = await current(env, subject)
    budget = subject.budget
    env.now += 200
    restarted = WriterPrimitives(env.db, clock=lambda: env.now)
    assert (await restarted.file(subject, args)).outcome == "exists"
    subject = await current(env, subject)
    assert subject.budget == budget and subject.budget.deadline_at == 1300
    async with env.db._engine.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(tasks)
                .where(
                    tasks.c.created_by_kind == "integration_writer",
                )
            )
            == 1
        )
    task = await env.db.get_task(subject.writer.task_id)
    assert task.status is TaskStatus.BLOCKED
    assert task.profile_id is None and task.class_hint == "standard-high"
    assert task.branch_name == "candidate" and task.repo_id == "r"
    # The class is a routing hint; retries cannot change it or refresh a clock.
    changed = args.model_copy(update={"budget_seconds": 900})
    assert (await restarted.file(subject, changed)).reason == "ordinal_request_changed"


async def test_filing_rolls_back_task_budget_and_journal_together(env, monkeypatch):
    subject = await make_subject(env)
    append = env.db.append_integration_subject_journal_on
    monkeypatch.setattr(
        env.db,
        "append_integration_subject_journal_on",
        AsyncMock(side_effect=RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        await env.writers.file(subject, filing())
    assert (await current(env, subject)).writer.status is WriterStatus.NONE
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(tasks)) == 0
    monkeypatch.setattr(env.db, "append_integration_subject_journal_on", append)
    assert (await env.writers.file(subject, filing())).outcome == "filed"


async def test_lease_expires_without_clock_reset_or_release(env):
    subject = await make_subject(env)
    subject, fence = await file_and_lease(env, subject)
    assert (await env.db.get_task(subject.writer.task_id)).status is TaskStatus.READY
    async with env.db._engine.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(integration_outbox)
                .where(
                    integration_outbox.c.event_type == "task.created",
                )
            )
            == 1
        )
    await env.writers.ownership.assert_current(fence)
    async with env.writers.ownership.mutation_exclusion(fence):
        pass
    env.now += 59
    replay = await env.writers.lease(subject, leasing(subject))
    assert replay.detail["expires_at"] == 1060
    env.now += 1
    with pytest.raises(StaleFence, match="unexpired"):
        await env.writers.ownership.assert_current(fence)
    with pytest.raises(StaleFence, match="unexpired"):
        async with env.writers.ownership.mutation_exclusion(fence):
            pytest.fail("expired writer entered a publish section")
    assert (await env.writers.lease(subject, leasing(subject))).outcome == "stale"
    other = await make_subject(env, subject_id="other")
    await env.writers.file(other, filing())
    other = await current(env, other)
    busy = await env.writers.lease(other, leasing(other))
    assert busy.outcome == "busy" and busy.detail["holder"] == fence.owner_id
    assert (await env.writers.file(subject, filing(ordinal=1))).outcome == "configuration_blocked"


async def test_lease_is_capped_by_budget_and_attempt_exhaustion(env):
    subject = await make_subject(env)
    await env.writers.file(subject, filing())
    subject = await current(env, subject)
    result = await env.writers.lease(
        subject, leasing(subject).model_copy(update={"ttl_seconds": 9999})
    )
    assert result.detail["expires_at"] == subject.budget.deadline_at
    subject = await current(env, subject)
    async with env.db.immediate() as conn:
        await conn.execute(
            update(integration_subjects)
            .where(integration_subjects.c.id == subject.id)
            .values(budget_attempts=2)
        )
    assert (
        await env.writers.lease(await current(env, subject), leasing(subject))
    ).outcome == "stale"


async def test_detached_parent_reservation_transfers_with_new_fence(env):
    subject = await make_subject(env, kind=SubjectKind.PARENT_EPISODE)
    original = await BranchOwnership(env.db).acquire(
        BranchKey(repository_id="r", branch="candidate"),
        "source",
        "worker",
    )
    await env.writers.file(subject, filing(role=WriterRole.VERIFIER))
    subject = await current(env, subject)
    result = await env.writers.lease(subject, leasing(subject))
    assert result.outcome == "leased"
    fence = Fence.model_validate(result.detail["fence"])
    assert fence.token > original.token
    with pytest.raises(StaleFence):
        await env.writers.ownership.assert_current(original)


async def attached(env, *, state="stopped", dirty=False, extra=0):
    tip = env.branch("candidate", extra=extra)
    subject, fence = await file_and_lease(env, await make_subject(env))
    slot = await env.slot("slot", "candidate", locked_by_task_id=fence.owner_id)
    await env.session(
        "session", work_dir=slot, task_id=fence.owner_id, state=state, desired_state=state
    )
    await env.writers.ownership.attach(fence, "session", "ws-slot")
    if dirty:
        (slot / "uncommitted.txt").write_text("must survive\n")
    return subject, fence, slot, tip


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("running", "live"),
        ("provider_live", "live"),
        ("provider_error", "unknown"),
        ("provider_missing", "unknown"),
        ("session_missing", "unknown"),
        ("stale_fence", "unknown"),
        ("workspace_live", "live"),
    ],
)
async def test_live_and_unknown_stop_never_release(env, failure, expected):
    subject, fence, slot, tip = await attached(
        env, state="running" if failure == "running" else "stopped"
    )
    args = WriterStopProofArgs(task_id=fence.owner_id, fence_token=fence.token)
    if failure == "provider_live":
        env.probe.return_value = False
    elif failure == "provider_error":
        env.probe.side_effect = RuntimeError("probe unavailable")
    elif failure == "provider_missing":
        env.recovery.confirm_stopped = None
    elif failure == "session_missing":
        async with env.db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).values(session_id="missing"))
    elif failure == "stale_fence":
        args = args.model_copy(update={"fence_token": fence.token + 99})
    elif failure == "workspace_live":
        await env.session("other-live", work_dir=slot, state="running", desired_state="running")
    env.now += 100  # expiry is never a substitute for stop proof
    result = await env.writers.stop_proof(subject, args)
    assert result.outcome == expected
    owner = await BranchOwnership(env.db).get_owner(fence.target)
    assert owner["handoff_state"] == "attached" and owner["fence_token"] == fence.token
    assert remote_sha(env.origin, "candidate") == tip
    assert (await current(env, subject)).writer.status is WriterStatus.FILED


@pytest.mark.parametrize("dirty,extra", [(True, 0), (False, 1), (False, 0)])
async def test_stop_preserves_unpublished_work_and_releases_once(env, dirty, extra):
    subject, fence, slot, tip = await attached(env, dirty=dirty, extra=extra)
    args = WriterStopProofArgs(task_id=fence.owner_id, fence_token=fence.token)
    result = await env.writers.stop_proof(subject, args)
    assert result.outcome == ("preserved_and_released" if dirty or extra else "released")
    if dirty or extra:
        ref = result.detail["preserved_ref"]
        held = remote_sha(env.origin, ref)
        assert held
        assert git(env.origin, "merge-base", "--is-ancestor", tip, held) == ""
        if dirty:
            assert git(env.origin, "show", f"{held}:uncommitted.txt") == "must survive"
            assert (slot / "uncommitted.txt").read_text() == "must survive\n"
    owner = await BranchOwnership(env.db).get_owner(fence.target)
    assert owner["handoff_state"] == "released" and owner["fence_token"] == fence.token + 1
    assert (await current(env, subject)).writer.status is WriterStatus.STOPPED
    assert (await env.db.get_task(fence.owner_id)).status is TaskStatus.BLOCKED
    assert (
        await env.db.get_task_meta(fence.owner_id, "blocked_terminal")
        == "integration_repair_retained_handoff"
    )
    replay = await env.writers.stop_proof(subject, args)  # original observation is replay-safe
    assert replay.outcome == result.outcome and replay.detail == result.detail
    assert env.probe.await_count == 1
    async with env.db._engine.connect() as conn:
        workspace = await conn.execute(select(workspaces).where(workspaces.c.id == "ws-slot"))
        workspace = workspace.mappings().one()
        assert workspace["locked_by_task_id"] is None
        if dirty:
            assert not workspace["enabled"]
    with pytest.raises(StaleFence):
        await env.writers.ownership.assert_current(fence)
    next_subject = await current(env, subject)
    await env.writers.file(next_subject, filing(ordinal=1))
    next_subject = await current(env, subject)
    successor = await env.writers.lease(next_subject, leasing(next_subject))
    assert successor.outcome == "leased"
    assert successor.detail["fence"]["token"] > fence.token


async def test_replay_after_recovery_committed_before_subject_write(env, monkeypatch):
    subject, fence, slot, tip = await attached(env, dirty=True)
    update_subject = env.writers._update_on
    monkeypatch.setattr(env.writers, "_update_on", AsyncMock(side_effect=RuntimeError("crash")))
    args = WriterStopProofArgs(task_id=fence.owner_id, fence_token=fence.token)
    with pytest.raises(RuntimeError, match="crash"):
        await env.writers.stop_proof(subject, args)
    assert (await current(env, subject)).writer.status is WriterStatus.FILED
    monkeypatch.setattr(env.writers, "_update_on", update_subject)
    result = await env.writers.stop_proof(subject, args)
    assert result.outcome == "preserved_and_released"
    assert remote_sha(env.origin, result.detail["preserved_ref"])
    assert env.probe.await_count == 1


async def test_preservation_failure_keeps_fence_workspace_and_local_work(env, monkeypatch):
    subject, fence, slot, tip = await attached(env, dirty=True)
    from src.git.manager import GitError

    monkeypatch.setattr(
        env.recovery.git,
        "apush_validated_ref",
        AsyncMock(side_effect=GitError("origin unavailable")),
    )
    result = await env.writers.stop_proof(subject, WriterStopProofArgs(task_id=fence.owner_id))
    assert result.outcome == "unknown" and result.reason == "origin_unreachable"
    assert (await BranchOwnership(env.db).get_owner(fence.target))["handoff_state"] == "attached"
    assert (slot / "uncommitted.txt").read_text() == "must survive\n"


async def test_ports_and_human_holds_do_not_mutate(env):
    subject = await make_subject(env)
    ports = PrimitivePorts()
    env.writers.bind(ports)
    assert ports.bound == {
        Primitive.WRITER_FILE,
        Primitive.WRITER_LEASE,
        Primitive.WRITER_STOP_PROOF,
    }
    async with env.db.immediate() as conn:
        await conn.execute(update(integration_subjects).values(gate_id="human-hold"))
    result = await ports.invoke(subject, filing())
    assert result.outcome == "configuration_blocked"
    assert (await current(env, subject)).writer.status is WriterStatus.NONE


async def test_ref_alias_cannot_bypass_a_foreign_owner(env):
    subject = await make_subject(env)
    await env.owner("foreign", "refs/heads/candidate", "another-task", state="reserved")
    await env.writers.file(subject, filing())
    subject = await current(env, subject)
    result = await env.writers.lease(subject, leasing(subject))
    assert result.outcome == "busy" and result.detail["holder"] == "another-task"


async def test_never_leased_writer_can_stop_without_releasing_domain_owner(env):
    subject = await make_subject(env, kind=SubjectKind.PARENT_EPISODE)
    domain = await BranchOwnership(env.db).acquire(
        BranchKey(repository_id="r", branch="candidate"),
        "source",
        "worker",
    )
    await env.writers.file(subject, filing())
    subject = await current(env, subject)
    original_budget = subject.budget
    env.now += 301
    result = await env.writers.stop_proof(
        subject, WriterStopProofArgs(task_id=subject.writer.task_id)
    )
    assert result.outcome == "released" and result.detail["fence_token"] is None
    assert (await current(env, subject)).budget == original_budget
    assert (await BranchOwnership(env.db).get_owner(domain.target))["owner_id"] == "source"
    assert (
        await env.writers.file(await current(env, subject), filing(ordinal=1))
    ).outcome == "filed"
    successor = await current(env, subject)
    assert (await env.writers.lease(successor, leasing(successor))).outcome == "leased"
    env.probe.assert_not_awaited()


@pytest.mark.parametrize(
    "change,expected", [("session", "live"), ("fence", "unknown"), ("human_hold", "unknown")]
)
async def test_changed_stop_identity_during_preservation_keeps_lease(
    env, monkeypatch, change, expected
):
    subject, fence, slot, tip = await attached(env, dirty=True)
    push = env.recovery.git.apush_validated_ref

    async def push_then_change(*args, **kwargs):
        await push(*args, **kwargs)
        async with env.db.immediate() as conn:
            if change == "session":
                from src.database.tables import sessions

                await conn.execute(
                    update(sessions)
                    .where(sessions.c.id == "session")
                    .values(
                        instance_token="new-instance",
                        state="running",
                        desired_state="running",
                    )
                )
            elif change == "fence":
                await conn.execute(
                    update(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.owner_id == fence.owner_id,
                    )
                    .values(fence_token=fence.token + 10)
                )
            else:
                await conn.execute(
                    update(integration_subjects)
                    .where(
                        integration_subjects.c.id == subject.id,
                    )
                    .values(gate_id="operator-hold")
                )

    monkeypatch.setattr(env.recovery.git, "apush_validated_ref", push_then_change)
    result = await env.writers.stop_proof(subject, WriterStopProofArgs(task_id=fence.owner_id))
    assert result.outcome == expected
    owner = await BranchOwnership(env.db).get_owner(fence.target)
    assert owner["handoff_state"] == "attached"
    assert remote_sha(env.origin, owner["ref"])
    async with env.db._engine.connect() as conn:
        assert (
            await conn.scalar(
                select(workspaces.c.locked_by_task_id).where(
                    workspaces.c.id == "ws-slot",
                )
            )
            == fence.owner_id
        )
    assert (slot / "uncommitted.txt").read_text() == "must survive\n"


async def test_manual_writer_pause_is_binding_before_lease(env):
    subject = await make_subject(env)
    await env.writers.file(subject, filing())
    subject = await current(env, subject)
    await env.db.pause_task(subject.writer.task_id)
    result = await env.writers.lease(subject, leasing(subject))
    assert result.outcome == "stale"
    assert (await env.db.get_task(subject.writer.task_id)).status is TaskStatus.PAUSED
    assert await env.db.get_task_meta(subject.writer.task_id, "manual_pause")
    assert (
        await BranchOwnership(env.db).get_owner(BranchKey(repository_id="r", branch="candidate"))
        is None
    )


async def test_inactive_project_does_not_file_a_writer(env):
    subject = await make_subject(env)
    async with env.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(status="PAUSED"))
    result = await env.writers.file(subject, filing())
    assert result.outcome == "configuration_blocked"
    assert (await current(env, subject)).writer.status is WriterStatus.NONE


async def test_stop_journal_identity_survives_a_target_ref_change(env):
    subject, fence, slot, tip = await attached(env)
    assert (
        await env.writers.stop_proof(subject, WriterStopProofArgs(task_id=fence.owner_id))
    ).outcome == "released"
    second_tip = env.branch("second")
    subject = await current(env, subject)
    async with env.db.immediate() as conn:
        row = await env.db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values={"target_ref": "refs/heads/second", "head_sha": second_tip},
            now=env.now,
        )
    subject = Subject.from_row(row)
    assert (await env.writers.file(subject, filing(ordinal=1))).outcome == "filed"
    subject = await current(env, subject)
    lease = await env.writers.lease(subject, leasing(subject))
    assert lease.outcome == "leased" and lease.detail["fence"]["token"] == fence.token
    subject = await current(env, subject)
    assert (
        await env.writers.stop_proof(subject, WriterStopProofArgs(task_id=subject.writer.task_id))
    ).outcome == "released"
    journal = await env.db.list_integration_subject_journal(subject.id)
    stops = [entry for entry in journal if entry["primitive"] == Primitive.WRITER_STOP_PROOF]
    assert len(stops) == 2
    assert len({entry["idempotency_key"] for entry in stops}) == 2
