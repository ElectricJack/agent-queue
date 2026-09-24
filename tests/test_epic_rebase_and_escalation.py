"""Rebase warnings and durable stuck-batch notices for the integration train."""

# The imported fixtures intentionally share names with injected test parameters.
# ruff: noqa: F811

from __future__ import annotations

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_check_evidence,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    messages,
    tasks,
)
from src.integration.epic_dependencies import declare
from src.integration.repair import RepairService
from tests.test_epic_pr_review_evidence import case  # noqa: F401 -- pytest fixture
from tests.test_integration_repair import _boundary, _policy, db  # noqa: F401 -- pytest fixture


async def _add_dependent(case):
    async with case["db"].immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="e2",
                project_id="p",
                repo_id="repo",
                title="Dependent epic",
                description="",
                status="COMPLETED",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await declare(conn, dependent_task_id="e2", dependency_task_id="e1", now=1.0)


async def test_rejection_flags_declared_dependents_and_replay_is_idempotent(case):
    await _add_dependent(case)
    for _ in range(2):
        evidence = await case["producer"].snapshot_from_pull_request(
            "e1", verdict="rejected", reviewer_login="jkern", reviewed_sha=case["first"]
        )
        assert evidence["verdict"] == "rejected"
    assert await case["db"].get_task_labels("e2") == ["needs-rebase"]


async def test_approval_does_not_flag_dependents(case):
    await _add_dependent(case)
    await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert await case["db"].get_task_labels("e2") == []


async def test_rejection_without_dependents_is_harmless(case):
    evidence = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="rejected", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert evidence["verdict"] == "rejected"


@pytest.fixture
async def sealed_batch(db):
    policy = _boundary(primary_attempts=10).repair.model_dump(mode="json")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request-batch",
                source_manifest_digest="manifest",
                base_sha="a" * 40,
                lifecycle="sealing",
                current_revision=1,
                integration_branch="aq/integration/batch",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=10.0,
                updated_at=10.0,
            )
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                id="review-e1",
                source_task_id="e1",
                repository_id="repo",
                source_base="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                reviewer_task_id=None,
                reviewer_session_attempt_id=None,
                reviewer_identity="github:jkern",
                review_kind="parent",
                generation=1,
                verdict="approved",
                evidence={},
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="batch",
                ordinal=0,
                task_id="e1",
                repository_id="repo",
                source_base_sha="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                review_evidence_id="review-e1",
                review_evidence={},
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="repairing")
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="repair-batch-batch",
                target_kind="batch",
                batch_id="batch",
                episode_id="batch",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=10.0,
                updated_at=10.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="repair-batch-batch",
                ordinal=0,
                policy=policy,
                starting_sha="a" * 40,
                current_subject={"kind": "batch", "revision": 1, "candidate_sha": "b" * 40},
                attempts=0,
                state="active",
            )
        )
    return {"batch_id": "batch", "operation_id": "repair-batch-batch"}


async def _record(db, batch, number: int, conclusion: str):
    evidence_id = f"check-{number}"
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_check_evidence).values(
                id=evidence_id,
                operation_id=batch["operation_id"],
                batch_id=batch["batch_id"],
                candidate_revision=1,
                producer_id="forge",
                workflow_id="workflow",
                run_id=f"run-{number}",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": conclusion},
                conclusion=conclusion,
                classification="conclusive",
                observed_at=100.0 + number,
            )
        )
    return await RepairService(db).record_result(
        batch["operation_id"], evidence_id, now=100.0 + number
    )


async def _notices(db):
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages).where(messages.c.id == "msg-stuck-batch-batch")
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def test_batch_escalates_on_third_failure_with_age_and_members(db, sealed_batch):
    for number in range(1, 3):
        await _record(db, sealed_batch, number, "failure")
    assert await _notices(db) == []
    await _record(db, sealed_batch, 3, "failure")
    notices = await _notices(db)
    assert len(notices) == 1
    assert notices[0]["to_id"] == "supervisor-p"
    assert "batch" in notices[0]["body"]
    assert "93 seconds" in notices[0]["body"]
    assert "e1" in notices[0]["body"]


async def test_batch_escalation_is_once_even_after_more_failures(db, sealed_batch):
    for number in range(1, 6):
        await _record(db, sealed_batch, number, "failure")
    assert len(await _notices(db)) == 1


async def test_failure_streak_crosses_primary_and_debug_stages(db, sealed_batch):
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == sealed_batch["operation_id"])
            .values(policy_snapshot=_policy())
        )
        await conn.execute(
            update(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == sealed_batch["operation_id"])
            .values(policy=_boundary().repair.model_dump(mode="json"))
        )
    await _record(db, sealed_batch, 1, "failure")
    second = await _record(db, sealed_batch, 2, "failure")
    assert second["action"] == "dispatch_debug"
    await _record(db, sealed_batch, 3, "failure")
    assert len(await _notices(db)) == 1


async def test_batch_success_resets_failure_streak(db, sealed_batch):
    await _record(db, sealed_batch, 1, "failure")
    await _record(db, sealed_batch, 2, "failure")
    await _record(db, sealed_batch, 3, "success")
    # A later check run re-arms the same batch repair stage.
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == sealed_batch["operation_id"])
            .values(state="active")
        )
    await _record(db, sealed_batch, 4, "failure")
    assert await _notices(db) == []
