"""A completed child of a container nothing can collect opens its own PR.

Regression for noble-nexus-85: bright-rapids-84.1/.2/.3/.4/.7/.9 all reached
COMPLETED on pushed branches with ``pr_url`` NULL and no delivery, so the root
train could never admit them.  ``open_for_epic`` refuses any parented task, and
both pull-request lines of ``stall.sweep`` require a ``pr_url`` to already be on
the row, so nothing in the daemon noticed.  The only fallback was the agent's
own ``aq git create-pr``, which is why .5 and .6 got pull requests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_parent_episodes,
    integration_repair_operations,
    integration_subjects,
    playbook_artifacts,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
)
from src.integration.stranded_children import (
    STRANDED_CHILD_AFTER_SECONDS,
    StrandedChildPullRequestService,
    stranded_child_statement,
)
from src.models import Project, RepoConfig, RepoSourceType

BASE = "b" * 40
HEAD = "c" * 40
PR = "https://github.com/o/r/pull/1066"
ARTIFACT = "a" * 64
CONTAINER_BRANCH = "aq/epic/container"


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("stranded-children.db")
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
    async with database._engine.begin() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=ARTIFACT,
                playbook_id="collection",
                source_digest="sha256:" + "c" * 64,
                contract_fingerprint="sha256:" + "d" * 64,
                compiler_build="test",
                path=f"/artifacts/{ARTIFACT}.json",
                created_at=1.0,
            )
        )
    yield database


async def _container(
    db,
    *,
    task_id="container",
    status="PAUSED",
    checkpoint_state="awaiting_children",
    episode=True,
    operation_state="active",
    subject_phase="building",
    updated_at=1.0,
    project_id="p",
    repo_id="repo",
):
    """A container epic as ``checkpoint_and_suspend_parent`` leaves it."""
    episode_id = f"ep-{task_id}"
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id=task_id, project_id=project_id, repo_id=repo_id,
                title="The container",
                description="", status=status, branch_name=CONTAINER_BRANCH,
                created_at=updated_at, updated_at=updated_at,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id=f"origin-{task_id}", task_id=task_id, repository_id=repo_id,
                branch_name=CONTAINER_BRANCH, parent_ref="main", base_sha=BASE,
                creation_generation=0, reserved=True, materialized=True,
                created_at=updated_at,
            )
        )
        if episode:
            await conn.execute(
                insert(integration_parent_episodes).values(
                    id=episode_id, parent_task_id=task_id, repository_id=repo_id,
                    generation=1, pre_collection_checkpoint_sha=BASE,
                    created_at=updated_at,
                )
            )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id=task_id, repository_id=repo_id, branch=CONTAINER_BRANCH,
                generation=1, checkpoint_sha=BASE, state=checkpoint_state,
                episode_id=episode_id if episode else None, version=1,
                branch_owner_id=task_id, updated_at=updated_at,
            )
        )
        if episode:
            if operation_state is not None:
                await conn.execute(
                    insert(integration_repair_operations).values(
                        id=f"op-{task_id}", target_kind="parent", parent_task_id=task_id,
                        episode_id=episode_id, state=operation_state, policy_snapshot={},
                        artifact_snapshot={"artifact_sha256": ARTIFACT,
                                           "playbook_id": "collection"},
                        required_check_version="1", created_at=updated_at,
                        updated_at=updated_at,
                    )
                )
            if subject_phase is not None:
                await conn.execute(
                    insert(integration_subjects).values(
                        id=f"subject-{task_id}", project_id=project_id,
                        repository_id=repo_id,
                        kind="parent_episode", subject_key=f"parent:{task_id}",
                        engine="reconciler", phase=subject_phase,
                        policy_playbook_id="collection",
                        policy_artifact_sha256=ARTIFACT, task_id=task_id,
                        parent_episode_id=episode_id,
                        target_ref=f"refs/heads/{CONTAINER_BRANCH}",
                        head_sha=BASE, base_sha=BASE, generation=1, due_set_at=1.0,
                        max_wait_seconds=3600, refusal_streak=0, writer_status="none",
                        version=0, created_at=updated_at, updated_at=updated_at,
                        **(
                            {"closed_reason": "collection ended"}
                            if subject_phase == "done"
                            else {"next_due_at": updated_at}
                        ),
                    )
                )
    return task_id


async def _child(
    db,
    task_id="container.1",
    *,
    parent_task_id="container",
    status="COMPLETED",
    head=HEAD,
    pr_url=None,
    updated_at=1.0,
    project_id="p",
    repo_id="repo",
):
    """A child as ``file_prepared_children_on`` files it and a close leaves it."""
    branch = f"aq/{task_id}"
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id=task_id, project_id=project_id, repo_id=repo_id,
                title=f"Stage {task_id}",
                description="", status=status, branch_name=branch, pr_url=pr_url,
                parent_task_id=parent_task_id, created_at=updated_at,
                updated_at=updated_at,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id=f"origin-{task_id}", task_id=task_id, repository_id=repo_id,
                branch_name=branch, parent_ref=CONTAINER_BRANCH, base_sha=BASE,
                parent_task_id=parent_task_id, creation_generation=1, reserved=True,
                materialized=True, created_at=updated_at,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id=task_id, repository_id=repo_id, branch=branch, generation=1,
                checkpoint_sha=head, state="working", version=1,
                branch_owner_id=task_id, updated_at=updated_at,
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


async def _sweep(db, *, now=1000.0, limit=100):
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                stranded_child_statement(
                    completed_before=now - STRANDED_CHILD_AFTER_SECONDS, limit=limit
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# The close leg: epic child completes -> a pull request exists for main
# ---------------------------------------------------------------------------


async def test_a_completed_child_of_a_consumed_container_opens_a_pr_to_main(db):
    """bright-rapids-84.1: the container has no live collector left."""
    await _container(db, operation_state=None, subject_phase=None)
    branch = await _child(db)
    git = _git()

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == "opened"
    assert result["reason"].startswith("paused_epic:")
    git.acreate_pr.assert_awaited_once()
    assert git.acreate_pr.await_args.kwargs["branch"] == branch
    assert git.acreate_pr.await_args.kwargs["base"] == "main"
    assert git.acreate_pr.await_args.kwargs["body"].endswith("AQ-Task: container.1")
    assert await _pr_url(db, "container.1") == PR


async def test_a_child_of_a_collecting_container_gets_no_pr(db):
    """The healthy train: the container still has an operation and a Subject."""
    await _container(db)
    await _child(db)
    git = _git()

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == "not_stranded"
    git.acreate_pr.assert_not_awaited()
    assert await _pr_url(db, "container.1") is None


async def test_a_child_of_an_unfinished_container_gets_no_pr(db):
    """Children can be filed before the container suspends itself."""
    await _container(db, status="IN_PROGRESS")
    await _child(db)
    git = _git()

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == "not_stranded"
    assert result["reason"] == "its container may still collect"
    git.acreate_pr.assert_not_awaited()


async def test_each_stranded_shape_names_its_own_reason(db):
    await _container(db, subject_phase=None)  # no_consumer
    await _child(db, "no-consumer")
    await _container(db, task_id="terminal", status="COMPLETED")
    await _child(db, "terminal.1", parent_task_id="terminal")
    await _container(db, task_id="done-subject", subject_phase="done")
    await _child(db, "done-subject.1", parent_task_id="done-subject")

    service = StrandedChildPullRequestService(db, git_manager=_git())
    reasons = {
        task_id: (await service.open_for_child(task_id))["reason"]
        for task_id in ("no-consumer", "terminal.1", "done-subject.1")
    }

    assert reasons["no-consumer"].startswith("no_consumer:")
    assert reasons["terminal.1"].startswith("no_parent_collection:")
    assert reasons["done-subject.1"].startswith("paused_epic:")


async def test_a_delivered_child_is_left_to_its_collection(db):
    await _container(db, operation_state=None, subject_phase=None)
    await _child(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="receipt-1", domain_key="receipt-1", source_task_id="container.1",
                repository_id="repo", target_branch=CONTAINER_BRANCH,
                reviewed_head_sha=HEAD, disposition="code", created_at=1.0,
            )
        )
    git = _git()

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == "delivered"
    git.acreate_pr.assert_not_awaited()


async def test_a_child_that_already_has_a_pr_is_left_alone(db):
    await _container(db, operation_state=None, subject_phase=None)
    await _child(db, pr_url="https://github.com/o/r/pull/1")
    git = _git()

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == "already_open"
    git.acreate_pr.assert_not_awaited()


async def test_a_promoted_child_is_left_to_its_collection(db):
    """An unfinished promotion of this head is the collection's business."""
    from src.database.tables import integration_promotion_intents

    await _container(db, operation_state=None, subject_phase=None)
    await _child(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="intent-1", domain_key="intent-1", receipt_id="receipt-intent",
                operation_key="op-container", source_task_id="container.1",
                source_head=HEAD, source_base=BASE, repository_id="repo",
                target_branch=CONTAINER_BRANCH, expected_target=BASE,
                fence_owner_id="op-container", fence_token=1, state="prepared",
                created_at=1.0, updated_at=1.0,
            )
        )
    git = _git()

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == "not_stranded"
    assert "intent-1" in result["reason"]
    git.acreate_pr.assert_not_awaited()


@pytest.mark.parametrize(
    ("git_kwargs", "outcome"),
    [
        ({"remote_head": None}, "branch_not_published"),
        ({"remote_head": "d" * 40}, "branch_moved"),
        ({"ahead": 0}, "already_on_default"),
    ],
)
async def test_it_never_proposes_an_unproved_head(db, git_kwargs, outcome):
    await _container(db, operation_state=None, subject_phase=None)
    await _child(db)
    git = _git(**git_kwargs)

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == outcome
    git.acreate_pr.assert_not_awaited()
    assert await _pr_url(db, "container.1") is None


async def test_a_child_with_no_recorded_head_is_a_stall_not_a_pull_request(db):
    """No checkpoint means nothing exact to propose; the sweep reports it."""
    await _container(db, operation_state=None, subject_phase=None)
    await _child(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "container.1")
            .values(checkpoint_sha=BASE)
        )
    git = _git()

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == "blocked"
    git.acreate_pr.assert_not_awaited()


async def test_a_root_is_never_a_stranded_child(db):
    """The root path owns its own pull request; do not double-open."""
    await _container(db, operation_state=None, subject_phase=None)
    await _child(db, parent_task_id=None)
    git = _git()

    result = await StrandedChildPullRequestService(db, git_manager=git).open_for_child(
        "container.1"
    )

    assert result["outcome"] == "not_eligible"
    git.acreate_pr.assert_not_awaited()


# ---------------------------------------------------------------------------
# stall.sweep
# ---------------------------------------------------------------------------


async def test_the_sweep_names_a_completed_child_with_no_pr_and_no_delivery(db):
    await _container(db, operation_state=None, subject_phase=None, updated_at=1.0)
    await _child(db, updated_at=1.0)

    rows = await _sweep(db, now=1.0 + STRANDED_CHILD_AFTER_SECONDS + 1)

    assert [row["task_id"] for row in rows] == ["container.1"]
    assert rows[0]["parent_status"] == "PAUSED"
    assert rows[0]["parent_state"] == "awaiting_children"
    assert rows[0]["subject_phase"] is None


async def test_the_sweep_stays_quiet_inside_the_fifteen_minute_window(db):
    await _container(db, operation_state=None, subject_phase=None, updated_at=1.0)
    await _child(db, updated_at=1.0)

    assert await _sweep(db, now=1.0 + STRANDED_CHILD_AFTER_SECONDS - 1) == []


async def test_the_sweep_stays_quiet_while_the_container_can_collect(db):
    await _container(db)
    await _child(db, updated_at=1.0)

    assert await _sweep(db, now=1000.0) == []


async def test_the_sweep_stays_quiet_once_a_pull_request_exists(db):
    await _container(db, operation_state=None, subject_phase=None)
    await _child(db, pr_url="https://github.com/o/r/pull/1")

    assert await _sweep(db, now=1000.0) == []


async def test_the_sweep_ignores_a_project_outside_the_managed_modes(db):
    await _container(db, operation_state=None, subject_phase=None)
    await _child(db)
    async with db.immediate() as conn:
        from src.database.tables import projects as project_table

        await conn.execute(
            update(project_table).where(project_table.c.id == "p")
            .values(hierarchical_integration_mode="disabled")
        )

    assert await _sweep(db, now=1000.0) == []


async def test_the_sweep_still_names_a_child_reworked_onto_an_undelivered_head(db):
    """A receipt for an older head is not a delivery of this one."""
    await _container(db, operation_state=None, subject_phase=None, updated_at=1.0)
    await _child(db, updated_at=1.0)
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="receipt-1", domain_key="receipt-1", source_task_id="container.1",
                repository_id="repo", target_branch=CONTAINER_BRANCH,
                reviewed_head_sha="9" * 40, disposition="code", created_at=1.0,
            )
        )

    assert [row["task_id"] for row in await _sweep(db, now=1000.0)] == ["container.1"]




# ---------------------------------------------------------------------------
# The close leg is wired into the session close
# ---------------------------------------------------------------------------


async def test_a_child_close_reaches_the_stranded_child_leg(tmp_path, monkeypatch):
    """The service alone is not the fix: the session close has to call it."""
    from src.database.tables import workspaces as workspaces_table
    from src.models import PhaseResult, TaskStatus
    from src.orchestrator.git_ops import GitOpsMixin
    from tests.session_dispatch_helpers import (
        create_session_project,
        drain_running_tasks,
        make_session_orch,
    )

    orch = await make_session_orch(tmp_path)
    try:
        await create_session_project(orch)
        await orch.db.create_repo(
            RepoConfig(
                id="repo",
                project_id="p-1",
                source_type=RepoSourceType.LINK,
                url="https://github.com/org/repo.git",
                source_path=str(tmp_path / "repo"),
                default_branch="main",
            )
        )
        await orch.db.update_project(
            "p-1", hierarchical_integration_mode="train", integration_repository_id="repo"
        )
        await _container(
            orch.db, operation_state=None, subject_phase=None,
            project_id="p-1", repo_id="repo",
        )
        await _child(orch.db, project_id="p-1", repo_id="repo")
        async with orch.db._engine.begin() as conn:
            await conn.execute(
                update(workspaces_table).where(workspaces_table.c.id == "ws-p-1")
                .values(locked_by_task_id="container.1")
            )
        child = await orch.db.get_task("container.1")

        # The producer verification is its own phase; the close leg runs only
        # once it proceeds.
        monkeypatch.setattr(
            GitOpsMixin,
            "_phase_verify_hierarchy_producer",
            AsyncMock(return_value=PhaseResult.CONTINUE),
        )
        monkeypatch.setattr(
            "src.integration.hierarchy.verify_workspace_checkpoint",
            AsyncMock(return_value=HEAD),
        )
        orch.git._arun = AsyncMock(return_value=HEAD)
        orch.git.aremote_branch_head = AsyncMock(return_value=HEAD)
        orch.git.acommits_ahead_of_base = AsyncMock(return_value=1)
        orch.git.acreate_pr = AsyncMock(return_value=PR)

        result = await orch.complete_session_task(child, outcome="pass", notes="done")

        assert result["status"] == TaskStatus.COMPLETED.value
        orch.git.acreate_pr.assert_awaited_once()
        assert orch.git.acreate_pr.await_args.kwargs["base"] == "main"
        assert orch.git.acreate_pr.await_args.kwargs["branch"] == "aq/container.1"
        assert await _pr_url(orch.db, "container.1") == PR
    finally:
        await drain_running_tasks(orch)
        await orch.shutdown()
