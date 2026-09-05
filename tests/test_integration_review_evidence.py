"""Trusted real-review verdict production for integration sources."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, select, update

from src.database import Database
from src.database.tables import (
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_review_evidence,
    task_branch_origins,
    task_dependencies,
    task_integration_checkpoints,
    task_session_attempts,
    tasks,
)
from src.git.manager import GitManager
from src.integration.models import BranchKey, PromotionInput
from src.integration.ownership import BranchOwnership
from src.integration.promotion import PromotionService
from src.integration.review_evidence import ReviewEvidenceProducer
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


def _git(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
async def review_case(tmp_path):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "--initial-branch=main", str(remote)])
    _git(["clone", str(remote), str(work)])
    _git(["config", "user.name", "Reviewer Test"], work)
    _git(["config", "user.email", "review@example.test"], work)
    (work / "base.txt").write_text("base\n")
    _git(["add", "base.txt"], work)
    _git(["commit", "-m", "base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "main"], work)
    _git(["switch", "-c", "aq/leaf"], work)
    (work / "leaf.txt").write_text("finished\n")
    _git(["add", "leaf.txt"], work)
    _git(["commit", "-m", "leaf work"], work)
    head = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "aq/leaf"], work)
    _git(["push", "origin", f"{base}:refs/heads/aq/parent"], work)

    db = Database(str(tmp_path / "review.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(remote),
        )
    )
    await db.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
    )
    await db.create_profile(AgentProfile(id="reviewer", name="Reviewer", harness="claude"))
    await db.create_agent(
        Agent(
            id="agent",
            name="Agent",
            profile_id="reviewer",
            state=AgentState.IDLE,
        )
    )
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="parent",
            description="",
        )
    )
    await db.create_task(
        Task(
            id="leaf",
            project_id="p",
            parent_task_id="parent",
            repo_id="repo",
            branch_name="aq/leaf",
            title="leaf",
            description="",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.create_task(
        Task(
            id="review",
            project_id="p",
            profile_id="reviewer",
            assigned_agent_id="agent",
            title="review",
            description="",
            status=TaskStatus.IN_PROGRESS,
            claim_epoch=4,
        )
    )
    await db.update_task("review", claim_epoch=4)
    await db.update_agent("agent", state=AgentState.BUSY, current_task_id="review")
    await db.add_dependency("review", "leaf", "discovered-from")
    await db.create_session(
        SessionRecord(
            id="session",
            project_id="p",
            profile_id="reviewer",
            harness="claude",
            provider="fake",
            name="review-session",
            lifecycle="task",
            work_dir=str(work),
            epoch="review-epoch",
            instance_token="review-token",
            started_at=2.0,
            last_activity=2.0,
            task_id="review",
            agent_id="agent",
            state="running",
            desired_state="running",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-leaf",
                task_id="leaf",
                repository_id="repo",
                parent_task_id="parent",
                parent_repository_id="repo",
                parent_ref="aq/parent",
                base_sha=base,
                creation_generation=1,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="leaf",
                repository_id="repo",
                branch="aq/leaf",
                checkpoint_sha=head,
                generation=3,
                state="working",
                version=1,
                updated_at=1.0,
            )
        )
    session = SimpleNamespace(
        id="session",
        task_id="review",
        project_id="p",
        profile_id="reviewer",
        agent_id="agent",
        state="running",
    )
    promotion = PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager())
    yield {
        "db": db,
        "promotion": promotion,
        "session": session,
        "base": base,
        "head": head,
        "work": work,
    }
    await db.close()


async def test_leaf_close_review_hook_and_delivery_promote_command_end_to_end(
    review_case, command_handler_factory, monkeypatch
):
    from unittest.mock import AsyncMock

    from src.models import PhaseResult

    case = review_case
    db = case["db"]
    await db.create_profile(
        AgentProfile(id="worker", name="Worker", harness="claude", needs_workspace=True)
    )
    await db.create_agent(
        Agent(id="worker-agent", name="Worker", profile_id="worker", state=AgentState.BUSY)
    )
    await db.transition_task("leaf", TaskStatus.READY, assigned_agent_id=None)
    await db.transition_task(
        "leaf", TaskStatus.IN_PROGRESS, assigned_agent_id="worker-agent", claim_epoch=1
    )
    await db.update_agent("worker-agent", current_task_id="leaf")
    await db.create_workspace(
        Workspace(
            id="leaf-workspace",
            project_id="p",
            workspace_path=str(case["work"]),
            source_type=RepoSourceType.CLONE,
            locked_by_agent_id="worker-agent",
            locked_by_task_id="leaf",
        )
    )
    await db.create_session(
        SessionRecord(
            id="leaf-session",
            project_id="p",
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="leaf-session",
            lifecycle="task",
            work_dir=str(case["work"]),
            epoch="leaf-epoch",
            instance_token="leaf-token",
            started_at=time.time(),
            last_activity=time.time(),
            task_id="leaf",
            agent_id="worker-agent",
            state="running",
            desired_state="running",
        )
    )

    handler = await command_handler_factory()
    await handler.orchestrator.db.close()
    handler.orchestrator.db = db
    handler.orchestrator.git = GitManager()
    handler.orchestrator.promotion_service = case["promotion"]
    monkeypatch.setattr(
        handler.orchestrator,
        "_phase_verify",
        AsyncMock(return_value=PhaseResult.CONTINUE),
    )
    monkeypatch.setattr(
        handler.orchestrator,
        "_run_completion_pipeline",
        AsyncMock(return_value=(None, True)),
    )
    monkeypatch.setattr(
        handler.orchestrator, "_get_default_branch", AsyncMock(return_value="main")
    )

    leaf_close = await handler.execute(
        "task_close",
        {
            "task_id": "leaf",
            "session_id": "leaf-session",
            "outcome": "pass",
            "work_outcome": "shipped",
            "summary": "leaf complete",
        },
    )
    assert leaf_close["success"] is True
    assert (await db.get_task("leaf")).status is TaskStatus.COMPLETED
    checkpoint = await db.get_integration_checkpoint("leaf")
    assert checkpoint["checkpoint_sha"] == case["head"]

    review_close = await handler.execute(
        "task_close",
        {
            "task_id": "review",
            "session_id": "session",
            "outcome": "pass",
            "work_outcome": "no-op",
            "summary": "looks good",
        },
    )
    assert review_close["success"] is True, review_close
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-parent",
                task_id="parent",
                repository_id="repo",
                parent_task_id=None,
                parent_repository_id="repo",
                parent_ref="main",
                base_sha=case["base"],
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="parent-episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha=case["base"],
                created_at=3.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="parent-operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="parent-episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=3.0,
                updated_at=3.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                checkpoint_sha=case["base"],
                generation=0,
                episode_id="parent-episode",
                state="awaiting_children",
                version=0,
                updated_at=3.0,
            )
        )

    assert (await db.get_task("review")).status is TaskStatus.COMPLETED
    approved = await db.get_applicable_integration_review_evidence(
        source_task_id="leaf",
        repository_id="repo",
        source_base=case["base"],
        reviewed_head_sha=case["head"],
        current_generation=checkpoint["generation"],
    )
    assert approved and approved["reviewer_session_attempt_id"]

    fence = await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"),
        "parent-operation",
        "collector",
    )
    promoted = await handler.execute(
        "delivery_promote",
        PromotionInput(
            operation_key="parent-episode",
            source_task_id="leaf",
            source_head=case["head"],
            source_base=case["base"],
            expected_target=case["base"],
            fence=fence,
        ).model_dump(mode="json"),
    )
    assert promoted["success"] is True
    assert promoted["outcome"] == "promoted"
    receipts = await db.list_integration_delivery_receipts(
        source_task_id="leaf", repository_id="repo", target_branch="aq/parent"
    )
    assert receipts[0]["parent_operation_id"] == "parent-operation"
    assert receipts[0]["parent_episode_id"] == "parent-episode"


async def test_reject_then_successful_review_close_never_mints_approval(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    rejected = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="rejected", feedback="fix it"
    )
    async with db.immediate() as conn:
        await producer.reject_and_reopen_on(
            conn,
            "leaf",
            "review",
            rejected,
            context="reopen_with_feedback",
            assigned_agent_id=None,
        )
    approved = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="approved", summary="review closed"
    )
    async with db.immediate() as conn:
        await producer.complete_review_on(
            conn, "review", approved, context="session_close", assigned_agent_id=None
        )

    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(integration_review_evidence)
            )
        ).mappings().all()
    assert [row["verdict"] for row in rows] == ["rejected"]
    assert (await db.get_task("leaf")).status is TaskStatus.READY
    assert (await db.get_task("review")).status is TaskStatus.COMPLETED


async def test_parent_review_rejection_rolls_completed_episode_for_next_collection(
    review_case,
):
    case = review_case
    db = case["db"]
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-parent",
                task_id="parent",
                repository_id="repo",
                parent_task_id=None,
                parent_repository_id="repo",
                parent_ref="main",
                base_sha=case["base"],
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
        await conn.execute(
            delete(task_dependencies).where(task_dependencies.c.task_id == "review")
        )
        await conn.execute(
            insert(task_dependencies).values(
                task_id="review",
                depends_on_task_id="parent",
                dep_type="discovered-from",
            )
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="completed-episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha=case["base"],
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="completed-operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="completed-episode",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="completed-verification",
                operation_id="completed-operation",
                parent_task_id="parent",
                episode_id="completed-episode",
                generation=0,
                head_sha=case["base"],
                required_check_version="test",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="completed-operation",
                verification_id="completed-verification",
                parent_task_id="parent",
                episode_id="completed-episode",
                completed_at=2.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                checkpoint_sha=case["base"],
                verified_sha=case["base"],
                verified_generation=0,
                generation=0,
                episode_id="completed-episode",
                current_verification_id="completed-verification",
                last_completed_operation_id="completed-operation",
                last_completed_verification_id="completed-verification",
                state="verifying",
                version=1,
                updated_at=2.0,
            )
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status="COMPLETED")
        )
    await db.append_integration_review_evidence(
        {
            "id": "unrelated-old-rejection",
            "source_task_id": "parent",
            "repository_id": "repo",
            "source_base": case["base"],
            "reviewed_head_sha": "f" * 40,
            "reviewed_tree_sha": "e" * 40,
            "reviewer_task_id": "historic-review",
            "reviewer_session_attempt_id": None,
            "review_kind": "parent",
            "generation": 99,
            "verdict": "rejected",
            "evidence": {"historic": True},
            "created_at": 1.0,
        }
    )

    producer = ReviewEvidenceProducer(db, case["promotion"])
    rejected = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="rejected"
    )
    async with db.immediate() as conn:
        await producer.reject_and_reopen_on(
            conn,
            "parent",
            "review",
            rejected,
            context="reopen_with_feedback",
            assigned_agent_id=None,
        )

    checkpoint = await db.get_integration_checkpoint("parent")
    assert (await db.get_task("parent")).status is TaskStatus.READY
    assert checkpoint["generation"] == 1
    assert checkpoint["episode_id"] is None
    assert checkpoint["current_verification_id"] is None
    assert checkpoint["last_completed_operation_id"] == "completed-operation"
    assert checkpoint["last_completed_verification_id"] == "completed-verification"
    async with db._engine.connect() as conn:
        completion = (
            await conn.execute(select(integration_parent_operation_completions))
        ).mappings().one()
    assert completion["verification_id"] == "completed-verification"


async def test_disabled_project_review_keeps_legacy_close_without_git_observation(review_case):
    case = review_case
    await case["db"].update_project("p", hierarchical_integration_mode="disabled")

    class MustNotResolveRepository:
        async def _resolve_repository(self, _repository_id):
            raise AssertionError("disabled legacy review reached integration Git observation")

    evidence = await ReviewEvidenceProducer(
        case["db"], MustNotResolveRepository()
    ).snapshot(
        await case["db"].get_task("review"),
        case["session"],
        verdict="approved",
        summary="legacy review",
    )

    assert evidence is None


async def test_approval_transaction_crash_rolls_back_evidence_and_transition(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    evidence = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="approved"
    )

    with pytest.raises(RuntimeError, match="crash after writes"):
        async with db.immediate() as conn:
            await producer.complete_review_on(
                conn,
                "review",
                evidence,
                context="session_close",
                assigned_agent_id=None,
                expect_claim_epoch=4,
            )
            raise RuntimeError("crash after writes")

    assert (await db.get_task("review")).status is TaskStatus.IN_PROGRESS
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(integration_review_evidence.c.id).where(
                    integration_review_evidence.c.id == evidence["id"]
                )
            )
        ).first() is None
    async with db.immediate() as conn:
        await producer.complete_review_on(
            conn,
            "review",
            evidence,
            context="session_close",
            assigned_agent_id=None,
            expect_claim_epoch=4,
        )
    assert (await db.get_task("review")).status is TaskStatus.COMPLETED


async def test_rejection_transaction_crash_rolls_back_evidence_and_reopen(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    evidence = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="rejected", feedback="fix"
    )

    with pytest.raises(RuntimeError, match="crash after writes"):
        async with db.immediate() as conn:
            await producer.reject_and_reopen_on(
                conn,
                "leaf",
                "review",
                evidence,
                context="reopen_with_feedback",
                assigned_agent_id=None,
            )
            raise RuntimeError("crash after writes")

    assert (await db.get_task("leaf")).status is TaskStatus.COMPLETED
    async with db.immediate() as conn:
        await producer.reject_and_reopen_on(
            conn,
            "leaf",
            "review",
            evidence,
            context="reopen_with_feedback",
            assigned_agent_id=None,
        )
    assert (await db.get_task("leaf")).status is TaskStatus.READY


async def test_approval_snapshot_is_stale_after_another_reviewer_rejects(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    stale_approval = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="approved"
    )
    await db.create_agent(
        Agent(id="agent-2", name="Agent 2", profile_id="reviewer", state=AgentState.BUSY)
    )
    await db.create_task(
        Task(
            id="review-2",
            project_id="p",
            profile_id="reviewer",
            assigned_agent_id="agent-2",
            title="review 2",
            description="",
            status=TaskStatus.IN_PROGRESS,
            claim_epoch=1,
        )
    )
    await db.update_agent("agent-2", current_task_id="review-2")
    await db.add_dependency("review-2", "leaf", "discovered-from")
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_session_attempts).values(
                id="attempt-2",
                session_id="session-2",
                task_id="review-2",
                project_id="p",
                agent_id="agent-2",
                agent_name="Agent 2",
                profile_id="reviewer",
                name="review-session-2",
                lifecycle="task",
                harness="claude",
                provider="fake",
                state="running",
                work_dir=str(case["work"]),
                started_at=3.0,
                session_started_at=3.0,
            )
        )
    session_2 = SimpleNamespace(
        id="session-2",
        task_id="review-2",
        project_id="p",
        profile_id="reviewer",
        agent_id="agent-2",
        state="running",
    )
    rejected = await producer.snapshot(
        await db.get_task("review-2"), session_2, verdict="rejected", feedback="stale"
    )
    async with db.immediate() as conn:
        await producer.reject_and_reopen_on(
            conn,
            "leaf",
            "review-2",
            rejected,
            context="reopen_with_feedback",
            assigned_agent_id=None,
        )

    with pytest.raises(Exception, match="review snapshot changed"):
        async with db.immediate() as conn:
            await producer.complete_review_on(
                conn,
                "review",
                stale_approval,
                context="session_close",
                assigned_agent_id=None,
                expect_claim_epoch=4,
            )
