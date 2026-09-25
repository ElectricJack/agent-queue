"""A completed train root always gets its pull request, or says why not.

Regression for noble-harbor-74: a childless root filed by ``file_root_on``
closed through the leaf checkpoint, and nothing ever opened the PR the train
seats a root by.  The reconciler retries a missed PR; the redrive control is
the supervisor's dry-run-first handle on one stuck root.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    events,
    projects,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
)
from src.integration.root_pull_requests import (
    REDRIVE_EVENT,
    RootDeliveryRedrive,
    RootPullRequestReconciler,
)
from src.models import Project, RepoConfig, RepoSourceType
from tests.db_fixtures import lease_dsn

BASE = "b" * 40
HEAD = "c" * 40
PR = "https://github.com/o/r/pull/9"


@pytest.fixture
async def db():
    database = Database(lease_dsn("root-pull-requests.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="train project"))
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
    yield database
    await database.close()


async def _root(
    db,
    task_id="r1",
    *,
    status="COMPLETED",
    checkpoint_sha=HEAD,
    pr_url=None,
    parent_task_id=None,
):
    """A root as ``file_root_on`` files it and a leaf close leaves it."""
    branch = f"aq/epic/{task_id}"
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id=task_id, project_id="p", repo_id="repo", title=f"Fix {task_id}",
                description="", status=status, branch_name=branch, pr_url=pr_url,
                parent_task_id=parent_task_id, created_at=1.0, updated_at=1.0,
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
                branch_owner_id=task_id, updated_at=1.0,
            )
        )
    return branch


def _git(*, remote_head=HEAD, ahead=1, pr_url=PR):
    git = AsyncMock()
    git.aremote_branch_head.return_value = remote_head
    git.acommits_ahead_of_base.return_value = ahead
    git.acreate_pr.return_value = pr_url
    return git


async def _pr_url(db, task_id):
    async with db._engine.connect() as conn:
        return (
            await conn.execute(select(tasks.c.pr_url).where(tasks.c.id == task_id))
        ).scalar_one()


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


async def test_reconciler_opens_the_pr_a_completed_leaf_root_lacks(db):
    branch = await _root(db)
    git = _git()

    await RootPullRequestReconciler(db, git).tick(100.0)

    git.acreate_pr.assert_awaited_once()
    assert git.acreate_pr.await_args.kwargs["branch"] == branch
    assert await _pr_url(db, "r1") == PR


async def test_reconciler_selects_only_pr_ready_train_roots(db):
    await _root(db, "at-base", checkpoint_sha=BASE)
    await _root(db, "running", status="IN_PROGRESS")
    await _root(db, "has-pr", pr_url="https://github.com/o/r/pull/1")
    await _root(db, "delivered")
    await _root(db, "child", parent_task_id="has-pr")
    # An epic completed before the train verified it: no completed
    # aggregate verification, so its head is not the one to propose.
    await _root(db, "unverified-epic")
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="unverified-epic.1", project_id="p", repo_id="repo", title="child",
                description="", status="COMPLETED", parent_task_id="unverified-epic",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="receipt-1", domain_key="receipt-1", source_task_id="delivered",
                repository_id="repo", target_branch="main", disposition="code",
                created_at=1.0,
            )
        )
    git = _git()

    await RootPullRequestReconciler(db, git).tick(100.0)

    git.acreate_pr.assert_not_awaited()


async def test_reconciler_ignores_a_project_outside_train_mode(db):
    await _root(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p")
            .values(hierarchical_integration_mode="hierarchy")
        )
    git = _git()

    await RootPullRequestReconciler(db, git).tick(100.0)

    git.acreate_pr.assert_not_awaited()


async def test_reconciler_backs_off_a_root_it_cannot_open(db):
    await _root(db)
    git = _git()
    git.acreate_pr.side_effect = [RuntimeError("GitHub is down"), PR]
    reconciler = RootPullRequestReconciler(db, git, interval_seconds=10.0)

    await reconciler.tick(100.0)
    await reconciler.tick(110.0)  # due, but r1 is deferred until 100 + 2 * 10
    assert git.acreate_pr.await_count == 1
    assert await _pr_url(db, "r1") is None

    await reconciler.tick(120.0)
    assert git.acreate_pr.await_count == 2
    assert await _pr_url(db, "r1") == PR


async def test_reconciler_opens_nothing_for_a_head_already_on_main(db):
    """stark-torrent / fleet-apex: fix-forwards merged by hand propose nothing."""
    await _root(db)
    git = _git(ahead=0)
    reconciler = RootPullRequestReconciler(db, git, interval_seconds=10.0)

    await reconciler.tick(100.0)
    await reconciler.tick(110.0)

    git.acreate_pr.assert_not_awaited()
    assert git.acommits_ahead_of_base.await_count == 1
    assert await _pr_url(db, "r1") is None


# ---------------------------------------------------------------------------
# Redrive
# ---------------------------------------------------------------------------


async def test_redrive_dry_run_reports_the_missing_pr_and_writes_nothing(db):
    branch = await _root(db)
    git = _git()

    result = await RootDeliveryRedrive(db, git).run("r1")

    assert result["outcome"] == "would_open"
    assert result["kind"] == "leaf"
    assert result["branch"] == branch
    assert result["head_sha"] == HEAD
    assert result["base_sha"] == BASE
    assert result["remote_head_sha"] == HEAD
    assert result["ahead_by"] == 1
    assert result["checkpoint"]["state"] == "working"
    git.acreate_pr.assert_not_awaited()
    assert await _pr_url(db, "r1") is None


async def test_redrive_apply_opens_the_pr_for_the_reported_head(db):
    await _root(db)
    git = _git()

    result = await RootDeliveryRedrive(db, git, clock=lambda: 5.0).run(
        "r1", dry_run=False, expected_head_sha=HEAD, reason="noble-harbor-74 stuck",
        operator_id="human:local-operator",
    )

    assert result["outcome"] == "opened"
    assert result["pr_url"] == PR
    assert await _pr_url(db, "r1") == PR
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(select(events).where(events.c.event_type == REDRIVE_EVENT))
        ).mappings().one()
    payload = json.loads(row["payload"])
    assert row["task_id"] == "r1"
    assert payload["reason"] == "noble-harbor-74 stuck"
    assert payload["operator_id"] == "human:local-operator"
    assert payload["head_sha"] == HEAD
    assert payload["pr_url"] == PR


async def test_redrive_apply_refuses_a_head_the_dry_run_did_not_report(db):
    await _root(db)
    git = _git()

    result = await RootDeliveryRedrive(db, git).run(
        "r1", dry_run=False, expected_head_sha="d" * 40, reason="stuck"
    )

    assert result["outcome"] == "changed"
    git.acreate_pr.assert_not_awaited()


async def test_redrive_blocks_when_the_remote_branch_moved(db):
    await _root(db)
    git = _git(remote_head="e" * 40)

    result = await RootDeliveryRedrive(db, git).run("r1")

    assert result["outcome"] == "blocked"
    assert result["remote_head_sha"] == "e" * 40
    assert "moved" in result["reason"]


async def test_redrive_blocks_an_unpublished_branch(db):
    await _root(db)

    result = await RootDeliveryRedrive(db, _git(remote_head=None)).run("r1")

    assert result["outcome"] == "blocked"
    assert "not published" in result["reason"]


async def test_redrive_reports_a_head_already_on_the_default_branch(db):
    await _root(db)

    result = await RootDeliveryRedrive(db, _git(ahead=0)).run(
        "r1", dry_run=False, expected_head_sha=HEAD, reason="stuck"
    )

    assert result["outcome"] == "nothing_to_redrive"
    assert result["ahead_by"] == 0


async def test_redrive_reports_a_leaf_whose_close_recorded_no_head(db):
    await _root(db, checkpoint_sha=BASE)

    result = await RootDeliveryRedrive(db, _git()).run("r1")

    assert result["outcome"] == "blocked"
    assert "origin base" in result["reason"]


async def test_redrive_leaves_an_unverified_epic_to_its_parent_integration(db):
    await _root(db, "e1")
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="e1.1", project_id="p", repo_id="repo", title="child", description="",
                status="COMPLETED", parent_task_id="e1", created_at=1.0, updated_at=1.0,
            )
        )
    git = _git()

    result = await RootDeliveryRedrive(db, git).run("e1")

    assert result["outcome"] == "blocked"
    assert result["kind"] == "parent"
    assert "aggregate verification" in result["reason"]
    git.bind_github_repository.assert_not_awaited()


@pytest.mark.parametrize(
    ("setup", "outcome"),
    [
        ({"pr_url": "https://github.com/o/r/pull/1"}, "nothing_to_redrive"),
        ({"status": "IN_PROGRESS"}, "not_eligible"),
    ],
)
async def test_redrive_classifies_roots_with_nothing_to_drive(db, setup, outcome):
    await _root(db, **setup)

    result = await RootDeliveryRedrive(db, _git()).run("r1")

    assert result["outcome"] == outcome


async def test_redrive_refuses_unknown_nested_and_legacy_tasks(db):
    await _root(db, "r1")
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="nested", project_id="p", repo_id="repo", title="n", description="",
                status="COMPLETED", parent_task_id="r1", created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(tasks).values(
                id="legacy", project_id="p", repo_id="repo", title="l", description="",
                status="COMPLETED", branch_name="aq/legacy", created_at=1.0, updated_at=1.0,
            )
        )
    redrive = RootDeliveryRedrive(db, _git())

    assert (await redrive.run("missing"))["outcome"] == "not_found"
    assert (await redrive.run("nested"))["outcome"] == "not_eligible"
    legacy = await redrive.run("legacy")
    assert legacy["outcome"] == "not_eligible"
    assert "legacy completion pipeline" in legacy["reason"]
