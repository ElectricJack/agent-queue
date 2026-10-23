"""Composition root for verified waits and restart-safe continuation draining."""

from __future__ import annotations

import time
import asyncio
import logging

from sqlalchemy import select

from src.waits.conditions import WaitError

from src.waits.listener import WaitSourceListener
from src.waits.registry import build_registry
from src.waits.timers import WaitTimerDriver
from src.waits.dispatcher import WakeDispatcher
from src.waits.graph import recompute_from
from src.waits.source_locking import lock_database_source
from src.waits.decisions import WaitDecisions
from src.waits.human import HumanWaits
from src.wait_runtime.lifecycle import WaitLifecycle
from src.wait_runtime.scheduler import SchedulerWaitService

logger = logging.getLogger(__name__)


BRIDGE_TICK_BUDGET_SECONDS = 10.0


class WaitService:
    def __init__(self, orch, *, clock=time.time, owner=None, connect=None):
        self.orch = orch
        self.scheduler = SchedulerWaitService(orch, clock=clock)
        orch.wait_scheduler_service = self.scheduler
        self.registry = getattr(orch, "wait_registry", None) or build_registry(
            clock=clock, resource_reader=getattr(orch, "wait_resource_reader", None)
            or self.scheduler.reader,
            ci_reader=getattr(orch, "wait_ci_reader", None),
        )
        self.listener = WaitSourceListener(self, clock=clock, owner=owner, connect=connect)
        self.timers = WaitTimerDriver(self, clock=clock)
        self.decisions = WaitDecisions(self, clock=clock)
        self.human = HumanWaits(self, clock=clock)
        self.lifecycle = WaitLifecycle(self, clock=clock)
        from src.wait_runtime.legacy_bridge import LegacyWaitBridge

        self.legacy_bridge = LegacyWaitBridge(self, clock=clock)
        self._bridge_lock = asyncio.Lock()
        self.registry.loop_handler = self.loop_escalation
        self._dispatcher = WakeDispatcher(
            self.registry, clock=clock,
            recompute_priority=getattr(orch, "wait_recompute_priority", recompute_from),
            resolve_escalation=getattr(orch, "wait_resolve_escalation",
                                       self.decisions.resolve_escalation),
            lock_source=getattr(orch, "wait_lock_source", self.lock_source),
        )
        handlers = getattr(orch, "wait_v2_handlers", None)
        if handlers is None:
            handlers = orch.wait_v2_handlers = {}
        for name, callback in (("wait_deadline_fire", self.timers.fire),
                               ("wait_legacy_bridge", self.legacy_bridge.run),
                               ("wait_source_commit", self.source_commit),
                               ("wait_decide", self.decisions.decide),
                               ("wait_lift", self.human.lift),
                               ("wait_register", self.lifecycle.register),
                               ("wait_graph", self.graph),
                               ("wait_explain", self.explain),
                               ("wait_cancel", self.decisions.cancel),
                               ("wait_ack", self.lifecycle.acknowledge),
                               ("wait_resume_dispatch", self.lifecycle.resume.dispatch),
                               ("wait_evict", self.lifecycle.evict),
                               ("wait_sleep_decide", self.lifecycle.sleep_decide)):
            if name in handlers:
                raise ValueError(f"duplicate wait lifecycle owner: {name}")
            handlers[name] = callback
        require_wait_handlers(handlers, self.scheduler.sync)
        deadlines = getattr(orch, "wait_deadline_handlers", None)
        if deadlines is None:
            deadlines = orch.wait_deadline_handlers = {}
        if "escalation" in deadlines:
            raise ValueError("duplicate wait lifecycle owner: escalation deadline")
        deadlines["escalation"] = self.escalation_deadline
        deadlines["reservation"] = self.scheduler.deadline_locked
        deadlines["transition"] = self.lifecycle.hibernate.transition
        deadlines["ceiling"] = self.lifecycle.ceiling

    async def lock_source(self, conn, condition):
        if condition.kind == "resource":
            # AdmissionReader observes current typed constraints under the
            # dispatcher's graph/owner lock. Events never supply its verdict.
            return
        if condition.kind in {"ci_check", "ci_run"}:
            callback = getattr(self.orch, "wait_ci_lock_source", None)
            if callback is not None:
                return await callback(conn, condition)
        await lock_database_source(conn, condition)

    async def graph(self, args, *, principal, scope, handler):
        from src.database.queries.wait_queries import wait_graph

        async with self.orch.db._engine.connect() as conn:
            result = await wait_graph(
                conn, project_id=scope.get('project_id'), fleet=scope.get('fleet', False),
                fleet_authorized=scope.get('fleet', False), costs=args.get('costs', False),
                now=self.lifecycle.clock(),
            )
        return {'success': True, 'graph': result}

    async def explain(self, args, *, principal, scope, handler):
        from src.database.queries.wait_queries import wait_explain
        from src.database.tables import tasks, wait_records

        async with self.orch.db._engine.connect() as conn:
            project_id = (scope or {}).get("project_id")
            if project_id is None:
                # A local operator names no project; the target row carries it.
                table, key = ((wait_records, args["wait_id"]) if args.get("wait_id")
                              else (tasks, args["task_id"]))
                project_id = await conn.scalar(
                    select(table.c.project_id).where(table.c.id == key))
                if project_id is None:
                    raise WaitError("wait.not_found", "wait or task not found")
            result = await wait_explain(
                conn, project_id=project_id, wait_id=args.get("wait_id"),
                task_id=args.get("task_id"), history=args.get("history", False),
                now=self.lifecycle.clock(),
            )
        if args.get("wait_id") and result["waits"]:
            result["explanation"] = result["waits"][0]
        return result

    async def escalation_deadline(self, conn, deadline, wait, *, now, handler):
        callback = (self.scheduler.deadline_locked if wait["origin"] == "scheduler"
                    else self.decisions.raise_escalation)
        return await callback(conn, deadline, wait, now=now, handler=handler)

    @property
    def enabled(self):
        from src.waits.policy import enabled

        return enabled(self.orch.config)

    async def processing_enabled(self):
        """Flags gate new work, never abandon registered waits on rollback."""
        if self.enabled:
            return True
        from sqlalchemy import exists, or_, select
        from src.database.tables import continuations, wait_records

        async with self.orch.db._engine.connect() as conn:
            return bool(await conn.scalar(select(or_(
                exists(select(wait_records.c.id).where(wait_records.c.cleared_at.is_(None))),
                exists(select(continuations.c.id).where(continuations.c.resume_state.not_in(
                    ("acknowledged", "failed", "cancelled")))),
            ))))

    @property
    def dispatcher(self):
        return getattr(self.orch, "wait_dispatcher", self._dispatcher)

    async def command(self, name, args):
        return await self.orch._command_handler.wait_service_command(name, args)

    async def lease(self, *, release=False):
        return await self.orch._command_handler.wait_service_lease(self.listener, release=release)

    async def drain(self, watermark):
        return await self.orch._command_handler.wait_service_drain(self.listener, watermark)

    async def quarantine_deadline(self, deadline, refusal):
        return await self.orch._command_handler.wait_service_quarantine_deadline(
            self.timers, deadline, refusal,
        )

    async def source_commit(self, args, *, principal, scope, handler):
        seq = await self.orch.db.wait_source_commit(**args)
        return {"success": True, "source_seq": seq}

    async def loop_escalation(self, conn, wait, *, now):
        from sqlalchemy import update
        from src.database.tables import wait_deadlines

        await self.decisions.raise_escalation(
            conn, {"generation": 1}, wait, now=now, handler=self.orch._command_handler,
        )
        await conn.execute(update(wait_deadlines).where(
            wait_deadlines.c.wait_id == wait["id"],
            wait_deadlines.c.deadline_type == "escalation", wait_deadlines.c.generation == 1,
        ).values(fired_at=now))

    async def tick(self):
        if await self.processing_enabled():
            if self.enabled:
                await self._bounded_bridge()
            await self.lifecycle.tick()
            return await self.listener.tick()
        return False

    async def run(self):
        # Configuration is hot reloadable. Stay dormant without opening a
        # listener, but retain the service task for a later project canary.
        while not await self.processing_enabled():
            await asyncio.sleep(self.listener.renewal_seconds)
        if self.enabled:
            await self.startup_bridge()
        try:
            async with asyncio.TaskGroup() as group:
                maintenance = group.create_task(self.maintain())
                try:
                    await self.listener.run()
                finally:
                    maintenance.cancel()
        finally:
            await self.lifecycle.stop()

    async def maintain(self):
        """Continue parked work even while source draining or orchestration is slow."""
        while True:
            try:
                if await self.processing_enabled():
                    await self.lifecycle.tick()
            except Exception:
                logger.exception("Wait continuation maintenance failed")
            await asyncio.sleep(self.listener.renewal_seconds)

    async def _bounded_bridge(self):
        """Wait briefly for the bridge; a slow pass finishes beside the scheduler."""
        task = getattr(self, "_bridge_task", None)
        if (task is not None and not task.done()) or self._bridge_lock.locked():
            return
        self._bridge_task = asyncio.create_task(self._run_bridge())
        # A slow pass keeps running; the scheduler continues after the budget.
        await asyncio.wait({self._bridge_task}, timeout=BRIDGE_TICK_BUDGET_SECONDS)

    async def _run_bridge(self):
        try:
            await self.startup_bridge()
        except Exception:
            logger.exception("Wait startup bridge failed")

    async def startup_bridge(self):
        """Durable project receipts permit restart and newly enabled canary recovery."""
        from sqlalchemy import exists, select
        from src.database.tables import projects, wait_bridge_progress
        from src.waits.decision import project_policy

        async with self._bridge_lock:
            # Held migrations used to rerun their complete project scan every
            # scheduler cycle. Retry once a minute; explicit operator bridge
            # commands still run immediately, and completed receipts stay durable.
            now = time.monotonic()
            if now - getattr(self, "_last_bridge_attempt", float("-inf")) < 60:
                return
            self._last_bridge_attempt = now
            async with self.orch.db._engine.connect() as conn:
                pids = list((await conn.execute(select(projects.c.id).where(~exists(
                    select(wait_bridge_progress.c.identity).where(
                        wait_bridge_progress.c.project_id == projects.c.id,
                        wait_bridge_progress.c.phase == 'agent-waits',
                        wait_bridge_progress.c.identity == '*',
                        wait_bridge_progress.c.state == 'complete',
                    ),
                )))).scalars())
            bridge = self.legacy_bridge
            for pid in pids:
                if bridge.retry_at.get(pid, 0) > bridge.clock():
                    continue
                if project_policy(self.orch.config.wait_policy, pid).v2_enabled:
                    result = await self.command('wait_legacy_bridge', {
                        'project_id': pid, 'mode': 'agent-waits', 'apply': True,
                    })
                    report = (result.get('result') or {}).get('projects', {}).get(pid, {})
                    if not result.get('success') or report.get('state') == 'held':
                        bridge.retry_at[pid] = bridge.clock() + bridge.RETRY_SECONDS
                    if not result.get('success'):
                        return


def require_wait_handlers(handlers, scheduler_sync):
    """Fail startup when a verified-wait command would answer "not installed"."""
    from src.commands.contracts.wait_v2 import WAIT_V2_ARGS

    # scheduler_tick installs the same owner lazily; claim it here so the
    # check below sees it.
    handlers.setdefault("scheduler_wait_sync", scheduler_sync)
    # wait_checkpoint falls back to src.waits.checkpoint inside the command.
    expected = (set(WAIT_V2_ARGS) | {"wait_register", "wait_cancel"}) - {"wait_checkpoint"}
    missing = sorted(expected - set(handlers))
    if missing:
        raise RuntimeError(f"verified wait commands have no handler: {', '.join(missing)}")
