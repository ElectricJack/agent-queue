"""A GitHub pull-request verdict becomes exact train review evidence."""

from __future__ import annotations

import subprocess

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_review_evidence,
    project_integration_schedules,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.promotion import PromotionService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.scheduler import TrainService
from src.integration.settling import settled
from src.models import Project, RepoConfig, RepoSourceType
from tests.db_fixtures import lease_dsn


def _git(*args: str, cwd=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
async def case(tmp_path):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git("init", "--bare", "--initial-branch=main", str(remote))
    _git("clone", str(remote), str(work))
    _git("config", "user.name", "GitHub Review Test", cwd=work)
    _git("config", "user.email", "review@example.test", cwd=work)
    (work / "source.txt").write_text("base\n")
    _git("add", "source.txt", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    base = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "origin", "main", cwd=work)
    _git("switch", "-c", "aq/epic/retire-the-publisher", cwd=work)
    (work / "source.txt").write_text("first\n")
    _git("commit", "-am", "first epic head", cwd=work)
    first = _git("rev-parse", "HEAD", cwd=work)
    tree = _git("rev-parse", "HEAD^{tree}", cwd=work)
    _git("push", "origin", "HEAD", cwd=work)
    (work / "source.txt").write_text("second\n")
    _git("commit", "-am", "second epic head", cwd=work)
    second = _git("rev-parse", "HEAD", cwd=work)

    db = Database(lease_dsn("epic-pr-review-evidence.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="PR review project"))
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote))
    )
    await db.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        integration_mode="pull_request",
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(project_integration_schedules).values(
                project_id="p", interval_seconds=300, next_due_at=1300.0, updated_at=1000.0
            )
        )
        await conn.execute(
            insert(tasks).values(
                id="e1",
                project_id="p",
                repo_id="repo",
                title="Epic",
                description="",
                status="COMPLETED",
                branch_name="aq/epic/retire-the-publisher",
                pr_url="https://github.com/o/r/pull/7",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(tasks).values(
                id="c1",
                project_id="p",
                repo_id="repo",
                title="Child",
                description="",
                status="COMPLETED",
                parent_task_id="e1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-e1",
                task_id="e1",
                repository_id="repo",
                base_sha=base,
                creation_generation=1,
                reserved=True,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-e1",
                parent_task_id="e1",
                repository_id="repo",
                generation=1,
                pre_collection_checkpoint_sha=base,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-e1",
                target_kind="parent",
                parent_task_id="e1",
                episode_id="episode-e1",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1},
                artifact_snapshot={"version": 1},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-e1",
                operation_id="operation-e1",
                parent_task_id="e1",
                episode_id="episode-e1",
                generation=1,
                head_sha=first,
                required_check_version="checks-v1",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-e1",
                verification_id="verification-e1",
                parent_task_id="e1",
                episode_id="episode-e1",
                completed_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="e1",
                repository_id="repo",
                branch="aq/epic/retire-the-publisher",
                checkpoint_sha=first,
                generation=1,
                verified_sha=first,
                verified_generation=1,
                episode_id="episode-e1",
                current_verification_id="verification-e1",
                last_completed_operation_id="operation-e1",
                last_completed_verification_id="verification-e1",
                updated_at=1.0,
            )
        )
    promotion = PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager())
    yield {
        "db": db,
        "producer": ReviewEvidenceProducer(db, promotion, clock=lambda: 1000.0),
        "work": work,
        "first": first,
        "second": second,
        "tree": tree,
        "base": base,
    }
    await db.close()


async def _rows(db):
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_review_evidence))).mappings().all()
    return [dict(row) for row in rows]


async def test_approval_writes_exact_trusted_evidence_and_is_eligible(case):
    evidence = await case["producer"].snapshot_from_pull_request(
        "e1",
        verdict="approved",
        reviewer_login="jkern",
        reviewed_sha=case["first"],
        summary="Looks good",
    )
    assert evidence == (await _rows(case["db"]))[0]
    assert {
        key: evidence[key]
        for key in (
            "source_task_id",
            "repository_id",
            "source_base",
            "reviewed_head_sha",
            "reviewed_tree_sha",
            "reviewer_task_id",
            "reviewer_session_attempt_id",
            "reviewer_identity",
            "review_kind",
            "generation",
            "verdict",
        )
    } == {
        "source_task_id": "e1",
        "repository_id": "repo",
        "source_base": case["base"],
        "reviewed_head_sha": case["first"],
        "reviewed_tree_sha": case["tree"],
        "reviewer_task_id": None,
        "reviewer_session_attempt_id": None,
        "reviewer_identity": "github:jkern",
        "review_kind": "parent",
        "generation": 1,
        "verdict": "approved",
    }
    assert evidence["evidence"] == {
        "decision_path": "github_pull_request",
        "summary": "Looks good",
        "feedback": "",
        "reviewed_sha": case["first"],
        "pr_url": "https://github.com/o/r/pull/7",
        "verification_id": "verification-e1",
    }
    async with case["db"].immediate() as conn:
        assert await settled(conn, project_id="p", now=1299.0) is False
        assert await settled(conn, project_id="p", now=1300.0) is True
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert [member["task_id"] for member in members] == ["e1"]
    assert members[0]["review"] == evidence


async def test_approval_of_moved_head_is_not_reused(case):
    first = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    _git("push", "origin", "HEAD", cwd=case["work"])
    async with case["db"].immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-e1-next",
                parent_task_id="e1",
                repository_id="repo",
                generation=2,
                pre_collection_checkpoint_sha=case["first"],
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-e1-next",
                target_kind="parent",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1},
                artifact_snapshot={"version": 1},
                required_check_version="checks-v1",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-e1-next",
                operation_id="operation-e1-next",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                generation=2,
                head_sha=case["second"],
                required_check_version="checks-v1",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-e1-next",
                verification_id="verification-e1-next",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                completed_at=2.0,
            )
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "e1")
            .values(
                generation=2,
                checkpoint_sha=case["second"],
                verified_sha=case["second"],
                verified_generation=2,
                episode_id="episode-e1-next",
                current_verification_id="verification-e1-next",
                last_completed_operation_id="operation-e1-next",
                last_completed_verification_id="verification-e1-next",
            )
        )
    async with case["db"].immediate() as conn:
        candidates = await case["db"].eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=10
        )
        assert await case["db"].latest_exact_reviews_on(conn, candidates) == {}
    second = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["second"]
    )
    assert len(await _rows(case["db"])) == 2
    assert first["reviewed_head_sha"] == case["first"]
    assert second["reviewed_head_sha"] == case["second"]


async def test_rejection_records_feedback_without_arming_window(case):
    evidence = await case["producer"].snapshot_from_pull_request(
        "e1",
        verdict="rejected",
        reviewer_login="jkern",
        reviewed_sha=case["first"],
        feedback="Needs a test for the cap.",
    )
    assert evidence["verdict"] == "rejected"
    assert evidence["evidence"]["feedback"] == "Needs a test for the cap."
    async with case["db"].immediate() as conn:
        assert await settled(conn, project_id="p", now=1e12) is False


async def test_epic_without_pull_request_is_refused(case):
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(pr_url=None))
    result = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert result is None
    assert await _rows(case["db"]) == []


async def test_unverified_head_is_refused(case):
    with pytest.raises(HierarchyError, match="reviewed head"):
        await case["producer"].snapshot_from_pull_request(
            "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["second"]
        )
    assert await _rows(case["db"]) == []


async def test_approval_is_refused_when_remote_head_moved(case):
    _git("push", "origin", "HEAD", cwd=case["work"])
    with pytest.raises(HierarchyError, match="reviewed remote ref"):
        await case["producer"].snapshot_from_pull_request(
            "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
        )
    assert await _rows(case["db"]) == []


async def test_later_rejection_wins_when_clock_has_not_advanced(case):
    approved = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    rejected = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="rejected", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert rejected["created_at"] > approved["created_at"]
    async with case["db"].immediate() as conn:
        candidates = await case["db"].eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=10
        )
        latest = await case["db"].latest_exact_reviews_on(conn, candidates)
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert list(latest.values()) == [rejected]
    assert members == []


async def test_duplicate_approval_is_idempotent(case):
    first = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    second = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert first == second
    assert len(await _rows(case["db"])) == 1
