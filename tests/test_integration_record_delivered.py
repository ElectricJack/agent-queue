"""Exact externally delivered completion without a daemon scheduler."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.commands.contracts.integration import IntegrationRecordDeliveredArgs
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import tasks
from src.git.manager import GitError
from src.integration.delivered_close import DeliveredClose
from src.integration.provenance import CompletionIdentity, GitProvenance
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.models import Task, TaskStatus
from src.profiles.capabilities import DENY_ALL
from tests.test_delivery_consumers import git
from tests.test_integration_train_sources import completed, world as fixture_world

world = fixture_world


async def source(world, *, landed=True, origin=True):
    base = git(world.origin.clone, "rev-parse", "origin/main")
    if origin:
        head = await completed(world, "external", done=False, source_base=base)
    else:
        await world.db.create_task(
            Task(
                id="external",
                project_id="p",
                repo_id="r",
                title="external",
                description="",
                branch_name="aq/external",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        head = world.origin.work("external")
    if landed:
        world.origin.land("external")
    await world.db.transition_task("external", TaskStatus.BLOCKED)
    return IntegrationRecordDeliveredArgs(
        task_id="external",
        project_id="p",
        source_sha=head,
        base_sha=base,
        reason="Reviewed work delivered directly to the designated default branch",
        tests=["focused check passed"],
        commands=["area check passed"],
        dry_run=False,
    )


@pytest.mark.parametrize("origin", [True, False])
async def test_record_delivered_retains_passing_exact_source_and_is_idempotent(world, origin):
    request = await source(world, origin=origin)
    control = DeliveredClose(world.db)
    preview = await control.record(request.model_copy(update={"dry_run": True}))
    assert preview["outcome"] == "preview"
    assert (await world.db.get_task("external")).status is TaskStatus.BLOCKED
    assert await world.db.get_task_completion("external") is None
    result = await control.record(request)
    assert result["outcome"] == "recorded"
    assert await control.record(request) == result
    completion = await world.db.get_task_completion("external")
    assert completion.outcome == "pass" and completion.work_outcome == "shipped"
    assert completion.commits == [request.source_sha]
    assert completion.tests == request.tests and completion.commands == request.commands
    assert len(await world.db.get_task_completions("external")) == 1
    assert (await world.db.get_task("external")).status is TaskStatus.COMPLETED
    proof = GitProvenance(world.truth.git, str(world.origin.clone), repository_url=world.origin.url)
    await proof.git.afetch_origin(
        str(world.origin.clone), repository_url=world.origin.url, all_heads=True
    )
    retained = await proof.read_completion(CompletionIdentity("p", "r", "external", completion.id))
    assert retained["source_oid"] == request.source_sha and retained["artifact"] is True
    recorded = await world.db.get_task_branch_origin_for_promotion("external", "r")
    assert recorded["base_sha"] == request.base_sha and recorded["materialized"]


@pytest.mark.parametrize(
    "guard", ["unlanded", "foreign_project", "wrong_base", "claim", "child", "pause", "deliverable"]
)
async def test_record_delivered_refuses_unproved_or_held_work_without_completion(world, guard):
    request = await source(world, landed=guard != "unlanded")
    expected = {
        "unlanded": "not contained",
        "foreign_project": "explicitly selected project",
        "wrong_base": "origin does not match",
        "claim": "workspace or claim",
        "child": "open children",
        "pause": "operator pause",
        "deliverable": "required deliverables",
    }[guard]
    if guard == "foreign_project":
        request = request.model_copy(update={"project_id": "foreign"})
    elif guard == "wrong_base":
        request = request.model_copy(update={"base_sha": request.source_sha})
    elif guard == "claim":
        await world.db.set_task_meta("external", "claimed_by_session", "stopped-session")
    elif guard == "child":
        await completed(world, "child", parent="external", done=False)
    elif guard == "pause":
        await world.db.set_task_meta("external", "manual_pause", {"reason": "explicit hold"})
    elif guard == "deliverable":
        async with world.db._engine.begin() as conn:
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == "external")
                .values(
                    deliverables=json.dumps(
                        [{"required": True, "kind": "file", "target": "proof"}]
                    ),
                )
            )
    with pytest.raises(ValueError, match=expected):
        await DeliveredClose(world.db).record(request)
    assert (await world.db.get_task("external")).status is TaskStatus.BLOCKED
    assert await world.db.get_task_completion("external") is None


async def test_record_delivered_rolls_back_when_retention_publication_fails(world, monkeypatch):
    request = await source(world, origin=False)
    monkeypatch.setattr(
        GitProvenance, "write_completion", AsyncMock(side_effect=GitError("push refused"))
    )
    with pytest.raises(GitError, match="push refused"):
        await DeliveredClose(world.db).record(request)
    assert (await world.db.get_task("external")).status is TaskStatus.BLOCKED
    assert await world.db.get_task_completion("external") is None
    assert await world.db.get_task_branch_origin_for_promotion("external", "r") is None


@pytest.mark.parametrize(
    "kind", [PrincipalKind.SESSION, PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK]
)
async def test_record_delivered_refuses_every_nonlocal_principal(world, kind):
    request = await source(world)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    with principal_context(ExecutionPrincipal(kind=kind, policy=DENY_ALL, elevated=True)):
        result = await handler._cmd_integration_record_delivered(request.model_dump())
    assert result["success"] is False and "local operator" in result["error"]
    assert await world.db.get_task_completion("external") is None


async def test_record_delivered_default_movement_rolls_back_completion(world, monkeypatch):
    request = await source(world)
    original = GitProvenance.write_completion

    async def moved(proof, *args, **kwargs):
        result = await original(proof, *args, **kwargs)
        # An external main push is independent of our branch ownership lock.
        world.origin.work("other")
        world.origin.land("other")
        return result

    monkeypatch.setattr(GitProvenance, "write_completion", moved)
    with pytest.raises(ValueError, match="default branch changed"):
        await DeliveredClose(world.db).record(request)
    assert await world.db.get_task_completion("external") is None
    assert (await world.db.get_task("external")).status is TaskStatus.BLOCKED


async def test_record_delivered_atomically_releases_only_its_detached_reservation(world):
    request = await source(world)
    target = BranchKey(repository_id="r", branch="aq/external")
    ownership = BranchOwnership(world.db)
    fence = await ownership.acquire(target, "external", "worker")
    preview = await DeliveredClose(world.db).record(request.model_copy(update={"dry_run": True}))
    assert preview["outcome"] == "preview"
    assert (await ownership.get_owner(target))["handoff_state"] == "reserved"
    await DeliveredClose(world.db).record(request)
    owner = await ownership.get_owner(target)
    assert owner["handoff_state"] == "released" and owner["fence_token"] == fence.token + 1


async def test_record_delivered_refuses_another_tasks_reservation(world):
    request = await source(world)
    ownership = BranchOwnership(world.db)
    target = BranchKey(repository_id="r", branch="aq/external")
    await ownership.acquire(target, "another-task", "worker")
    with pytest.raises(ValueError, match="branch ownership"):
        await DeliveredClose(world.db).record(request)
    assert (await ownership.get_owner(target))["handoff_state"] == "reserved"
    assert await world.db.get_task_completion("external") is None


async def idle_train(world):
    from sqlalchemy import insert
    from src.database import tables as t
    from src.integration.subjects import PolicyArtifactPin, Subject, SubjectSchedule
    from src.commands.contracts.integration import IntegrationQuiesceArgs

    sha = "sha256:" + "1" * 64
    subject = Subject(
        id="idle-root",
        project_id="p",
        repository_id="r",
        kind="root_batch",
        subject_key="root_batch:r:integration-sweep:p:1",
        phase="admitting",
        policy=PolicyArtifactPin(playbook_id="root-test", artifact_sha256=sha),
        target_ref="refs/heads/main",
        schedule=SubjectSchedule.progress(now=1, max_wait_seconds=3600),
        created_at=1,
        updated_at=1,
    )
    async with world.db.immediate() as conn:
        await conn.execute(
            insert(t.playbook_artifacts).values(
                artifact_sha256=sha,
                playbook_id="root-test",
                source_digest="sha256:" + "c" * 64,
                contract_fingerprint="sha256:" + "d" * 64,
                compiler_build="test",
                path="/artifact",
                created_at=1,
            )
        )
        await world.db.ensure_integration_subject_on(conn, subject.to_row())
        await world.db.lock_integration_schedule_on(
            conn, project_id="p", now=1, default_interval_seconds=300
        )
        await conn.execute(
            update(t.project_integration_schedules)
            .where(
                t.project_integration_schedules.c.project_id == "p",
            )
            .values(
                enabled=True,
                request_sequence=1,
                outstanding_request_id="integration-sweep:p:1",
                outstanding_trigger="periodic",
                outstanding_requested_at=1,
            )
        )
    project = await world.db.get_project("p")
    return IntegrationQuiesceArgs(
        project_id="p",
        expected_generation=project.hierarchical_integration_generation,
        subject_id=subject.id,
        expected_version=0,
        reason="Operator switches to development after external delivery",
        dry_run=False,
    )


async def test_local_quiesce_closes_unfrozen_root_and_paused_reservation_without_changing_task(
    world,
):
    from src.integration.quiesce import TrainQuiesce
    from src.commands.contracts.integration import IntegrationQuiesceOwner

    request = await idle_train(world)
    await source(world)
    await world.db.transition_task("external", TaskStatus.PAUSED)
    target = BranchKey(repository_id="r", branch="aq/external")
    ownership = BranchOwnership(world.db)
    fence = await ownership.acquire(target, "external", "worker")
    owner = await ownership.get_owner(target)
    request = request.model_copy(
        update={
            "owners": [IntegrationQuiesceOwner(owner_row_id=owner["id"], fence_token=fence.token)]
        }
    )
    control = TrainQuiesce(world.db)
    assert (await control.run(request.model_copy(update={"dry_run": True})))["outcome"] == "preview"
    assert (await ownership.get_owner(target))["handoff_state"] == "reserved"
    result = await control.run(request)
    assert result["outcome"] == "quiesced"
    assert (await ownership.get_owner(target))["handoff_state"] == "released"
    assert (await world.db.get_integration_subject("idle-root"))["phase"] == "done"
    assert (await world.db.get_task("external")).status is TaskStatus.PAUSED
    assert await world.db.get_task_completion("external") is None
    from src.database import tables as t

    async with world.db._engine.connect() as conn:
        schedule = (
            (
                await conn.execute(
                    select(t.project_integration_schedules).where(
                        t.project_integration_schedules.c.project_id == "p"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert schedule["outstanding_request_id"] is None and schedule["enabled"] is False


@pytest.mark.parametrize(
    "guard", ["version", "generation", "head", "batch", "claim", "task_active", "fence"]
)
async def test_quiesce_refuses_active_or_changed_evidence_without_writes(world, guard):
    from src.database import tables as t
    from src.integration.quiesce import TrainQuiesce
    from src.commands.contracts.integration import IntegrationQuiesceOwner

    request = await idle_train(world)
    if guard == "version":
        request = request.model_copy(update={"expected_version": 7})
    elif guard == "generation":
        request = request.model_copy(update={"expected_generation": 7})
    elif guard == "head":
        async with world.db._engine.begin() as conn:
            await conn.execute(
                update(t.integration_subjects)
                .where(t.integration_subjects.c.id == "idle-root")
                .values(head_sha="a" * 40)
            )
    elif guard == "batch":
        from src.integration.batches import Batch, BatchMember, BatchStore

        request_source = await source(world)
        await BatchStore(world.db).freeze(
            Batch("live", "p", "r", "refs/heads/main"),
            (BatchMember("external", request_source.source_sha, request_source.base_sha),),
            trees={
                "external": git(
                    world.origin.clone, "rev-parse", request_source.source_sha + "^{tree}"
                )
            },
        )
    elif guard == "claim":
        await source(world)
        await world.db.set_task_meta("external", "claimed_by_session", "held")
    else:
        await source(world)
        if guard == "fence":
            await world.db.transition_task("external", TaskStatus.PAUSED)
        target = BranchKey(repository_id="r", branch="aq/external")
        ownership = BranchOwnership(world.db)
        fence = await ownership.acquire(target, "external", "worker")
        owner = await ownership.get_owner(target)
        request = request.model_copy(
            update={
                "owners": [
                    IntegrationQuiesceOwner(
                        owner_row_id=owner["id"], fence_token=fence.token + (guard == "fence")
                    )
                ]
            }
        )
    with pytest.raises(ValueError):
        await TrainQuiesce(world.db).run(request)
    assert (await world.db.get_integration_subject("idle-root"))["phase"] == "admitting"
    from src.database import tables as t

    async with world.db._engine.connect() as conn:
        schedule = (
            (
                await conn.execute(
                    select(t.project_integration_schedules).where(
                        t.project_integration_schedules.c.project_id == "p"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert schedule["enabled"] and schedule["outstanding_request_id"] == "integration-sweep:p:1"


@pytest.mark.parametrize("state", ["completed", "cancelled", "active"])
async def test_quiesce_requires_ended_collector_operation_and_preserves_its_work(world, state):
    from sqlalchemy import insert
    from src.database import tables as t
    from src.integration.quiesce import TrainQuiesce
    from src.commands.contracts.integration import IntegrationQuiesceOwner

    request = await idle_train(world)
    await source(world)
    async with world.db._engine.begin() as conn:
        await conn.execute(
            insert(t.integration_parent_episodes).values(
                id="episode",
                parent_task_id="external",
                repository_id="r",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1,
            )
        )
        await conn.execute(
            insert(t.integration_repair_operations).values(
                id="collector",
                target_kind="parent",
                parent_task_id="external",
                episode_id="episode",
                state=state,
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                created_at=1,
                updated_at=1,
            )
        )
    target = BranchKey(repository_id="r", branch="aq/external")
    ownership = BranchOwnership(world.db)
    fence = await ownership.acquire(target, "collector", "collector")
    owner = await ownership.get_owner(target)
    request = request.model_copy(
        update={
            "owners": [IntegrationQuiesceOwner(owner_row_id=owner["id"], fence_token=fence.token)]
        }
    )
    if state == "active":
        with pytest.raises(ValueError):
            await TrainQuiesce(world.db).run(request)
        assert (await ownership.get_owner(target))["handoff_state"] == "reserved"
    else:
        assert (await TrainQuiesce(world.db).run(request))["outcome"] == "quiesced"
        assert (await ownership.get_owner(target))["handoff_state"] == "released"
        assert (await world.db.get_task("external")).status is TaskStatus.BLOCKED
        assert git(world.origin.clone, "rev-parse", "origin/aq/external")


@pytest.mark.parametrize(
    "kind", [PrincipalKind.SESSION, PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK]
)
async def test_quiesce_refuses_even_elevated_nonlocal_principal(world, kind):
    request = await idle_train(world)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    with principal_context(ExecutionPrincipal(kind=kind, policy=DENY_ALL, elevated=True)):
        result = await handler._cmd_integration_quiesce(request.model_dump())
    assert not result["success"] and "local operator" in result["error"]
    assert (await world.db.get_integration_subject("idle-root"))["phase"] == "admitting"


async def test_quiesce_does_not_treat_legacy_empty_batch_as_frozen_work(world):
    from sqlalchemy import insert
    from src.database import tables as t
    from src.integration.quiesce import TrainQuiesce

    request = await idle_train(world)
    async with world.db._engine.begin() as conn:
        await conn.execute(
            insert(t.integration_batches).values(
                id="historical-empty",
                project_id="p",
                repository_id="r",
                request_id="integration-sweep:p:0",
                trigger="periodic",
                lifecycle="empty",
                intent="open",
                cleanup_state="complete",
                created_at=0,
                updated_at=0,
                policy_snapshot={},
                artifact_snapshot={},
                source_manifest_digest="empty",
            )
        )
    assert (await TrainQuiesce(world.db).run(request))["outcome"] == "quiesced"


async def expired_mutation(world, *, landed=False):
    from sqlalchemy import insert
    from src.database import tables as t
    from src.commands.contracts.integration import IntegrationReconcileExpiredMutationArgs

    delivered = await source(world)
    head = delivered.source_sha if landed else delivered.base_sha
    git(world.origin.clone, "push", "-q", "origin", head + ":refs/heads/aq/attempt")
    fence = await BranchOwnership(world.db).acquire(
        BranchKey(repository_id="r", branch="aq/attempt"), "ended-operation", "collector"
    )
    async with world.db._engine.begin() as conn:
        await conn.execute(
            insert(t.integration_batches).values(
                id="ended-batch",
                project_id="p",
                repository_id="r",
                request_id="old-request",
                base_sha=delivered.base_sha,
                integration_branch="refs/heads/aq/attempt",
                lifecycle="aborted",
                intent="aborted",
                cleanup_state="complete",
                created_at=1,
                updated_at=1,
                source_manifest_digest="test",
                policy_snapshot={},
                artifact_snapshot={},
            )
        )
        await conn.execute(
            insert(t.integration_candidate_revisions).values(
                batch_id="ended-batch",
                revision=0,
                construction_base_sha=delivered.base_sha,
                state="superseded",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(t.integration_repair_operations).values(
                id="ended-operation",
                target_kind="batch",
                batch_id="ended-batch",
                episode_id="ended-batch",
                state="cancelled",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(t.integration_candidate_ref_mutations).values(
                id="expired-attempt",
                batch_id="ended-batch",
                revision=0,
                purpose="candidate_partial",
                repository_id="r",
                branch="refs/heads/aq/attempt",
                target_branch="refs/heads/aq/attempt",
                expected_old_sha=delivered.base_sha,
                desired_sha=delivered.source_sha,
                operation_id="ended-operation",
                operation_episode_id="ended-batch",
                operation_stage=0,
                lease_owner_id="old-sealer",
                lease_fence_token=1,
                branch_owner_id="ended-operation",
                branch_owner_role="collector",
                branch_fence_token=fence.token,
                nonce="old-nonce",
                state="reserved",
                expires_at=1,
                created_at=0,
                updated_at=0,
            )
        )
    return IntegrationReconcileExpiredMutationArgs(
        project_id="p",
        mutation_id="expired-attempt",
        expected_nonce="old-nonce",
        expected_branch_fence=fence.token,
        expected_lease_fence=1,
        reason="Stopped operator reconciles the abandoned attempt using actual Git",
        dry_run=False,
    )


@pytest.mark.parametrize("landed", [False, True])
async def test_expired_mutation_recovery_records_actual_remote_without_changing_git_or_task(
    world, landed
):
    from src.database import tables as t
    from src.integration.mutation_recovery import ExpiredMutationRecovery

    request = await expired_mutation(world, landed=landed)
    before = git(world.origin.clone, "ls-remote", "origin", "refs/heads/aq/attempt")
    control = ExpiredMutationRecovery(world.db)
    preview = await control.run(request.model_copy(update={"dry_run": True}))
    assert preview["outcome"] == "preview"
    result = await control.run(request)
    assert result["outcome"] == ("applied" if landed else "superseded")
    async with world.db._engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(t.integration_candidate_ref_mutations).where(
                        t.integration_candidate_ref_mutations.c.id == request.mutation_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["state"] == result["disposition"]
    assert row["remote_sha"] == (result["remote_sha"] if landed else None)
    assert (await world.db.get_task("external")).status is TaskStatus.BLOCKED
    assert git(world.origin.clone, "ls-remote", "origin", "refs/heads/aq/attempt") == before


@pytest.mark.parametrize(
    "guard",
    ["nonce", "fence", "operation", "expiry", "claim", "foreign", "remote_error", "remote_moves"],
)
async def test_expired_mutation_recovery_refuses_changed_or_live_evidence(
    world, guard, monkeypatch
):
    from src.database import tables as t
    from src.git.manager import GitManager, RemoteRefResult, RemoteRefState
    from src.integration.mutation_recovery import ExpiredMutationRecovery

    request = await expired_mutation(world)
    if guard == "nonce":
        request = request.model_copy(update={"expected_nonce": "wrong"})
    elif guard == "fence":
        request = request.model_copy(update={"expected_branch_fence": 7})
    elif guard == "foreign":
        request = request.model_copy(update={"project_id": "foreign"})
    elif guard == "claim":
        await world.db.set_task_meta("external", "claimed_by_session", "stopped")
    elif guard in ("operation", "expiry"):
        async with world.db._engine.begin() as conn:
            if guard == "operation":
                await conn.execute(
                    update(t.integration_repair_operations)
                    .where(t.integration_repair_operations.c.id == "ended-operation")
                    .values(state="active")
                )
            else:
                await conn.execute(
                    update(t.integration_candidate_ref_mutations)
                    .where(t.integration_candidate_ref_mutations.c.id == "expired-attempt")
                    .values(expires_at=1e12)
                )
    elif guard == "remote_error":
        monkeypatch.setattr(
            GitManager,
            "als_remote_ref",
            AsyncMock(return_value=RemoteRefResult(RemoteRefState.ERROR)),
        )
    else:
        original = GitManager.als_remote_ref
        calls = 0

        async def moving(git_manager, *args, **kwargs):
            nonlocal calls
            calls += 1
            result = await original(git_manager, *args, **kwargs)
            if calls == 1:
                head = git(world.origin.clone, "rev-parse", "origin/aq/external")
                git(world.origin.clone, "push", "-q", "origin", head + ":refs/heads/aq/attempt")
            return result

        monkeypatch.setattr(GitManager, "als_remote_ref", moving)
    with pytest.raises(ValueError):
        await ExpiredMutationRecovery(world.db).run(request)
    async with world.db._engine.connect() as conn:
        assert (
            await conn.scalar(
                select(t.integration_candidate_ref_mutations.c.state).where(
                    t.integration_candidate_ref_mutations.c.id == "expired-attempt"
                )
            )
            == "reserved"
        )


@pytest.mark.parametrize(
    "kind", [PrincipalKind.SESSION, PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK]
)
async def test_expired_mutation_recovery_requires_local_operator(world, kind):
    request = await expired_mutation(world)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    with principal_context(ExecutionPrincipal(kind=kind, policy=DENY_ALL, elevated=True)):
        result = await handler._cmd_integration_reconcile_expired_mutation(request.model_dump())
    assert not result["success"] and "local operator" in result["error"]
