"""Flag-enabled isolated child origins and hierarchy mutation guards."""

from __future__ import annotations

import asyncio
import subprocess

import pytest
from sqlalchemy import select, update

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    task_delivery_receipts,
    integration_outbox,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.hierarchy import (
    HierarchyIntegration,
    materialize_exact_branch,
    verify_workspace_checkpoint,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus, Workspace


BASE = "a" * 40
NEXT = "b" * 40


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "hierarchy.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="hierarchy"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            source_path=str(tmp_path),
        )
    )
    await database.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
    )
    yield database
    await database.close()


@pytest.fixture
def hierarchy(db):
    return HierarchyIntegration(db, default_head_resolver=lambda _repo, _branch: BASE)


async def _create(db, task_id: str, *, parent_id: str | None = None) -> None:
    await db.create_task(
        Task(
            id=task_id,
            project_id="p",
            repo_id="repo",
            parent_task_id=parent_id,
            title=task_id,
            description=task_id,
            status=TaskStatus.IN_PROGRESS,
        )
    )


async def _origins(db) -> list[dict]:
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(task_branch_origins, tasks.c.branch_name.label("branch"))
                .join(tasks, tasks.c.id == task_branch_origins.c.task_id)
                .order_by(task_branch_origins.c.created_at)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def test_project_mode_and_designated_repository_round_trip_and_validate(tmp_path):
    database = Database(str(tmp_path / "project.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="one"))
    await database.create_project(Project(id="other", name="two"))
    await database.create_repo(
        RepoConfig(id="foreign", project_id="other", source_type=RepoSourceType.LINK)
    )

    with pytest.raises(ValueError, match="same project"):
        await database.update_project(
            "p",
            hierarchical_integration_mode="train",
            integration_repository_id="foreign",
        )

    project = await database.get_project("p")
    assert project.hierarchical_integration_mode == "disabled"
    assert project.integration_repository_id is None
    await database.close()


async def test_branchless_three_level_tree_reserves_distinct_top_down_origins(db, hierarchy):
    await _create(db, "root")

    first = await hierarchy.file_children("root", [{"title": "child"}], 0)
    second = await hierarchy.file_children(
        first["children"][0]["task_id"], [{"title": "grandchild"}], 0
    )

    assert first["generation"] == 1
    assert second["generation"] == 1
    origins = await _origins(db)
    by_task = {row["task_id"]: row for row in origins}
    assert set(by_task) == {"root", "root.1", "root.1.1"}
    assert {row["branch"] for row in origins} == {"aq/root", "aq/root.1", "aq/root.1.1"}
    assert by_task["root"]["parent_ref"] == "main"
    assert by_task["root.1"]["parent_ref"] == "aq/root"
    assert by_task["root.1.1"]["parent_ref"] == "aq/root.1"
    assert all(
        row["parent_ref"] != "main" for row in origins if row["parent_task_id"] is not None
    )

    async with db._engine.connect() as conn:
        events = (
            await conn.execute(
                select(integration_outbox.c.event_type, integration_outbox.c.payload).order_by(
                    integration_outbox.c.created_at, integration_outbox.c.id
                )
            )
        ).all()
    assert [event_type for event_type, _ in events] == [
        "integration.branch_materialization_pending",
        "integration.branch_materialization_pending",
        "integration.branch_materialization_pending",
    ]


async def test_batch_filing_advances_generation_once_and_concurrent_stale_writer_loses(
    db, hierarchy
):
    await _create(db, "parent")

    result = await hierarchy.file_children(
        "parent", [{"title": "A"}, {"title": "B"}], 0
    )
    assert result["generation"] == 1
    assert {origin["creation_generation"] for origin in result["origins"]} == {1}

    outcomes = await asyncio.gather(
        hierarchy.file_children("parent", [{"title": "C"}], 1),
        hierarchy.file_children("parent", [{"title": "D"}], 1),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    error = next(value for value in outcomes if isinstance(value, Exception))
    assert isinstance(error, HierarchyError)
    assert error.code == "stale_parent"


async def test_later_child_does_not_rewrite_earlier_child_base(db, hierarchy):
    await _create(db, "parent")

    first = await hierarchy.file_children("parent", [{"title": "A"}], 0)
    await hierarchy.checkpoint_parent("parent", NEXT, 1)
    second = await hierarchy.file_children("parent", [{"title": "B"}], 1)

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert first["origins"][0]["base_sha"] == BASE
    assert second["origins"][0]["base_sha"] == NEXT
    origins = {row["task_id"]: row for row in await _origins(db)}
    assert origins[first["children"][0]["task_id"]]["base_sha"] == BASE


async def test_pending_origin_is_not_claim_frontier_eligible(db, hierarchy):
    await _create(db, "parent")
    result = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = result["children"][0]["task_id"]
    await db.transition_task(child_id, TaskStatus.READY)

    assert await db.count_ready_by_profile("p") == {}

    async with db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.task_id == child_id)
            .values(materialized=True, materialized_at=1.0)
        )
    assert await db.count_ready_by_profile("p") == {None: 1}


async def test_checkpoint_rejects_stale_generation(db, hierarchy):
    await _create(db, "parent")
    await hierarchy.file_children("parent", [{"title": "child"}], 0)

    with pytest.raises(HierarchyError) as exc:
        await hierarchy.checkpoint_parent("parent", NEXT, 0)
    assert exc.value.code == "stale"


async def test_ordinary_create_routes_all_child_writes_through_atomic_origin_writer(
    db, hierarchy, internal_plugins_handler
):
    await _create(db, "parent")
    handler = await internal_plugins_handler(db=db)
    handler.orchestrator.hierarchy_integration = hierarchy

    result = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "parent_id": "parent",
            "title": "child",
            "description": "child",
            "requires_kinds": ["project-repo"],
            "labels": ["integration-child"],
        },
    )

    assert result["created"] == "parent.1"
    child = await db.get_task("parent.1")
    assert child.repo_id == "repo"
    assert child.branch_name == "aq/parent.1"
    assert [row.kind_id for row in await db.fetch_task_workspace_requirements("parent.1")] == [
        "project-repo"
    ]
    assert await db.get_task_labels("parent.1") == ["integration-child"]


async def test_proposal_commit_uses_one_atomic_hierarchy_transaction(
    db, hierarchy, internal_plugins_handler
):
    handler = await internal_plugins_handler(db=db)
    handler.orchestrator.hierarchy_integration = hierarchy
    proposal = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p",
            "source": "spec:test",
            "tasks": [
                {"tempId": "parent", "title": "parent", "description": ""},
                {"tempId": "a", "title": "A", "description": ""},
                {"tempId": "b", "title": "B", "description": ""},
            ],
            "edges": [
                {"from": "a", "to": "parent", "dep_type": "parent-child"},
                {"from": "b", "to": "parent", "dep_type": "parent-child"},
            ],
        },
    )

    committed = await handler.execute(
        "task_batch_commit", {"proposal_id": proposal["proposal_id"]}
    )

    assert committed["success"] is True
    created = [await db.get_task(task_id) for task_id in committed["task_ids"]]
    parent = next(task for task in created if task.title == "parent")
    children = [task for task in created if task.title in {"A", "B"}]
    assert {task.parent_task_id for task in children} == {parent.id}
    checkpoint = await db.get_integration_checkpoint(parent.id)
    assert checkpoint["generation"] == 1
    assert len(await _origins(db)) == 3


async def test_reparent_unmaterialized_child_invalidates_both_parents(db, hierarchy):
    await _create(db, "old")
    await _create(db, "new")
    filed = await hierarchy.file_children("old", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]

    result = await hierarchy.mutate_hierarchy(
        child_id,
        "reparent",
        {"parent_id": "new", "expected_old_generation": 1, "expected_new_generation": 0},
    )

    assert result["outcome"] == "updated"
    assert result["old_parent_generation"] == 2
    assert result["new_parent_generation"] == 1
    async with db._engine.connect() as conn:
        checkpoints = {
            row["task_id"]: dict(row)
            for row in (
                await conn.execute(select(task_integration_checkpoints))
            ).mappings().all()
        }
        child = (
            await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == child_id))
        ).one()
    assert child.parent_task_id == "new"
    assert checkpoints["old"]["verified_sha"] is None
    assert checkpoints["new"]["verified_sha"] is None


async def test_canonical_set_parent_cannot_bypass_origin_guard(db, hierarchy):
    await _create(db, "old")
    await _create(db, "new")
    filed = await hierarchy.file_children("old", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]

    with pytest.raises(HierarchyError) as exc:
        async with db.immediate() as conn:
            await db.set_parent(child_id, "new", conn=conn)
    assert exc.value.code == "delivery_target_fixed"


async def test_delete_unmaterialized_child_retires_origin_and_invalidates_parent(db, hierarchy):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]

    await db.delete_task(child_id)

    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["generation"] == 2
    async with db._engine.connect() as conn:
        origin = (
            await conn.execute(
                select(task_branch_origins).where(task_branch_origins.c.task_id == child_id)
            )
        ).mappings().one()
    assert origin["retired_at"] is not None


async def test_materialized_child_cannot_be_deleted_or_archived(db, hierarchy):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.task_id == child_id)
            .values(materialized=True, materialized_at=1.0)
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == child_id).values(status=TaskStatus.FAILED.value)
        )

    with pytest.raises(HierarchyError) as deleted:
        await db.delete_task(child_id)
    assert deleted.value.code == "delivery_target_fixed"
    with pytest.raises(HierarchyError) as archived:
        await db.archive_task(child_id)
    assert archived.value.code == "delivery_target_fixed"


async def test_delivered_task_cannot_be_reopened(db, hierarchy):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == child_id).values(status=TaskStatus.COMPLETED.value)
        )
        await conn.execute(
            task_delivery_receipts.insert().values(
                id="receipt",
                domain_key="receipt:child",
                source_task_id=child_id,
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                disposition="code",
                created_at=1.0,
            )
        )

    with pytest.raises(HierarchyError) as reopened:
        await db.transition_task(child_id, TaskStatus.READY)
    assert reopened.value.code == "delivery_target_fixed"


async def test_undelivered_reopen_invalidates_parent_generation(db, hierarchy):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == child_id)
            .values(status=TaskStatus.FAILED.value)
        )

    await db.transition_task(child_id, TaskStatus.READY)

    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["generation"] == 2
    assert checkpoint["verified_sha"] is None


async def test_materialization_creates_only_absent_or_exact_remote_ref(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git(["init", "--bare", str(remote)], tmp_path)
    _git(["init", str(work)], tmp_path)
    _git(["config", "user.email", "test@example.com"], work)
    _git(["config", "user.name", "Test"], work)
    (work / "value.txt").write_text("base\n")
    _git(["add", "value.txt"], work)
    _git(["commit", "-m", "base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["remote", "add", "origin", str(remote)], work)
    git = GitManager()

    assert await materialize_exact_branch(git, str(work), "aq/child", base) == base
    assert _git(["rev-parse", "refs/heads/aq/child"], remote) == base
    assert await materialize_exact_branch(git, str(work), "aq/child", base) == base

    (work / "value.txt").write_text("other\n")
    _git(["commit", "-am", "other"], work)
    other = _git(["rev-parse", "HEAD"], work)
    _git(["push", "--force", "origin", f"{other}:refs/heads/aq/child"], work)
    with pytest.raises(HierarchyError) as conflict:
        await materialize_exact_branch(git, str(work), "aq/child", base)
    assert conflict.value.code == "delivery_target_fixed"
    assert _git(["rev-parse", "refs/heads/aq/child"], remote) == other


async def test_materialized_origin_is_published_to_ready_only_after_exact_confirmation(
    db, hierarchy
):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    origin = filed["origins"][0]
    child_id = filed["children"][0]["task_id"]
    assert (await db.get_task(child_id)).status is TaskStatus.DEFINED
    service = HierarchyIntegration(
        db,
        default_head_resolver=lambda _repo, _branch: BASE,
        branch_materializer=lambda _repo, _branch, base_sha: base_sha,
    )

    await service.materialize_origin(origin["id"])

    assert (await db.get_task(child_id)).status is TaskStatus.READY
    persisted = await db.get_task_branch_origin_for_promotion(child_id, "repo")
    assert persisted["materialized"] is True


async def test_checkpoint_verifies_actual_clean_and_pushed_workspace_head(db, tmp_path):
    remote = tmp_path / "checkpoint.git"
    work = tmp_path / "checkpoint-work"
    _git(["init", "--bare", str(remote)], tmp_path)
    _git(["init", str(work)], tmp_path)
    _git(["config", "user.email", "test@example.com"], work)
    _git(["config", "user.name", "Test"], work)
    _git(["checkout", "-b", "aq/parent"], work)
    (work / "value.txt").write_text("base\n")
    _git(["add", "value.txt"], work)
    _git(["commit", "-m", "base"], work)
    _git(["remote", "add", "origin", str(remote)], work)
    _git(["push", "-u", "origin", "aq/parent"], work)
    await _create(db, "parent")
    await db.update_task("parent", branch_name="aq/parent")
    await db.create_workspace(
        Workspace(
            id="checkpoint-slot",
            project_id="p",
            workspace_path=str(work),
            source_type=RepoSourceType.LINK,
            locked_by_task_id="parent",
        )
    )
    task = await db.get_task("parent")
    repo = await db.get_repo("repo")
    git = GitManager()
    base = _git(["rev-parse", "HEAD"], work)

    (work / "dirty.txt").write_text("dirty\n")
    with pytest.raises(HierarchyError, match="uncommitted"):
        await verify_workspace_checkpoint(db, git, task.__dict__, repo, base)
    _git(["add", "dirty.txt"], work)
    _git(["commit", "-m", "next"], work)
    next_head = _git(["rev-parse", "HEAD"], work)
    with pytest.raises(HierarchyError, match="exactly pushed"):
        await verify_workspace_checkpoint(db, git, task.__dict__, repo, next_head)

    _git(["push", "origin", "aq/parent"], work)
    assert await verify_workspace_checkpoint(
        db, git, task.__dict__, repo, next_head
    ) == next_head
