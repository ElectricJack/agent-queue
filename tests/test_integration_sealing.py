"""Atomic full-frontier integration-train sealing."""

from __future__ import annotations

import asyncio
import re
import subprocess
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    gates,
    integration_batch_members,
    integration_batches,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    playbook_artifacts,
    project_integration_leases,
    project_integration_schedules,
    task_branch_origins,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_labels,
    tasks,
)
from src.integration.models import (
    ArtifactSnapshot,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.models import Project, RepoConfig, RepoSourceType, TaskStatus
from src.profiles.capabilities import CapabilityPolicy
from tests.pg_dsn import ensure_worker_postgres_dsn


BASE_SHA = "a" * 40
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


def _artifact() -> ArtifactSnapshot:
    return ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "1" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "2" * 64,
        source_digest="sha256:" + "3" * 64,
        compiler_build="task8b-test",
        compiled_at="2026-09-05T00:00:00Z",
        version=1,
    )


def _policy() -> dict:
    boundary = IntegrationBoundaryPolicy(
        required_checks=RequiredCheckSet(
            version="checks-v1", names=("unit",), producer_id="forge"
        ),
        repair=RepairPolicy(
            primary_seconds=30,
            primary_attempts=2,
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
    return HierarchicalIntegrationPolicy(
        parent=boundary,
        root=boundary,
        branchless_parent="verifier",
        on_failed_child="block",
    ).model_dump(mode="json")


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "integration-sealing.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="integration project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            default_branch="main",
        )
    )
    yield database
    await database.close()


@pytest.fixture(params=["sqlite", "postgres"])
async def concurrent_db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "integration-sealing-concurrent.db"))
        await database.initialize()
    await database.create_project(Project(id="p", name="integration project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            default_branch="main",
        )
    )
    yield database
    await database.close()


async def _enable_train(db) -> dict:
    artifact = _artifact()
    policy = _policy()
    await db.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        hierarchical_integration_policy=policy,
        integration_mode="pull_request",
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/task8b-artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    return policy


async def _request(db, *, now: float = 10.0) -> dict:
    from src.integration.scheduler import IntegrationScheduler

    return await IntegrationScheduler(db).mark_due("p", now, "manual")


async def _seed_leaf(db, task_id: str, head: str, **task_overrides) -> dict:
    review = _review_row(task_id, head, evidence_id=f"review-{task_id}")
    async with db.immediate() as conn:
        await conn.execute(insert(tasks).values(**_task_row(task_id, **task_overrides)))
        await conn.execute(insert(task_branch_origins).values(**_origin_row(task_id)))
        await conn.execute(
            insert(task_integration_checkpoints).values(
                **_checkpoint_row(task_id, head)
            )
        )
        await conn.execute(insert(integration_review_evidence).values(**review))
    return review


async def _seed_exact_parent(db) -> None:
    parent_head = "e" * 40
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks),
            [
                _task_row("parent"),
                _task_row(
                    "child",
                    parent_task_id="parent",
                    status=TaskStatus.COMPLETED.value,
                    pr_url=None,
                ),
            ],
        )
        await conn.execute(insert(task_branch_origins).values(**_origin_row("parent")))
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-parent",
                parent_task_id="parent",
                repository_id="repo",
                generation=4,
                pre_collection_checkpoint_sha=BASE_SHA,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-parent",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1, "kind": "parent-proof"},
                artifact_snapshot={"artifact_sha256": "sha256:" + "1" * 64},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-parent",
                operation_id="operation-parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                generation=4,
                head_sha=parent_head,
                required_check_version="checks-v1",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-parent",
                verification_id="verification-parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                completed_at=2.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                **_checkpoint_row(
                    "parent",
                    parent_head,
                    generation=4,
                    verified_sha=parent_head,
                    verified_generation=4,
                    episode_id="episode-parent",
                    current_verification_id="verification-parent",
                    last_completed_operation_id="operation-parent",
                    last_completed_verification_id="verification-parent",
                )
            )
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                **_review_row(
                    "parent",
                    parent_head,
                    evidence_id="review-parent",
                    review_kind="parent",
                    generation=4,
                    evidence={
                        "decision": "approved",
                        "verification_id": "verification-parent",
                    },
                )
            )
        )


def _task_row(task_id: str, **overrides):
    values = {
        "id": task_id,
        "project_id": "p",
        "parent_task_id": None,
        "repo_id": "repo",
        "title": task_id,
        "description": "root delivery",
        "status": TaskStatus.COMPLETED.value,
        "integration_mode": "pull_request",
        "pr_url": f"https://example.test/pull/{task_id}",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    values.update(overrides)
    return values


def _origin_row(task_id: str):
    return {
        "id": f"origin-{task_id}",
        "task_id": task_id,
        "repository_id": "repo",
        "base_sha": BASE_SHA,
        "creation_generation": 0,
        "reserved": True,
        "materialized": False,
        "created_at": 1.0,
        "materialized_at": None,
    }


def _checkpoint_row(task_id: str, head: str, **overrides):
    values = {
        "task_id": task_id,
        "repository_id": "repo",
        "branch": f"aq/{task_id}",
        "generation": 1,
        "checkpoint_sha": head,
        "state": "working",
        "version": 1,
        "updated_at": 1.0,
    }
    values.update(overrides)
    return values


def _review_row(task_id: str, head: str, *, evidence_id: str, **overrides):
    values = {
        "id": evidence_id,
        "source_task_id": task_id,
        "repository_id": "repo",
        "source_base": BASE_SHA,
        "reviewed_head_sha": head,
        "reviewed_tree_sha": f"{int(head, 16) + 10_000:040x}",
        "reviewer_task_id": f"reviewer-{task_id}",
        "reviewer_session_attempt_id": None,
        "review_kind": "leaf",
        "generation": 1,
        "verdict": "approved",
        "evidence": {"decision": "approved", "task_id": task_id, "head": head},
        "created_at": 1.0,
    }
    values.update(overrides)
    return values


async def test_keyset_pages_full_frontier_and_bulk_review_selects_latest_exact(db):
    leaf_ids = [f"root-{index:03d}" for index in range(205)]
    heads = {task_id: f"{index + 1:040x}" for index, task_id in enumerate(leaf_ids)}
    async with db.immediate() as conn:
        await conn.execute(insert(tasks), [_task_row(task_id) for task_id in leaf_ids])
        await conn.execute(
            insert(task_branch_origins), [_origin_row(task_id) for task_id in leaf_ids]
        )
        await conn.execute(
            insert(task_integration_checkpoints),
            [_checkpoint_row(task_id, heads[task_id]) for task_id in leaf_ids],
        )
        reviews = [
            _review_row(task_id, heads[task_id], evidence_id=f"review-{task_id}")
            for task_id in leaf_ids
        ]
        reviews.extend(
            [
                _review_row(
                    "root-050",
                    heads["root-050"],
                    evidence_id="review-root-050-earlier",
                    created_at=2.0,
                ),
                _review_row(
                    "root-050",
                    heads["root-050"],
                    evidence_id="review-root-050-latest-rejection",
                    verdict="rejected",
                    evidence={"decision": "rejected", "reason": "latest wins"},
                    created_at=3.0,
                ),
            ]
        )
        await conn.execute(insert(integration_review_evidence), reviews)

        scanned = []
        reviews_by_key = {}
        after = None
        while True:
            page = await db.eligible_root_page_on(
                conn,
                project_id="p",
                repository_id="repo",
                after=after,
                limit=17,
            )
            if not page:
                break
            scanned.extend(page)
            reviews_by_key.update(await db.latest_exact_reviews_on(conn, page))
            after = (page[-1]["task_id"], page[-1]["source_head"])

    assert [(row["task_id"], row["source_head"]) for row in scanned] == [
        (task_id, heads[task_id]) for task_id in leaf_ids
    ]
    assert len({row["task_id"] for row in scanned}) == 205
    assert {row["source_kind"] for row in scanned} == {"leaf"}
    rejected_key = ("root-050", "repo", BASE_SHA, heads["root-050"], 1)
    assert reviews_by_key[rejected_key]["id"] == "review-root-050-latest-rejection"
    assert reviews_by_key[rejected_key]["verdict"] == "rejected"
    assert reviews_by_key[rejected_key]["evidence"] == {
        "decision": "rejected",
        "reason": "latest wins",
    }


async def test_root_projection_requires_leaf_or_exact_current_parent_identity(db):
    parent_head = "e" * 40
    leaf_head = "f" * 40
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks),
            [
                _task_row("leaf"),
                _task_row("parent"),
                _task_row(
                    "parent-child",
                    parent_task_id="parent",
                    status=TaskStatus.COMPLETED.value,
                    pr_url=None,
                ),
            ],
        )
        await conn.execute(
            insert(task_branch_origins), [_origin_row("leaf"), _origin_row("parent")]
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-parent",
                parent_task_id="parent",
                repository_id="repo",
                generation=4,
                pre_collection_checkpoint_sha=BASE_SHA,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-parent",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1, "kind": "parent-proof"},
                artifact_snapshot={"artifact_sha256": "sha256:" + "1" * 64},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-parent",
                operation_id="operation-parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                generation=4,
                head_sha=parent_head,
                required_check_version="checks-v1",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-parent",
                verification_id="verification-parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                completed_at=2.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                **_checkpoint_row("leaf", leaf_head)
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                **_checkpoint_row(
                    "parent",
                    parent_head,
                    generation=4,
                    verified_sha=parent_head,
                    verified_generation=4,
                    episode_id="episode-parent",
                    current_verification_id="verification-parent",
                    last_completed_operation_id="operation-parent",
                    last_completed_verification_id="verification-parent",
                )
            )
        )
        await conn.execute(
            insert(integration_review_evidence),
            [
                _review_row("leaf", leaf_head, evidence_id="review-leaf"),
                _review_row(
                    "parent",
                    parent_head,
                    evidence_id="review-parent",
                    review_kind="parent",
                    generation=4,
                    evidence={
                        "decision": "approved",
                        "verification_id": "verification-parent",
                    },
                ),
            ],
        )

        page = await db.eligible_root_page_on(
            conn,
            project_id="p",
            repository_id="repo",
            after=None,
            limit=10,
        )
        reviews = await db.latest_exact_reviews_on(conn, page)

    assert [(row["task_id"], row["source_kind"]) for row in page] == [
        ("leaf", "leaf"),
        ("parent", "parent"),
    ]
    parent = page[1]
    assert parent["source_base"] == BASE_SHA
    assert parent["source_head"] == parent_head
    assert parent["generation"] == 4
    assert parent["current_verification_id"] == "verification-parent"
    parent_key = ("parent", "repo", BASE_SHA, parent_head, 4)
    assert reviews[parent_key]["id"] == "review-parent"
    assert reviews[parent_key]["evidence"]["verification_id"] == "verification-parent"


async def test_root_projection_excludes_each_common_near_miss(db):
    task_ids = (
        "good",
        "nested",
        "not-completed",
        "wrong-repository",
        "missing-checkpoint",
        "retired-origin",
        "held",
        "gated",
        "already-batched",
        "already-delivered",
        "blank-pr",
    )
    for index, task_id in enumerate(task_ids):
        await _seed_leaf(db, task_id, f"{index + 1:040x}")
    await db.create_repo(
        RepoConfig(
            id="other-repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            default_branch="main",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == "nested")
            .values(parent_task_id="not-completed")
        )
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == "not-completed")
            .values(status=TaskStatus.READY.value)
        )
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == "wrong-repository")
            .values(repo_id="other-repo")
        )
        await conn.execute(
            delete(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "missing-checkpoint"
            )
        )
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.task_id == "retired-origin")
            .values(retired_at=2.0)
        )
        await conn.execute(insert(task_labels).values(task_id="held", label="hold:operator"))
        await conn.execute(
            insert(gates).values(
                id="gate-open",
                project_id="p",
                gate_type="human",
                title="blocking gate",
                question="continue?",
                status="open",
                created_at=1.0,
            )
        )
        await conn.execute(insert(task_gates).values(task_id="gated", gate_id="gate-open"))
        await conn.execute(
            insert(integration_batches).values(
                id="prior-batch",
                project_id="p",
                repository_id="repo",
                request_id="prior-request",
                trigger="manual",
                source_manifest_digest="sha256:" + "4" * 64,
                base_sha=BASE_SHA,
                lifecycle="sealing",
                integration_branch="refs/heads/aq/integration/p/prior",
                policy_snapshot={"version": 1},
                artifact_snapshot={"artifact_sha256": "sha256:" + "1" * 64},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        batched_review = (
            await conn.execute(
                select(integration_review_evidence).where(
                    integration_review_evidence.c.source_task_id == "already-batched"
                )
            )
        ).mappings().one()
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="prior-batch",
                ordinal=0,
                task_id="already-batched",
                pr_url="https://example.test/pull/already-batched",
                repository_id="repo",
                source_base_sha=BASE_SHA,
                reviewed_head_sha=batched_review["reviewed_head_sha"],
                reviewed_tree_sha=batched_review["reviewed_tree_sha"],
                review_evidence_id=batched_review["id"],
                review_evidence=dict(batched_review),
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "prior-batch")
            .values(lifecycle="sealed")
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="root-receipt",
                domain_key="root-receipt-domain",
                source_task_id="already-delivered",
                target_task_id=None,
                repository_id="repo",
                target_branch="main",
                disposition="code",
                created_at=2.0,
            )
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "blank-pr").values(pr_url="   ")
        )

        page = await db.eligible_root_page_on(
            conn,
            project_id="p",
            repository_id="repo",
            after=None,
            limit=100,
        )

    assert [row["task_id"] for row in page] == ["good"]


async def test_zero_root_seal_is_terminal_resource_free_and_request_replay(db):
    from src.integration.scheduler import TrainService

    policy = await _enable_train(db)
    request = await _request(db)
    service = TrainService(db, page_size=3)

    first = await service.seal("p", request["request_id"], 20.0)
    replay = await service.seal("p", request["request_id"], 30.0)

    assert first == replay
    assert first["outcome"] == "empty"
    assert first["batch_id"]
    assert first["operation_id"] is None
    async with db._engine.connect() as conn:
        batch = (
            await conn.execute(
                select(integration_batches).where(
                    integration_batches.c.id == first["batch_id"]
                )
            )
        ).mappings().one()
        schedule = (
            await conn.execute(
                select(project_integration_schedules).where(
                    project_integration_schedules.c.project_id == "p"
                )
            )
        ).mappings().one()
        assert (
            await conn.execute(
                select(project_integration_leases).where(
                    project_integration_leases.c.project_id == "p"
                )
            )
        ).all() == []
        assert (
            await conn.execute(
                select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == first["batch_id"]
                )
            )
        ).all() == []
        assert (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.batch_id == first["batch_id"]
                )
            )
        ).all() == []
        events = (await conn.execute(select(integration_outbox))).mappings().all()

    assert batch["lifecycle"] == "empty"
    assert batch["base_sha"] is None
    assert batch["integration_branch"] is None
    assert batch["policy_snapshot"] == policy
    assert batch["artifact_snapshot"] == policy["root"]["route"]["artifact"]
    assert schedule["outstanding_request_id"] is None
    assert schedule["last_completed_sweep_at"] == 20.0
    assert [event["event_type"] for event in events] == ["integration.sweep_due"]


async def test_one_root_seal_freezes_review_and_real_unstarted_operation(db):
    from src.integration.scheduler import TrainService

    policy = await _enable_train(db)
    review = await _seed_leaf(db, "root", "b" * 40)
    request = await _request(db)
    service = TrainService(db, page_size=1)

    first = await service.seal("p", request["request_id"], 20.0)
    replay = await service.seal("p", request["request_id"], 30.0)

    assert first == replay
    assert first["outcome"] == "sealed"
    assert first["batch_id"] != first["operation_id"]
    async with db._engine.connect() as conn:
        batch = (
            await conn.execute(
                select(integration_batches).where(
                    integration_batches.c.id == first["batch_id"]
                )
            )
        ).mappings().one()
        member = (
            await conn.execute(
                select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == first["batch_id"]
                )
            )
        ).mappings().one()
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.id == first["operation_id"]
                )
            )
        ).mappings().one()
        stages = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == first["operation_id"]
                )
            )
        ).all()
        pins = (
            await conn.execute(
                select(integration_operation_artifact_pins).where(
                    integration_operation_artifact_pins.c.operation_id
                    == first["operation_id"]
                )
            )
        ).mappings().all()
        sealed_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "integration.sealed"
                )
            )
        ).mappings().all()

    assert batch["lifecycle"] == "sealed"
    assert batch["integration_branch"].startswith("refs/heads/aq/integration/")
    assert batch["policy_snapshot"] == policy
    assert member["task_id"] == "root"
    assert member["source_ref"] == "refs/heads/aq/root"
    assert member["source_ref_retention"] == "delete"
    assert member["review_evidence_id"] == review["id"]
    assert member["review_evidence"] == review
    assert operation["target_kind"] == "batch"
    assert operation["batch_id"] == first["batch_id"]
    assert operation["artifact_snapshot"] == policy["root"]["route"]["artifact"]
    assert stages == []
    assert [pin["artifact_sha256"] for pin in pins] == [
        policy["root"]["route"]["artifact"]["artifact_sha256"]
    ]
    assert len(sealed_events) == 1
    assert sealed_events[0]["payload"] == {
        "project_id": "p",
        "batch_id": first["batch_id"],
        "operation_id": first["operation_id"],
        "event_id": f"integration-sealed:{first['batch_id']}",
    }


async def test_seal_freezes_source_ref_and_retention_from_authoritative_checkpoint(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    policy = _policy()
    policy["cleanup"] = {
        "successful_source_refs": "retain",
        "failed_work_retention_seconds": 1234,
    }
    await db.update_project("p", hierarchical_integration_policy=policy)
    await _seed_leaf(db, "root", "b" * 40)
    request = await _request(db)

    sealed = await TrainService(db).seal("p", request["request_id"], 20.0)
    await db.update_task("root", branch_name="mutated-after-seal")

    async with db._engine.connect() as conn:
        batch = (
            await conn.execute(
                select(integration_batches).where(integration_batches.c.id == sealed["batch_id"])
            )
        ).mappings().one()
        member = (
            await conn.execute(
                select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == sealed["batch_id"]
                )
            )
        ).mappings().one()

    assert member["source_ref"] == "refs/heads/aq/root"
    assert member["source_ref_retention"] == "retain"
    assert batch["policy_snapshot"]["cleanup"]["failed_work_retention_seconds"] == 1234


async def test_nonempty_seal_retains_first_request_for_manual_and_periodic_coalescing(db):
    from src.integration.scheduler import IntegrationScheduler, TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(
        project_id="p", now=1.0, enabled=True, interval_seconds=5
    )
    first = await scheduler.mark_due("p", 10.0, "manual")
    sealed = await TrainService(db).seal("p", first["request_id"], 20.0)

    manual = await scheduler.mark_due("p", 30.0, "manual")
    periodic = await scheduler.mark_due("p", 40.0, "periodic")

    assert sealed["outcome"] == "sealed"
    for replay in (manual, periodic):
        assert replay["outcome"] == "coalesced"
        assert replay["request_id"] == first["request_id"]
        assert replay["trigger"] == "manual"
        assert replay["requested_at"] == 10.0
        assert replay["request_sequence"] == 1
    async with db._engine.connect() as conn:
        schedule = (
            await conn.execute(
                select(project_integration_schedules).where(
                    project_integration_schedules.c.project_id == "p"
                )
            )
        ).mappings().one()
        sweep_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "integration.sweep_due"
                )
            )
        ).mappings().all()
    assert schedule["outstanding_request_id"] == first["request_id"]
    assert schedule["outstanding_trigger"] == "manual"
    assert schedule["outstanding_requested_at"] == 10.0
    assert schedule["request_sequence"] == 1
    assert len(sweep_events) == 1


def test_integration_branch_is_ref_safe_for_adversarial_project_and_request_ids():
    from src.git.manager import _validate_ref
    from src.integration.scheduler import TrainService

    inputs = (
        ("../project:^~ with space.lock", "request@{bad}..\\value"),
        (".leading", "trailing."),
    )
    refs = [TrainService._integration_branch(project, request) for project, request in inputs]

    assert refs[0] != refs[1]
    for ref in refs:
        assert re.fullmatch(
            r"refs/heads/aq/integration/p-[0-9a-f]{32}/r-[0-9a-f]{32}", ref
        )
        assert _validate_ref(ref) == ref
        checked = subprocess.run(
            ["git", "check-ref-format", ref],
            check=False,
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stderr


async def test_live_batch_is_busy_without_frontier_read_and_expired_batch_resumes(
    db, monkeypatch
):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    first_request = await _request(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="existing-batch",
                project_id="p",
                repository_id="repo",
                request_id=first_request["request_id"],
                trigger="manual",
                source_manifest_digest="sha256:" + "4" * 64,
                base_sha=BASE_SHA,
                lifecycle="sealing",
                current_revision=0,
                integration_branch="refs/heads/aq/integration/p/existing",
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                cleanup_state="pending",
                created_at=10.0,
                updated_at=10.0,
            )
        )
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="p",
                repository_id="repo",
                batch_id="existing-batch",
                owner_id="sealer-existing-batch",
                fence_token=1,
                heartbeat_at=10.0,
                expires_at=40.0,
            )
        )

    async def forbidden_frontier(*_args, **_kwargs):
        raise AssertionError("busy sealing inspected the source frontier")

    monkeypatch.setattr(db, "eligible_root_page_on", forbidden_frontier)
    busy = await TrainService(db).seal("p", first_request["request_id"], 20.0)
    assert busy == {
        "outcome": "busy",
        "project_id": "p",
        "request_id": first_request["request_id"],
        "batch_id": "existing-batch",
        "operation_id": None,
    }

    monkeypatch.undo()
    resumed = await TrainService(db, page_size=1).seal(
        "p", first_request["request_id"], 50.0
    )
    assert resumed["outcome"] == "sealed"
    assert resumed["batch_id"] == "existing-batch"
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(select(integration_batches.c.id))
        ).scalars().all() == ["existing-batch"]


async def test_seal_exhausts_small_pages_past_200_and_advances_over_rejections(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    leaf_ids = [f"root-{index:03d}" for index in range(205)]
    heads = {task_id: f"{index + 1:040x}" for index, task_id in enumerate(leaf_ids)}
    task_rows = [_task_row(task_id) for task_id in leaf_ids]
    task_rows[50]["integration_mode"] = "direct"
    task_rows[150]["integration_mode"] = "corrupt-value"
    review_rows = [
        _review_row(task_id, heads[task_id], evidence_id=f"review-{task_id}")
        for task_id in leaf_ids
    ]
    review_rows.append(
        _review_row(
            "root-100",
            heads["root-100"],
            evidence_id="review-root-100-rejected-latest",
            verdict="rejected",
            evidence={"decision": "rejected", "reason": "newest exact review"},
            created_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(insert(tasks), task_rows)
        await conn.execute(
            insert(task_branch_origins), [_origin_row(task_id) for task_id in leaf_ids]
        )
        await conn.execute(
            insert(task_integration_checkpoints),
            [_checkpoint_row(task_id, heads[task_id]) for task_id in leaf_ids],
        )
        await conn.execute(insert(integration_review_evidence), review_rows)
    request = await _request(db)

    result = await TrainService(db, page_size=7).seal(
        "p", request["request_id"], 20.0
    )

    async with db._engine.connect() as conn:
        members = (
            await conn.execute(
                select(integration_batch_members)
                .where(integration_batch_members.c.batch_id == result["batch_id"])
                .order_by(integration_batch_members.c.ordinal)
            )
        ).mappings().all()
    expected = [
        task_id for task_id in leaf_ids if task_id not in {"root-050", "root-100"}
    ]
    assert result["outcome"] == "sealed"
    assert [member["task_id"] for member in members] == expected
    assert [member["ordinal"] for member in members] == list(range(203))
    assert "root-150" in expected


async def test_failure_after_first_member_insert_rolls_back_every_sealing_write(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root-a", "b" * 40)
    await _seed_leaf(db, "root-b", "c" * 40)
    request = await _request(db)
    async with db.immediate() as conn:
        await conn.execute(
            text(
                "CREATE TRIGGER task8b_fail_second_member BEFORE INSERT ON "
                "integration_batch_members WHEN NEW.ordinal = 1 BEGIN "
                "SELECT RAISE(ABORT, 'injected second member failure'); END"
            )
        )

    with pytest.raises((IntegrityError, DBAPIError), match="injected second member failure"):
        await TrainService(db, page_size=1).seal("p", request["request_id"], 20.0)

    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches))).all() == []
        assert (await conn.execute(select(integration_batch_members))).all() == []
        assert (await conn.execute(select(project_integration_leases))).all() == []
        assert (await conn.execute(select(integration_repair_operations))).all() == []
        assert (
            await conn.execute(
                select(integration_outbox.c.event_type).order_by(integration_outbox.c.id)
            )
        ).scalars().all() == ["integration.sweep_due"]
        schedule = (
            await conn.execute(
                select(project_integration_schedules).where(
                    project_integration_schedules.c.project_id == "p"
                )
            )
        ).mappings().one()
        assert schedule["outstanding_request_id"] == request["request_id"]

    async with db.immediate() as conn:
        await conn.execute(text("DROP TRIGGER task8b_fail_second_member"))
    replay = await TrainService(db, page_size=1).seal(
        "p", request["request_id"], 30.0
    )
    assert replay["outcome"] == "sealed"
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_batches))).all()) == 1
        assert len((await conn.execute(select(integration_batch_members))).all()) == 2
        assert len((await conn.execute(select(integration_repair_operations))).all()) == 1
        assert (
            await conn.execute(
                select(integration_outbox.c.event_type).where(
                    integration_outbox.c.event_type == "integration.sealed"
                )
            )
        ).scalars().all() == ["integration.sealed"]


async def test_sealed_member_review_snapshot_is_immutable(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    request = await _request(db)
    sealed = await TrainService(db).seal("p", request["request_id"], 20.0)

    with pytest.raises((IntegrityError, DBAPIError), match="membership is immutable"):
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_batch_members)
                .where(integration_batch_members.c.batch_id == sealed["batch_id"])
                .values(review_evidence={"changed": True})
            )


async def test_integration_seal_command_is_typed_and_project_scoped(
    command_handler_factory,
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="integration project"))
    service = AsyncMock()
    service.seal.return_value = {
        "outcome": "sealed",
        "project_id": "p",
        "request_id": "integration-sweep:p:1",
        "batch_id": "batch-1",
        "operation_id": "repair-batch-batch-1",
    }
    handler.orchestrator.integration_train_service = service
    args = {
        "project_id": "p",
        "request_id": "integration-sweep:p:1",
        "now": 20.0,
    }

    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_seal"]),
        project_id="p",
        session_id="session",
    )
    with principal_context(session):
        denied_session = await handler.execute("integration_seal", args)
    assert denied_session["outcome"] == "unauthorized"

    wrong_project = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_seal"]),
        project_id="other",
    )
    with principal_context(wrong_project):
        denied_project = await handler.execute("integration_seal", args)
    assert denied_project["outcome"] == "unauthorized"

    capable = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_seal"]),
        project_id="p",
    )
    with principal_context(capable):
        allowed = await handler.execute("integration_seal", args)
    assert allowed == {"success": True, **service.seal.return_value}
    service.seal.assert_awaited_once_with("p", "integration-sweep:p:1", 20.0)
    await handler.db.close()


async def test_seal_lock_excludes_descendant_reopen_until_frozen(
    concurrent_db, monkeypatch
):
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    await _seed_exact_parent(concurrent_db)
    request = await _request(concurrent_db)
    first_page_scanned = asyncio.Event()
    finish_snapshot = asyncio.Event()
    original_page = concurrent_db.eligible_root_page_on
    calls = 0

    async def pause_after_first_page(*args, **kwargs):
        nonlocal calls
        page = await original_page(*args, **kwargs)
        calls += 1
        if calls == 1:
            first_page_scanned.set()
            await finish_snapshot.wait()
        return page

    monkeypatch.setattr(concurrent_db, "eligible_root_page_on", pause_after_first_page)
    seal_task = asyncio.create_task(
        TrainService(concurrent_db, page_size=1).seal(
            "p", request["request_id"], 20.0
        )
    )
    await asyncio.wait_for(first_page_scanned.wait(), timeout=3)
    reopen_task = asyncio.create_task(
        concurrent_db.transition_task("child", TaskStatus.READY)
    )
    await asyncio.sleep(0.05)
    assert not reopen_task.done()
    finish_snapshot.set()

    sealed = await asyncio.wait_for(seal_task, timeout=5)
    assert sealed["outcome"] == "sealed"
    with pytest.raises(HierarchyError) as rejected:
        await asyncio.wait_for(reopen_task, timeout=5)
    assert rejected.value.code == "sealed"
    assert (await concurrent_db.get_task("child")).status is TaskStatus.COMPLETED


async def test_descendant_reopen_before_seal_changes_fresh_snapshot(concurrent_db):
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    await _seed_exact_parent(concurrent_db)
    await concurrent_db.transition_task("child", TaskStatus.READY)
    request = await _request(concurrent_db)

    result = await TrainService(concurrent_db, page_size=1).seal(
        "p", request["request_id"], 20.0
    )

    assert result["outcome"] == "empty"
    assert (await concurrent_db.get_task("child")).status is TaskStatus.READY


async def test_public_review_append_requires_existing_source_task_project(db):
    missing = _review_row("missing", "b" * 40, evidence_id="review-missing")

    with pytest.raises(ValueError, match="source task project does not exist"):
        await db.append_integration_review_evidence(missing)


async def test_postgres_sealer_first_freezes_approval_before_later_rejection(
    concurrent_db, monkeypatch
):
    if concurrent_db._engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock ordering only")
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    approval = await _seed_leaf(concurrent_db, "root", "b" * 40)
    request = await _request(concurrent_db)
    review_read = asyncio.Event()
    finish_seal = asyncio.Event()
    original_reviews = concurrent_db.latest_exact_reviews_on

    async def pause_after_review_read(*args, **kwargs):
        reviews = await original_reviews(*args, **kwargs)
        review_read.set()
        await finish_seal.wait()
        return reviews

    monkeypatch.setattr(
        concurrent_db, "latest_exact_reviews_on", pause_after_review_read
    )
    seal_task = asyncio.create_task(
        TrainService(concurrent_db).seal("p", request["request_id"], 20.0)
    )
    await asyncio.wait_for(review_read.wait(), timeout=3)
    rejection = _review_row(
        "root",
        "b" * 40,
        evidence_id="review-root-later-rejection",
        verdict="rejected",
        evidence={"decision": "rejected"},
        created_at=2.0,
    )
    append_task = asyncio.create_task(
        concurrent_db.append_integration_review_evidence(rejection)
    )
    await asyncio.sleep(0.05)
    assert not append_task.done()
    finish_seal.set()

    sealed = await asyncio.wait_for(seal_task, timeout=5)
    await asyncio.wait_for(append_task, timeout=5)
    async with concurrent_db._engine.connect() as conn:
        member = (
            await conn.execute(
                select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == sealed["batch_id"]
                )
            )
        ).mappings().one()
    assert sealed["outcome"] == "sealed"
    assert member["review_evidence_id"] == approval["id"]


async def test_postgres_rejection_writer_first_makes_seal_exclude_root(
    concurrent_db, monkeypatch
):
    if concurrent_db._engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock ordering only")
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    await _seed_leaf(concurrent_db, "root", "b" * 40)
    request = await _request(concurrent_db)
    writer_has_lock = asyncio.Event()
    finish_writer = asyncio.Event()
    original_lock = concurrent_db.lock_hierarchy_project

    async def pause_review_writer(conn, project_id):
        await original_lock(conn, project_id)
        if asyncio.current_task().get_name() == "task8b-review-writer":
            writer_has_lock.set()
            await finish_writer.wait()

    monkeypatch.setattr(concurrent_db, "lock_hierarchy_project", pause_review_writer)
    rejection = _review_row(
        "root",
        "b" * 40,
        evidence_id="review-root-first-rejection",
        verdict="rejected",
        evidence={"decision": "rejected"},
        created_at=2.0,
    )
    append_task = asyncio.create_task(
        concurrent_db.append_integration_review_evidence(rejection),
        name="task8b-review-writer",
    )
    await asyncio.wait_for(writer_has_lock.wait(), timeout=3)
    seal_task = asyncio.create_task(
        TrainService(concurrent_db).seal("p", request["request_id"], 20.0)
    )
    await asyncio.sleep(0.05)
    assert not seal_task.done()
    finish_writer.set()

    await asyncio.wait_for(append_task, timeout=5)
    sealed = await asyncio.wait_for(seal_task, timeout=5)
    assert sealed["outcome"] == "empty"


async def test_immediate_transition_preserves_ordinary_status_and_post_commit_callback(db):
    ready_events = []

    async def on_ready(entries):
        ready_events.extend(entries)

    db.set_ready_listener(on_ready)
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                **_task_row(
                    "ordinary",
                    status=TaskStatus.DEFINED.value,
                    integration_mode=None,
                    pr_url=None,
                )
            )
        )

    await db.transition_task("ordinary", TaskStatus.READY, context="ordinary-test")

    assert (await db.get_task("ordinary")).status is TaskStatus.READY
    assert ready_events == [("ordinary", "promoted")]
