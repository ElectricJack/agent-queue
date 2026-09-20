"""Orchestrator cycle step for the task graph layout driver (§4.6)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from src.models import ProjectStatus
from src.task_graph.layout.constants import ENGINE_RULES_VERSION
from src.task_graph.layout.driver import LayoutDriver

logger = logging.getLogger(__name__)

MAX_RULES_ATTEMPTS = 3
"""Failed engine-rules rebuilds a ``(project, variant)`` gets per version.

Past this the pair is skipped with a WARNING rather than retried forever:
the walk returns at the first pair it enqueues, so a pair that fails
deterministically would otherwise consume every sweep. Bumping
``ENGINE_RULES_VERSION`` starts a new ledger and a fresh budget.
"""

STALE_JOB_BUDGETS = 4
"""Tidy budgets a job may sit ``running`` before it is reaped as orphaned.

Shutdown waits 30 s for the background layout step while a tidy may run for
``tidy_job_budget_seconds`` (60), so a restart can leave a claimed row
``running`` forever — nothing re-claims one. Four budgets is comfortably
past any legitimate run and still well inside one 900 s sweep.
"""

MAX_LAYOUT_PROJECTS_PER_CYCLE = 10
"""Dirty projects folded per cycle; the rest wait for the next one.

The step runs every 5 s and each project costs a snapshot load plus a
publish transaction, so an unbounded fan-out would let one busy moment
stall the whole cycle. Marks are durable, so deferring a project only
delays its layout.
"""


class LayoutStepMixin:
    _layout_failures: dict[str, int]
    _layout_last_reconcile_check: float | None
    _layout_bg: asyncio.Task | None

    def schedule_layout_step(self) -> asyncio.Task | None:
        """Start ``_run_layout_step`` in the background, at most one at a time.

        Layout is a projection of task state; folding a batch of dirty marks
        re-runs the engine over the dirty containers, which measured 0.5 s
        in a 5,600-task hierarchy and 1.4 s for one change in a 5,000-task
        flat project.  Awaiting that inline made every status change stretch
        the 5 s cycle, so the cycle now only *kicks* the step and moves on.
        Marks are durable: a step that is skipped because one is already
        running picks them up on the next cycle.
        """
        bg = getattr(self, "_layout_bg", None)
        if bg is not None and not bg.done():
            return bg
        if bg is not None:
            exc = bg.exception() if not bg.cancelled() else None
            if exc is not None:
                logger.warning("background layout step failed: %s", exc)
        self._layout_bg = asyncio.create_task(self._run_layout_step(), name="layout-step")
        return self._layout_bg

    async def wait_for_layout_step(self, timeout: float | None = None) -> None:
        """Wait for the in-flight background step, if any (tests, shutdown).

        ``asyncio.wait`` rather than ``await bg``: the step's own
        cancellation or failure must not surface in the caller (shutdown
        already logs and moves on), and *timeout* bounds how long a tidy
        job — 60 s budget — can hold up a shutdown.  Marks are durable, so
        a step abandoned at the timeout is simply re-run after restart.
        """
        bg = getattr(self, "_layout_bg", None)
        if bg is None or bg.done():
            return
        done, _pending = await asyncio.wait({bg}, timeout=timeout)
        if not done:
            logger.warning(
                "background layout step still running after %.0fs; not waiting", timeout
            )

    async def _run_layout_step(self) -> None:
        """Run one layout cycle step; never raise into ``run_one_cycle``.

        Layout is a projection of task state and its marks are durable, so
        anything that fails here is retried on the next cycle. The caller in
        ``core.py`` guards the call as well — this belt is here so a direct
        caller (tests, a future scheduler) gets the same guarantee.
        """
        try:
            await self._layout_step_body()
        except Exception as exc:  # noqa: BLE001
            logger.warning("layout step failed: %s", exc)

    async def _layout_step_body(self) -> None:
        """Fold dirty marks into layouts, run tidy jobs, and sweep for drift.

        While ``graph_layout.enabled`` is false nothing consumes
        ``layout_dirty``, so the table would grow without bound; the step
        discards the marks wholesale once per ``reconcile_interval_seconds``
        instead. That is safe because a project with no meta row gets a full
        layout when the feature is turned on — a discarded mark can only
        cost a tidier starting point, never correctness.
        """
        cfg = getattr(self.config, "graph_layout", None)
        if not cfg:
            return
        if not hasattr(self, "_layout_failures"):
            self._layout_failures = {}

        # One step-local clock gates both the disabled-mode trim and the
        # reconcile sweep, so neither runs its per-project polls every cycle.
        now = time.monotonic()
        last_check = getattr(self, "_layout_last_reconcile_check", None)
        sweep_due = last_check is None or now - last_check >= cfg.reconcile_interval_seconds
        if sweep_due:
            self._layout_last_reconcile_check = now

        if not cfg.enabled:
            if sweep_due:
                trimmed = await self.db.trim_layout_dirty()
                if trimmed:
                    logger.debug("layout disabled: discarded %d dirty mark(s)", trimmed)
            return

        driver = LayoutDriver(self.db, tidy_job_seconds=cfg.tidy_job_budget_seconds)

        job = await self.db.next_layout_job()
        if job:
            recorded = False
            try:
                await driver.full_layout(job["project_id"], job["variant"])
                await self.db.finish_layout_job(job["id"], error=None)
                recorded = True
            except Exception as exc:  # noqa: BLE001
                logger.error("layout job %s failed: %s", job["id"], exc)
                await self.db.finish_layout_job(job["id"], error=str(exc))
                recorded = True
            finally:
                if not recorded:
                    # Cancellation (shutdown) is a BaseException, so neither
                    # branch above ran and the row would stay 'running'
                    # forever — nothing re-claims a running job. Record it
                    # here; the sweep's reaper is the backstop for the case
                    # the process dies before this write lands.
                    with contextlib.suppress(Exception):
                        await self.db.finish_layout_job(
                            job["id"], error="cancelled: daemon stopped mid-job"
                        )

        dirty = await self.db.dirty_layout_projects()
        for pid in dirty[:MAX_LAYOUT_PROJECTS_PER_CYCLE]:
            try:
                await driver.process_dirty(pid, min_age_seconds=cfg.incremental_debounce_ms / 1000)
                self._layout_failures.pop(pid, None)
            except Exception as exc:  # noqa: BLE001
                n = self._layout_failures.get(pid, 0) + 1
                self._layout_failures[pid] = n
                logger.warning("layout batch for %s failed (%d): %s", pid, n, exc)
                if n >= 3:
                    logger.error("layout for %s failed 3 times; enqueuing tidy", pid)
                    for variant in ("all", "active"):
                        await self.db.enqueue_layout_job(pid, variant, "tidy")
                    self._layout_failures.pop(pid, None)

        if not sweep_due:
            return
        cutoff = time.time() - cfg.reconcile_interval_seconds
        projects = await self.db.list_projects()
        for project in projects:
            meta = await self.db.get_layout_meta(project.id, "all")
            if meta and (meta.get("reconciled_at") or 0) < cutoff:
                try:
                    await driver.reconcile(project.id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("layout reconcile for %s failed: %s", project.id, exc)
        await self._converge_engine_rules(tidy_job_seconds=cfg.tidy_job_budget_seconds)

    async def _converge_engine_rules(self, *, tidy_job_seconds: float) -> None:
        """Queue one re-tidy for a scope still laid out under older rules.

        ``reconcile`` compares presence, ``container_id`` and ``kind`` — it
        deliberately does not chase *geometry* drift — so a change to the flow
        rules or to ordinal assignment would never reach an install whose
        layout is already published. ``layout_jobs`` rows are never trimmed and
        ``kind`` is free text, so a job of kind ``rules:<version>`` doubles as
        the convergence ledger: its presence means "this pair has been laid out
        under these rules". Bumping ``ENGINE_RULES_VERSION`` is therefore the
        only action a rules change needs — no schema, no operator step
        (reorganisation design §3.3).

        At most **one** pair per sweep, and none at all while a rules job is
        still queued or running anywhere: the layout step claims one job per
        5 s cycle and ``full_layout`` is a CPU-bound thread with a 60 s budget,
        so a fan-out of every project x variant would put a long tail of full
        rebuilds in front of every project's ordinary incremental work.

        The walk takes two passes — never-attempted pairs first, then pairs
        whose attempts all failed — because it is ordered and returns at the
        first pair it enqueues: without the split, one deterministically
        failing pair would be re-picked every sweep and no other project would
        ever converge. A pair that has spent ``MAX_RULES_ATTEMPTS`` is skipped
        with one WARNING per sweep; bumping ``ENGINE_RULES_VERSION`` starts a
        new ledger and therefore a fresh budget.

        Cost, on the ``reconcile_interval_seconds`` sweep only and never on the
        5 s cycle: **five statements flat**, whatever the project count — the
        reaper, the ledger, the published-pair set, the active-project list,
        and at most one enqueue.
        """
        kind = f"rules:{ENGINE_RULES_VERSION}"

        for row in await self.db.reap_stale_layout_jobs(
            started_before=time.time() - STALE_JOB_BUDGETS * tidy_job_seconds,
            error="orphaned: daemon stopped mid-job",
        ):
            logger.warning(
                "reaped layout job %s (%s %s/%s) stuck running since %.0f",
                row["id"],
                row["kind"],
                row["project_id"],
                row["variant"],
                row["started_at"] or 0.0,
            )

        ledger = await self.db.layout_job_ledger(kind)
        if any(entry["in_flight"] for entry in ledger.values()):
            return  # the previous sweep's rebuild has not been consumed yet

        published = await self.db.published_layout_variants()
        projects = await self.db.list_projects(status=ProjectStatus.ACTIVE)

        fresh: list[tuple[str, str]] = []
        retry: list[tuple[str, str]] = []
        for project in sorted(projects, key=lambda p: p.id):
            for variant in ("active", "all"):  # active first: the default canvas
                if (project.id, variant) not in published:
                    # Nothing published; this scope's first full layout will
                    # already use the current rules.
                    continue
                entry = ledger.get((project.id, variant))
                if entry is None:
                    fresh.append((project.id, variant))
                elif entry["settled"]:
                    continue
                elif entry["failed"] >= MAX_RULES_ATTEMPTS:
                    logger.warning(
                        "giving up on the %s rebuild for %s/%s after %d failed attempts; "
                        "last error: %s",
                        kind,
                        project.id,
                        variant,
                        entry["failed"],
                        entry["last_error"],
                    )
                else:
                    retry.append((project.id, variant))

        for project_id, variant in fresh + retry:
            job = await self.db.enqueue_layout_job(project_id, variant, kind)
            if job["kind"] != kind:
                # ``enqueue_layout_job`` de-dupes on (project, variant,
                # status) whatever the kind and handed back somebody else's
                # tidy. Leave the pair stale rather than record it as
                # converged; a later sweep retries it.
                continue
            logger.info(
                "queued %s rebuild for %s/%s (engine rules v%d)",
                kind,
                project_id,
                variant,
                ENGINE_RULES_VERSION,
            )
            return
