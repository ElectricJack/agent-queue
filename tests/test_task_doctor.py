"""Doctor coverage for stale task attention metadata."""

import pytest

from src.database import Database
from src.doctor.models import DoctorContext, Severity
from src.doctor.task_checks import container_dependency_report, run_check
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn


async def test_container_dependencies_report_ready_leaf_edges_and_preserve_graph():
    from src.doctor.stall_checks import _container_dependency_findings

    db = Database(lease_dsn("doctor-container-deps.db"))
    await db.initialize()
    try:
        for project_id in ("p", "other"):
            await db.create_project(Project(id=project_id, name=project_id))
        for tid in ("container", "unflagged", "empty", "leaf", "child", "other-epic",
                    "ready", "blocked", "done", "failed", "provenance", "membership"):
            await db.create_task(Task(id=tid, project_id="p", title=tid, description=""))
        await db.create_task(Task(id="cross", project_id="other", title="cross", description=""))
        await db.add_dependency("leaf", "container", "parent-child")
        await db.add_dependency("child", "unflagged", "parent-child")
        await db.delete_task_meta("unflagged", "container")
        async with db._engine.begin() as conn:
            await db.declare_container("empty", conn=conn)
            await db.declare_container("other-epic", conn=conn)
        for tid, target, dep_type in (
            ("ready", "container", "waits-for"), ("blocked", "unflagged", "blocks"),
            ("blocked", "empty", "conditional-blocks"), ("cross", "container", "blocks"),
            ("done", "container", "blocks"), ("failed", "container", "blocks"),
            ("other-epic", "container", "blocks"), ("provenance", "container", "related"),
            ("membership", "container", "parent-child"), ("ready", "leaf", "blocks"),
        ):
            await db.add_dependency(tid, target, dep_type)
        for tid, status in (("ready", TaskStatus.READY), ("blocked", TaskStatus.BLOCKED),
                            ("done", TaskStatus.COMPLETED), ("failed", TaskStatus.FAILED)):
            await db.update_task(tid, status=status)
        before = await db.get_typed_dependencies("ready")

        result = await run_check(db, "tasks.container_dependencies")
        assert result.severity is Severity.WARN and not result.fixable
        assert result.data["truncated"] is False
        assert {(e["task_id"], e["depends_on"], e["dep_type"]) for e in result.data["edges"]} == {
            ("ready", "container", "waits-for"), ("blocked", "unflagged", "blocks"),
            ("blocked", "empty", "conditional-blocks"), ("cross", "container", "blocks"),
        }
        ctx = DoctorContext(config=None, db=db)
        findings = await _container_dependency_findings(ctx, {"p"})
        assert len(findings) == 3
        assert all(f["kind"] == "container_dependency" and f["project_id"] == "p" for f in findings)
        assert any("ready [READY]" in f["detail"] and "container" in f["detail"] for f in findings)
        assert await container_dependency_report(ctx, project_ids=set()) == {
            "edges": [], "truncated": False,
        }
        assert await db.get_typed_dependencies("ready") == before
        assert (await db.get_task("ready")).status is TaskStatus.READY
    finally:
        await db.close()


async def test_container_dependencies_empty_and_unavailable_database():
    assert (await run_check(None, "tasks.container_dependencies")).severity is Severity.INFO
    db = Database(lease_dsn("doctor-container-empty.db"))
    await db.initialize()
    try:
        assert (await run_check(db, "tasks.container_dependencies")).severity is Severity.OK
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_attention_check_reports_and_repairs_live_and_completed_rows(tmp_path):
    db = Database(lease_dsn("doctor.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="p"))
    for task_id, status in (("live", TaskStatus.IN_PROGRESS), ("done", TaskStatus.COMPLETED)):
        await db.create_task(Task(id=task_id, project_id="p", title=task_id, description="", status=status))
        await db.set_task_meta(task_id, "needs_attention", "slot_reset_failed")
    await db.create_task(Task(id="ready", project_id="p", title="ready", description="", status=TaskStatus.READY))
    await db.set_task_meta("ready", "needs_attention", "unresolved")

    finding = await run_check(db, "tasks.stale_attention")
    assert finding.severity is Severity.WARN
    assert finding.fixable and finding.data["count"] == 2
    repaired = await run_check(db, "tasks.stale_attention", repair=True)
    assert repaired.severity is Severity.OK
    assert await db.get_task_meta("live", "needs_attention") is None
    assert await db.get_task_meta("done", "needs_attention") is None
    assert await db.get_task_meta("ready", "needs_attention") == "unresolved"
    await db.close()


@pytest.mark.asyncio
async def test_archive_history_does_not_create_an_archive_blocker():
    """Append-only integration history survives a successful archive by id."""
    from sqlalchemy import insert

    from src.config import AppConfig, ArchiveConfig
    from src.database.tables import integration_parent_episodes
    from src.models import RepoConfig, RepoSourceType

    db = Database(lease_dsn("doctor-archive.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="p"))
    await db.create_repo(RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK))
    for task_id in ("tracked", "plain"):
        await db.create_task(
            Task(
                id=task_id,
                project_id="p",
                title=task_id,
                description="",
                status=TaskStatus.COMPLETED,
            )
        )
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="ep",
                parent_task_id="tracked",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )
    config = AppConfig(archive=ArchiveConfig(enabled=True, after_hours=0, statuses=["COMPLETED"]))
    # The check reports what the sweep recorded, so the sweep has to have run.
    await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=0)

    finding = await run_check(db, "tasks.archive_blocked", config=config)

    assert finding.severity is Severity.OK
    assert finding.fixable is False
    assert finding.data == {}
    assert await db.get_task("tracked") is None
    assert await db.get_task("plain") is None
    await db.close()


@pytest.mark.asyncio
async def test_archive_blocked_count_is_the_true_total_when_the_list_is_capped():
    """The count may not saturate at the page size (F2)."""

    from src.config import AppConfig, ArchiveConfig
    from src.database.queries.archive_queries import ARCHIVE_REFUSAL_KEY
    from src.models import RepoConfig, RepoSourceType

    db = Database(lease_dsn("doctor-archive-cap.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="p"))
    await db.create_repo(RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK))
    for n in range(60):
        await db.create_task(
            Task(
                id=f"root-{n:02d}",
                project_id="p",
                title="t",
                description="",
                status=TaskStatus.COMPLETED,
            )
        )
        await db.set_task_meta(
            f"root-{n:02d}", ARCHIVE_REFUSAL_KEY, {"code": "sealed", "detail": "d", "at": 1.0}
        )
    config = AppConfig(archive=ArchiveConfig(enabled=True, after_hours=0, statuses=["COMPLETED"]))

    finding = await run_check(db, "tasks.archive_blocked", config=config)

    assert finding.data["count"] == 60
    assert len(finding.data["roots"]) == 50
    assert finding.detail.startswith("60 terminal root(s)")
    await db.close()


@pytest.mark.asyncio
async def test_archive_blocked_check_is_informational_when_auto_archive_is_off():
    from src.config import AppConfig, ArchiveConfig

    db = Database(lease_dsn("doctor-archive-off.db"))
    await db.initialize()
    config = AppConfig(archive=ArchiveConfig(enabled=False))

    finding = await run_check(db, "tasks.archive_blocked", config=config)

    assert finding.severity is Severity.INFO
    assert "disabled" in finding.detail
    await db.close()
