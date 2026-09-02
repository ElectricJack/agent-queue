from unittest.mock import AsyncMock, patch

from src.models import Project, Task
from src.task_graph.layout.driver import LayoutDriver, LayoutRelayDepthExceeded


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


async def test_layout_step_caps_dirty_projects_per_cycle(orchestrator_factory):
    """12 dirty projects: 10 this cycle, the remaining 2 on the next one."""
    from src.orchestrator.layout_step import MAX_LAYOUT_PROJECTS_PER_CYCLE

    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    for i in range(12):
        pid = f"p{i:02d}"
        await o.db.create_project(Project(id=pid, name=pid))
        await o.db.create_task(Task(id=f"t{i:02d}", project_id=pid, title="t", description=""))

    seen: list[list[str]] = []
    orig = LayoutDriver.process_dirty

    async def spy(self, pid, **kw):
        seen[-1].append(pid)
        return await orig(self, pid, **kw)

    with patch.object(LayoutDriver, "process_dirty", spy):
        seen.append([])
        await o._run_layout_step()
        assert len(seen[-1]) == MAX_LAYOUT_PROJECTS_PER_CYCLE == 10
        first = list(seen[-1])
        seen.append([])
        await o._run_layout_step()
    assert len(seen[-1]) == 2
    assert set(first) | set(seen[-1]) == {f"p{i:02d}" for i in range(12)}


async def test_layout_step_swallows_a_raising_db_call(orchestrator_factory):
    """The step is a projection: a failure must never reach ``run_one_cycle``."""
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.db.next_layout_job = AsyncMock(side_effect=RuntimeError("boom"))
    await o._run_layout_step()  # must not raise


async def test_layout_step_trims_dirty_marks_while_disabled(orchestrator_factory):
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = False
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    assert await o.db.dirty_layout_projects() == ["p1"]

    await o._run_layout_step()
    assert await o.db.dirty_layout_projects() == []
    assert await o.db.get_layout_meta("p1", "all") is None  # nothing processed

    # The trim is gated on the reconcile interval, so a second cycle inside
    # the same window does not re-run it.
    await o.db.create_task(Task(id="b", project_id="p1", title="b", description=""))
    await o._run_layout_step()
    assert await o.db.dirty_layout_projects() == ["p1"]


async def test_layout_step_reconcile_sweep_is_interval_gated(orchestrator_factory):
    """The sweep polls ``get_layout_meta`` once per project, so it must not
    run every 5-second cycle — only once per ``reconcile_interval_seconds``."""
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))

    sweeps = 0
    real = o.db.list_projects

    async def counting(*a, **kw):
        nonlocal sweeps
        sweeps += 1
        return await real(*a, **kw)

    o.db.list_projects = counting
    await o._run_layout_step()
    assert sweeps == 1
    await o._run_layout_step()  # inside the interval
    assert sweeps == 1

    o._layout_last_reconcile_check -= o.config.graph_layout.reconcile_interval_seconds + 1
    await o._run_layout_step()
    assert sweeps == 2
