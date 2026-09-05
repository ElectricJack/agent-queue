"""Orchestrator cycle step for the task graph layout driver (§4.6)."""

from __future__ import annotations

import asyncio
import logging
import time

from src.task_graph.layout.driver import LayoutDriver

logger = logging.getLogger(__name__)

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

    async def wait_for_layout_step(self) -> None:
        """Await the in-flight background step, if any (tests, shutdown)."""
        bg = getattr(self, "_layout_bg", None)
        if bg is not None and not bg.done():
            await bg

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
            try:
                await driver.full_layout(job["project_id"], job["variant"])
                await self.db.finish_layout_job(job["id"], error=None)
            except Exception as exc:  # noqa: BLE001
                logger.error("layout job %s failed: %s", job["id"], exc)
                await self.db.finish_layout_job(job["id"], error=str(exc))

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
        for project in await self.db.list_projects():
            meta = await self.db.get_layout_meta(project.id, "all")
            if meta and (meta.get("reconciled_at") or 0) < cutoff:
                try:
                    await driver.reconcile(project.id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("layout reconcile for %s failed: %s", project.id, exc)
