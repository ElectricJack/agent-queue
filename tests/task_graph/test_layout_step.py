import asyncio
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

from src.models import Project, ProjectStatus, Task
from src.task_graph.layout.constants import ROOT
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


async def test_row_aspect_reload_relays_out_every_published_pair(orchestrator_factory):
    import copy

    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    # Keep the engine-rules sweep from adding an unrelated queue entry: this
    # test isolates the automatic all-project request made by the config hook.
    for variant in ("active", "all"):
        job = await o.db.enqueue_layout_job("p00", variant, _rules_kind())
        await o.db.finish_layout_job(job["id"], error=None)
    before = {
        variant: (await o.db.get_layout_meta("p00", variant))["layout_version"]
        for variant in ("active", "all")
    }
    previous = copy.deepcopy(o.config)
    changed = copy.deepcopy(o.config)
    changed.graph_layout.row_aspect = 2.0

    await o._on_config_reloaded(
        {
            "config": changed,
            "previous_config": previous,
            "changed_sections": ["graph_layout"],
        }
    )
    request = await o.db.create_layout_tidy_request(reason="row_aspect:2")
    assert request["total"] == 2 and request["pending"] == 2

    # First pass releases active, then each following pass runs one full
    # layout and releases the next durable pair.
    await o._run_layout_step()
    await o._run_layout_step()
    await o._run_layout_step()
    assert {
        variant: (await o.db.get_layout_meta("p00", variant))["layout_version"] > before[variant]
        for variant in ("active", "all")
    } == {"active": True, "all": True}


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

    # One sweep reads the project list twice: once for the reconcile loop and
    # once, active-only, for the engine-rules convergence step.
    o.db.list_projects = counting
    await o._run_layout_step()
    assert sweeps == 2
    await o._run_layout_step()  # inside the interval
    assert sweeps == 2

    o._layout_last_reconcile_check -= o.config.graph_layout.reconcile_interval_seconds + 1
    await o._run_layout_step()
    assert sweeps == 4


async def test_queued_reflow_does_not_load_a_snapshot_before_the_sweep(orchestrator_factory):
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    driver = LayoutDriver(o.db)
    for variant in ("all", "active"):
        await driver.full_layout("p1", variant)
    await driver.process_dirty("p1", min_age_seconds=0)
    await o.db.enqueue_layout_reflows("p1", "active", [ROOT])

    # Reflow rows are not dirty marks.  An idle five-second cycle leaves the
    # queue alone and therefore cannot pay for a project snapshot.
    o._layout_last_reconcile_check = time.monotonic()
    real = o.db.load_project_snapshot
    calls = 0

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real(*args, **kwargs)

    o.db.load_project_snapshot = counted
    await o._run_layout_step()
    assert calls == 0
    assert (await o.db.layout_reflow_status())["queued"] == 1


async def test_layout_sweep_claims_and_acknowledges_one_reflow_group(orchestrator_factory):
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    driver = LayoutDriver(o.db)
    for variant in ("all", "active"):
        await driver.full_layout("p1", variant)
    await driver.process_dirty("p1", min_age_seconds=0)
    before = (await o.db.get_layout_meta("p1", "active"))["layout_version"]
    await o.db.enqueue_layout_reflows("p1", "active", [ROOT])

    o._layout_last_reconcile_check = None  # force the periodic sweep
    await o._run_layout_step()

    assert (await o.db.layout_reflow_status())["queued"] == 0
    assert (await o.db.get_layout_meta("p1", "active"))["layout_version"] == before + 1


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


async def _converge(o) -> None:
    """The convergence step on its own, with the step's own budget."""
    await o._converge_engine_rules(
        tidy_job_seconds=o.config.graph_layout.tidy_job_budget_seconds
    )


async def _force_running(o, job_id: str, *, age_seconds: float) -> None:
    """Leave a job the way a daemon killed mid-rebuild leaves it."""
    from sqlalchemy import update

    from src.database.tables import layout_jobs

    async with o.db._engine.begin() as conn:
        await conn.execute(
            update(layout_jobs)
            .where(layout_jobs.c.id == job_id)
            .values(status="running", started_at=time.time() - age_seconds)
        )


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

    # Never-attempted pairs go first (a failing pair must not head-of-line
    # block the fleet), so the retry lands on the sweep after.
    queued = [j for j in await _rules_jobs(o) if j["status"] == "queued"]
    assert [(j["project_id"], j["variant"]) for j in queued] == [("p00", "all")]
    await o.db.finish_layout_job(queued[0]["id"], error=None)

    await _converge(o)
    queued = [j for j in await _rules_jobs(o) if j["status"] == "queued"]
    assert [(j["project_id"], j["variant"]) for j in queued] == [("p00", "active")]
    assert bad["id"] not in {j["id"] for j in queued}


async def test_an_unrelated_tidy_in_flight_defers_the_pair(orchestrator_factory):
    """``enqueue_layout_job`` de-dupes on (project, variant, status) whatever
    the kind, so an operator tidy already queued hands its own row back — the
    pair must be left stale rather than recorded as converged."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    tidy = await o.db.enqueue_layout_job("p00", "active", "tidy")

    await _converge(o)

    assert ("p00", "active") not in await o.db.layout_job_ledger(_rules_kind())
    assert (await o.db.get_layout_job(tidy["id"]))["kind"] == "tidy"
    # The walk moved on to the next pair rather than spending the sweep.
    assert [(j["project_id"], j["variant"]) for j in await _rules_jobs(o)] == [("p00", "all")]

    # Once the queue drains, the deferred pair is picked up.
    await o.db.finish_layout_job(tidy["id"], error=None)
    for job in await _rules_jobs(o):
        await o.db.finish_layout_job(job["id"], error=None)
    await _converge(o)
    assert (await o.db.layout_job_ledger(_rules_kind()))[("p00", "active")]["settled"]


async def test_a_project_with_no_meta_row_is_skipped(orchestrator_factory):
    """Nothing published means the project's first full layout already uses
    today's rules; enqueuing a tidy for it would rebuild nothing."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    await o.db.create_project(Project(id="p000", name="fresh"))  # sorts first
    assert await o.db.get_layout_meta("p000", "active") is None

    await _converge(o)

    assert [(j["project_id"], j["variant"]) for j in await _rules_jobs(o)] == [("p00", "active")]


async def _count_statements(o) -> int:
    from sqlalchemy import event

    seen: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().upper()
        if head.startswith(("SELECT", "UPDATE", "INSERT", "DELETE")):
            seen.append(statement)

    event.listen(o.db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        await _converge(o)
    finally:
        event.remove(o.db._engine.sync_engine, "before_cursor_execute", _hook)
    return len(seen)


async def _settle_every_pair(o, n: int) -> None:
    for i in range(n):
        for variant in ("active", "all"):
            job = await o.db.enqueue_layout_job(f"p{i:02d}", variant, _rules_kind())
            await o.db.finish_layout_job(job["id"], error=None)


async def test_the_sweep_is_bounded_when_nothing_is_stale(orchestrator_factory):
    """Steady state costs a FIXED number of statements — the same for three
    projects as for six — and writes nothing."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=3)
    await _settle_every_pair(o, 3)
    before = len(await _rules_jobs(o))

    small = await _count_statements(o)
    assert len(await _rules_jobs(o)) == before

    for i in range(3, 6):
        pid = f"p{i:02d}"
        await o.db.create_project(Project(id=pid, name=pid))
        await o.db.create_task(Task(id=f"t{i:02d}", project_id=pid, title="t", description=""))
    o.config.graph_layout.incremental_debounce_ms = 0
    await o._run_layout_step()  # publish the three new projects
    async with o.db._engine.begin() as conn:
        from sqlalchemy import delete as sa_delete

        from src.database.tables import layout_jobs as lj

        await conn.execute(sa_delete(lj).where(lj.c.kind == _rules_kind(), lj.c.status != "done"))
    await _settle_every_pair(o, 6)

    big = await _count_statements(o)
    assert big == small, (big, small)
    assert small <= 6, small


async def test_convergence_stands_down_while_a_rules_job_is_in_flight(orchestrator_factory):
    """One rebuild at a time across the whole fleet: a wedged job queue must
    not accumulate a convergence backlog in front of ordinary work."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=3)
    await o.db.enqueue_layout_job("p00", "active", _rules_kind())

    await _converge(o)

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
        await _converge(o)
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


# ── orphan reaping and retry budget (review F1/F2) ─────────────────────


async def test_a_job_stuck_running_is_reaped_and_convergence_resumes(orchestrator_factory):
    """A daemon killed mid-rebuild leaves `status='running'` forever: nothing
    ever claims it again, so the fleet-wide stand-down would silently disable
    convergence for every project."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    budget = o.config.graph_layout.tidy_job_budget_seconds
    orphan = await o.db.enqueue_layout_job("p00", "active", _rules_kind())
    await _force_running(o, orphan["id"], age_seconds=budget * 4 + 10)

    await _converge(o)

    reaped = await o.db.get_layout_job(orphan["id"])
    assert reaped["status"] == "failed"
    assert "orphaned" in (reaped["error"] or "")
    # Convergence is no longer wedged: the never-attempted pair goes first.
    assert [(j["project_id"], j["variant"]) for j in await _rules_jobs(o) if j["id"] != orphan["id"]] == [
        ("p00", "all")
    ]

    # …and the reaped pair, whose rebuild never finished, is retried after it.
    for job in await _rules_jobs(o):
        if job["status"] == "queued":
            await o.db.finish_layout_job(job["id"], error=None)
    await _converge(o)
    fresh = [j for j in await _rules_jobs(o) if j["status"] == "queued"]
    assert [(j["project_id"], j["variant"]) for j in fresh] == [("p00", "active")]


async def test_a_running_job_inside_its_budget_is_left_alone(orchestrator_factory):
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    budget = o.config.graph_layout.tidy_job_budget_seconds
    live = await o.db.enqueue_layout_job("p00", "active", _rules_kind())
    await _force_running(o, live["id"], age_seconds=budget)

    await _converge(o)

    assert (await o.db.get_layout_job(live["id"]))["status"] == "running"
    assert len(await _rules_jobs(o)) == 1  # stood down behind the live job


async def test_an_operator_tidy_stuck_running_is_reaped_too(orchestrator_factory):
    """The reaper is about `layout_jobs`, not about convergence."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    budget = o.config.graph_layout.tidy_job_budget_seconds
    tidy = await o.db.enqueue_layout_job("p00", "all", "tidy")
    await _force_running(o, tidy["id"], age_seconds=budget * 4 + 1)

    await _converge(o)

    assert (await o.db.get_layout_job(tidy["id"]))["status"] == "failed"


async def test_a_cancelled_job_is_recorded_failed_rather_than_left_running(orchestrator_factory):
    """Shutdown cancels the background step; the runner must not leave the
    row it claimed stuck in `running`."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    job = await o.db.enqueue_layout_job("p00", "all", "tidy")

    with patch(
        "src.task_graph.layout.driver.LayoutDriver.full_layout",
        new=AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await o._run_layout_step()

    row = await o.db.get_layout_job(job["id"])
    assert row["status"] == "failed" and "cancel" in (row["error"] or "")


async def test_a_failing_pair_does_not_block_the_other_projects(orchestrator_factory):
    """The walk is ordered and returns at the first pair it enqueues, so a
    deterministically failing pair would otherwise be re-picked forever."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=3)
    failed = await o.db.enqueue_layout_job("p00", "active", _rules_kind())
    await o.db.finish_layout_job(failed["id"], error="boom")

    picked: list[tuple[str, str]] = []
    for _ in range(5):
        await _converge(o)
        queued = [j for j in await _rules_jobs(o) if j["status"] == "queued"]
        assert len(queued) == 1
        picked.append((queued[0]["project_id"], queued[0]["variant"]))
        await o.db.finish_layout_job(queued[0]["id"], error=None)

    # Pass 1 — every never-attempted pair — comes before the failed retry.
    assert picked == [
        ("p00", "all"),
        ("p01", "active"),
        ("p01", "all"),
        ("p02", "active"),
        ("p02", "all"),
    ]
    await _converge(o)
    retry = [j for j in await _rules_jobs(o) if j["status"] == "queued"]
    assert [(j["project_id"], j["variant"]) for j in retry] == [("p00", "active")]


async def test_a_failed_pair_is_retried_at_most_three_times(orchestrator_factory, caplog):
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    for variant in ("active", "all"):
        for _ in range(3 if variant == "active" else 1):
            job = await o.db.enqueue_layout_job("p00", variant, _rules_kind())
            await o.db.finish_layout_job(job["id"], error="boom" if variant == "active" else None)

    with caplog.at_level(logging.WARNING, logger="src.orchestrator.layout_step"):
        await _converge(o)

    assert [j for j in await _rules_jobs(o) if j["status"] == "queued"] == []
    warned = [r.getMessage() for r in caplog.records]
    assert any("p00" in m and "active" in m and "giving up" in m for m in warned), warned


async def test_a_version_bump_resets_the_retry_budget(orchestrator_factory):
    o = await orchestrator_factory()
    await _laid_out_install(o, n=1)
    current_kind = _rules_kind()
    next_version = int(current_kind.removeprefix("rules:")) + 1
    next_kind = f"rules:{next_version}"
    for variant in ("active", "all"):
        for _ in range(3):
            job = await o.db.enqueue_layout_job("p00", variant, current_kind)
            await o.db.finish_layout_job(job["id"], error="boom")

    await _converge(o)
    assert [j for j in await _rules_jobs(o, current_kind) if j["status"] == "queued"] == []

    with patch("src.orchestrator.layout_step.ENGINE_RULES_VERSION", next_version):
        await _converge(o)
    fresh = await _rules_jobs(o, next_kind)
    assert [(j["project_id"], j["variant"]) for j in fresh] == [("p00", "active")]


async def test_convergence_walks_active_projects_only(orchestrator_factory):
    """A paused or archived project must not spend a CPU-bound full layout."""
    o = await orchestrator_factory()
    await _laid_out_install(o, n=2)
    await o.db.update_project("p00", status=ProjectStatus.PAUSED.value)

    await _converge(o)

    assert [(j["project_id"], j["variant"]) for j in await _rules_jobs(o)] == [("p01", "active")]
