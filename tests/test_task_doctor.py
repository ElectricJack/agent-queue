"""Doctor coverage for stale task attention metadata."""

import pytest

from src.database import Database
from src.doctor.models import Severity
from src.doctor.task_checks import run_check
from src.models import Project, Task, TaskStatus


@pytest.mark.asyncio
async def test_stale_attention_check_reports_and_repairs_live_and_completed_rows(tmp_path):
    db = Database(str(tmp_path / "doctor.db"))
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
