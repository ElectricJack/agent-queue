"""Evidence and dry-run behavior for terminal legacy repository binding."""

import json
import time

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    development_deliveries, projects, task_comments, task_completion_records,
)
from src.integration.legacy_repositories import LegacyRepositoryBinding
from src.integration.status import IntegrationStatusService
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("legacy-repositories.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="project"))
    await database.create_repo(RepoConfig(
        id="repo", project_id="p", source_type=RepoSourceType.CLONE,
        url="https://github.com/acme/widgets.git",
    ))
    yield database
    await database.close()


async def test_dry_run_then_bind_only_proven_terminal_hierarchy(db):
    for task_id, parent, status in (
        ("parent", None, TaskStatus.COMPLETED),
        ("child", "parent", TaskStatus.COMPLETED),
        ("unproven-parent", None, TaskStatus.COMPLETED),
        ("unproven-child", "unproven-parent", TaskStatus.FAILED),
        ("standalone", None, TaskStatus.COMPLETED),
    ):
        await db.create_task(Task(
            id=task_id, project_id="p", title=task_id, description="",
            parent_task_id=parent, status=status,
        ))
    now = time.time() + 1
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            integration_repository_id="repo", hierarchical_integration_mode="observe",
            hierarchical_integration_desired_mode="train",
        ))
        await conn.execute(insert(task_completion_records).values(
            id="completion-child", task_id="child", outcome="pass",
            commits=json.dumps(["a" * 40]), completed_at=now,
        ))
        await conn.execute(insert(development_deliveries).values(
            id="delivery-child", project_id="p", repository_id="repo",
            target_ref="refs/heads/main", expected_sha=None, prepared_sha="a" * 40,
            state="delivered", manifest=[{"task_id": "child", "source_sha": "a" * 40}],
            evidence={}, reason="published", created_at=now, updated_at=now,
        ))

    service = LegacyRepositoryBinding(db)
    preview = await service.run("p", principal="supervisor", dry_run=True)
    assert preview["bound"] == [
        {"task_id": "child", "proof": "development_delivery"},
        {"task_id": "parent", "proof": "delivered_children"},
    ]
    assert preview["unproven"] == ["unproven-child", "unproven-parent"]
    assert (await db.get_task("child")).repo_id is None

    applied = await service.run("p", principal="supervisor", dry_run=False,
                                reason="reconcile delivered legacy hierarchy")
    assert applied["bound"] == preview["bound"]
    assert (await db.get_task("child")).repo_id == "repo"
    assert (await db.get_task("parent")).repo_id == "repo"
    assert (await db.get_task("unproven-parent")).repo_id is None
    assert (await db.get_task("standalone")).repo_id is None
    async with db._engine.connect() as conn:
        comments = (await conn.execute(select(task_comments.c.task_id).where(
            task_comments.c.project_id == "p"
        ))).scalars().all()
    assert sorted(comments) == ["child", "parent"]
    blockers = (await IntegrationStatusService(db).task_blockers("parent"))["blockers"]
    assert "repository_not_designated" not in {item["code"] for item in blockers}
    assert (await service.run("p", principal="supervisor", dry_run=False,
                              reason="repeat"))["outcome"] == "nothing_to_bind"


async def test_apply_requires_reason_and_wrong_repository_receipt_is_unproven(db):
    await db.create_repo(RepoConfig(
        id="other", project_id="p", source_type=RepoSourceType.CLONE,
        url="https://github.com/acme/other.git",
    ))
    await db.create_task(Task(id="parent", project_id="p", title="parent",
                              description="", status=TaskStatus.COMPLETED))
    await db.create_task(Task(id="child", project_id="p", title="child",
                              description="", parent_task_id="parent",
                              status=TaskStatus.COMPLETED))
    now = time.time() + 1
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            integration_repository_id="repo", hierarchical_integration_mode="observe",
        ))
        await conn.execute(insert(task_completion_records).values(
            id="completion-child", task_id="child", outcome="pass",
            commits=json.dumps(["b" * 40]), completed_at=now,
        ))
        await conn.execute(insert(development_deliveries).values(
            id="wrong-delivery", project_id="p", repository_id="other",
            target_ref="refs/heads/main", state="delivered",
            manifest=[{"task_id": "child", "source_sha": "b" * 40}],
            evidence={}, reason="wrong repository", created_at=now, updated_at=now,
        ))
    service = LegacyRepositoryBinding(db)
    assert (await service.run("p", principal="supervisor", dry_run=False))["outcome"] == "invalid"
    preview = await service.run("p", principal="supervisor")
    assert preview["bound"] == []
    assert preview["unproven"] == ["child", "parent"]
    assert (await db.get_task("child")).repo_id is None
