"""Prepared child-to-parent promotion and crash recovery."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select, update
from unittest.mock import AsyncMock
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database import Database
from src.database.tables import (
    integration_branch_owners,
    integration_check_evidence,
    integration_episode_receipt_acceptances,
    integration_outbox,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    playbook_artifacts,
    sessions,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.git.manager import GitError, GitManager
from src.integration.models import BranchKey, Fence, PromotionInput
from src.integration.ownership import BranchOwnership
from src.models import Project, RepoConfig, RepoSourceType, SessionRecord, Task, TaskStatus


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "promotion.db"))
    await database.initialize()
    await database.create_project(Project(id="project", name="Promotion project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="project",
            source_type=RepoSourceType.CLONE,
            url=str(tmp_path / "origin.git"),
        )
    )
    await database.create_task(
        Task(
            id="parent",
            project_id="project",
            title="Parent",
            description="",
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    async with database.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="collector-op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    await database.create_task(
        Task(
            id="child",
            project_id="project",
            title="Child",
            description="",
            parent_task_id="parent",
            repo_id="repo",
            branch_name="aq/child",
        )
    )
    async with database.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                checkpoint_sha="a" * 40,
                generation=0,
                episode_id="episode",
                state="awaiting_children",
                version=0,
                updated_at=1.0,
            )
        )
    yield database
    await database.close()


def _review(*, evidence_id: str, generation: int, verdict: str = "approved") -> dict:
    return {
        "id": evidence_id,
        "source_task_id": "child",
        "repository_id": "repo",
        "source_base": "a" * 40,
        "reviewed_head_sha": "b" * 40,
        "reviewed_tree_sha": "c" * 40,
        "reviewer_task_id": "review",
        "reviewer_session_attempt_id": "attempt",
        "review_kind": "leaf",
        "generation": generation,
        "verdict": verdict,
        "evidence": {"checks": ["focused"]},
        "created_at": float(generation),
    }


async def test_review_evidence_uses_latest_generation_and_fails_closed(db):
    await db.append_integration_review_evidence(_review(evidence_id="approved", generation=1))
    found = await db.get_applicable_integration_review_evidence(
        source_task_id="child",
        repository_id="repo",
        source_base="a" * 40,
        reviewed_head_sha="b" * 40,
        current_generation=1,
    )
    assert found and found["id"] == "approved"

    unrelated = _review(evidence_id="newer-other-head", generation=1)
    unrelated.update(reviewed_head_sha="d" * 40, created_at=2.0)
    await db.append_integration_review_evidence(unrelated)
    found = await db.get_applicable_integration_review_evidence(
        source_task_id="child",
        repository_id="repo",
        source_base="a" * 40,
        reviewed_head_sha="b" * 40,
        current_generation=1,
    )
    assert found and found["id"] == "approved"

    rejected_same_tuple = _review(
        evidence_id="newer-same-tuple-rejection", generation=1, verdict="rejected"
    )
    rejected_same_tuple["created_at"] = 3.0
    await db.append_integration_review_evidence(rejected_same_tuple)
    assert (
        await db.get_applicable_integration_review_evidence(
            source_task_id="child",
            repository_id="repo",
            source_base="a" * 40,
            reviewed_head_sha="b" * 40,
            current_generation=1,
        )
        is None
    )

    await db.append_integration_review_evidence(
        _review(evidence_id="rejected", generation=2, verdict="rejected")
    )
    assert (
        await db.get_applicable_integration_review_evidence(
            source_task_id="child",
            repository_id="repo",
            source_base="a" * 40,
            reviewed_head_sha="b" * 40,
            current_generation=2,
        )
        is None
    )


def _intent_values() -> dict:
    return {
        "domain_key": "domain",
        "operation_key": "activation-1",
        "project_id": "project",
        "receipt_id": "receipt-1",
        "source_task_id": "child",
        "target_task_id": "parent",
        "source_head": "b" * 40,
        "source_base": "a" * 40,
        "repository_id": "repo",
        "origin_url": "/remote.git",
        "target_branch": "aq/parent",
        "expected_target": "d" * 40,
        "fence_owner_id": "collector",
        "fence_token": 1,
        "review_evidence": _review(evidence_id="approved", generation=1),
        "authors": [{"name": "Author", "email": "author@example.test"}],
        "provenance": {"principal": "service:collector"},
        "commit_metadata": {"message": "message"},
        "created_at": 1.0,
    }


async def test_intent_reservation_reuses_domain_and_blocks_other_target_work(db):
    values = _intent_values()
    first = await db.reserve_integration_promotion_intent(values)
    again = await db.reserve_integration_promotion_intent(values | {"receipt_id": "other"})
    assert first["id"] == again["id"]
    assert again["receipt_id"] == "receipt-1"

    with pytest.raises(ValueError, match="unresolved promotion"):
        await db.reserve_integration_promotion_intent(
            values
            | {
                "domain_key": "other-domain",
                "source_task_id": "other-child",
                "receipt_id": "receipt-2",
            }
        )


async def test_intent_reservation_freezes_operation_key_on_reuse(db):
    values = _intent_values()
    await db.reserve_integration_promotion_intent(values)

    with pytest.raises(ValueError, match="operation_key"):
        await db.reserve_integration_promotion_intent(
            values | {"operation_key": "different-operation"}
        )


async def test_conflict_resolution_reservation_is_immutable_and_reopens_target(db):
    values = _intent_values() | {
        "id": "conflicted-intent",
        "operation_key": "collector-op",
        "fence_owner_id": "collector-op",
    }
    await db.reserve_integration_promotion_intent(values)
    await db.mark_integration_promotion_conflict("conflicted-intent", {"paths": ["shared.txt"]})
    resolution = {
        "resolved_head_sha": "e" * 40,
        "resolved_tree_sha": "f" * 40,
        "repair_commit_shas": ["1" * 40, "e" * 40],
        "operation_id": "collector-op",
        "stage_ordinal": 0,
        "repair_task_id": "repair-task",
        "repair_session_id": "repair-session",
        "repair_session_instance_token": "instance",
        "repair_workspace_id": "repair-workspace",
        "fence_owner_id": "repair-task",
        "fence_token": 2,
    }

    async with db.immediate() as conn:
        first = await db.reserve_integration_conflict_resolution(
            conn, "conflicted-intent", resolution
        )
    async with db.immediate() as conn:
        replay = await db.reserve_integration_conflict_resolution(
            conn, "conflicted-intent", resolution
        )

    assert first["state"] == replay["state"] == "resolution_reserved"
    assert replay["receipt_id"] == values["receipt_id"]
    assert replay["resolution_head_sha"] == "e" * 40
    assert replay["resolution_commit_shas"] == ["1" * 40, "e" * 40]
    async with db.immediate() as conn:
        with pytest.raises(ValueError, match="resolution identity changed"):
            await db.reserve_integration_conflict_resolution(
                conn,
                "conflicted-intent",
                resolution | {"resolved_tree_sha": "2" * 40},
            )
    with pytest.raises(ValueError, match="unresolved promotion"):
        await db.reserve_integration_promotion_intent(
            values
            | {
                "id": "competing-intent",
                "domain_key": "competing-domain",
                "receipt_id": "competing-receipt",
                "source_task_id": "competing-child",
            }
        )


async def test_concurrent_different_domains_reserve_only_one_target_intent(db):
    first = _intent_values()
    second = first | {
        "id": "intent-2",
        "domain_key": "different-domain",
        "operation_key": "activation-2",
        "receipt_id": "receipt-2",
        "source_task_id": "other-child",
    }

    results = await asyncio.gather(
        db.reserve_integration_promotion_intent(first),
        db.reserve_integration_promotion_intent(second),
        return_exceptions=True,
    )

    assert sum(isinstance(result, dict) for result in results) == 1
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "unresolved promotion" in str(failures[0])


def _git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
async def promotion_case(db, tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "--initial-branch=main", str(origin)])
    _git(["clone", str(origin), str(work)])
    _git(["config", "user.name", "Seed"], work)
    _git(["config", "user.email", "seed@example.test"], work)
    (work / "shared.txt").write_text("base\n")
    _git(["add", "shared.txt"], work)
    _git(["commit", "-m", "base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "main"], work)
    _git(["push", "origin", f"{base}:refs/heads/aq/parent"], work)

    _git(["switch", "-c", "aq/child"], work)
    _git(["config", "user.name", "Bob Builder"], work)
    _git(["config", "user.email", "bob@example.test"], work)
    (work / "child.txt").write_text("one\n")
    _git(["add", "child.txt"], work)
    _git(["commit", "-m", "first\n\nCo-authored-by: Carol Coder <carol@example.test>"], work)
    _git(["config", "user.name", "Alice Author"], work)
    _git(["config", "user.email", "alice@example.test"], work)
    (work / "child.txt").write_text("one\ntwo\n")
    _git(["commit", "-am", "second"], work)
    head = _git(["rev-parse", "HEAD"], work)
    tree = _git(["rev-parse", "HEAD^{tree}"], work)
    _git(["push", "origin", "aq/child"], work)

    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id="child",
                repository_id="repo",
                parent_task_id="parent",
                parent_repository_id="repo",
                parent_ref="aq/parent",
                base_sha=base,
                creation_generation=7,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
    await db.append_integration_review_evidence(
        {
            "id": "review-evidence",
            "source_task_id": "child",
            "repository_id": "repo",
            "source_base": base,
            "reviewed_head_sha": head,
            "reviewed_tree_sha": tree,
            "reviewer_task_id": "review-task",
            "reviewer_session_attempt_id": None,
            "review_kind": "leaf",
            "generation": 0,
            "verdict": "approved",
            "evidence": {"checks": ["focused"]},
            "created_at": 2.0,
        }
    )
    fence = await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"),
        "collector-op",
        "collector",
    )
    request = PromotionInput(
        operation_key="collector-op",
        source_task_id="child",
        source_head=head,
        source_base=base,
        expected_target=base,
        fence=fence,
    )
    return {
        "origin": origin,
        "work": work,
        "base": base,
        "head": head,
        "tree": tree,
        "fence": fence,
        "request": request,
        "data_dir": tmp_path / "data",
    }


def _hierarchy_policy() -> dict:
    artifact = {
        "playbook_id": "hierarchical-delivery",
        "artifact_sha256": "sha256:" + "a" * 64,
        "schema_generation": 2,
        "contract_fingerprint": "sha256:" + "b" * 64,
        "source_digest": "sha256:" + "c" * 64,
        "compiler_build": "test",
        "compiled_at": "2026-09-05T00:00:00Z",
        "version": 1,
    }
    boundary = {
        "required_checks": {"version": "checks-v1", "names": ["unit"], "producer_id": "ci"},
        "repair": {
            "primary_seconds": 30,
            "primary_attempts": 2,
            "debug_seconds": 60,
            "debug_attempts": 1,
            "debug_intelligence_class": "debug-high",
        },
        "route": {
            "playbook_id": "hierarchical-delivery",
            "scope": "project",
            "scope_identifier": "project",
            "artifact": artifact,
        },
    }
    return {
        "version": 1,
        "parent": boundary,
        "root": boundary,
        "branchless_parent": "skip",
        "on_failed_child": "block",
    }


@pytest.fixture
async def conflict_resolution_case(db, promotion_case):
    from src.integration.promotion import PromotionConflict, PromotionService

    case = promotion_case
    work = case["work"]
    (work / "shared.txt").write_text("child version\n")
    _git(["add", "shared.txt"], work)
    _git(["commit", "-m", "child conflict"], work)
    source = _git(["rev-parse", "HEAD"], work)
    source_tree = _git(["rev-parse", "HEAD^{tree}"], work)
    _git(["push", "origin", "aq/child"], work)

    _git(["switch", "-c", "aq/parent", case["base"]], work)
    (work / "shared.txt").write_text("parent version\n")
    _git(["commit", "-am", "parent conflict"], work)
    target = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "aq/parent"], work)
    await db.append_integration_review_evidence(
        {
            "id": "resolution-review",
            "source_task_id": "child",
            "repository_id": "repo",
            "source_base": case["base"],
            "reviewed_head_sha": source,
            "reviewed_tree_sha": source_tree,
            "reviewer_task_id": "review-task",
            "reviewer_session_attempt_id": None,
            "review_kind": "leaf",
            "generation": 0,
            "verdict": "approved",
            "evidence": {"checks": ["focused"]},
            "created_at": 3.0,
        }
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "collector-op")
            .values(state="cancelled")
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="resolution-episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha=target,
                created_at=3.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="resolution-op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="resolution-episode",
                active_stage=0,
                state="active",
                policy_snapshot=_hierarchy_policy(),
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=3.0,
                updated_at=3.0,
            )
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(
                checkpoint_sha=target,
                episode_id="resolution-episode",
                state="awaiting_children",
            )
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(owner_id="resolution-op", fence_token=2)
        )
    collector_fence = BranchKey(repository_id="repo", branch="aq/parent")
    request = case["request"].model_copy(
        update={
            "operation_key": "resolution-op",
            "source_head": source,
            "expected_target": target,
            "fence": Fence(target=collector_fence, owner_id="resolution-op", token=2),
        }
    )
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    with pytest.raises(PromotionConflict) as caught:
        await service.prepare(request)

    (work / "shared.txt").write_text("resolved version\n")
    (work / "child.txt").write_text("one\ntwo\n")
    _git(["add", "shared.txt", "child.txt"], work)
    _git(["commit", "-m", "resolve child conflict"], work)
    (work / "repair.txt").write_text("verified repair\n")
    _git(["add", "repair.txt"], work)
    _git(["commit", "-m", "finish repair"], work)
    resolved_head = _git(["rev-parse", "HEAD"], work)
    resolved_tree = _git(["rev-parse", "HEAD^{tree}"], work)
    repair_commits = tuple(
        _git(["rev-list", "--reverse", f"{target}..{resolved_head}"], work).splitlines()
    )

    await db.update_project(
        "project",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
        hierarchical_integration_policy=_hierarchy_policy(),
    )
    async with db.immediate() as conn:
        artifact = _hierarchy_policy()["parent"]["route"]["artifact"]
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact,
                scope="project",
                scope_identifier="project",
                profile_fingerprint="",
                path="/tmp/hierarchical-delivery-artifact",
                size_bytes=1,
                validation="{}",
                created_at=3.0,
            )
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status="PAUSED")
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "child").values(status="COMPLETED")
        )
    await db.create_task(
        Task(
            id="repair-task",
            project_id="project",
            title="Resolve conflict",
            description="",
            status=TaskStatus.IN_PROGRESS,
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="child",
                repository_id="repo",
                branch="aq/child",
                checkpoint_sha=source,
                generation=0,
                state="working",
                version=0,
                updated_at=3.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="resolution-op",
                ordinal=0,
                policy={},
                repair_task_id="repair-task",
                writer_kind="repair_delegate",
                starting_sha=target,
                deadline_at=4_000_000_000.0,
                attempts=1,
                state="active",
            )
        )
        await conn.execute(
            insert(workspaces).values(
                id="resolution-workspace",
                project_id="project",
                workspace_path=str(work),
                source_type="link",
                locked_by_task_id="repair-task",
                enabled=True,
                created_at=3.0,
            )
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(
                owner_id="repair-task",
                owner_role="repair",
                fence_token=3,
                handoff_state="attached",
                session_id="resolution-session",
                workspace_id="resolution-workspace",
            )
        )
    await db.create_session(
        SessionRecord(
            id="resolution-session",
            task_id="repair-task",
            project_id="project",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-resolution",
            lifecycle="task",
            state="running",
            work_dir=str(work),
            epoch="epoch",
            instance_token="resolution-instance",
            started_at=3.0,
        )
    )
    return case | {
        "intent_id": caught.value.value.intent_id,
        "receipt_id": caught.value.value.receipt_id,
        "target": target,
        "source": source,
        "source_tree": source_tree,
        "resolved_head": resolved_head,
        "resolved_tree": resolved_tree,
        "repair_commits": repair_commits,
        "resolution_fence": {
            "target": {"repository_id": "repo", "branch": "aq/parent"},
            "owner_id": "repair-task",
            "token": 3,
        },
    }


def _resolution_principal(*, task_id: str = "repair-task", session_id: str = "resolution-session"):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind
    from src.profiles.capabilities import CapabilityPolicy

    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=[
                "integration_resolve_conflict",
                "integration_push_conflict_resolution",
            ]
        ),
        session_id=session_id,
        task_id=task_id,
        project_id="project",
        profile_id="repairer",
    )


def _resolution_request(case: dict, **updates):
    from src.integration.models import ConflictResolutionInput

    values = {
        "intent_id": case["intent_id"],
        "operation_id": "resolution-op",
        "resolved_head_sha": case["resolved_head"],
        "resolved_tree_sha": case["resolved_tree"],
        "repair_commit_shas": case["repair_commits"],
        "fence": case["resolution_fence"],
    }
    values.update(updates)
    return ConflictResolutionInput(**values)


async def test_clean_promotion_is_retained_attributed_pushed_and_reconciled(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())

    prepared = await service.prepare(case["request"])
    assert prepared.prepared_sha
    retained = next((case["data_dir"] / "integration-repositories").glob("*.git"))
    assert (
        _git(["rev-parse", f"refs/aq/integration-intents/{prepared.intent_id}"], retained)
        == prepared.prepared_sha
    )
    assert _git(["show", "-s", "--format=%P", prepared.prepared_sha], retained) == case["base"]
    assert _git(["show", "-s", "--format=%ae", prepared.prepared_sha], retained) == (
        "alice@example.test"
    )
    message = _git(["show", "-s", "--format=%B", prepared.prepared_sha], retained)
    assert f"AQ-Receipt: {prepared.receipt_id}" in message
    assert "Co-authored-by: Bob Builder <bob@example.test>" in message
    assert "Co-authored-by: Carol Coder <carol@example.test>" in message

    pushed = await service.push(prepared.intent_id, case["fence"])
    recovered = await service.reconcile(prepared.intent_id)
    again = await service.reconcile(prepared.intent_id)
    assert pushed == recovered == again
    assert recovered.receipt_id == prepared.receipt_id
    assert recovered.prepared_sha == prepared.prepared_sha
    assert (
        _git(["ls-remote", "--heads", "origin", "refs/heads/aq/parent"], case["work"]).split()[0]
        == prepared.prepared_sha
    )

    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 1
        assert await conn.scalar(select(func.count()).select_from(integration_outbox)) == 2
        delivery = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "delivery.applied"
                )
            )
        ).mappings().one()
        assert delivery["payload"]["operation_id"] == case["fence"].owner_id
        assert delivery["payload"]["promotion_intent_id"] == prepared.intent_id


async def test_conflict_resolution_push_reconcile_writes_original_receipt_and_events(
    db, conflict_resolution_case, command_handler_factory
):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.integration.promotion import PromotionService
    from src.profiles.capabilities import CapabilityPolicy

    case = conflict_resolution_case
    handler = await command_handler_factory()
    await handler.orchestrator.db.close()
    handler.orchestrator.db = db
    handler.orchestrator.promotion_service = PromotionService(
        db, data_dir=case["data_dir"], git_manager=GitManager()
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=[
                "integration_resolve_conflict",
                "integration_push_conflict_resolution",
            ]
        ),
        session_id="resolution-session",
        task_id="repair-task",
        project_id="project",
        profile_id="repairer",
    )
    reserve_args = {
        "intent_id": case["intent_id"],
        "operation_id": "resolution-op",
        "resolved_head_sha": case["resolved_head"],
        "resolved_tree_sha": case["resolved_tree"],
        "repair_commit_shas": list(case["repair_commits"]),
        "fence": case["resolution_fence"],
    }
    with principal_context(principal):
        reserved = await handler.execute("integration_resolve_conflict", reserve_args)
        pushed = await handler.execute(
            "integration_push_conflict_resolution",
            {"intent_id": case["intent_id"], "fence": case["resolution_fence"]},
        )
    reconciled = await handler.execute(
        "integration_reconcile_promotion", {"intent_id": case["intent_id"]}
    )

    assert reserved["outcome"] == "reserved"
    assert pushed["outcome"] == "pushed"
    assert reconciled["outcome"] == "applied"
    assert reconciled["receipt_id"] == case["receipt_id"]
    assert (
        _git(["ls-remote", "origin", "refs/heads/aq/parent"], case["work"]).split()[0]
        == case["resolved_head"]
    )
    async with db._engine.connect() as conn:
        receipt = (
            await conn.execute(
                select(task_delivery_receipts).where(
                    task_delivery_receipts.c.id == case["receipt_id"]
                )
            )
        ).mappings().one()
        events = {
            row["event_type"]: row
            for row in (await conn.execute(select(integration_outbox))).mappings().all()
        }
    assert receipt["domain_key"] == (
        await db.get_integration_promotion_intent(case["intent_id"])
    )["domain_key"]
    assert receipt["source_task_id"] == "child"
    assert receipt["target_task_id"] == "parent"
    assert receipt["reviewed_head_sha"] == case["source"]
    assert receipt["reviewed_tree_sha"] == case["source_tree"]
    assert receipt["before_sha"] == case["target"]
    assert receipt["after_sha"] == case["resolved_head"]
    assert receipt["squash_sha"] is None
    assert receipt["parent_operation_id"] == "resolution-op"
    assert receipt["parent_episode_id"] == "resolution-episode"
    assert receipt["resolution_evidence"]["kind"] == "conflict_resolution"
    assert receipt["resolution_evidence"]["repair_commit_shas"] == list(
        case["repair_commits"]
    )
    assert receipt["resolution_evidence"]["remote_proof"] == {
        "kind": "exact_resolution_tip",
        "remote_sha": case["resolved_head"],
        "resolved_tree_sha": case["resolved_tree"],
        "repair_commit_shas": list(case["repair_commits"]),
    }
    assert events["integration.resolution_push_observed"]["payload"] == {
        "event_id": f"resolution-pushed-{case['intent_id']}",
        "project_id": "project",
        "operation_id": "resolution-op",
        "promotion_intent_id": case["intent_id"],
    }
    assert "delivery.applied" in events
    assert "integration.cleanup_pending" in events


@pytest.mark.parametrize("invalid_proof", ["wrong_tree", "unlisted_commit"])
async def test_resolution_push_rejects_changed_reserved_git_proof(
    db, conflict_resolution_case, invalid_proof
):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionInvariantError, PromotionService

    case = conflict_resolution_case
    updates = (
        {"resolved_tree_sha": "f" * 40}
        if invalid_proof == "wrong_tree"
        else {"repair_commit_shas": (case["resolved_head"],)}
    )
    request = _resolution_request(case, **updates)
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())

    with principal_context(_resolution_principal()):
        await service.reserve_resolution(request)
        with pytest.raises(PromotionInvariantError, match="resolution (tree|commit range)"):
            await service.push_resolution(case["intent_id"], request.fence)

    assert (
        _git(["ls-remote", "origin", "refs/heads/aq/parent"], case["work"]).split()[0]
        == case["target"]
    )


async def test_resolution_push_rejects_merge_commit_range(db, conflict_resolution_case):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionInvariantError, PromotionService

    case = conflict_resolution_case
    work = case["work"]
    _git(["switch", "-c", "resolution-side", case["target"]], work)
    (work / "side.txt").write_text("side\n")
    _git(["add", "side.txt"], work)
    _git(["commit", "-m", "side repair"], work)
    _git(["switch", "aq/parent"], work)
    _git(["merge", "--no-ff", "resolution-side", "-m", "merge repair"], work)
    merged_head = _git(["rev-parse", "HEAD"], work)
    merged_tree = _git(["rev-parse", "HEAD^{tree}"], work)
    merged_commits = tuple(
        _git(["rev-list", "--reverse", f"{case['target']}..{merged_head}"], work).splitlines()
    )
    request = _resolution_request(
        case,
        resolved_head_sha=merged_head,
        resolved_tree_sha=merged_tree,
        repair_commit_shas=merged_commits,
    )
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())

    with principal_context(_resolution_principal()):
        await service.reserve_resolution(request)
        with pytest.raises(PromotionInvariantError, match="merge commit"):
            await service.push_resolution(case["intent_id"], request.fence)

    assert (
        _git(["ls-remote", "origin", "refs/heads/aq/parent"], work).split()[0]
        == case["target"]
    )


async def test_resolution_push_rejects_moved_target(db, conflict_resolution_case):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionService, PromotionTargetMoved

    case = conflict_resolution_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    request = _resolution_request(case)
    with principal_context(_resolution_principal()):
        await service.reserve_resolution(request)

    _git(["switch", "-c", "resolution-competitor", case["target"]], case["work"])
    (case["work"] / "competitor.txt").write_text("moved\n")
    _git(["add", "competitor.txt"], case["work"])
    _git(["commit", "-m", "move target"], case["work"])
    moved = _git(["rev-parse", "HEAD"], case["work"])
    _git(["push", "origin", "HEAD:refs/heads/aq/parent"], case["work"])

    with principal_context(_resolution_principal()):
        with pytest.raises(PromotionTargetMoved, match="target branch moved"):
            await service.push_resolution(case["intent_id"], request.fence)
    assert _git(["ls-remote", "origin", "refs/heads/aq/parent"], case["work"]).split()[0] == moved
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 0


@pytest.mark.parametrize("phase", ["before_resolution_push", "after_resolution_push"])
async def test_resolution_push_crash_replays_exact_reserved_identity(
    db, conflict_resolution_case, phase
):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionService

    case = conflict_resolution_case
    request = _resolution_request(case)
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    with principal_context(_resolution_principal()):
        await service.reserve_resolution(request)
        crashing = PromotionService(
            db,
            data_dir=case["data_dir"],
            git_manager=GitManager(),
            crash_hook=CrashOnce(phase),
        )
        with pytest.raises(InjectedCrash, match=phase):
            await crashing.push_resolution(case["intent_id"], request.fence)

        recovered, already_applied = await service.push_resolution(
            case["intent_id"], request.fence
        )
    again = await service.reconcile(case["intent_id"])

    assert recovered.intent_id == again.intent_id == case["intent_id"]
    assert already_applied is (phase == "after_resolution_push")
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 1
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(integration_outbox)
                .where(integration_outbox.c.event_type == "integration.resolution_push_observed")
            )
            == 1
        )


@pytest.mark.parametrize("authority_change", ["stage_expired", "session_replaced"])
async def test_resolution_push_rechecks_authority_inside_mutation_exclusion(
    db, conflict_resolution_case, authority_change
):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionService, PromotionTargetMoved

    case = conflict_resolution_case
    request = _resolution_request(case)
    initial = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    with principal_context(_resolution_principal()):
        await initial.reserve_resolution(request)

    async def change_between_validation_and_write(phase: str) -> None:
        if phase == "before_resolution_authority_recheck":
            async with db.immediate() as conn:
                if authority_change == "stage_expired":
                    await conn.execute(
                        update(integration_repair_stages)
                        .where(
                            integration_repair_stages.c.operation_id == "resolution-op",
                            integration_repair_stages.c.ordinal == 0,
                        )
                        .values(deadline_at=0.0)
                    )
                else:
                    await conn.execute(
                        update(sessions)
                        .where(sessions.c.id == "resolution-session")
                        .values(instance_token="replacement-instance")
                    )

    racing = PromotionService(
        db,
        data_dir=case["data_dir"],
        git_manager=GitManager(),
        crash_hook=change_between_validation_and_write,
    )
    with principal_context(_resolution_principal()):
        with pytest.raises(PromotionTargetMoved, match="authority is stale"):
            await racing.push_resolution(case["intent_id"], request.fence)
    assert (
        _git(["ls-remote", "origin", "refs/heads/aq/parent"], case["work"]).split()[0]
        == case["target"]
    )


async def test_debug_successor_pushes_primary_reserved_oids_after_proved_handoff(
    db, conflict_resolution_case
):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionService, PromotionTargetMoved

    case = conflict_resolution_case
    request = _resolution_request(case)
    ownership = BranchOwnership(db, confirm_handoff=lambda _row: True)
    service = PromotionService(
        db,
        data_dir=case["data_dir"],
        git_manager=GitManager(),
        ownership=ownership,
    )
    with principal_context(_resolution_principal()):
        await service.reserve_resolution(request)

    await db.create_task(
        Task(
            id="debug-repair-task",
            project_id="project",
            title="Debug conflict resolution",
            description="",
            status=TaskStatus.IN_PROGRESS,
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    debug_fence = await ownership.transfer(
        Fence.model_validate(case["resolution_fence"]), "debug-repair-task", "repair"
    )
    await db.create_session(
        SessionRecord(
            id="debug-resolution-session",
            task_id="debug-repair-task",
            project_id="project",
            profile_id="debug-repairer",
            harness="fake",
            provider="fake",
            name="s-debug-resolution",
            lifecycle="task",
            state="running",
            work_dir=str(case["work"]),
            epoch="debug-epoch",
            instance_token="debug-resolution-instance",
            started_at=4.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == "resolution-op",
                integration_repair_stages.c.ordinal == 0,
            )
            .values(state="expired", completed_at=4.0)
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="resolution-op",
                ordinal=1,
                policy={},
                repair_task_id="debug-repair-task",
                writer_kind="repair_delegate",
                starting_sha=case["target"],
                deadline_at=4_000_000_000.0,
                attempts=1,
                state="active",
            )
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "resolution-op")
            .values(active_stage=1, state="escalated", updated_at=4.0)
        )
        await conn.execute(
            update(workspaces)
            .where(workspaces.c.id == "resolution-workspace")
            .values(locked_by_task_id="debug-repair-task")
        )
    await ownership.attach(
        debug_fence,
        "debug-resolution-session",
        "resolution-workspace",
        expected_role="repair",
    )

    with principal_context(_resolution_principal()):
        with pytest.raises(PromotionTargetMoved, match="authority is stale"):
            await service.push_resolution(case["intent_id"], request.fence)

    debug_principal = _resolution_principal(
        task_id="debug-repair-task", session_id="debug-resolution-session"
    )
    with principal_context(debug_principal):
        pushed, already_applied = await service.push_resolution(
            case["intent_id"], debug_fence
        )
    reconciled = await service.reconcile(case["intent_id"])

    assert pushed.intent_id == reconciled.intent_id == case["intent_id"]
    assert already_applied is False
    intent = await db.get_integration_promotion_intent(case["intent_id"])
    assert intent["resolution_task_id"] == "repair-task"
    assert intent["resolution_session_id"] == "resolution-session"
    assert intent["resolution_session_instance_token"] == "resolution-instance"
    assert intent["resolution_fence_token"] == 3
    assert intent["resolution_push_evidence"]["repair_task_id"] == "debug-repair-task"
    assert intent["resolution_push_evidence"]["repair_session_id"] == (
        "debug-resolution-session"
    )
    assert intent["resolution_push_evidence"]["repair_session_instance_token"] == (
        "debug-resolution-instance"
    )
    assert intent["resolution_push_evidence"]["fence_token"] == debug_fence.token


async def test_expired_repair_stage_cannot_reserve_resolution(db, conflict_resolution_case):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionService, PromotionTargetMoved

    case = conflict_resolution_case
    service = PromotionService(
        db,
        data_dir=case["data_dir"],
        git_manager=GitManager(),
        clock=lambda: 5_000_000_000.0,
    )
    with principal_context(_resolution_principal()):
        with pytest.raises(PromotionTargetMoved, match="authority is stale"):
            await service.reserve_resolution(_resolution_request(case))

    intent = await db.get_integration_promotion_intent(case["intent_id"])
    assert intent["state"] == "conflict"
    assert intent["resolution_head_sha"] is None


async def test_concurrent_exact_resolution_reservation_names_one_replay(
    db, conflict_resolution_case
):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionService

    case = conflict_resolution_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    request = _resolution_request(case)
    with principal_context(_resolution_principal()):
        first, second = await asyncio.gather(
            service.reserve_resolution(request),
            service.reserve_resolution(request),
        )

    assert first[0] == second[0]
    assert sorted((first[1], second[1])) == [False, True]


async def test_committed_resolution_replay_survives_writer_and_source_cleanup(
    db, conflict_resolution_case
):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionService

    case = conflict_resolution_case
    request = _resolution_request(case)
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    with principal_context(_resolution_principal()):
        await service.reserve_resolution(request)
        await service.push_resolution(case["intent_id"], request.fence)
    committed = await service.reconcile(case["intent_id"])

    await db.update_session("resolution-session", state="stopped")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(
                handoff_state="released",
                session_id=None,
                workspace_id=None,
                confirmed_workspace_id="resolution-workspace",
            )
        )
        await conn.execute(
            update(workspaces)
            .where(workspaces.c.id == "resolution-workspace")
            .values(enabled=False, locked_by_task_id=None)
        )
    shutil.rmtree(case["work"])

    replay = await service.reconcile(case["intent_id"])
    assert replay == committed
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 1


async def test_resolution_receipt_drives_parent_readiness_verification_and_completion(
    db, conflict_resolution_case
):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.integration.models import ConflictResolutionInput
    from src.integration.parent_completion import ParentCompletion
    from src.integration.promotion import PromotionService
    from src.profiles.capabilities import CapabilityPolicy

    case = conflict_resolution_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=[
                "integration_resolve_conflict",
                "integration_push_conflict_resolution",
            ]
        ),
        session_id="resolution-session",
        task_id="repair-task",
        project_id="project",
        profile_id="repairer",
    )
    reservation = ConflictResolutionInput(
        intent_id=case["intent_id"],
        operation_id="resolution-op",
        resolved_head_sha=case["resolved_head"],
        resolved_tree_sha=case["resolved_tree"],
        repair_commit_shas=case["repair_commits"],
        fence=case["resolution_fence"],
    )
    with principal_context(principal):
        await service.reserve_resolution(reservation)
        await service.push_resolution(case["intent_id"], reservation.fence)
    await service.reconcile(case["intent_id"])

    completion = ParentCompletion(db)
    readiness = await completion.readiness("parent")
    assert readiness["outcome"] == "ready"
    assert readiness["head_sha"] == case["resolved_head"]
    assert [receipt["id"] for receipt in readiness["receipts"]] == [case["receipt_id"]]

    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_check_evidence).values(
                id="resolution-aggregate-check",
                operation_id="resolution-op",
                parent_task_id="parent",
                parent_generation=0,
                parent_head_sha=case["resolved_head"],
                producer_id="ci",
                workflow_id="workflow",
                run_id="resolution-run",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=4.0,
            )
        )
    verified = await completion.verify_parent(
        "parent", 0, case["resolved_head"], ["resolution-aggregate-check"]
    )
    assert verified["outcome"] == "verified"

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(
                owner_id="parent",
                owner_role="verifier",
                fence_token=4,
                handoff_state="reserved",
                session_id=None,
                workspace_id=None,
            )
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(branch_owner_id="parent")
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS")
        )
    completed = await completion.complete_parent("parent", 0, case["resolved_head"])

    assert completed["outcome"] == "completed"
    assert (await db.get_task("parent")).status is TaskStatus.COMPLETED
    async with db._engine.connect() as conn:
        pinned = (
            await conn.execute(select(integration_parent_operation_completions))
        ).mappings().one()
    assert pinned["operation_id"] == "resolution-op"
    assert pinned["verification_id"] == verified["verification_id"]
    assert pinned["parent_task_id"] == "parent"
    assert pinned["episode_id"] == "resolution-episode"

    await db.transition_task("parent", TaskStatus.READY, assigned_agent_id=None)
    from src.integration.hierarchy import HierarchyIntegration

    rollover = HierarchyIntegration(
        db,
        checkpoint_verifier=lambda _task, _repo, head: head,
        ancestry_verifier=lambda _repo, ancestor, descendant: (
            ancestor == case["resolved_head"] and descendant == case["resolved_head"]
        ),
    )
    next_episode = await rollover.checkpoint_parent("parent", case["resolved_head"], 1)
    carried_readiness = await rollover.readiness("parent")
    assert carried_readiness["outcome"] == "ready"
    assert [row["id"] for row in carried_readiness["receipts"]] == [case["receipt_id"]]
    async with db._engine.connect() as conn:
        carried = (
            await conn.execute(
                select(integration_episode_receipt_acceptances).where(
                    integration_episode_receipt_acceptances.c.receipt_id == case["receipt_id"]
                )
            )
        ).mappings().one()
        original = (
            await conn.execute(
                select(task_delivery_receipts).where(
                    task_delivery_receipts.c.id == case["receipt_id"]
                )
            )
        ).mappings().one()
    assert carried["operation_id"] == next_episode["operation_id"]
    assert carried["previous_operation_id"] == "resolution-op"
    assert carried["previous_episode_id"] == "resolution-episode"
    assert original["parent_operation_id"] == "resolution-op"
    assert original["parent_episode_id"] == "resolution-episode"


async def test_clean_promotion_preserves_independent_parent_changes(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    work = case["work"]
    _git(["switch", "-c", "parent-advanced", case["base"]], work)
    (work / "parent.txt").write_text("parent-only\n")
    _git(["add", "parent.txt"], work)
    _git(["commit", "-m", "independent parent change"], work)
    target = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "HEAD:refs/heads/aq/parent"], work)

    request = case["request"].model_copy(update={"expected_target": target})
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(request)
    await service.push(prepared.intent_id, case["fence"])

    retained = next((case["data_dir"] / "integration-repositories").glob("*.git"))
    assert _git(["show", "-s", "--format=%P", prepared.prepared_sha], retained) == target
    assert _git(["show", f"{prepared.prepared_sha}:parent.txt"], retained) == "parent-only"
    assert _git(["show", f"{prepared.prepared_sha}:child.txt"], retained) == "one\ntwo"
    assert _git(["rev-list", "--count", f"{target}..{prepared.prepared_sha}"], retained) == "1"


async def test_late_push_marker_cannot_regress_a_committed_intent(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(case["request"])
    await service.push(prepared.intent_id, case["fence"])

    await db.mark_integration_promotion_pushed(prepared.intent_id, {"remote": "late"})
    intent = await db.get_integration_promotion_intent(prepared.intent_id)
    await db.mark_integration_promotion_prepared(
        prepared.intent_id,
        prepared_sha=intent["prepared_sha"],
        recovery_ref=intent["recovery_ref"],
    )

    intent = await db.get_integration_promotion_intent(prepared.intent_id)
    assert intent["state"] == "committed"


class InjectedCrash(RuntimeError):
    pass


class CrashOnce:
    def __init__(self, phase: str):
        self.phase = phase
        self.seen = False

    def __call__(self, phase: str) -> None:
        if phase == self.phase and not self.seen:
            self.seen = True
            raise InjectedCrash(phase)


@pytest.mark.parametrize(
    "phase",
    [
        "after_object",
        "after_recovery_ref",
        "after_prepare",
        "before_push",
        "after_push",
        "before_outbox_ack",
    ],
)
async def test_crash_retries_make_one_squash_and_one_receipt(db, promotion_case, phase):
    from src.integration.promotion import PromotionService

    case = promotion_case
    crashing = PromotionService(
        db,
        data_dir=case["data_dir"],
        git_manager=GitManager(),
        crash_hook=CrashOnce(phase),
    )
    if phase in {"after_object", "after_recovery_ref", "after_prepare"}:
        with pytest.raises(InjectedCrash, match=phase):
            await crashing.prepare(case["request"])
        prepared = await PromotionService(
            db, data_dir=case["data_dir"], git_manager=GitManager()
        ).prepare(case["request"])
    else:
        prepared = await crashing.prepare(case["request"])

    if phase not in {"after_object", "after_recovery_ref", "after_prepare"}:
        with pytest.raises(InjectedCrash, match=phase):
            await crashing.push(prepared.intent_id, case["fence"])

    recovered_service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    if phase == "before_push":
        recovered = await recovered_service.push(prepared.intent_id, case["fence"])
    elif phase in {"after_object", "after_recovery_ref", "after_prepare"}:
        recovered = await recovered_service.push(prepared.intent_id, case["fence"])
    else:
        recovered = await recovered_service.reconcile(prepared.intent_id)
    again = await recovered_service.reconcile(prepared.intent_id)

    assert recovered == again
    remote_tip = _git(
        ["ls-remote", "--heads", "origin", "refs/heads/aq/parent"], case["work"]
    ).split()[0]
    audit = case["data_dir"] / "integration-repositories"
    retained = next(audit.glob("*.git"))
    assert _git(["rev-list", "--count", f"{case['base']}..{remote_tip}"], retained) == "1"
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 1
        assert await conn.scalar(select(func.count()).select_from(integration_outbox)) == 2


async def test_conflict_records_inputs_and_never_creates_a_receipt(db, promotion_case):
    from src.integration.promotion import PromotionConflict, PromotionService

    case = promotion_case
    work = case["work"]
    (work / "shared.txt").write_text("child version\n")
    _git(["add", "shared.txt"], work)
    _git(["commit", "-m", "child conflict"], work)
    source = _git(["rev-parse", "HEAD"], work)
    source_tree = _git(["rev-parse", "HEAD^{tree}"], work)
    _git(["push", "origin", "aq/child"], work)

    _git(["switch", "-c", "aq/parent", case["base"]], work)
    (work / "shared.txt").write_text("parent version\n")
    _git(["commit", "-am", "parent conflict"], work)
    target = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "aq/parent"], work)
    await db.append_integration_review_evidence(
        {
            "id": "conflict-review",
            "source_task_id": "child",
            "repository_id": "repo",
            "source_base": case["base"],
            "reviewed_head_sha": source,
            "reviewed_tree_sha": source_tree,
            "reviewer_task_id": "review-task",
            "reviewer_session_attempt_id": None,
            "review_kind": "leaf",
            "generation": 0,
            "verdict": "approved",
            "evidence": {"checks": ["focused"]},
            "created_at": 3.0,
        }
    )
    request = case["request"].model_copy(update={"source_head": source, "expected_target": target})

    with pytest.raises(PromotionConflict) as caught:
        await PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager()).prepare(
            request
        )

    intent = await db.get_integration_promotion_intent(caught.value.value.intent_id)
    # The identity is reserved before construction, but no delivery receipt
    # exists for a conflict.
    assert caught.value.value.receipt_id == intent["receipt_id"]
    assert intent["state"] == "conflict"
    assert intent["prepared_sha"] is None
    assert intent["conflict_diagnostics"]["base"] == case["base"]
    assert intent["conflict_diagnostics"]["source"] == source
    assert intent["conflict_diagnostics"]["target"] == target
    assert "shared.txt" in intent["conflict_diagnostics"]["paths"]
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 0


def test_conflict_diagnostics_terminates_and_bounds_many_paths():
    script = """
import json
from src.integration.promotion import PromotionService
intent = {"source_base": "a" * 40, "source_head": "b" * 40, "expected_target": "c" * 40}
stdout = "".join(f"100644 100644 deadbeef deadbeef M\\t{'p' * 96}{index}\\n" for index in range(1200))
diagnostics = PromotionService._conflict_diagnostics(intent, stdout, "conflict")
print(json.dumps({
    "size": len(json.dumps(diagnostics, sort_keys=True).encode("utf-8")),
    "truncated": diagnostics["truncated"],
    "path_count": len(diagnostics["paths"]),
}))
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("paths-heavy conflict diagnostics did not terminate")
    result = json.loads(completed.stdout)
    assert result["size"] <= 65536
    assert result["truncated"] is True
    assert result["path_count"] < 1200


async def test_moved_source_is_rejected_even_when_old_review_is_approved(db, promotion_case):
    from src.integration.promotion import PromotionService, PromotionSourceMoved

    case = promotion_case
    (case["work"] / "later.txt").write_text("moved\n")
    _git(["add", "later.txt"], case["work"])
    _git(["commit", "-m", "move source"], case["work"])
    _git(["push", "origin", "aq/child"], case["work"])

    with pytest.raises(PromotionSourceMoved, match="source branch moved"):
        await PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager()).prepare(
            case["request"]
        )


async def test_divergent_target_blocks_push_and_reconcile(db, promotion_case):
    from src.integration.promotion import (
        PromotionInvariantError,
        PromotionService,
        PromotionTargetMoved,
    )

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(case["request"])
    _git(["switch", "-c", "competing", case["base"]], case["work"])
    (case["work"] / "competing.txt").write_text("other\n")
    _git(["add", "competing.txt"], case["work"])
    _git(["commit", "-m", "competing"], case["work"])
    _git(["push", "origin", "HEAD:refs/heads/aq/parent"], case["work"])

    with pytest.raises(PromotionTargetMoved):
        await service.push(prepared.intent_id, case["fence"])
    with pytest.raises(PromotionInvariantError, match="diverged"):
        await service.reconcile(prepared.intent_id)


async def test_stale_initial_target_leaves_no_intent_and_correct_retry_succeeds(
    db, promotion_case
):
    from src.integration.promotion import PromotionService, PromotionTargetMoved

    case = promotion_case
    work = case["work"]
    _git(["switch", "-c", "new-parent-tip", case["base"]], work)
    (work / "parent.txt").write_text("new parent tip\n")
    _git(["add", "parent.txt"], work)
    _git(["commit", "-m", "advance parent"], work)
    target = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "HEAD:refs/heads/aq/parent"], work)
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())

    with pytest.raises(PromotionTargetMoved, match="expected tip"):
        await service.prepare(case["request"])

    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(integration_promotion_intents)) == 0

    correct = case["request"].model_copy(update={"expected_target": target})
    prepared = await service.prepare(correct)
    promoted = await service.push(prepared.intent_id, correct.fence)
    assert promoted.receipt_id == prepared.receipt_id


async def test_concurrent_same_domain_reuses_deterministic_preparation(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    first, second = await asyncio.gather(
        service.prepare(case["request"]),
        service.prepare(case["request"]),
    )
    assert first == second


async def test_retained_ref_survives_disposable_source_checkout_cleanup(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(case["request"])
    shutil.rmtree(case["work"])

    result = await service.push(prepared.intent_id, case["fence"])
    retained = next((case["data_dir"] / "integration-repositories").glob("*.git"))
    assert result.prepared_sha == _git(
        ["rev-parse", f"refs/aq/integration-intents/{prepared.intent_id}"], retained
    )


async def test_review_generation_comes_from_source_checkpoint_not_branch_origin(db, promotion_case):
    from src.database.tables import task_integration_checkpoints
    from src.integration.promotion import PromotionService, PromotionSourceMoved

    case = promotion_case
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="child",
                repository_id="repo",
                branch="aq/child",
                generation=1,
                state="working",
                version=1,
                updated_at=4.0,
            )
        )
    with pytest.raises(PromotionSourceMoved, match="review evidence"):
        await PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager()).prepare(
            case["request"]
        )


async def test_repository_origin_change_blocks_a_prepared_push(db, promotion_case):
    from src.integration.promotion import PromotionInvariantError, PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(case["request"])
    await db.update_repo("repo", url="/different/origin.git")

    with pytest.raises(PromotionInvariantError, match="repository identity changed"):
        await service.push(prepared.intent_id, case["fence"])


async def test_review_evidence_rows_are_append_only(db):
    from src.database.tables import integration_review_evidence
    from sqlalchemy import delete, update

    await db.append_integration_review_evidence(_review(evidence_id="immutable", generation=0))
    async with db.immediate() as conn:
        with pytest.raises((IntegrityError, DBAPIError)):
            await conn.execute(
                update(integration_review_evidence)
                .where(integration_review_evidence.c.id == "immutable")
                .values(verdict="rejected")
            )
    async with db.immediate() as conn:
        with pytest.raises((IntegrityError, DBAPIError)):
            await conn.execute(
                delete(integration_review_evidence).where(
                    integration_review_evidence.c.id == "immutable"
                )
            )


class AmbiguousPushOnceGitManager(GitManager):
    def __init__(self):
        super().__init__()
        self.ambiguous = True

    async def apush_expected_delivery(self, *args, **kwargs):
        result = await super().apush_expected_delivery(*args, **kwargs)
        if self.ambiguous:
            self.ambiguous = False
            raise GitError("simulated lost response after accepted push")
        return result


async def test_delivery_promote_replays_ambiguous_accepted_push_without_intent_id(
    db, promotion_case, command_handler_factory
):
    from src.integration.promotion import PromotionService

    handler = await command_handler_factory()
    await handler.orchestrator.db.close()
    handler.orchestrator.db = db
    handler.orchestrator.promotion_service = PromotionService(
        db,
        data_dir=promotion_case["data_dir"],
        git_manager=AmbiguousPushOnceGitManager(),
    )
    args = promotion_case["request"].model_dump(mode="json")

    first = await handler.execute("delivery_promote", args)
    async with db._engine.connect() as conn:
        reserved_receipt = await conn.scalar(select(integration_promotion_intents.c.receipt_id))
    replay = await handler.execute("delivery_promote", args)
    again = await handler.execute("delivery_promote", args)

    assert first["outcome"] == "runtime_error"
    assert "intent_id" not in first
    assert replay["outcome"] == "promoted"
    assert again["outcome"] == "already_promoted"
    assert replay["intent_id"] == again["intent_id"]
    assert replay["receipt_id"] == again["receipt_id"] == reserved_receipt
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 1
        assert await conn.scalar(select(func.count()).select_from(integration_outbox)) == 2


async def test_completed_delivery_replay_ignores_old_fence_and_deleted_source_branch(
    db, promotion_case, command_handler_factory
):
    from src.integration.promotion import PromotionService

    case = promotion_case
    handler = await command_handler_factory()
    await handler.orchestrator.db.close()
    handler.orchestrator.db = db
    handler.orchestrator.promotion_service = PromotionService(
        db, data_dir=case["data_dir"], git_manager=GitManager()
    )
    args = case["request"].model_dump(mode="json")
    promoted = await handler.execute("delivery_promote", args)
    await BranchOwnership(db).transfer(case["fence"], "next-owner", "collector")
    _git(["push", "origin", ":refs/heads/aq/child"], case["work"])
    refs_before = _git(["ls-remote", "origin"], case["work"])
    handler.orchestrator.promotion_service = PromotionService(
        db, data_dir=case["data_dir"], git_manager=object()
    )

    replay = await handler.execute("delivery_promote", args)

    assert promoted["outcome"] == "promoted"
    assert replay["outcome"] == "already_promoted"
    assert replay["intent_id"] == promoted["intent_id"]
    assert replay["receipt_id"] == promoted["receipt_id"]
    assert _git(["ls-remote", "origin"], case["work"]) == refs_before


async def test_actual_push_holds_collector_fence_against_transfer(db, promotion_case):
    """A handoff cannot win between the last role check and remote mutation."""
    from src.integration.promotion import PromotionService

    entered = asyncio.Event()
    release = asyncio.Event()

    async def hook(phase: str):
        if phase == "before_push":
            entered.set()
            await release.wait()

    case = promotion_case
    service = PromotionService(
        db,
        data_dir=case["data_dir"],
        git_manager=GitManager(),
        crash_hook=hook,
    )
    prepared = await service.prepare(case["request"])
    pushing = asyncio.create_task(service.push(prepared.intent_id, case["fence"]))
    await entered.wait()
    transferring = asyncio.create_task(
        BranchOwnership(db).transfer(case["fence"], "next-owner", "collector")
    )
    await asyncio.sleep(0.05)
    assert not transferring.done()

    release.set()
    promoted = await pushing
    successor = await transferring

    assert promoted.prepared_sha
    assert successor.owner_id == "next-owner"


async def _seed_conflict_resolution_writer(handler) -> tuple[dict, object]:
    from src.commands.principal import ExecutionPrincipal, PrincipalKind
    from src.profiles.capabilities import CapabilityPolicy

    await _seed_handler_delivery(handler)
    intent_values = {
        "id": "conflicted-intent",
        "domain_key": "conflicted-domain",
        "operation_key": "collector-op",
        "project_id": "p",
        "receipt_id": "conflicted-receipt",
        "source_task_id": "child",
        "target_task_id": "parent",
        "source_head": "b" * 40,
        "source_base": "a" * 40,
        "repository_id": "repo",
        "origin_url": "/configured/origin.git",
        "target_branch": "aq/parent",
        "expected_target": "c" * 40,
        "fence_owner_id": "collector-op",
        "fence_token": 1,
        "review_evidence": _review(evidence_id="approved", generation=0),
        "authors": [],
        "provenance": {"principal": "service:collector", "source_branch": "aq/child"},
        "commit_metadata": {"message": "clean promotion"},
        "created_at": 1.0,
    }
    await handler.db.reserve_integration_promotion_intent(intent_values)
    await handler.db.mark_integration_promotion_conflict(
        "conflicted-intent", {"paths": ["shared.txt"]}
    )
    await handler.db.create_task(
        Task(
            id="repair-task",
            project_id="p",
            title="Repair",
            description="",
            status=TaskStatus.IN_PROGRESS,
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="collector-op",
                ordinal=0,
                policy={},
                repair_task_id="repair-task",
                writer_kind="repair_delegate",
                starting_sha="c" * 40,
                deadline_at=4_000_000_000.0,
                attempts=1,
                state="active",
            )
        )
        await conn.execute(
            insert(workspaces).values(
                id="repair-workspace",
                project_id="p",
                workspace_path="/tmp/repair-resolution",
                source_type="link",
                locked_by_task_id="repair-task",
                enabled=True,
                created_at=2.0,
            )
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(
                owner_id="repair-task",
                owner_role="repair",
                fence_token=2,
                handoff_state="attached",
                session_id="repair-session",
                workspace_id="repair-workspace",
            )
        )
    await handler.db.create_session(
        SessionRecord(
            id="repair-session",
            task_id="repair-task",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="s-repair",
            lifecycle="task",
            state="running",
            work_dir="/tmp/repair-resolution",
            epoch="epoch",
            instance_token="instance",
            started_at=2.0,
        )
    )
    args = {
        "intent_id": "conflicted-intent",
        "operation_id": "collector-op",
        "resolved_head_sha": "e" * 40,
        "resolved_tree_sha": "f" * 40,
        "repair_commit_shas": ["1" * 40, "e" * 40],
        "fence": {
            "target": {"repository_id": "repo", "branch": "aq/parent"},
            "owner_id": "repair-task",
            "token": 2,
        },
    }
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=[
                "integration_resolve_conflict",
                "integration_push_conflict_resolution",
            ]
        ),
        session_id="repair-session",
        task_id="repair-task",
        project_id="p",
        profile_id="repairer",
    )
    return args, principal


async def test_resolve_conflict_command_is_session_only_and_replays_exact_identity(
    command_handler_factory,
):
    from src.commands.principal import principal_context
    from src.integration.promotion import PromotionService

    handler = await command_handler_factory()
    args, principal = await _seed_conflict_resolution_writer(handler)
    handler.orchestrator.promotion_service = PromotionService(
        handler.db, data_dir=handler.config.data_dir
    )

    local = await handler.execute("integration_resolve_conflict", args)
    with principal_context(principal):
        reserved = await handler.execute("integration_resolve_conflict", args)
        replay = await handler.execute("integration_resolve_conflict", args)
        changed = await handler.execute(
            "integration_resolve_conflict",
            args | {"resolved_tree_sha": "2" * 40},
        )
        stale_push = await handler.execute(
            "integration_push_conflict_resolution",
            {
                "intent_id": args["intent_id"],
                "fence": args["fence"] | {"token": 1},
            },
        )
    unauthorized_push = await handler.execute(
        "integration_push_conflict_resolution",
        {"intent_id": args["intent_id"], "fence": args["fence"]},
    )

    assert local["outcome"] == "unauthorized"
    assert reserved["outcome"] == "reserved"
    assert replay["outcome"] == "already_reserved"
    assert changed["outcome"] == "invariant_error"
    assert stale_push["outcome"] == "stale"
    assert unauthorized_push["outcome"] == "unauthorized"
    assert reserved["intent_id"] == replay["intent_id"] == "conflicted-intent"
    assert reserved["receipt_id"] == replay["receipt_id"] == "conflicted-receipt"
    intent = await handler.db.get_integration_promotion_intent("conflicted-intent")
    assert intent["resolution_operation_id"] == "collector-op"
    assert intent["resolution_stage_ordinal"] == 0
    assert intent["resolution_task_id"] == "repair-task"
    assert intent["resolution_session_id"] == "repair-session"
    assert intent["resolution_session_instance_token"] == "instance"
    assert intent["resolution_workspace_id"] == "repair-workspace"
    assert intent["resolution_fence_token"] == 2


async def _seed_handler_delivery(handler) -> dict:
    from src.integration.models import PromotionValue

    await handler.db.create_project(Project(id="p", name="Project"))
    await handler.db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="/configured/origin.git",
        )
    )
    await handler.db.create_task(
        Task(
            id="parent",
            project_id="p",
            title="Parent",
            description="",
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="collector-op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                checkpoint_sha="a" * 40,
                generation=0,
                episode_id="episode",
                state="awaiting_children",
                version=0,
                updated_at=1.0,
            )
        )
    await BranchOwnership(handler.db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"),
        "collector-op",
        "collector",
    )
    await handler.db.create_task(
        Task(
            id="child",
            project_id="p",
            title="Child",
            description="",
            parent_task_id="parent",
            repo_id="repo",
            branch_name="aq/child",
        )
    )
    value = PromotionValue(intent_id="intent", receipt_id="receipt", prepared_sha="d" * 40)
    service = type("FakePromotion", (), {})()
    service.prepare = AsyncMock(return_value=value)
    service.push = AsyncMock(return_value=value)
    service.reconcile = AsyncMock(return_value=value)
    handler.orchestrator.promotion_service = service
    return {
        "operation_key": "activation",
        "source_task_id": "child",
        "source_head": "b" * 40,
        "source_base": "a" * 40,
        "expected_target": "c" * 40,
        "fence": {
            "target": {"repository_id": "repo", "branch": "aq/parent"},
            "owner_id": "collector-op",
            "token": 1,
        },
    }


async def test_local_handler_invokes_injected_promotion_service(command_handler_factory):
    handler = await command_handler_factory()
    args = await _seed_handler_delivery(handler)

    result = await handler.execute("delivery_promote", args)

    assert result == {
        "success": True,
        "outcome": "promoted",
        "intent_id": "intent",
        "receipt_id": "receipt",
        "prepared_sha": "d" * 40,
    }


async def test_session_cannot_promote_even_when_capability_audit_would_allow(
    command_handler_factory,
):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import CapabilityPolicy

    handler = await command_handler_factory()
    args = await _seed_handler_delivery(handler)
    handler.config.security.capability_enforcement = "audit"
    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["delivery_promote"], derived_from_legacy=True
        ),
        project_id="p",
        task_id="child",
        session_id="worker",
    )

    with principal_context(session):
        result = await handler.execute("delivery_promote", args)

    assert result["outcome"] == "unauthorized"
    handler.orchestrator.promotion_service.prepare.assert_not_awaited()


async def test_playbook_project_scope_cannot_be_mixed_with_another_promotion(
    command_handler_factory,
):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import CapabilityPolicy

    handler = await command_handler_factory()
    args = await _seed_handler_delivery(handler)
    await handler.db.create_project(Project(id="other", name="Other"))
    playbook = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["delivery_promote"]),
        project_id="other",
    )

    with principal_context(playbook):
        result = await handler.execute("delivery_promote", args)

    assert result["outcome"] == "unauthorized"
    handler.orchestrator.promotion_service.prepare.assert_not_awaited()
