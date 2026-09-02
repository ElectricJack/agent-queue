from unittest.mock import AsyncMock, patch

from src.models import Project, Task
from src.task_graph.layout.driver import LayoutRelayDepthExceeded


async def test_layout_step_processes_dirty_and_jobs(orchestrator_factory):
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await o._run_layout_step()  # no meta yet -> full layout via process_dirty
    assert (await o.db.get_layout_meta("p1", "all"))["node_count"] == 1
    job = await o.db.enqueue_layout_job("p1", "all", "tidy")
    await o._run_layout_step()
    assert (await o.db.get_layout_job(job["id"]))["status"] == "done"


async def test_layout_step_is_noop_when_disabled(orchestrator_factory):
    o = await orchestrator_factory()
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await o._run_layout_step()
    assert await o.db.get_layout_meta("p1", "all") is None


async def test_layout_step_escalates_relay_depth_exceeded_to_tidy_jobs(orchestrator_factory):
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))

    async with o.db._engine.begin() as conn:
        await o.db.mark_layout_dirty("p1", ["a"], "created", conn=conn)

    with patch(
        "src.task_graph.layout.driver.LayoutDriver.process_dirty",
        new=AsyncMock(side_effect=LayoutRelayDepthExceeded("boom")),
    ):
        await o._run_layout_step()
        await o._run_layout_step()
        await o._run_layout_step()

    from sqlalchemy import select

    from src.database.tables import layout_jobs

    async with o.db._engine.begin() as conn:
        rows = (
            await conn.execute(
                select(layout_jobs).where(
                    layout_jobs.c.project_id == "p1",
                    layout_jobs.c.kind == "tidy",
                    layout_jobs.c.status == "queued",
                )
            )
        ).mappings().all()
    assert {r["variant"] for r in rows} == {"all", "active"}
    assert o._layout_failures.get("p1", 0) == 0
