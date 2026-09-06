"""Durable bounded repair stages and execution delegates."""

from __future__ import annotations

import asyncio
import subprocess
import pytest
from types import SimpleNamespace
from sqlalchemy import insert, select, update
from unittest.mock import AsyncMock

from src.database import Database
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stage_evidence,
    integration_repair_stages,
    playbook_artifacts,
    sessions,
    task_integration_checkpoints,
    task_branch_origins,
    task_delivery_receipts,
    tasks,
    workspaces,
)
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
from src.git.manager import GitManager, RemoteRefResult, RemoteRefState
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
)
from src.scheduler import AssignAction
from src.profiles.capabilities import CapabilityPolicy


STARTING_SHA = "a" * 40


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
async def db(tmp_path):
    database = Database(str(tmp_path / "repair.db"))
    await database.initialize()
    await _configure_db(database)
    yield database
    await database.close()


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
    db, *, evidence_id: str = "failed-check", starting_sha: str = STARTING_SHA
) -> None:
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            title="Parent",
            description="",
            status=TaskStatus.PAUSED,
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
                policy_snapshot=_policy(),
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
                observed_at=9.0,
            )
        )


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


async def test_start_accepts_only_exact_persisted_conflict_trigger(db):
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
        "operation", STARTING_SHA, "conflict-intent", now=100.0
    )
    assert started["outcome"] == "started"


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
    assert rows[1]["profile_id"] == "debugger"
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


async def _seed_root_operation(db) -> str:
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
                lifecycle="testing",
                current_revision=0,
                integration_branch="aq/integration/batch",
                policy_snapshot=_policy(),
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
                head_sha=STARTING_SHA,
                state="testing",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        operation = await RepairService(db).reserve_batch_operation_on(
            conn, "batch", now=50.0
        )
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
        route_validator=lambda _intelligence_class, _profile_id: True,
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
        route_validator=lambda _intelligence_class, _profile_id: True,
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
            profile_id="repairer",
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
        route_validator=lambda _intelligence_class, _profile_id: True,
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
async def test_debug_dispatch_retains_unfinished_primary_workspace_atomically(
    db, tmp_path, retained_state
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

    async def stopped(owner):
        await db.update_session(
            owner["session_id"], state="stopped", desired_state="stopped"
        )
        return {
            "session_id": owner["session_id"],
            "workspace_id": owner["workspace_id"],
            "head_sha": retained_head,
            "instance_token": "token",
        }

    service = RepairService(
        db,
        route_validator=lambda _intelligence_class, _profile_id: True,
        confirm_stopped=stopped,
    )
    await service.start("operation", STARTING_SHA, "failed-check", now=100.0)
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
            lifecycle="task",
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
            .values(status="IN_PROGRESS")
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

    failed_stop = await RepairService(
        db,
        route_validator=lambda _intelligence_class, _profile_id: True,
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
    assert stage["starting_sha"] == retained_head
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
        SimpleNamespace(db=db, git=GitManager()),
        debug_task,
        await db.get_project("p"),
        SimpleNamespace(workspace=workspace),
        {"base_sha": STARTING_SHA},
        Fence.model_validate(debug["fence"]),
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
    service = RepairService(
        db, route_validator=lambda _intelligence_class, _profile_id: True
    )
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
        route_validator=lambda _intelligence_class, _profile_id: True,
        confirm_stopped=stopped,
    )
    debug = await handoff_service.dispatch("operation", 1)
    assert debug["outcome"] == "dispatched"
    commit_proof = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "commits": exact_commits,
    }
    async with db.immediate() as conn:
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
    primary_service = RepairService(
        db, route_validator=lambda _intelligence_class, _profile_id: True
    )
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
        route_validator=lambda _intelligence_class, _profile_id: True,
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
    from src.intelligence_classes import IntelligenceClass
    from src.integration.repair import RepairService
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
        route_validator=lambda _intelligence_class, _profile_id: True,
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
        route_validator=lambda _intelligence_class, _profile_id: True,
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

    assert refused["outcome"] == "human_required"
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
        route_validator=lambda _intelligence_class, _profile_id: True,
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
        route_validator=lambda _intelligence_class, _profile_id: True,
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
    assert closed["success"] is True
    assert (await handler.db.get_task(repair_task_id)).status is TaskStatus.COMPLETED
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


async def test_batch_repair_delegate_can_file_only_explicit_project_root(
    command_handler_factory,
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
        route_validator=lambda _intelligence_class, _profile_id: True,
    )
    await service.start(operation_id, STARTING_SHA, "batch", now=100.0)
    dispatched = await service.dispatch(operation_id, 0)
    repair_task_id = dispatched["repair_task_id"]
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == repair_task_id)
            .values(status="IN_PROGRESS")
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
