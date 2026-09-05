"""Durable hierarchical-integration persistence contracts."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, inspect, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database import Database
from src.database.tables import integration_batches, integration_promotion_intents
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
    with pytest.raises(IntegrityError):
        async with conn.begin_nested():
            await conn.execute(statement)


async def test_active_batch_is_unique_per_project(db):
    values = {
        "project_id": "p",
        "repository_id": "repo",
        "source_manifest_digest": "manifest",
        "lifecycle": "sealed",
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
            conn, insert(integration_batches).values(id="b2", **values)
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
        "source_manifest_digest": "manifest",
        "lifecycle": "sealing",
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
        "review_evidence": {},
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(id="b1", **batch))
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
        "id": "b1", "project_id": "p", "repository_id": "repo", "source_manifest_digest": "manifest",
        "lifecycle": "sealing", "current_revision": 0, "policy_snapshot": {}, "artifact_snapshot": {},
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
        await conn.execute(insert(integration_batch_members).values(
            batch_id="b1", ordinal=0, task_id="root", repository_id="repo", source_base_sha="a" * 40,
            reviewed_head_sha="b" * 40, reviewed_tree_sha="c" * 40, review_evidence={},
        ))
        await conn.execute(insert(integration_candidate_revisions).values(
            batch_id="b1", revision=0, construction_base_sha="a" * 40, next_member_ordinal=0,
            state="constructing", created_at=1.0, updated_at=1.0,
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
        integration_batch_members, integration_candidate_revisions, integration_repair_operations,
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
            "integration_batch_members", "integration_candidate_revisions", "integration_repair_operations",
            "integration_repair_stages", "integration_check_evidence", "project_integration_schedules",
            "project_integration_leases", "integration_outbox",
        } <= names
    finally:
        await database.close()


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
        "source_manifest_digest": "manifest",
        "lifecycle": "sealed",
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
