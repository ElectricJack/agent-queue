"""Durable bounded repair stages and execution delegates."""

from __future__ import annotations

import asyncio
import json
import subprocess
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from sqlalchemy import insert, select, update

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.commands.task_commands import TaskCommandsMixin
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_child_dispositions,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stage_evidence,
    integration_repair_stages,
    integration_review_evidence,
    messages,
    playbook_artifacts,
    sessions,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.git.manager import GitManager, RemoteRefResult, RemoteRefState
from src.integration.models import (
    ArtifactSnapshot,
    BranchKey,
    Fence,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.integration.ownership import BranchOwnership
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from src.profiles.capabilities import CapabilityPolicy
from src.scheduler import AssignAction

STARTING_SHA = "a" * 40


@pytest.mark.parametrize("task_branch", ["aq/integration/batch", "refs/heads/aq/integration/batch"])
def test_repair_delegate_matches_full_root_ref_after_claim(task_branch):
    from src.integration.repair import RepairService

    task = {
        "project_id": "p", "parent_task_id": None, "repo_id": "repo",
        "branch_name": task_branch, "created_by_kind": "integration_repair",
        "created_by_id": "operation", "status": TaskStatus.READY.value,
    }
    target = BranchKey(repository_id="repo", branch="refs/heads/aq/integration/batch")
    assert RepairService._delegate_task_matches(task, {"id": "operation"}, target, "p")


@pytest.mark.parametrize("recorded,head,expected", [
    ([], STARTING_SHA, True),
    ([STARTING_SHA], STARTING_SHA, True),
    ([], "f" * 40, True),
    (["f" * 40], STARTING_SHA, False),
    ([STARTING_SHA], "f" * 40, False),
    ([STARTING_SHA, "f" * 40], STARTING_SHA, False),
    (["f" * 40, STARTING_SHA], STARTING_SHA, False),
    ([STARTING_SHA, STARTING_SHA], STARTING_SHA, False),
    ({}, STARTING_SHA, False),
    (None, STARTING_SHA, False),
])
def test_unchanged_close_names_nothing_or_only_the_confirmed_head(recorded, head, expected):
    from src.integration.noop_repair import names_only_the_confirmed_head

    assert names_only_the_confirmed_head(recorded, head) is expected


def _artifact() -> ArtifactSnapshot:
    return ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
        compiled_at="2026-09-05T00:00:00Z",
        version=1,
    )


def _boundary(*, primary_seconds: int = 30, primary_attempts: int = 2):
    return IntegrationBoundaryPolicy(
        required_checks=RequiredCheckSet(
            version="checks-v1", names=("unit",), producer_id="forge"
        ),
        repair=RepairPolicy(
            primary_seconds=primary_seconds,
            primary_attempts=primary_attempts,
            debug_seconds=60,
            debug_attempts=1,
            debug_intelligence_class="debug-high",
            debug_profile_id="debugger",
        ),
        route=PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            activation_id="activation-audit",
            artifact=_artifact(),
        ),
        primary_intelligence_class="primary-medium",
        primary_profile_id="repairer",
        verifier_intelligence_class="verifier-high",
        verifier_profile_id="verifier",
    )


def _policy() -> dict:
    boundary = _boundary()
    return HierarchicalIntegrationPolicy(
        parent=boundary,
        root=boundary,
        branchless_parent="verifier",
        on_failed_child="block",
    ).model_dump(mode="json")


@pytest.fixture
async def db(tmp_path, reuse_database):
    database = await reuse_database("repair.db")
    await _configure_db(database)
    yield database


async def _configure_db(database) -> None:
    await database.create_profile(AgentProfile(id="repairer", name="Repairer"))
    await database.create_profile(AgentProfile(id="debugger", name="Debugger"))
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
    )
    await database.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
        hierarchical_integration_policy=_policy(),
    )


async def _seed_parent_operation(
    db, *, evidence_id: str = "failed-check", starting_sha: str = STARTING_SHA, policy=None,
    parent_status: TaskStatus = TaskStatus.PAUSED,
) -> None:
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            title="Parent",
            description="",
            status=parent_status,
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=3,
                pre_collection_checkpoint_sha=starting_sha,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot=policy or _policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                required_check_version="checks-v1",
                route_playbook_id="hierarchical-delivery",
                route_scope="project",
                route_scope_identifier="p",
                route_activation_id="activation-audit",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                generation=3,
                checkpoint_sha=starting_sha,
                state="verifying",
                episode_id="episode",
                version=1,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id=evidence_id,
                operation_id="operation",
                parent_task_id="parent",
                parent_generation=3,
                parent_head_sha=starting_sha,
                producer_id="forge",
                workflow_id="workflow",
                run_id="run-1",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": "failure"},
                conclusion="failure",
                classification="conclusive",
                observed_at=9.0,
            )
        )


async def _add_parent_evidence(
    db,
    evidence_id: str,
    *,
    run_id: str,
    conclusion: str,
    classification: str = "conclusive",
    generation: int = 3,
    head_sha: str = STARTING_SHA,
    observed_at: float = 9.0,
) -> None:
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_check_evidence).values(
                id=evidence_id,
                operation_id="operation",
                parent_task_id="parent",
                parent_generation=generation,
                parent_head_sha=head_sha,
                producer_id="forge",
                workflow_id="workflow",
                run_id=run_id,
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": conclusion},
                conclusion=conclusion,
                classification=classification,
                observed_at=observed_at,
            )
        )


async def _repair_stage(db, operation_id: str, ordinal: int) -> dict:
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == ordinal,
                )
            )
        ).mappings().one()
    return dict(row)


@pytest.mark.parametrize("operation_state", ["completed", "cancelled", "active", "human_required"])
@pytest.mark.parametrize("task_status", [TaskStatus.READY, TaskStatus.PAUSED])
async def test_terminal_delegate_retirement_preserves_outcomes(db, operation_state, task_status):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await db.create_task(Task(id="delegate", project_id="p", title="Repair", description="", status=task_status))
    await db.set_task_meta("delegate", "needs_attention", "slot_reset_failed")
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(state=operation_state))
        await conn.execute(update(integration_repair_stages).values(
            repair_task_id="delegate", writer_kind="repair_delegate"))
    before = await _repair_stage(db, "operation", 0)
    result = await service.retire_terminal_delegates(200.0)
    if operation_state in {"active", "human_required"}:
        assert result == []
        assert (await db.get_task("delegate")).status == task_status
        assert await db.get_task_meta("delegate", "needs_attention") == "slot_reset_failed"
        return
    assert result == ["delegate"]
    retired = await db.get_task("delegate")
    # A terminal, non-success disposition: not indefinite PAUSED, never a pass.
    assert retired.status == TaskStatus.FAILED
    assert retired.resume_after is None
    assert retired.retry_count == 0
    assert await db.get_task_meta("delegate", "needs_attention") is None
    record = await db.get_task_meta("delegate", "integration_retirement")
    assert record["state"] == operation_state
    assert record["disposition"] == (
        "cancelled" if operation_state == "cancelled" else "superseded"
    )
    assert record["previous_status"] == task_status.value
    assert record["previous_attention"] == "slot_reset_failed"
    assert record["cleanup"] == {"state": "clear", "blockers": []}
    assert await _repair_stage(db, "operation", 0) == before
    assert await service.retire_terminal_delegates(201.0) == []
    with pytest.raises(ValueError, match="delegate is no longer required"):
        await db.transition_task("delegate", TaskStatus.READY, context="restart_task", force=True)


async def test_legacy_paused_retirement_rolls_forward_to_a_terminal_disposition(db):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await db.create_task(Task(
        id="delegate", project_id="p", title="Repair", description="", status=TaskStatus.PAUSED,
    ))
    await db.set_task_meta("delegate", "integration_retirement", {
        "operation_id": "operation", "state": "cancelled", "previous_status": "BLOCKED",
        "retired_at": 150.0, "reason": "integration operation operation is cancelled",
        "previous_attention": "stuck_timeout",
    })
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(state="cancelled"))
        await conn.execute(update(integration_repair_stages).values(
            repair_task_id="delegate", writer_kind="repair_delegate"))
    assert await service.retire_terminal_delegates(200.0) == ["delegate"]
    assert (await db.get_task("delegate")).status == TaskStatus.FAILED
    record = await db.get_task_meta("delegate", "integration_retirement")
    assert record["disposition"] == "cancelled"
    assert record["previous_status"] == "BLOCKED"
    assert record["previous_attention"] == "stuck_timeout"
    assert record["paused_at"] == 150.0


@pytest.mark.parametrize("operation_state", ["completed", "cancelled"])
@pytest.mark.parametrize("delegate_kind", ["repair", "verifier"])
async def test_terminal_delegate_cannot_resume_even_before_owner_cleanup(db, operation_state, delegate_kind):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await RepairService(db).start("operation", STARTING_SHA, "failed-check", now=100.0)
    await db.create_task(Task(id="delegate", project_id="p", title="Delegate", description="", status=TaskStatus.PAUSED))
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(state=operation_state))
        if delegate_kind == "repair":
            await conn.execute(update(integration_repair_stages).values(
                repair_task_id="delegate", writer_kind="repair_delegate"))
        else:
            await conn.execute(update(integration_repair_operations).values(verifier_task_id="delegate"))
    await db.create_session(SessionRecord(
        id="retained", task_id="delegate", project_id="p", profile_id="repairer",
        harness="fake", provider="fake", name="retained", lifecycle="task",
        state="stopped", desired_state="stopped", work_dir="/tmp/retained", epoch="epoch",
        instance_token="instance", started_at=100.0,
    ))
    assert await db.get_task_meta("delegate", "integration_retirement") is None
    assert await db.get_terminal_integration_delegate_operation("delegate") == {
        "id": "operation", "state": operation_state,
    }
    with pytest.raises(ValueError, match="delegate is no longer required"):
        await db.resume_task("delegate")
    with pytest.raises(ValueError, match="delegate is no longer required"):
        await db.transition_task("delegate", TaskStatus.READY, context="restart_task", force=True)
    assert await db.recover_orphaned_pause("delegate") is None
    assert (await db.get_task("delegate")).status == TaskStatus.PAUSED
    assert (await db.get_session("retained")).task_id == "delegate"


@pytest.mark.parametrize("state", ["running", "stopped"])
async def test_terminal_delegate_retirement_waits_for_session_detachment(db, state):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await db.create_task(Task(id="delegate", project_id="p", title="Repair", description="", status=TaskStatus.READY))
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(state="completed"))
        await conn.execute(update(integration_repair_stages).values(
            repair_task_id="delegate", writer_kind="repair_delegate"))
    await db.create_session(SessionRecord(
        id="leftover", task_id="delegate", project_id="p", profile_id="repairer",
        harness="fake", provider="fake", name="leftover", lifecycle="task",
        state=state, desired_state="running", work_dir="/tmp/leftover", epoch="epoch",
        instance_token="instance", started_at=100.0,
    ))
    assert await service.retire_terminal_delegates(200.0) == []
    assert (await db.get_task("delegate")).status == TaskStatus.READY


async def _blocked_delegate(db, **fields) -> None:
    await db.create_task(Task(
        id="delegate", project_id="p", title="Repair", description="",
        status=TaskStatus.BLOCKED, repo_id="repo", branch_name="aq/parent", **fields,
    ))
    await db.update_task("delegate", created_at=1.0)
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(
            repair_task_id="delegate", writer_kind="repair_delegate"))


async def _stopped_delegate_writer(
    db, *, sid: str = "writer", task_id: str = "delegate"
) -> None:
    """The delegate's writer died without closing, as the reconciler records it."""
    await db.create_session(SessionRecord(
        id=sid, task_id=task_id, project_id="p", profile_id="repairer",
        harness="fake", provider="fake", name=sid, lifecycle="task",
        state="running", desired_state="running", work_dir="/tmp/retained", epoch="epoch",
        instance_token=sid, started_at=100.0, last_activity=150.0,
    ))
    await db.update_session(sid, state="stopped", desired_state="stopped", ended_at=200.0,
                            end_reason="session_exited_open")
    await db.set_task_meta(task_id, "needs_attention", "session_exited_open")


@pytest.mark.parametrize("operation_state", ["active", "escalated", "human_required"])
@pytest.mark.parametrize("delegate_kind", ["repair", "verifier"])
async def test_integration_owned_failure_is_one_incident_with_its_stage_budget(
    db, operation_state, delegate_kind
):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await RepairService(db).start("operation", STARTING_SHA, "failed-check", now=100.0)
    await _blocked_delegate(db)
    await _stopped_delegate_writer(db)
    async with db.immediate() as conn:
        fields = {"state": operation_state}
        if delegate_kind == "verifier":
            fields["verifier_task_id"] = "delegate"
            await conn.execute(update(integration_repair_stages).values(
                repair_task_id=None, writer_kind=None))
        await conn.execute(update(integration_repair_operations).values(**fields))

    first = await db.notify_task_recovery("delegate", project_id="p")
    assert first["outcome"] == "not_actionable"
    assert first["operation_id"] == "operation"
    assert await db.queue_task_recovery_notifications() == 0
    replay = await db.notify_task_recovery("delegate", project_id="p")
    assert replay["outcome"] == "existing"
    assert replay["incident_id"] == first["incident_id"]
    assert replay["redelivered"] is False
    incident = await db.get_task_meta("delegate", "supervisor_recovery_incident")
    assert incident["id"] == first["incident_id"]
    assert incident["owner"] == {
        "kind": "integration_operation", "operation_id": "operation",
        "operation_state": operation_state, "role": "delegate", "stage": 0,
        "stage_state": "active",
        "attempts": 0, "attempt_limit": 2, "deadline_at": 130.0,
        "deadline_kind": "stage_runtime",
    }
    assert incident["retry_allowed"] is False
    assert "generic recovery is refused" in incident["next_action"]
    with pytest.raises(ValueError, match="owned by integration operation operation"):
        await db.decide_task_recovery(
            "delegate", incident["id"], "retry", "try again", author_kind="supervisor",
            author_id="supervisor-p", stopped_session={"id": "writer", "instance_token": "writer"},
        )
    assert (await db.get_task("delegate")).status == TaskStatus.BLOCKED
    assert await db.get_task_meta("delegate", "supervisor_recovery_attempts") is None
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(messages))).all() == []

    # Waiting for a passing result to be accepted is its own clock.
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(state="awaiting_completion"))
        owner = await db._recovery_owner(conn, {"id": "delegate", "project_id": "p"})
    assert owner["deadline_kind"] == "acceptance_wait"


async def test_dead_integration_delegate_records_incident_without_supervisor_notice(db):
    from src.config import AppConfig
    from src.integration.repair import RepairService
    from src.sessions.fake import FakeProvider
    from src.sessions.provider import SessionSpec
    from src.sessions.reconciler import SessionReconciler

    await _seed_parent_operation(db)
    await RepairService(db).start("operation", STARTING_SHA, "failed-check", now=100.0)
    await _blocked_delegate(db, retry_count=3, max_retries=3)
    await db.transition_task("delegate", TaskStatus.IN_PROGRESS, force=True)
    provider = FakeProvider()
    await provider.start(SessionSpec(
        session_name="writer", work_dir="/tmp/retained", command=("fake",),
        instance_token="writer",
    ))
    await db.create_session(SessionRecord(
        id="writer", task_id="delegate", project_id="p", profile_id="repairer",
        harness="fake", provider="fake", name="writer", lifecycle="task",
        state="running", desired_state="running", work_dir="/tmp/retained", epoch="epoch",
        instance_token="writer", started_at=100.0,
    ))
    provider.script_death("writer")
    config = AppConfig()
    config.sessions.enabled = True
    config.agents_config.stuck_timeout_seconds = 0
    reconciler = SessionReconciler(
        db, config, SimpleNamespace(create=lambda *_: provider), epoch="epoch"
    )
    await reconciler.tick(now=10_000.0)

    assert (await db.get_task("delegate")).status == TaskStatus.BLOCKED
    assert (await db.get_session("writer")).state == "stopped"
    assert await db.get_task_meta("delegate", "needs_attention") == "session_exited_open"
    assert await db.queue_task_recovery_notifications() == 0
    incident = await db.get_task_meta("delegate", "supervisor_recovery_incident")
    result = await db.notify_task_recovery("delegate", project_id="p")
    assert result["outcome"] == "existing"
    assert incident["id"] == result["incident_id"]
    assert incident["owner"]["deadline_at"] == 130.0
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(messages))).all() == []


@pytest.mark.parametrize("delivered", [False, True])
async def test_legacy_delegate_recovery_notice_is_archived_without_redelivery(db, delivered):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await RepairService(db).start("operation", STARTING_SHA, "failed-check", now=100.0)
    await _blocked_delegate(db)
    await _stopped_delegate_writer(db)
    await db.notify_task_recovery("delegate", project_id="p")
    incident = await db.get_task_meta("delegate", "supervisor_recovery_incident")
    await db.create_session(SessionRecord(
        id="supervisor", project_id="p", profile_id="repairer", harness="fake",
        provider="fake", name="n-supervisor--p", lifecycle="named", state="running",
        desired_state="running", epoch="epoch", instance_token="supervisor",
        work_dir="/tmp/supervisor", started_at=100.0,
    ))
    await db.update_session("supervisor", state="stopped", desired_state="stopped")
    message_id = "msg-" + incident["id"]
    async with db.immediate() as conn:
        await conn.execute(insert(messages).values(
            id=message_id, project_id="p", from_kind="system", from_id="task-recovery",
            to_kind="session", to_id="supervisor-p", subject="Task recovery: delegate",
            body="Legacy delegate notice", created_at=200.0, archive_after_inject=1,
            body_kind="task_recovery", delivered_at=300.0 if delivered else None,
        ))

    replay = await db.notify_task_recovery("delegate", project_id="p")
    assert replay["outcome"] == "existing"
    assert replay["redelivered"] is False
    archived = await db.get_message(message_id)
    assert archived.archived_at is not None
    assert archived.delivered_at == (300.0 if delivered else None)
    assert await db.queue_task_recovery_notifications() == 0
    assert (await db.get_message(message_id)).archived_at == archived.archived_at
    assert await db.get_task_meta("delegate", "supervisor_recovery_incident") == incident
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(messages))).all()) == 1


async def test_integration_parent_failure_still_notifies_supervisor(db):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db, parent_status=TaskStatus.BLOCKED)
    await RepairService(db).start("operation", STARTING_SHA, "failed-check", now=100.0)
    await db.update_task("parent", created_at=1.0)
    await _stopped_delegate_writer(db, task_id="parent")

    first = await db.notify_task_recovery("parent", project_id="p")
    assert first["outcome"] == "queued"
    assert await db.queue_task_recovery_notifications() == 0
    incident = await db.get_task_meta("parent", "supervisor_recovery_incident")
    assert incident["owner"]["role"] == "parent"
    async with db._engine.connect() as conn:
        queued = (await conn.execute(select(messages))).mappings().all()
    assert len(queued) == 1
    assert queued[0]["subject"] == "Task recovery: parent"




@pytest.mark.parametrize("operation_state,disposition", [
    ("cancelled", "cancelled"), ("completed", "superseded"),
])
async def test_live_writer_of_an_ended_operation_is_told_not_to_retry_its_close(
    orchestrator_factory, operation_state, disposition
):
    """A close that can never be accepted must say so, not look like a stale race."""
    from src.integration.repair import RepairService

    orchestrator = await orchestrator_factory()
    db = orchestrator.db
    await _configure_db(db)
    await _seed_parent_operation(db)
    await RepairService(db).start("operation", STARTING_SHA, "failed-check", now=100.0)
    await db.create_task(Task(
        id="delegate", project_id="p", title="Repair", description="",
        status=TaskStatus.IN_PROGRESS, repo_id="repo", branch_name="aq/parent",
    ))
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(
            repair_task_id="delegate", writer_kind="repair_delegate"))
        await conn.execute(update(integration_repair_operations).values(state=operation_state))
    orchestrator._get_default_branch = AsyncMock(return_value="main")

    result = await orchestrator.complete_session_task(
        await db.get_task("delegate"), outcome="pass", session_live=True, session_id="writer",
    )

    assert result["verification_retry"] is True
    assert f"Integration operation operation is {operation_state}" in result["feedback"]
    assert f"retired ({disposition})" in result["feedback"]
    assert "Do not retry the close" in result["feedback"]
    assert "aq session drain-ack" in result["feedback"]
    assert result["retired"]["operation_id"] == "operation"
    assert result["retired"]["disposition"] == disposition
    assert (await db.get_task("delegate")).status == TaskStatus.IN_PROGRESS


@pytest.mark.parametrize("operation_state,retired", [
    ("escalated", True),
    # A human resume may revive the expired stage with this same writer.
    ("human_required", False),
])
async def test_live_writer_of_an_expired_stage_learns_whether_it_is_retired(
    orchestrator_factory, operation_state, retired
):
    """The expired stage-0 writer of batch 19cbd was told only "close is stale"."""
    from src.integration.repair import RepairService

    orchestrator = await orchestrator_factory()
    db = orchestrator.db
    await _configure_db(db)
    await _seed_parent_operation(db)
    await RepairService(db).start("operation", STARTING_SHA, "failed-check", now=100.0)
    await db.create_task(Task(
        id="delegate", project_id="p", title="Repair", description="",
        status=TaskStatus.IN_PROGRESS, repo_id="repo", branch_name="aq/parent",
    ))
    stage = await _repair_stage(db, "operation", 0)
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(
            repair_task_id="delegate", writer_kind="repair_delegate", state="expired"))
        if operation_state == "escalated":
            await conn.execute(insert(integration_repair_stages).values(
                operation_id="operation", ordinal=1, policy=stage["policy"],
                starting_sha=STARTING_SHA, attempts=0, state="active",
            ))
        await conn.execute(update(integration_repair_operations).values(
            state=operation_state, active_stage=1 if operation_state == "escalated" else 0))
    orchestrator._get_default_branch = AsyncMock(return_value="main")

    result = await orchestrator.complete_session_task(
        await db.get_task("delegate"), outcome="pass", session_live=True, session_id="writer",
    )

    assert result["verification_retry"] is True
    assert (await db.get_task("delegate")).status == TaskStatus.IN_PROGRESS
    if not retired:
        assert "retired" not in result
        assert result["feedback"] == "Repair stage is no longer active; close is stale."
        return
    assert result["feedback"].startswith(
        "Repair stage 0 of integration operation operation is expired; the operation "
        "moved on to stage 1: this delegate is retired (superseded)"
    )
    assert "retired (superseded)" in result["feedback"]
    assert "Do not retry the close" in result["feedback"]
    assert "aq session drain-ack" in result["feedback"]
    assert (result["retired"]["stage"], result["retired"]["stage_state"]) == (0, "expired")


@pytest.mark.parametrize("invalid", [None, "stage", "operation", "owner", "branch", "provenance"])
async def test_repair_origin_uses_exact_active_branch_reservation(db, invalid):
    from src.database.queries.claim_queries import _frontier_where
    from src.database.queries.hierarchy_queries import ProjectIntegrationMode
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    async with db.immediate() as conn:
        await conn.execute(insert(integration_branch_owners).values(
            id="owner", repository_id="repo", ref="aq/parent", owner_id="operation",
            owner_role="collector", fence_token=1, handoff_state="reserved",
            created_at=1.0, updated_at=1.0,
        ))
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    result = await service.dispatch("operation", 0)
    task_id = result["repair_task_id"]
    async with db.immediate() as conn:
        if invalid == "stage":
            await conn.execute(update(integration_repair_stages).values(state="expired"))
        elif invalid == "operation":
            await conn.execute(update(integration_repair_operations).values(state="completed"))
        elif invalid == "owner":
            await conn.execute(update(integration_branch_owners).values(owner_role="collector"))
        elif invalid == "branch":
            await conn.execute(update(integration_branch_owners).values(ref="aq/unrelated"))
        elif invalid == "provenance":
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(created_by_id="other"))
        for mode in (None, ProjectIntegrationMode(True, "repo")):
            claimable = await conn.scalar(select(tasks.c.id).where(
                tasks.c.id == task_id, _frontier_where("p", mode),
            ))
            assert (claimable == task_id) is (invalid is None)
    assert await db.is_hierarchy_task_runnable(task_id) is (invalid is None)


async def test_start_activates_reserved_parent_operation_once(db):
    """Replaying start must not reset the stage clock or immutable trigger binding."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    service = RepairService(db)

    started = await service.start(
        "operation", STARTING_SHA, "failed-check", now=100.0
    )
    replay = await service.start(
        "operation", STARTING_SHA, "failed-check", now=125.0
    )

    assert started == {
        "outcome": "started",
        "operation_id": "operation",
        "stage": 0,
        "starting_sha": STARTING_SHA,
        "started_at": 100.0,
        "deadline_at": 130.0,
    }
    assert replay == started | {"outcome": "already_started"}
    async with db._engine.connect() as conn:
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "operation",
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()
    assert stage["trigger_id"] == "failed-check"
    assert stage["current_subject"] == {
        "kind": "parent",
        "generation": 3,
        "head_sha": STARTING_SHA,
    }
    assert stage["deadline_event_id"] == "repair-deadline-operation-0"
    assert stage["attempts"] == 0
    assert stage["repair_task_id"] is None
    assert stage["writer_kind"] is None


@pytest.mark.parametrize("trigger_id", ["conflict-intent", "operation"])
async def test_start_accepts_only_exact_persisted_conflict_trigger(db, trigger_id):
    """A caller's trigger string is not proof without the conflicted intent."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="conflict-intent",
                domain_key="conflict-domain",
                operation_key="operation",
                project_id="p",
                receipt_id="receipt-conflict",
                source_task_id="child",
                target_task_id="parent",
                source_head="b" * 40,
                source_base="c" * 40,
                repository_id="repo",
                target_branch="aq/parent",
                expected_target=STARTING_SHA,
                fence_owner_id="operation",
                fence_token=1,
                state="conflict",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    started = await RepairService(db).start(
        "operation", STARTING_SHA, trigger_id, now=100.0
    )
    assert started["outcome"] == "started"
    replay = await RepairService(db).start("operation", STARTING_SHA, trigger_id, now=200.0)
    assert replay["outcome"] == "already_started"
    async with db._engine.connect() as conn:
        stage = (await conn.execute(select(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == "operation",
            integration_repair_stages.c.ordinal == 0,
        ))).mappings().one()
    assert stage["trigger_id"] == "conflict-intent"
    assert stage["started_at"] == 100.0


async def _seed_repeated_parent_conflict(db, *, deadline_at: float = 300.0):
    """Leave a completed debug delegate detached after its first repair."""
    await _seed_parent_operation(db)
    await db.create_task(
        Task(
            id="first-child",
            project_id="p",
            parent_task_id="parent",
            title="First child",
            description="",
            status=TaskStatus.COMPLETED,
            repo_id="repo",
            branch_name="aq/first-child",
        )
    )
    await db.create_task(
        Task(
            id="second-child",
            project_id="p",
            parent_task_id="parent",
            title="Second child",
            description="",
            status=TaskStatus.COMPLETED,
            repo_id="repo",
            branch_name="aq/second-child",
        )
    )
    await db.create_task(
        Task(
            id="repair-operation-1",
            project_id="p",
            title="Repair integration stage 1",
            description="old repair dossier",
            status=TaskStatus.COMPLETED,
            repo_id="repo",
            branch_name="aq/parent",
            profile_id="debugger", route_source="legacy",
            intelligence_class="debug-high",
            created_by_kind="integration_repair",
            created_by_id="operation",
        )
    )
    first_head = "b" * 40
    current_head = "d" * 40
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "operation")
            .values(active_stage=1, state="escalated", updated_at=110.0)
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="operation",
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                starting_sha=STARTING_SHA,
                trigger_id="failed-check",
                current_subject={"kind": "parent", "generation": 3, "head_sha": STARTING_SHA},
                deadline_event_id="repair-deadline-operation-0",
                started_at=100.0,
                deadline_at=130.0,
                attempts=2,
                dossier={"budget": {"attempts": 2}},
                state="failed",
                completed_at=110.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="operation",
                ordinal=1,
                policy=_boundary().repair.model_dump(mode="json"),
                repair_task_id="repair-operation-1",
                writer_kind="repair_delegate",
                starting_sha=first_head,
                trigger_id="stage-exhausted:operation:0",
                current_subject={"kind": "parent", "generation": 3, "head_sha": first_head},
                deadline_event_id="repair-deadline-operation-1",
                started_at=110.0,
                deadline_at=deadline_at,
                attempts=1,
                dossier={
                    "operation_id": "operation",
                    "starting_sha": first_head,
                    "trigger_id": "stage-exhausted:operation:0",
                    "branch_sha": first_head,
                    "budget": {
                        "ordinal": 1,
                        "started_at": 110.0,
                        "deadline_at": deadline_at,
                        "attempt_limit": 1,
                        "attempts": 1,
                    },
                },
                state="active",
            )
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="first-repair-receipt",
                domain_key="first-repair-domain",
                source_task_id="first-child",
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                reviewed_head_sha="c" * 40,
                before_sha=STARTING_SHA,
                after_sha=first_head,
                disposition="code",
                parent_operation_id="operation",
                parent_episode_id="episode",
                created_at=120.0,
            )
        )
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="second-conflict",
                domain_key="second-conflict-domain",
                operation_key="operation",
                project_id="p",
                receipt_id="second-conflict-receipt",
                source_task_id="second-child",
                target_task_id="parent",
                source_head="e" * 40,
                source_base=first_head,
                repository_id="repo",
                target_branch="aq/parent",
                expected_target=current_head,
                fence_owner_id="operation",
                fence_token=7,
                state="conflict",
                conflict_diagnostics={"paths": ["shared.py"]},
                created_at=140.0,
                updated_at=140.0,
            )
        )
        await conn.execute(
            insert(integration_branch_owners).values(
                id="continued-owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=7,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=140.0,
            )
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(state="awaiting_children")
        )
    return current_head


async def test_second_parent_conflict_reuses_current_stage_without_resetting_budget(db):
    """A later exact conflict resumes the detached debugger under its frozen budget."""
    from src.integration.repair import RepairService

    current_head = await _seed_repeated_parent_conflict(db)
    service = RepairService(db)
    before = await _repair_stage(db, "operation", 1)

    continued = await service.start("operation", current_head, "operation", now=150.0)
    replay = await service.start("operation", current_head, "operation", now=151.0)

    assert continued["outcome"] == replay["outcome"] == "already_started"
    assert continued["continued"] is True
    assert "continued" not in replay
    assert continued["stage"] == replay["stage"] == 1
    task = await db.get_task("repair-operation-1")
    assert task.status is TaskStatus.PAUSED
    assert "second-conflict" in task.description
    after = await _repair_stage(db, "operation", 1)
    for field in ("started_at", "deadline_at", "attempts", "policy", "deadline_event_id"):
        assert after[field] == before[field]
    assert after["starting_sha"] == current_head
    assert after["trigger_id"] == "second-conflict"
    assert after["current_subject"] == {
        "kind": "parent",
        "generation": 3,
        "head_sha": current_head,
    }
    assert after["dossier"]["budget"] == before["dossier"]["budget"]
    assert after["dossier"]["receipts"][0]["id"] == "first-repair-receipt"
    assert after["dossier"]["current_conflict"] == {
        "intent_id": "second-conflict",
        "source_task_id": "second-child",
        "source_head": "e" * 40,
        "source_base": "b" * 40,
        "expected_target": current_head,
        "diagnostics": {"paths": ["shared.py"]},
    }

    # This is the literal stage pinned in the already-running playbook artifact.
    dispatched = await service.dispatch("operation", 0)
    assert dispatched["outcome"] == "dispatched"
    assert dispatched["stage"] == 1
    assert dispatched["repair_task_id"] == "repair-operation-1"
    assert (await db.get_task("repair-operation-1")).status is TaskStatus.READY
    owner = await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert owner["owner_id"] == "repair-operation-1"
    assert owner["owner_role"] == "repair"
    assert owner["fence_token"] == 8

    # Replayed events from the first conflict cannot reopen or retake the writer.
    stale = await service.start(
        "operation", "b" * 40, "stage-exhausted:operation:0", now=152.0
    )
    assert stale["outcome"] == "stale"
    assert (await db.get_task("repair-operation-1")).status is TaskStatus.READY


@pytest.mark.parametrize(
    "blocker",
    ["multiple", "expired", "live_session", "workspace", "collector_owner", "fence"],
)
async def test_parent_repair_continuation_requires_one_detached_current_conflict(db, blocker):
    """Ambiguous or still-attached completion evidence never reopens the delegate."""
    from src.integration.repair import RepairService

    current_head = await _seed_repeated_parent_conflict(
        db, deadline_at=149.0 if blocker == "expired" else 300.0
    )
    async with db.immediate() as conn:
        if blocker == "multiple":
            await conn.execute(
                insert(integration_promotion_intents).values(
                    id="other-conflict",
                    domain_key="other-conflict-domain",
                    operation_key="operation",
                    project_id="p",
                    receipt_id="other-conflict-receipt",
                    source_task_id="first-child",
                    target_task_id="parent",
                    source_head="f" * 40,
                    source_base="b" * 40,
                    repository_id="repo",
                    target_branch="aq/parent",
                    expected_target=current_head,
                    fence_owner_id="operation",
                    fence_token=7,
                    state="conflict",
                    created_at=140.0,
                    updated_at=140.0,
                )
            )
        elif blocker == "workspace":
            await conn.execute(
                insert(workspaces).values(
                    id="leftover-workspace",
                    project_id="p",
                    workspace_path="/tmp/leftover-repair",
                    source_type="link",
                    locked_by_task_id="repair-operation-1",
                    enabled=True,
                    created_at=140.0,
                )
            )
        elif blocker in {"collector_owner", "fence"}:
            values = (
                {"owner_id": "replacement-collector"}
                if blocker == "collector_owner"
                else {"fence_token": 8}
            )
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.id == "continued-owner")
                .values(**values)
            )
    if blocker == "live_session":
        await db.create_session(
            SessionRecord(
                id="leftover-session",
                task_id="repair-operation-1",
                project_id="p",
                profile_id="debugger",
                harness="fake",
                provider="fake",
                name="leftover-repair",
                lifecycle="task",
                state="running",
                work_dir="/tmp/leftover-repair",
                epoch="epoch",
                instance_token="old-instance",
                started_at=140.0,
            )
        )

    service = RepairService(db)
    result = await service.start("operation", current_head, "operation", now=150.0)

    assert result["outcome"] == "stale"
    assert (await db.get_task("repair-operation-1")).status is TaskStatus.COMPLETED
    stage = await _repair_stage(db, "operation", 1)
    assert stage["trigger_id"] == "stage-exhausted:operation:0"
    assert stage["starting_sha"] == "b" * 40
    if blocker == "expired":
        expired = await service.expire("operation", 1, now=150.0)
        assert expired == {
            "outcome": "expired",
            "action": "block_for_human",
            "operation_id": "operation",
            "stage": 1,
        }
        assert (await db.get_integration_operation("operation"))["state"] == "human_required"
        async with db._engine.connect() as conn:
            event = (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.event_type == "integration.human_blocked"
                    )
                )
            ).mappings().one()
        assert event["payload"]["operation_id"] == "operation"


@pytest.mark.parametrize("corruption", ["policy", "checkpoint", "batch_revision"])
async def test_start_reports_corrupt_persisted_identity_as_invariant(db, corruption):
    """Corrupt frozen relationships have a deterministic public outcome."""
    from src.integration.repair import RepairService

    if corruption == "batch_revision":
        operation_id = await _seed_root_operation(db)
        async with db.immediate() as conn:
            await conn.execute(
                integration_candidate_revisions.delete().where(
                    integration_candidate_revisions.c.batch_id == "batch"
                )
            )
        trigger_id = "batch"
    else:
        await _seed_parent_operation(db)
        operation_id = "operation"
        trigger_id = "failed-check"
        async with db.immediate() as conn:
            if corruption == "policy":
                await conn.execute(
                    update(integration_repair_operations)
                    .where(integration_repair_operations.c.id == operation_id)
                    .values(policy_snapshot={})
                )
            else:
                await conn.execute(
                    task_integration_checkpoints.delete().where(
                        task_integration_checkpoints.c.task_id == "parent"
                    )
                )
    result = await RepairService(db).start(
        operation_id, STARTING_SHA, trigger_id, now=100.0
    )
    assert result == {"outcome": "invariant_error", "operation_id": operation_id}


async def test_parent_red_without_a_stage_opens_one_and_wakes_the_verifier(db):
    """A parent's first trusted red has no stage yet and must still land somewhere.

    Batches open stage 0 when the candidate is built and parents only on a merge
    conflict, so the first red aggregate reached ``record_result`` with no stage
    row, reported ``stale``, and let the playbook complete the run green while
    the held verifier waited on a head that could never verify.
    """
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await db.create_task(
        Task(
            id="verifier",
            project_id="p",
            title="Verify parent",
            description="",
            status=TaskStatus.IN_PROGRESS,
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "operation")
            .values(verifier_task_id="verifier")
        )
    service = RepairService(db)

    result = await service.record_result("operation", "failed-check", now=105.0)
    replay = await service.record_result("operation", "failed-check", now=106.0)

    assert result["outcome"] == "started"
    assert result["action"] == "stage_opened"
    assert result["stage"] == 0
    stage = await _repair_stage(db, "operation", 0)
    assert stage["state"] == "active"
    assert stage["starting_sha"] == STARTING_SHA
    assert stage["trigger_id"] == "failed-check"
    # The triggering red opens the stage exactly as a conflict does, so it
    # spends no attempt, and no writer is named until dispatch claims it.
    assert stage["attempts"] == 0
    assert stage["repair_task_id"] is None
    # A redelivery dedupes through the existing duplicate branch.
    assert replay["action"] == "duplicate"
    async with db._engine.connect() as conn:
        notes = (
            await conn.execute(select(messages).where(messages.c.to_id == "verifier"))
        ).mappings().all()
        links = (
            await conn.execute(select(integration_repair_stage_evidence))
        ).mappings().all()
    assert [note["to_kind"] for note in notes] == ["task"]
    assert STARTING_SHA in notes[0]["body"]
    assert [(link["evidence_id"], link["counted_attempt"]) for link in links] == [
        ("failed-check", False)
    ]


@pytest.mark.parametrize(
    ("generation", "head_sha"),
    [(2, STARTING_SHA), (3, "b" * 40)],
    ids=["superseded_generation", "superseded_head"],
)
async def test_parent_red_about_a_superseded_subject_stays_stale(db, generation, head_sha):
    """``stale`` is reserved for evidence the parent has already moved past."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db,
        "superseded",
        run_id="run-superseded",
        conclusion="failure",
        generation=generation,
        head_sha=head_sha,
    )

    result = await RepairService(db).record_result("operation", "superseded", now=105.0)

    assert result == {"outcome": "continue", "action": "stale", "attempts": 0}
    async with db._engine.connect() as conn:
        stages = (await conn.execute(select(integration_repair_stages))).mappings().all()
        notes = (await conn.execute(select(messages))).mappings().all()
    assert stages == []
    assert notes == []


async def test_record_result_counts_each_conclusive_run_attempt_once(db):
    """Duplicate and infrastructure evidence must not consume repair attempts."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await _add_parent_evidence(
        db,
        "infra-check",
        run_id="run-infra",
        conclusion="failure",
        classification="infrastructure",
    )

    first = await service.record_result("operation", "failed-check", now=105.0)
    duplicate = await service.record_result("operation", "failed-check", now=106.0)
    infrastructure = await service.record_result(
        "operation", "infra-check", now=107.0
    )

    assert first["outcome"] == "continue"
    assert first["action"] == "repair"
    assert first["attempts"] == 1
    assert duplicate == first | {"action": "duplicate"}
    assert infrastructure["outcome"] == "continue"
    assert infrastructure["action"] == "infrastructure_retry"
    assert infrastructure["attempts"] == 1
    async with db._engine.connect() as conn:
        links = (
            await conn.execute(
                select(integration_repair_stage_evidence).order_by(
                    integration_repair_stage_evidence.c.evidence_id
                )
            )
        ).mappings().all()
    assert [(row["evidence_id"], row["counted_attempt"]) for row in links] == [
        ("failed-check", True),
        ("infra-check", False),
    ]
async def test_primary_attempt_exhaustion_activates_one_debug_stage(db):
    """The final primary attempt must atomically advance, never restart stage zero."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db, "failed-check-2", run_id="run-2", conclusion="failure"
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="receipt-1",
                domain_key="parent-receipt-1",
                source_task_id=None,
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                disposition="noop",
                resolution_evidence={"reason": "no source diff"},
                parent_operation_id="operation",
                parent_episode_id="episode",
                created_at=4.0,
            )
        )
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await service.record_result("operation", "failed-check", now=105.0)

    exhausted = await service.record_result(
        "operation", "failed-check-2", now=110.0
    )
    replay = await service.record_result(
        "operation", "failed-check-2", now=111.0
    )

    assert exhausted == {
        "outcome": "escalate",
        "action": "dispatch_debug",
        "attempts": 2,
        "stage": 1,
    }
    assert replay == exhausted | {"action": "duplicate"}
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(integration_repair_stages)
                .where(integration_repair_stages.c.operation_id == "operation")
                .order_by(integration_repair_stages.c.ordinal)
            )
        ).mappings().all()
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.id == "operation"
                )
            )
        ).mappings().one()
    assert operation["active_stage"] == 1
    assert operation["state"] == "escalated"
    assert rows[0]["state"] == "failed"
    assert rows[0]["completed_at"] == 110.0
    assert rows[1]["state"] == "active"
    assert rows[1]["started_at"] == 110.0
    assert rows[1]["deadline_at"] == 170.0
    assert rows[1]["intelligence_class"] == "debug-high"
    # The stored policy still names ``debug_profile_id``; it is ignored.
    assert rows[0]["profile_id"] is None
    assert rows[1]["profile_id"] is None
    assert rows[1]["repair_task_id"] is None
    dossier = rows[1]["dossier"]
    assert dossier["previous_stage"]["attempts"] == 2
    assert dossier["manifest"] == {
        "kind": "parent_episode",
        "parent_task_id": "parent",
        "episode_id": "episode",
        "generation": 3,
    }
    assert dossier["branch_sha"] == STARTING_SHA
    assert dossier["receipts"] == [
        {
            "id": "receipt-1",
            "source_task_id": None,
            "disposition": "noop",
            "after_sha": None,
        }
    ]
    assert [item["evidence_id"] for item in dossier["failed_checks"]] == [
        "failed-check",
        "failed-check-2",
    ]
    assert [item["run_id"] for item in dossier["logs"]] == ["run-1", "run-2"]
    assert dossier["hypotheses"][-1]["classification"] == "conclusive"
    assert dossier["commands_attempted"][-1]["workflow_id"] == "workflow"
    assert dossier["previous_stage"]["dossier"]["budget"] == {
        "ordinal": 0,
        "started_at": 100.0,
        "deadline_at": 130.0,
        "attempt_limit": 2,
        "attempts": 2,
    }
    assert dossier["budget"] == {
        "ordinal": 1,
        "started_at": 110.0,
        "deadline_at": 170.0,
        "attempt_limit": 1,
        "attempts": 0,
    }


async def test_debug_exhaustion_blocks_only_parent_subtree_and_emits_stable_events(db):
    """The final debug failure is durable human escalation, not auto completion."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db, "failed-check-2", run_id="run-2", conclusion="failure"
    )
    await _add_parent_evidence(
        db, "debug-failed", run_id="run-debug", conclusion="failure"
    )
    await _add_parent_evidence(
        db, "after-budget", run_id="run-after-budget", conclusion="failure"
    )
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await service.record_result("operation", "failed-check", now=101.0)
    await service.record_result("operation", "failed-check-2", now=102.0)

    blocked = await service.record_result("operation", "debug-failed", now=103.0)
    replay = await service.record_result("operation", "debug-failed", now=104.0)
    exhausted = await service.record_result("operation", "after-budget", now=105.0)

    assert blocked == {
        "outcome": "human_required",
        "action": "block_for_human",
        "attempts": 1,
    }
    assert replay == blocked | {"action": "duplicate"}
    assert exhausted == {
        "outcome": "budget_exhausted",
        "action": "block_for_human",
        "attempts": 1,
    }
    assert (await db.get_task("parent")).status is TaskStatus.BLOCKED
    assert await db.get_task_meta("parent", "blocked_terminal") == (
        "integration_repair_exhausted"
    )
    async with db._engine.connect() as conn:
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.id == "operation"
                )
            )
        ).mappings().one()
        events = (
            await conn.execute(
                select(integration_outbox)
                .where(
                    integration_outbox.c.event_type.in_(
                        ("integration.repair_exhausted", "integration.human_blocked")
                    )
                )
                .order_by(integration_outbox.c.id)
            )
        ).mappings().all()
    assert operation["state"] == "human_required"
    assert [(row["event_type"], row["id"]) for row in events] == [
        ("integration.repair_exhausted", "repair-exhausted-operation-0"),
        ("integration.human_blocked", "repair-human-operation"),
    ]


async def test_primary_deadline_expires_without_any_ci_event(db):
    """A persisted absolute deadline must advance the ladder without CI traffic."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)

    early = await service.expire("operation", 0, now=129.0)
    expired = await service.expire("operation", 0, now=130.0)
    replay = await service.expire("operation", 0, now=131.0)

    assert early == {
        "outcome": "not_due",
        "action": "wait",
        "operation_id": "operation",
        "stage": 0,
    }
    assert expired == {
        "outcome": "expired",
        "action": "dispatch_debug",
        "operation_id": "operation",
        "stage": 1,
    }
    assert replay == {
        "outcome": "already_terminal",
        "action": "dispatch_debug",
        "operation_id": "operation",
        "stage": 1,
    }


async def test_exhaustion_continues_with_fresh_bounded_stages_and_deduped_workers(db):
    from src.integration.repair import RepairService

    policy = _policy()
    policy["parent"]["repair"]["on_exhausted"] = "continue"
    await _seed_parent_operation(db, policy=policy)
    ownership = BranchOwnership(db)
    await ownership.acquire(BranchKey(repository_id="repo", branch="aq/parent"), "operation", "collector")
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    first = await service.dispatch("operation", 0)
    assert first["outcome"] == "dispatched"
    for ordinal, due in ((0, 130.0), (1, 190.0), (2, 250.0)):
        # Continuous work may renew a bounded stage after authoritative
        # progress; worker turnover (or a writer nobody claimed) cannot.
        head = str(ordinal + 1) * 40
        previous = await _repair_stage(db, "operation", ordinal)
        async with db.immediate() as conn:
            await service.bind_current_parent_subject_on(
                conn, "operation", head_sha=head, now=due - 1,
                commit_proof={"base_sha": previous["current_subject"]["head_sha"],
                              "head_sha": head, "commits": [head]},
            )
        expired = await service.expire("operation", ordinal, now=due)
        assert expired["action"] == "dispatch_debug"
        assert expired["stage"] == ordinal + 1
        replay_timeout = await service.expire("operation", ordinal, now=due + 1)
        assert replay_timeout["action"] == "dispatch_debug"
        assert replay_timeout["stage"] == ordinal + 1
        terminal = await _repair_stage(db, "operation", ordinal)
        assert terminal["state"] == "expired" and terminal["completed_at"] == due
        successor = await _repair_stage(db, "operation", ordinal + 1)
        assert successor["started_at"] == due and successor["deadline_at"] == due + 60
        assert successor["attempts"] == 0
        dispatched = await service.dispatch("operation", ordinal + 1)
        assert dispatched["outcome"] == "dispatched", dispatched
        assert dispatched["repair_task_id"] != first["repair_task_id"]
        replay = await service.dispatch("operation", ordinal + 1)
        assert replay["outcome"] == "already_dispatched"
        assert replay["repair_task_id"] == dispatched["repair_task_id"]
        first = dispatched
    operation = await db.get_integration_operation("operation")
    assert operation["active_stage"] == 3 and operation["state"] == "escalated"


@pytest.mark.parametrize("exhaustion", ["timeout", "failure", "closed_writer", "generation_only"])
async def test_continuous_unchanged_head_stops_once_with_supervisor_dossier(db, exhaustion):
    from src.integration.repair import RepairService

    policy = _policy()
    policy["parent"]["repair"]["on_exhausted"] = "continue"
    await _seed_parent_operation(db, policy=policy)
    ownership = BranchOwnership(db)
    await ownership.acquire(BranchKey(repository_id="repo", branch="aq/parent"),
                            "operation", "collector")
    service = RepairService(db, clock=lambda: 150.0)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    primary = await service.dispatch("operation", 0)
    # The primary writer gave up (a closed writer escalates on the clock).
    await db.transition_task(primary["repair_task_id"], TaskStatus.FAILED, force=True)
    await service.expire("operation", 0, now=130.0)
    delegate = await service.dispatch("operation", 1)
    if exhaustion == "generation_only":
        async with db.immediate() as conn:
            await conn.execute(update(task_integration_checkpoints)
                               .where(task_integration_checkpoints.c.task_id == "parent")
                               .values(generation=4))
            await service.bind_current_parent_subject_on(
                conn, "operation", head_sha=STARTING_SHA, now=150.0,
            )
    if exhaustion == "failure":
        await _add_parent_evidence(db, "debug-red", run_id="debug-run", conclusion="failure")
        result = await service.record_result("operation", "debug-red", now=150.0)
        assert result["outcome"] == "budget_exhausted"
        assert result["action"] == "supervisor_recovery"
        replay = await service.record_result("operation", "debug-red", now=151.0)
        assert replay["outcome"] == "budget_exhausted" and replay["action"] == "duplicate"
    elif exhaustion == "closed_writer":
        await db.transition_task(delegate["repair_task_id"], TaskStatus.COMPLETED, force=True)
        assert (await service.dispatch("operation", 1))["outcome"] == "stale"
    else:
        # A writer that closed without moving the head ends the budget.
        await db.transition_task(delegate["repair_task_id"], TaskStatus.FAILED, force=True)
        result = await service.expire("operation", 1, now=190.0)
        assert result["stage"] == 1 and result["action"] == "supervisor_recovery"
    for _ in range(3):
        assert (await service.expire("operation", 1, now=200.0))["action"] == "supervisor_recovery"
        assert (await service.dispatch("operation", 1))["outcome"] == "stale"
        assert await service.pending_dispatches() == []
        assert await service.due_stages(now=300.0) == []
    operation = await db.get_integration_operation("operation")
    assert operation["active_stage"] == 1 and operation["state"] == "escalated"
    stage = await _repair_stage(db, "operation", 1)
    assert stage["deadline_at"] == 190.0
    assert stage["dossier"]["supervisor_recovery"]["subject"]["head_sha"] == STARTING_SHA
    assert (await db.get_task("parent")).status == TaskStatus.PAUSED
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_repair_stages.c.ordinal)
                .order_by(integration_repair_stages.c.ordinal))).scalars().all() == [0, 1]
        notices = (await conn.execute(select(messages).where(
            messages.c.body_kind == "integration_repair_no_progress",
        ))).mappings().all()
        assert len(notices) == 1
        assert "operation" in notices[0]["body"] and delegate["repair_task_id"] in notices[0]["body"]
        owner = (await conn.execute(select(integration_branch_owners))).mappings().one()
        assert owner["owner_id"] == delegate["repair_task_id"]
        assert owner["handoff_state"] == "reserved"


async def _continuous_parent_stage(db, *, owner_recovery=None, starting_sha=STARTING_SHA):
    """A continuing parent ladder at stage 0 with its delegate dispatched (deadline 130)."""
    from src.integration.repair import RepairService

    policy = _policy()
    policy["parent"]["repair"]["on_exhausted"] = "continue"
    await _seed_parent_operation(db, policy=policy, starting_sha=starting_sha)
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"), "operation", "collector"
    )
    service = RepairService(db, clock=lambda: 150.0, owner_recovery=owner_recovery)
    await service.start("operation", starting_sha, "failed-check", now=100.0)
    dispatched = await service.dispatch("operation", 0)
    assert dispatched["outcome"] == "dispatched"
    return service, dispatched["repair_task_id"]


async def _closed_green_parent(db, *, ordinal=1, head=STARTING_SHA, escalated=False, evidence_values=None):
    """A PASS no-op delegate with newer exact-head green over an earlier red."""
    service, delegate = await _continuous_parent_stage(db, starting_sha=head)
    original = await _repair_stage(db, "operation", 0)
    subject = {"kind": "parent", "generation": 3, "head_sha": head}
    dossier = dict(original["dossier"])
    dossier["repair_commits"] = [head]
    dossier["branch_sha"] = head
    if escalated:
        dossier["supervisor_recovery"] = {
            "incident_id": f"repair-no-progress:operation:{ordinal}",
            "subject": subject, "recorded_at": 135.0,
        }
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == delegate).values(
            status="COMPLETED", assigned_agent_id=None, claim_epoch=1,
        ))
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "parent"
        ).values(checkpoint_sha=head))
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == "operation",
            integration_repair_stages.c.ordinal == 0,
        ).values(state="expired", repair_task_id=None, writer_kind=None,
                 dossier=dict(original["dossier"]) | {"repair_commits": [head] if ordinal == 1 else []}))
        if ordinal == 2:
            await conn.execute(insert(integration_repair_stages).values(
                **(original | {"ordinal": 1, "current_subject": subject,
                    "deadline_event_id": "repair-deadline-operation-1", "state": "expired",
                    "repair_task_id": None, "writer_kind": None,
                    "dossier": dict(original["dossier"]) | {"repair_commits": [head]}})
            ))
        await conn.execute(insert(integration_repair_stages).values(
            **(original | {"ordinal": ordinal, "current_subject": subject,
                          "deadline_event_id": f"repair-deadline-operation-{ordinal}",
                          "state": "expired" if escalated else "active", "dossier": dossier})
        ))
        await conn.execute(update(integration_branch_owners).values(fence_token=3))
        await conn.execute(insert(task_completion_records).values(
            id="noop-completion", task_id=delegate, outcome="pass", branch="aq/parent",
            commits="[]", completed_at=124.0,
        ))
        await conn.execute(insert(task_metadata).values(
            task_id=delegate, key="accepted_close", value=json.dumps({
                "completion_id": "noop-completion", "session_id": "closing-session", "claim_epoch": 1,
            }),
        ))
        await conn.execute(insert(integration_outbox).values(
            id="noop-close-audit", dedup_key="noop-close-audit", project_id="p",
            event_type="integration.repair_delegate_closed", created_at=123.0,
            available_at=123.0, payload={"operation_id": "operation", "stage": ordinal,
                "task_id": delegate, "session_id": "closing-session", "instance_token": "instance",
                "workspace_id": "closing-workspace", "fence_token": 2},
        ))
        await conn.execute(update(integration_repair_operations).where(
            integration_repair_operations.c.id == "operation"
        ).values(active_stage=ordinal, state="escalated" if escalated else "active"))
    async with db.immediate() as conn:
        await conn.execute(insert(integration_check_evidence).values(**({
            "id": "green-noop", "operation_id": "operation", "parent_task_id": "parent",
            "parent_generation": 3, "parent_head_sha": head, "producer_id": "forge",
            "workflow_id": "workflow", "run_id": "green-run", "attempt": 1,
            "required_check_version": "checks-v1", "checks": {"unit": "success"},
            "classification": "conclusive", "conclusion": "success", "observed_at": 125.0,
        } | (evidence_values or {}))))
    service._promotion = SimpleNamespace(
        _resolve_repository=AsyncMock(return_value=SimpleNamespace(
            retained_git_dir="/tmp/retained", origin_url="/tmp/origin", repo=SimpleNamespace(default_branch="main"))),
        _ensure_retained_repository=AsyncMock(),
        git=SimpleNamespace(als_remote_ref=AsyncMock(return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=head))),
    )
    return service, delegate, subject


async def _apply_noop_preview(service, preview):
    return await service.reevaluate(
        preview["operation_id"], dry_run=False, expected_subject=preview["subject"],
        expected_episode_id=preview["episode_id"], expected_generation=preview["generation"],
        expected_stage=preview["stage"], expected_fence_token=preview["fence_token"],
        expected_snapshot_digest=preview["snapshot_digest"],
        reason="Reviewed exact no-op repair",
    )


@pytest.mark.parametrize("ordinal,head", [
    (1, "37eec020a93d30149f2cecaba5dbe5772d198389"),
    (2, "c77247f6ecc780f77672dff106f7ed47350a279b"),
])
@pytest.mark.parametrize("entry", ["dispatch", "expiry", "recovery"])
async def test_completed_noop_on_exact_green_head_passes_without_no_progress(db, ordinal, head, entry):
    service, delegate, subject = await _closed_green_parent(
        db, ordinal=ordinal, head=head, escalated=entry == "recovery",
    )
    before = await _repair_stage(db, "operation", ordinal)
    if entry == "dispatch":
        result = await service.dispatch("operation", ordinal)
        assert result["reason"] == "trusted_green_subject"
    elif entry == "expiry":
        result = await service.expire("operation", ordinal, now=150.0)
        assert result["action"] == "none"
    else:
        preview = await service.reevaluate("operation")
        assert preview["outcome"] == "would_reevaluate", preview
        assert await _repair_stage(db, "operation", ordinal) == before
        result = await _apply_noop_preview(service, preview)
        assert result["outcome"] == "reevaluated"
    stage = await _repair_stage(db, "operation", ordinal)
    assert (stage["state"], stage["attempts"], stage["deadline_at"]) == (
        "passed", 1, before["deadline_at"],
    )
    assert stage["dossier"]["green_subject_verification"]["evidence_ids"] == ["green-noop"]
    assert stage["dossier"]["completed_delegate_attempts"] == [
        {"task_id": delegate, "claim_epoch": 1, "recorded_at": 150.0},
    ]
    assert (await db.get_integration_operation("operation"))["state"] == "active"
    assert (await db.get_integration_operation("operation"))["verifier_task_id"]
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED
    assert await _ordinals(db) == list(range(ordinal + 1))
    assert await _notices(db, "integration_repair_no_progress") == []
    if entry == "recovery":
        assert stage["dossier"]["supervisor_recovery"] == before["dossier"]["supervisor_recovery"]
    assert (await service.reevaluate("operation", expected_subject=subject))["outcome"] == "already_settled"
    assert (await _repair_stage(db, "operation", ordinal))["attempts"] == 1


@pytest.mark.parametrize("recorded,settles", [
    ([], True),
    ([STARTING_SHA], True),
    (["f" * 40], False),
    ([STARTING_SHA, "f" * 40], False),
])
async def test_noop_settlement_settles_a_close_that_named_the_unchanged_head(db, recorded, settles):
    service, _delegate, subject = await _closed_green_parent(db, escalated=True)
    async with db.immediate() as conn:
        await conn.execute(update(task_completion_records).values(
            commits=json.dumps(recorded)))
    if not settles:
        before = await _repair_stage(db, "operation", 1)
        assert (await service.reevaluate("operation", expected_subject=subject))["outcome"] == "blocked"
        assert await _repair_stage(db, "operation", 1) == before
        return
    preview = await service.reevaluate("operation")
    assert preview["outcome"] == "would_reevaluate", preview
    assert (await _apply_noop_preview(service, preview))["outcome"] == "reevaluated"
    stage = await _repair_stage(db, "operation", 1)
    assert stage["state"] == "passed"
    proof = stage["dossier"]["green_subject_verification"]["close_proof"]
    assert proof["completion_id"] == "noop-completion"
    assert proof["completion"]["commits"] == json.dumps(recorded)
    assert (await db.get_integration_operation("operation"))["verifier_task_id"]
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED


@pytest.mark.parametrize("invalid", [
    "missing", "wrong_head", "wrong_generation", "wrong_producer", "wrong_version",
    "partial", "infrastructure", "newer_red", "cancelled", "held", "human_gate",
    "live_writer", "canonical_head_moved", "not_completed", "unresolved_write",
    "incomplete_audit", "incomplete_accepted_close", "nonpass", "authored_completion",
    "committed_and_authored", "wrong_dossier_head",
    "introduced_commit", "remote_moved", "locked_workspace", "live_session", "other_incident",
])
async def test_noop_recovery_refuses_incomplete_stale_or_blocked_green(db, invalid):
    from src.database.tables import gates, task_gates

    values = {
        "missing": {"checks": {"unit": "missing"}},
        "wrong_head": {"parent_head_sha": "d" * 40},
        "wrong_generation": {"parent_generation": 2},
        "wrong_producer": {"producer_id": "untrusted"},
        "wrong_version": {"required_check_version": "old"},
        "partial": {"checks": {}},
        "infrastructure": {"classification": "infrastructure"},
        "newer_red": {"conclusion": "failure", "checks": {"unit": "failure"}},
        "cancelled": {"conclusion": "cancelled", "checks": {"unit": "cancelled"}},
    }
    service, delegate, subject = await _closed_green_parent(
        db, escalated=True, evidence_values=values.get(invalid),
    )
    async with db.immediate() as conn:
        if invalid == "held":
            await conn.execute(insert(task_metadata).values(
                task_id="parent", key="manual_pause", value="operator hold",
            ))
        if invalid == "human_gate":
            await conn.execute(insert(gates).values(
                id="hold", project_id="p", gate_type="human", title="Decision", created_at=1.0,
            ))
            await conn.execute(insert(task_gates).values(task_id="parent", gate_id="hold"))
        if invalid == "live_writer":
            await conn.execute(update(integration_branch_owners).values(handoff_state="handoff_pending"))
        if invalid == "canonical_head_moved":
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent"
            ).values(checkpoint_sha="e" * 40))
        if invalid == "not_completed":
            await conn.execute(update(tasks).where(tasks.c.id == delegate).values(status="READY"))
        if invalid == "unresolved_write":
            await conn.execute(insert(integration_promotion_intents).values(
                id="pending", domain_key="pending", operation_key="operation", project_id="p",
                receipt_id="pending-receipt", source_task_id="child", target_task_id="parent",
                source_head="b" * 40, source_base="c" * 40, repository_id="repo",
                target_branch="aq/parent", expected_target=STARTING_SHA,
                fence_owner_id=delegate, fence_token=2, state="conflict", created_at=1.0, updated_at=1.0,
            ))
        if invalid == "incomplete_audit":
            await conn.execute(update(integration_outbox).where(
                integration_outbox.c.id == "noop-close-audit"
            ).values(payload={"operation_id": "operation", "stage": 1, "task_id": delegate}))
        if invalid == "incomplete_accepted_close":
            await conn.execute(update(task_metadata).where(task_metadata.c.key == "accepted_close")
                               .values(value="{}"))
        if invalid == "nonpass":
            await conn.execute(update(task_completion_records).values(outcome="fail"))
        if invalid == "authored_completion":
            await conn.execute(update(task_completion_records).values(
                commits=json.dumps(["f" * 40])))
        if invalid == "committed_and_authored":
            await conn.execute(update(task_completion_records).values(
                commits=json.dumps([STARTING_SHA, "f" * 40])))
        if invalid == "wrong_dossier_head":
            changed = dict((await _repair_stage(db, "operation", 1))["dossier"])
            changed["branch_sha"] = "f" * 40
            await conn.execute(update(integration_repair_stages).where(
                integration_repair_stages.c.ordinal == 1
            ).values(dossier=changed))
        if invalid in {"introduced_commit", "other_incident"}:
            changed = dict((await _repair_stage(db, "operation", 1))["dossier"])
            if invalid == "introduced_commit":
                changed["repair_commits"] = [STARTING_SHA, "f" * 40]
            else:
                changed["supervisor_recovery"] = {"incident_id": "other-incident"}
            await conn.execute(update(integration_repair_stages).where(
                integration_repair_stages.c.ordinal == 1
            ).values(dossier=changed))
        if invalid == "locked_workspace":
            await conn.execute(insert(workspaces).values(
                id="locked", project_id="p", workspace_path="/tmp/locked", source_type="link",
                locked_by_task_id=delegate, enabled=True, created_at=1.0,
            ))
    if invalid == "live_session":
        await db.create_session(SessionRecord(
            id="still-live", task_id=delegate, project_id="p", profile_id="repairer", harness="fake",
            provider="fake", name="still-live", lifecycle="task", state="running", work_dir="/tmp/still-live",
            epoch="epoch", instance_token="live", started_at=120.0,
        ))
    if invalid == "remote_moved":
        service._promotion.git.als_remote_ref.return_value = RemoteRefResult(RemoteRefState.PRESENT, oid="f" * 40)
    before = await _repair_stage(db, "operation", 1)
    result = await service.reevaluate("operation", expected_subject=subject)
    assert result["outcome"] == "blocked"
    assert await _repair_stage(db, "operation", 1) == before
    assert (await db.get_integration_operation("operation"))["state"] == "escalated"


async def test_noop_recovery_requires_green_coverage_across_latest_workflows(db):
    service, _delegate, subject = await _closed_green_parent(db, escalated=True)
    operation = await db.get_integration_operation("operation")
    policy = dict(operation["policy_snapshot"])
    policy["parent"]["required_checks"]["names"] = ["unit", "lint"]
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(policy_snapshot=policy))
    assert (await service.reevaluate("operation", expected_subject=subject))["outcome"] == "blocked"
    async with db.immediate() as conn:
        await conn.execute(insert(integration_check_evidence).values(
            id="green-lint", operation_id="operation", parent_task_id="parent", parent_generation=3,
            parent_head_sha=STARTING_SHA, producer_id="forge", workflow_id="lint-workflow",
            run_id="lint-run", attempt=1, required_check_version="checks-v1",
            checks={"lint": "success"}, conclusion="success", classification="conclusive", observed_at=126.0,
        ))
    preview = await service.reevaluate("operation")
    accepted = await _apply_noop_preview(service, preview)
    assert accepted["outcome"] == "reevaluated", accepted
    assert accepted["evidence_ids"] == ["green-lint", "green-noop"]


async def test_noop_recovery_accepts_the_same_full_and_short_branch_ref(db):
    service, delegate, _subject = await _closed_green_parent(db, escalated=True)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == delegate).values(
            branch_name="refs/heads/aq/parent",
        ))
        await conn.execute(update(task_completion_records).values(branch="refs/heads/aq/parent"))
    preview = await service.reevaluate("operation")
    assert preview["outcome"] == "would_reevaluate", preview
    assert (await _apply_noop_preview(service, preview))["outcome"] == "reevaluated"


async def test_completed_delegate_is_counted_once_even_without_green_ci(db):
    service, delegate = await _continuous_parent_stage(db)
    await db.transition_task(delegate, TaskStatus.COMPLETED, force=True)
    result = await service.dispatch("operation", 0)
    assert result["stage"] == 1
    primary = await _repair_stage(db, "operation", 0)
    assert primary["attempts"] == 1
    assert primary["dossier"]["budget"]["attempts"] == 1
    await service.expire("operation", 0, now=200.0)
    assert (await _repair_stage(db, "operation", 0))["attempts"] == 1


@pytest.mark.parametrize("changed", ["head", "fence", "episode", "stage", "audit", "red"])
async def test_noop_apply_reproves_preview_and_refuses_movement(db, changed):
    service, _delegate, _subject = await _closed_green_parent(db, escalated=True)
    preview = await service.reevaluate("operation")
    assert preview["outcome"] == "would_reevaluate", preview
    async with db.immediate() as conn:
        if changed == "head":
            await conn.execute(update(task_integration_checkpoints).values(checkpoint_sha="f" * 40))
        elif changed == "fence":
            await conn.execute(update(integration_branch_owners).values(fence_token=4))
        elif changed == "episode":
            await conn.execute(insert(integration_parent_episodes).values(
                id="other-episode", parent_task_id="parent", repository_id="repo", generation=4,
                pre_collection_checkpoint_sha=STARTING_SHA, created_at=130.0,
            ))
            await conn.execute(update(integration_repair_operations).values(episode_id="other-episode"))
        elif changed == "stage":
            current = await _repair_stage(db, "operation", 1)
            await conn.execute(insert(integration_repair_stages).values(**(current | {
                "ordinal": 2, "deadline_event_id": "repair-deadline-operation-2",
            })))
            await conn.execute(update(integration_repair_operations).values(active_stage=2))
        elif changed == "audit":
            await conn.execute(update(integration_outbox).where(
                integration_outbox.c.id == "noop-close-audit",
            ).values(created_at=123.5))
        else:
            await conn.execute(insert(integration_check_evidence).values(
                id="late-red", operation_id="operation", parent_task_id="parent", parent_generation=3,
                parent_head_sha=STARTING_SHA, producer_id="forge", workflow_id="workflow",
                run_id="late-red", attempt=1, required_check_version="checks-v1",
                checks={"unit": "failure"}, conclusion="failure", classification="conclusive", observed_at=140.0,
            ))
    before = await _repair_stage(db, "operation", 1)
    result = await _apply_noop_preview(service, preview)
    assert result["outcome"] in {"stale", "blocked"}, result
    assert await _repair_stage(db, "operation", 1) == before


@pytest.mark.parametrize("hold", ["manual", "gate", "workspace", "session"])
async def test_noop_recovery_preserves_holds_on_previous_delegates(db, hold):
    from src.database.tables import gates, task_gates

    service, _delegate, _subject = await _closed_green_parent(db, escalated=True)
    await db.create_task(Task(id="earlier-delegate", project_id="p", title="Earlier repair", description="",
                              status=TaskStatus.COMPLETED))
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.ordinal == 0,
        ).values(repair_task_id="earlier-delegate", writer_kind="repair_delegate"))
        if hold == "manual":
            await conn.execute(insert(task_metadata).values(
                task_id="earlier-delegate", key="manual_pause", value="hold",
            ))
        elif hold == "gate":
            await conn.execute(insert(gates).values(
                id="earlier-gate", project_id="p", gate_type="human", title="Hold", created_at=1.0,
            ))
            await conn.execute(insert(task_gates).values(task_id="earlier-delegate", gate_id="earlier-gate"))
        elif hold == "workspace":
            await conn.execute(insert(workspaces).values(
                id="earlier-slot", project_id="p", workspace_path="/tmp/earlier", source_type="link",
                locked_by_task_id="earlier-delegate", enabled=True, created_at=1.0,
            ))
    if hold == "session":
        await db.create_session(SessionRecord(
            id="earlier-session", task_id="earlier-delegate", project_id="p", profile_id="repairer",
            harness="fake", provider="fake", name="earlier", lifecycle="task", state="running", work_dir="/tmp/earlier",
            epoch="epoch", instance_token="earlier-instance", started_at=120.0,
        ))
    assert (await service.reevaluate("operation"))["outcome"] == "blocked"


async def _drained_parent_holder(db):
    """A pool worker stopped for good, its claim on the parent left behind.

    Built by driving the writers that produce the live shape (fleet-delta-97):
    ``record_holder``/``activate_claim`` write the claim record the exact-holder
    proof now reads, then the pool's own drain — ``terminate_pool_session``
    with the task's status — clears the session's ``task_id`` and ``claim_phase``
    and hands the parent back ``IN_PROGRESS`` with no agent, and the teardown's
    last statement marks the row ``stopped``/``stopped``.  A session row pointing
    back at the parent is not what a real stop leaves, which is exactly why the
    proof used to match nothing.
    """
    await db.create_agent(
        Agent(id="drained-parent-agent", name="drained-parent-agent", profile_id="worker")
    )
    await db.create_session(SessionRecord(
        id="stale-parent-holder", task_id=None, project_id="p", profile_id="worker",
        harness="fake", provider="fake", name="drained-parent", lifecycle="pool",
        state="running", desired_state="running", work_dir="/tmp/drained-parent",
        epoch="epoch", instance_token="drained-instance", started_at=100.0,
        agent_id="drained-parent-agent",
    ))
    async with db.immediate() as conn:
        await db.record_holder(
            conn, session_id="stale-parent-holder", task_id="parent", claim_epoch=1,
            agent_id="drained-parent-agent", work_dir="/tmp/drained-parent", now=101.0,
            agent_reserved=True,
        )
    assert await db.activate_claim("stale-parent-holder", "parent", epoch=1, now=102.0)
    assert (
        await db.terminate_pool_session(
            "stale-parent-holder", reason="drained", task_status=TaskStatus.IN_PROGRESS
        )
    ).released
    await db.update_session(
        "stale-parent-holder", state="stopped", desired_state="stopped",
        ended_at=120.0, end_reason="drained",
    )


async def _stale_parent_noop(db, *, collector_fence=True):
    """A closed green no-op parent whose stopped worker still holds its claim.

    *collector_fence* is the one difference between the two live shapes.  True is
    ``sharp-glacier-30``: the branch fence is the operation's own collector
    reservation, which ``stale_parent_claim_on`` accepts.  False is
    ``calm-quest-88``: the fence is the closed delegate's reserved **repair**
    row, so that proof refuses the parent at its collector-role check.
    """
    service, delegate, subject = await _closed_green_parent(db, escalated=True)
    await db.create_task(Task(id="terminal-child", project_id="p", title="Completed no-op", description="",
                             parent_task_id="parent", status=TaskStatus.COMPLETED))
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(
            status="IN_PROGRESS", claim_epoch=1,
        ))
    await _drained_parent_holder(db)
    service._owner_recovery = SimpleNamespace(confirm_stopped=AsyncMock(return_value=True))
    async with db.immediate() as conn:
        if collector_fence:
            await conn.execute(update(integration_branch_owners).values(
                owner_id="operation", owner_role="collector",
            ))
        else:
            await conn.execute(update(integration_branch_owners).values(
                owner_id=delegate, owner_role="repair", handoff_state="reserved",
            ))
        await conn.execute(insert(task_branch_origins).values(
            id="child-origin", task_id="terminal-child", repository_id="repo",
            parent_task_id="parent", parent_repository_id="repo", parent_ref="aq/parent",
            base_sha=STARTING_SHA, creation_generation=3, created_at=1.0,
        ))
        await conn.execute(insert(integration_child_dispositions).values(
            parent_task_id="parent", child_task_id="terminal-child", revision=0,
            disposition="noop", parent_operation_id="operation", parent_episode_id="episode",
            updated_at=2.0,
        ))
        await conn.execute(insert(task_delivery_receipts).values(
            id="child-noop", domain_key="child-noop", source_task_id="terminal-child",
            target_task_id="parent", repository_id="repo", target_branch="aq/parent",
            before_sha=STARTING_SHA, after_sha=STARTING_SHA, review_evidence={},
            parent_operation_id="operation", parent_episode_id="episode", disposition="noop",
            disposition_revision=0, resolution_evidence={"reason": "No change required"},
            verification_evidence={"checks": "passed"}, created_at=2.0,
        ))
    return service, delegate, subject


async def test_noop_recovery_releases_exact_stopped_parent_claim_before_fresh_verifier(db):
    service, _delegate, _subject = await _stale_parent_noop(db)
    after_release = db._after_release

    async def committed_release(transition):
        assert (await db.get_session("stale-parent-holder")).task_id is None
        assert (await db.get_task("parent")).status is TaskStatus.PAUSED
        assert (await db.get_integration_operation("operation"))["verifier_task_id"] == "verify-operation"
        await after_release(transition)

    db._after_release = AsyncMock(side_effect=committed_release)
    preview = await service.reevaluate("operation")
    assert preview["outcome"] == "would_reevaluate", preview
    assert preview["planned_steps"][0] == "release_stale_parent_claim_to_paused_collection"
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS
    # A refused proof may not have touched the claim it refused to prove.
    assert await db.get_task_meta("parent", "claimed_by_session") == "stale-parent-holder"
    result = await _apply_noop_preview(service, preview)
    assert result["outcome"] == "reevaluated", result
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED
    holder = await db.get_session("stale-parent-holder")
    assert holder.task_id is None and holder.claim_phase is None
    assert holder.state == "stopped" and holder.end_reason == "drained"
    assert await db.get_task_meta("parent", "claimed_by_session") is None
    assert (await db.get_integration_operation("operation"))["verifier_task_id"] == "verify-operation"
    service._owner_recovery.confirm_stopped.assert_awaited()
    assert all(call.args[0]["instance_token"] == "drained-instance"
               for call in service._owner_recovery.confirm_stopped.await_args_list)
    assert (await _apply_noop_preview(service, preview))["outcome"] == "already_settled"
    assert (await _repair_stage(db, "operation", 1))["attempts"] == 1
    db._after_release.assert_awaited_once()


@pytest.mark.parametrize("invalid", ["running", "epoch", "instance", "provider", "provider_error",
                                    "workspace", "owner", "child"])
async def test_noop_recovery_refuses_unproved_stale_parent_claim(db, invalid):
    service, _delegate, _subject = await _stale_parent_noop(db)
    async with db.immediate() as conn:
        if invalid in {"running", "epoch", "instance"}:
            values = {"running": {"state": "running"}, "epoch": {"last_claim_epoch": 2},
                      "instance": {"instance_token": ""}}[invalid]
            await conn.execute(update(sessions).where(sessions.c.id == "stale-parent-holder").values(**values))
        elif invalid == "workspace":
            await conn.execute(insert(workspaces).values(
                id="stale-slot", project_id="p", workspace_path="/tmp/drained-parent",
                source_type="link", enabled=True, created_at=1.0,
            ))
        elif invalid == "owner":
            await conn.execute(update(integration_branch_owners).values(owner_id="different-operation"))
        elif invalid == "child":
            await conn.execute(update(tasks).where(tasks.c.id == "terminal-child").values(status="READY"))
    if invalid == "provider":
        service._owner_recovery.confirm_stopped.return_value = False
    elif invalid == "provider_error":
        service._owner_recovery.confirm_stopped.side_effect = RuntimeError("provider unavailable")
    before = await _repair_stage(db, "operation", 1)
    preview = await service.reevaluate("operation")
    assert preview["outcome"] == "blocked", preview
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS
    # A refused proof may not have touched the claim it refused to prove.
    assert await db.get_task_meta("parent", "claimed_by_session") == "stale-parent-holder"
    assert await _repair_stage(db, "operation", 1) == before


async def test_noop_apply_refuses_stale_parent_instance_movement(db):
    service, _delegate, _subject = await _stale_parent_noop(db)
    preview = await service.reevaluate("operation")
    assert preview["outcome"] == "would_reevaluate", preview
    async with db.immediate() as conn:
        await conn.execute(update(sessions).where(sessions.c.id == "stale-parent-holder").values(
            instance_token="replacement-instance",
        ))
    result = await _apply_noop_preview(service, preview)
    assert result["outcome"] == "stale", result
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS
    # A refused proof may not have touched the claim it refused to prove.
    assert await db.get_task_meta("parent", "claimed_by_session") == "stale-parent-holder"


async def test_noop_recovery_preserves_old_verifier_and_files_a_fresh_one(db):
    service, _delegate, _subject = await _closed_green_parent(db, escalated=True)
    await db.create_task(Task(id="verify-operation", project_id="p", title="Old verifier", description="",
                              status=TaskStatus.FAILED))
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(verifier_task_id="verify-operation"))
    preview = await service.reevaluate("operation")
    result = await _apply_noop_preview(service, preview)
    assert result["outcome"] == "reevaluated", result
    assert (await db.get_task("verify-operation")).status is TaskStatus.FAILED
    assert (await db.get_integration_operation("operation"))["verifier_task_id"] == "verify-operation-g3"
    assert (await _apply_noop_preview(service, preview))["outcome"] == "already_settled"


async def test_a_repair_fenced_stale_parent_needs_no_widened_collector_check(db):
    """calm-quest-88: the no-op recovery must not be widened to reach this parent.

    The same parent as
    ``test_noop_recovery_releases_exact_stopped_parent_claim_before_fresh_verifier``,
    but its branch fence is the closed delegate's reserved ``repair`` row rather
    than the operation's own ``collector`` reservation.  ``reevaluate-repair``
    refuses that parent at ``stale_parent_claim_on``'s collector-role check
    ("parent is not an unassigned aggregate under this operation's collector
    fence"), so this state is unreachable from that control on its own.

    It is reachable from the shipped stale-claim release instead, and the check
    is *not* widened to meet it: the release returns the parent to unassigned
    PAUSED on the exact-holder proof alone, and ``stale_parent_claim_on``
    short-circuits on that state before it ever looks at the fence.  The check
    keeps refusing a parent that is still IN_PROGRESS under a repair fence.
    """
    service, _delegate, _subject = await _stale_parent_noop(db, collector_fence=False)
    blocked = await service.reevaluate("operation")
    assert blocked["outcome"] == "blocked"
    assert "collector fence" in blocked["reason"]

    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS
    assert "parent" in await db.stale_container_claim_candidates()

    assert (await db.release_stale_container_claim("parent")).released

    parent = await db.get_task("parent")
    assert (parent.status, parent.assigned_agent_id) == (TaskStatus.PAUSED, None)
    assert (await db.get_session("stale-parent-holder")).task_id is None
    preview = await service.reevaluate("operation")
    assert preview["outcome"] == "would_reevaluate", preview
    assert (await _apply_noop_preview(service, preview))["outcome"] == "reevaluated"
    assert (await db.get_integration_operation("operation"))["verifier_task_id"] == "verify-operation"


async def _claimed_writer(db, task_id: str, *, epoch: int, live: bool, sid: str) -> None:
    """A writer that claimed *task_id*; ``live=False`` is one that died without closing."""
    await db.create_session(SessionRecord(
        id=sid, task_id=task_id, project_id="p", profile_id="repairer", harness="fake",
        provider="fake", name=sid, lifecycle="task", state="running",
        desired_state="running", work_dir=f"/tmp/{sid}", epoch="epoch", instance_token=sid,
        started_at=101.0, last_activity=110.0,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
            status="IN_PROGRESS" if live else "BLOCKED", claim_epoch=epoch,
        ))
    if not live:
        await db.update_session(sid, state="stopped", desired_state="stopped", ended_at=120.0,
                                end_reason="session_exited_open")
        await db.set_task_meta(task_id, "needs_attention", "session_exited_open")


async def _notices(db, body_kind: str) -> list[dict]:
    async with db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(
            select(messages).where(messages.c.body_kind == body_kind)
        )).mappings()]


async def _ordinals(db) -> list[int]:
    async with db._engine.connect() as conn:
        return (await conn.execute(select(integration_repair_stages.c.ordinal).order_by(
            integration_repair_stages.c.ordinal
        ))).scalars().all()


async def test_transient_prepare_preserves_legacy_stage_attempts_and_fence(db, tmp_path, monkeypatch):
    from src.commands.claim_commands import ClaimCommandsMixin

    _service, delegate = await _continuous_parent_stage(db)
    await _claimed_writer(db, delegate, epoch=1, live=True, sid="preparing-writer")
    await db.update_session("preparing-writer", claim_phase="preparing", work_dir=str(tmp_path))
    slot = Workspace(id="repair-slot", project_id="p", workspace_path=str(tmp_path),
                     source_type=RepoSourceType.LINK, locked_by_task_id=delegate)
    await db.create_workspace(slot)
    target = BranchKey(repository_id="repo", branch="aq/parent")
    ownership = BranchOwnership(db)
    fence = ownership._fence(await ownership.get_owner(target))
    before = await _repair_stage(db, "operation", 0)
    commands = ClaimCommandsMixin()
    commands.db = db
    commands.orchestrator = SimpleNamespace(
        _task_control_lock=lambda _task_id: asyncio.Lock(), claim_waiters={},
        _worktree_slots=lambda: SimpleNamespace(reset_slot_for_task=AsyncMock(return_value="aq/parent")),
        _hierarchy_origin_and_fence=AsyncMock(return_value=({"base_sha": STARTING_SHA}, fence, "repair")),
        _hierarchy_repair_start=AsyncMock(return_value=STARTING_SHA),
        _emit_task_event=AsyncMock(), bus=SimpleNamespace(emit=AsyncMock()),
    )
    activate = db.activate_claim
    calls = 0

    async def conflict_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncpg.DeadlockDetectedError("deadlock detected")
        return await activate(*args, **kwargs)

    monkeypatch.setattr(db, "activate_claim", conflict_once)
    session = await db.get_session("preparing-writer")
    result = await commands._prepare_and_activate(
        session, session, await db.get_task(delegate), slot=slot,
    )
    assert result["result"] == "claimed", result
    assert calls == 2
    after = await _repair_stage(db, "operation", 0)
    assert (after["attempts"], after["repair_task_id"]) == (before["attempts"], delegate)
    assert await _ordinals(db) == [0]
    owner = await ownership.get_owner(target)
    assert (owner["owner_id"], owner["fence_token"], owner["session_id"]) == (
        delegate, fence.token, session.id,
    )
    assert await db.get_task_meta(delegate, "needs_attention") is None


async def test_never_claimed_delegate_extends_its_stage_and_notices_once(db):
    """Capacity is not a failed repair: no successor, one primary budget, one notice."""
    service, delegate = await _continuous_parent_stage(db)
    task = await db.get_task(delegate)
    assert (task.status, task.claim_epoch) == (TaskStatus.READY, 0)

    first = await service.expire("operation", 0, now=130.0)
    assert first == {
        "outcome": "not_due", "action": "wait", "operation_id": "operation", "stage": 0,
        "reason": "writer_unclaimed", "deadline_at": 160.0,
    }
    assert (await service.expire("operation", 0, now=159.0)) == {
        "outcome": "not_due", "action": "wait", "operation_id": "operation", "stage": 0,
    }
    again = await service.expire("operation", 0, now=175.0)
    assert (again["reason"], again["deadline_at"]) == ("writer_unclaimed", 205.0)

    stage = await _repair_stage(db, "operation", 0)
    assert (stage["state"], stage["attempts"], stage["repair_task_id"]) == ("active", 0, delegate)
    assert stage["dossier"]["budget"]["deadline_at"] == 205.0
    assert stage["dossier"]["deadline_deferral_count"] == 2
    assert [entry["claim_epoch"] for entry in stage["dossier"]["deadline_deferrals"]] == [0, 0]
    assert await _ordinals(db) == [0]
    operation = await db.get_integration_operation("operation")
    assert (operation["active_stage"], operation["state"]) == (0, "active")
    assert await service.due_stages(now=204.0) == []
    notices = await _notices(db, "integration_repair_deferred")
    assert len(notices) == 1
    assert delegate in notices[0]["body"] and "capacity wait" in notices[0]["body"]
    assert notices[0]["to_id"] == "supervisor-p"


async def test_claim_after_the_deadline_starts_the_stage_budget(db):
    """Queue time is not a repair attempt: the claim starts the stage's budget.

    A stage's deadline runs from its activation, so a delegate the pool cannot
    staff in time is claimed with its budget already spent. Its close is then
    refused for a repair attempt that never began, which is how a claimed batch
    repair ended up holding a claim it could neither complete nor release.
    """
    service, delegate = await _continuous_parent_stage(db)

    running = await service.start_claimed_stage_budget(delegate, now=120.0)
    assert running == {
        "outcome": "within_budget", "operation_id": "operation", "stage": 0,
        "deadline_at": 130.0,
    }
    assert (await _repair_stage(db, "operation", 0))["deadline_at"] == 130.0

    # Not claimed on this delegate (the pool has not picked it up yet): its
    # clock stays where activation put it, so a real attempt still meets its own
    # budget.
    unclaimed = await service.start_claimed_stage_budget(delegate, now=140.0)
    assert unclaimed == {
        "outcome": "not_claimed", "task_id": delegate,
        "operation_id": "operation", "stage": 0,
    }
    assert (await _repair_stage(db, "operation", 0))["deadline_at"] == 130.0

    await _claimed_writer(db, delegate, epoch=1, live=True, sid="late-writer")
    started = await service.start_claimed_stage_budget(delegate, now=140.0)
    assert started == {
        "outcome": "started", "operation_id": "operation", "stage": 0,
        "deadline_at": 170.0, "reason": "writer_claimed",
    }

    stage = await _repair_stage(db, "operation", 0)
    assert (stage["state"], stage["attempts"], stage["repair_task_id"]) == ("active", 0, delegate)
    assert stage["deadline_at"] == 170.0
    assert stage["dossier"]["budget"]["deadline_at"] == 170.0
    assert stage["dossier"]["deadline_deferral_count"] == 1
    assert stage["dossier"]["deadline_deferrals"] == [
        {
            "reason": "writer_claimed", "at": 140.0,
            "previous_deadline_at": 130.0, "deadline_at": 170.0,
            "task_id": delegate, "task_status": "IN_PROGRESS", "claim_epoch": 1,
        }
    ]
    assert await _ordinals(db) == [0]
    operation = await db.get_integration_operation("operation")
    assert (operation["active_stage"], operation["state"]) == (0, "active")

    notices = await _notices(db, "integration_repair_deferred")
    assert len(notices) == 1
    assert delegate in notices[0]["body"]
    assert "Queue time is not a repair attempt" in notices[0]["body"]

    # A re-claim inside the fresh budget never extends it, and the supervisor
    # hears about a late claim once per stage.
    assert (await service.start_claimed_stage_budget(delegate, now=160.0)) == {
        "outcome": "within_budget", "operation_id": "operation", "stage": 0,
        "deadline_at": 170.0,
    }
    assert (await _repair_stage(db, "operation", 0))["deadline_at"] == 170.0
    assert len(await _notices(db, "integration_repair_deferred")) == 1
    assert await service.due_stages(now=169.0) == []
    assert [stage["stage"] for stage in await service.due_stages(now=171.0)] == [0]


async def test_a_claim_never_rearms_a_stage_it_did_not_claim(db):
    """Only the claimed delegate's own current stage is re-armed."""
    service, delegate = await _continuous_parent_stage(db)
    await _claimed_writer(db, delegate, epoch=1, live=True, sid="late-writer")

    unknown = await service.start_claimed_stage_budget("not-a-delegate", now=140.0)
    assert unknown == {"outcome": "no_stage", "task_id": "not-a-delegate"}

    # A stage the ladder already retired belongs to ``expire``, not to a claim:
    # its ordinal and its successor are the ladder's to spend.
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == "operation",
            integration_repair_stages.c.ordinal == 0,
        ).values(state="expired", deadline_at=100.0))
    retired = await service.start_claimed_stage_budget(delegate, now=140.0)
    assert retired == {"outcome": "no_stage", "task_id": delegate}
    assert (await _repair_stage(db, "operation", 0))["deadline_at"] == 100.0


async def test_live_writer_is_revisited_and_real_head_progress_escalates(db):
    service, delegate = await _continuous_parent_stage(db)
    await _claimed_writer(db, delegate, epoch=1, live=True, sid="live-writer")

    waiting = await service.expire("operation", 0, now=130.0)
    assert (waiting["action"], waiting["reason"], waiting["deadline_at"]) == (
        "wait", "writer_live", 430.0
    )
    assert await _ordinals(db) == [0]
    assert (await db.get_task(delegate)).status is TaskStatus.IN_PROGRESS
    assert await _notices(db, "integration_repair_deferred") == []

    # The live writer publishes: that is real head progress, and the
    # bounded ladder continues from the new head as before.
    head = "b" * 40
    async with db.immediate() as conn:
        await service.bind_current_parent_subject_on(
            conn, "operation", head_sha=head, now=200.0,
            commit_proof={"base_sha": STARTING_SHA, "head_sha": head, "commits": [head]},
        )
    expired = await service.expire("operation", 0, now=430.0)
    assert (expired["outcome"], expired["action"], expired["stage"]) == (
        "expired", "dispatch_debug", 1
    )
    successor = await _repair_stage(db, "operation", 1)
    assert successor["dossier"]["allocation"]["subject_sha"] == head
    assert successor["starting_sha"] == head


async def test_conclusive_attempt_escalates_even_when_the_head_is_unchanged(db):
    service, delegate = await _continuous_parent_stage(db)
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="dead-writer")
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(attempts=1))

    expired = await service.expire("operation", 0, now=130.0)

    assert (expired["action"], expired["stage"]) == ("dispatch_debug", 1)
    assert "writer_refiles" not in (await _repair_stage(db, "operation", 0))["dossier"]


async def test_stopped_unpublished_writer_refiles_its_ordinal_and_caps_the_head(db):
    """A dead writer that published nothing is refiled, never a new stage on its own."""
    service, delegate = await _continuous_parent_stage(db)
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="dead-1")

    refiled = await service.expire("operation", 0, now=130.0)

    assert refiled == {
        "outcome": "not_due", "action": "wait", "operation_id": "operation", "stage": 0,
        "reason": "writer_refiled", "deadline_at": 160.0, "dispatch": "already_dispatched",
    }
    stage = await _repair_stage(db, "operation", 0)
    assert (stage["state"], stage["repair_task_id"], stage["deadline_at"]) == (
        "active", delegate, 160.0
    )
    (record,) = stage["dossier"]["writer_refiles"]
    assert (record["task_id"], record["claim_epoch"], record["subject_sha"]) == (
        delegate, 1, STARTING_SHA
    )
    assert record["stop_proof"]["outcome"] == "detached"
    task = await db.get_task(delegate)
    assert task.status is TaskStatus.READY
    assert "refiled at the same ordinal" in task.description
    assert await db.get_task_meta(delegate, "needs_attention") is None
    owner = await BranchOwnership(db).get_owner(BranchKey(repository_id="repo", branch="aq/parent"))
    assert (owner["owner_id"], owner["handoff_state"]) == (delegate, "reserved")
    assert await _ordinals(db) == [0]
    assert (await db.get_integration_operation("operation"))["state"] == "active"

    # The refiled writer dies again: one refile per stage, then the ladder.
    await _claimed_writer(db, delegate, epoch=2, live=False, sid="dead-2")
    escalated = await service.expire("operation", 0, now=160.0)
    assert (escalated["action"], escalated["stage"]) == ("dispatch_debug", 1)
    debug = await service.dispatch("operation", 1)
    assert debug["outcome"] == "dispatched"

    # Three writers have now started on the unchanged head (stage 0, its
    # refile, stage 1): the debug writer dying unpublished ends the budget.
    await _claimed_writer(db, debug["repair_task_id"], epoch=1, live=False, sid="dead-3")
    stopped = await service.expire("operation", 1, now=220.0)
    assert (stopped["action"], stopped["stage"]) == ("supervisor_recovery", 1)
    assert "writer_refiles" not in (await _repair_stage(db, "operation", 1))["dossier"]
    assert await _ordinals(db) == [0, 1]
    assert (await db.get_integration_operation("operation"))["state"] == "escalated"
    assert len(await _notices(db, "integration_repair_no_progress")) == 1


async def test_successor_allocations_are_capped_per_unchanged_subject(db):
    """Refiles and successor stages share one budget of three writers per head."""
    service, delegate = await _continuous_parent_stage(db)
    stage = await _repair_stage(db, "operation", 0)
    refile = {"task_id": delegate, "claim_epoch": 1, "subject_sha": STARTING_SHA}
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(
            dossier=dict(stage["dossier"]) | {"writer_refiles": [refile, refile]}
        ))
    await db.transition_task(delegate, TaskStatus.FAILED, force=True)

    result = await service.expire("operation", 0, now=130.0)

    assert (result["action"], result["stage"]) == ("supervisor_recovery", 0)
    assert await _ordinals(db) == [0]
    recovery = (await _repair_stage(db, "operation", 0))["dossier"]["supervisor_recovery"]
    assert "3 writers" in recovery["reason"]


async def test_stopped_writer_on_a_stale_fence_waits_without_refile_or_successor(db):
    service, delegate = await _continuous_parent_stage(db)
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="dead-writer")
    async with db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(
            owner_id="intruder", owner_role="repair", fence_token=9,
        ))

    for now, deadline in ((130.0, 430.0), (430.0, 730.0)):
        result = await service.expire("operation", 0, now=now)
        assert (result["action"], result["reason"], result["deadline_at"]) == (
            "wait", "stale_fence", deadline
        )
    stage = await _repair_stage(db, "operation", 0)
    assert "writer_refiles" not in stage["dossier"]
    assert stage["dossier"]["deadline_deferrals"][-1]["detail"].startswith(
        "the fence belongs to intruder"
    )
    assert await _ordinals(db) == [0]
    assert (await db.get_task(delegate)).status is TaskStatus.BLOCKED
    owner = (await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    ))
    assert (owner["owner_id"], owner["fence_token"]) == ("intruder", 9)
    assert len(await _notices(db, "integration_repair_deferred")) == 1


class _FakeOwnerRecovery:
    """Scripted stop proofs; a real (non-dry) release frees the owner row."""

    def __init__(self, db, outcomes):
        self.db, self.outcomes, self.calls = db, list(outcomes), []

    async def recover(self, owner_row_id, *, principal, dry_run=False):
        from src.integration.owner_recovery import RecoveryOutcome

        self.calls.append((principal, dry_run))
        outcome, reason = self.outcomes.pop(0)
        if outcome != "not_eligible" and not dry_run:
            async with self.db.immediate() as conn:
                await conn.execute(update(integration_branch_owners).where(
                    integration_branch_owners.c.id == owner_row_id
                ).values(handoff_state="released", fence_token=integration_branch_owners.c.fence_token + 1,
                         session_id=None, workspace_id=None))
        evidence = {"detail": reason} if reason else {"stop_proof": {"session_id": "dead"}}
        return RecoveryOutcome(owner_row_id, outcome, reason, evidence, dry_run)


async def _attached_dead_writer(db, delegate: str) -> None:
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="dead")
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == delegate).values(status="IN_PROGRESS"))
        await conn.execute(update(integration_branch_owners).values(
            handoff_state="attached", session_id="dead",
        ))


async def _dead_writer_with_released_fence(db, delegate: str) -> None:
    """A repair delegate whose worker died and whose fence owner recovery freed.

    This is the shape ``aq task restart`` found on 2026-10-05: the writer's
    session is gone, its claim is closed with the task BLOCKED, and the branch
    fence is ``released`` — no reserved repair fence for the delegate.
    """
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="dead")
    async with db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(
            handoff_state="released",
            fence_token=integration_branch_owners.c.fence_token + 1,
            session_id=None,
            workspace_id=None,
            confirmed_workspace_id="ws-dead",
        ))


def _restart_command(db, service):
    """The ``restart_task`` collaborators an integration delegate restart reads.

    ``CommandHandler`` composes both mixins; these tests call the handler
    method unbound, so the double carries the sibling methods explicitly.
    """
    command = SimpleNamespace(db=db, _integration_repair_service=lambda: service)
    for name in ("_restart_repair_delegate", "_undo_refused_restart"):
        setattr(command, name, partial(getattr(TaskCommandsMixin, name), command))
    return command


async def test_restart_restores_the_dead_repair_delegates_branch_reservation(db):
    """A restarted repair delegate must be claimable, not READY and stranded.

    A repair delegate owns no branch origin: the pool claim frontier admits it
    only on the exact reserved ``repair`` fence of its operation's branch. A
    restart that only moved the row to READY left it off the frontier with
    ``frontier_origin_not_materialized`` and zero counted demand, so no pool
    worker could ever pick it up.
    """
    service, delegate = await _continuous_parent_stage(db)
    await _dead_writer_with_released_fence(db, delegate)
    stage_before = await _repair_stage(db, "operation", 0)
    assert await db.is_hierarchy_task_runnable(delegate) is False

    result = await TaskCommandsMixin._cmd_restart_task(
        _restart_command(db, service), {"task_id": delegate}
    )

    assert result["restarted"] == delegate
    assert (result["previous_status"], result["reservation"]) == ("BLOCKED", "acquired")
    assert (await db.get_task(delegate)).status is TaskStatus.READY
    owner = await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert (owner["owner_id"], owner["owner_role"], owner["handoff_state"]) == (
        delegate, "repair", "reserved",
    )
    assert owner["session_id"] is None and owner["workspace_id"] is None
    assert await db.is_hierarchy_task_runnable(delegate) is True
    assert await db.claim_frontier_exclusions(delegate) == []
    # The claim predicate itself, in both the hoisted and the correlated form.
    from src.database.queries.claim_queries import _frontier_where
    from src.database.queries.hierarchy_queries import ProjectIntegrationMode
    async with db._engine.connect() as conn:
        for mode in (None, ProjectIntegrationMode(True, "repo")):
            claimable = await conn.scalar(select(tasks.c.id).where(
                tasks.c.id == delegate, _frontier_where("p", mode),
            ))
    assert claimable == delegate
    # The same delegate and the same stage resume; no budget or clock was reset.
    stage_after = await _repair_stage(db, "operation", 0)
    for field in ("ordinal", "state", "repair_task_id", "writer_kind", "started_at",
                  "deadline_at", "attempts", "starting_sha", "current_subject"):
        assert stage_after[field] == stage_before[field]
    assert await service.missing_delegate_reservations() == []


@pytest.mark.parametrize("fence_state", ["attached", "handoff_pending"])
async def test_restart_refuses_while_the_stopped_writer_still_holds_the_fence(db, fence_state):
    """A fence no handoff can prove free is refused, naming the control to run."""
    service, delegate = await _continuous_parent_stage(db)
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="dead")
    async with db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(
            handoff_state=fence_state, session_id="dead",
        ))

    result = await TaskCommandsMixin._cmd_restart_task(
        _restart_command(db, service), {"task_id": delegate}
    )

    assert "aq integration reserve-owner --task-id" in result["error"]
    assert "aq integration release-owner --task-id" in result["error"]
    assert (await db.get_task(delegate)).status is TaskStatus.BLOCKED
    owner = await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert (owner["owner_id"], owner["handoff_state"], owner["session_id"]) == (
        delegate, fence_state, "dead",
    )
    assert await db.is_hierarchy_task_runnable(delegate) is False


async def test_attached_stopped_writer_is_proven_by_owner_recovery_then_refiled(db):
    recovery = _FakeOwnerRecovery(db, [("released", None), ("released", None)])
    service, delegate = await _continuous_parent_stage(db, owner_recovery=recovery)
    await _attached_dead_writer(db, delegate)

    result = await service.expire("operation", 0, now=130.0)

    assert recovery.calls == [("repair_stage_expiry", True), ("repair_stage_expiry", False)]
    assert (result["reason"], result["dispatch"]) == ("writer_refiled", "dispatched")
    (record,) = (await _repair_stage(db, "operation", 0))["dossier"]["writer_refiles"]
    assert record["stop_proof"]["outcome"] == "released"
    assert (await db.get_task(delegate)).status is TaskStatus.READY
    owner = await BranchOwnership(db).get_owner(BranchKey(repository_id="repo", branch="aq/parent"))
    assert (owner["owner_id"], owner["handoff_state"]) == (delegate, "reserved")
    assert await _ordinals(db) == [0]


@pytest.mark.parametrize("scripted, expected", [
    ([("preserved_and_released", None)], ("expired", "dispatch_debug", None)),
    ([("not_eligible", "writer_live")], ("not_due", "wait", "writer_live")),
    ([("not_eligible", "origin_unreachable")], ("not_due", "wait", "origin_unreachable")),
    (None, ("not_due", "wait", "stop_proof_unavailable")),
])
async def test_attached_stopped_writer_without_a_clean_release_never_refiles(
    db, scripted, expected
):
    recovery = _FakeOwnerRecovery(db, scripted) if scripted is not None else None
    service, delegate = await _continuous_parent_stage(db, owner_recovery=recovery)
    await _attached_dead_writer(db, delegate)

    result = await service.expire("operation", 0, now=130.0)

    assert (result["outcome"], result["action"], result.get("reason")) == expected
    if scripted is not None:
        # Unpublished work keeps the rollover path: the successor's dispatch
        # performs the preserving release, never this expiry.
        assert recovery.calls == [("repair_stage_expiry", True)]
    assert "writer_refiles" not in (await _repair_stage(db, "operation", 0))["dossier"]
    owner = await BranchOwnership(db).get_owner(BranchKey(repository_id="repo", branch="aq/parent"))
    assert (owner["owner_id"], owner["handoff_state"]) == (delegate, "attached")
    noticed = expected[2] in {"origin_unreachable", "stop_proof_unavailable"}
    assert len(await _notices(db, "integration_repair_deferred")) == int(noticed)


async def test_operator_hold_on_a_stopped_writer_stays_binding(db):
    """An operator's hold is a human decision: never refiled, dispatched or superseded."""
    service, delegate = await _continuous_parent_stage(db)
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="dead-writer")
    await db.set_task_meta(delegate, "manual_pause", {"status": "BLOCKED", "sessions": []})

    held = await service.expire("operation", 0, now=130.0)

    assert (held["action"], held["reason"], held["deadline_at"]) == (
        "wait", "operator_hold", 430.0
    )
    assert (await db.get_task(delegate)).status is TaskStatus.BLOCKED
    assert "writer_refiles" not in (await _repair_stage(db, "operation", 0))["dossier"]
    assert await _ordinals(db) == [0]
    assert await _notices(db, "integration_repair_deferred") == []
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == delegate).values(status="PAUSED"))
    refused = await service.dispatch("operation", 0)
    assert refused["outcome"] == "human_required" and "operator" in refused["reason"]
    assert (await db.get_task(delegate)).status is TaskStatus.PAUSED


async def test_finite_policy_human_gate_is_not_expired_or_dispatched_around(db):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"), "operation", "collector"
    )
    service = RepairService(db, clock=lambda: 150.0)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    primary = await service.dispatch("operation", 0)
    await db.transition_task(primary["repair_task_id"], TaskStatus.FAILED, force=True)
    assert (await service.expire("operation", 0, now=130.0))["stage"] == 1
    debug = await service.dispatch("operation", 1)
    # The debug writer was never claimed; under ``human`` the gate still binds.
    assert (await db.get_task(debug["repair_task_id"])).claim_epoch == 0

    blocked = await service.expire("operation", 1, now=190.0)

    assert blocked["action"] == "block_for_human"
    for now in (200.0, 500.0):
        assert (await service.expire("operation", 1, now=now))["action"] == "block_for_human"
        assert (await service.dispatch("operation", 1))["outcome"] == "stale"
        assert await service.due_stages(now=now) == []
    assert (await db.get_integration_operation("operation"))["state"] == "human_required"
    assert (await db.get_task("parent")).status is TaskStatus.BLOCKED
    assert await _ordinals(db) == [0, 1]


async def test_mechanical_dispatch_refusal_is_retryable_unknown_with_one_notice(db):
    from src.integration.repair import RepairService

    policy = _policy()
    policy["parent"]["repair"]["on_exhausted"] = "continue"
    await _seed_parent_operation(db, policy=policy)
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"), "operation", "collector"
    )
    service = RepairService(db, clock=lambda: 150.0)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await db.create_task(Task(
        id="repair-operation-0", project_id="p", title="Unrelated", description="",
        status=TaskStatus.DEFINED,
    ))

    for _ in range(2):
        refused = await service.dispatch("operation", 0)
        assert refused["outcome"] == "unknown"
        assert refused["reason_code"] == "delegate_id_collision"
        assert "repair-operation-0 already exists" in refused["reason"]
    stage = await _repair_stage(db, "operation", 0)
    assert (stage["state"], stage["repair_task_id"], stage["writer_kind"]) == ("active", None, None)
    assert (await db.get_integration_operation("operation"))["state"] == "active"
    # Nothing was consumed: the continuation pass still offers the stage.
    assert await service.pending_dispatches() == [{"operation_id": "operation", "ordinal": 0}]
    notices = await _notices(db, "integration_repair_dispatch_unknown")
    assert len(notices) == 1 and "delegate_id_collision" in notices[0]["body"]

    await db.delete_task("repair-operation-0")
    assert (await service.dispatch("operation", 0))["outcome"] == "dispatched"


async def test_reviewed_playbooks_see_unknown_dispatch_as_declared_busy():
    """The frozen contract (and its fingerprint) is unchanged; the reason survives."""
    from src.commands.contracts.integration import (
        INTEGRATION_REPAIR_DISPATCH,
        IntegrationRepairDispatchArgs,
        register_integration_contracts,
    )
    from src.commands.contracts.registry import ContractRegistry

    assert "unknown" not in {spec.name for spec in INTEGRATION_REPAIR_DISPATCH.execution.outcomes}
    registry = ContractRegistry()
    register_integration_contracts(registry)
    registration = registry.get("integration_repair_dispatch")
    with patch("src.commands.contracts.builtin._handler") as factory:
        factory.return_value.execute = AsyncMock(return_value={
            "success": False, "outcome": "unknown", "operation_id": "operation", "stage": 0,
            "reason": "integration branch aq/parent has no owner row",
            "reason_code": "owner_missing",
        })
        result = await registration.invoke(
            IntegrationRepairDispatchArgs(operation_id="operation", stage=0), None
        )
    assert result.outcome == "busy"
    assert result.summary == "integration branch aq/parent has no owner row"
    assert (result.value.operation_id, result.value.stage) == ("operation", 0)


async def test_due_stage_query_does_not_require_an_agent_or_ci_event(db):
    """Task10's scan must see due clocks before a writer is ever dispatched."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)

    assert await service.due_stages(now=129.0) == []
    assert await service.due_stages(now=130.0) == [
        {
            "operation_id": "operation",
            "stage": 0,
            "deadline_at": 130.0,
            "deadline_event_id": "repair-deadline-operation-0",
        }
    ]


async def test_batch_operation_reservation_is_terminal_stable_and_has_no_stage(db):
    """Task8 must reuse one pinned operation even after it becomes terminal."""
    from src.integration.repair import RepairService

    await db.update_project("p", hierarchical_integration_mode="train")
    artifact = _artifact()
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(mode="json"),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request-batch",
                source_manifest_digest="manifest",
                base_sha=STARTING_SHA,
                lifecycle="building",
                current_revision=0,
                integration_branch="aq/integration/batch",
                policy_snapshot=_policy(),
                artifact_snapshot=artifact.model_dump(mode="json"),
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        service = RepairService(db)
        reserved = await service.reserve_batch_operation_on(conn, "batch", now=50.0)
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == reserved["id"])
            .values(state="completed", updated_at=70.0)
        )
        replay = await service.reserve_batch_operation_on(conn, "batch", now=80.0)

    assert replay == reserved | {"state": "completed", "updated_at": 70.0}
    assert reserved["id"] == "repair-batch-batch"
    assert reserved["batch_id"] == "batch"
    assert reserved["state"] == "active"
    async with db._engine.connect() as conn:
        stages = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == reserved["id"]
                )
            )
        ).all()
        pins = (
            await conn.execute(
                select(integration_operation_artifact_pins).where(
                    integration_operation_artifact_pins.c.operation_id == reserved["id"]
                )
            )
        ).mappings().all()
    assert stages == []
    assert [row["artifact_sha256"] for row in pins] == [artifact.artifact_sha256]


async def test_parent_green_waits_for_guarded_completion_and_remains_deadline_bounded(db):
    """Green evidence alone must not end a parent's repair episode or clock."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db, "green-check", run_id="run-green", conclusion="success"
    )
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)

    green = await service.record_result("operation", "green-check", now=110.0)
    expired = await service.expire("operation", 0, now=130.0)

    assert green == {
        "outcome": "continue",
        "action": "completion_ready",
        "attempts": 1,
    }
    assert expired["outcome"] == "expired"
    assert expired["action"] == "dispatch_debug"
    async with db._engine.connect() as conn:
        primary = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "operation",
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()
    assert primary["success_evidence_id"] == "green-check"
    assert primary["success_subject"] == {
        "kind": "parent",
        "generation": 3,
        "head_sha": STARTING_SHA,
    }
    assert primary["state"] == "expired"


async def test_parent_green_and_timeout_serialize_to_one_debug_stage(db):
    """Racing evidence and deadline processing cannot restart stage zero."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db, "green-race", run_id="run-green-race", conclusion="success"
    )
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)

    evidence, timeout = await asyncio.gather(
        service.record_result("operation", "green-race", now=130.0),
        service.expire("operation", 0, now=130.0),
    )

    assert timeout["outcome"] in {"expired", "already_terminal"}
    assert evidence["action"] in {"completion_ready", "stale"}
    async with db._engine.connect() as conn:
        stages = (
            await conn.execute(
                select(integration_repair_stages)
                .where(integration_repair_stages.c.operation_id == "operation")
                .order_by(integration_repair_stages.c.ordinal)
            )
        ).mappings().all()
    assert [row["ordinal"] for row in stages] == [0, 1]
    assert stages[0]["state"] == "expired"
    assert stages[1]["state"] == "active"


async def _seed_root_operation(
    db,
    *,
    branch: str = "aq/integration/batch",
    policy=None,
    candidate_state: str = "testing",
    candidate_sha: str | None = STARTING_SHA,
    candidate_evidence: bool = True,
    batch_lifecycle: str = "testing",
) -> str:
    from src.integration.repair import RepairService

    await db.update_project("p", hierarchical_integration_mode="train")
    artifact = _artifact()
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(mode="json"),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request-batch",
                source_manifest_digest="manifest",
                base_sha=STARTING_SHA,
                lifecycle=batch_lifecycle,
                current_revision=0,
                integration_branch=branch,
                policy_snapshot=policy or _policy(),
                artifact_snapshot=artifact.model_dump(mode="json"),
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch",
                revision=0,
                construction_base_sha=STARTING_SHA,
                head_sha=candidate_sha,
                state=candidate_state,
                created_at=1.0,
                updated_at=1.0,
            )
        )
        operation = await RepairService(db).reserve_batch_operation_on(
            conn, "batch", now=50.0
        )
        if not candidate_evidence:
            return operation["id"]
        await conn.execute(
            insert(integration_check_evidence).values(
                id="root-green",
                operation_id=operation["id"],
                batch_id="batch",
                candidate_revision=0,
                producer_id="forge",
                workflow_id="workflow",
                run_id="root-run",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=9.0,
            )
        )
    return operation["id"]


async def _stage_delegate_description(db, service, operation_id: str, ordinal: int = 0) -> str:
    stage = await _repair_stage(db, operation_id, ordinal)
    async with db.immediate() as conn:
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.id == operation_id
                )
            )
        ).mappings().one()
        return await service._delegate_description_on(conn, dict(operation), stage)


async def _add_candidate_evidence(
    db,
    operation_id: str,
    evidence_id: str,
    *,
    run_id: str,
    conclusion: str,
    observed_at: float = 10.0,
) -> None:
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_check_evidence).values(
                id=evidence_id,
                operation_id=operation_id,
                batch_id="batch",
                candidate_revision=0,
                producer_id="forge",
                workflow_id="workflow",
                run_id=run_id,
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": conclusion},
                conclusion=conclusion,
                classification="conclusive",
                observed_at=observed_at,
            )
        )


async def test_pending_ci_delegate_description_carries_sha_and_protocol(db):
    """A stage-0 delegate dispatched before CI reports sees no failures and a hold protocol."""
    from src.integration.repair import RepairService

    # ``built`` is the state production keeps while candidate CI runs.
    operation_id = await _seed_root_operation(
        db, candidate_state="built", candidate_sha="c" * 40, candidate_evidence=False
    )
    service = RepairService(db)
    assert (await service.start(operation_id, "c" * 40, "batch", now=100.0))["outcome"] == "started"
    await _add_candidate_evidence(
        db, operation_id, "pending-evidence", run_id="root-run", conclusion="pending"
    )
    description = await _stage_delegate_description(db, service, operation_id)

    assert "## Awaiting candidate CI" in description
    assert ("c" * 40) in description
    assert "CI run: root-run (pending)" in description
    assert "There is nothing on record to repair" in description
    assert "Do not push" in description


async def test_recorded_failure_delegate_description_omits_hold_protocol(db):
    """A failure the stage dossier already records is the work; no hold contradicts it."""
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(
        db, candidate_state="built", candidate_sha="c" * 40, candidate_evidence=False
    )
    service = RepairService(db)
    await service.start(operation_id, "c" * 40, "batch", now=100.0)
    await _add_candidate_evidence(
        db, operation_id, "failing-evidence", run_id="failing-run", conclusion="failure"
    )
    recorded = await service.record_result(operation_id, "failing-evidence", now=101.0)
    assert recorded["action"] == "repair"
    description = await _stage_delegate_description(db, service, operation_id)

    assert "failing-evidence" in description
    assert "## Awaiting candidate CI" not in description
    assert "## Candidate CI is green" not in description
    assert "There is nothing on record to repair" not in description


async def test_unrecorded_failure_evidence_keeps_the_hold(db):
    """Failure evidence the dossier does not record yet leaves the writer an empty dossier.

    Candidate revisions stay ``built`` through CI, so a red observation that
    ``record_result`` has not folded in yet used to drop the hold outright and
    hand the writer an empty dossier with no instruction at all.
    """
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(
        db, candidate_state="built", candidate_sha="c" * 40, candidate_evidence=False
    )
    service = RepairService(db)
    await service.start(operation_id, "c" * 40, "batch", now=100.0)
    await _add_candidate_evidence(
        db, operation_id, "failing-evidence", run_id="failing-run", conclusion="failure"
    )
    description = await _stage_delegate_description(db, service, operation_id)

    assert "## Awaiting candidate CI" in description
    assert f"Candidate SHA: {'c' * 40}" in description
    assert "CI run: failing-run (failure)" in description
    assert "has not recorded that failure in this stage's dossier yet" in description
    assert "If and only if a required check on the exact candidate SHA fails" in description


@pytest.mark.parametrize("conclusion", ["cancelled", "inconclusive", "pending"])
async def test_non_failure_latest_run_keeps_the_hold(db, conclusion):
    """A cancelled, inconclusive or still-pending run is no failure: the writer holds.

    A concurrency-cancelled candidate run never reaches ``record_result``, so
    stage 0 used to dispatch with an empty dossier and no hold or protocol.
    """
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(
        db, candidate_state="built", candidate_sha="c" * 40, candidate_evidence=False
    )
    service = RepairService(db)
    await service.start(operation_id, "c" * 40, "batch", now=100.0)
    await _add_candidate_evidence(
        db, operation_id, "latest", run_id="latest-run", conclusion=conclusion
    )
    description = await _stage_delegate_description(db, service, operation_id)

    assert "## Awaiting candidate CI" in description
    assert "## Candidate CI is green" not in description
    assert f"Candidate SHA: {'c' * 40}" in description
    assert f"CI run: latest-run ({conclusion})" in description
    assert "There is nothing on record to repair" in description
    assert "push nothing at all to the batch branch" in description
    assert "close this stage pass-unchanged when CI turns green" in description


@pytest.mark.parametrize(
    ("candidate_state", "candidate_sha", "expected_sha"),
    [
        ("built", "c" * 40, "c" * 40),
        ("constructing", None, None),
    ],
)
async def test_pre_testing_dispatch_names_the_pending_candidate(
    db, candidate_state, candidate_sha, expected_sha
):
    """A stage dispatched before the revision reaches testing still names the candidate.

    Stage 0 opens as soon as the candidate revision exists, so the writer used
    to receive an empty dossier with no candidate identity and had to ask the
    supervisor what to do. It now reads the candidate SHA, the revision state
    and the pending protocol whatever state the revision is in.
    """
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(
        db,
        candidate_state=candidate_state,
        candidate_sha=candidate_sha,
        candidate_evidence=False,
    )
    service = RepairService(db)
    starting = candidate_sha or STARTING_SHA
    assert (await service.start(operation_id, starting, "batch", now=100.0))["outcome"] == "started"
    stage = await _repair_stage(db, operation_id, 0)
    async with db.immediate() as conn:
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.id == operation_id
                )
            )
        ).mappings().one()
        description = await service._delegate_description_on(conn, dict(operation), stage)

    assert "## Awaiting candidate CI" in description
    assert f"(revision state: {candidate_state})" in description
    if expected_sha is None:
        # No candidate head yet: the subject is the construction base and the
        # section says so instead of naming a SHA that does not exist.
        assert "Candidate SHA: not built yet" in description
        assert f"Construction base: {STARTING_SHA}" in description
    else:
        assert f"Candidate SHA: {expected_sha}" in description
    # Nothing has been observed yet, so the protocol is spelled out.
    assert "CI run: not yet visible" in description
    assert "There is nothing on record to repair" in description
    assert "push nothing at all to the batch branch" in description
    assert "close this stage pass-unchanged when CI turns green" in description
    assert "If and only if a required check on the exact candidate SHA fails" in description


async def test_pre_testing_dispatch_delegate_task_carries_candidate_ci(db):
    """The dispatched delegate task itself, not just the helper, carries the section.

    Stage 0 opens while the batch is repairing and its candidate is still being
    constructed: that dispatch is the one that used to reach the writer with
    nothing but an empty dossier.
    """
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(
        db,
        candidate_state="constructing",
        candidate_sha=None,
        candidate_evidence=False,
        batch_lifecycle="repairing",
    )
    service = RepairService(db)
    assert (await service.start(operation_id, STARTING_SHA, "batch", now=100.0))["outcome"] == "started"
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/integration/batch"), operation_id, "collector"
    )
    assert (await service.dispatch(operation_id, 0))["outcome"] == "dispatched"
    delegate = await db.get_task(f"repair-{operation_id}-0")

    assert "## Awaiting candidate CI" in delegate.description
    assert "(revision state: constructing)" in delegate.description
    assert f"Construction base: {STARTING_SHA}" in delegate.description
    assert "push nothing at all to the batch branch" in delegate.description
    assert "close this stage pass-unchanged when CI turns green" in delegate.description


@pytest.mark.parametrize("candidate_state", ["built", "green"])
async def test_green_candidate_says_push_nothing_and_close_unchanged(db, candidate_state):
    """A candidate already green at dispatch tells the writer to push nothing.

    Without an instruction the writer of a green candidate saw an empty dossier
    and pushed a fix after green CI, aborting the batch (0917a17bb).  The
    revision may still read ``built`` until the green is attested.
    """
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(
        db, candidate_state=candidate_state, candidate_sha="c" * 40
    )
    service = RepairService(db)
    await service.start(operation_id, "c" * 40, "batch", now=100.0)
    description = await _stage_delegate_description(db, service, operation_id)

    assert "## Candidate CI is green — push nothing" in description
    assert f"(revision state: {candidate_state})" in description
    assert f"Candidate SHA: {'c' * 40}" in description
    assert "CI run: root-run (success)" in description
    assert "push nothing at all to the batch branch" in description
    assert "close this stage pass-unchanged now" in description
    assert "## Awaiting candidate CI" not in description
    assert "If and only if a required check" not in description


async def test_green_candidate_delegate_task_carries_push_nothing(db):
    """The dispatched delegate task itself carries the green instruction.

    Stage 0 of a built candidate dispatches only once its audit PR is
    published, by which time candidate CI may already be green.
    """
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(
        db, candidate_state="built", candidate_sha="c" * 40, batch_lifecycle="repairing"
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_candidate_publications).values(
                batch_id="batch",
                revision=0,
                state="pr_published",
                repository_id="repo",
                repository_numeric_id=99,
                repository_full_name="acme/widgets",
                base_ref="main",
                head_ref="aq/integration/batch",
                head_sha="c" * 40,
                expected_old_sha="0" * 40,
                idempotency_key="publication",
                pr_number=9,
                pr_url="https://github.com/acme/widgets/pull/9",
                created_at=2.0,
                updated_at=2.0,
            )
        )
    service = RepairService(db)
    assert (await service.start(operation_id, "c" * 40, "batch", now=100.0))["outcome"] == "started"
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/integration/batch"), operation_id, "collector"
    )
    assert (await service.dispatch(operation_id, 0))["outcome"] == "dispatched"
    delegate = await db.get_task(f"repair-{operation_id}-0")

    assert "## Candidate CI is green — push nothing" in delegate.description
    assert "close this stage pass-unchanged now" in delegate.description
    assert "## Awaiting candidate CI" not in delegate.description


async def test_rebuild_conflict_suppresses_the_green_candidate_section(db):
    """A frozen rebuild conflict is the stage's work even though the candidate is green.

    Main advanced after green CI; the writer must merge the new base, so a
    push-nothing instruction would contradict the recorded conflict.
    """
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(
        db, candidate_state="green", candidate_sha="c" * 40
    )
    service = RepairService(db)
    await service.start(operation_id, "c" * 40, "batch", now=100.0)
    stage = await _repair_stage(db, operation_id, 0)
    dossier = dict(stage["dossier"]) | {
        "candidate_rebuild_conflict": {
            "kind": "candidate_rebuild",
            "candidate_sha": "c" * 40,
            "new_base_sha": "d" * 40,
        }
    }
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == operation_id)
            .values(dossier=dossier)
        )
    description = await _stage_delegate_description(db, service, operation_id)

    assert "candidate_rebuild_conflict" in description
    assert "## Candidate CI is green" not in description
    assert "## Awaiting candidate CI" not in description
    assert "push nothing at all to the batch branch" not in description


async def test_recorded_conflict_suppresses_the_pending_candidate_section(db):
    """A recorded conflict is the stage's work; a pending-CI hold would contradict it."""
    from src.integration.repair import RepairService

    candidate = "c" * 40
    operation_id = await _seed_root_operation(
        db,
        candidate_state="built",
        candidate_sha=candidate,
        candidate_evidence=False,
        batch_lifecycle="sealing",
    )
    service = RepairService(db)
    async with db.immediate() as conn:
        # Membership is frozen while the batch seals, so the source row is
        # written before the batch leaves sealing.
        await conn.execute(
            insert(integration_review_evidence).values(
                id="review-0",
                source_task_id="source-0",
                repository_id="repo",
                source_base=STARTING_SHA,
                reviewed_head_sha="f" * 40,
                reviewed_tree_sha="e" * 40,
                review_kind="code",
                generation=0,
                verdict="approved",
                evidence={},
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="batch",
                ordinal=0,
                task_id="source-0",
                repository_id="repo",
                source_base_sha=STARTING_SHA,
                reviewed_head_sha="f" * 40,
                reviewed_tree_sha="e" * 40,
                review_evidence_id="review-0",
                review_evidence={},
            )
        )
        await conn.execute(
            update(integration_batches).where(integration_batches.c.id == "batch").values(
                lifecycle="testing"
            )
        )
    await service.start(operation_id, candidate, "batch", now=100.0)
    stage = await _repair_stage(db, operation_id, 0)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_candidate_member_results).values(
                batch_id="batch",
                revision=0,
                member_ordinal=0,
                input_head_sha=candidate,
                input_tree_sha="e" * 40,
                result="conflict",
                conflict_evidence={
                    "operation_id": operation_id,
                    "batch_id": "batch",
                    "revision": 0,
                    "ordinal": 0,
                    "partial_head_sha": "d" * 40,
                    "source_base_sha": STARTING_SHA,
                    "source_head_sha": "f" * 40,
                },
                created_at=1.0,
                updated_at=1.0,
            )
        )
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.id == operation_id
                )
            )
        ).mappings().one()
        description = await service._delegate_description_on(conn, dict(operation), stage)

    assert "## Candidate member conflict" in description
    assert f"Partial head: {'d' * 40}" in description
    assert "## Awaiting candidate CI" not in description
    assert "push nothing at all to the batch branch" not in description


async def test_continuous_batch_timeout_stops_without_advancing_candidate_head(db):
    from src.integration.repair import RepairService

    policy = _policy()
    policy["root"]["repair"]["on_exhausted"] = "continue"
    operation_id = await _seed_root_operation(db, policy=policy)
    service = RepairService(db)
    assert (await service.start(operation_id, STARTING_SHA, "batch", now=100.0))["outcome"] == "started"
    assert (await service.expire(operation_id, 0, now=130.0))["stage"] == 1
    result = await service.expire(operation_id, 1, now=190.0)
    assert result["stage"] == 1 and result["action"] == "supervisor_recovery"
    assert (await db.get_integration_operation(operation_id))["state"] == "escalated"
    assert (await db.get_integration_batch("batch"))["lifecycle"] == "testing"
    stage = await _repair_stage(db, operation_id, 1)
    assert stage["dossier"]["supervisor_recovery"]["subject"]["candidate_sha"] == STARTING_SHA
    assert await service.due_stages(now=300.0) == []


async def _record_root_green(db, service, operation_id, *, now=110.0):
    """Project the candidate and stage identities persisted by green CI."""
    await service.record_result(operation_id, "root-green", now=now)
    async with db.immediate() as conn:
        await conn.execute(update(integration_candidate_revisions).values(
            state="green", ci_evidence_id="root-green",
        ))
        await conn.execute(update(integration_batches).values(
            tested_candidate_sha=STARTING_SHA, ci_evidence_id="root-green",
        ))


async def test_batch_noop_green_keeps_the_normal_promotion_handoff(db):
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(db)
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/integration/batch"), operation_id, "collector",
    )
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    dispatched = await service.dispatch(operation_id, 0)
    delegate = dispatched["repair_task_id"]
    await db.transition_task(delegate, TaskStatus.COMPLETED, force=True)
    async with db.immediate() as conn:
        await conn.execute(update(integration_candidate_revisions).values(state="green", ci_evidence_id="root-green"))
        await conn.execute(update(integration_batches).values(tested_candidate_sha=STARTING_SHA, ci_evidence_id="root-green"))
        await conn.execute(update(integration_branch_owners).values(fence_token=3))
        await conn.execute(insert(task_completion_records).values(
            id="batch-noop-completion", task_id=delegate, outcome="pass", branch="aq/integration/batch",
            commits="[]", completed_at=124.0,
        ))
        await conn.execute(insert(task_metadata).values(
            task_id=delegate, key="accepted_close", value=json.dumps({
                "completion_id": "batch-noop-completion", "session_id": "closing", "claim_epoch": 0,
            }),
        ))
        await conn.execute(insert(integration_outbox).values(
            id="batch-noop-close", dedup_key="batch-noop-close", project_id="p", created_at=123.0,
            available_at=123.0, event_type="integration.repair_delegate_closed", payload={
                "operation_id": operation_id, "stage": 0, "task_id": delegate,
                "session_id": "closing", "workspace_id": "closed", "instance_token": "closed",
                "fence_token": 2,
            },
        ))
    service._promotion = SimpleNamespace(
        _resolve_repository=AsyncMock(return_value=SimpleNamespace(
            retained_git_dir="/tmp/retained", origin_url="/tmp/origin", repo=SimpleNamespace(default_branch="main"))),
        _ensure_retained_repository=AsyncMock(),
        git=SimpleNamespace(als_remote_ref=AsyncMock(return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=STARTING_SHA))),
    )
    result = await service.expire(operation_id, 0, now=150.0)
    assert result["action"] == "none", result
    stage = await _repair_stage(db, operation_id, 0)
    assert stage["state"] == "awaiting_completion"
    assert stage["attempts"] == 1
    assert (await db.get_integration_operation(operation_id))["state"] == "active"
    async with db._engine.connect() as conn:
        owner = (await conn.execute(select(integration_branch_owners))).mappings().one()
    assert (owner["owner_id"], owner["owner_role"], owner["handoff_state"]) == (operation_id, "collector", "reserved")


@pytest.mark.parametrize("now", [110.0, 130.0, 131.0])
async def test_root_green_same_head_adoption_preserves_evidence_and_budget(db, now):
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    await _record_root_green(db, service, operation_id)
    tables = (integration_batches, integration_candidate_revisions, integration_repair_stages)
    async with db.immediate() as conn:
        before = [(await conn.execute(select(table))).mappings().all() for table in tables]
        await service.adopt_batch_repair_on(
            conn, operation_id, head_sha=STARTING_SHA,
            commit_proof={"base_sha": STARTING_SHA, "head_sha": STARTING_SHA, "commits": []},
            now=now,
        )
        after = [(await conn.execute(select(table))).mappings().all() for table in tables]
    assert after == before
    assert after[2][0]["deadline_at"] == 130.0
    assert after[2][0]["state"] == "awaiting_completion"


@pytest.mark.parametrize("invalid", [
    "changed_head", "old_candidate", "current_subject", "success_subject",
    "success_evidence", "missing_evidence", "candidate_evidence", "batch_evidence",
    "tested_sha", "candidate_state", "batch_state", "evidence_revision",
    "evidence_operation", "evidence_batch", "evidence_version", "evidence_failure",
    "evidence_inconclusive", "missing_proof", "stale_proof", "extra_commits",
    "expired_stage", "completed_operation",
])
async def test_root_green_adoption_refuses_changed_head_or_stale_proof(db, invalid):
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    await _record_root_green(db, service, operation_id)
    head = STARTING_SHA
    proof = {"base_sha": head, "head_sha": head, "commits": []}
    subject = {"kind": "batch", "revision": 1, "candidate_sha": "b" * 40}
    mutations = {
        "current_subject": (integration_repair_stages, {"current_subject": subject}),
        "success_subject": (integration_repair_stages, {"success_subject": subject}),
        "success_evidence": (integration_repair_stages, {"success_evidence_id": None}),
        "candidate_evidence": (integration_candidate_revisions, {"ci_evidence_id": None}),
        "batch_evidence": (integration_batches, {"ci_evidence_id": None}),
        "tested_sha": (integration_batches, {"tested_candidate_sha": "b" * 40}),
        "candidate_state": (integration_candidate_revisions, {"state": "testing"}),
        "batch_state": (integration_batches, {"lifecycle": "aborted"}),
        "evidence_revision": (integration_check_evidence, {"candidate_revision": 1}),
        "evidence_operation": (integration_check_evidence, {"operation_id": "other"}),
        "evidence_batch": (integration_check_evidence, {"batch_id": "other"}),
        "evidence_version": (integration_check_evidence, {"required_check_version": "old"}),
        "evidence_failure": (integration_check_evidence, {"conclusion": "failure"}),
        "evidence_inconclusive": (integration_check_evidence, {"classification": "inconclusive"}),
        "expired_stage": (integration_repair_stages, {"state": "expired"}),
        "completed_operation": (integration_repair_operations, {"state": "completed"}),
    }
    async with db.immediate() as conn:
        replacement_evidence_id = "missing" if invalid == "missing_evidence" else None
        if invalid in mutations:
            table, values = mutations[invalid]
            if table is integration_check_evidence:
                # Evidence is append-only: point at a different immutable
                # record instead of editing the original green result.
                evidence = dict((await conn.execute(select(table))).mappings().one())
                replacement_evidence_id = "stale-evidence"
                evidence.update(id=replacement_evidence_id, run_id="stale-run", **values)
                await conn.execute(insert(table).values(**evidence))
            else:
                await conn.execute(update(table).values(**values))
        if replacement_evidence_id is not None:
            for table, column in (
                (integration_repair_stages, "success_evidence_id"),
                (integration_candidate_revisions, "ci_evidence_id"),
                (integration_batches, "ci_evidence_id"),
            ):
                await conn.execute(update(table).values(**{column: replacement_evidence_id}))
        elif invalid == "old_candidate":
            await conn.execute(insert(integration_candidate_revisions).values(
                batch_id="batch", revision=1, construction_base_sha=STARTING_SHA,
                head_sha="b" * 40, state="built", created_at=111.0, updated_at=111.0,
            ))
            await conn.execute(update(integration_batches).values(current_revision=1))
    if invalid == "changed_head":
        head = "b" * 40
        proof = {"base_sha": STARTING_SHA, "head_sha": head, "commits": [head]}
    elif invalid == "missing_proof":
        proof = None
    elif invalid == "stale_proof":
        proof["base_sha"] = "b" * 40
    elif invalid == "extra_commits":
        proof["commits"] = [head]
    tables = (integration_batches, integration_candidate_revisions, integration_repair_stages)
    async with db._engine.connect() as conn:
        before = [(await conn.execute(select(table))).mappings().all() for table in tables]
    with pytest.raises(ValueError):
        async with db.immediate() as conn:
            await service.adopt_batch_repair_on(
                conn, operation_id, head_sha=head, commit_proof=proof, now=111.0,
            )
    async with db._engine.connect() as conn:
        after = [(await conn.execute(select(table))).mappings().all() for table in tables]
    assert after == before


@pytest.mark.parametrize("head", [STARTING_SHA, "b" * 40])
async def test_root_active_adoption_still_refuses_at_deadline(db, head):
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    with pytest.raises(ValueError, match="stage is no longer active"):
        async with db.immediate() as conn:
            await service.adopt_batch_repair_on(
                conn, operation_id, head_sha=head,
                commit_proof={
                    "base_sha": STARTING_SHA, "head_sha": head,
                    "commits": [] if head == STARTING_SHA else [head],
                }, now=130.0,
            )


async def test_an_expired_stage_with_a_late_push_rebuilds_and_releases_its_writer(db):
    """The supported way forward for a batch whose expired stage left a push behind.

    This is the state the incident batch was left in once the ladder ran:stage 0
    out of budget, its writer having already pushed, so its ordinal is spent and
    it must not bind a candidate revision.  Two routes exist, and both are
    pinned here rather than left to the incident: the *batch* rebuilds on the
    successor stage, and the *writer* is not stranded -- its seat is durably
    retired, which is the proof the retired-delegate drain
    (``aq session drain-ack``) needs to stop it after its own ack.  Before this
    task, an attached writer whose stage had lapsed had neither.
    """
    from src.integration.repair import RepairService

    policy = _policy()
    policy["root"]["repair"]["on_exhausted"] = "continue"
    operation_id = await _seed_root_operation(db, policy=policy)
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/integration/batch"), operation_id, "collector",
    )
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    dispatched = await service.dispatch(operation_id, 0)
    delegate = dispatched["repair_task_id"]
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="late-pusher")

    # The writer published before anything could close it, so the stage subject
    # moved off the head its writer was allocated on. That is real progress, so
    # the ladder escalates instead of waiting for a writer that is gone.
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == operation_id,
            integration_repair_stages.c.ordinal == 0,
        ).values(current_subject={"kind": "batch", "revision": 0, "candidate_sha": "e" * 40}))

    expired = await service.expire(operation_id, 0, now=130.0)
    assert (expired["outcome"], expired["action"], expired["stage"]) == (
        "expired", "dispatch_debug", 1,
    )

    spent = await _repair_stage(db, operation_id, 0)
    successor = await _repair_stage(db, operation_id, 1)
    assert (spent["state"], spent["repair_task_id"]) == ("expired", delegate)
    assert (successor["state"], successor["deadline_at"], successor["repair_task_id"]) == (
        "active", 190.0, None,
    )
    rebuilt = await service.dispatch(operation_id, 1)
    assert rebuilt["outcome"] == "dispatched", [
        n["body"] for n in await _notices(db, "integration_repair_dispatch_unknown")
    ]
    assert rebuilt["repair_task_id"] != delegate
    # The rebuild runs on the subject the push left behind.
    assert (await _repair_stage(db, operation_id, 1))["current_subject"] == {
        "kind": "batch", "revision": 0, "candidate_sha": "e" * 40,
    }
    assert (await db.get_integration_operation(operation_id))["active_stage"] == 1

    # A spent ordinal never adopts: the close's own scope is already inactive,
    # so the batch rebuilds on the successor rather than binding this revision
    # from an expired stage.
    async with db.immediate() as conn:
        scope = await db.get_repair_filing_scope(delegate, session_id="late-pusher", conn=conn)
    assert scope is not None
    assert (scope["active"], scope["stage"], scope["operation_id"]) == (
        False, 0, operation_id,
    )

    # And the writer is not left holding an unprovable claim: its seat is
    # retired for good, which is exactly the proof the drain path requires.
    async with db.immediate() as conn:
        retirement = await db.get_retired_integration_writer(delegate, conn=conn)
    assert retirement is not None
    assert retirement["disposition"] == "superseded"
    assert retirement["operation_id"] == operation_id

    # The pool claim that would re-arm a budget re-arms nothing here: the stage
    # belongs to the ladder now.
    assert (await service.start_claimed_stage_budget(delegate, now=140.0)) == {
        "outcome": "no_stage", "task_id": delegate,
    }
    assert (await _repair_stage(db, operation_id, 0))["deadline_at"] == spent["deadline_at"]

    # The rebuild keeps the operation running, so the batch still has a route.
    assert await service.due_stages(now=189.0) == []
    assert [stage["stage"] for stage in await service.due_stages(now=191.0)] == [1]


async def test_a_live_writer_close_rechecks_its_stage_instead_of_being_refused(db):
    """A deadline bounds the wait for a writer, not the writer a stage already holds.

    ``expire`` answers a due stage whose writer is live with a bounded recheck
    (:data:`WRITER_RECHECK_SECONDS`), never a supersession.  The guarded close
    holds that writer's exact fence, so it gets the same answer -- refusing it
    instead is what left a worker holding a claim it could neither complete nor
    release, retrying the same refused close forever.
    """
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    dispatched = await service.dispatch(operation_id, 0)
    delegate = dispatched["repair_task_id"]

    async def close_at(now: float) -> dict:
        async with db.immediate() as conn:
            deferred = await service.defer_lapsed_writer_close(
                conn, operation_id, delegate, now=now
            )
            if not deferred["deferred"]:
                return deferred
            await service.adopt_batch_repair_on(
                conn, operation_id, head_sha=STARTING_SHA,
                commit_proof={
                    "base_sha": STARTING_SHA, "head_sha": STARTING_SHA, "commits": [],
                },
                now=now,
            )
            return deferred

    # Nobody holds the fence yet: the guard stands, and the close is refused.
    await _claimed_writer(db, delegate, epoch=1, live=False, sid="dead-writer")
    stranded = await close_at(130.0)
    assert stranded == {
        "deferred": False, "reason": "writer_not_live",
        "operation_id": operation_id, "stage": 0,
    }
    with pytest.raises(ValueError, match="stage is no longer active"):
        async with db.immediate() as conn:
            await service.adopt_batch_repair_on(
                conn, operation_id, head_sha=STARTING_SHA,
                commit_proof={
                    "base_sha": STARTING_SHA, "head_sha": STARTING_SHA, "commits": [],
                }, now=130.0,
            )
    assert (await _repair_stage(db, operation_id, 0))["deadline_at"] == 130.0

    # The live writer closing late is recheked once, and its close lands.
    await db.create_session(SessionRecord(
        id="late-writer", task_id=delegate, project_id="p", profile_id="repairer",
        harness="fake", provider="fake", name="late-writer", lifecycle="task",
        state="running", desired_state="running", work_dir="/tmp/late-writer",
        epoch="epoch", instance_token="late-writer", started_at=129.0, last_activity=140.0,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == delegate).values(
            status="IN_PROGRESS",
        ))
    deferred = await close_at(140.0)
    assert deferred == {
        "deferred": True, "reason": "writer_live",
        "operation_id": operation_id, "stage": 0, "deadline_at": 440.0,
    }

    stage = await _repair_stage(db, operation_id, 0)
    assert (stage["state"], stage["attempts"], stage["repair_task_id"]) == ("active", 0, delegate)
    assert stage["deadline_at"] == 440.0
    assert stage["dossier"]["deadline_deferrals"] == [
        {
            "reason": "writer_live", "at": 140.0,
            "previous_deadline_at": 130.0, "deadline_at": 440.0,
            "task_id": delegate, "task_status": "IN_PROGRESS", "claim_epoch": 1,
        }
    ]
    assert await _ordinals(db) == [0]
    # A live writer is not news, and a stage inside its budget is untouched.
    assert await _notices(db, "integration_repair_deferred") == []
    assert await close_at(200.0) == {
        "deferred": False, "reason": "within_budget",
        "operation_id": operation_id, "stage": 0, "deadline_at": 440.0,
    }
    assert (await _repair_stage(db, operation_id, 0))["deadline_at"] == 440.0


async def test_root_green_waits_for_promotion_but_new_revision_reuses_budget(db):
    """A main-movement rebuild clears readiness without resetting the stage clock."""
    from src.integration.repair import RepairService

    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    started = await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    green = await service.record_result(operation_id, "root-green", now=110.0)

    waiting = await service.expire(operation_id, 0, now=130.0)
    assert green["action"] == "completion_ready"
    assert waiting == {
        "outcome": "not_due",
        "action": "awaiting_promotion",
        "operation_id": operation_id,
        "stage": 0,
    }
    async with db.immediate() as conn:
        exact_replay = await service.bind_current_batch_subject_on(
            conn, operation_id, now=131.0
        )
    assert exact_replay["subject"] == {
        "kind": "batch",
        "revision": 0,
        "candidate_sha": STARTING_SHA,
    }
    async with db._engine.connect() as conn:
        replayed_stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()
    assert replayed_stage["state"] == "awaiting_completion"
    assert replayed_stage["success_evidence_id"] == "root-green"

    next_sha = "b" * 40
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_candidate_revisions)
            .where(
                integration_candidate_revisions.c.batch_id == "batch",
                integration_candidate_revisions.c.revision == 0,
            )
            .values(state="superseded", updated_at=140.0)
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch",
                revision=1,
                construction_base_sha=next_sha,
                head_sha=next_sha,
                state="testing",
                created_at=140.0,
                updated_at=140.0,
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(current_revision=1, updated_at=140.0)
        )
        rebound = await service.bind_current_batch_subject_on(
            conn, operation_id, now=140.0
        )

    assert rebound == {
        "operation_id": operation_id,
        "stage": 0,
        "subject": {"kind": "batch", "revision": 1, "candidate_sha": next_sha},
        "deadline_due": True,
    }
    async with db._engine.connect() as conn:
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()
    assert stage["started_at"] == started["started_at"]
    assert stage["deadline_at"] == started["deadline_at"]
    assert stage["attempts"] == 1
    assert stage["success_evidence_id"] is None
    assert stage["state"] == "active"
    assert (await service.expire(operation_id, 0, now=140.0))["outcome"] == "expired"


async def test_parent_subject_binding_preserves_budget_and_replay_readiness(db):
    """Only a genuinely newer authoritative parent subject invalidates green."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    service = RepairService(db)
    started = await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    green = await service.record_result("operation", "failed-check", now=105.0)
    assert green["attempts"] == 1

    async with db.immediate() as conn:
        replay = await service.bind_current_parent_subject_on(
            conn, "operation", head_sha=STARTING_SHA, now=110.0
        )
    assert replay["changed"] is False

    next_head = "e" * 40
    async with db.immediate() as conn:
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(generation=4, updated_at=111.0)
        )
        rebound = await service.bind_current_parent_subject_on(
            conn, "operation", head_sha=next_head, now=111.0
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="rebound-failure",
                operation_id="operation",
                parent_task_id="parent",
                parent_generation=4,
                parent_head_sha=next_head,
                producer_id="forge",
                workflow_id="workflow",
                run_id="rebound-run",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": "failure"},
                conclusion="failure",
                classification="conclusive",
                observed_at=112.0,
            )
        )
    assert rebound == {
        "operation_id": "operation",
        "stage": 0,
        "subject": {"kind": "parent", "generation": 4, "head_sha": next_head},
        "deadline_due": False,
        "changed": True,
    }

    exhausted = await service.record_result("operation", "rebound-failure", now=112.0)
    assert exhausted["outcome"] == "escalate"
    async with db._engine.connect() as conn:
        stages = (
            await conn.execute(
                select(integration_repair_stages)
                .where(integration_repair_stages.c.operation_id == "operation")
                .order_by(integration_repair_stages.c.ordinal)
            )
        ).mappings().all()
    assert stages[0]["started_at"] == started["started_at"]
    assert stages[0]["deadline_at"] == started["deadline_at"]
    assert stages[0]["attempts"] == 2
    assert stages[1]["starting_sha"] == next_head
    assert stages[1]["current_subject"] == rebound["subject"]


async def test_repair_start_command_denies_sessions_and_scopes_playbooks(
    command_handler_factory,
):
    """A command capability must not replace exact principal and project authority."""
    handler = await command_handler_factory()
    await _configure_db(handler.db)
    await _seed_parent_operation(handler.db)
    args = {
        "operation_id": "operation",
        "starting_sha": STARTING_SHA,
        "trigger_id": "failed-check",
    }
    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_repair_start"]
        ),
        project_id="p",
        session_id="session",
        task_id="parent",
    )
    with principal_context(session):
        denied_session = await handler.execute("integration_repair_start", args)
    foreign_playbook = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_repair_start"]
        ),
        project_id="foreign",
    )
    with principal_context(foreign_playbook):
        denied_foreign = await handler.execute("integration_repair_start", args)

    assert denied_session["outcome"] == "unauthorized"
    assert denied_foreign["outcome"] == "unauthorized"
    assert await handler.db.get_integration_operation("operation") is not None
    async with handler.db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "operation"
                )
            )
        ).first() is None

    local = await handler.execute("integration_repair_start", args)
    assert local["outcome"] == "started"
    dispatch_session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_repair_dispatch"]
        ),
        project_id="p",
        session_id="session",
        task_id="parent",
    )
    with principal_context(dispatch_session):
        denied_dispatch = await handler.execute(
            "integration_repair_dispatch",
            {"operation_id": "operation", "stage": 0},
        )
    assert denied_dispatch["outcome"] == "unauthorized"


async def test_dispatch_persists_paused_delegate_before_handoff_then_wakes_it(db):
    """No repair writer may become runnable before its durable handoff succeeds."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=4,
                handoff_state="attached",
                session_id="old-session",
                workspace_id="old-workspace",
                created_at=1.0,
                updated_at=1.0,
            )
        )

    observed: dict = {}

    async def confirm_handoff(_owner):
        async with db._engine.connect() as conn:
            stage = (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == "operation",
                        integration_repair_stages.c.ordinal == 0,
                    )
                )
            ).mappings().one()
            task = (
                await conn.execute(
                    select(tasks).where(tasks.c.id == stage["repair_task_id"])
                )
            ).mappings().one()
        observed.update(
            task_id=task["id"],
            status=task["status"],
            writer_kind=stage["writer_kind"],
        )
        return True

    service = RepairService(
        db,
        confirm_handoff=confirm_handoff,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)

    dispatched = await service.dispatch("operation", 0)
    replay = await service.dispatch("operation", 0)

    repair_task_id = "repair-operation-0"
    assert observed == {
        "task_id": repair_task_id,
        "status": "PAUSED",
        "writer_kind": "repair_delegate",
    }
    assert dispatched == {
        "outcome": "dispatched",
        "operation_id": "operation",
        "stage": 0,
        "repair_task_id": repair_task_id,
        "writer_kind": "repair_delegate",
        "fence": {
            "target": {"repository_id": "repo", "branch": "aq/parent"},
            "owner_id": repair_task_id,
            "token": 5,
        },
    }
    assert replay == dispatched | {"outcome": "already_dispatched"}
    task = await db.get_task(repair_task_id)
    assert task.status is TaskStatus.READY
    assert task.parent_task_id is None
    assert task.branch_name == "aq/parent"
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == repair_task_id)
            .values(status="IN_PROGRESS")
        )
        await conn.execute(
            insert(workspaces).values(
                id="launched-repair-workspace",
                project_id="p",
                workspace_path="/tmp/launched-repair",
                source_type="link",
                locked_by_task_id=repair_task_id,
                enabled=True,
                created_at=3.0,
            )
        )
    await db.create_session(
        SessionRecord(
            id="launched-repair-session",
            task_id=repair_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-launched-repair",
            lifecycle="task",
            state="running",
            work_dir="/tmp/launched-repair",
            epoch="epoch",
            instance_token="launched-token",
            started_at=3.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "owner")
            .values(
                handoff_state="attached",
                session_id="launched-repair-session",
                workspace_id="launched-repair-workspace",
            )
        )
    launched_replay = await service.dispatch("operation", 0)
    assert launched_replay == dispatched | {"outcome": "already_dispatched"}
    async with db._engine.connect() as conn:
        origins = (
            await conn.execute(
                select(task_branch_origins).where(
                    task_branch_origins.c.task_id == repair_task_id
                )
            )
        ).all()
    assert origins == []


@pytest.mark.parametrize("held", [False, True])
async def test_continuous_replay_never_releases_operator_held_delegate(db, held):
    """An operator hold on a continuous delegate stays binding until resume."""
    from src.integration.repair import RepairService

    policy = _policy()
    policy["parent"]["repair"]["on_exhausted"] = "continue"
    await _seed_parent_operation(db, policy=policy)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=4,
                handoff_state="attached",
                session_id="old-session",
                workspace_id="old-workspace",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    handoff = {"confirmed": False}
    service = RepairService(db, confirm_handoff=lambda _owner: handoff["confirmed"])
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    # The collector has not confirmed it stopped: the delegate is filed but
    # never launched, so the continuation sweep must keep replaying it.
    interrupted = await service.dispatch("operation", 0)
    assert interrupted["outcome"] == "busy", interrupted
    repair_task_id = interrupted["repair_task_id"]
    assert (await db.get_task(repair_task_id)).status is TaskStatus.PAUSED
    pending = [{"operation_id": "operation", "ordinal": 0}]
    assert await service.pending_dispatches() == pending
    handoff["confirmed"] = True
    if not held:
        dispatched = await service.dispatch("operation", 0)
        assert dispatched["outcome"] == "dispatched", dispatched
        assert (await db.get_task(repair_task_id)).status is TaskStatus.READY
        assert await service.pending_dispatches() == []
        return

    await db.pause_task(repair_task_id)
    assert await service.pending_dispatches() == []
    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner_before = await BranchOwnership(db).get_owner(target)
    # An explicit dispatch must respect the hold before changing ownership.
    refused = await service.dispatch("operation", 0)
    assert refused["outcome"] == "human_required", refused
    assert "held by an operator" in refused["reason"]
    assert (await db.get_task(repair_task_id)).status is TaskStatus.PAUSED
    assert await db.get_task_meta(repair_task_id, "manual_pause") is not None
    assert (await service.dispatch("operation", 0))["outcome"] == "human_required"
    assert (await db.get_task(repair_task_id)).status is TaskStatus.PAUSED
    assert await service.pending_dispatches() == []
    assert await BranchOwnership(db).get_owner(target) == owner_before

    await db.resume_task(repair_task_id)
    assert (await db.get_task(repair_task_id)).status is TaskStatus.READY
    assert await db.get_task_meta(repair_task_id, "manual_pause") is None
    assert (await service.dispatch("operation", 0))["outcome"] == "dispatched"
    assert (await BranchOwnership(db).get_owner(target))["owner_id"] == repair_task_id
    assert (await service.dispatch("operation", 0))["outcome"] == "already_dispatched"
    assert await service.pending_dispatches() == []













async def test_repair_dispatch_command_derives_current_stage_with_real_service(
    command_handler_factory,
):
    """Operation-bound events cannot choose or restart a repair stage."""
    from src.integration.repair import RepairService

    handler = await command_handler_factory()
    await _configure_db(handler.db)
    await _seed_parent_operation(handler.db)
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="command-owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=4,
                handoff_state="attached",
                session_id="old-session",
                workspace_id="old-workspace",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        handler.db,
        confirm_handoff=lambda _owner: True,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    handler.orchestrator.repair_service = service
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_repair_dispatch"]
        ),
        project_id="p",
    )

    with principal_context(principal):
        stale = await handler.execute(
            "integration_repair_dispatch",
            {
                "operation_id": "operation",
                "batch_id": "foreign-batch",
                "revision": 0,
                "head_sha": STARTING_SHA,
            },
        )
        result = await handler.execute(
            "integration_repair_dispatch", {"operation_id": "operation"}
        )

    assert stale["outcome"] == "stale"
    assert result["outcome"] == "dispatched"
    assert result["stage"] == 0
    assert result["repair_task_id"] == "repair-operation-0"
    await handler.db.close()


async def test_primary_dispatch_reuses_only_exact_live_attached_verifier(
    command_handler_factory,
):
    """Verifier reuse keeps verifier ownership and works without a primary route."""
    from src.integration.repair import RepairService

    handler = await command_handler_factory()
    db = handler.db
    await _configure_db(db)
    await _seed_parent_operation(db)
    await db.create_task(
        Task(
            id="verifier",
            project_id="p",
            title="Verifier",
            description="",
            status=TaskStatus.IN_PROGRESS,
            repo_id="repo",
            branch_name="aq/parent",
            profile_id="repairer", route_source="legacy",
        )
    )
    await db.create_session(
        SessionRecord(
            id="verifier-session",
            task_id="verifier",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-verifier",
            lifecycle="task",
            state="running",
            work_dir="/tmp/verifier",
            epoch="epoch",
            instance_token="token",
            started_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "operation")
            .values(verifier_task_id="verifier")
        )
        await conn.execute(
            insert(workspaces).values(
                id="verifier-workspace",
                project_id="p",
                workspace_path="/tmp/verifier",
                source_type="link",
                locked_by_task_id="verifier",
                enabled=True,
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="verifier",
                owner_role="verifier",
                fence_token=7,
                handoff_state="attached",
                session_id="verifier-session",
                workspace_id="verifier-workspace",
                created_at=2.0,
                updated_at=2.0,
            )
        )

    async def stopped(owner):
        await db.update_session(
            owner["session_id"], state="stopped", desired_state="stopped"
        )
        return {
            "session_id": owner["session_id"],
            "workspace_id": owner["workspace_id"],
            "head_sha": "f" * 40,
            "instance_token": "token",
        }

    service = RepairService(
        db,
        confirm_stopped=stopped,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    reused = await service.dispatch("operation", 0)
    replay = await service.dispatch("operation", 0)

    expected = {
        "outcome": "writer_reused",
        "operation_id": "operation",
        "stage": 0,
        "repair_task_id": "verifier",
        "writer_kind": "existing_verifier",
        "fence": {
            "target": {"repository_id": "repo", "branch": "aq/parent"},
            "owner_id": "verifier",
            "token": 7,
        },
    }
    assert reused == expected
    assert replay == expected
    owner = await BranchOwnership(db).get_owner(BranchKey(repository_id="repo", branch="aq/parent"))
    assert owner["owner_role"] == "verifier"

    await service.record_result("operation", "failed-check", now=101.0)
    next_head = "e" * 40
    handler.orchestrator.git.aget_current_branch = AsyncMock(return_value="aq/parent")
    handler.orchestrator.git._arun = AsyncMock(
        side_effect=lambda args, *, cwd: "" if args[0] == "status" else next_head
    )
    handler.orchestrator.git.als_remote_ref = AsyncMock(
        return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=next_head)
    )
    handler.orchestrator.git.ais_ancestor = AsyncMock(return_value=True)
    handler._current_scope = {
        "kind": "session",
        "session_id": "verifier-session",
        "session_instance_token": "token",
        "task_id": "verifier",
        "project_id": "p",
        "elevated": False,
    }
    filed = await handler._cmd_create_task(
        {"title": "Verifier finding", "description": "fix", "reason": "found in repair"}
    )
    assert filed["success"] is True
    child = await db.get_task(filed["task_id"])
    assert child.parent_task_id == "parent"
    # Filing under the repaired parent must not materialize an origin chain
    # for the verifier itself, which would re-branch it off ``aq/parent``.
    assert (await db.get_task("verifier")).branch_name == "aq/parent"
    owner = await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert (owner["owner_id"], owner["owner_role"]) == ("verifier", "verifier")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_check_evidence).values(
                id="verifier-failed-2",
                operation_id="operation",
                parent_task_id="parent",
                parent_generation=4,
                parent_head_sha=next_head,
                producer_id="forge",
                workflow_id="workflow",
                run_id="verifier-run-2",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": "failure"},
                conclusion="failure",
                classification="conclusive",
                observed_at=102.0,
            )
        )
    await service.record_result("operation", "verifier-failed-2", now=102.0)
    debug = await service.dispatch("operation", 1)
    assert debug["outcome"] == "dispatched"
    assert (await db.get_task("verifier")).status is TaskStatus.PAUSED
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED
    assert (await db.get_integration_checkpoint("parent"))["episode_id"] == "episode"


@pytest.mark.parametrize(
    "retained_state",
    ["tracked", "untracked", "unmerged", "unpushed"],
)
@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_debug_dispatch_retains_unfinished_primary_workspace_atomically(
    db, tmp_path, retained_state, lifecycle
):
    """A stopped primary's exact workspace is rebound before debug becomes ready."""
    from src.integration.repair import RepairService

    checkout = tmp_path / "retained"
    remote = tmp_path / "remote.git"

    def git(*args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=check,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    checkout.mkdir()
    git("init", "-b", "aq/parent")
    git("config", "user.name", "Repair Test")
    git("config", "user.email", "repair@example.test")
    (checkout / "tracked.txt").write_text("base\n")
    git("add", "tracked.txt")
    git("commit", "-m", "base")
    git("remote", "add", "origin", str(remote))
    git("push", "-u", "origin", "aq/parent")
    published_head = git("rev-parse", "HEAD")
    if retained_state == "tracked":
        (checkout / "tracked.txt").write_text("tracked edit\n")
    elif retained_state == "untracked":
        (checkout / "untracked.txt").write_text("untracked work\n")
    elif retained_state == "unpushed":
        (checkout / "unpushed.txt").write_text("committed locally\n")
        git("add", "unpushed.txt")
        git("commit", "-m", "local repair")
        assert git("rev-parse", "HEAD") != git(
            "rev-parse", "refs/remotes/origin/aq/parent"
        )
    else:
        git("switch", "-c", "conflicting-repair")
        (checkout / "tracked.txt").write_text("debug side\n")
        git("commit", "-am", "debug side")
        git("switch", "aq/parent")
        (checkout / "tracked.txt").write_text("primary side\n")
        git("commit", "-am", "primary side")
        git("merge", "conflicting-repair", check=False)
        assert "UU tracked.txt" in git("status", "--porcelain=v1")
    retained_head = git("rev-parse", "HEAD")

    policy = _policy()
    if lifecycle == "pool":
        policy["parent"]["repair"]["on_exhausted"] = "continue"
    await _seed_parent_operation(db, starting_sha=published_head, policy=policy)
    await _add_parent_evidence(
        db, "failed-check-2", run_id="run-2", conclusion="failure", head_sha=published_head
    )
    async with db.immediate() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="parent-origin", task_id="parent", repository_id="repo",
            base_sha=published_head, creation_generation=0, reserved=True,
            materialized=True, created_at=1.0, materialized_at=1.0,
        ))
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )

    async def stopped(owner):
        await db.update_session(
            owner["session_id"], state="stopped", desired_state="stopped"
        )
        return {
            "session_id": owner["session_id"],
            "workspace_id": owner["workspace_id"],
            "head_sha": retained_head,
            "instance_token": (await db.get_session(owner["session_id"])).instance_token,
        }

    service = RepairService(
        db,
        confirm_stopped=stopped,
    )
    await service.start("operation", published_head, "failed-check", now=100.0)
    primary = await service.dispatch("operation", 0)
    primary_task_id = primary["repair_task_id"]
    await db.create_session(
        SessionRecord(
            id="primary-session",
            task_id=primary_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-primary",
            lifecycle=lifecycle,
            last_claim_epoch=1 if lifecycle == "pool" else None,
            state="running",
            work_dir=str(checkout),
            epoch="epoch",
            instance_token="token",
            started_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="retained",
                project_id="p",
                workspace_path=str(checkout),
                source_type="link",
                locked_by_task_id=primary_task_id,
                enabled=True,
                created_at=2.0,
            )
        )
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == primary_task_id)
            .values(status="IN_PROGRESS", claim_epoch=1 if lifecycle == "pool" else 0)
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "owner")
            .values(
                handoff_state="attached",
                session_id="primary-session",
                workspace_id="retained",
            )
        )
    await service.record_result("operation", "failed-check", now=101.0)
    await service.record_result("operation", "failed-check-2", now=102.0)
    if lifecycle == "pool":
        from src.claim_file import write_claim_file
        write_claim_file(str(checkout), {"task_id": primary_task_id, "claim_epoch": 1})

    failed_stop = await RepairService(
        db,
        confirm_stopped=lambda _owner: False,
    ).dispatch("operation", 1)
    assert failed_stop["outcome"] == "busy"
    assert (await db.get_task(failed_stop["repair_task_id"])).status is TaskStatus.PAUSED
    assert (await db.get_workspace("retained")).locked_by_task_id == primary_task_id

    debug, concurrent = await asyncio.gather(
        service.dispatch("operation", 1),
        service.dispatch("operation", 1),
    )
    if debug["outcome"] != "dispatched":
        debug, concurrent = concurrent, debug
    replay = await service.dispatch("operation", 1)

    assert debug["outcome"] == "dispatched"
    assert concurrent["outcome"] in {"already_dispatched", "busy"}
    assert concurrent.get("repair_task_id") == debug["repair_task_id"]
    assert replay == debug | {"outcome": "already_dispatched"}
    debug_task = await db.get_task(debug["repair_task_id"])
    assert debug_task.status is TaskStatus.READY
    assert debug_task.preferred_workspace_id == "retained"
    assert (await db.get_task(primary_task_id)).status is TaskStatus.BLOCKED
    assert await db.get_task_meta(primary_task_id, "blocked_terminal") == (
        "integration_repair_retained_handoff"
    )
    workspace = await db.get_workspace("retained")
    assert workspace.locked_by_task_id == debug_task.id
    async with db._engine.connect() as conn:
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "operation",
                    integration_repair_stages.c.ordinal == 1,
                )
            )
        ).mappings().one()
    assert stage["retained_workspace_id"] == "retained"
    assert stage["starting_sha"] == published_head
    assert stage["current_subject"]["head_sha"] == published_head
    assert stage["dossier"]["branch_sha"] == retained_head
    assert stage["retained_handoff"] == {
        "old_task_id": primary_task_id,
        "new_task_id": debug_task.id,
        "old_session_id": "primary-session",
        "workspace_id": "retained",
        "old_fence_token": 2,
        "new_fence_token": 3,
        "head_sha": retained_head,
        "instance_token": "token",
    }
    from src.orchestrator.workspace import WorkspaceMixin

    runtime = SimpleNamespace(db=db, git=GitManager())
    project = await db.get_project("p")
    origin, fence, role = await WorkspaceMixin._hierarchy_origin_and_fence(
        runtime, debug_task, project
    )
    assert role == "repair" and origin["base_sha"] == published_head
    # Pool admission proves remote ancestry before it attaches the retained
    # checkout. Local-only commits must not become that required remote base.
    assert await WorkspaceMixin._hierarchy_repair_start(
        runtime, str(checkout), origin, fence, repository_url=str(remote)
    ) == published_head
    assert git("ls-remote", "origin", "refs/heads/aq/parent").split()[0] == published_head
    before = (
        git("status", "--porcelain=v1"),
        git("ls-files", "--stage"),
        retained_head,
        {
            path.name: path.read_bytes()
            for path in checkout.iterdir()
            if path.name != ".git" and path.is_file()
        },
    )
    prepared = await WorkspaceMixin._prepare_exact_origin_workspace(
        runtime,
        debug_task,
        project,
        SimpleNamespace(workspace=workspace),
        origin,
        fence,
    )
    assert prepared == "aq/parent"
    after = (
        git("status", "--porcelain=v1"),
        git("ls-files", "--stage"),
        git("rev-parse", "HEAD"),
        {
            path.name: path.read_bytes()
            for path in checkout.iterdir()
            if path.name != ".git" and path.is_file()
        },
    )
    assert after == before
    if lifecycle == "pool":
        from src.claim_file import read_claim_file, write_claim_file
        assert (await db.get_session("primary-session")).task_id is None
        assert read_claim_file(str(checkout)) is None
        await db.create_session(SessionRecord(
            id="successor-session", task_id=debug_task.id, project_id="p",
            profile_id="repairer", harness="fake", provider="fake", name="s-successor",
            lifecycle="pool", state="running", work_dir=str(checkout), epoch="next",
            instance_token="next-token", started_at=104.0, last_claim_epoch=2))
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == debug_task.id).values(
                status="IN_PROGRESS", claim_epoch=2))
            await conn.execute(update(integration_branch_owners).where(
                integration_branch_owners.c.id == "owner").values(
                handoff_state="attached", session_id="successor-session", workspace_id="retained"))
        write_claim_file(str(checkout), {"task_id": debug_task.id, "claim_epoch": 2})
        if retained_head == published_head:
            # Uncommitted checkout work never advances the published subject.
            # Its writer is live, so the clock neither retires it nor burns
            # another stage: the deadline is revisited and everything stays put.
            expired = await service.expire("operation", 1, now=162.0)
            assert expired["stage"] == 1
            assert (expired["action"], expired["reason"]) == ("wait", "writer_live")
            stage = await _repair_stage(db, "operation", 1)
            assert stage["state"] == "active" and stage["deadline_at"] == 462.0
            assert (await db.get_integration_operation("operation"))["active_stage"] == 1
            assert (await db.get_workspace("retained")).locked_by_task_id == debug_task.id
            assert git("rev-parse", "HEAD") == retained_head
            assert git("ls-files", "--stage") == before[1]
            assert (checkout / "tracked.txt").read_bytes() == before[3]["tracked.txt"]
            return
        # Committed checkout progress renews continuous work only once it is
        # published: push it, then bind the exact advanced subject.
        git("push", "origin", f"{retained_head}:refs/heads/aq/parent")
        async with db.immediate() as conn:
            await service.bind_current_parent_subject_on(
                conn, "operation", head_sha=retained_head, now=150.0
            )
        assert (await service.expire("operation", 1, now=162.0))["stage"] == 2
        third = await service.dispatch("operation", 2)
        assert third["outcome"] == "dispatched"
        assert third["repair_task_id"] != debug_task.id
        assert (await db.get_session("successor-session")).task_id is None
        assert read_claim_file(str(checkout)) is None
        assert (await db.get_workspace("retained")).locked_by_task_id == third["repair_task_id"]
        third_task = await db.get_task(third["repair_task_id"])
        origin, fence, role = await WorkspaceMixin._hierarchy_origin_and_fence(
            runtime, third_task, project
        )
        assert role == "repair" and origin["base_sha"] == retained_head
        assert await WorkspaceMixin._hierarchy_repair_start(
            runtime, str(checkout), origin, fence, repository_url=str(remote)
        ) == retained_head
        assert await WorkspaceMixin._prepare_exact_origin_workspace(
            runtime, third_task, project,
            SimpleNamespace(workspace=await db.get_workspace("retained")), origin, fence,
        ) == "aq/parent"
        assert git("rev-parse", "HEAD") == retained_head
        assert git("status", "--porcelain=v1") == before[0]
        assert git("ls-files", "--stage") == before[1]


async def test_debug_dossier_refreshes_exact_unpushed_commits_and_late_receipts(
    db, tmp_path
):
    """Fresh debug context snapshots exact lineage and current matching receipts."""
    from src.integration.repair import RepairService

    checkout = tmp_path / "dossier-retained"
    checkout.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-b", "aq/parent")
    git("config", "user.name", "Dossier Test")
    git("config", "user.email", "dossier@example.test")
    (checkout / "repair.txt").write_text("base\n")
    git("add", "repair.txt")
    git("commit", "-m", "base")
    base_sha = git("rev-parse", "HEAD")

    await _seed_parent_operation(db, starting_sha=base_sha)
    await _add_parent_evidence(
        db,
        "dossier-failed-2",
        run_id="dossier-run-2",
        conclusion="failure",
        head_sha=base_sha,
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="dossier-owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(db)
    await service.start("operation", base_sha, "failed-check", now=100.0)
    async with db._engine.connect() as conn:
        initial_dossier = (
            await conn.execute(
                select(integration_repair_stages.c.dossier).where(
                    integration_repair_stages.c.operation_id == "operation",
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).scalar_one()

    # This receipt is deliberately finalized after primary stage creation.
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="late-receipt",
                domain_key="late-parent-receipt",
                source_task_id=None,
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                disposition="noop",
                resolution_evidence={"reason": "resolved during primary repair"},
                parent_operation_id="operation",
                parent_episode_id="episode",
                created_at=101.0,
            )
        )
    primary = await service.dispatch("operation", 0)
    primary_task_id = primary["repair_task_id"]
    await db.create_session(
        SessionRecord(
            id="dossier-primary-session",
            task_id=primary_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-dossier-primary",
            lifecycle="task",
            state="running",
            work_dir=str(checkout),
            epoch="epoch",
            instance_token="dossier-token",
            started_at=102.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="dossier-workspace",
                project_id="p",
                workspace_path=str(checkout),
                source_type="link",
                locked_by_task_id=primary_task_id,
                enabled=True,
                created_at=102.0,
            )
        )
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == primary_task_id)
            .values(status="IN_PROGRESS")
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "dossier-owner")
            .values(
                handoff_state="attached",
                session_id="dossier-primary-session",
                workspace_id="dossier-workspace",
            )
        )
    await service.record_result("operation", "failed-check", now=103.0)
    await service.record_result("operation", "dossier-failed-2", now=104.0)

    for index in (1, 2, 3):
        with (checkout / "repair.txt").open("a") as stream:
            stream.write(f"repair {index}\n")
        git("add", "repair.txt")
        git("commit", "-m", f"repair {index}")
    head_sha = git("rev-parse", "HEAD")
    exact_commits = git("rev-list", "--reverse", f"{base_sha}..{head_sha}").splitlines()
    assert len(exact_commits) == 3

    from src.orchestrator.workspace import WorkspaceMixin

    provider = SimpleNamespace(
        stop=AsyncMock(),
        confirm_stopped=AsyncMock(return_value=True),
    )
    runtime = SimpleNamespace(
        db=db,
        config=SimpleNamespace(),
        session_providers=SimpleNamespace(create=lambda *_args: provider),
        git=GitManager(),
    )

    async def stopped(owner):
        return await WorkspaceMixin.aconfirm_integration_owner_stopped_for_repair(
            runtime, owner
        )

    handoff_service = RepairService(
        db,
        confirm_stopped=stopped,
    )
    debug = await handoff_service.dispatch("operation", 1)
    assert debug["outcome"] == "dispatched"
    retained = await db.get_active_integration_repair_for_task(debug["repair_task_id"])
    assert retained["stage_starting_sha"] == base_sha
    assert retained["stage_subject"]["head_sha"] == base_sha
    assert retained["stage_dossier"]["branch_sha"] == head_sha
    assert retained["stage_dossier"]["repair_commits"] == exact_commits
    commit_proof = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "commits": exact_commits,
    }
    async with db.immediate() as conn:
        # Publication-backed subject binding is a separate transition from
        # retaining a checkout. It can still advance with the exact proof.
        bound = await handoff_service.bind_current_parent_subject_on(
            conn, "operation", head_sha=head_sha, commit_proof=commit_proof, now=105.0
        )
        assert bound["changed"] is True
        replay = await handoff_service.bind_current_parent_subject_on(
            conn,
            "operation",
            head_sha=head_sha,
            commit_proof=commit_proof,
            now=105.0,
        )
    assert replay["changed"] is False
    assert (await handoff_service.dispatch("operation", 1))["outcome"] == (
        "already_dispatched"
    )
    async with db.immediate() as conn:
        with pytest.raises(
            ValueError, match="repair commit proof does not match the current subject"
        ):
            await handoff_service.bind_current_parent_subject_on(
                conn,
                "operation",
                head_sha=base_sha,
                commit_proof=commit_proof,
                now=106.0,
            )

    async with db._engine.connect() as conn:
        stages = (
            await conn.execute(
                select(integration_repair_stages)
                .where(integration_repair_stages.c.operation_id == "operation")
                .order_by(integration_repair_stages.c.ordinal)
            )
        ).mappings().all()
    dossier = stages[1]["dossier"]
    assert dossier["repair_commits"] == exact_commits
    assert [receipt["id"] for receipt in dossier["receipts"]] == ["late-receipt"]
    assert dossier["manifest"] == initial_dossier["manifest"]
    assert dossier["previous_stage"]["dossier"]["budget"] == {
        **initial_dossier["budget"],
        "attempts": 2,
    }
    assert dossier["budget"] == {
        "ordinal": 1,
        "started_at": 104.0,
        "deadline_at": 164.0,
        "attempt_limit": 1,
        "attempts": 0,
    }
    debug_task = await db.get_task(debug["repair_task_id"])
    assert "late-receipt" in debug_task.description
    assert all(commit_sha in debug_task.description for commit_sha in exact_commits)


async def _prepare_retained_debug_boundary(db, workspace_path: str, head_sha: str):
    """Create an attached primary exactly at the debug handoff boundary."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db, "failed-check-2", run_id="run-2", conclusion="failure"
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="boundary-owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    primary_service = RepairService(db)
    await primary_service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    primary = await primary_service.dispatch("operation", 0)
    primary_task_id = primary["repair_task_id"]
    await db.create_session(
        SessionRecord(
            id="boundary-primary-session",
            task_id=primary_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-boundary-primary",
            lifecycle="task",
            state="running",
            work_dir=workspace_path,
            epoch="epoch",
            instance_token="boundary-token",
            started_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="boundary-workspace",
                project_id="p",
                kind_id="project-repo",
                workspace_path=workspace_path,
                source_type="link",
                locked_by_task_id=primary_task_id,
                enabled=True,
                created_at=2.0,
            )
        )
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == primary_task_id)
            .values(status="IN_PROGRESS")
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "boundary-owner")
            .values(
                handoff_state="attached",
                session_id="boundary-primary-session",
                workspace_id="boundary-workspace",
            )
        )
    await primary_service.record_result("operation", "failed-check", now=101.0)
    await primary_service.record_result("operation", "failed-check-2", now=102.0)
    proof = {
        "session_id": "boundary-primary-session",
        "workspace_id": "boundary-workspace",
        "head_sha": head_sha,
        "instance_token": "boundary-token",
    }

    async def stopped(_owner):
        await db.update_session(
            "boundary-primary-session", state="stopped", desired_state="stopped"
        )
        return proof

    return primary_task_id, stopped


@pytest.mark.parametrize("crash_point", ["before_cas", "after_cas", "after_ready"])
async def test_retained_dispatch_recovers_at_each_durable_boundary(
    db, tmp_path, monkeypatch, crash_point
):
    """A response-loss crash at each handoff boundary resumes one debug writer."""
    from src.integration.repair import RepairService

    primary_task_id, stopped = await _prepare_retained_debug_boundary(
        db, str(tmp_path), STARTING_SHA
    )
    original_stopped = stopped
    if crash_point == "before_cas":
        failed = False

        async def stopped_once(owner):
            nonlocal failed
            if not failed:
                failed = True
                await db.update_session(
                    "boundary-primary-session",
                    state="stopped",
                    desired_state="stopped",
                )
                raise RuntimeError("injected before retained CAS")
            return await original_stopped(owner)

        stopped = stopped_once
    service = RepairService(
        db,
        confirm_stopped=stopped,
    )
    if crash_point == "after_cas":
        original_handoff = service._retained_debug_handoff
        failed = False

        async def crash_after_cas(*args, **kwargs):
            nonlocal failed
            fence = await original_handoff(*args, **kwargs)
            if not failed:
                failed = True
                raise RuntimeError("injected after retained CAS")
            return fence

        monkeypatch.setattr(service, "_retained_debug_handoff", crash_after_cas)
    if crash_point == "after_ready":
        original_notify = db._notify_ready
        failed = False

        async def crash_after_ready(task_ids):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("injected after READY commit")
            return await original_notify(task_ids)

        monkeypatch.setattr(db, "_notify_ready", crash_after_ready)

    with pytest.raises(RuntimeError, match="injected"):
        await service.dispatch("operation", 1)
    recovered = await service.dispatch("operation", 1)
    debug_task_id = recovered["repair_task_id"]
    assert recovered["outcome"] in {"dispatched", "already_dispatched"}
    assert (await db.get_task(debug_task_id)).status is TaskStatus.READY
    assert (await db.get_workspace("boundary-workspace")).locked_by_task_id == debug_task_id
    owner = await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert (owner["owner_id"], owner["owner_role"], owner["handoff_state"]) == (
        debug_task_id,
        "repair",
        "reserved",
    )
    assert (await db.get_task(primary_task_id)).status is TaskStatus.BLOCKED


async def test_scheduler_launches_retained_debug_in_exact_workspace(
    session_orch, tmp_path
):
    """The real scheduler preparation and launch attach the retained checkout."""
    from src.git.manager import GitManager
    from src.integration.repair import RepairService
    from src.intelligence_classes import IntelligenceClass
    from tests.session_dispatch_helpers import fake_provider

    db = session_orch.db
    await _configure_db(db)
    await db.update_profile(
        "debugger", harness="claude", default_class="debug-high"
    )
    session_orch.session_spec_builder._intelligence_classes["debug-high"] = (
        IntelligenceClass(
            "debug-high",
            "Debug high",
            "",
            {"anthropic": {"model": "claude-sonnet-5"}},
        )
    )
    await db.create_agent(
        Agent(
            id="debug-agent",
            name="Debug Agent",
            profile_id="debugger",
            state=AgentState.IDLE,
        )
    )
    checkout = tmp_path / "scheduler-retained"
    checkout.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-b", "aq/parent")
    git("config", "user.name", "Repair Test")
    git("config", "user.email", "repair@example.test")
    (checkout / "tracked.txt").write_text("base\n")
    git("add", "tracked.txt")
    git("commit", "-m", "base")
    retained_head = git("rev-parse", "HEAD")
    (checkout / "tracked.txt").write_text("unfinished\n")
    (checkout / "untracked.txt").write_text("untracked\n")
    before = (
        git("rev-parse", "HEAD"),
        git("ls-files", "--stage"),
        (checkout / "tracked.txt").read_bytes(),
        (checkout / "untracked.txt").read_bytes(),
    )
    _primary_task_id, stopped = await _prepare_retained_debug_boundary(
        db, str(checkout), retained_head
    )
    debug = await RepairService(
        db,
        confirm_stopped=stopped,
    ).dispatch("operation", 1)
    debug_task_id = debug["repair_task_id"]
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="parent-origin",
                task_id="parent",
                repository_id="repo",
                base_sha=STARTING_SHA,
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
    session_orch.git = GitManager()
    await session_orch._execute_task(
        AssignAction("debug-agent", debug_task_id, "p")
    )

    session = await db.get_session_for_task(debug_task_id)
    owner = await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert session is not None and session.state == "running"
    assert session.work_dir == str(checkout)
    assert owner["handoff_state"] == "attached"
    assert owner["session_id"] == session.id
    assert owner["workspace_id"] == "boundary-workspace"
    assert len(fake_provider(session_orch).starts) == 1
    launched_replay = await RepairService(db).dispatch("operation", 1)
    assert launched_replay == debug | {"outcome": "already_dispatched"}
    after = (
        git("rev-parse", "HEAD"),
        git("ls-files", "--stage"),
        (checkout / "tracked.txt").read_bytes(),
        (checkout / "untracked.txt").read_bytes(),
    )
    assert after == before


@pytest.mark.parametrize(
    ("writer_kind", "owner_role"),
    [
        ("existing_verifier", "repair"),
        ("repair_delegate", "verifier"),
    ],
)
async def test_retained_handoff_rejects_mismatched_writer_kind_and_owner_role(
    db, writer_kind, owner_role
):
    """The durable writer subtype and current branch role must be one exact pair."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db, "failed-check-2", run_id="run-2", conclusion="failure"
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        db,
        confirm_stopped=lambda owner: {
            "session_id": owner["session_id"],
            "workspace_id": owner["workspace_id"],
            "head_sha": "f" * 40,
            "instance_token": "primary-token",
        },
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    primary = await service.dispatch("operation", 0)
    await db.create_session(
        SessionRecord(
            id="primary-session",
            task_id=primary["repair_task_id"],
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-primary",
            lifecycle="task",
            state="stopped",
            desired_state="stopped",
            work_dir="/tmp/retained-role",
            epoch="epoch",
            instance_token="primary-token",
            started_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="retained-role",
                project_id="p",
                workspace_path="/tmp/retained-role",
                source_type="link",
                locked_by_task_id=primary["repair_task_id"],
                enabled=True,
                created_at=2.0,
            )
        )
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == "operation",
                integration_repair_stages.c.ordinal == 0,
            )
            .values(writer_kind=writer_kind)
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "owner")
            .values(
                owner_role=owner_role,
                handoff_state="attached",
                session_id="primary-session",
                workspace_id="retained-role",
            )
        )
    await service.record_result("operation", "failed-check", now=101.0)
    await service.record_result("operation", "failed-check-2", now=102.0)

    refused = await service.dispatch("operation", 1)

    # An unexpected owner shape is mechanical: retryable, never a human gate.
    assert refused["outcome"] == "unknown"
    assert refused["reason_code"] == "owner_not_predecessor"
    owner = await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert (owner["owner_id"], owner["owner_role"], owner["fence_token"]) == (
        primary["repair_task_id"],
        owner_role,
        2,
    )


async def test_retained_stop_proof_rejects_replaced_session_instance(db):
    """A confirmed old process cannot mark a replacement instance stopped."""
    from src.orchestrator.workspace import WorkspaceMixin

    await _seed_parent_operation(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS")
        )
        await conn.execute(
            insert(workspaces).values(
                id="stop-workspace",
                project_id="p",
                workspace_path="/tmp/stop-workspace",
                source_type="link",
                locked_by_task_id="parent",
                enabled=True,
                created_at=2.0,
            )
        )
    await db.create_session(
        SessionRecord(
            id="stop-session",
            task_id="parent",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-stop",
            lifecycle="task",
            state="running",
            work_dir="/tmp/stop-workspace",
            epoch="epoch",
            instance_token="old-token",
            started_at=2.0,
        )
    )

    async def replace_instance(_handle):
        await db.update_session("stop-session", instance_token="replacement-token")
        return True

    provider = SimpleNamespace(
        stop=AsyncMock(),
        confirm_stopped=AsyncMock(side_effect=replace_instance),
    )
    runtime = SimpleNamespace(
        db=db,
        config=SimpleNamespace(),
        session_providers=SimpleNamespace(create=lambda *_args: provider),
        git=SimpleNamespace(
            aget_current_branch=AsyncMock(return_value="aq/parent"),
            _arun=AsyncMock(return_value="f" * 40),
        ),
    )

    proof = await WorkspaceMixin.aconfirm_integration_owner_stopped_for_repair(
        runtime,
        {
            "repository_id": "repo",
            "ref": "aq/parent",
            "owner_id": "parent",
            "owner_role": "verifier",
            "fence_token": 7,
            "handoff_state": "attached",
            "session_id": "stop-session",
            "workspace_id": "stop-workspace",
        },
    )

    assert proof is None
    session = await db.get_session("stop-session")
    assert session.instance_token == "replacement-token"
    assert session.state == "running"


async def test_running_repair_delegate_files_real_child_from_clean_pushed_head(
    command_handler_factory,
    monkeypatch,
):
    """Repair filing uses the writer's current pushed head and keeps its budget."""
    from src.integration.repair import RepairService

    handler = await command_handler_factory()
    await _configure_db(handler.db)
    await _seed_parent_operation(handler.db)
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        handler.db,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await service.record_result("operation", "failed-check", now=101.0)
    dispatched = await service.dispatch("operation", 0)
    repair_task_id = dispatched["repair_task_id"]
    next_head = "e" * 40
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == repair_task_id)
            .values(status="IN_PROGRESS")
        )
        await conn.execute(
            insert(workspaces).values(
                id="repair-workspace",
                project_id="p",
                workspace_path="/tmp/repair",
                source_type="link",
                locked_by_task_id=repair_task_id,
                enabled=True,
                created_at=2.0,
            )
        )
    await handler.db.create_session(
        SessionRecord(
            id="repair-session",
            task_id=repair_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-repair",
            lifecycle="task",
            state="running",
            work_dir="/tmp/repair",
            epoch="epoch",
            instance_token="token",
            started_at=2.0,
        )
    )
    handler.orchestrator.git.aget_current_branch = AsyncMock(return_value="aq/parent")

    async def run_git(args, *, cwd):
        assert cwd == "/tmp/repair"
        return "" if args[0] == "status" else next_head

    handler.orchestrator.git._arun = AsyncMock(side_effect=run_git)
    handler.orchestrator.git.als_remote_ref = AsyncMock(
        return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=next_head)
    )
    handler.orchestrator.git.ais_ancestor = AsyncMock(return_value=True)
    handler._current_scope = {
        "kind": "session",
        "session_id": "repair-session",
        "session_instance_token": "token",
        "task_id": repair_task_id,
        "project_id": "p",
        "elevated": False,
    }

    reserved = await handler._cmd_create_task(
        {"title": "Too early", "description": "fix", "reason": "not attached"}
    )
    assert reserved["success"] is False
    assert reserved["error"] == "repair stage is no longer active"

    await handler.db.create_session(
        SessionRecord(
            id="wrong-repair-session",
            task_id=repair_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-wrong-repair",
            lifecycle="task",
            state="running",
            work_dir="/tmp/repair",
            epoch="epoch",
            instance_token="wrong-token",
            started_at=3.0,
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(
                handoff_state="attached",
                session_id="repair-session",
                workspace_id="repair-workspace",
            )
        )
    handler._current_scope["session_id"] = "wrong-repair-session"
    wrong_session = await handler._cmd_create_task(
        {"title": "Wrong session", "description": "fix", "reason": "not owner"}
    )
    assert wrong_session["success"] is False
    assert wrong_session["error"] == "repair stage is no longer active"
    handler._current_scope["session_id"] = "repair-session"

    handler._current_scope["session_instance_token"] = None
    missing_instance = await handler._cmd_create_task(
        {"title": "Missing instance", "description": "fix", "reason": "old token"}
    )
    assert missing_instance["success"] is False
    assert missing_instance["error"] == "repair stage is no longer active"
    handler._current_scope["session_instance_token"] = "stale-token"
    stale_instance = await handler._cmd_create_task(
        {"title": "Stale instance", "description": "fix", "reason": "old token"}
    )
    assert stale_instance["success"] is False
    assert stale_instance["error"] == "repair stage is no longer active"
    handler._current_scope["session_instance_token"] = "token"

    original_lock = handler.db.lock_filing_scope
    raced = False

    async def advance_fence(conn, task_ids):
        nonlocal raced
        result = await original_lock(conn, task_ids)
        if not raced:
            raced = True
            await conn.execute(
                update(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == "repo",
                    integration_branch_owners.c.ref == "aq/parent",
                )
                .values(fence_token=integration_branch_owners.c.fence_token + 1)
            )
        return result

    monkeypatch.setattr(handler.db, "lock_filing_scope", advance_fence)
    stale_fence = await handler._cmd_create_task(
        {"title": "Fence moved", "description": "fix", "reason": "raced handoff"}
    )
    assert stale_fence["success"] is False
    assert stale_fence["error"] == "repair stage is no longer active"
    monkeypatch.setattr(handler.db, "lock_filing_scope", original_lock)

    replaced = False

    async def replace_session_instance(conn, task_ids):
        nonlocal replaced
        result = await original_lock(conn, task_ids)
        if not replaced:
            replaced = True
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == "repair-session")
                .values(instance_token="replacement-token")
            )
        return result

    monkeypatch.setattr(handler.db, "lock_filing_scope", replace_session_instance)
    stale_at_cas = await handler._cmd_create_task(
        {"title": "Session replaced", "description": "fix", "reason": "raced restart"}
    )
    assert stale_at_cas["success"] is False
    assert stale_at_cas["error"] == "repair stage is no longer active"
    monkeypatch.setattr(handler.db, "lock_filing_scope", original_lock)
    await handler.db.update_session("repair-session", instance_token="token")

    filed = await handler._cmd_create_task(
        {"title": "Follow-up", "description": "fix", "reason": "repair found it"}
    )

    assert filed["success"] is True
    child = await handler.db.get_task(filed["task_id"])
    assert child.parent_task_id == "parent"
    async with handler.db._engine.connect() as conn:
        origin = (
            await conn.execute(
                select(task_branch_origins).where(
                    task_branch_origins.c.task_id == child.id
                )
            )
        ).mappings().one()
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "operation",
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()
    assert origin["base_sha"] == next_head
    assert (stage["started_at"], stage["deadline_at"], stage["attempts"]) == (
        100.0,
        130.0,
        1,
    )
    checkpoint = await handler.db.get_integration_checkpoint("parent")
    assert checkpoint["generation"] == 4

    handler.orchestrator.git._arun = AsyncMock(return_value=" M dirty.py")
    refused = await handler._cmd_create_task(
        {"title": "Late", "description": "fix", "reason": "dirty follow-up"}
    )
    assert refused["success"] is False
    assert refused["code"] == "hierarchy.dirty"

    handler.orchestrator.git._arun = AsyncMock(side_effect=run_git)
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(owner_id="operation", owner_role="collector")
        )
    stale_owner = await handler._cmd_create_task(
        {"title": "Lost lease", "description": "fix", "reason": "late finding"}
    )
    assert stale_owner["success"] is False
    assert stale_owner["error"] == "repair stage is no longer active"

    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(owner_id=repair_task_id, owner_role="repair")
        )
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == "operation",
                integration_repair_stages.c.ordinal == 0,
            )
            .values(state="expired", completed_at=140.0)
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "operation")
            .values(state="human_required")
        )
    handler.orchestrator.git._arun = AsyncMock(side_effect=run_git)
    stale = await handler._cmd_create_task(
        {"title": "Too late", "description": "fix", "reason": "late finding"}
    )
    assert stale["success"] is False
    assert stale["error"] == "repair stage is no longer active"


async def test_running_parent_files_child_from_its_current_pushed_head(
    command_handler_factory,
):
    """A self-parent filing must not copy the parent's stale checkpoint SHA."""
    handler = await command_handler_factory()
    await _configure_db(handler.db)
    await _seed_parent_operation(handler.db)
    current_head = "d" * 40
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS")
        )
        await conn.execute(
            insert(workspaces).values(
                id="parent-workspace",
                project_id="p",
                workspace_path="/tmp/parent",
                source_type="link",
                locked_by_task_id="parent",
                enabled=True,
                created_at=2.0,
            )
        )
    await handler.db.create_session(
        SessionRecord(
            id="parent-session",
            task_id="parent",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-parent",
            lifecycle="task",
            state="running",
            work_dir="/tmp/parent",
            epoch="epoch",
            instance_token="token",
            started_at=2.0,
        )
    )
    handler.orchestrator.git.aget_current_branch = AsyncMock(return_value="aq/parent")

    async def run_git(args, *, cwd):
        assert cwd == "/tmp/parent"
        return "" if args[0] == "status" else current_head

    handler.orchestrator.git._arun = AsyncMock(side_effect=run_git)
    handler.orchestrator.git.als_remote_ref = AsyncMock(
        return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=current_head)
    )
    handler._current_scope = {
        "kind": "session",
        "session_id": "parent-session",
        "task_id": "parent",
        "project_id": "p",
        "elevated": False,
    }

    filed = await handler._cmd_create_task(
        {
            "title": "Child",
            "description": "fix",
            "reason": "split current work",
            "parent_id": "parent",
        }
    )

    assert filed["success"] is True
    child = await handler.db.get_task(filed["task_id"])
    assert child.parent_task_id == "parent"
    async with handler.db._engine.connect() as conn:
        origin = (
            await conn.execute(
                select(task_branch_origins).where(
                    task_branch_origins.c.task_id == child.id
                )
            )
        ).mappings().one()
    assert origin["base_sha"] == current_head


async def test_real_task_close_bypasses_legacy_pipeline_and_rejects_stale_stage(
    command_handler_factory,
):
    """Closing a repair writer records only its own lifecycle boundary."""
    from src.integration.repair import RepairService

    handler = await command_handler_factory()
    await _configure_db(handler.db)
    await _seed_parent_operation(handler.db)
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        handler.db,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    dispatched = await service.dispatch("operation", 0)
    repair_task_id = dispatched["repair_task_id"]
    clean_head = "f" * 40
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == repair_task_id)
            .values(status="IN_PROGRESS")
        )
        await conn.execute(
            insert(workspaces).values(
                id="close-workspace",
                project_id="p",
                workspace_path="/tmp/close-repair",
                source_type="link",
                locked_by_task_id=repair_task_id,
                enabled=True,
                created_at=2.0,
            )
        )
    await handler.db.create_session(
        SessionRecord(
            id="close-session",
            task_id=repair_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-close-repair",
            lifecycle="task",
            state="running",
            work_dir="/tmp/close-repair",
            epoch="epoch",
            instance_token="token",
            started_at=2.0,
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(
                handoff_state="attached",
                session_id="close-session",
                workspace_id="close-workspace",
            )
        )
    handler.orchestrator.git.aget_current_branch = AsyncMock(return_value="aq/parent")
    handler.orchestrator.git._arun = AsyncMock(
        side_effect=lambda args, **_kwargs: "" if args[0] == "status" else clean_head
    )
    handler.orchestrator.git.als_remote_ref = AsyncMock(
        return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=clean_head)
    )
    handler.orchestrator.git.ais_ancestor = AsyncMock(return_value=True)
    handler.orchestrator.git.arev_parse = AsyncMock(return_value=clean_head)
    handler.orchestrator._run_completion_pipeline = AsyncMock(
        side_effect=AssertionError("repair delegate entered legacy integration")
    )
    handler.orchestrator._preserve_unpushed_on_failure = AsyncMock(
        side_effect=AssertionError("repair failure tried generic branch push")
    )
    handler.orchestrator.arelease_integration_writer_for_retry = AsyncMock(
        return_value=True
    )
    handler.orchestrator.release_session_task_resources = AsyncMock()
    handler._current_scope = {
        "kind": "session",
        "session_id": "close-session",
        "task_id": repair_task_id,
        "project_id": "p",
        "elevated": False,
    }
    failed = await handler._cmd_task_close(
        {
            "task_id": repair_task_id,
            "session_id": "close-session",
            "outcome": "fail",
            "failure_class": "hard",
            "summary": "repair remains unfinished",
        }
    )
    assert failed["success"] is False
    assert failed["result"] == "verification_failed"
    assert (await handler.db.get_task(repair_task_id)).status is TaskStatus.IN_PROGRESS
    handler.orchestrator._preserve_unpushed_on_failure.assert_not_awaited()

    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "operation")
            .values(state="human_required")
        )
    stale = await handler._cmd_task_close(
        {
            "task_id": repair_task_id,
            "session_id": "close-session",
            "outcome": "pass",
            "summary": "repair work complete",
        }
    )
    assert stale["success"] is False
    assert stale["result"] == "verification_failed"
    assert (await handler.db.get_task(repair_task_id)).status is TaskStatus.IN_PROGRESS

    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "operation")
            .values(state="active")
        )
    original_remote = handler.orchestrator.git.als_remote_ref

    async def move_fence(*args, **kwargs):
        async with handler.db.immediate() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == "repo",
                    integration_branch_owners.c.ref == "aq/parent",
                )
                .values(fence_token=integration_branch_owners.c.fence_token + 1)
            )
        return RemoteRefResult(RemoteRefState.PRESENT, oid=clean_head)

    handler.orchestrator.git.als_remote_ref = AsyncMock(side_effect=move_fence)
    raced = await handler._cmd_task_close(
        {
            "task_id": repair_task_id,
            "session_id": "close-session",
            "outcome": "pass",
            "summary": "repair work complete",
        }
    )
    assert raced["success"] is False
    assert raced["result"] == "verification_failed"
    assert (await handler.db.get_task(repair_task_id)).status is TaskStatus.IN_PROGRESS
    handler.orchestrator.git.als_remote_ref = original_remote
    closed = await handler._cmd_task_close(
        {
            "task_id": repair_task_id,
            "session_id": "close-session",
            "outcome": "pass",
            "summary": "repair work complete",
        }
    )
    assert closed["success"] is True, closed
    assert (await handler.db.get_task(repair_task_id)).status is TaskStatus.COMPLETED
    completed_stage = await _repair_stage(handler.db, "operation", 0)
    assert completed_stage["attempts"] == 1
    assert completed_stage["dossier"]["completed_delegate_attempts"][0]["task_id"] == repair_task_id
    handler.orchestrator._run_completion_pipeline.assert_not_awaited()
    async with handler.db._engine.connect() as conn:
        close_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type
                    == "integration.repair_delegate_closed"
                )
            )
        ).mappings().all()
    assert len(close_events) == 1
    assert close_events[0]["payload"]["task_id"] == repair_task_id
    emitted = [call.args[0] for call in handler.orchestrator.bus.emit.await_args_list]
    assert "task.completed" not in emitted
    assert "task.closed" in emitted


@pytest.mark.parametrize("full_ref", [False, True])
async def test_batch_repair_delegate_can_file_only_explicit_project_root(
    command_handler_factory, full_ref,
):
    """Batch repair grants no implicit or arbitrary structural parent scope."""
    from src.integration.repair import RepairService

    handler = await command_handler_factory()
    await _configure_db(handler.db)
    from src.integration.hierarchy import HierarchyIntegration

    handler.orchestrator.hierarchy_integration = HierarchyIntegration(
        handler.db,
        default_head_resolver=lambda _repo, _branch: STARTING_SHA,
    )
    branch = "refs/heads/aq/integration/batch" if full_ref else "aq/integration/batch"
    operation_id = await _seed_root_operation(handler.db, branch=branch)
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="batch-owner",
                repository_id="repo",
                ref=branch,
                owner_id="batch",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        handler.db,
    )
    await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    dispatched = await service.dispatch(operation_id, 0)
    repair_task_id = dispatched["repair_task_id"]
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == repair_task_id)
            .values(status="IN_PROGRESS", branch_name="aq/integration/batch")
        )
        await conn.execute(
            insert(workspaces).values(
                id="batch-repair-workspace",
                project_id="p",
                workspace_path="/tmp/batch-repair",
                source_type="link",
                locked_by_task_id=repair_task_id,
                enabled=True,
                created_at=2.0,
            )
        )
    await handler.db.create_session(
        SessionRecord(
            id="batch-repair-session",
            task_id=repair_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-batch-repair",
            lifecycle="task",
            state="running",
            work_dir="/tmp/batch-repair",
            epoch="epoch",
            instance_token="token",
            started_at=2.0,
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "batch-owner")
            .values(
                handoff_state="attached",
                session_id="batch-repair-session",
                workspace_id="batch-repair-workspace",
            )
        )
    handler._current_scope = {
        "kind": "session",
        "session_id": "batch-repair-session",
        "session_instance_token": "token",
        "task_id": repair_task_id,
        "project_id": "p",
        "elevated": False,
    }
    implicit = await handler._cmd_create_task(
        {"title": "Finding", "description": "fix", "reason": "batch finding"}
    )
    assert implicit["success"] is False
    assert "explicitly request a root" in implicit["error"]

    explicit = await handler._cmd_create_task(
        {
            "title": "Finding",
            "description": "fix",
            "reason": "batch finding",
            "root": True,
        }
    )
    assert explicit["success"] is True
    filed = await handler.db.get_task(explicit["task_id"])
    assert filed.parent_task_id is None
    assert await handler.db.get_typed_dependencies(filed.id) == [
        (repair_task_id, "discovered-from")
    ]


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
@pytest.mark.parametrize("green", [False, True])
@pytest.mark.parametrize("lapsed", [False, True])
async def test_root_repair_close_reads_the_candidate_subject_and_frees_a_pool_slot(
    command_handler_factory, lifecycle, green, lapsed
):
    """A root delegate anchors its proof on ``candidate_sha``, not ``head_sha``.

    Root stages bind ``{"kind": "batch", "revision", "candidate_sha"}`` while
    parent stages bind ``head_sha``.  Reading ``head_sha`` unconditionally
    raised ``KeyError`` inside the close pipeline's guard, so a passing root
    repair landed BLOCKED with its pushed commits unrecorded — and on a pool
    session the slot was released with the delegate never completed.

    With *lapsed* the stage's budget ran out while this exact fenced writer was
    attached.  ``adopt_batch_repair_on`` refused such a close outright
    ("stage is no longer active"), which is how a worker ended up holding a
    claim it could neither complete nor release: every close was refused and
    retried in session forever.  A deadline bounds the wait for a writer, not
    the writer a stage already holds — the same answer ``expire`` gives a live
    writer (``writer_live``, :data:`WRITER_RECHECK_SECONDS`) — so the guarded
    close re-arms the stage and completes.
    """
    import time

    from src.integration.repair import RepairService

    handler = await command_handler_factory()
    await _configure_db(handler.db)
    operation_id = await _seed_root_operation(handler.db)
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="batch-owner",
                repository_id="repo",
                ref="aq/integration/batch",
                owner_id="batch",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        handler.db,
    )
    await service.start(operation_id, STARTING_SHA, "batch", now=time.time())
    dispatched = await service.dispatch(operation_id, 0)
    repair_task_id = dispatched["repair_task_id"]
    repair_head = STARTING_SHA if green else "d" * 40
    if lapsed and not green:
        # The stage ran out of budget before its worker got to the close, the
        # way it does when the pool cannot staff the delegate in time.
        async with handler.db.immediate() as conn:
            await conn.execute(
                update(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == 0,
                ).values(deadline_at=time.time() - 1.0)
            )
    is_pool = lifecycle == "pool"
    await handler.db.create_agent(
        Agent(
            id="root-repair-agent",
            name="Root Repair Agent",
            profile_id="repairer",
            state=AgentState.BUSY,
            current_task_id=repair_task_id,
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == repair_task_id)
            .values(
                status="IN_PROGRESS",
                claim_epoch=1,
                assigned_agent_id="root-repair-agent",
            )
        )
        await conn.execute(
            insert(workspaces).values(
                id="root-repair-workspace",
                project_id="p",
                workspace_path="/tmp/root-repair",
                source_type="link",
                locked_by_task_id=repair_task_id,
                locked_by_agent_id="root-repair-agent",
                enabled=True,
                created_at=2.0,
            )
        )
    await handler.db.create_session(
        SessionRecord(
            id="root-repair-session",
            task_id=repair_task_id,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-root-repair",
            lifecycle=lifecycle,
            state="running",
            work_dir="/tmp/root-repair",
            epoch="epoch",
            instance_token="token",
            started_at=2.0,
            claim_phase="active",
            claim_phase_at=2.0,
            agent_id="root-repair-agent",
            last_claim_epoch=1 if is_pool else None,
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "batch-owner")
            .values(
                handoff_state="attached",
                session_id="root-repair-session",
                workspace_id="root-repair-workspace",
            )
        )

    handler.orchestrator.git.aget_current_branch = AsyncMock(
        return_value="aq/integration/batch"
    )

    async def run_git(args, *, cwd):
        if args[0] == "status":
            return ""
        if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "HEAD"
        if args[:2] == ["switch", "--detach"]:
            return ""
        if args[0] == "rev-list":
            return "" if green else f"{repair_head}\n"
        return repair_head

    handler.orchestrator.git._arun = AsyncMock(side_effect=run_git)
    handler.orchestrator.git.als_remote_ref = AsyncMock(
        return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=repair_head)
    )
    handler.orchestrator.git.ais_ancestor = AsyncMock(return_value=True)
    handler.orchestrator.git.arev_parse = AsyncMock(return_value=repair_head)
    handler.orchestrator._run_completion_pipeline = AsyncMock(
        side_effect=AssertionError("root repair delegate entered legacy integration")
    )
    async def release_proved_owner(*_args, **_kwargs):
        async with handler.db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).where(
                integration_branch_owners.c.id == "batch-owner"
            ).values(handoff_state="released", session_id=None, workspace_id=None,
                     confirmed_workspace_id="root-repair-workspace"))
        return True
    handler.orchestrator.arelease_integration_writer_for_retry = AsyncMock(
        side_effect=release_proved_owner
    )
    handler.orchestrator.release_session_task_resources = AsyncMock()
    handler._current_scope = {
        "kind": "session",
        "session_id": "root-repair-session",
        "task_id": repair_task_id,
        "project_id": "p",
        "elevated": False,
    }

    # Legacy built candidates could leave their stage bound to the old
    # construction base. Close must refresh under the exact writer fence.
    if green:
        await _record_root_green(handler.db, service, operation_id, now=time.time())
        scope = await handler.db.get_repair_filing_scope(
            repair_task_id, session_id="root-repair-session",
        )
        refused = await service.complete_delegate(
            repair_task_id,
            **{key: scope[key] for key in (
                "operation_id", "stage", "session_id", "instance_token", "workspace_id",
            )},
            fence_token=scope["fence_token"] + 1,
            head_sha=repair_head,
            commit_proof={"base_sha": repair_head, "head_sha": repair_head, "commits": []},
        )
        assert refused == {"outcome": "stale"}
        assert (await handler.db.get_task(repair_task_id)).status is TaskStatus.IN_PROGRESS
    else:
        async with handler.db.immediate() as conn:
            await conn.execute(update(integration_repair_stages).where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.ordinal == 0,
            ).values(current_subject={"kind": "batch", "revision": 0,
                                      "candidate_sha": "e" * 40}))
    from src.integration.outbox import enqueue_integration_event
    async with handler.db.immediate() as conn:
        await enqueue_integration_event(
            conn, event_id=f"repair-delegate-closed-{operation_id}-0-{repair_task_id}",
            dedup_key=f"repair-delegate-closed:{operation_id}:0:{repair_task_id}",
            project_id="p", event_type="integration.repair_delegate_closed",
            payload={"task_id": repair_task_id, "fence_token": 0}, available_at=1,
        )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(),
        session_id="root-repair-session",
        session_instance_token="token",
        task_id=repair_task_id,
        project_id="p",
    )
    with principal_context(principal):
        closed = await handler._cmd_task_close(
            {
                "task_id": repair_task_id,
                "session_id": "root-repair-session",
                "outcome": "pass",
                "summary": "root repair pushed",
                **({"claim_epoch": 1} if is_pool else {}),
            }
        )

    assert closed["success"] is True, closed
    assert (await handler.db.get_task(repair_task_id)).status is TaskStatus.COMPLETED
    handler.orchestrator._run_completion_pipeline.assert_not_awaited()
    # The lineage proof starts at the batch subject's ``candidate_sha``.
    if green:
        handler.orchestrator.git.ais_ancestor.assert_not_awaited()
    else:
        assert handler.orchestrator.git.ais_ancestor.await_args.args == (
            "/tmp/root-repair", STARTING_SHA, repair_head,
        )
    handler.orchestrator.git.als_remote_ref.assert_awaited()
    stage = await _repair_stage(handler.db, operation_id, 0)
    if lapsed and not green:
        # expire's live-writer recheck, applied by the close, and nothing else:
        # no successor ordinal, no attempt spent, the writer's own fence intact.
        assert stage["state"] == "active" and stage["attempts"] == 0
        assert stage["deadline_at"] > time.time()
        assert len(stage["dossier"]["deadline_deferrals"]) == 1
        recheck = stage["dossier"]["deadline_deferrals"][0]
        assert recheck["reason"] == "writer_live"
        assert recheck["task_id"] == repair_task_id
        assert (recheck["task_status"], recheck["claim_epoch"]) == ("IN_PROGRESS", 1)
        assert recheck["deadline_at"] == stage["deadline_at"]
        assert recheck["previous_deadline_at"] < time.time() < recheck["deadline_at"]
        assert stage["dossier"]["budget"]["deadline_at"] == stage["deadline_at"]
        # A live writer's recheck is not news.
        assert await _notices(handler.db, "integration_repair_deferred") == []
    else:
        # A green handoff may outlive the deadline (adopt's
        # ``awaiting_completion`` path), and a close inside budget re-arms
        # nothing: no deferral either way.
        assert stage["dossier"].get("deadline_deferrals") in (None, [])
    async with handler.db._engine.connect() as conn:
        close_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type
                    == "integration.repair_delegate_closed"
                )
            )
        ).mappings().all()
    async with handler.db._engine.connect() as conn:
        revision = 0 if green else 1
        candidate = (await conn.execute(select(integration_candidate_revisions).where(
            integration_candidate_revisions.c.batch_id == "batch",
            integration_candidate_revisions.c.revision == revision,
        ))).mappings().one()
    assert candidate["head_sha"] == repair_head
    assert candidate["ci_evidence_id"] == ("root-green" if green else None)
    assert candidate["repair_parent_revision"] == (None if green else 0)
    assert len(close_events) == 2
    close_events = [e for e in close_events if e["payload"].get("fence_token") != 0]
    assert close_events[0]["payload"]["task_id"] == repair_task_id
    assert close_events[0]["payload"]["workspace_id"] == "root-repair-workspace"
    assert close_events[0]["payload"]["batch_id"] == "batch"
    assert close_events[0]["payload"]["revision"] == revision
    assert close_events[0]["payload"]["head_sha"] == repair_head
    close_fence = {key: close_events[0]["payload"][key] for key in (
        "operation_id", "stage", "task_id", "session_id", "instance_token",
        "workspace_id", "fence_token",
    )}
    current = await handler._cmd_integration_repair_close_current(close_fence)
    assert current == {
        "success": True, "outcome": "current", "batch_id": "batch", "revision": revision,
    }
    stale = await handler._cmd_integration_repair_close_current(
        {**close_fence, "fence_token": close_fence["fence_token"] + 1}
    )
    assert stale["outcome"] == "stale"
    async with handler.db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "batch"
        ).values(current_revision=2))
    superseded = await handler._cmd_integration_repair_close_current(close_fence)
    assert superseded["outcome"] == "stale"

    session = await handler.db.get_session("root-repair-session")
    if is_pool:
        # The slot goes straight back on the market for the next claim, so
        # the close has to have completed the delegate before letting go.
        assert session.task_id is None
        assert session.last_claim_epoch == 1
        slot = await handler.db.get_workspace("root-repair-workspace")
        assert slot.locked_by_task_id is None
        assert slot.locked_by_agent_id == "root-repair-agent"
        handler.orchestrator.release_session_task_resources.assert_not_awaited()
    else:
        assert session.task_id == repair_task_id
        handler.orchestrator.release_session_task_resources.assert_awaited()


@pytest.mark.parametrize("owner_state", ["reserved", "released", "handoff_pending"])
async def test_debug_escalation_accepts_a_released_primary_repair_writer(db, owner_state):
    """A primary delegate that closed cleanly is a provable predecessor.

    A successful close self-transfers the delegate back to a ``reserved``
    fence in its own ``repair`` role
    (``arelease_integration_writer_for_retry``) — already stopped and
    detached.  ``_predecessor_matches`` knows only ``collector`` and
    ``verifier``, so before amber-delta the ladder answered
    ``human_required`` for exactly the owner state a clean close produces.
    """
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db, "failed-check-2", run_id="run-2", conclusion="failure"
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        db, confirm_handoff=lambda _owner: True,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    primary = await service.dispatch("operation", 0)
    primary_task_id = primary["repair_task_id"]

    # What a clean delegate close leaves behind: the task is COMPLETED and
    # the branch is reserved back to it with no session or workspace.
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == primary_task_id).values(status="COMPLETED")
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "owner")
            .values(
                owner_id=primary_task_id,
                owner_role="repair",
                handoff_state=owner_state,
                session_id="old-session" if owner_state == "handoff_pending" else None,
                workspace_id="old-workspace" if owner_state == "handoff_pending" else None,
                confirmed_workspace_id="repair-workspace",
            )
        )
    await service.record_result("operation", "failed-check", now=101.0)
    await service.record_result("operation", "failed-check-2", now=102.0)

    debug = await service.dispatch("operation", 1)

    assert debug["outcome"] == "dispatched"
    assert debug["repair_task_id"] != primary_task_id
    assert (await db.get_task(debug["repair_task_id"])).status is TaskStatus.READY
    owner = await BranchOwnership(db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert owner["owner_id"] == debug["repair_task_id"]
    assert owner["owner_role"] == "repair"
    assert owner["handoff_state"] == "reserved"
    assert int(owner["fence_token"]) == debug["fence"]["token"]


async def test_debug_escalation_still_refuses_an_unrelated_repair_owner(db):
    """The released-primary allowance is bound to *this* operation's stage 0."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(
        db, "failed-check-2", run_id="run-2", conclusion="failure"
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        db,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await service.dispatch("operation", 0)
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "owner")
            .values(
                owner_id="some-other-repair-task",
                owner_role="repair",
                handoff_state="reserved",
                session_id=None,
                workspace_id=None,
            )
        )
    await service.record_result("operation", "failed-check", now=101.0)
    await service.record_result("operation", "failed-check-2", now=102.0)

    debug = await service.dispatch("operation", 1)

    assert debug["outcome"] == "unknown"
    assert debug["reason_code"] == "owner_not_predecessor"
    assert "some-other-repair-task" in debug["reason"]


async def test_repair_delegate_is_filed_unrouted_with_the_stage_class_hint(
    command_handler_factory,
):
    """Mandatory routing: the delegate carries the stage class as a hint only.

    The stored policy still names ``primary_profile_id`` (a pre-routing
    snapshot); it validates, is ignored, and the delegate gets no profile or
    class: the router writes its route later.  The command handler's repair
    service needs no route validator for this.
    """
    handler = await command_handler_factory()
    await _configure_db(handler.db)
    await _seed_parent_operation(handler.db)
    snapshot = HierarchicalIntegrationPolicy.model_validate(_policy())
    assert snapshot.parent.primary_profile_id == "repairer"
    assert snapshot.parent.repair.debug_profile_id == "debugger"
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = handler._integration_repair_service()
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)

    dispatched = await service.dispatch("operation", 0)

    assert dispatched["outcome"] == "dispatched"
    delegate = await handler.db.get_task(dispatched["repair_task_id"])
    assert delegate.created_by_kind == "integration_repair"
    assert delegate.profile_id is None
    assert delegate.intelligence_class is None
    assert delegate.class_hint == "primary-medium"
    assert delegate.route_source == "unrouted"
    async with handler.db._engine.connect() as conn:
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "operation"
                )
            )
        ).mappings().one()
    assert stage["intelligence_class"] == "primary-medium"
    assert stage["profile_id"] is None


async def test_repair_stage_without_a_class_is_configuration_blocked(db):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    async with db.immediate() as conn:
        # A legacy stage that recorded only a (now ignored) profile.
        await conn.execute(
            update(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == "operation")
            .values(intelligence_class=None, profile_id="repairer")
        )

    blocked = await service.dispatch("operation", 0)

    assert blocked["outcome"] == "configuration_blocked"
    assert await db.get_task("repair-operation-0") is None


async def _claim_epoch_args(handler, task_id: str, lifecycle: str) -> dict:
    """A pool close must present the claim epoch it holds; a task close must not."""
    if lifecycle != "pool":
        return {}
    return {"claim_epoch": (await handler.db.get_task(task_id)).claim_epoch}


async def _stage_closable_repair_delegate(
    handler, monkeypatch, *, lifecycle: str, clean: bool = True
):
    """Drive a repair delegate to the point of closing, on either lifecycle.

    Returns ``(repair_task_id, stopped)`` where *stopped* accumulates one
    entry per session-provider stop.  ``clean=False`` makes the checkout
    report dirty, which is the failed-detach-proof case: no handoff evidence
    exists and nothing may be released on the strength of a database unlock.
    """
    from src.integration.repair import RepairService

    await _configure_db(handler.db)
    await _seed_parent_operation(handler.db)
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = RepairService(
        handler.db,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    repair_task_id = (await service.dispatch("operation", 0))["repair_task_id"]
    await handler.db.create_agent(
        Agent(
            id="repair-agent",
            name="Repairer",
            profile_id="repairer",
            state=AgentState.BUSY,
            current_task_id=repair_task_id,
        )
    )
    clean_head = "f" * 40
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == repair_task_id)
            .values(status="IN_PROGRESS")
        )
        await conn.execute(
            insert(workspaces).values(
                id="close-workspace",
                project_id="p",
                workspace_path="/tmp/close-repair",
                source_type="link",
                locked_by_task_id=repair_task_id,
                locked_by_agent_id="repair-agent",
                enabled=True,
                created_at=2.0,
            )
        )
    await handler.db.create_session(
        SessionRecord(
            id="close-session",
            task_id=repair_task_id,
            agent_id="repair-agent",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-close-repair",
            lifecycle=lifecycle,
            state="running",
            work_dir="/tmp/close-repair",
            epoch="epoch",
            instance_token="token",
            started_at=2.0,
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "owner")
            .values(
                handoff_state="attached",
                session_id="close-session",
                workspace_id="close-workspace",
            )
        )
        if lifecycle == "pool":
            # What a real claim records; ``release_claim`` fences on it.
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == "close-session")
                .values(
                    last_claim_epoch=(
                        await handler.db.get_task(repair_task_id)
                    ).claim_epoch
                )
            )

    detached = False

    async def run_unlocked(args, *, cwd):
        nonlocal detached
        if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "HEAD" if detached else "aq/parent"
        if args[:2] == ["status", "--porcelain"]:
            return "" if clean else " M src/x.py"
        if args[:2] == ["fetch", "origin"]:
            return ""
        if args[:2] == ["switch", "--detach"]:
            detached = True
            return ""
        return clean_head

    handler.orchestrator.git.aget_current_branch = AsyncMock(return_value="aq/parent")
    handler.orchestrator.git._arun = AsyncMock(
        side_effect=lambda args, **_kwargs: "" if args[0] == "status" else clean_head
    )
    handler.orchestrator.git._arun_unlocked = AsyncMock(side_effect=run_unlocked)
    handler.orchestrator.git.als_remote_ref = AsyncMock(
        return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=clean_head)
    )
    handler.orchestrator.git.ais_ancestor = AsyncMock(return_value=True)
    handler.orchestrator.git.arev_parse = AsyncMock(return_value=clean_head)
    stopped: list[str] = []

    async def stop(_handle, *, grace):
        stopped.append("stop")

    monkeypatch.setattr(
        handler.orchestrator.session_providers,
        "create",
        lambda *_args: SimpleNamespace(
            stop=stop, confirm_stopped=AsyncMock(return_value=True)
        ),
    )
    handler._current_scope = {
        "kind": "session",
        "session_id": "close-session",
        "task_id": repair_task_id,
        "project_id": "p",
        "elevated": False,
    }
    return repair_task_id, stopped


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_successful_delegate_close_leaves_a_transferable_owner(
    command_handler_factory, monkeypatch, lifecycle
):
    """The close itself must free the branch, not leave it attached forever.

    ``execution.py`` already called ``arelease_integration_writer_for_retry``
    on this leg, but that method only handled ``owner_role='worker'``, so a
    repair delegate's row stayed ``attached`` to the session the close then
    stopped.  The teardown next drops ``workspaces.locked_by_task_id`` (and,
    for a pool session, ``sessions.task_id`` as well), which is evidence
    ``aconfirm_integration_owner_handoff`` needs, so no later transfer could
    ever confirm the handoff either and every subsequent transfer of the
    branch raised ``BranchBusy`` (amber-delta).

    Both lifecycles run it: the shipped repair profiles are pool profiles,
    and a pool close proves the checkout detached instead of stopping the
    session it is running inside.
    """
    handler = await command_handler_factory()
    repair_task_id, stopped = await _stage_closable_repair_delegate(
        handler, monkeypatch, lifecycle=lifecycle
    )
    handler.orchestrator.release_session_task_resources = AsyncMock()

    closed = await handler._cmd_task_close(
        {
            "task_id": repair_task_id,
            "session_id": "close-session",
            "outcome": "pass",
            "summary": "repair work complete",
            **await _claim_epoch_args(handler, repair_task_id, lifecycle),
        }
    )

    assert closed["success"] is True, closed
    assert closed.get("retain_claim") is None
    assert (await handler.db.get_task(repair_task_id)).status is TaskStatus.COMPLETED
    # A push-model writer is stopped; the pool worker loop is not -- its proof
    # is the detached checkout plus the claim release that follows.
    assert stopped == (["stop"] if lifecycle == "task" else [])
    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(handler.db).get_owner(target)
    assert owner["owner_id"] == repair_task_id
    assert owner["owner_role"] == "repair"
    assert owner["handoff_state"] == "reserved"
    assert owner["session_id"] is None
    assert owner["workspace_id"] is None
    assert owner["confirmed_workspace_id"] == "close-workspace"
    assert (await handler.db.get_workspace("close-workspace")).locked_by_task_id is None
    if lifecycle == "pool":
        # The claim really was released afterwards, and the pool keeps the
        # agent-lock that only ``terminate_pool_session`` drops.
        assert (await handler.db.get_session("close-session")).task_id is None
        assert (
            await handler.db.get_workspace("close-workspace")
        ).locked_by_agent_id == "repair-agent"

    # The point of all of it: the next owner's transfer is provable.
    successor = await BranchOwnership(handler.db).transfer(
        Fence(target=target, owner_id=repair_task_id, token=int(owner["fence_token"])),
        "operation",
        "collector",
    )
    assert successor.owner_id == "operation"


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_delegate_close_retains_everything_when_the_handoff_is_unproven(
    command_handler_factory, monkeypatch, lifecycle
):
    """A failed stop/detach proof must not release resources or the claim.

    ``arelease_integration_writer_for_retry`` returning ``False`` means the
    writer may still hold the checkout.  Tearing down anyway would hand the
    branch onward on nothing but a database unlock -- what design spec §9.1
    forbids -- and would erase the session/workspace/claim evidence any later
    proof reads.  The close still commits (the work is done); what it must
    not do is let go (amber-delta review).
    """
    handler = await command_handler_factory()
    repair_task_id, _stopped = await _stage_closable_repair_delegate(
        handler, monkeypatch, lifecycle=lifecycle, clean=False
    )
    real_release = handler.orchestrator.release_session_task_resources
    released_resources = AsyncMock()
    handler.orchestrator.release_session_task_resources = released_resources

    closed = await handler._cmd_task_close(
        {
            "task_id": repair_task_id,
            "session_id": "close-session",
            "outcome": "pass",
            "summary": "repair work complete",
            **await _claim_epoch_args(handler, repair_task_id, lifecycle),
        }
    )

    assert closed["success"] is True, closed
    assert closed["needs_attention"] == "integration_handoff_unproven"
    assert (
        await handler.db.get_task_meta(repair_task_id, "needs_attention")
        == "integration_handoff_unproven"
    )
    # Nothing was released on either lifecycle.
    released_resources.assert_not_awaited()
    await real_release(repair_task_id, agent_id="repair-agent")
    owner = await BranchOwnership(handler.db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert owner["owner_id"] == repair_task_id
    assert owner["session_id"] == "close-session"
    assert owner["workspace_id"] == "close-workspace"
    workspace = await handler.db.get_workspace("close-workspace")
    assert workspace.locked_by_task_id == repair_task_id
    assert workspace.locked_by_agent_id == "repair-agent"
    assert (await handler.db.get_session("close-session")).task_id == repair_task_id
    if lifecycle == "pool":
        assert closed["retain_claim"] is True


async def test_active_repair_delegate_cannot_archive_and_legacy_archive_is_restored(db):
    from src.database.queries.hierarchy_queries import HierarchyError
    from src.database.tables import archived_tasks
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    async with db.immediate() as conn:
        await conn.execute(insert(integration_branch_owners).values(
            id="archive-owner", repository_id="repo", ref="aq/parent",
            owner_id="operation", owner_role="collector", fence_token=1,
            handoff_state="reserved", created_at=1.0, updated_at=1.0,
        ))
    service = RepairService(db, confirm_handoff=lambda _owner: True)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    dispatched = await service.dispatch("operation", 0)
    task_id = dispatched["repair_task_id"]
    async with db.immediate() as conn:
        # A routed delegate: the restore below must file it unrouted again.
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
            status="COMPLETED", profile_id="repairer", intelligence_class="primary-medium",
            route_source="router",
        ))
    # The refusal names the operation, its state, the seat this task occupies
    # in it and what would let go -- not a bare code the operator has to guess at.
    with pytest.raises(
        HierarchyError,
        match=f"operation is active and owns {task_id} as its repair_stage",
    ) as refusal:
        await db.archive_task(task_id)
    assert refusal.value.code == "integration_owned"
    assert refusal.value.context["integration_operation"] == {
        "operation_id": "operation",
        "state": "active",
        "role": "repair_stage",
        "task_id": task_id,
    }
    # Reproduce the historical archive, before that guard existed.
    async with db.immediate() as conn:
        task = await db._get_task_conn(task_id, conn=conn)
        await db._archive_one(task, conn=conn)
        before = (await conn.execute(select(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == "operation"
        ))).mappings().one()
    assert await db.get_task(task_id) is None
    restored = await service.dispatch("operation", 0)
    assert restored["outcome"] in {"dispatched", "already_dispatched"}
    assert restored["repair_task_id"] == task_id
    restored_task = await db.get_task(task_id)
    assert restored_task.status is TaskStatus.READY
    assert (restored_task.profile_id, restored_task.intelligence_class) == (None, None)
    assert restored_task.class_hint == "primary-medium"
    assert restored_task.route_source == "unrouted"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(archived_tasks.c.id).where(
            archived_tasks.c.id == task_id
        ))).scalar_one_or_none() is None
        after = (await conn.execute(select(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == "operation"
        ))).mappings().one()
    assert after["attempts"] == before["attempts"]
    assert after["deadline_at"] == before["deadline_at"]


@pytest.mark.parametrize("resolution", ["conflict", "reserved", "wrong_head", "observed", "none"])
async def test_parent_delegate_close_requires_recorded_resolution(db, monkeypatch, resolution):
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    head = "d" * 40
    if resolution != "none":
        values = {
            "id": "close-intent", "domain_key": "close-domain", "operation_key": "operation",
            "project_id": "p", "receipt_id": "close-receipt", "source_task_id": "child",
            "target_task_id": "parent", "source_head": "b" * 40, "source_base": "c" * 40,
            "repository_id": "repo", "target_branch": "aq/parent", "expected_target": STARTING_SHA,
            "fence_owner_id": "operation", "fence_token": 1, "state": "conflict",
            "created_at": 1.0, "updated_at": 1.0,
        }
        if resolution != "conflict":
            values.update(
                state="resolution_reserved", resolution_head_sha=head,
                resolution_tree_sha="e" * 40, resolution_commit_shas=[head],
                resolution_operation_id="operation", resolution_stage_ordinal=0,
                resolution_task_id="parent", resolution_session_id="session",
                resolution_session_instance_token="instance", resolution_workspace_id="workspace",
                resolution_fence_owner_id="parent", resolution_fence_token=2,
                resolution_push_evidence=(None if resolution == "reserved" else {
                    "kind": "exact_resolution_push_observed",
                    "remote_sha": head if resolution == "observed" else "f" * 40,
                }),
            )
        async with db.immediate() as conn:
            await conn.execute(insert(integration_promotion_intents).values(**values))
    scope = {
        "active": True, "operation_id": "operation", "stage": 0, "writer_kind": "repair_delegate",
        "session_id": "session", "instance_token": "instance", "workspace_id": "workspace",
        "fence_token": 2, "target_kind": "parent", "project_id": "p",
    }
    monkeypatch.setattr(db, "get_repair_filing_scope", AsyncMock(return_value=scope))
    transition = AsyncMock(return_value=SimpleNamespace(flipped=[], settled=[], ready=[]))
    monkeypatch.setattr(db, "_apply_transition", transition)
    service = RepairService(db)
    bind = AsyncMock()
    monkeypatch.setattr(service, "bind_current_parent_subject_on", bind)
    identity = {"completion_id": "close", "session_id": "session", "claim_epoch": 1}
    result = await service.complete_delegate(
        "parent", operation_id="operation", stage=0, session_id="session",
        instance_token="instance", workspace_id="workspace", fence_token=2,
        head_sha=head, now=110.0, accepted_close=identity,
    )
    if resolution in {"observed", "none"}:
        assert result["outcome"] == "completed"
        transition.assert_awaited_once()
        # The delegate's close identity rides on its COMPLETED transition.
        assert transition.await_args.kwargs["accepted_close"] == identity
        bind.assert_awaited_once()
    else:
        assert result["outcome"] == "resolution_required"
        assert result["intent_id"] == "close-intent"
        assert "integration-push-conflict-resolution" in result["feedback"]
        transition.assert_not_awaited()
        bind.assert_not_awaited()
    async with db._engine.connect() as conn:
        events = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "integration.repair_delegate_closed"
        ))).all()
    assert len(events) == (1 if resolution in {"observed", "none"} else 0)


async def test_legacy_handoff_agent_state_loads_and_normalizes(db):
    from src.database.tables import agents

    await db.create_agent(Agent(id='legacy', name='Legacy', profile_id='repairer'))
    async with db.immediate() as conn:
        await conn.execute(update(agents).where(agents.c.id == 'legacy').values(state='idle'))
    assert (await db.get_agent('legacy')).state is AgentState.IDLE
    await db.normalize_agent_state_casing()
    await db.normalize_agent_state_casing()
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(agents.c.state).where(
            agents.c.id == 'legacy'
        ))).scalar_one() == 'IDLE'




@pytest.mark.parametrize("recovery", ["reconcile", "command"])
async def test_pending_primary_release_recovers_stranded_debug_delegate(db, recovery):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.integration.repair import RepairService

    await _seed_parent_operation(db)
    await _add_parent_evidence(db, "failed-check-2", run_id="run-2", conclusion="failure")
    await BranchOwnership(db).acquire(BranchKey(repository_id="repo", branch="aq/parent"), "operation", "collector")
    service = RepairService(db, confirm_handoff=lambda _owner: False)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    primary = await service.dispatch("operation", 0)
    async with db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(
            handoff_state="handoff_pending", session_id="stopped-session",
            workspace_id="old-workspace",
        ))
    await service.record_result("operation", "failed-check", now=101.0)
    await service.record_result("operation", "failed-check-2", now=102.0)
    blocked = await service.dispatch("operation", 1)
    assert blocked["outcome"] == "busy"
    task_id = blocked["repair_task_id"]
    assert task_id != primary["repair_task_id"]
    before = await _repair_stage(db, "operation", 1)
    # Provider-backed owner recovery has now preserved and detached the writer.
    async with db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(
            handoff_state="released", session_id=None, workspace_id=None,
            confirmed_workspace_id="old-workspace",
        ))
    if recovery == "reconcile":
        await service.reconcile_delegate_reservations(103.0)
    else:
        handler = IntegrationCommandsMixin()
        handler.db = db
        handler.orchestrator = SimpleNamespace(repair_service=service)
        result = await handler._cmd_integration_reserve_owner({"task_id": task_id})
        assert result["outcome"] == "acquired"
        replay = await handler._cmd_integration_reserve_owner({"task_id": task_id})
        assert replay["outcome"] == "already_reserved"
    assert (await db.get_task(task_id)).status == TaskStatus.READY
    after = await _repair_stage(db, "operation", 1)
    for field in ("started_at", "deadline_at", "attempts", "policy"):
        assert after[field] == before[field]
    assert (await service.reserve_delegate(primary["repair_task_id"]))["outcome"] == "not_eligible"


@pytest.mark.parametrize("invalid", [
    None, "session", "detached", "epoch", "claim", "intent", "subject", "resolved",
])
async def test_parent_repair_prime_reads_only_current_attached_conflict(db, invalid):
    from src.integration.repair import RepairService
    from src.prime.sections import build_task_context_section

    current_head = await _seed_repeated_parent_conflict(db)
    await RepairService(db).start("operation", current_head, "operation", now=150.0)
    repair_id = "repair-operation-1"
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == repair_id).values(
            status="IN_PROGRESS", claim_epoch=4,
        ))
        await conn.execute(insert(workspaces).values(
            id="prime-workspace", project_id="p", workspace_path="/tmp/prime-repair",
            source_type="link", locked_by_task_id=repair_id, enabled=True, created_at=2.0,
        ))
    await db.create_session(SessionRecord(
        id="prime-session", task_id=repair_id, project_id="p", profile_id="debugger",
        harness="fake", provider="fake", name="prime-repair", lifecycle="pool",
        state="running", work_dir="/tmp/prime-repair", epoch="epoch",
        instance_token="token", started_at=2.0, claim_phase="active", last_claim_epoch=4,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(
            owner_id=repair_id, owner_role="repair", fence_token=8,
            handoff_state="attached", session_id="prime-session", workspace_id="prime-workspace",
        ))
        if invalid == "detached":
            await conn.execute(update(integration_branch_owners).values(handoff_state="reserved"))
        elif invalid == "epoch":
            await conn.execute(update(sessions).values(last_claim_epoch=3))
        elif invalid == "claim":
            await conn.execute(update(sessions).values(claim_phase="preparing"))
        elif invalid == "intent":
            await conn.execute(update(integration_promotion_intents).values(operation_key="other"))
        elif invalid == "subject":
            await conn.execute(update(integration_repair_stages).values(
                current_subject={"kind": "parent", "head_sha": "f" * 40},
            ))
        elif invalid == "resolved":
            await conn.execute(update(integration_promotion_intents).values(state="superseded"))
    session_id = "other-session" if invalid == "session" else "prime-session"
    context = await db.get_parent_repair_prime_context(repair_id, session_id=session_id)
    task = await db.get_task(repair_id)
    section = await build_task_context_section(db, SimpleNamespace(), task, session_id=session_id)
    if invalid:
        assert context is None
        assert "Current parent conflict repair" not in section.body
        return
    assert context["intent_id"] == "second-conflict"
    assert context["source_task_id"] == "second-child"
    assert context["source_base"] == "b" * 40
    assert context["source_head"] == "e" * 40
    assert context["expected_target"] == current_head
    assert context["conflict_diagnostics"] == {"paths": ["shared.py"]}
    assert context["fence"] == {
        "target": {"repository_id": "repo", "branch": "aq/parent"},
        "owner_id": repair_id, "token": 8,
    }
    assert '"token": 8' in section.body
    assert '"intent_id": "second-conflict"' in section.body
    # Refresh reads the new attachment, never the original collector's token 7.
    async with db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(fence_token=9))
    refreshed = await db.get_parent_repair_prime_context(repair_id, session_id=session_id)
    assert refreshed["fence"]["token"] == 9


RESOLUTION_HEAD = "d" * 40


def _continuous_boundary(**overrides) -> IntegrationBoundaryPolicy:
    """The shipped continuous ladder: a debug stage continues by default."""
    boundary = _boundary()
    repair = boundary.repair.model_copy(
        update={"on_exhausted": "continue"} | overrides
    )
    return boundary.model_copy(update={"repair": repair})


def _continuous_policy() -> dict:
    boundary = _continuous_boundary()
    return HierarchicalIntegrationPolicy(
        parent=boundary, root=boundary, branchless_parent="verifier", on_failed_child="block",
    ).model_dump(mode="json")


async def _seed_resolved_parent_conflict(db, *, delegate: str = "resolution-delegate"):
    """Leave stage 0 closed on the head of its own recorded conflict resolution."""
    await _seed_parent_operation(db, policy=_continuous_policy())
    await db.create_task(
        Task(
            id="child",
            project_id="p",
            parent_task_id="parent",
            title="Conflicting child",
            description="",
            status=TaskStatus.COMPLETED,
            repo_id="repo",
            branch_name="aq/child",
        )
    )
    await db.create_task(
        Task(
            id=delegate,
            project_id="p",
            title="Repair integration stage 0",
            description="resolved the conflict",
            status=TaskStatus.COMPLETED,
            repo_id="repo",
            branch_name="aq/parent",
            profile_id="repairer",
            route_source="legacy",
            intelligence_class="primary-medium",
            created_by_kind="integration_repair",
            created_by_id="operation",
        )
    )
    resolution_evidence = {
        "kind": "conflict_resolution",
        "original_source_base": "9" * 40,
        "original_source_head": "b" * 40,
        "original_source_tree": "c" * 40,
        "original_expected_target": STARTING_SHA,
        "resolved_head_sha": RESOLUTION_HEAD,
        "resolved_tree_sha": "e" * 40,
        "repair_commit_shas": [RESOLUTION_HEAD],
        "authoring": {
            "operation_id": "operation",
            "stage_ordinal": 0,
            "repair_task_id": delegate,
            "repair_session_id": "resolution-session",
            "repair_session_instance_token": "resolution-instance",
            "repair_workspace_id": "resolution-workspace",
            "fence": {
                "repository_id": "repo",
                "branch": "aq/parent",
                "owner_id": delegate,
                "token": 4,
            },
        },
        "push_authority": {
            "kind": "exact_resolution_push_observed",
            "remote_sha": RESOLUTION_HEAD,
        },
        "remote_proof": {
            "kind": "exact_resolution_tip",
            "remote_sha": RESOLUTION_HEAD,
            "resolved_tree_sha": "e" * 40,
            "repair_commit_shas": [RESOLUTION_HEAD],
        },
    }
    async with db.immediate() as conn:
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "parent"
        ).values(state="awaiting_children"))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="child", repository_id="repo", branch="aq/child",
            generation=0, checkpoint_sha="b" * 40, state="working", version=1,
            updated_at=120.0,
        ))
        await conn.execute(insert(task_branch_origins).values(
            id="child-origin", task_id="child", repository_id="repo",
            branch_name="aq/child", parent_task_id="parent",
            parent_repository_id="repo", parent_ref="aq/parent",
            base_sha=STARTING_SHA, creation_generation=3, created_at=110.0,
        ))
        await conn.execute(insert(task_delivery_receipts).values(
            id="resolution-receipt", domain_key="resolution-receipt-domain",
            source_task_id="child", target_task_id="parent", repository_id="repo",
            target_branch="aq/parent", reviewed_head_sha="b" * 40,
            reviewed_tree_sha="c" * 40, before_sha=STARTING_SHA, squash_sha=None,
            after_sha=RESOLUTION_HEAD,
            review_evidence={"review": {"source_base": "9" * 40}},
            resolution_evidence=resolution_evidence,
            parent_operation_id="operation", parent_episode_id="episode",
            disposition="code", created_at=190.0,
        ))
        await conn.execute(insert(integration_promotion_intents).values(
            id="resolution-intent", domain_key="resolution-intent-domain",
            operation_key="operation", project_id="p", receipt_id="resolution-receipt",
            source_task_id="child", target_task_id="parent", source_head="b" * 40,
            source_base="9" * 40, repository_id="repo", target_branch="aq/parent",
            expected_target=STARTING_SHA, fence_owner_id="operation", fence_token=3,
            state="committed",
            review_evidence={"reviewed_tree_sha": "c" * 40},
            authors=[], provenance={}, commit_metadata={},
            resolution_head_sha=RESOLUTION_HEAD, resolution_tree_sha="e" * 40,
            resolution_commit_shas=[RESOLUTION_HEAD],
            resolution_operation_id="operation", resolution_stage_ordinal=0,
            resolution_task_id=delegate, resolution_session_id="resolution-session",
            resolution_session_instance_token="resolution-instance",
            resolution_workspace_id="resolution-workspace",
            resolution_fence_owner_id=delegate, resolution_fence_token=4,
            resolution_push_started_at=198.0,
            resolution_push_evidence={
                "kind": "exact_resolution_push_observed",
                "remote_sha": RESOLUTION_HEAD,
            },
            remote_evidence={
                "kind": "exact_resolution_tip", "remote_sha": RESOLUTION_HEAD,
                "resolved_tree_sha": "e" * 40,
                "repair_commit_shas": [RESOLUTION_HEAD],
            },
            committed_at=200.0, created_at=150.0, updated_at=200.0,
        ))
        await conn.execute(insert(integration_branch_owners).values(
            id="resolved-owner", repository_id="repo", ref="aq/parent",
            owner_id="operation", owner_role="collector", fence_token=3,
            handoff_state="released", created_at=1.0, updated_at=200.0,
        ))
    return delegate


async def _record_resolution_stage(db, delegate, *, ordinal=0, attempts=1):
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == "operation",
            integration_repair_stages.c.ordinal == ordinal,
        ).values(
            repair_task_id=delegate,
            writer_kind="repair_delegate",
            attempts=attempts,
            current_subject={"kind": "parent", "generation": 3, "head_sha": RESOLUTION_HEAD},
            dossier=(
                dict(
                    (
                        await conn.execute(
                            select(integration_repair_stages.c.dossier).where(
                                integration_repair_stages.c.operation_id == "operation",
                                integration_repair_stages.c.ordinal == ordinal,
                            )
                        )
                    ).scalar_one()
                    or {}
                )
                | {"branch_sha": RESOLUTION_HEAD, "repair_commits": [RESOLUTION_HEAD]}
            ),
        ))
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "parent"
        ).values(checkpoint_sha=RESOLUTION_HEAD, updated_at=200.0))


async def test_recorded_parent_resolution_schedules_verification_without_a_successor(db):
    """A closed conflict resolution is verified, not repaired again."""
    from src.integration.repair import RepairService

    delegate = await _seed_resolved_parent_conflict(db)
    service = RepairService(db, clock=lambda: 210.0)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await _record_resolution_stage(db, delegate)

    assert (await service.pending_dispatches()) == [  # the closed delegate is selected
        {"operation_id": "operation", "ordinal": 0}
    ]
    result = await service.dispatch("operation", 0)

    assert result["outcome"] == "already_dispatched"
    assert result["stage"] == 0
    assert result["reason"] == "resolution_recorded_for_subject"
    assert result["head_sha"] == RESOLUTION_HEAD
    async with db._engine.connect() as conn:
        stages = (await conn.execute(select(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == "operation"
        ))).mappings().all()
        incidents = (await conn.execute(select(messages.c.id).where(
            messages.c.body_kind == "integration_repair_no_progress"
        ))).all()
        operation = (await conn.execute(select(integration_repair_operations).where(
            integration_repair_operations.c.id == "operation"
        ))).mappings().one()
    # No successor writer was allocated and no incident names a stalled repair.
    assert [row["ordinal"] for row in stages] == [0]
    assert stages[0]["state"] == "passed"
    assert stages[0]["dossier"]["resolution_verification"] == {
        "intent_id": "resolution-intent",
        "resolution_head_sha": RESOLUTION_HEAD,
        "stage": 0,
        "recorded_at": 210.0,
    }
    assert incidents == []
    assert operation["state"] == "active"
    # The resolved head now belongs to aggregate verification, not to a writer.
    assert await service.pending_dispatches() == []
    assert await service.expire("operation", 0, now=210.0) == {
        "outcome": "already_terminal", "action": "none",
        "operation_id": "operation", "stage": 0,
    }
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["state"] == "integration_ready"
    verifier = (await db.get_integration_operation("operation"))["verifier_task_id"]
    assert verifier == "verify-operation"
    assert (await db.get_task(verifier)).status is TaskStatus.PAUSED
    async with db._engine.connect() as conn:
        ready = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "task.integration_ready",
        ))).mappings().all()
    assert [row["payload"]["head_sha"] for row in ready] == [RESOLUTION_HEAD]


async def test_unchanged_resolved_successor_stage_does_not_escalate_the_operation(db):
    """A successor that changed nothing cannot escalate an already-resolved subject."""
    from src.integration.repair import RepairService

    delegate = await _seed_resolved_parent_conflict(db)
    service = RepairService(db, clock=lambda: 400.0)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await _record_resolution_stage(db, delegate)
    await db.create_task(
        Task(
            id="successor-delegate",
            project_id="p",
            title="Repair integration stage 1",
            description="nothing left to repair",
            status=TaskStatus.COMPLETED,
            repo_id="repo",
            branch_name="aq/parent",
            profile_id="debugger",
            route_source="legacy",
            intelligence_class="debug-high",
            created_by_kind="integration_repair",
            created_by_id="operation",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(insert(integration_repair_stages).values(
            operation_id="operation", ordinal=1,
            policy=_continuous_boundary().repair.model_dump(mode="json"),
            repair_task_id="successor-delegate", writer_kind="repair_delegate",
            starting_sha=RESOLUTION_HEAD, trigger_id="stage-exhausted:operation:0",
            current_subject={"kind": "parent", "generation": 3, "head_sha": RESOLUTION_HEAD},
            deadline_event_id="repair-deadline-operation-1",
            started_at=200.0, deadline_at=300.0, attempts=0,
            dossier={
                "operation_id": "operation",
                "starting_sha": RESOLUTION_HEAD,
                "trigger_id": "stage-exhausted:operation:0",
                "branch_sha": RESOLUTION_HEAD,
                "allocation": {
                    "subject_sha": RESOLUTION_HEAD, "ordinal": 1, "allocated_at": 200.0,
                },
                "budget": {
                    "ordinal": 1, "started_at": 200.0, "deadline_at": 300.0,
                    "attempt_limit": 1, "attempts": 0,
                },
            },
            state="active",
        ))
        await conn.execute(update(integration_repair_operations).where(
            integration_repair_operations.c.id == "operation"
        ).values(active_stage=1))

    result = await service.expire("operation", 1, now=400.0)

    assert result == {
        "outcome": "expired", "action": "none",
        "operation_id": "operation", "stage": 1,
    }
    stage = await _repair_stage(db, "operation", 1)
    assert stage["state"] == "passed"
    assert stage["dossier"]["resolution_verification"]["intent_id"] == "resolution-intent"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_repair_operations).where(
            integration_repair_operations.c.id == "operation"
        ))).mappings().one()["state"] == "active"
        assert (await conn.execute(select(messages.c.id).where(
            messages.c.body_kind == "integration_repair_no_progress"
        ))).all() == []
        assert (await conn.execute(select(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == "operation",
            integration_repair_stages.c.ordinal == 2,
        ))).all() == []


async def _settle_resolution_then_red(
    db,
    *,
    evidence_id: str = "resolution-red",
    head_sha: str = RESOLUTION_HEAD,
    classification: str = "conclusive",
):
    """Settle stage 0 on its recorded resolution, then record exact-head CI red."""
    from src.integration.repair import RepairService

    delegate = await _seed_resolved_parent_conflict(db)
    service = RepairService(db, clock=lambda: 210.0)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await _record_resolution_stage(db, delegate)
    settled = await service.dispatch("operation", 0)
    assert settled["reason"] == "resolution_recorded_for_subject"
    # The verifier's CI runs because the settle woke it, so its red always
    # lands after the resolution committed.
    await _add_parent_evidence(
        db,
        evidence_id,
        run_id="run-resolution",
        conclusion="failure",
        classification=classification,
        head_sha=head_sha,
        observed_at=220.0,
    )
    return service, delegate


async def test_red_at_a_settled_resolution_head_opens_the_next_stage(db):
    """Exact-head CI red after a settled resolution owes the parent a repair.

    Settling the resolved stage is what wakes the verifier, so its CI always
    reports after the stage ended. ``record_result`` used to answer that red
    ``stale`` because the active stage was no longer live, the playbook then
    completed the run green, and nothing ever repaired the resolution head.
    """
    service, delegate = await _settle_resolution_then_red(db)
    verifier = (await db.get_integration_operation("operation"))["verifier_task_id"]
    assert verifier == "verify-operation"

    result = await service.record_result("operation", "resolution-red", now=230.0)
    replay = await service.record_result("operation", "resolution-red", now=231.0)

    assert result == {
        "outcome": "started", "action": "stage_opened", "attempts": 0, "stage": 1,
    }
    assert replay == result | {"action": "duplicate"}
    settled = await _repair_stage(db, "operation", 0)
    assert settled["state"] == "passed"
    assert settled["dossier"]["resolution_verification"]["resolution_head_sha"] == (
        RESOLUTION_HEAD
    )
    stage = await _repair_stage(db, "operation", 1)
    assert stage["state"] == "active"
    assert stage["starting_sha"] == RESOLUTION_HEAD
    assert stage["trigger_id"] == "resolution-red"
    assert stage["current_subject"] == {
        "kind": "parent", "generation": 3, "head_sha": RESOLUTION_HEAD,
    }
    # The red opens the stage the way a conflict does: no attempt is spent and
    # no writer is named until the dispatcher claims it. A stage after zero
    # runs on the debug rung, as every later ordinal does.
    assert stage["attempts"] == 0
    assert stage["repair_task_id"] is None
    assert stage["intelligence_class"] == "debug-high"
    assert (stage["started_at"], stage["deadline_at"]) == (230.0, 290.0)
    assert stage["dossier"]["budget"] == {
        "ordinal": 1, "started_at": 230.0, "deadline_at": 290.0,
        "attempt_limit": 1, "attempts": 0,
    }
    assert stage["dossier"]["allocation"] == {
        "subject_sha": RESOLUTION_HEAD, "ordinal": 1, "allocated_at": 230.0,
    }
    assert stage["dossier"]["previous_stage"] == {
        "ordinal": 0,
        "state": "passed",
        "trigger_id": "failed-check",
        "starting_sha": STARTING_SHA,
        "resolution_verification": settled["dossier"]["resolution_verification"],
    }
    operation = await db.get_integration_operation("operation")
    assert (operation["active_stage"], operation["state"]) == (1, "escalated")
    async with db._engine.connect() as conn:
        notes = (
            await conn.execute(select(messages).where(messages.c.to_id == verifier))
        ).mappings().all()
        links = (
            await conn.execute(
                select(integration_repair_stage_evidence).where(
                    integration_repair_stage_evidence.c.evidence_id == "resolution-red"
                )
            )
        ).mappings().all()
    assert [note["body_kind"] for note in notes] == ["integration_parent_red"]
    assert RESOLUTION_HEAD in notes[0]["body"]
    assert [(link["ordinal"], link["counted_attempt"]) for link in links] == [(1, False)]

    # The dispatcher owes the new stage a writer, and that writer repairs the
    # red rather than being settled again on the same recorded resolution.
    assert await service.pending_dispatches() == [{"operation_id": "operation", "ordinal": 1}]
    dispatched = await service.dispatch("operation", 1)
    assert dispatched["outcome"] == "dispatched", dispatched
    assert dispatched["repair_task_id"] not in {None, delegate}
    assert (await _repair_stage(db, "operation", 1))["state"] == "active"


@pytest.mark.parametrize(
    ("head_sha", "classification"),
    [(STARTING_SHA, "conclusive"), (RESOLUTION_HEAD, "infrastructure")],
    ids=["superseded_head", "infrastructure"],
)
async def test_red_that_is_not_about_the_settled_head_stays_stale(
    db, head_sha, classification
):
    """Only a conclusive red on the parent's live head reopens a settled operation."""
    service, _delegate = await _settle_resolution_then_red(
        db, head_sha=head_sha, classification=classification
    )

    result = await service.record_result("operation", "resolution-red", now=230.0)

    assert result == {"outcome": "continue", "action": "stale", "attempts": 1}
    async with db._engine.connect() as conn:
        ordinals = (
            await conn.execute(
                select(integration_repair_stages.c.ordinal).where(
                    integration_repair_stages.c.operation_id == "operation"
                )
            )
        ).scalars().all()
        notes = (
            await conn.execute(
                select(messages.c.id).where(messages.c.body_kind == "integration_parent_red")
            )
        ).all()
    assert ordinals == [0]
    assert notes == []
    operation = await db.get_integration_operation("operation")
    assert (operation["active_stage"], operation["state"]) == (0, "active")


async def test_red_on_a_reopened_collections_cancelled_stage_opens_the_next_stage(db):
    """A reopened collection keeps its cancelled stage; its next red is new work."""
    from src.integration.repair import RepairService

    await _seed_parent_operation(db, policy=_continuous_policy())
    service = RepairService(db)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    async with db.immediate() as conn:
        # ``aq integration reopen-collection`` without a conflict: the stage
        # stays cancelled while the operation comes back to life around it.
        await conn.execute(
            update(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == "operation")
            .values(state="cancelled", completed_at=110.0)
        )
    await _add_parent_evidence(db, "reopened-red", run_id="run-reopened", conclusion="failure")

    result = await service.record_result("operation", "reopened-red", now=120.0)

    assert result == {
        "outcome": "started", "action": "stage_opened", "attempts": 0, "stage": 1,
    }
    stage = await _repair_stage(db, "operation", 1)
    assert stage["state"] == "active"
    assert stage["trigger_id"] == "reopened-red"
    assert stage["starting_sha"] == STARTING_SHA
    assert stage["dossier"]["previous_stage"]["state"] == "cancelled"
    assert (await _repair_stage(db, "operation", 0))["state"] == "cancelled"
    operation = await db.get_integration_operation("operation")
    assert (operation["active_stage"], operation["state"]) == (1, "escalated")

async def _settle_resolution_while_collecting(db) -> None:
    """Settle stage 0 on its recorded resolution while a later child still runs."""
    from src.integration.repair import RepairService

    delegate = await _seed_resolved_parent_conflict(db)
    await db.create_task(
        Task(
            id="later-child",
            project_id="p",
            parent_task_id="parent",
            title="Later child",
            description="",
            status=TaskStatus.IN_PROGRESS,
            repo_id="repo",
            branch_name="aq/later-child",
        )
    )
    service = RepairService(db, clock=lambda: 210.0)
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
    await _record_resolution_stage(db, delegate)
    settled = await service.dispatch("operation", 0)
    assert settled["reason"] == "resolution_recorded_for_subject"
    assert (await _repair_stage(db, "operation", 0))["state"] == "passed"


async def _later_child_conflicts(db) -> None:
    """The later child completes and its promotion conflicts on the resolved head."""
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "later-child").values(
            status=TaskStatus.COMPLETED.value,
        ))
        await conn.execute(insert(integration_promotion_intents).values(
            id="later-conflict", domain_key="later-conflict-domain",
            operation_key="operation", project_id="p", receipt_id="later-receipt",
            source_task_id="later-child", target_task_id="parent", source_head="f" * 40,
            source_base=STARTING_SHA, repository_id="repo", target_branch="aq/parent",
            expected_target=RESOLUTION_HEAD, fence_owner_id="operation", fence_token=5,
            state="conflict", conflict_diagnostics={"paths": ["src/shared.py"]},
            review_evidence={"reviewed_tree_sha": "7" * 40},
            authors=[], provenance={}, commit_metadata={},
            created_at=290.0, updated_at=290.0,
        ))


@pytest.mark.parametrize("trigger", ["operation", "later-conflict"])
async def test_a_later_conflict_after_a_settled_resolution_opens_a_fresh_stage(db, trigger):
    """Review finding R2: a settled stage answered ``stale`` to the parent's next conflict.

    Since a recorded resolution settles its stage ``passed`` on a live operation,
    ``start`` only knew how to continue a live stage, so a later child's
    conflict was stranded with no stage and no writer.
    """
    from src.integration.repair import RepairService

    await _settle_resolution_while_collecting(db)
    await _later_child_conflicts(db)
    repair = RepairService(db, clock=lambda: 300.0)

    # The shipped policy passes its operation key; a direct caller names the intent.
    started = await repair.start("operation", RESOLUTION_HEAD, trigger, now=300.0)

    assert (started["outcome"], started["stage"]) == ("started", 1), started
    assert started["starting_sha"] == RESOLUTION_HEAD
    replay = await repair.start("operation", RESOLUTION_HEAD, trigger, now=301.0)
    assert (replay["outcome"], replay["stage"]) == ("already_started", 1)
    operation = await db.get_integration_operation("operation")
    assert (operation["state"], operation["active_stage"]) == ("escalated", 1)
    # The policy's literal stage-zero dispatch reaches the fresh stage's writer.
    dispatched = await repair.dispatch("operation", 0)
    assert dispatched["outcome"] == "dispatched", dispatched
    assert dispatched["stage"] == 1
    settled = await _repair_stage(db, "operation", 0)
    fresh = await _repair_stage(db, "operation", 1)
    assert settled["state"] == "passed"
    assert settled["dossier"]["resolution_verification"]["intent_id"] == "resolution-intent"
    assert fresh["trigger_id"] == "later-conflict"
    assert fresh["repair_task_id"] == dispatched["repair_task_id"]
    assert fresh["dossier"]["current_conflict"]["intent_id"] == "later-conflict"
    assert fresh["dossier"]["previous_stage"] == {
        "ordinal": 0,
        "state": "passed",
        "trigger_id": "failed-check",
        "starting_sha": STARTING_SHA,
    }


async def test_a_settled_resolution_replays_its_own_start_without_a_fresh_stage(db):
    """Only a new conflict opens a stage after a settle; a replay stays idempotent."""
    from src.integration.repair import RepairService

    await _settle_resolution_while_collecting(db)
    repair = RepairService(db, clock=lambda: 300.0)

    replay = await repair.start("operation", STARTING_SHA, "failed-check", now=300.0)
    assert (replay["outcome"], replay["stage"]) == ("already_started", 0)
    # The settled stage's conflict is committed: the alias names no conflict now.
    alias = await repair.start("operation", RESOLUTION_HEAD, "operation", now=300.0)
    assert alias == {"outcome": "stale", "operation_id": "operation"}
    resolved = await repair.start("operation", STARTING_SHA, "resolution-intent", now=300.0)
    assert resolved == {"outcome": "stale", "operation_id": "operation"}
    async with db._engine.connect() as conn:
        ordinals = (await conn.execute(select(integration_repair_stages.c.ordinal).where(
            integration_repair_stages.c.operation_id == "operation"
        ))).scalars().all()
    assert ordinals == [0]
    operation = await db.get_integration_operation("operation")
    assert (operation["state"], operation["active_stage"]) == ("active", 0)


@pytest.fixture(autouse=True)
def reconciler_primitive_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives
    authorize_root_primitives(monkeypatch)


@pytest.fixture
async def ordinary_env(db):
    from src.integration.batches import Batch, BatchMember, BatchStore, candidate_ref
    from src.integration.lock import BranchLock
    from src.integration.repair import OrdinaryRepairService

    env = SimpleNamespace(db=db, now=1000.0)
    env.store = BatchStore(db)
    await env.store.freeze(Batch("ordinary", "p", "repo", "refs/heads/aq/epic"),
                           (BatchMember("source", STARTING_SHA, "a" * 40),),
                           trees={"source": "b" * 40})
    env.ref = candidate_ref("ordinary")
    env.target = BranchKey(repository_id="repo", branch=env.ref)
    env.locks = BranchLock(db, clock=lambda: env.now)
    env.service = OrdinaryRepairService(db, locks=env.locks, clock=lambda: env.now)
    return env


async def test_ordinary_repair_allocates_once_across_concurrent_visits_and_restart(ordinary_env):
    from src.integration.repair import OrdinaryRepairService

    env = ordinary_env
    results = await asyncio.gather(*(env.service.allocate(
        "ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA,
        intelligence_class="deep-high", priority=290,
    ) for _ in range(8)))
    assert [r["outcome"] for r in results].count("filed") == 1
    assert len({r["task_id"] for r in results}) == 1
    task_id = results[0]["task_id"]
    restarted = OrdinaryRepairService(env.db, locks=env.locks, clock=lambda: env.now)
    original = await restarted.input(task_id)
    assert original == {"batch_id": "ordinary", "attempt": 1, "repository_id": "repo",
                        "target_ref": env.ref, "starting_sha": STARTING_SHA}
    for _ in range(5):
        replay = await restarted.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha="c" * 40)
        assert (replay["outcome"], replay["attempt_count"], replay["task_id"]) == (
            "exists", 1, task_id)
    task = await env.db.get_task(task_id)
    assert (task.class_hint, task.priority) == ("deep-high", 290)
    async with env.db._engine.connect() as conn:
        assert len((await conn.execute(select(tasks))).all()) == 1
        assert not (await conn.execute(select(integration_repair_operations))).first()
        assert not (await conn.execute(select(integration_repair_stages))).first()
    assert (await env.store.get("ordinary")).repair_attempt_count == 1


async def test_ordinary_repair_next_allocation_counts_once_and_has_no_ceiling(ordinary_env):
    env = ordinary_env
    first = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA,
                                       ttl_seconds=10)
    await env.db.update_task(first["task_id"], status=TaskStatus.COMPLETED)
    env.now += 10
    async with env.db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == "ordinary")
                           .values(repair_attempt_count=100))
    results = await asyncio.gather(*(env.service.allocate(
        "ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha="c" * 40,
    ) for _ in range(4)))
    assert [r["outcome"] for r in results].count("filed") == 1
    assert {r["attempt_count"] for r in results} == {101}
    assert (await env.store.get("ordinary")).repair_attempt_count == 101


async def test_ordinary_repair_missing_completed_input_does_not_spend_a_successor(ordinary_env):
    from sqlalchemy import delete

    from src.database.tables import task_context

    env = ordinary_env
    first = await env.service.allocate(
        "ordinary", target_ref=env.ref, head_sha=STARTING_SHA,
        authorize=AsyncMock(return_value=True),
    )
    await env.db.update_task(first["task_id"], status=TaskStatus.COMPLETED)
    await env.locks.release((await env.locks.get(env.target)).grant())
    # Archival removes context; there is no authoritative start to compare.
    async with env.db.immediate() as conn:
        await conn.execute(delete(task_context).where(task_context.c.id == first["task_id"]))
    blocker = AsyncMock(return_value=None)
    result = await env.service.allocate(
        "ordinary", target_ref=env.ref, head_sha="c" * 40,
        authorize=AsyncMock(return_value=True), completion_blocker=blocker,
    )
    assert (result["outcome"], result["reason"], result["task_id"], result["attempt_count"]) == (
        "blocked", "repair_input_unconfirmed", first["task_id"], 1,
    )
    blocker.assert_not_awaited()
    assert (await env.store.get("ordinary")).repair_attempt_count == 1


@pytest.mark.parametrize("lost", ["released", "expired"])
async def test_ordinary_repair_replay_restores_detached_reservation_without_allocation(
    ordinary_env, lost,
):
    from src.database.queries.claim_queries import _frontier_where
    from src.database.queries.hierarchy_queries import ProjectIntegrationMode
    from src.integration.repair import OrdinaryRepairService

    env = ordinary_env
    first = await env.service.allocate("ordinary", target_ref=env.ref, head_sha=STARTING_SHA,
                                       authorize=AsyncMock(return_value=True), ttl_seconds=10)
    task_id = first["task_id"]
    await env.db.update_task(task_id, status=TaskStatus.READY)
    original = await env.service.input(task_id)
    old = (await env.locks.get(env.target)).grant()
    if lost == "released":
        await env.locks.release(old)
        assert not await env.db.is_hierarchy_task_runnable(task_id)
    else:
        env.now += 10

    restarted = OrdinaryRepairService(env.db, locks=env.locks, clock=lambda: env.now)
    replay = await restarted.allocate("ordinary", target_ref=env.ref, head_sha="c" * 40,
                                      authorize=AsyncMock(return_value=True))
    assert (replay["outcome"], replay["task_id"], replay["attempt_count"],
            replay["lease_expired"]) == ("exists", task_id, 1, False)
    lease = await env.locks.get(env.target)
    assert (lease.holder, lease.fence) == (task_id, old.token + 1)
    assert lease.expires_at > env.now
    assert await restarted.input(task_id) == original
    assert (await env.store.get("ordinary")).repair_attempt_count == 1
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.id).where(
            tasks.c.id == task_id, _frontier_where("p", ProjectIntegrationMode(True, "repo")),
        )) == task_id
        assert not (await conn.execute(select(integration_repair_stages))).first()


@pytest.mark.parametrize("status", [TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS])
async def test_ordinary_repair_replay_never_regrants_claimed_expired_writer(ordinary_env, status):
    env = ordinary_env
    first = await env.service.allocate("ordinary", target_ref=env.ref, head_sha=STARTING_SHA,
                                       authorize=AsyncMock(return_value=True), ttl_seconds=10)
    await env.db.update_task(first["task_id"], status=status)
    old = await env.locks.get(env.target)
    env.now += 10
    replay = await env.service.allocate("ordinary", target_ref=env.ref, head_sha=STARTING_SHA,
                                        authorize=AsyncMock(return_value=True))
    assert replay["outcome"] == "exists" and replay["lease_expired"] is True
    assert await env.locks.get(env.target) == old
    assert (await env.store.get("ordinary")).repair_attempt_count == 1


@pytest.mark.parametrize("blocked_by", ["other_writer", "held", "unconfirmed", "target_changed"])
async def test_ordinary_repair_reservation_recovery_honors_current_authority(ordinary_env, blocked_by):
    env = ordinary_env
    first = await env.service.allocate("ordinary", target_ref=env.ref, head_sha=STARTING_SHA,
                                       authorize=AsyncMock(return_value=True))
    await env.db.update_task(first["task_id"], status=TaskStatus.READY)
    old = (await env.locks.get(env.target)).grant()
    await env.locks.release(old)
    kwargs = {"target_ref": env.ref, "held": blocked_by == "held"}
    authorize = AsyncMock(return_value=blocked_by != "unconfirmed")
    if blocked_by == "other_writer":
        await env.locks.acquire(env.target, "another-writer")
    if blocked_by == "target_changed":
        kwargs["target_ref"] = "refs/heads/aq/epic"
    before = await env.locks.get(env.target)
    replay = await env.service.allocate("ordinary", head_sha=STARTING_SHA,
                                        authorize=authorize, **kwargs)
    assert replay["outcome"] == {
        "other_writer": "busy", "held": "held", "unconfirmed": "exists",
        "target_changed": "exists",
    }[blocked_by]
    assert await env.locks.get(env.target) == before
    assert (await env.store.get("ordinary")).repair_attempt_count == 1


@pytest.mark.parametrize("authorize", [None, AsyncMock(return_value=False)])
async def test_ordinary_repair_refuses_unconfirmed_publication(ordinary_env, authorize):
    env = ordinary_env
    result = await env.service.allocate("ordinary", target_ref=env.ref,
                                       head_sha=STARTING_SHA, authorize=authorize)
    assert (result["success"], result["outcome"]) == (False, "unconfirmed")
    assert (await env.store.get("ordinary")).repair_attempt_count == 0
    assert await env.locks.get(env.target) is None
    async with env.db._engine.connect() as conn:
        assert not (await conn.execute(select(tasks))).first()


@pytest.mark.parametrize("constraint,outcome", [("held", "held"),
                                                 ("review_rejected", "rejected")])
async def test_ordinary_repair_green_preserves_binding_constraints(ordinary_env, constraint, outcome):
    env = ordinary_env
    result = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA,
                                       green_sha=STARTING_SHA, **{constraint: True})
    assert (result["outcome"], result["attempt_count"]) == (outcome, 0)


async def test_ordinary_repair_brief_leads_the_description_and_freezes_at_filing(ordinary_env):
    """A conflict brief reaches the worker verbatim; the input stays machine-readable."""
    from src.integration.repair import OrdinaryRepairService

    env = ordinary_env
    brief = (f"The batch merge conflicted while building the starting head {STARTING_SHA}.\n"
             "Conflicting member: source (source deadbeef); reason: alembic_head_collision.\n"
             "Still to merge onto the starting head, in this order:\n"
             "  1. source (source deadbeef)")
    authorize = AsyncMock(return_value=True)
    filed = await env.service.allocate(
        "ordinary", target_ref=env.ref, head_sha=STARTING_SHA, brief=brief, authorize=authorize,
    )
    assert filed["outcome"] == "filed"
    authorize.assert_awaited_once_with()
    description = (await env.db.get_task(filed["task_id"])).description
    assert description.startswith(brief + "\n\nRepair the observed head on " + env.ref)
    assert (await OrdinaryRepairService(env.db, locks=env.locks, clock=lambda: env.now).input(
        filed["task_id"])) == {"batch_id": "ordinary", "attempt": 1, "repository_id": "repo",
                               "target_ref": env.ref, "starting_sha": STARTING_SHA}
    assert json.loads(description[description.index("{"):])["starting_sha"] == STARTING_SHA
    # A later visit never rewrites the filed instructions.
    replay = await env.service.allocate("ordinary", target_ref=env.ref, head_sha="c" * 40,
                                        brief="a different brief")
    assert replay["outcome"] == "exists"
    assert (await env.db.get_task(filed["task_id"])).description == description


async def test_ordinary_repair_green_beats_counter_and_expired_attached_writer(ordinary_env):
    from src.integration.ownership import StaleFence

    env = ordinary_env
    old = await env.locks.acquire(env.target, "dead-writer", ttl_seconds=10)
    async with env.db.immediate() as conn:
        await conn.execute(update(integration_batches).values(repair_attempt_count=500))
        await conn.execute(update(integration_branch_owners).values(
            handoff_state="handoff_pending", session_id="dead", workspace_id="lost"))
    assert (await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA,
                                     green_sha=STARTING_SHA))["outcome"] == "busy"
    env.now += 10
    green = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA,
                                       green_sha=STARTING_SHA)
    assert (green["outcome"], green["attempt_count"]) == ("green", 500)
    transport = AsyncMock()
    with pytest.raises(StaleFence, match="expired"):
        await env.locks.fenced_push(old, git=transport, checkout_path="/lost",
                                   repository=None, tip_oid=STARTING_SHA,
                                   expected_old_oid="a" * 40)
    transport.apush_repository_oid.assert_not_awaited()
    async with env.db._engine.connect() as conn:
        assert not (await conn.execute(select(tasks))).first()


async def test_ordinary_repair_claim_and_workspace_use_leased_ref_without_origin(ordinary_env):
    from src.database.queries.claim_queries import _frontier_where
    from src.database.queries.hierarchy_queries import ProjectIntegrationMode
    from src.orchestrator.workspace import WorkspaceMixin
    from src.git.manager import RemoteRefResult, RemoteRefState

    env = ordinary_env
    result = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA)
    await env.db.update_task(result["task_id"], status=TaskStatus.READY)
    task = await env.db.get_task(result["task_id"])
    async with env.db._engine.connect() as conn:
        for mode in (None, ProjectIntegrationMode(True, "repo")):
            assert await conn.scalar(select(tasks.c.id).where(
                tasks.c.id == task.id, _frontier_where("p", mode))) == task.id
        assert not (await conn.execute(select(task_branch_origins))).first()
    stub = SimpleNamespace(db=env.db, git=SimpleNamespace(
        _arun=AsyncMock(return_value="c" * 40), afetch_origin=AsyncMock(),
        als_remote_ref=AsyncMock(return_value=RemoteRefResult(RemoteRefState.PRESENT, oid="c" * 40)),
        ais_ancestor=AsyncMock(side_effect=AssertionError("moved head needs no old proof")),
    ))
    origin, fence, role = await WorkspaceMixin._hierarchy_origin_and_fence(
        stub, task, await env.db.get_project("p"))
    assert role == "repair" and fence.owner_id == task.id
    assert await WorkspaceMixin._hierarchy_repair_start(
        stub, "/work", origin, fence, repository_url="") == "c" * 40


async def test_ordinary_repair_close_uses_normal_published_completion(
    command_handler_factory, tmp_path,
):
    from dataclasses import replace

    from src.git.identity import GitIdentity, task_publish_policy
    from src.git.manager import GitError
    from src.integration.batches import Batch, BatchMember, BatchStore, candidate_ref
    from src.integration.repair import OrdinaryRepairService
    from tests.test_integration_gitops import commit, git

    handler = await command_handler_factory()
    await _configure_db(handler.db)
    remote = tmp_path / "ordinary.git"
    checkout = tmp_path / "ordinary-work"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(checkout))
    git(checkout, "config", "user.name", "Tester")
    git(checkout, "config", "user.email", "tester@example.test")
    base = commit(checkout, {"base": "base"})
    git(checkout, "push", "origin", "HEAD:main")
    git(checkout, "config", "user.name", "Source Worker")
    git(checkout, "config", "user.email", "source@example.test")
    with patch.dict("os.environ", GitIdentity("Source Worker", "source@example.test").env()):
        starting = commit(checkout, {"source": "inherited source"})
    git(checkout, "config", "user.name", "Tester")
    git(checkout, "config", "user.email", "tester@example.test")
    await handler.db.update_repo("repo", url=str(remote), source_path=str(checkout))
    store = BatchStore(handler.db)
    await store.freeze(Batch("ordinary", "p", "repo", "refs/heads/main"),
                       (BatchMember("source", starting, base),), trees={"source": starting})
    ref = candidate_ref("ordinary")
    git(checkout, "push", "origin", f"HEAD:{ref}")
    service = OrdinaryRepairService(handler.db)
    result = await service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=ref, head_sha=starting)
    task_id = result["task_id"]
    git(checkout, "checkout", "-b", ref.removeprefix("refs/heads/"))
    with patch.dict("os.environ", GitIdentity("Tester", "tester@example.test").env()):
        head = commit(checkout, {"fixed": "fixed"})
    policy = await task_publish_policy(
        None, SimpleNamespace(git_identity_name="Tester", git_identity_email="tester@example.test"),
        handler.db, GitManager(), task_id,
    )
    assert policy.authorized_heads == {starting}
    assert await GitManager().acheck_publish_identity(
        str(checkout), head, base_ref=base, branch="aq/identity-probe", policy=policy,
    ) == []
    with pytest.raises(GitError, match="refusing to publish"):
        await GitManager().acheck_publish_identity(
            str(checkout), head, base_ref=base, branch="aq/identity-probe",
            policy=replace(policy, authorized_heads=frozenset()),
        )
    assert service.progress(starting, starting, green=True)
    assert not service.progress(starting, starting, green=False)
    assert service.progress(starting, head, green=False)
    assert await service.added_commits(task_id, GitManager(), str(checkout), head) == [head]
    assert await service.added_commits(task_id, GitManager(), str(checkout), starting) == []
    await handler.db.update_task(task_id, status=TaskStatus.IN_PROGRESS)
    async with handler.db.immediate() as conn:
        await conn.execute(insert(workspaces).values(
            id="ordinary-work", project_id="p", workspace_path=str(checkout), source_type="link",
            locked_by_task_id=task_id, enabled=True, created_at=1))
    await handler.db.create_session(SessionRecord(
        id="ordinary-session", task_id=task_id, project_id="p", profile_id="repairer",
        harness="fake", provider="fake", name="ordinary", lifecycle="task", state="running",
        work_dir=str(checkout), epoch="epoch", instance_token="token", started_at=1))
    handler.orchestrator.git = GitManager()
    handler.orchestrator._run_completion_pipeline = AsyncMock(
        side_effect=AssertionError("ordinary repair entered legacy completion pipeline"))
    handler.orchestrator.arelease_integration_writer_for_retry = AsyncMock(
        side_effect=AssertionError("ordinary repair required stop proof"))
    handler.orchestrator.release_session_task_resources = AsyncMock()
    handler._current_scope = {"kind": "session", "session_id": "ordinary-session",
                              "task_id": task_id, "project_id": "p", "elevated": False}
    args = {"task_id": task_id, "session_id": "ordinary-session", "outcome": "pass",
            "summary": "published ordinary repair"}
    refused = await handler._cmd_task_close(args)
    assert refused["success"] is False
    assert (await handler.db.get_task(task_id)).status == TaskStatus.IN_PROGRESS
    git(checkout, "push", "origin", f"HEAD:{ref}")
    closed = await handler._cmd_task_close(args)
    assert closed["success"] is True, closed
    assert (await handler.db.get_task(task_id)).status == TaskStatus.COMPLETED
    completion = await handler.db.get_task_completion(task_id)
    assert completion.outcome == "pass"
    assert head in completion.commits
    async with handler.db._engine.connect() as conn:
        assert not (await conn.execute(select(integration_repair_operations))).first()
        assert not (await conn.execute(select(integration_promotion_intents))).first()
    assert (await service.locks.get(BranchKey(repository_id="repo", branch=ref))).holder is None


async def test_ordinary_repair_worker_git_push_rejects_expired_or_wrong_ref(ordinary_env):
    import time

    from src.git.manager import GitError
    from src.plugins.internal.git import GitPlugin

    env = ordinary_env
    env.now = time.time()
    result = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA)
    task_id = result["task_id"]
    plugin = GitPlugin.__new__(GitPlugin)
    plugin._db = SimpleNamespace(_db=env.db)
    plugin._git = SimpleNamespace(aref_exists=AsyncMock(return_value=True),
                                 apush_validated_delivery=AsyncMock(return_value=STARTING_SHA))
    plugin._ctx = SimpleNamespace(_bus=None)
    publication = SimpleNamespace(task_id=task_id, branch=env.ref.removeprefix("refs/heads/"),
                                  default_branch="main", repository_url="/authorized")
    plugin._worker_publication = AsyncMock(return_value=publication)
    plugin._publish_policy = AsyncMock(return_value=SimpleNamespace(notes=[]))
    plugin._source_ci_inherited_oids = AsyncMock(return_value=[])
    with patch("src.plugins.internal.git._worker_principal", return_value=object()):
        async with env.db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).values(expires_at=time.time() - 1))
        with pytest.raises(GitError, match="expired"):
            await plugin._push("/work", None, {}, None)
        plugin._git.apush_validated_delivery.assert_not_awaited()
        lease = await env.locks.acquire(env.target, task_id)
        publication.branch = "aq/other"
        with pytest.raises(GitError, match="allocated ref"):
            await plugin._push("/work", None, {}, None)
        plugin._git.apush_validated_delivery.assert_not_awaited()
        publication.branch = env.ref.removeprefix("refs/heads/")
        assert (await plugin._push("/work", None, {}, None))[:2] == (publication.branch, STARTING_SHA)
        plugin._git.apush_validated_delivery.assert_awaited_once()
        assert (await env.locks.get(env.target)).fence == lease.token


async def test_ordinary_repair_restart_does_not_renew_expired_running_writer(ordinary_env):
    env = ordinary_env
    first = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA,
                                       ttl_seconds=10)
    await env.db.update_task(first["task_id"], status=TaskStatus.IN_PROGRESS)
    expired = await env.locks.get(env.target)
    env.now += 10
    replay = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha="c" * 40)
    assert (replay["outcome"], replay["attempt_count"], replay["task_id"], replay["lease_expired"]) == (
        "exists", 1, first["task_id"], True)
    assert await env.locks.get(env.target) == expired
    epic = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref="refs/heads/aq/epic", head_sha="c" * 40)
    assert epic["outcome"] == "exists" and epic["target_changed"]
    assert await env.locks.get(BranchKey(repository_id="repo", branch="aq/epic")) is None


@pytest.mark.parametrize("status", [TaskStatus.PAUSED, TaskStatus.BLOCKED, TaskStatus.FAILED])
async def test_ordinary_repair_keeps_normal_recovery_task_after_lease_expiry(ordinary_env, status):
    env = ordinary_env
    first = await env.service.allocate("ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha=STARTING_SHA,
                                       ttl_seconds=10)
    await env.db.update_task(first["task_id"], status=status, retry_count=0)
    env.now += 10
    results = await asyncio.gather(*(env.service.allocate(
        "ordinary", authorize=AsyncMock(return_value=True), target_ref=env.ref, head_sha="c" * 40,
    ) for _ in range(4)))
    assert {r["task_id"] for r in results} == {first["task_id"]}
    assert {r["outcome"] for r in results} == {"exists"}
    assert (await env.store.get("ordinary")).repair_attempt_count == 1
