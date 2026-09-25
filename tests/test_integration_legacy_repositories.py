"""Evidence and dry-run behavior for terminal legacy repository binding."""

import json
import time

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    development_deliveries,
    integration_legacy_deliveries,
    projects,
    task_comments,
    task_completion_records,
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


async def _legacy_delivery(conn, task_id, parent, proof, *, repository_id="repo"):
    """The row ``aq integration adopt-legacy-deliveries`` records for *task_id*."""
    await conn.execute(insert(integration_legacy_deliveries).values(
        task_id=task_id, project_id="p", parent_task_id=parent,
        repository_id=repository_id, target_ref="refs/heads/main", target_sha="c" * 40,
        delivered_sha="d" * 40 if proof == "superseded" else None, proof=proof,
        operator_id="supervisor", reason="operator decision", created_at=time.time(),
    ))


async def test_recorded_legacy_deliveries_prove_children_and_their_parent(db):
    """Supersede, retire and accept decisions are proof; a parent needs every child proven."""
    for task_id, parent, status in (
        ("parent", None, TaskStatus.COMPLETED),
        ("parent.receipt", "parent", TaskStatus.COMPLETED),
        ("parent.superseded", "parent", TaskStatus.COMPLETED),
        ("parent.retired", "parent", TaskStatus.FAILED),
        ("parent.accepted", "parent", TaskStatus.COMPLETED),
        ("elsewhere", None, TaskStatus.COMPLETED),
        ("elsewhere.1", "elsewhere", TaskStatus.COMPLETED),
        ("reopened", None, TaskStatus.COMPLETED),
        ("reopened.1", "reopened", TaskStatus.IN_PROGRESS),
    ):
        await db.create_task(Task(
            id=task_id, project_id="p", title=task_id, description="",
            parent_task_id=parent, status=status,
        ))
    await db.create_repo(RepoConfig(
        id="other", project_id="p", source_type=RepoSourceType.CLONE,
        url="https://github.com/acme/other.git",
    ))
    now = time.time() + 1
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            integration_repository_id="repo", hierarchical_integration_mode="observe",
        ))
        await conn.execute(insert(task_completion_records).values(
            id="completion-receipt", task_id="parent.receipt", outcome="pass",
            commits=json.dumps(["a" * 40]), completed_at=now,
        ))
        await conn.execute(insert(development_deliveries).values(
            id="delivery-receipt", project_id="p", repository_id="repo",
            target_ref="refs/heads/main", prepared_sha="a" * 40, state="delivered",
            manifest=[{"task_id": "parent.receipt", "source_sha": "a" * 40}],
            evidence={}, reason="published", created_at=now, updated_at=now,
        ))
        await _legacy_delivery(conn, "parent.superseded", "parent", "superseded")
        await _legacy_delivery(conn, "parent.retired", "parent", "abandoned")
        await _legacy_delivery(conn, "parent.accepted", "parent", "operator_accepted")
        # Recorded against a repository that is no longer the designated one.
        await _legacy_delivery(conn, "elsewhere.1", "elsewhere", "operator_accepted",
                               repository_id="other")
        # Reopened after adoption: live work again, not a historical delivery.
        await _legacy_delivery(conn, "reopened.1", "reopened", "operator_accepted")

    service = LegacyRepositoryBinding(db)
    preview = await service.run("p", principal="supervisor", dry_run=True)
    assert preview["bound"] == [
        {"task_id": "parent", "proof": "delivered_children"},
        {"task_id": "parent.accepted", "proof": "legacy_delivery",
         "legacy_proof": "operator_accepted"},
        {"task_id": "parent.receipt", "proof": "development_delivery"},
        {"task_id": "parent.retired", "proof": "legacy_delivery", "legacy_proof": "abandoned"},
        {"task_id": "parent.superseded", "proof": "legacy_delivery",
         "legacy_proof": "superseded"},
    ]
    assert preview["unproven"] == ["elsewhere", "elsewhere.1", "reopened"]

    applied = await service.run("p", principal="supervisor", dry_run=False,
                                reason="bind attested legacy hierarchy")
    assert applied["bound"] == preview["bound"]
    for item in preview["bound"]:
        assert (await db.get_task(item["task_id"])).repo_id == "repo"
    for task_id in ("elsewhere", "elsewhere.1", "reopened", "reopened.1"):
        assert (await db.get_task(task_id)).repo_id is None
    async with db._engine.connect() as conn:
        body = (await conn.execute(select(task_comments.c.body).where(
            task_comments.c.task_id == "parent.superseded"
        ))).scalar_one()
    assert "proof=legacy_delivery (superseded)" in body
    blockers = (await IntegrationStatusService(db).task_blockers("parent"))["blockers"]
    assert "repository_not_designated" not in {item["code"] for item in blockers}
