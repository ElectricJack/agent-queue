"""A finished train epic opens one pull request for its collected children."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    epic_dependencies,
    projects,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.integration.epic_pr import EpicPullRequestService, render_body
from src.integration.parent_completion import ParentCompletion
from src.models import Project, RepoConfig, RepoSourceType
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("epic-pr.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="epic pr project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            url="https://github.com/o/r.git",
            source_path="/repo/checkout",
            default_branch="main",
        )
    )
    await database.update_project(
        "p", hierarchical_integration_mode="train", integration_repository_id="repo"
    )
    async with database.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="e1",
                project_id="p",
                repo_id="repo",
                title="Retire the publisher",
                description="",
                status="COMPLETED",
                branch_name="aq/epic/retire-the-publisher",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        for child_id, title in (("c1", "Remove the tick"), ("c2", "Remove the sweep")):
            await conn.execute(
                insert(tasks).values(
                    id=child_id,
                    project_id="p",
                    repo_id="repo",
                    title=title,
                    description="",
                    status="COMPLETED",
                    parent_task_id="e1",
                    branch_name=f"aq/{child_id}",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
    yield database
    await database.close()


def test_body_lists_children_and_carries_the_epic_trailer():
    body = render_body(
        epic_id="e1",
        epic_title="Retire the publisher",
        children=[{"id": "c1", "title": "Remove the tick"}],
        dependencies=[],
    )
    assert "## Children" in body
    assert "- `c1` Remove the tick" in body
    assert body.rstrip().endswith("AQ-Epic: e1")


def test_body_of_a_childless_root_has_no_children_section():
    body = render_body(epic_id="r1", epic_title="Fix one thing", children=[], dependencies=[])
    assert "## Children" not in body
    assert body.startswith("Fix one thing")
    assert body.rstrip().endswith("AQ-Epic: r1")


def test_body_states_declared_dependencies():
    body = render_body(
        epic_id="e2",
        epic_title="Build on it",
        children=[{"id": "c9", "title": "Do the thing"}],
        dependencies=[{"id": "e1", "title": "Retire the publisher"}],
    )
    assert "## Depends on" in body
    assert "- `e1` Retire the publisher" in body


def test_body_omits_the_dependency_section_when_there_are_none():
    body = render_body(
        epic_id="e1", epic_title="Standalone", children=[{"id": "c1", "title": "X"}], dependencies=[]
    )
    assert "## Depends on" not in body


async def test_open_creates_one_pr_and_records_the_url(db):
    git = AsyncMock()
    git.acreate_pr.return_value = "https://github.com/o/r/pull/7"
    service = EpicPullRequestService(db, git_manager=git, clock=lambda: 100.0)

    result = await service.open_for_epic("e1")

    assert result == {"outcome": "opened", "epic_id": "e1", "pr_url": "https://github.com/o/r/pull/7"}
    git.bind_github_repository.assert_awaited_once_with("https://github.com/o/r.git")
    git.acreate_pr.assert_awaited_once_with(
        "/repo/checkout",
        branch="aq/epic/retire-the-publisher",
        title="Retire the publisher",
        body=render_body(
            epic_id="e1",
            epic_title="Retire the publisher",
            children=[
                {"id": "c1", "title": "Remove the tick"},
                {"id": "c2", "title": "Remove the sweep"},
            ],
            dependencies=[],
        ),
        base="main",
        project_id="p",
        repository=git.bind_github_repository.return_value,
    )
    async with db.immediate() as conn:
        stored = (await conn.execute(select(tasks.c.pr_url).where(tasks.c.id == "e1"))).scalar_one()
    assert stored == "https://github.com/o/r/pull/7"


async def test_open_is_idempotent(db):
    git = AsyncMock()
    git.acreate_pr.return_value = "https://github.com/o/r/pull/7"
    service = EpicPullRequestService(db, git_manager=git)

    await service.open_for_epic("e1")
    second = await service.open_for_epic("e1")

    assert second == {
        "outcome": "already_open",
        "epic_id": "e1",
        "pr_url": "https://github.com/o/r/pull/7",
    }
    git.acreate_pr.assert_awaited_once()


async def test_open_refuses_while_a_child_is_incomplete(db):
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "c2").values(status="READY"))
    git = AsyncMock()

    result = await EpicPullRequestService(db, git_manager=git).open_for_epic("e1")

    assert result == {"outcome": "children_incomplete", "epic_id": "e1", "pending": ["c2"]}
    git.acreate_pr.assert_not_awaited()


async def test_declared_dependencies_reach_the_body(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="e0", project_id="p", title="Earlier epic", description="", status="COMPLETED",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(epic_dependencies).values(
                dependent_task_id="e1", dependency_task_id="e0", declared_at=1.0
            )
        )
    git = AsyncMock()
    git.acreate_pr.return_value = "https://github.com/o/r/pull/8"

    await EpicPullRequestService(db, git_manager=git).open_for_epic("e1")

    assert "- `e0` Earlier epic" in git.acreate_pr.await_args.kwargs["body"]


async def test_open_refuses_non_train_or_nested_parent(db):
    git = AsyncMock()
    service = EpicPullRequestService(db, git_manager=git)
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="e0", project_id="p", title="Root", description="", status="COMPLETED",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(parent_task_id="e0"))
    assert await service.open_for_epic("e1") == {"outcome": "not_root_epic", "epic_id": "e1"}
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(parent_task_id=None))
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(hierarchical_integration_mode="hierarchy")
        )
    assert await service.open_for_epic("e1") == {"outcome": "not_train", "epic_id": "e1"}
    git.acreate_pr.assert_not_awaited()


async def test_parent_completion_retries_pr_after_github_failure(db):
    git = AsyncMock()
    git.acreate_pr.side_effect = [RuntimeError("temporary GitHub failure"), "https://github.com/o/r/pull/7"]
    completion = ParentCompletion(db, git_manager=git)
    completion._complete_parent_transition = AsyncMock(
        side_effect=[
            {"outcome": "completed", "task_id": "e1"},
            {"outcome": "already_completed", "task_id": "e1"},
            {"outcome": "already_completed", "task_id": "e1"},
        ]
    )

    first = await completion.complete_parent("e1", 1, "a" * 40)
    second = await completion.complete_parent("e1", 1, "a" * 40)
    third = await completion.complete_parent("e1", 1, "a" * 40)

    assert first == {
        "outcome": "pr_open_failed",
        "task_id": "e1",
        "reason": "temporary GitHub failure",
    }
    assert second == third == {"outcome": "already_completed", "task_id": "e1"}
    assert git.acreate_pr.await_count == 2


BASE = "b" * 40
HEAD = "c" * 40


async def _leaf_root(db, task_id="r1", *, checkpoint_sha=HEAD, **checkpoint):
    """A childless train root as ``file_root_on`` files it and a leaf close leaves it."""
    branch = f"aq/epic/{task_id}"
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id=task_id, project_id="p", repo_id="repo", title="Fix one thing",
                description="", status="COMPLETED", branch_name=branch,
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id=f"origin-{task_id}", task_id=task_id, repository_id="repo",
                branch_name=branch, parent_ref="main", base_sha=BASE,
                creation_generation=0, reserved=True, materialized=True, created_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id=task_id, repository_id="repo", branch=branch, generation=0,
                checkpoint_sha=checkpoint_sha, state="working", version=1,
                branch_owner_id=task_id, updated_at=1.0, **checkpoint,
            )
        )
    return branch


async def test_childless_train_root_opens_its_pr_at_the_leaf_checkpoint(db):
    """Regression for noble-harbor-74: a leaf root closed with no PR, forever."""
    branch = await _leaf_root(db)
    git = AsyncMock()
    git.acreate_pr.return_value = "https://github.com/o/r/pull/9"

    result = await EpicPullRequestService(db, git_manager=git).open_for_epic("r1")

    assert result == {"outcome": "opened", "epic_id": "r1", "pr_url": "https://github.com/o/r/pull/9"}
    kwargs = git.acreate_pr.await_args.kwargs
    assert kwargs["branch"] == branch
    assert kwargs["base"] == "main"
    assert "## Children" not in kwargs["body"]
    async with db.immediate() as conn:
        stored = (await conn.execute(select(tasks.c.pr_url).where(tasks.c.id == "r1"))).scalar_one()
    assert stored == "https://github.com/o/r/pull/9"


async def test_childless_root_without_a_checkpoint_stays_with_the_legacy_pipeline(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="legacy", project_id="p", repo_id="repo", title="Legacy root",
                description="", status="COMPLETED", branch_name="aq/legacy",
                created_at=1.0, updated_at=1.0,
            )
        )
    git = AsyncMock()

    result = await EpicPullRequestService(db, git_manager=git).open_for_epic("legacy")

    assert result == {"outcome": "not_epic", "epic_id": "legacy"}
    git.acreate_pr.assert_not_awaited()


async def test_childless_root_at_its_origin_base_has_no_changes_to_propose(db):
    await _leaf_root(db, checkpoint_sha=BASE)
    git = AsyncMock()

    result = await EpicPullRequestService(db, git_manager=git).open_for_epic("r1")

    assert result == {"outcome": "no_changes", "epic_id": "r1"}
    git.acreate_pr.assert_not_awaited()


async def test_childless_root_checkpoint_on_another_branch_is_not_ready(db):
    await _leaf_root(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "r1")
            .values(branch="aq/epic/somewhere-else")
        )
    git = AsyncMock()

    result = await EpicPullRequestService(db, git_manager=git).open_for_epic("r1")

    assert result == {"outcome": "checkpoint_not_ready", "epic_id": "r1"}
    git.acreate_pr.assert_not_awaited()


async def test_childless_root_already_on_the_default_branch_opens_nothing(db):
    """A leaf root whose head was merged by hand (a fix-forward) proposes nothing."""
    await _leaf_root(db)
    git = AsyncMock()
    git.acommits_ahead_of_base.return_value = 0

    result = await EpicPullRequestService(db, git_manager=git).open_for_epic("r1")

    assert result == {"outcome": "already_on_default", "epic_id": "r1", "head_sha": HEAD}
    git.acommits_ahead_of_base.assert_awaited_once_with(
        repository=git.bind_github_repository.return_value, base="main", head_sha=HEAD
    )
    git.acreate_pr.assert_not_awaited()


async def test_unknown_comparison_does_not_block_the_pr(db):
    await _leaf_root(db)
    git = AsyncMock()
    git.acommits_ahead_of_base.return_value = None
    git.acreate_pr.return_value = "https://github.com/o/r/pull/9"

    result = await EpicPullRequestService(db, git_manager=git).open_for_epic("r1")

    assert result["outcome"] == "opened"
