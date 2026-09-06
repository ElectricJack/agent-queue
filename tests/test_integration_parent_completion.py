"""Receipt-driven parent collection and guarded completion."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database import Database
from src.database.tables import (
    integration_check_evidence,
    integration_episode_receipt_acceptances,
    integration_branch_owners,
    integration_batch_members,
    integration_batches,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_parent_operation_completions,
    integration_parent_verification_evidence,
    integration_parent_episodes,
    integration_parent_verifications,
    integration_review_evidence,
    integration_repair_operations,
    integration_repair_stages,
    playbook_artifacts,
    task_delivery_receipts,
    task_integration_checkpoints,
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
from src.integration.hierarchy import HierarchyIntegration
from src.integration.status import IntegrationStatusService
from src.database.queries.hierarchy_queries import HierarchyError
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, Task, TaskStatus
from src.database.queries.task_queries import StaleClaim


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "parent-completion.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="integration project"))
    yield database
    await database.close()


async def _enable_project(db, *, on_failed_child: str = "block") -> dict:
    artifact = _artifact()
    policy = HierarchicalIntegrationPolicy(
        parent=_boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child=on_failed_child,
    ).model_dump(mode="json")
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
    )
    await db.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
        hierarchical_integration_policy=policy,
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    return policy


async def _seed_parent_identity(db, *, generation: int = 0) -> None:
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
    )
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=generation,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )


async def _parent_tree(db, *, children: int = 2, on_failed_child: str = "block"):
    await _enable_project(db, on_failed_child=on_failed_child)
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    hierarchy = HierarchyIntegration(
        db,
        default_head_resolver=lambda _repo, _branch: "a" * 40,
        checkpoint_verifier=lambda _task, _repo, head: head,
    )
    filed = await hierarchy.file_children(
        "parent", [{"title": f"child {index}"} for index in range(children)], 0
    )
    checkpointed = await hierarchy.checkpoint_parent("parent", "a" * 40, 1)
    child_ids = [row["task_id"] for row in filed["children"]]
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id.in_(child_ids)).values(status="COMPLETED")
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id.in_(child_ids))
            .values(checkpoint_sha="b" * 40)
        )
    return hierarchy, checkpointed, child_ids


async def _code_receipt(
    db, child_id: str, before_sha: str, after_sha: str, **overrides
) -> None:
    async with db.immediate() as conn:
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == "parent"
                )
            )
        ).mappings().one()
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.parent_task_id == "parent",
                    integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                )
            )
        ).mappings().one()
        values = {
            "id": f"receipt-{child_id}",
            "domain_key": f"delivery-{child_id}",
            "source_task_id": child_id,
            "target_task_id": "parent",
            "repository_id": "repo",
            "target_branch": "aq/parent",
            "reviewed_head_sha": "b" * 40,
            "reviewed_tree_sha": "c" * 40,
            "before_sha": before_sha,
            "squash_sha": after_sha,
            "after_sha": after_sha,
            "review_evidence": {"id": f"review-{child_id}"},
            "parent_operation_id": operation["id"],
            "parent_episode_id": checkpoint["episode_id"],
            "disposition": "code",
            "created_at": float(int(child_id.rsplit(".", 1)[1])),
        }
        values.update(overrides)
        await conn.execute(insert(task_delivery_receipts).values(**values))


def _artifact() -> ArtifactSnapshot:
    return ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
        compiled_at="2026-09-05T00:00:00Z",
        version=4,
    )


def _boundary() -> IntegrationBoundaryPolicy:
    return IntegrationBoundaryPolicy(
        required_checks=RequiredCheckSet(
            version="parent-v1", names=("unit",), producer_id="forge-observer"
        ),
        repair=RepairPolicy(debug_intelligence_class="deep-high"),
        route=PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            activation_id="activation-audit-only",
            artifact=_artifact(),
        ),
        primary_intelligence_class="medium",
        primary_profile_id="integrator",
        verifier_intelligence_class="high",
        verifier_profile_id="verifier",
    )


def test_hierarchical_policy_freezes_full_parent_and_root_inputs():
    policy = HierarchicalIntegrationPolicy(
        version=1,
        parent=_boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child="block",
    )

    dumped = policy.model_dump(mode="json")
    assert dumped["parent"]["route"]["artifact"] == _artifact().model_dump(mode="json")
    assert dumped["parent"]["route"]["playbook_id"] == "hierarchical-delivery"
    assert dumped["parent"]["route"]["scope"] == "project"
    assert dumped["parent"]["route"]["scope_identifier"] == "p"
    with pytest.raises(Exception):
        policy.parent.required_checks.names = ("changed",)


@pytest.mark.parametrize("field,value", [("branchless_parent", "guess"), ("on_failed_child", "ignore")])
def test_hierarchical_policy_rejects_unruled_choices(field, value):
    values = {
        "version": 1,
        "parent": _boundary(),
        "root": _boundary(),
        "branchless_parent": "verifier",
        "on_failed_child": "block",
    }
    values[field] = value
    with pytest.raises(Exception):
        HierarchicalIntegrationPolicy(**values)


async def test_project_policy_round_trips_as_nullable_json(db):
    assert (await db.get_project("p")).hierarchical_integration_policy is None
    policy = HierarchicalIntegrationPolicy(
        parent=_boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child="block",
    ).model_dump(mode="json")
    await db.update_project("p", hierarchical_integration_policy=policy)
    assert (await db.get_project("p")).hierarchical_integration_policy == policy


async def test_parent_episode_operation_identity_survives_completion(db):
    await _seed_parent_identity(db)
    values = {
        "target_kind": "parent",
        "parent_task_id": "parent",
        "episode_id": "episode",
        "active_stage": 0,
        "state": "active",
        "policy_snapshot": {},
        "artifact_snapshot": {},
        "required_check_version": "checks-v1",
        "route_playbook_id": "hierarchical-delivery",
        "route_scope": "project",
        "route_scope_identifier": "p",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_repair_operations).values(id="op-1", **values))
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "op-1")
            .values(state="completed")
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(integration_repair_operations).values(id="op-2", **values)
                )


async def test_parent_identity_foreign_keys_reject_mismatched_episode_links(db):
    await _seed_parent_identity(db)
    async with db.immediate() as conn:
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(integration_repair_operations).values(
                        id="wrong-operation",
                        target_kind="parent",
                        parent_task_id="parent",
                        episode_id="missing-episode",
                        active_stage=0,
                        state="active",
                        policy_snapshot={},
                        artifact_snapshot={},
                        required_check_version="test",
                        created_at=1.0,
                        updated_at=1.0,
                    )
                )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(task_integration_checkpoints).values(
                        task_id="parent",
                        repository_id="repo",
                        branch="aq/parent",
                        generation=0,
                        episode_id="missing-episode",
                        state="working",
                        version=0,
                        updated_at=1.0,
                    )
                )


async def test_check_evidence_and_verification_links_are_append_only(db):
    await _seed_parent_identity(db, generation=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                generation=1,
                state="verifying",
                version=0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                route_playbook_id="hierarchical-delivery",
                route_scope="project",
                route_scope_identifier="p",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="evidence",
                operation_id="op",
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="a" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification",
                operation_id="op",
                parent_task_id="parent",
                episode_id="episode",
                generation=1,
                head_sha="a" * 40,
                required_check_version="checks-v1",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verification_evidence).values(
                verification_id="verification", evidence_id="evidence"
            )
        )
        for statement in (
            update(integration_check_evidence)
            .where(integration_check_evidence.c.id == "evidence")
            .values(conclusion="failure"),
            delete(integration_check_evidence).where(
                integration_check_evidence.c.id == "evidence"
            ),
            update(integration_parent_verification_evidence)
            .where(
                integration_parent_verification_evidence.c.verification_id
                == "verification"
            )
            .values(evidence_id="changed"),
        ):
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(statement)


async def test_parent_operation_artifact_pin_prevents_collection(db):
    await _seed_parent_identity(db)
    artifact = _artifact()
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
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
            insert(integration_repair_operations).values(
                id="op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot=artifact.model_dump(mode="json"),
                required_check_version="checks-v1",
                route_playbook_id="hierarchical-delivery",
                route_scope="project",
                route_scope_identifier="p",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_operation_artifact_pins).values(
                operation_id="op", artifact_sha256=artifact.artifact_sha256
            )
        )

    assert await db.collect_playbook_artifacts(before=2.0, min_versions=0) == []
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(playbook_artifacts.c.artifact_sha256).where(
                    playbook_artifacts.c.artifact_sha256 == artifact.artifact_sha256
                )
            )
        ).scalar_one() == artifact.artifact_sha256


async def test_first_parent_checkpoint_reserves_one_frozen_episode_operation(db):
    policy = await _enable_project(db)
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            parent_task_id=None,
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                generation=2,
                checkpoint_sha="a" * 40,
                state="working",
                version=0,
                updated_at=1.0,
            )
        )
    hierarchy = HierarchyIntegration(
        db, checkpoint_verifier=lambda _task, _repo, head: head
    )

    with pytest.raises(StaleClaim):
        await hierarchy.checkpoint_and_suspend_parent(
            "parent", "b" * 40, 2, expect_claim_epoch=99
        )
    assert (await db.get_integration_checkpoint("parent"))["episode_id"] is None
    assert await db.get_active_parent_integration_operation("parent") is None

    result = await hierarchy.checkpoint_parent("parent", "b" * 40, 2)
    checkpoint = await db.get_integration_checkpoint("parent")
    operation = await db.get_integration_operation(result["operation_id"])

    assert result["episode_id"] == checkpoint["episode_id"]
    assert operation["episode_id"] == result["episode_id"]
    assert operation["policy_snapshot"] == policy
    assert operation["artifact_snapshot"] == policy["parent"]["route"]["artifact"]
    assert operation["route_playbook_id"] == "hierarchical-delivery"
    assert operation["route_scope"] == "project"
    assert operation["route_scope_identifier"] == "p"
    assert operation["active_stage"] == 0
    async with db._engine.connect() as conn:
        pins = (
            await conn.execute(
                select(integration_operation_artifact_pins).where(
                    integration_operation_artifact_pins.c.operation_id == operation["id"]
                )
            )
        ).mappings().all()
    assert [row["artifact_sha256"] for row in pins] == [
        policy["parent"]["route"]["artifact"]["artifact_sha256"]
    ]


async def test_terminal_children_require_complete_contiguous_receipt_chain(db):
    hierarchy, _checkpointed, children = await _parent_tree(db)

    assert (await hierarchy.readiness("parent"))["outcome"] == "waiting"
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    partial = await hierarchy.readiness("parent")
    assert partial["outcome"] == "waiting"
    assert partial["head_sha"] == "d" * 40
    await _code_receipt(db, children[1], "d" * 40, "e" * 40)

    ready = await hierarchy.readiness("parent")
    assert ready["outcome"] == "ready"
    assert ready["head_sha"] == "e" * 40
    assert [row["source_task_id"] for row in ready["receipts"]] == children


async def test_status_uses_current_receipt_among_multiple_historical_deliveries(db):
    _hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="historic-receipt",
                domain_key="historic-delivery",
                source_task_id=child_id,
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                reviewed_head_sha="1" * 40,
                reviewed_tree_sha="2" * 40,
                before_sha="a" * 40,
                squash_sha="3" * 40,
                after_sha="3" * 40,
                review_evidence={"id": "historic-review"},
                parent_operation_id=None,
                parent_episode_id=None,
                disposition="code",
                created_at=0.0,
            )
        )
    await _code_receipt(db, child_id, "a" * 40, "d" * 40)

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    assert projection["parent_readiness"]["operation_id"] == checkpointed["operation_id"]
    assert "missing_receipt" not in {
        blocker["code"] for blocker in projection["blockers"]
    }


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"reviewed_head_sha": "9" * 40},
        {"repository_id": "wrong-repository"},
        {"target_branch": "wrong-branch"},
        {"parent_operation_id": None, "parent_episode_id": None},
    ],
)
async def test_status_rejects_receipt_not_applicable_to_current_parent_context(
    db, invalid_values
):
    _hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    await _code_receipt(db, child_id, "a" * 40, "d" * 40, **invalid_values)

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    assert "missing_receipt" in {
        blocker["code"] for blocker in projection["blockers"]
    }


async def test_status_blocks_failed_child_without_current_disposition_receipt(db):
    _hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == children[0]).values(status="FAILED")
        )

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "missing_receipt")
    assert blocker["cause"] == "failed_child"


@pytest.mark.parametrize(
    "ordinal,attempts,expected_limit",
    [(0, 2, 2), (1, 1, 1)],
)
async def test_status_uses_typed_limit_for_current_repair_stage(
    db, ordinal, attempts, expected_limit
):
    _hierarchy, checkpointed, _children = await _parent_tree(db, children=1)
    policy = _boundary().repair.model_dump(mode="json")
    policy["primary_attempts"] = 2
    policy["debug_attempts"] = 1
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(active_stage=ordinal)
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=ordinal,
                policy=policy,
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                started_at=1.0,
                deadline_at=100.0,
                attempts=attempts,
                state="active",
            )
        )

    projection = await IntegrationStatusService(db, clock=lambda: 50.0).task_blockers(
        "parent"
    )

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "budget_exhausted")
    assert blocker["cause"] == "attempts"
    assert blocker["stage"] == ordinal
    assert blocker["limit"] == expected_limit


async def test_parent_current_stage_remains_deadline_bound_while_awaiting_completion(db):
    _hierarchy, checkpointed, _children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                started_at=1.0,
                deadline_at=100.0,
                attempts=1,
                state="awaiting_completion",
            )
        )

    projection = await IntegrationStatusService(db, clock=lambda: 101.0).task_blockers(
        "parent"
    )

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "budget_exhausted")
    assert blocker["cause"] == "deadline"
    assert blocker["deadline_at"] == 100.0


@pytest.mark.parametrize("policy", ["block", "ask"])
async def test_failed_child_readiness_exposes_frozen_disposition_policy(db, policy):
    hierarchy, _checkpointed, children = await _parent_tree(
        db, children=1, on_failed_child=policy
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == children[0]).values(status="FAILED")
        )

    readiness = await hierarchy.readiness("parent")

    assert readiness["outcome"] == "failed"
    assert readiness["on_failed_child"] == policy
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "failed_child"}
    ]


async def test_arbitrary_resolution_json_cannot_satisfy_code_receipt_chain(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(
            update(task_delivery_receipts)
            .where(task_delivery_receipts.c.id == f"receipt-{children[0]}")
            .values(
                squash_sha=None,
                resolution_evidence={"kind": "conflict_resolution", "trusted": True},
            )
        )

    readiness = await hierarchy.readiness("parent")

    assert readiness["outcome"] == "waiting"
    assert readiness["head_sha"] == "a" * 40
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "receipt_chain"}
    ]


async def test_unbound_historic_receipts_do_not_satisfy_current_parent_episode(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(
            update(task_delivery_receipts)
            .where(task_delivery_receipts.c.source_task_id == children[0])
            .values(parent_operation_id=None, parent_episode_id=None)
        )

    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "waiting"
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "receipt_missing"}
    ]


async def test_receipt_bound_to_unrelated_historic_episode_is_rejected(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="unrelated-episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=0.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="unrelated-operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="unrelated-episode",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=0.0,
                updated_at=0.0,
            )
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="historic-receipt",
                domain_key="historic-delivery",
                source_task_id=children[0],
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                before_sha="a" * 40,
                squash_sha="d" * 40,
                after_sha="d" * 40,
                review_evidence={"id": "historic-review"},
                parent_operation_id="unrelated-operation",
                parent_episode_id="unrelated-episode",
                disposition="code",
                created_at=0.0,
            )
        )

    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "waiting"
    assert readiness["receipts"] == []


async def test_disposition_revision_supersedes_only_changed_child(db):
    hierarchy, _checkpointed, children = await _parent_tree(db)
    await _code_receipt(db, children[1], "a" * 40, "d" * 40)

    first = await hierarchy.record_disposition(
        children[0],
        disposition="noop",
        reviewed_head_sha="b" * 40,
        reviewed_tree_sha="c" * 40,
        verification_evidence={"producer_id": "forge-observer", "evidence_id": "noop-1"},
        resolution_evidence={"authority": "playbook", "decision_id": "decision-1"},
    )
    assert first["revision"] == 0
    assert (await hierarchy.readiness("parent"))["outcome"] == "ready"

    second = await hierarchy.record_disposition(
        children[0],
        disposition="skipped",
        reviewed_head_sha="b" * 40,
        reviewed_tree_sha="c" * 40,
        verification_evidence={"producer_id": "forge-observer", "evidence_id": "noop-2"},
        resolution_evidence={"authority": "operator", "decision_id": "decision-2"},
    )
    assert second["revision"] == 1
    projection = await hierarchy.readiness("parent")
    assert projection["outcome"] == "ready"
    selected = {row["source_task_id"]: row for row in projection["receipts"]}
    assert selected[children[0]]["disposition"] == "skipped"
    assert selected[children[1]]["id"] == f"receipt-{children[1]}"
    status = await IntegrationStatusService(db).task_blockers("parent")
    assert status is not None
    assert "missing_receipt" not in {item["code"] for item in status["blockers"]}


async def test_parent_completion_pins_exact_verification_for_rollover(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="older-verification",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                episode_id=checkpointed["episode_id"],
                generation=0,
                head_sha="c" * 40,
                required_check_version="parent-v1",
                created_at=1.5,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="check-unit",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="d" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS"))

    verified = await hierarchy.verify_parent("parent", 1, "d" * 40, ["check-unit"])
    assert verified["outcome"] == "verified"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["current_verification_id"] == verified["verification_id"]
    assert checkpoint["checkpoint_sha"] == "d" * 40
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                trigger_id="check-unit",
                current_subject={
                    "kind": "parent",
                    "generation": 1,
                    "head_sha": "d" * 40,
                },
                deadline_event_id=f"repair-deadline-{checkpointed['operation_id']}-0",
                success_subject={
                    "kind": "parent",
                    "generation": 1,
                    "head_sha": "d" * 40,
                },
                success_evidence_id="check-unit",
                started_at=1.0,
                deadline_at=100.0,
                attempts=1,
                state="awaiting_completion",
            )
        )
    async with db._engine.connect() as conn:
        verified_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_verified"
                )
            )
        ).mappings().all()
    assert len(verified_events) == 1
    assert verified_events[0]["payload"]["operation_id"] == checkpointed["operation_id"]

    with pytest.raises(Exception, match="integration completion"):
        await db.transition_task("parent", TaskStatus.COMPLETED, force=True)
    assert (await hierarchy.complete_parent("parent", 1, "d" * 40))["outcome"] == "invariant_error"
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(owner_id="parent", owner_role="verifier", fence_token=2)
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(branch_owner_id="parent")
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(state="human_required")
        )
    assert (
        await hierarchy.complete_parent("parent", 1, "d" * 40)
    )["outcome"] == "invariant_error"
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(state="escalated")
        )
    completed = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert completed["outcome"] == "completed"
    assert (await db.get_task("parent")).status is TaskStatus.COMPLETED
    assert (await db.get_integration_operation(checkpointed["operation_id"]))["state"] == "completed"
    completed_status = await IntegrationStatusService(db).task_blockers("parent")
    assert completed_status is not None
    assert completed_status["parent_readiness"]["operation_id"] == checkpointed["operation_id"]
    assert "missing_receipt" not in {
        item["code"] for item in completed_status["blockers"]
    }
    assert completed_status["repair"] == []
    async with db._engine.connect() as conn:
        completion = (
            await conn.execute(select(integration_parent_operation_completions))
        ).mappings().one()
        repair_stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id
                    == checkpointed["operation_id"]
                )
            )
        ).mappings().one()
    assert completion["operation_id"] == checkpointed["operation_id"]
    assert completion["verification_id"] == verified["verification_id"]
    assert completion["parent_task_id"] == "parent"
    assert completion["episode_id"] == checkpointed["episode_id"]
    assert repair_stage["state"] == "passed"
    assert repair_stage["completed_at"] is not None
    completed_checkpoint = await db.get_integration_checkpoint("parent")
    assert completed_checkpoint["last_completed_operation_id"] == checkpointed["operation_id"]
    assert completed_checkpoint["last_completed_verification_id"] == verified["verification_id"]

    await db.transition_task("parent", TaskStatus.READY, assigned_agent_id=None)
    rolled = await db.get_integration_checkpoint("parent")
    assert rolled["episode_id"] is None
    assert rolled["current_verification_id"] is None
    assert rolled["last_completed_operation_id"] == checkpointed["operation_id"]
    assert rolled["last_completed_verification_id"] == verified["verification_id"]
    assert rolled["generation"] == 2

    rollover = HierarchyIntegration(
        db,
        checkpoint_verifier=lambda _task, _repo, head: head,
        ancestry_verifier=lambda _repo, ancestor, descendant: (
            ancestor == "d" * 40 and descendant == "d" * 40
        ),
    )
    next_episode = await rollover.checkpoint_parent("parent", "d" * 40, 2)
    readiness = await rollover.readiness("parent")
    assert next_episode["episode_id"] != checkpointed["episode_id"]
    assert readiness["outcome"] == "ready"
    assert [row["source_task_id"] for row in readiness["receipts"]] == children
    async with db._engine.connect() as conn:
        carried = (
            await conn.execute(select(integration_episode_receipt_acceptances))
        ).mappings().one()
    assert carried["receipt_id"] == f"receipt-{children[0]}"
    assert carried["previous_verification_id"] == verified["verification_id"]
    assert carried["operation_id"] == next_episode["operation_id"]
    status = await IntegrationStatusService(db).task_blockers("parent")
    assert status is not None
    assert "missing_receipt" not in {item["code"] for item in status["blockers"]}


async def test_child_added_after_verification_makes_completion_stale(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_check_evidence).values(
                id="check-unit",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="d" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
    await hierarchy.verify_parent("parent", 1, "d" * 40, ["check-unit"])
    await hierarchy.file_children("parent", [{"title": "new defect"}], 1)

    result = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert result["outcome"] in {"waiting", "stale_verification"}


async def test_collector_to_parent_verifier_wake_advances_live_head(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn,
            "parent",
            TaskStatus.PAUSED,
            context="integration_parent_suspended",
            _manual_pause_control=True,
        )
    with pytest.raises(HierarchyError, match="guarded verifier wake"):
        await db.transition_task("parent", TaskStatus.READY, force=True)
    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(db).get_owner(target)
    worker = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
    collector = await BranchOwnership(db).transfer(
        worker, checkpointed["operation_id"], "collector"
    )
    verifier = await BranchOwnership(db).transfer(collector, "parent", "verifier")

    result = await hierarchy.wake_verifier("parent", verifier)

    assert result["outcome"] == "woken"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == "d" * 40
    assert checkpoint["branch_owner_id"] == "parent"
    assert checkpoint["state"] == "verifying"
    assert (await db.get_task("parent")).status is TaskStatus.READY


async def test_parent_prime_summary_uses_receipt_readiness_projection(db):
    from src.prime.sections import build_integration_delivery_summary

    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)

    summary = await build_integration_delivery_summary(db, await db.get_task("parent"))

    assert "Readiness: **ready**" in summary
    assert "Pre-collection head: `" + "a" * 40 + "`" in summary
    assert "Current aggregate head: `" + "d" * 40 + "`" in summary
    assert f"`{children[0]}`: code squash `{'d' * 40}`" in summary
    assert "Required aggregate checks: `unit`" in summary


async def test_branchless_parent_creates_exact_routed_verifier_delegate_before_handoff(db):
    await db.create_profile(AgentProfile(id="verifier", name="Verifier", harness="claude"))
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        projection = await hierarchy.parent_completion.mark_ready_on(conn, "parent")

    operation = await db.get_integration_operation(checkpointed["operation_id"])
    delegate = await db.get_task(operation["verifier_task_id"])
    assert projection["state"] == "integration_ready"
    assert delegate.parent_task_id is None
    assert delegate.status is TaskStatus.PAUSED
    assert delegate.repo_id == "repo"
    assert delegate.branch_name == "aq/parent"
    assert delegate.profile_id == "verifier"
    assert delegate.intelligence_class == "high"

    async with db._engine.connect() as conn:
        ready_event = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_ready"
                )
            )
        ).mappings().one()
    payload = dict(ready_event["payload"])
    payload.pop("event_id")
    assert payload == {
        "project_id": "p",
        "operation_id": checkpointed["operation_id"],
        "task_id": "parent",
        "title": "parent",
        "episode_id": checkpointed["episode_id"],
        "generation": 1,
        "head_sha": "d" * 40,
        "verifier_task_id": delegate.id,
        "target": {"repository_id": "repo", "branch": "aq/parent"},
        "expected_token": 1,
        "next_owner_id": delegate.id,
        "next_role": "verifier",
    }

    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(db).get_owner(target)
    worker = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
    collector = await BranchOwnership(db).transfer(
        worker, checkpointed["operation_id"], "collector"
    )
    verifier = await BranchOwnership(db).transfer(
        collector, delegate.id, "verifier"
    )
    async with db.immediate() as conn:
        replayed_projection = await hierarchy.parent_completion.mark_ready_on(
            conn, "parent"
        )
    async with db._engine.connect() as conn:
        ready_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_ready"
                )
            )
        ).mappings().all()
    assert replayed_projection["state"] == "integration_ready"
    assert len(ready_events) == 1
    assert ready_events[0]["payload"] == ready_event["payload"]
    assert (await hierarchy.wake_verifier("parent", verifier))["outcome"] == "woken"
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED
    assert (await db.get_task(delegate.id)).status is TaskStatus.READY


async def test_transfer_owner_replay_after_crash_still_wakes_verifier(
    command_handler_factory,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="integration project"))
    await db.create_profile(AgentProfile(id="verifier", name="Verifier", harness="claude"))
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        await hierarchy.parent_completion.mark_ready_on(conn, "parent")
    operation = await db.get_integration_operation(checkpointed["operation_id"])
    target = BranchKey(repository_id="repo", branch="aq/parent")
    current = await BranchOwnership(db).get_owner(target)
    crashed_transfer = await BranchOwnership(db).transfer(
        Fence(target=target, owner_id=current["owner_id"], token=current["fence_token"]),
        operation["verifier_task_id"],
        "verifier",
    )

    result = await handler.execute(
        "integration_transfer_owner",
        {
            "target": target.model_dump(mode="json"),
            "expected_token": crashed_transfer.token - 1,
            "next_owner_id": operation["verifier_task_id"],
            "next_role": "verifier",
        },
    )

    assert result["outcome"] == "transferred"
    assert (await db.get_task(operation["verifier_task_id"])).status is TaskStatus.READY


async def test_sealed_batch_member_protects_descendant_mutation(db):
    await _enable_project(db)
    await db.create_task(Task(id="root", project_id="p", title="root", description=""))
    await db.create_task(
        Task(id="root.1", project_id="p", parent_task_id="root", title="child", description="")
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request-batch",
                source_manifest_digest="sha256:" + "d" * 64,
                base_sha="a" * 40,
                lifecycle="sealing",
                integration_branch="refs/heads/integration/batch",
                current_revision=0,
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                id="review-batch",
                source_task_id="root",
                repository_id="repo",
                source_base="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                reviewer_task_id="root",
                review_kind="review",
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
                task_id="root",
                repository_id="repo",
                source_base_sha="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                review_evidence_id="review-batch",
                review_evidence={},
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="sealed")
        )
        with pytest.raises(HierarchyError, match="sealed"):
            await db.guard_integration_mutation("root.1", "reopen", conn=conn)
