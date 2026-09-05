"""Trusted real-review verdict production for integration sources."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from src.database import Database
from src.database.tables import (
    integration_review_evidence,
    task_branch_origins,
    task_integration_checkpoints,
    task_session_attempts,
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
    Task,
    TaskStatus,
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
        await conn.execute(
            insert(task_session_attempts).values(
                id="attempt",
                session_id="session",
                task_id="review",
                project_id="p",
                agent_id="agent",
                agent_name="Agent",
                profile_id="reviewer",
                name="review-session",
                lifecycle="task",
                harness="claude",
                provider="fake",
                state="running",
                work_dir=str(work),
                started_at=2.0,
                session_started_at=2.0,
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


async def test_leaf_real_reviewer_approval_is_atomic_and_drives_promotion(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    evidence = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="approved", summary="looks good"
    )
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
    approved = await db.get_applicable_integration_review_evidence(
        source_task_id="leaf",
        repository_id="repo",
        source_base=case["base"],
        reviewed_head_sha=case["head"],
        current_generation=3,
    )
    assert approved and approved["reviewer_session_attempt_id"] == "attempt"

    fence = await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"), "collector", "collector"
    )
    promoted = await case["promotion"].prepare(
        PromotionInput(
            operation_key="parent-episode",
            source_task_id="leaf",
            source_head=case["head"],
            source_base=case["base"],
            expected_target=case["base"],
            fence=fence,
        )
    )
    assert (await case["promotion"].push(promoted.intent_id, fence)).receipt_id


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
                select(integration_review_evidence).where(
                    integration_review_evidence.c.reviewer_session_attempt_id == "attempt"
                )
            )
        ).mappings().all()
    assert [row["verdict"] for row in rows] == ["rejected"]
    assert (await db.get_task("leaf")).status is TaskStatus.READY
    assert (await db.get_task("review")).status is TaskStatus.COMPLETED


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
