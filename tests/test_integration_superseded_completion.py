"""Supersession proves one incident against disposable PostgreSQL and Git."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, text, update

from src.commands.contracts.integration import IntegrationSupersedeCompletionArgs
from src.commands.handler import CommandHandler
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig
from src.database import tables as t
from src.database.queries.hierarchy_queries import HierarchyError
from src.git.manager import GitManager
from src.integration.engine import lock_key
from src.integration.git_truth import GitTruth
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.runtime_contracts import PolicyArtifactPin, Subject, SubjectSchedule
from src.integration.superseded_completion import SupersededCompletion, SupersessionSettlement
from src.models import Project, RepoConfig, RepoSourceType, SessionRecord, Task, TaskCompletion
from src.models import TaskStatus, Workspace
from src.profiles.capabilities import DENY_ALL
from src.sessions.obsolete import ObsoleteClose, ObsoleteCloseRefused, ObsoleteOwnerRelease

ARTIFACT = "sha256:" + "1" * 64


@pytest.fixture
async def case(reuse_database, tmp_path):
    db = await reuse_database("superseded")
    git = GitManager()
    remote, checkout = tmp_path / "origin.git", tmp_path / "checkout"
    await git._arun(["init", "--bare", str(remote)], cwd=str(tmp_path))
    await git._arun(["init", "-b", "main", str(checkout)], cwd=str(tmp_path))
    async def run(*args):
        return await git._arun(list(args), cwd=str(checkout))
    for key, value in (("user.name", "Tester"), ("user.email", "test@example.test"),
                       ("commit.gpgsign", "false")):
        await run("config", key, value)
    await run("remote", "add", "origin", str(remote))
    (checkout / "seed").write_text("base")
    await run("add", ".")
    await run("commit", "-m", "base")
    base = await run("rev-parse", "HEAD")
    await run("push", "origin", "main")
    await run("checkout", "-b", "aq/root")
    (checkout / "work").write_text("retained passing work")
    await run("add", ".")
    await run("commit", "-m", "root work")
    head = await run("rev-parse", "HEAD")
    await run("push", "origin", "aq/root")
    provenance = GitProvenance(git, str(checkout), repository_url=str(remote))
    identity = CompletionIdentity("p", "r", "root", "close-1")
    await provenance.write_completion(CompletedSource(identity, head))
    truth = GitTruth(git)
    async def snapshot(target):
        return await truth.snapshot(str(checkout), project_id="p", repository_id="r",
                                    repository_url=str(remote), target_ref=target.target_ref)
    db._delivery_observer = SimpleNamespace(snapshot=snapshot, truth=truth)
    await db.create_project(Project(id="p", name="Project"))
    await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE,
                                   url=str(remote)))
    await db.create_task(Task(id="root", project_id="p", repo_id="r", title="root",
                              description="root", status=TaskStatus.PAUSED, branch_name="aq/root"))
    await db.save_task_completion(TaskCompletion(
        id="close-1", task_id="root", outcome="pass", work_outcome="changed",
        branch="aq/root", commits=[head], pr_url="https://example.test/pull/21", completed_at=2,
    ))
    async with db._engine.begin() as conn:
        await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(
            integration_repository_id="r", hierarchical_integration_mode="train"))
        await conn.execute(insert(t.playbook_artifacts).values(
            artifact_sha256=ARTIFACT, playbook_id="policy", source_digest=ARTIFACT,
            contract_fingerprint=ARTIFACT, compiler_build="test", path="/fixture", created_at=1))
        await conn.execute(insert(t.task_branch_origins).values(
            id="origin", task_id="root", repository_id="r", branch_name="aq/root",
            base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=1, materialized_at=1))
        await conn.execute(insert(t.integration_branch_owners).values(
            id="owner", repository_id="r", ref="refs/heads/aq/root", owner_id="root",
            owner_role="worker", fence_token=3, handoff_state="reserved", created_at=1,
            updated_at=1))
    subject = Subject(id="subject", project_id="p", repository_id="r", kind="source",
                      subject_key="source:root:close-1", engine="reconciler", phase="testing",
                      task_id="root", policy=PolicyArtifactPin(playbook_id="policy",
                          artifact_sha256=ARTIFACT), target_ref="refs/heads/aq/root",
                      head_sha=head, base_sha=base, generation=0,
                      schedule=SubjectSchedule.progress(now=1, max_wait_seconds=300).model_copy(
                          update={"refusal_streak": 4}),
                      created_at=1, updated_at=1)
    await db.ensure_integration_subject(subject.to_row())
    incident, _ = await db.create_escalation(
        id="incident", project_id="p", task_id="root", source_kind="task_attempt",
        source_identity="close-1", incident_key="root:close-1", supervisor_owner="supervisor-p",
        task_title="root", task_status="BLOCKED", summary="supersession", investigation="done",
        decision_requested="Disposition?", severity="high", now=1,
    )
    reply = await db.accept_escalation_reply(
        incident["id"], transport="dashboard", external_message_id="human-directive",
        verified_actor="human:local-operator", text="This work is superseded; close obsolete.",
        received_at=3,
    )
    request = IntegrationSupersedeCompletionArgs(
        project_id="p", task_id="root", subject_id="subject", expected_version=0,
        expected_claim_epoch=0, completion_id="close-1", expected_head_sha=head,
        human_reply_id=reply["reply"]["id"], reason="Human superseded this root",
    )
    closer = ObsoleteClose(db, release_owner=None, git_manager=None)
    service = SupersededCompletion(db, closer=closer, confirm_stopped=AsyncMock(return_value=True))
    yield SimpleNamespace(db=db, git=git, run=run, head=head, base=base, request=request,
                          service=service, closer=closer, snapshot=snapshot, identity=identity,
                          provenance=provenance, checkout=checkout)


async def apply(case, request=None):
    request = request or case.request
    preview = await case.service.run(request, principal="human:local-operator")
    applied = request.model_copy(update={"dry_run": False,
                                        "expected_proof_sha256": preview["proof_sha256"]})
    return applied, await case.service.run(applied, principal="human:local-operator")


async def rows(case, table):
    async with case.db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(table))).mappings()]


async def test_stale_merge_pipeline_and_obsolete_refuse_then_exact_supersession_preserves(case):
    handler = CommandHandler(SimpleNamespace(db=case.db, git=None), AppConfig())
    stale = await handler.execute("pr_merge", {"project_id": "p",
                                             "pr_url": "https://example.test/pull/21"})
    assert stale["outcome"] == "integration_managed" and not stale["success"]
    with pytest.raises(ObsoleteCloseRefused, match="still owns"):
        await case.closer.close("root", reason="superseded", principal="human:local-operator")
    history = await rows(case, t.task_completion_records)
    origins = await rows(case, t.task_branch_origins)
    preview = await handler.execute("integration_supersede_completion", case.request.model_dump())
    assert preview["success"] and preview["outcome"] == "preview"
    assert (await case.db.get_task("root")).status == TaskStatus.PAUSED
    request = case.request.model_copy(update={"dry_run": False,
                                             "expected_proof_sha256": preview["proof_sha256"]})
    result = await handler.execute("integration_supersede_completion", request.model_dump())
    assert result["success"] and result["outcome"] == "superseded"
    assert result["cleanup"]["state"] == "pending"  # no daemon release port in this fixture
    assert await rows(case, t.task_completion_records) == history
    assert await rows(case, t.task_branch_origins) == origins
    assert (await case.db.get_task("root")).status == TaskStatus.COMPLETED
    assert await case.db.get_task_meta("root", "work_outcome") == "abandoned"
    assert (await case.db.get_integration_subject("subject"))["phase"] == "done"
    assert (await case.db.get_integration_subject("subject"))["refusal_streak"] == 4
    assert await case.provenance.remote_completion(case.identity) is not None
    journal = await rows(case, t.integration_subject_journal)
    assert len(journal) == 1
    assert journal[0]["payload"]["proof"]["database"]["completion"] == history[0]
    assert journal[0]["payload"]["proof"]["database"]["reply"]["verified_actor"] == (
        "human:local-operator")
    replay = await handler.execute("integration_supersede_completion", request.model_dump())
    assert replay["success"] and replay["outcome"] == "already_superseded"
    assert len(await rows(case, t.integration_subject_journal)) == 1
    changed = request.model_copy(update={"reason": "different disposition"})
    assert not (await handler.execute("integration_supersede_completion", changed.model_dump()))[
        "success"]


@pytest.mark.parametrize("field,value", [
    ("expected_version", 9), ("expected_claim_epoch", 9), ("expected_head_sha", "f" * 40),
    ("completion_id", "older-completion"), ("human_reply_id", "agent-report"),
    ("task_id", "foreign"), ("project_id", "foreign"),
])
async def test_exact_input_refusals(case, field, value):
    with pytest.raises(ValueError):
        await case.service.run(case.request.model_copy(update={field: value}),
                               principal="human:local-operator")
    assert (await case.db.get_integration_subject("subject"))["phase"] != "done"


@pytest.mark.parametrize("mutation", ["owner", "completion", "subject", "origin", "remote"])
async def test_proof_changes_between_preview_and_apply_refuse(case, mutation):
    preview = await case.service.run(case.request, principal="human:local-operator")
    async with case.db._engine.begin() as conn:
        if mutation == "owner":
            await conn.execute(update(t.integration_branch_owners).values(fence_token=4))
        elif mutation == "completion":
            await conn.execute(update(t.task_completion_records).values(summary="changed"))
        elif mutation == "subject":
            await conn.execute(update(t.integration_subjects).values(version=1))
        elif mutation == "origin":
            await conn.execute(update(t.task_branch_origins).values(base_refresh={"changed": True}))
    if mutation == "remote":
        (case.checkout / "work").write_text("changed source")
        await case.run("add", ".")
        await case.run("commit", "-m", "changed source")
        await case.run("push", "origin", "aq/root")
    request = case.request.model_copy(update={"dry_run": False,
                                             "expected_proof_sha256": preview["proof_sha256"]})
    with pytest.raises(ValueError):
        await case.service.run(request, principal="human:local-operator")
    assert not await rows(case, t.integration_subject_journal)


async def test_cas_detects_change_during_remote_probe(case):
    preview = await case.service.run(case.request, principal="human:local-operator")
    async def racing(target):
        observed = await case.snapshot(target)
        async with case.db._engine.begin() as conn:
            await conn.execute(update(t.integration_branch_owners).values(fence_token=4))
        return observed
    case.db._delivery_observer.snapshot = racing
    request = case.request.model_copy(update={"dry_run": False,
                                             "expected_proof_sha256": preview["proof_sha256"]})
    with pytest.raises(ValueError, match="identity changed"):
        await case.service.run(request, principal="human:local-operator")
    assert not await rows(case, t.integration_subject_journal)


async def stopped_writer(case):
    await case.db.create_session(SessionRecord(
        id="writer", project_id="p", profile_id="worker", harness="codex", provider="fake",
        name="pool-writer", lifecycle="pool", work_dir=str(case.checkout), epoch="epoch",
        instance_token="exact-process", started_at=1, state="stopped", desired_state="stopped",
        task_id="root", last_claim_epoch=0,
    ))
    await case.db.create_workspace(Workspace(id="checkout", project_id="p",
        workspace_path=str(case.checkout), source_type=RepoSourceType.LINK, locked_by_task_id="root"))
    async with case.db._engine.begin() as conn:
        await conn.execute(update(t.integration_branch_owners).values(
            handoff_state="attached", session_id="writer", workspace_id="checkout"))
        await conn.execute(update(t.integration_subjects).values(
            writer_status="working", writer_task_id="root", writer_session_id="writer",
            writer_fence_token=3))


@pytest.mark.parametrize("writer_state", ["running", "unknown", "stopped"])
async def test_exact_writer_requires_provider_proof(case, writer_state):
    await stopped_writer(case)
    async with case.db._engine.begin() as conn:
        if writer_state == "running":
            await conn.execute(update(t.sessions).values(state="running", desired_state="running"))
        elif writer_state == "unknown":
            await conn.execute(update(t.integration_subjects).values(writer_status="unknown"))
    case.service.confirm_stopped.return_value = False
    with pytest.raises(ValueError, match="writer|Writer"):
        await case.service.run(case.request, principal="human:local-operator")


async def test_stopped_exact_writer_retains_instance_and_stops_subject(case):
    await stopped_writer(case)
    _, result = await apply(case)
    row = await case.db.get_integration_subject("subject")
    assert row["writer_status"] == "stopped"
    assert row["writer_stop_proof"]["instance_token"] == "exact-process"
    assert result["cleanup"]["state"] == "pending"


@pytest.mark.parametrize("role,state,project,elevated", [
    ("worker", "running", "p", True), ("supervisor", "stopped", "p", True),
    ("supervisor", "running", "other", True), ("supervisor", "running", "p", False),
])
async def test_worker_stale_and_foreign_authority_refuse(case, role, state, project, elevated):
    await case.db.create_project(Project(id="other", name="Other"))
    await case.db.create_session(SessionRecord(
        id="caller", project_id=project, profile_id=role, harness="codex", provider="fake",
        name="caller", lifecycle="named", work_dir="/tmp", epoch="epoch", instance_token="caller",
        started_at=1, state=state, desired_state="running" if state == "running" else "stopped"))
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                   session_id="caller", project_id=project, elevated=elevated)
    with principal_context(principal), pytest.raises(ValueError):
        await case.service.run(case.request, principal="supervisor session:caller")


async def test_supervisor_stopping_during_probe_refuses(case):
    await case.db.create_session(SessionRecord(
        id="caller", project_id="p", profile_id="supervisor", harness="codex", provider="fake",
        name="caller", lifecycle="named", work_dir="/tmp", epoch="epoch", instance_token="caller",
        started_at=1, state="running", desired_state="running"))
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                   session_id="caller", project_id="p", elevated=True)
    with principal_context(principal):
        preview = await case.service.run(case.request, principal="supervisor session:caller")
        async def racing(target):
            observed = await case.snapshot(target)
            async with case.db._engine.begin() as conn:
                await conn.execute(update(t.sessions).values(state="stopped", desired_state="stopped"))
            return observed
        case.db._delivery_observer.snapshot = racing
        with pytest.raises(ValueError, match="supervisor"):
            await case.service.run(case.request.model_copy(update={"dry_run": False,
                "expected_proof_sha256": preview["proof_sha256"]}),
                principal="supervisor session:caller")
    assert not await rows(case, t.integration_subject_journal)


async def test_active_repository_writer_refuses_without_wait(case):
    preview = await case.service.run(case.request, principal="human:local-operator")
    request = case.request.model_copy(update={"dry_run": False,
                                             "expected_proof_sha256": preview["proof_sha256"]})
    async with case.db._engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": lock_key("r")})
        with pytest.raises(ValueError, match="operation is active"):
            await asyncio.wait_for(case.service.run(request, principal="human:local-operator"), 5)


async def test_later_obsolete_guard_rolls_back_subject_and_journal(case):
    await case.db.create_task(Task(id="child", project_id="p", title="child", description="child",
                                  status=TaskStatus.READY, parent_task_id="root"))
    with pytest.raises(ObsoleteCloseRefused):
        await apply(case)
    assert (await case.db.get_integration_subject("subject"))["phase"] != "done"
    assert not await rows(case, t.integration_subject_journal)


async def test_missing_or_spoofed_human_evidence_refuses(case):
    async with case.db._engine.begin() as conn:
        await conn.execute(update(t.escalation_messages).values(verified_actor="session:worker"))
    with pytest.raises(ValueError, match="verified human"):
        await apply(case)


async def test_missing_retained_provenance_refuses(case):
    await case.run("push", "origin", ":" + case.identity.ref)
    with pytest.raises(ValueError, match="provenance"):
        await apply(case)


async def test_subject_gate_is_not_waived(case):
    async with case.db._engine.begin() as conn:
        await conn.execute(update(t.integration_subjects).values(gate_id="product-approval"))
    with pytest.raises(ValueError, match="gate"):
        await apply(case)
    assert not await rows(case, t.integration_subject_journal)


async def test_unrelated_subject_is_not_settled(case):
    subject = Subject.from_row(await case.db.get_integration_subject("subject"))
    other = subject.model_copy(update={"id": "other-subject", "subject_key": "another-generation"})
    await case.db.ensure_integration_subject(other.to_row())
    with pytest.raises(ValueError, match="unrelated"):
        await apply(case)
    assert all(row["phase"] != "done" for row in await rows(case, t.integration_subjects))


async def parent_episode(case, *, operation_state="active"):
    subject = Subject.from_row(await case.db.get_integration_subject("subject"))
    async with case.db._engine.begin() as conn:
        await conn.execute(t.integration_subjects.delete())
        await conn.execute(insert(t.integration_parent_episodes).values(
            id="episode", parent_task_id="root", repository_id="r", generation=0,
            pre_collection_checkpoint_sha=case.base, created_at=1))
        await conn.execute(insert(t.task_integration_checkpoints).values(
            task_id="root", repository_id="r", branch="aq/root", generation=0,
            checkpoint_sha=case.head, state="integration_ready", episode_id="episode",
            branch_owner_id="owner", updated_at=1))
        await conn.execute(insert(t.integration_repair_operations).values(
            id="operation", target_kind="parent", parent_task_id="root", episode_id="episode",
            state=operation_state, policy_snapshot={}, artifact_snapshot={},
            required_check_version="v1", created_at=1, updated_at=1))
        await conn.execute(insert(t.integration_repair_stages).values(
            operation_id="operation", ordinal=0, policy={}, starting_sha=case.head,
            state="pending", attempts=2))
    await case.db.ensure_integration_subject(Subject.model_validate({
        **subject.model_dump(), "kind": "parent_episode", "subject_key": "parent:root:episode",
        "parent_episode_id": "episode",
    }).to_row())


@pytest.mark.parametrize("operation_state", ["active", "completed"])
async def test_exact_parent_operation_cancelled_without_replacing_checkpoint_history(case,
                                                                                   operation_state):
    await parent_episode(case, operation_state=operation_state)
    checkpoint = await rows(case, t.task_integration_checkpoints)
    episode = await rows(case, t.integration_parent_episodes)
    completions = await rows(case, t.task_completion_records)
    await apply(case)
    operation = (await rows(case, t.integration_repair_operations))[0]
    assert operation["state"] == ("completed" if operation_state == "completed" else "cancelled")
    assert (await rows(case, t.integration_repair_stages))[0]["attempts"] == 2
    assert await rows(case, t.task_integration_checkpoints) == checkpoint
    assert await rows(case, t.integration_parent_episodes) == episode
    assert await rows(case, t.task_completion_records) == completions


@pytest.mark.parametrize("change", ["checkpoint", "delegate", "collector"])
async def test_parent_checkpoint_delegates_and_collector_remain_fenced(case, change):
    await parent_episode(case)
    preview = await case.service.run(case.request, principal="human:local-operator")
    async with case.db._engine.begin() as conn:
        if change == "checkpoint":
            await conn.execute(update(t.task_integration_checkpoints).values(version=1))
        elif change == "delegate":
            await conn.execute(update(t.integration_repair_stages).values(
                repair_task_id="delegate", writer_kind="repair_delegate", state="active"))
        else:
            await conn.execute(insert(t.integration_branch_owners).values(
                id="collector", repository_id="r", ref="refs/heads/aq/collector",
                owner_role="collector", owner_id="operation", handoff_state="reserved",
                fence_token=1, created_at=1, updated_at=1))
    with pytest.raises(ValueError):
        await case.service.run(case.request.model_copy(update={"dry_run": False,
            "expected_proof_sha256": preview["proof_sha256"]}), principal="human:local-operator")
    assert (await rows(case, t.integration_repair_operations))[0]["state"] == "active"
    assert not await rows(case, t.integration_subject_journal)


async def test_concurrent_exact_requests_never_duplicate_settlement(case):
    preview = await case.service.run(case.request, principal="human:local-operator")
    request = case.request.model_copy(update={"dry_run": False,
                                             "expected_proof_sha256": preview["proof_sha256"]})
    results = await asyncio.gather(*(
        case.service.run(request, principal="human:local-operator") for _ in range(2)
    ), return_exceptions=True)
    assert any(isinstance(result, dict) and result["outcome"] == "superseded" for result in results)
    assert all(isinstance(result, (dict, ValueError)) for result in results)
    replay = await case.service.run(request, principal="human:local-operator")
    assert replay["outcome"] == "already_superseded"
    assert len(await rows(case, t.integration_subject_journal)) == 1
    assert len(await rows(case, t.task_completion_records)) == 1


async def test_open_task_gate_is_preserved(case):
    async with case.db._engine.begin() as conn:
        await conn.execute(insert(t.gates).values(id="gate", project_id="p", gate_type="human",
            title="Original product approval", created_at=1))
        await conn.execute(insert(t.task_gates).values(task_id="root", gate_id="gate"))
    with pytest.raises(ValueError, match="gate"):
        await apply(case)
    assert (await rows(case, t.gates))[0]["status"] == "open"


@pytest.mark.parametrize("race", [False, True])
async def test_normal_cleanup_proves_source_and_fences_reassigned_owner(case, monkeypatch, race):
    await case.db.create_workspace(Workspace(id="base", project_id="p",
        workspace_path=str(case.checkout), source_type=RepoSourceType.LINK))
    recovery = ObsoleteOwnerRelease(case.db, case.git, None,
                                   confirm_stopped=AsyncMock(return_value=True))
    release = recovery._release
    async def racing_release(row, *args, **kwargs):
        if race:
            async with case.db._engine.begin() as conn:
                await conn.execute(update(t.integration_branch_owners).where(
                    t.integration_branch_owners.c.id == row["id"],
                ).values(owner_id="another-task", fence_token=4))
        return await release(row, *args, **kwargs)
    monkeypatch.setattr(recovery, "_release", racing_release)
    async def recover(owner_id, principal):
        return await recovery.recover(owner_id, principal=principal)
    case.closer.release_owner = recover
    history = await rows(case, t.task_completion_records)
    _, result = await apply(case)
    owner = (await rows(case, t.integration_branch_owners))[0]
    assert result["cleanup"]["state"] == ("pending" if race else "done")
    assert owner["owner_id"] == ("another-task" if race else "root")
    assert owner["handoff_state"] == ("reserved" if race else "released")
    if race:
        assert result["cleanup"]["pending"][0]["reason"] == "stale_fence"
    assert await rows(case, t.task_completion_records) == history
    assert await case.provenance.remote_completion(case.identity) is not None
    assert (await rows(case, t.branch_retirements))[0]["task_id"] == "root"


@pytest.mark.parametrize("context", ["", "closed_obsolete"])
async def test_serialized_receipt_cannot_complete_managed_parent(case, context):
    await parent_episode(case)
    async with case.db.immediate() as conn:
        with pytest.raises(HierarchyError, match="completion"):
            await case.db._apply_transition(
                conn, "root", TaskStatus.COMPLETED, force=True, context=context,
                _obsolete_supersession_token={"subject_id": "subject", "task_id": "root"},
            )
    assert (await case.db.get_task("root")).status == TaskStatus.PAUSED


async def test_real_receipt_cannot_authorize_ordinary_completion_or_another_connection(case):
    await parent_episode(case)
    request, result = await apply(case)
    async with case.db.immediate() as conn:
        receipt = SupersessionSettlement(
            conn, "root", "subject", 1, result["journal_seq"], request.expected_proof_sha256,
        )
        assert await receipt.valid_on(conn, "root")
        async with case.db._engine.connect() as other:
            assert not await receipt.valid_on(other, "root")
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "root").values(status="PAUSED"))
        with pytest.raises(HierarchyError, match="completion"):
            await case.db._apply_transition(
                conn, "root", TaskStatus.COMPLETED, force=True,
                _obsolete_supersession_token=receipt,
            )


async def test_already_completed_root_preserves_claim_epoch_and_completion(case):
    async with case.db._engine.begin() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "root").values(status="COMPLETED"))
    history = await rows(case, t.task_completion_records)
    request, _ = await apply(case)
    assert (await case.db.get_task("root")).claim_epoch == 0
    assert await rows(case, t.task_completion_records) == history
    assert await case.db.get_task_meta("root", "work_outcome") == "abandoned"
    assert (await case.service.run(request, principal="human:local-operator"))[
        "outcome"] == "already_superseded"


@pytest.mark.parametrize("identity", ["released_owner", "checkpoint_owner"])
async def test_initial_source_owner_mismatch_refuses(case, identity):
    async with case.db._engine.begin() as conn:
        if identity == "released_owner":
            await conn.execute(update(t.integration_branch_owners).values(handoff_state="released"))
    if identity == "checkpoint_owner":
        await parent_episode(case)
        async with case.db._engine.begin() as conn:
            await conn.execute(update(t.task_integration_checkpoints).values(branch_owner_id="other"))
    with pytest.raises(ValueError, match="owner|owns"):
        await apply(case)
    assert not await rows(case, t.integration_subject_journal)


async def test_replay_preview_does_not_retry_cleanup(case):
    request, _ = await apply(case)
    case.closer.cleanup = AsyncMock()
    result = await case.service.run(request.model_copy(update={"dry_run": True}),
                                    principal="human:local-operator")
    assert result["outcome"] == "already_superseded"
    case.closer.cleanup.assert_not_awaited()


@pytest.mark.parametrize("change", ["disposition", "new_completion", "reopened"])
async def test_changed_settlement_cannot_replay_cleanup(case, change):
    request, _ = await apply(case)
    if change == "new_completion":
        await case.db.save_task_completion(TaskCompletion(
            id="close-2", task_id="root", outcome="pass", branch="aq/root",
            commits=[case.head], completed_at=3,
        ))
    elif change == "disposition":
        await case.db.set_task_meta("root", "work_outcome", "changed")
    else:
        async with case.db._engine.begin() as conn:
            await conn.execute(update(t.tasks).where(t.tasks.c.id == "root").values(
                status="PAUSED", claim_epoch=2))
    case.closer.cleanup = AsyncMock()
    with pytest.raises(ValueError, match="settlement"):
        await case.service.run(request, principal="human:local-operator")
    case.closer.cleanup.assert_not_awaited()
