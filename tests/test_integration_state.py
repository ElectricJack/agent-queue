"""Durable hierarchical-integration persistence contracts."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, inspect, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database import Database
from src.database.tables import (
    integration_batches,
    integration_promotion_intents,
    integration_review_evidence,
)
from src.integration.models import BranchKey, Fence, RepairPolicy, RequiredCheckSet
from src.models import Project, Task
from tests.pg_dsn import ensure_worker_postgres_dsn


POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "integration-state.db"))
        await database.initialize()
    await database.create_project(Project(id="p", name="integration project"))
    yield database
    await database.close()


async def _integrity_error_in_savepoint(conn, statement) -> None:
    """Keep a PostgreSQL transaction usable after an expected violation."""
    with pytest.raises((IntegrityError, DBAPIError)):
        async with conn.begin_nested():
            await conn.execute(statement)


def _batch_values(**overrides):
    values = {
        "id": "batch",
        "project_id": "p",
        "repository_id": "repo",
        "request_id": "request-1",
        "trigger": "periodic",
        "source_manifest_digest": "manifest",
        "base_sha": "a" * 40,
        "lifecycle": "sealing",
        "current_revision": 0,
        "integration_branch": "refs/heads/aq/integration/p/1",
        "policy_snapshot": {},
        "artifact_snapshot": {},
        "cleanup_state": "pending",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    values.update(overrides)
    return values


def _review_evidence_values(**overrides):
    values = {
        "id": "review-1",
        "source_task_id": "task-1",
        "repository_id": "repo",
        "source_base": "a" * 40,
        "reviewed_head_sha": "b" * 40,
        "reviewed_tree_sha": "c" * 40,
        "reviewer_task_id": "reviewer-1",
        "reviewer_session_attempt_id": None,
        "review_kind": "review",
        "generation": 0,
        "verdict": "approved",
        "evidence": {"approved": True},
        "created_at": 1.0,
    }
    values.update(overrides)
    return values


async def test_batch_request_and_empty_structure_are_database_invariants(db):
    from src.database.tables import (
        integration_batch_members,
        integration_repair_operations,
        project_integration_leases,
    )

    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                **_batch_values(
                    id="empty",
                    lifecycle="empty",
                    base_sha=None,
                    integration_branch=None,
                )
            )
        )
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_batches).values(
                **_batch_values(id="duplicate-request", lifecycle="failed")
            ),
        )
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_batches).values(
                **_batch_values(
                    id="invalid-empty",
                    project_id="other-project",
                    request_id="request-2",
                    lifecycle="empty",
                )
            ),
        )
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_batches).values(
                **_batch_values(
                    id="invalid-sealed",
                    project_id="third-project",
                    request_id="request-3",
                    lifecycle="sealed",
                    base_sha=None,
                    integration_branch=None,
                )
            ),
        )
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_batch_members).values(
                batch_id="empty",
                ordinal=0,
                task_id="task-1",
                repository_id="repo",
                source_base_sha="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                review_evidence_id="review-1",
                review_evidence={},
            ),
        )
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_repair_operations).values(
                id="empty-repair",
                target_kind="batch",
                batch_id="empty",
                episode_id="episode",
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                created_at=1.0,
                updated_at=1.0,
            ),
        )
        await _integrity_error_in_savepoint(
            conn,
            insert(project_integration_leases).values(
                project_id="p",
                repository_id="repo",
                batch_id="empty",
                owner_id="owner",
                fence_token=1,
                heartbeat_at=1.0,
                expires_at=2.0,
            ),
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="preexisting-repair",
                target_kind="batch",
                batch_id="future-empty-repair",
                episode_id="episode-2",
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_batches).values(
                **_batch_values(
                    id="future-empty-repair",
                    project_id="future-project-repair",
                    request_id="request-future-repair",
                    lifecycle="empty",
                    base_sha=None,
                    integration_branch=None,
                )
            ),
        )
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="future-project-lease",
                repository_id="repo",
                batch_id="future-empty-lease",
                owner_id="owner",
                fence_token=1,
                heartbeat_at=1.0,
                expires_at=2.0,
            )
        )
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_batches).values(
                **_batch_values(
                    id="future-empty-lease",
                    project_id="future-project-lease",
                    request_id="request-future-lease",
                    lifecycle="empty",
                    base_sha=None,
                    integration_branch=None,
                )
            ),
        )


async def test_post_sealing_batch_identity_is_frozen_and_cannot_return(db):
    identity_edits = {
        "project_id": "changed-project",
        "repository_id": "changed-repo",
        "request_id": "changed-request",
        "trigger": "manual",
        "source_manifest_digest": "changed-manifest",
        "base_sha": "d" * 40,
        "integration_branch": "refs/heads/changed",
        "policy_snapshot": {"changed": True},
        "artifact_snapshot": {"changed": True},
        "created_at": 2.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(**_batch_values()))
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="sealed")
        )
        for field, value in identity_edits.items():
            await _integrity_error_in_savepoint(
                conn,
                update(integration_batches)
                .where(integration_batches.c.id == "batch")
                .values(**{field: value}),
            )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(current_revision=1, updated_at=2.0)
        )
        await _integrity_error_in_savepoint(
            conn,
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="sealing"),
        )


async def test_batch_member_requires_append_only_review_evidence(db):
    from src.database.tables import integration_batch_members, integration_review_evidence

    member = {
        "batch_id": "batch",
        "ordinal": 0,
        "task_id": "task-1",
        "repository_id": "repo",
        "source_base_sha": "a" * 40,
        "reviewed_head_sha": "b" * 40,
        "reviewed_tree_sha": "c" * 40,
        "review_evidence": {"approved": True},
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(**_batch_values()))
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_batch_members).values(
                **member, review_evidence_id="missing-review"
            ),
        )
        await conn.execute(
            insert(integration_review_evidence).values(**_review_evidence_values())
        )
        await conn.execute(
            insert(integration_batch_members).values(
                **member, review_evidence_id="review-1"
            )
        )


async def test_active_batch_is_unique_per_project(db):
    values = {
        "project_id": "p",
        "repository_id": "repo",
        "request_id": "request-b1",
        "source_manifest_digest": "manifest",
        "base_sha": "a" * 40,
        "lifecycle": "sealed",
        "integration_branch": "refs/heads/integration/b1",
        "current_revision": 0,
        "policy_snapshot": "{}",
        "artifact_snapshot": "{}",
        "cleanup_state": "pending",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(id="b1", **values))
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_batches).values(
                id="b2", **(values | {"request_id": "request-b2"})
            ),
        )


async def test_negative_checkpoint_generation_is_rejected(db):
    from src.database.tables import task_integration_checkpoints

    async with db.immediate() as conn:
        await _integrity_error_in_savepoint(
            conn,
            insert(task_integration_checkpoints).values(
                task_id="deleted-task",
                repository_id="repo",
                branch="parent",
                generation=-1,
                state="working",
                version=0,
                updated_at=1.0,
            ),
        )


async def test_duplicate_promotion_intent_domain_key_is_rejected(db):
    values = {
        "domain_key": "delivery:t1:head:repo:parent",
        "receipt_id": "receipt-1",
        "source_task_id": "t1",
        "source_head": "a" * 40,
        "source_base": "b" * 40,
        "repository_id": "repo",
        "target_branch": "parent",
        "expected_target": "c" * 40,
        "fence_owner_id": "owner",
        "fence_token": 1,
        "state": "prepared",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_promotion_intents).values(id="i1", **values))
        await _integrity_error_in_savepoint(
            conn, insert(integration_promotion_intents).values(id="i2", **values)
        )


async def test_sealed_batch_membership_is_immutable(db):
    from src.database.tables import integration_batch_members

    batch = {
        "project_id": "p",
        "repository_id": "repo",
        "request_id": "request-b1",
        "source_manifest_digest": "manifest",
        "base_sha": "a" * 40,
        "lifecycle": "sealing",
        "integration_branch": "refs/heads/integration/b1",
        "current_revision": 0,
        "policy_snapshot": {},
        "artifact_snapshot": {},
        "cleanup_state": "pending",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    member = {
        "batch_id": "b1",
        "ordinal": 0,
        "task_id": "t1",
        "repository_id": "repo",
        "source_base_sha": "a" * 40,
        "reviewed_head_sha": "b" * 40,
        "reviewed_tree_sha": "c" * 40,
        "review_evidence_id": "review-1",
        "review_evidence": {},
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(id="b1", **batch))
        await conn.execute(
            insert(integration_review_evidence).values(**_review_evidence_values())
        )
        await conn.execute(insert(integration_batch_members).values(**member))
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "b1")
            .values(lifecycle="sealed")
        )
        await _integrity_error_in_savepoint(
            conn, insert(integration_batch_members).values(**(member | {"ordinal": 1, "task_id": "t2"}))
        )
        await _integrity_error_in_savepoint(
            conn,
            delete(integration_batch_members).where(
                integration_batch_members.c.batch_id == "b1"
            ),
        )


async def test_member_cannot_move_from_a_sealed_batch_to_a_sealing_batch(db):
    from src.database.tables import integration_batch_members

    batch = {
        "project_id": "p", "repository_id": "repo", "request_id": "request-b1",
        "source_manifest_digest": "manifest", "base_sha": "a" * 40,
        "lifecycle": "sealing", "integration_branch": "refs/heads/integration/b1",
        "current_revision": 0, "policy_snapshot": {},
        "artifact_snapshot": {}, "cleanup_state": "pending", "created_at": 1.0, "updated_at": 1.0,
    }
    member = {
        "batch_id": "b1", "ordinal": 0, "task_id": "t1", "repository_id": "repo",
        "source_base_sha": "a" * 40, "reviewed_head_sha": "b" * 40,
        "reviewed_tree_sha": "c" * 40, "review_evidence_id": "review-1",
        "review_evidence": {},
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(id="b1", **batch))
        await conn.execute(insert(integration_batches).values(
            id="b2",
            **batch | {
                "project_id": "p2",
                "request_id": "request-b2",
                "integration_branch": "refs/heads/integration/b2",
            },
        ))
        await conn.execute(
            insert(integration_review_evidence).values(**_review_evidence_values())
        )
        await conn.execute(insert(integration_batch_members).values(**member))
        await conn.execute(update(integration_batches).where(integration_batches.c.id == "b1").values(lifecycle="sealed"))
        await _integrity_error_in_savepoint(
            conn,
            update(integration_batch_members)
            .where(integration_batch_members.c.batch_id == "b1")
            .values(batch_id="b2"),
        )


async def test_durable_counters_and_fences_never_decrease(db):
    from src.database.tables import (
        integration_branch_owners, integration_candidate_revisions, integration_outbox,
        integration_repair_operations, project_integration_leases,
        project_integration_schedules, task_integration_checkpoints,
    )

    batch = {
        "id": "b1", "project_id": "p", "repository_id": "repo", "request_id": "request-b1",
        "source_manifest_digest": "manifest", "base_sha": "a" * 40,
        "lifecycle": "sealing", "integration_branch": "refs/heads/integration/b1",
        "current_revision": 2, "policy_snapshot": {}, "artifact_snapshot": {},
        "cleanup_state": "pending", "created_at": 1.0, "updated_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(**batch))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="parent", repository_id="repo", branch="parent", generation=2,
            state="working", version=3, updated_at=1.0,
        ))
        await conn.execute(insert(integration_branch_owners).values(
            id="owner", repository_id="repo", ref="parent", owner_id="owner", owner_role="collector",
            fence_token=2, handoff_state="reserved", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(project_integration_leases).values(
            project_id="lease", repository_id="repo", batch_id="b1", owner_id="owner",
            fence_token=2, heartbeat_at=1.0, expires_at=2.0,
        ))
        await conn.execute(insert(project_integration_schedules).values(
            project_id="p", enabled=False, interval_seconds=300, next_due_at=1.0,
            request_sequence=2, updated_at=1.0,
        ))
        await conn.execute(insert(integration_outbox).values(
            id="event", dedup_key="event", project_id="p", event_type="integration.sealed",
            payload={}, available_at=1.0, attempts=2, created_at=1.0,
        ))
        await conn.execute(insert(integration_candidate_revisions).values(
            batch_id="b1", revision=0, construction_base_sha="a" * 40, next_member_ordinal=2,
            state="constructing", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_repair_operations).values(
            id="op", target_kind="batch", batch_id="b1", episode_id="episode", active_stage=2,
            state="active", policy_snapshot={}, artifact_snapshot={}, required_check_version="v1",
            created_at=1.0, updated_at=1.0,
        ))
        for statement in (
            update(task_integration_checkpoints).where(task_integration_checkpoints.c.task_id == "parent").values(generation=1),
            update(task_integration_checkpoints).where(task_integration_checkpoints.c.task_id == "parent").values(version=2),
            update(integration_batches).where(integration_batches.c.id == "b1").values(current_revision=1),
            update(integration_branch_owners).where(integration_branch_owners.c.id == "owner").values(fence_token=1),
            update(project_integration_leases).where(project_integration_leases.c.project_id == "lease").values(fence_token=1),
            update(project_integration_schedules).where(project_integration_schedules.c.project_id == "p").values(request_sequence=1),
            update(integration_outbox).where(integration_outbox.c.id == "event").values(attempts=1),
            update(integration_candidate_revisions).where(integration_candidate_revisions.c.batch_id == "b1").values(next_member_ordinal=1),
            update(integration_repair_operations).where(integration_repair_operations.c.id == "op").values(active_stage=1),
        ):
            await _integrity_error_in_savepoint(conn, statement)


async def test_prepared_intent_and_receipt_audit_are_immutable(db):
    from src.database.tables import task_delivery_receipts

    intent = {
        "id": "intent", "domain_key": "intent", "receipt_id": "receipt", "source_head": "a" * 40,
        "source_base": "b" * 40, "repository_id": "repo", "target_branch": "parent",
        "expected_target": "c" * 40, "prepared_sha": "d" * 40, "fence_owner_id": "owner",
        "fence_token": 1, "state": "prepared", "created_at": 1.0, "updated_at": 1.0,
    }
    receipt = {
        "id": "receipt", "domain_key": "receipt", "repository_id": "repo", "target_branch": "parent",
        "disposition": "code", "created_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_promotion_intents).values(**intent))
        await conn.execute(insert(task_delivery_receipts).values(**receipt))
        await _integrity_error_in_savepoint(
            conn, update(integration_promotion_intents).where(integration_promotion_intents.c.id == "intent").values(source_head="e" * 40)
        )
        await _integrity_error_in_savepoint(
            conn, update(integration_promotion_intents).where(integration_promotion_intents.c.id == "intent").values(fence_token=2)
        )
        await conn.execute(update(integration_promotion_intents).where(integration_promotion_intents.c.id == "intent").values(state="pushed"))
        await _integrity_error_in_savepoint(
            conn, update(task_delivery_receipts).where(task_delivery_receipts.c.id == "receipt").values(after_sha="e" * 40)
        )
        await _integrity_error_in_savepoint(
            conn, delete(task_delivery_receipts).where(task_delivery_receipts.c.id == "receipt")
        )


async def test_materialized_branch_origin_cannot_be_deleted(db):
    from src.database.tables import task_branch_origins

    async with db.immediate() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="origin", task_id="task", repository_id="repo", base_sha="a" * 40,
            creation_generation=0, reserved=True, materialized=True, created_at=1.0,
        ))
        await _integrity_error_in_savepoint(
            conn, delete(task_branch_origins).where(task_branch_origins.c.id == "origin")
        )


async def test_candidate_member_results_are_ordered_and_unique_per_revision(db):
    from src.database.tables import (
        integration_batch_members, integration_candidate_member_results,
        integration_candidate_revisions,
    )

    async with db.immediate() as conn:
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_candidate_member_results).values(
                batch_id="missing", revision=0, member_ordinal=0, input_head_sha="a" * 40,
                input_tree_sha="b" * 40, result="applied", generated_squash_sha="c" * 40,
                created_at=1.0, updated_at=1.0,
            ),
        )
        await conn.execute(insert(integration_batches).values(
            id="b1", project_id="p", repository_id="repo", request_id="request-b1",
            source_manifest_digest="manifest", base_sha="a" * 40, lifecycle="sealing",
            integration_branch="refs/heads/integration/b1", current_revision=0,
            policy_snapshot={}, artifact_snapshot={},
            cleanup_state="pending", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(
            insert(integration_review_evidence).values(**_review_evidence_values())
        )
        await conn.execute(insert(integration_batch_members).values(
            batch_id="b1", ordinal=0, task_id="root", repository_id="repo", source_base_sha="a" * 40,
            reviewed_head_sha="b" * 40, reviewed_tree_sha="c" * 40,
            review_evidence_id="review-1", review_evidence={},
        ))
        await conn.execute(insert(integration_candidate_revisions).values(
            batch_id="b1", revision=0, construction_base_sha="a" * 40,
            next_member_ordinal=0, state="constructing", created_at=1.0, updated_at=1.0,
        ))
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_candidate_member_results).values(
                batch_id="b1", revision=0, member_ordinal=1, input_head_sha="a" * 40,
                input_tree_sha="b" * 40, result="applied", generated_squash_sha="c" * 40,
                created_at=1.0, updated_at=1.0,
            ),
        )
        await conn.execute(insert(integration_candidate_member_results).values(
            batch_id="b1", revision=0, member_ordinal=0, input_head_sha="a" * 40,
            input_tree_sha="b" * 40, result="applied", generated_squash_sha="c" * 40,
            created_at=1.0, updated_at=1.0,
        ))
        await _integrity_error_in_savepoint(
            conn,
            insert(integration_candidate_member_results).values(
                batch_id="b1", revision=0, member_ordinal=0, input_head_sha="a" * 40,
                input_tree_sha="b" * 40, result="applied", generated_squash_sha="c" * 40,
                created_at=1.0, updated_at=1.0,
            ),
        )


async def test_only_one_active_repair_operation_can_target_a_parent(db):
    from src.database.tables import integration_repair_operations

    values = {
        "target_kind": "parent", "parent_task_id": "parent", "active_stage": 0, "state": "active",
        "policy_snapshot": {}, "artifact_snapshot": {}, "required_check_version": "v1",
        "created_at": 1.0, "updated_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_repair_operations).values(id="op1", episode_id="one", **values))
        await _integrity_error_in_savepoint(
            conn, insert(integration_repair_operations).values(id="op2", episode_id="two", **values)
        )


async def test_materialized_branch_origin_is_immutable(db):
    from src.database.tables import task_branch_origins

    async with db.immediate() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="origin", task_id="task", repository_id="repo", base_sha="a" * 40,
            creation_generation=0, reserved=True, materialized=True, created_at=1.0,
        ))
        await _integrity_error_in_savepoint(
            conn,
            update(task_branch_origins)
            .where(task_branch_origins.c.id == "origin")
            .values(base_sha="b" * 40),
        )


async def test_all_durable_records_round_trip(db):
    from src.database.tables import (
        integration_batch_members,
        integration_branch_owners,
        integration_candidate_member_results,
        integration_candidate_revisions,
        integration_check_evidence,
        integration_outbox,
        integration_repair_operations,
        integration_repair_stages,
        project_integration_leases,
        project_integration_schedules,
        task_branch_origins,
        task_delivery_receipts,
        task_integration_checkpoints,
    )

    batch = {
        "id": "b1", "project_id": "p", "repository_id": "repo", "request_id": "request-b1",
        "source_manifest_digest": "manifest", "base_sha": "a" * 40,
        "lifecycle": "sealing", "integration_branch": "refs/heads/integration/b1",
        "current_revision": 0, "policy_snapshot": {}, "artifact_snapshot": {},
        "cleanup_state": "pending", "created_at": 1.0, "updated_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(**batch))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="parent", repository_id="repo", branch="parent", generation=0,
            state="working", version=0, updated_at=1.0,
        ))
        await conn.execute(insert(task_branch_origins).values(
            id="origin", task_id="child", repository_id="repo", base_sha="a" * 40,
            creation_generation=0, reserved=True, materialized=True, created_at=1.0,
        ))
        await conn.execute(insert(integration_branch_owners).values(
            id="owner", repository_id="repo", ref="refs/heads/parent", owner_id="owner",
            owner_role="collector", fence_token=1, handoff_state="reserved", created_at=1.0,
            updated_at=1.0,
        ))
        await conn.execute(insert(integration_promotion_intents).values(
            id="intent", domain_key="intent-key", receipt_id="receipt", source_head="a" * 40,
            source_base="b" * 40, repository_id="repo", target_branch="parent",
            expected_target="c" * 40, fence_owner_id="owner", fence_token=1, state="prepared",
            created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(task_delivery_receipts).values(
            id="receipt", domain_key="receipt-key", repository_id="repo", target_branch="parent",
            disposition="noop", resolution_evidence={"reason": "already-delivered"}, created_at=1.0,
        ))
        await conn.execute(
            insert(integration_review_evidence).values(**_review_evidence_values())
        )
        await conn.execute(insert(integration_batch_members).values(
            batch_id="b1", ordinal=0, task_id="root", repository_id="repo", source_base_sha="a" * 40,
            reviewed_head_sha="b" * 40, reviewed_tree_sha="c" * 40,
            review_evidence_id="review-1", review_evidence={},
        ))
        await conn.execute(insert(integration_candidate_revisions).values(
            batch_id="b1", revision=0, construction_base_sha="a" * 40, next_member_ordinal=0,
            state="constructing", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_candidate_member_results).values(
            batch_id="b1", revision=0, member_ordinal=0, input_head_sha="a" * 40,
            input_tree_sha="b" * 40, generated_squash_sha="c" * 40, result="applied",
            created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_repair_operations).values(
            id="op", target_kind="batch", batch_id="b1", episode_id="episode", active_stage=0,
            state="active", policy_snapshot={}, artifact_snapshot={}, required_check_version="v1",
            created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_repair_stages).values(
            operation_id="op", ordinal=0, policy={}, intelligence_class="standard-medium",
            starting_sha="a" * 40, attempts=0, state="pending",
        ))
        await conn.execute(insert(integration_check_evidence).values(
            id="evidence", batch_id="b1", candidate_revision=0, producer_id="github",
            workflow_id="workflow", run_id="run", attempt=0, required_check_version="v1",
            checks={}, conclusion="success", classification="required", observed_at=1.0,
        ))
        await conn.execute(insert(project_integration_schedules).values(
            project_id="p", enabled=False, interval_seconds=300, next_due_at=1.0,
            request_sequence=0, updated_at=1.0,
        ))
        await conn.execute(insert(project_integration_leases).values(
            project_id="lease-project", repository_id="repo", batch_id="b1", owner_id="owner",
            fence_token=1, heartbeat_at=1.0, expires_at=2.0,
        ))
        await conn.execute(insert(integration_outbox).values(
            id="event", dedup_key="event-key", project_id="p", event_type="integration.sealed",
            payload={}, available_at=1.0, attempts=0, created_at=1.0,
        ))

    tables = (
        task_integration_checkpoints, task_branch_origins, integration_branch_owners,
        integration_promotion_intents, task_delivery_receipts, integration_batches,
        integration_batch_members, integration_candidate_revisions, integration_candidate_member_results,
        integration_repair_operations,
        integration_repair_stages, integration_check_evidence, project_integration_schedules,
        project_integration_leases, integration_outbox,
    )
    async with db._engine.connect() as conn:
        for table in tables:
            assert len((await conn.execute(select(table))).mappings().all()) == 1


async def test_upgrade_from_prior_schema_creates_every_integration_table(tmp_path, disable_schema_cache):
    """The disposable migration path, not metadata.create_all, owns this DDL."""
    database = Database(str(tmp_path / "upgrade.db"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            names = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        assert {
            "task_integration_checkpoints", "task_branch_origins", "integration_branch_owners",
            "integration_promotion_intents", "task_delivery_receipts", "integration_batches",
            "integration_batch_members", "integration_candidate_revisions",
            "integration_candidate_member_results", "integration_repair_operations",
            "integration_repair_stages", "integration_check_evidence", "project_integration_schedules",
            "project_integration_leases", "integration_outbox",
            "integration_outbox_artifact_pins",
        } <= names
    finally:
        await database.close()


@pytest.mark.skipif(not POSTGRES_TEST_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_migration_cycle_from_prior_revision_to_final_and_back():
    """The unpublished final migration supports a real prior-schema round trip."""
    import asyncpg
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from src.database.engine import create_postgres_engine

    prefix, _, database_name = POSTGRES_TEST_DSN.rpartition("/")
    cycle_name = f"{database_name}_integration_cycle_{uuid.uuid4().hex[:8]}"
    cycle_dsn = f"{prefix}/{cycle_name}"
    admin_dsn = POSTGRES_TEST_DSN.replace("postgresql+asyncpg://", "postgresql://")
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{cycle_name}"')
        engine = create_postgres_engine(cycle_dsn, 0, 1)
        try:
            async def migrate(revision: str, *, downgrade: bool = False) -> None:
                config = Config("alembic.ini")
                async with engine.connect() as conn:
                    def run(sync_conn):
                        config.attributes["connection"] = sync_conn
                        (command.downgrade if downgrade else command.upgrade)(config, revision)
                    await conn.run_sync(run)
                    await conn.commit()

            await migrate("e6a1b2c3d4f5")
            await migrate("head")
            await migrate("e6a1b2c3d4f5", downgrade=True)
            await migrate("head")
            async with engine.connect() as conn:
                assert (await conn.execute(text("SELECT to_regclass('integration_candidate_member_results')"))).scalar_one()
                assert (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = 'integration_batches' "
                            "AND column_name = 'request_id'"
                        )
                    )
                ).scalar_one() == 1
        finally:
            await engine.dispose()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{cycle_name}"')
        await admin.close()


def test_sqlite_migration_cycle_from_prior_revision_to_final_and_back(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{tmp_path / 'integration-cycle.db'}")
    config = Config("alembic.ini")
    try:
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "e6a1b2c3d4f5")
            conn.commit()
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            conn.commit()
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "e6a1b2c3d4f5")
            conn.commit()
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            conn.commit()
            assert "integration_candidate_member_results" in inspect(conn).get_table_names()
    finally:
        engine.dispose()


def test_sqlite_task8a_migration_cycle_from_previous_head(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{tmp_path / 'task8a-cycle.db'}")
    config = Config("alembic.ini")

    def migrate(revision: str, *, downgrade: bool = False) -> None:
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            (command.downgrade if downgrade else command.upgrade)(config, revision)
            conn.commit()

    try:
        migrate("c7d8e9f0a1b2")
        with engine.begin() as conn:
            conn.execute(
                integration_batches.insert().values(
                    id="legacy-batch",
                    project_id="legacy-project",
                    repository_id="repo",
                    trigger="manual",
                    source_manifest_digest="manifest",
                    base_sha="a" * 40,
                    lifecycle="sealing",
                    integration_branch="refs/heads/integration/legacy",
                    policy_snapshot={},
                    artifact_snapshot={},
                    cleanup_state="pending",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            from src.database.tables import integration_batch_members

            conn.execute(
                integration_batch_members.insert().values(
                    batch_id="legacy-batch",
                    ordinal=0,
                    task_id="legacy-task",
                    repository_id="repo",
                    source_base_sha="a" * 40,
                    reviewed_head_sha="b" * 40,
                    reviewed_tree_sha="c" * 40,
                    review_evidence={"legacy": True},
                )
            )
        migrate("head")
        columns = {column["name"] for column in inspect(engine).get_columns("integration_batches")}
        member_columns = {
            column["name"]
            for column in inspect(engine).get_columns("integration_batch_members")
        }
        assert "request_id" in columns
        assert "review_evidence_id" in member_columns
        with engine.connect() as conn:
            migrated = conn.execute(
                select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == "legacy-batch"
                )
            ).mappings().one()
            assert migrated["review_evidence_id"] == (
                "task8a-legacy-review:legacy-batch:0"
            )

        migrate("c7d8e9f0a1b2", downgrade=True)
        columns = {column["name"] for column in inspect(engine).get_columns("integration_batches")}
        assert "request_id" not in columns

        migrate("head")
        columns = {column["name"] for column in inspect(engine).get_columns("integration_batches")}
        assert "request_id" in columns
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("base_sha", "integration_branch"),
    (
        (None, "refs/heads/integration/legacy"),
        ("a" * 40, None),
        (None, None),
    ),
    ids=("null-base", "null-branch", "both-null"),
)
def test_sqlite_task8a_upgrade_refuses_legacy_null_batch_identity_before_ddl(
    tmp_path, base_sha, integration_branch
):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(f"sqlite:///{tmp_path / 'task8a-null-identity.db'}")
    config = Config("alembic.ini")

    def migrate(revision: str, *, downgrade: bool = False) -> None:
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            try:
                (command.downgrade if downgrade else command.upgrade)(config, revision)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    try:
        migrate("c7d8e9f0a1b2")
        with engine.begin() as conn:
            conn.execute(
                integration_batches.insert().values(
                    id="legacy-null",
                    project_id="legacy-project",
                    repository_id="repo",
                    trigger="manual",
                    source_manifest_digest="manifest",
                    base_sha=base_sha,
                    lifecycle="sealed",
                    integration_branch=integration_branch,
                    policy_snapshot={},
                    artifact_snapshot={},
                    cleanup_state="pending",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
        with engine.connect() as conn:
            columns_before = tuple(
                column["name"]
                for column in inspect(conn).get_columns("integration_batches")
            )
            guards_before = tuple(
                conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "ORDER BY name"
                    )
                ).scalars()
            )

        with pytest.raises(RuntimeError, match="drain or reconcile"):
            migrate("head")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "c7d8e9f0a1b2"
            )
            assert conn.execute(
                text(
                    "SELECT base_sha, integration_branch FROM integration_batches "
                    "WHERE id = 'legacy-null'"
                )
            ).one() == (base_sha, integration_branch)
            assert tuple(
                column["name"]
                for column in inspect(conn).get_columns("integration_batches")
            ) == columns_before
            assert tuple(
                conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "ORDER BY name"
                    )
                ).scalars()
            ) == guards_before
            assert "request_id" not in columns_before

        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM integration_batches WHERE id = 'legacy-null'")
            )
        migrate("head")
        migrate("c7d8e9f0a1b2", downgrade=True)
        migrate("head")
        assert "request_id" in {
            column["name"]
            for column in inspect(engine).get_columns("integration_batches")
        }
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_TEST_DSN, reason="POSTGRES_TEST_DSN not set")
@pytest.mark.parametrize(
    ("base_sha", "integration_branch"),
    (
        (None, "refs/heads/integration/legacy"),
        ("a" * 40, None),
        (None, None),
    ),
    ids=("null-base", "null-branch", "both-null"),
)
async def test_postgres_task8a_upgrade_refuses_legacy_null_batch_identity_before_ddl(
    base_sha, integration_branch
):
    import asyncpg
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text

    from src.database.engine import create_postgres_engine

    prefix, _, database_name = POSTGRES_TEST_DSN.rpartition("/")
    cycle_name = f"{database_name}_task8a_null_{uuid.uuid4().hex[:8]}"
    cycle_dsn = f"{prefix}/{cycle_name}"
    admin_dsn = POSTGRES_TEST_DSN.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{cycle_name}"')
        engine = create_postgres_engine(cycle_dsn, 0, 1)
        try:
            async def migrate(revision: str, *, downgrade: bool = False) -> None:
                config = Config("alembic.ini")
                async with engine.connect() as conn:
                    def run(sync_conn):
                        config.attributes["connection"] = sync_conn
                        (command.downgrade if downgrade else command.upgrade)(
                            config, revision
                        )

                    try:
                        await conn.run_sync(run)
                    except BaseException:
                        await conn.rollback()
                        raise
                    await conn.commit()

            await migrate("c7d8e9f0a1b2")
            async with engine.begin() as conn:
                await conn.execute(
                    integration_batches.insert().values(
                        id="legacy-null",
                        project_id="legacy-project",
                        repository_id="repo",
                        trigger="manual",
                        source_manifest_digest="manifest",
                        base_sha=base_sha,
                        lifecycle="sealed",
                        integration_branch=integration_branch,
                        policy_snapshot={},
                        artifact_snapshot={},
                        cleanup_state="pending",
                        created_at=1.0,
                        updated_at=1.0,
                    )
                )
            async with engine.connect() as conn:
                columns_before = await conn.run_sync(
                    lambda sync_conn: tuple(
                        column["name"]
                        for column in inspect(sync_conn).get_columns(
                            "integration_batches"
                        )
                    )
                )
                guards_before = tuple(
                    (
                        await conn.execute(
                            text(
                                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                                "ORDER BY tgname"
                            )
                        )
                    ).scalars()
                )

            with pytest.raises(RuntimeError, match="drain or reconcile"):
                await migrate("head")

            async with engine.connect() as conn:
                assert (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one() == "c7d8e9f0a1b2"
                assert (
                    await conn.execute(
                        text(
                            "SELECT base_sha, integration_branch FROM integration_batches "
                            "WHERE id = 'legacy-null'"
                        )
                    )
                ).one() == (base_sha, integration_branch)
                columns_after = await conn.run_sync(
                    lambda sync_conn: tuple(
                        column["name"]
                        for column in inspect(sync_conn).get_columns(
                            "integration_batches"
                        )
                    )
                )
                guards_after = tuple(
                    (
                        await conn.execute(
                            text(
                                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                                "ORDER BY tgname"
                            )
                        )
                    ).scalars()
                )
                assert columns_after == columns_before
                assert guards_after == guards_before
                assert "request_id" not in columns_before

            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM integration_batches WHERE id = 'legacy-null'")
                )
            await migrate("head")
            await migrate("c7d8e9f0a1b2", downgrade=True)
            await migrate("head")
            async with engine.connect() as conn:
                columns = await conn.run_sync(
                    lambda sync_conn: {
                        column["name"]
                        for column in inspect(sync_conn).get_columns(
                            "integration_batches"
                        )
                    }
                )
                assert "request_id" in columns
        finally:
            await engine.dispose()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{cycle_name}"')
        await admin.close()


async def test_read_projections_and_receipts_survive_task_deletion(db):
    from src.database.tables import (
        integration_repair_operations,
        task_delivery_receipts,
        task_integration_checkpoints,
    )

    await db.create_task(Task(id="gone-task", project_id="p", title="gone", description="gone"))
    batch_values = {
        "project_id": "p",
        "repository_id": "repo",
        "request_id": "request-b1",
        "source_manifest_digest": "manifest",
        "base_sha": "a" * 40,
        "lifecycle": "sealed",
        "integration_branch": "refs/heads/integration/b1",
        "current_revision": 0,
        "policy_snapshot": {},
        "artifact_snapshot": {},
        "cleanup_state": "pending",
        "created_at": 2.0,
        "updated_at": 2.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(id="b1", **batch_values))
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="gone-task",
                repository_id="repo",
                branch="parent",
                generation=2,
                checkpoint_sha="a" * 40,
                state="awaiting_children",
                version=3,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op1",
                target_kind="batch",
                batch_id="b1",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot="{}",
                artifact_snapshot="{}",
                required_check_version="checks-v1",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="r1",
                domain_key="delivery:gone-task",
                source_task_id="gone-task",
                repository_id="repo",
                target_branch="parent",
                before_sha="b" * 40,
                after_sha="c" * 40,
                disposition="code",
                created_at=2.0,
            )
        )

    checkpoint = await db.get_integration_checkpoint("gone-task")
    batch = await db.get_integration_batch("b1")
    operation = await db.get_integration_operation("op1")
    assert checkpoint is not None and checkpoint["generation"] == 2
    assert batch is not None and batch["lifecycle"] == "sealed"
    assert operation is not None and operation["required_check_version"] == "checks-v1"

    # Audit rows use soft task references: removal of a task cannot erase delivery proof.
    await db.delete_task("gone-task")
    async with db._engine.connect() as conn:
        assert (await conn.execute(task_delivery_receipts.select())).mappings().one()["id"] == "r1"


def test_integration_value_types_are_immutable_and_validated():
    target = BranchKey(repository_id="repo", branch="parent")
    fence = Fence(target=target, owner_id="owner", token=1)
    checks = RequiredCheckSet(version="v1", names=("unit",), producer_id="github")
    policy = RepairPolicy(debug_intelligence_class="deep-high")

    assert fence.target == target
    assert checks.names == ("unit",)
    assert policy.primary_seconds == 1800
    with pytest.raises(Exception):
        BranchKey(repository_id="repo", branch="parent", extra="forbidden")
    with pytest.raises(Exception):
        RequiredCheckSet(version="v1", names=(), producer_id="github")
