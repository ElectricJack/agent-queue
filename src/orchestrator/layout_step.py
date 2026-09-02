"""Orchestrator cycle step for the task graph layout driver (§4.6)."""

from __future__ import annotations

import logging
import time

from src.task_graph.layout.driver import LayoutDriver

logger = logging.getLogger(__name__)


class LayoutStepMixin:
    _layout_failures: dict[str, int]

    async def _run_layout_step(self) -> None:
        cfg = getattr(self.config, "graph_layout", None)
        if not cfg or not cfg.enabled:
            return
        if not hasattr(self, "_layout_failures"):
            self._layout_failures = {}
        driver = LayoutDriver(self.db)

        job = await self.db.next_layout_job()
        if job:
            try:
                await driver.full_layout(job["project_id"], job["variant"])
                await self.db.finish_layout_job(job["id"], error=None)
            except Exception as exc:  # noqa: BLE001
                logger.error("layout job %s failed: %s", job["id"], exc)
                await self.db.finish_layout_job(job["id"], error=str(exc))

        for pid in await self.db.dirty_layout_projects():
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

        cutoff = time.time() - cfg.reconcile_interval_seconds
        for project in await self.db.list_projects():
            meta = await self.db.get_layout_meta(project.id, "all")
            if meta and (meta.get("reconciled_at") or 0) < cutoff:
                try:
                    await driver.reconcile(project.id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("layout reconcile for %s failed: %s", project.id, exc)
