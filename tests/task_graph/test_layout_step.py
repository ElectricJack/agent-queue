import asyncio
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
    # The first step's sweep also queued this install's engine-rules rebuild
    # (§3.3), and the queue is FIFO, so the tidy is one step behind it — at
    # most one rebuild, since convergence never queues a second while one is
    # in flight.
    await o._run_layout_step()
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


async def test_schedule_layout_step_runs_in_the_background_and_does_not_overlap(orchestrator_factory):
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_step(self):
        started.set()
        await release.wait()

    with patch.object(type(o), "_run_layout_step", new=slow_step):
        first = o.schedule_layout_step()
        await started.wait()
        second = o.schedule_layout_step()
        assert second is first  # one in flight at a time
        release.set()
        await o.wait_for_layout_step()
        assert first.done()

    # After the in-flight task finishes, the next call starts a real step.
    third = o.schedule_layout_step()
    assert third is not first
    await o.wait_for_layout_step()
    assert (await o.db.get_layout_meta("p1", "all"))["node_count"] == 1


# ── engine-rules convergence (reorganisation design §3.3) ───────────────


def _rules_kind() -> str:
    from src.orchestrator import layout_step as mod

    return f"rules:{mod.ENGINE_RULES_VERSION}"


async def _rules_jobs(o, kind: str | None = None) -> list[dict]:
    """Every convergence-ledger row, oldest first."""
    from sqlalchemy import select

    from src.database.tables import layout_jobs

    async with o.db._engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    select(layout_jobs)
                    .where(layout_jobs.c.kind == (kind or _rules_kind()))
                    .order_by(layout_jobs.c.requested_at, layout_jobs.c.id)
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _laid_out_install(o, n: int = 3):
    """``n`` projects with both variants published and an empty ledger."""
    from sqlalchemy import delete

    from src.database.tables import layout_jobs

    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    for i in range(n):
        pid = f"p{i:02d}"
        await o.db.create_project(Project(id=pid, name=pid))
        await o.db.create_task(Task(id=f"t{i:02d}", project_id=pid, title="t", description=""))
    await o._run_layout_step()
    for i in range(n):
        for variant in ("active", "all"):
            assert await o.db.get_layout_meta(f"p{i:02d}", variant) is not None
    # That first step also swept; start every test from a clean ledger.
    async with o.db._engine.begin() as conn:
        await conn.execute(delete(layout_jobs))
    o._layout_last_reconcile_check = None


async def _sweep(o) -> None:
    """One cycle whose reconcile interval has elapsed."""
    o._layout_last_reconcile_check = None
    await o._run_layout_step()


async def test_the_sweep_enqueues_one_stale_pair_per_sweep_active_first(orchestrator_factory):
    """A full layout is a CPU-bound thread and the step takes one job per
    cycle, so a fan-out of every (project, variant) would put a long tail of
    rebuilds in front of everyone's incremental work."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=3)

    await _sweep(o)

    jobs = await _rules_jobs(o)
    assert len(jobs) == 1
    assert (jobs[0]["project_id"], jobs[0]["variant"]) == ("p00", "active")
    assert jobs[0]["status"] == "queued"


async def test_successive_sweeps_walk_every_stale_pair(orchestrator_factory):
    o = await orchestrator_factory()
    await _laid_out_install(o, n=3)

    for _ in range(6):
        await _sweep(o)

    jobs = await _rules_jobs(o)
    assert [(j["project_id"], j["variant"]) for j in jobs] == [
        ("p00", "active"),
        ("p00", "all"),
        ("p01", "active"),
        ("p01", "all"),
        ("p02", "active"),
        ("p02", "all"),
    ]
    # A seventh sweep has nothing left to do.
    await _sweep(o)
    assert len(await _rules_jobs(o)) == 6


async def test_a_converged_pair_is_never_re_enqueued(orchestrator_factory):
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    done = await o.db.enqueue_layout_job("p00", "active", _rules_kind())
    await o.db.finish_layout_job(done["id"], error=None)

    await _sweep(o)

    jobs = await _rules_jobs(o)
    assert [(j["project_id"], j["variant"]) for j in jobs] == [
        ("p00", "active"),
        ("p00", "all"),
    ]


async def test_a_failed_rules_job_is_retried(orchestrator_factory):
    """A failed rebuild is not convergence — the pair still carries the old
    geometry, so a later sweep must pick it up again."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    bad = await o.db.enqueue_layout_job("p00", "active", _rules_kind())
    await o.db.finish_layout_job(bad["id"], error="boom")

    await _sweep(o)

    jobs = await _rules_jobs(o)
    assert len(jobs) == 2
    retry = [j for j in jobs if j["id"] != bad["id"]]
    assert [(j["project_id"], j["variant"]) for j in retry] == [("p00", "active")]


async def test_an_unrelated_tidy_in_flight_defers_the_pair(orchestrator_factory):
    """``enqueue_layout_job`` de-dupes on (project, variant, status) whatever
    the kind, so an operator tidy already queued hands its own row back — the
    pair must be left stale rather than recorded as converged."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    tidy = await o.db.enqueue_layout_job("p00", "active", "tidy")

    await o._converge_engine_rules(await o.db.list_projects())

    assert await o.db.layout_job_exists("p00", "active", _rules_kind()) is False
    assert (await o.db.get_layout_job(tidy["id"]))["kind"] == "tidy"
    # The walk moved on to the next pair rather than spending the sweep.
    assert [(j["project_id"], j["variant"]) for j in await _rules_jobs(o)] == [("p00", "all")]

    # Once the queue drains, the deferred pair is picked up.
    await o.db.finish_layout_job(tidy["id"], error=None)
    for job in await _rules_jobs(o):
        await o.db.finish_layout_job(job["id"], error=None)
    await o._converge_engine_rules(await o.db.list_projects())
    assert await o.db.layout_job_exists("p00", "active", _rules_kind()) is True


async def test_a_project_with_no_meta_row_is_skipped(orchestrator_factory):
    """Nothing published means the project's first full layout already uses
    today's rules; enqueuing a tidy for it would rebuild nothing."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    await o.db.create_project(Project(id="p000", name="fresh"))  # sorts first
    assert await o.db.get_layout_meta("p000", "active") is None

    await o._converge_engine_rules(await o.db.list_projects())

    assert [(j["project_id"], j["variant"]) for j in await _rules_jobs(o)] == [("p00", "active")]


async def test_the_sweep_is_bounded_when_nothing_is_stale(orchestrator_factory):
    """Steady state: one ledger read per pair, on the reconcile interval
    only, and no writes."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=3)
    for i in range(3):
        for variant in ("active", "all"):
            job = await o.db.enqueue_layout_job(f"p{i:02d}", variant, _rules_kind())
            await o.db.finish_layout_job(job["id"], error=None)
    before = len(await _rules_jobs(o))

    reads: list[tuple] = []
    real = o.db.layout_job_exists

    async def counting(project_id, variant, kind):
        reads.append((project_id, variant, kind))
        return await real(project_id, variant, kind)

    o.db.layout_job_exists = counting
    await o._converge_engine_rules(await o.db.list_projects())

    assert len(reads) == 6 == len(set(reads))
    assert len(await _rules_jobs(o)) == before


async def test_convergence_stands_down_while_a_rules_job_is_in_flight(orchestrator_factory):
    """One rebuild at a time across the whole fleet: a wedged job queue must
    not accumulate a convergence backlog in front of ordinary work."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=3)
    await o.db.enqueue_layout_job("p00", "active", _rules_kind())

    await o._converge_engine_rules(await o.db.list_projects())

    assert len(await _rules_jobs(o)) == 1


async def test_bumping_the_engine_rules_version_makes_every_pair_stale_again(
    orchestrator_factory,
):
    """Bumping the constant is the only action a rules change needs: a
    ``rules:1`` ledger row does not satisfy ``rules:2``."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    for variant in ("active", "all"):
        job = await o.db.enqueue_layout_job("p00", variant, "rules:1")
        await o.db.finish_layout_job(job["id"], error=None)

    with patch("src.orchestrator.layout_step.ENGINE_RULES_VERSION", 2):
        await o._converge_engine_rules(await o.db.list_projects())
        assert [(j["project_id"], j["variant"]) for j in await _rules_jobs(o, "rules:2")] == [
            ("p00", "active")
        ]
    assert len(await _rules_jobs(o, "rules:1")) == 2


async def test_no_convergence_while_the_layout_feature_is_disabled(orchestrator_factory):
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    o.config.graph_layout.enabled = False

    await _sweep(o)

    assert await _rules_jobs(o) == []
