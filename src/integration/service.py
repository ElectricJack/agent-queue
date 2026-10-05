"""Bounded control and remote reconciliation for durable integration work.

Two properties keep one stuck subject from stopping the train:

* **Every source and item is bounded.**  Each source callback, and each item of
  a page the service iterates itself, runs in its own task under a wall-clock
  budget.  A callback past its budget is cancelled and the pass moves on; the
  durable row stays where it was and is retried on a later pass.  A callback
  that refuses cancellation is left to unwind on its own and its source is
  skipped until it has, so two copies of one source never run at once.  (Bare
  ``asyncio.wait_for`` is not enough: it waits for the cancelled coroutine to
  finish, so an uncancellable call would still hold the pass.)
* **Refusals are revisited from durable state.**  The repair-dispatch source
  re-finds every active stage whose writer was never launched on every pass,
  whatever the event that should have dispatched it did, and retries it with
  exponential backoff.  Only the pacing is in memory: a restart retries at
  once, it never forgets the work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

IntegrationHandler = Callable[[dict[str, Any], float], Awaitable[Any]]
DrainHandler = Callable[[float], Awaitable[Any]]
RepairDispatcher = Callable[[dict[str, Any]], Awaitable[Any]]

#: Budget for one source callback (one bounded page of one source).  The
#: slowest sources observed on the operator's install (2026-09-28..10-02) were
#: GitHub PR reviews at 131 s, the outbox at 128 s and branch materialization
#: at 120 s; a hang is anything well past that.
DEFAULT_SOURCE_TIMEOUT_SECONDS = 300.0
#: Budget for one item of a page the service iterates itself.
DEFAULT_ITEM_TIMEOUT_SECONDS = 60.0
#: How long a timed-out callback gets to unwind its cancellation before the
#: pass stops waiting for it.
CANCEL_GRACE_SECONDS = 5.0
#: Repair dispatch pacing: the first sight of a stage dispatches at once, then
#: each further attempt waits twice as long, up to the ceiling.
DISPATCH_RETRY_BASE_SECONDS = 30.0
DISPATCH_RETRY_MAX_SECONDS = 600.0
#: Dispatch outcomes that leave the stage with a launched writer.
DISPATCH_SUCCESS = frozenset({"dispatched", "already_dispatched", "writer_reused"})


class _CallTimedOut(Exception):
    """One bounded callback exceeded its budget and was cancelled."""

    def __init__(self, budget: float) -> None:
        super().__init__(f"exceeded {budget:.0f}s")
        self.budget = budget


@dataclass
class _Retry:
    identity: tuple[Any, ...]
    attempts: int = 0
    due_at: float = 0.0
    outcome: str | None = None


class RetryBackoff:
    """Per-key exponential pacing for work re-found from durable rows.

    A key is due on first sight and whenever its identity changes (a different
    writer, a new state); after each attempt it waits ``base * 2**(n-1)``
    seconds, never more than ``ceiling``.
    """

    def __init__(self, *, base: float, ceiling: float) -> None:
        if base <= 0 or ceiling < base:
            raise ValueError("retry backoff needs 0 < base <= ceiling")
        self._base = base
        self._ceiling = ceiling
        self._entries: dict[Any, _Retry] = {}

    def due(self, key: Any, identity: tuple[Any, ...], now: float) -> bool:
        entry = self._entries.get(key)
        if entry is None or entry.identity != identity:
            self._entries[key] = _Retry(identity)
            return True
        return now >= entry.due_at

    def adopt(self, key: Any, identity: tuple[Any, ...], *, unless: tuple[Any, ...]) -> None:
        """Rename an entry recorded under the placeholder identity ``unless``."""
        entry = self._entries.get(key)
        if entry is not None and entry.identity == unless:
            entry.identity = identity

    def record(self, key: Any, now: float, outcome: str | None) -> _Retry:
        """Count one attempt; return the entry with the previous outcome kept aside."""
        entry = self._entries[key]
        previous = entry.outcome
        entry.attempts += 1
        entry.due_at = now + self.delay(entry.attempts)
        entry.outcome = outcome
        return _Retry(entry.identity, entry.attempts, entry.due_at, previous)

    def delay(self, attempts: int) -> float:
        exponent = min(max(attempts - 1, 0), 32)
        return min(self._base * (2**exponent), self._ceiling)

    def forget_idle(self, *, before: float) -> None:
        """Forget keys not attempted since their retry fell due before ``before``."""
        self._entries = {key: value for key, value in self._entries.items() if value.due_at >= before}

    def retain(self, keys: set[Any]) -> None:
        """Forget keys a complete scan no longer selects."""
        self._entries = {key: value for key, value in self._entries.items() if key in keys}

    def __contains__(self, key: Any) -> bool:
        return key in self._entries


class IntegrationService:
    """Poll durable integration work without becoming a second authority."""

    def __init__(
        self,
        db: Any,
        scheduler: Any,
        repair: Any,
        outbox: Any,
        *,
        candidate_ci_handler: IntegrationHandler | None = None,
        parent_ci_handler: DrainHandler | None = None,
        unresolved_intent_handler: IntegrationHandler | None = None,
        parent_intent_handler: IntegrationHandler | None = None,
        cleanup_handler: IntegrationHandler | None = None,
        drain_handler: DrainHandler | None = None,
        branch_discard_handler: DrainHandler | None = None,
        branch_materialization_handler: DrainHandler | None = None,
        collection_handler: DrainHandler | None = None,
        development_handler: DrainHandler | None = None,
        owner_recovery_handler: DrainHandler | None = None,
        review_handler: DrainHandler | None = None,
        root_pull_request_handler: DrainHandler | None = None,
        pr_cleanup_handler: DrainHandler | None = None,
        aborted_cleanup_handler: DrainHandler | None = None,
        repair_dispatch_handler: DrainHandler | None = None,
        repair_dispatcher: RepairDispatcher | None = None,
        green_promotion_handler: DrainHandler | None = None,
        subject_runtime: Any = None,
        parent_subject_runtime: Any = None,
        development_subject_runtime: Any = None,
        page_size: int = 100,
        interval_seconds: float = 5.0,
        source_timeout_seconds: float = DEFAULT_SOURCE_TIMEOUT_SECONDS,
        item_timeout_seconds: float = DEFAULT_ITEM_TIMEOUT_SECONDS,
        source_timeouts: Mapping[str, float] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if page_size <= 0:
            raise ValueError("integration service page size must be positive")
        if interval_seconds <= 0:
            raise ValueError("integration service interval must be positive")
        budgets = {"source": source_timeout_seconds, "item": item_timeout_seconds}
        budgets.update({f"source {name!r}": value for name, value in (source_timeouts or {}).items()})
        for label, value in budgets.items():
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise ValueError(f"integration service {label} timeout must be positive")
        self._development_handler = development_handler
        self._development_task = None
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._db = db
        self._scheduler = scheduler
        self._repair = repair
        self._outbox = outbox
        self._candidate_ci_handler = candidate_ci_handler
        self._parent_ci_handler = parent_ci_handler
        self._unresolved_intent_handler = unresolved_intent_handler
        self._parent_intent_handler = parent_intent_handler
        self._cleanup_handler = cleanup_handler
        self._drain_handler = drain_handler
        self._branch_discard_handler = branch_discard_handler
        self._branch_materialization_handler = branch_materialization_handler
        self._collection_handler = collection_handler
        self._owner_recovery_handler = owner_recovery_handler
        self._review_handler = review_handler
        self._root_pull_request_handler = root_pull_request_handler
        self._pr_cleanup_handler = pr_cleanup_handler
        self._aborted_cleanup_handler = aborted_cleanup_handler
        self._repair_dispatch_handler = repair_dispatch_handler
        self._repair_dispatcher = repair_dispatcher
        self._green_promotion_handler = green_promotion_handler
        self._subject_runtime = subject_runtime
        self._parent_subject_runtime = parent_subject_runtime
        self._development_subject_runtime = development_subject_runtime
        self._page_size = page_size
        self._interval_seconds = interval_seconds
        self._source_timeout_seconds = float(source_timeout_seconds)
        self._item_timeout_seconds = float(item_timeout_seconds)
        self._source_timeouts = {
            name: float(value) for name, value in (source_timeouts or {}).items()
        }
        self._clock = clock
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: Timed-out callbacks still unwinding their cancellation, per source.
        self._unwinding: dict[str, set[asyncio.Task[Any]]] = {}
        self._skip_reported: set[str] = set()
        self._dispatch_backoff = RetryBackoff(
            base=DISPATCH_RETRY_BASE_SECONDS, ceiling=DISPATCH_RETRY_MAX_SECONDS
        )
        self._cursors: dict[str, tuple[Any, ...] | None] = {
            "schedules": None,
            "orphaned_sweep": None,
            "repair_stages": None,
            "repair_dispatch": None,
            "repair_dispatch_refused": None,
            "candidate_ci": None,
            "intents": None,
            "cleanup": None,
        }

    async def tick(self, now: float, *, background: bool = False) -> None:
        """Run one control page; the live loop owns at most one remote pass."""
        if self._tick_lock.locked():
            return
        await self._tick_lock.acquire()
        try:
            if (
                not self._stop_event.is_set()
                and (self._reconciliation_task is None or self._reconciliation_task.done())
            ):
                self._reconciliation_task = asyncio.create_task(
                    self._reconcile(), name="integration-remote-reconciliation"
                )
            if not background and self._reconciliation_task is not None:
                await self._reconciliation_task
            await self._source("schedule", self._tick_schedules, self._clock())
            if self._green_promotion_handler is not None:
                await self._source(
                    "green promotion continuation", self._green_promotion_handler, self._clock()
                )
            await self._source("integration outbox", self._outbox.dispatch_due, self._clock())
        finally:
            self._tick_lock.release()

    async def _reconcile(self) -> None:
        """One bounded page per remote source, preserving CI/deadline ordering."""
        try:
            if self._subject_runtime is not None:
                await self._source("root subjects", self._subject_runtime.tick, self._clock())
            if self._parent_subject_runtime is not None:
                await self._source("parent subjects", self._parent_subject_runtime.tick, self._clock())
            if self._development_subject_runtime is not None:
                await self._source(
                    "development subjects", self._development_subject_runtime.tick, self._clock()
                )
            accepted = getattr(self._repair, "reconcile_accepted_delegates", None)
            if callable(accepted):
                await self._source("accepted repair delegates", accepted, self._clock())
            retire = getattr(self._repair, "retire_terminal_delegates", None)
            if callable(retire):
                await self._source("terminal repair delegates", retire, self._clock())
            if self._owner_recovery_handler is not None:
                await self._source("owner recovery", self._owner_recovery_handler, self._clock())
            if self._development_handler is not None and (
                self._development_task is None or self._development_task.done()
            ):
                self._development_task = asyncio.create_task(
                    self._source("development", self._development_handler, self._clock())
                )
            if self._branch_materialization_handler is not None:
                await self._source(
                    "branch materialization", self._branch_materialization_handler, self._clock()
                )
            if self._root_pull_request_handler is not None:
                await self._source(
                    "train root pull requests", self._root_pull_request_handler, self._clock()
                )
            if self._pr_cleanup_handler is not None:
                await self._source("delivered PR cleanup", self._pr_cleanup_handler, self._clock())
            if self._review_handler is not None:
                await self._source("GitHub PR reviews", self._review_handler, self._clock())
            if self._collection_handler is not None:
                await self._source("child collection", self._collection_handler, self._clock())
            # A candidate may have completed its exact CI run at the same time
            # its repair-stage clock becomes due.  Observe it before advancing
            # the deadline ladder: a conclusive result is still evidence, while
            # pending, unknown, and failed observations leave the finite clock
            # to the repair-deadline pass below.
            await self._source("candidate CI", self._tick_candidate_ci, self._clock())
            await self._source("repair deadline", self._tick_repair_stages, self._clock())
            if self._repair_dispatcher is not None:
                await self._source("repair dispatch", self._tick_repair_dispatch, self._clock())
            elif self._repair_dispatch_handler is not None:
                await self._source("repair dispatch", self._repair_dispatch_handler, self._clock())
            reconcile = getattr(self._repair, "reconcile_delegate_reservations", None)
            if callable(reconcile):
                await self._source("repair reservations", reconcile, self._clock())
            if self._parent_ci_handler is not None:
                await self._source("parent CI", self._parent_ci_handler, self._clock())
            await self._source("integration intent", self._tick_intents, self._clock())
            if self._cleanup_handler is not None:
                if self._aborted_cleanup_handler is not None:
                    await self._source(
                        "aborted batch cleanup", self._aborted_cleanup_handler, self._clock()
                    )
                await self._source("integration cleanup", self._tick_cleanup, self._clock())
            if self._drain_handler is not None:
                await self._source("integration drain", self._drain_handler, self._clock())
            if self._branch_discard_handler is not None:
                await self._source("branch discard", self._branch_discard_handler, self._clock())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("unexpected integration remote reconciliation failure")

    async def _tick_schedules(self, now: float) -> None:
        rows = await self._page(
            "schedules",
            self._db.due_integration_schedule_page,
            lambda row: (row["next_due_at"], row["project_id"]),
            now=now,
        )
        for row in rows:
            await self._isolated(
                "schedule",
                row,
                self._scheduler.mark_due,
                row["project_id"],
                self._clock(),
                "periodic",
            )

    async def release_orphaned_sweep_requests(self, now: float) -> int:
        """Free the sweep requests a restart orphaned; returns the schedules swept.

        A daemon start owes the frontier one thing here.  ``integration.sweep_due``
        is delivered once and then sealed by a playbook run this process is
        executing; a restart drops that task and nothing re-drives the run, so the
        request would keep coalescing every flush and tick until the unsealed grace
        runs out -- an hour per restart, measured on agent-queue at 59 minutes.
        ``_tick_schedules`` cannot notice it either, because the schedule it is
        named on may not be due for another sweep interval.

        This is one bounded page of every schedule holding a request, driven
        through the same ``mark_due`` the periodic tick uses, so the enabled and
        settling gates and the catch-up rules apply exactly as they do on a tick:
        the orphaned request is freed, and the next due boundary mints its
        successor.  Whatever this page leaves -- a fleet wider than one page, a
        schedule that is not due yet -- self-heals on the next due tick.
        """
        rows = await self._page(
            "orphaned_sweep",
            self._db.outstanding_sweep_schedule_page,
            lambda row: (row["project_id"],),
        )
        for row in rows:
            await self._isolated(
                "orphaned sweep",
                row,
                self._scheduler.mark_due,
                row["project_id"],
                now,
                "periodic",
            )
        if rows:
            logger.info(
                "integration: swept %d schedule(s) holding a sweep request at start",
                len(rows),
            )
        return len(rows)

    async def _tick_repair_stages(self, now: float) -> None:
        rows = await self._page(
            "repair_stages",
            self._db.due_integration_repair_stage_page,
            lambda row: (row["deadline_at"], row["operation_id"], row["stage"]),
            now=now,
        )
        for row in rows:
            await self._isolated(
                "repair deadline",
                row,
                self._repair.expire,
                row["operation_id"],
                int(row["stage"]),
                now=self._clock(),
            )

    async def _tick_repair_dispatch(self, now: float) -> None:
        """Re-dispatch every active stage whose writer was never launched.

        Two durable selections feed one pass: the repair service's own
        continuation rows (continuous policies, reopened collections) and every
        active stage, under any policy, whose last dispatch left no launched
        writer (``busy``, ``stale``, ``configuration_blocked``, an unexpected
        ownership shape).  A lost or failed playbook run therefore cannot leave
        a stage waiting for an event that will not come.  Human-blocked
        operations and supervisor-recovery stages are terminal and are never
        selected, so explicit human decisions stay binding.
        """
        selectors = []
        pending = getattr(self._repair, "pending_dispatches", None)
        if callable(pending):
            selectors.append(("repair_dispatch", pending))
        refused = getattr(self._db, "refused_integration_repair_dispatch_page", None)
        if callable(refused):
            selectors.append(("repair_dispatch_refused", refused))
        rows: dict[tuple[str, int], dict[str, Any]] = {}
        complete = True
        for cursor, selector in selectors:
            page, from_start = await self._page_scan(
                cursor, selector, lambda row: (row["operation_id"], int(row["ordinal"]))
            )
            complete = complete and from_start and len(page) < self._page_size
            for row in page:
                rows.setdefault((row["operation_id"], int(row["ordinal"])), {}).update(row)
        if complete:
            self._dispatch_backoff.retain(set(rows))
        for key, row in rows.items():
            observed_at = self._clock()
            # A refused dispatch may itself link the stage's new delegate, so a
            # stage gaining its first delegate keeps its pacing; a different
            # delegate (a refiled writer) starts over.
            identity = (row.get("repair_task_id"),)
            self._dispatch_backoff.adopt(key, identity, unless=(None,))
            if not self._dispatch_backoff.due(key, identity, observed_at):
                continue
            result = await self._isolated(
                "repair dispatch", row, self._repair_dispatcher, row
            )
            outcome = (result.get("outcome") if isinstance(result, dict) else None) or "error"
            retry = self._dispatch_backoff.record(key, self._clock(), outcome)
            if retry.outcome == outcome:
                continue  # Report each stage's dispatch state once per change.
            if outcome in DISPATCH_SUCCESS:
                logger.info(
                    "integration repair dispatch operation=%s stage=%s outcome=%s attempt=%d",
                    key[0], key[1], outcome, retry.attempts,
                )
            else:
                logger.warning(
                    "integration repair dispatch operation=%s stage=%s outcome=%s attempt=%d; "
                    "retrying with backoff (next in %.0fs): %s",
                    key[0], key[1], outcome, retry.attempts,
                    self._dispatch_backoff.delay(retry.attempts),
                    result.get("error") if isinstance(result, dict) else None,
                )

    async def _tick_candidate_ci(self, now: float) -> None:
        rows = await self._page(
            "candidate_ci",
            self._db.pending_candidate_ci_page,
            lambda row: (row["updated_at"], row["batch_id"], row["revision"]),
        )
        await self._run_optional("candidate CI", rows, self._candidate_ci_handler, now)

    async def _tick_intents(self, now: float) -> None:
        rows = await self._page(
            "intents",
            self._db.unresolved_integration_intent_page,
            lambda row: (row["updated_at"], row["id"]),
        )
        for row in rows:
            # Root intents promote ``main``; every other kind delivers a child
            # into its parent and has its own reconciler when one is wired.
            handler = self._unresolved_intent_handler
            if row.get("intent_kind") != "root" and self._parent_intent_handler is not None:
                handler = self._parent_intent_handler
            await self._run_optional("integration intent", [row], handler, now)

    async def _tick_cleanup(self, now: float) -> None:
        rows = await self._page(
            "cleanup",
            self._db.pending_integration_cleanup_page,
            lambda row: (row["next_attempt_at"], row["batch_id"], row["domain_key"]),
            now=now,
        )
        await self._run_optional("integration cleanup", rows, self._cleanup_handler, now)

    async def _page(
        self,
        source: str,
        selector: Callable[..., Awaitable[list[dict[str, Any]]]],
        cursor_for: Callable[[dict[str, Any]], tuple[Any, ...]],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        rows, _from_start = await self._page_scan(source, selector, cursor_for, **kwargs)
        return rows

    async def _page_scan(
        self,
        source: str,
        selector: Callable[..., Awaitable[list[dict[str, Any]]]],
        cursor_for: Callable[[dict[str, Any]], tuple[Any, ...]],
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return one page and whether it was read from the start of the keyset."""
        after = self._cursors[source]
        rows = await selector(after=after, limit=self._page_size, **kwargs)
        from_start = after is None
        if not rows and after is not None:
            self._cursors[source] = None
            rows = await selector(after=None, limit=self._page_size, **kwargs)
            from_start = True
        if rows:
            # Fairness state advances on what was scanned, before any handler can
            # decline, fail, or leave the durable row intentionally untouched.
            self._cursors[source] = cursor_for(rows[-1])
        return rows, from_start

    def _source_budget(self, name: str) -> float:
        return self._source_timeouts.get(name, self._source_timeout_seconds)

    def _still_unwinding(self, name: str) -> bool:
        tasks = self._unwinding.get(name)
        if tasks:
            tasks.difference_update({task for task in tasks if task.done()})
        if tasks:
            if name not in self._skip_reported:
                self._skip_reported.add(name)
                logger.warning(
                    "integration source=%s skipped until its timed-out call finishes unwinding",
                    name,
                )
            return True
        self._unwinding.pop(name, None)
        self._skip_reported.discard(name)
        return False

    async def _bounded(
        self, name: str, budget: float, callback: Callable, *args, **kwargs
    ) -> Any:
        """Await ``callback`` in its own task for at most ``budget`` seconds.

        The callback's own result, exception, or cancellation is returned or
        re-raised unchanged.  Past the budget it is cancelled and given
        :data:`CANCEL_GRACE_SECONDS` to unwind; one that is still running is
        tracked under ``name`` so the source is not started again until it
        has finished, and :class:`_CallTimedOut` is raised either way.
        """
        task = asyncio.ensure_future(callback(*args, **kwargs))
        try:
            done, _pending = await asyncio.wait((task,), timeout=budget)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if done:
            return task.result()
        task.cancel()
        try:
            await asyncio.wait((task,), timeout=CANCEL_GRACE_SECONDS)
        finally:
            if task.done():
                self._settle_abandoned(name, task)
            else:
                self._unwinding.setdefault(name, set()).add(task)
                task.add_done_callback(lambda done_task: self._settle_abandoned(name, done_task))
        raise _CallTimedOut(budget)

    def _settle_abandoned(self, name: str, task: asyncio.Task[Any]) -> None:
        """Retrieve a timed-out callback's end so it is reported, not leaked."""
        tasks = self._unwinding.get(name)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._unwinding.pop(name, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "integration %s callback failed while unwinding its timeout: %r", name, exc
            )

    async def _source(self, name: str, callback: Callable, *args, **kwargs) -> Any:
        if self._still_unwinding(name):
            return None
        started = self._clock()
        try:
            return await self._bounded(name, self._source_budget(name), callback, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except _CallTimedOut as exc:
            logger.warning(
                "integration source=%s exceeded its %.0fs budget; cancelled, later sources "
                "continue and its work remains retryable",
                name,
                exc.budget,
            )
            return None
        except Exception:
            logger.exception("integration %s source failed and remains retryable", name)
            return None
        finally:
            elapsed = self._clock() - started
            if elapsed > self._interval_seconds:
                logger.warning("integration slow source=%s elapsed=%.3fs", name, elapsed)

    async def _run_optional(
        self,
        name: str,
        rows: list[dict[str, Any]],
        handler: IntegrationHandler | None,
        now: float,
    ) -> None:
        for row in rows:
            if handler is None:
                logger.warning("%s remains pending: later-phase handler is unavailable", name)
                continue
            await self._isolated(name, row, handler, row, self._clock())

    async def _isolated(self, name: str, row: dict[str, Any], callback: Callable, *args, **kwargs):
        try:
            return await self._bounded(
                name, self._item_timeout_seconds, callback, *args, **kwargs
            )
        except asyncio.CancelledError:
            raise
        except _CallTimedOut as exc:
            logger.warning(
                "integration %s item exceeded its %.0fs budget; cancelled, later items "
                "continue and it remains retryable: %s",
                name,
                exc.budget,
                row,
            )
            return None
        except Exception:
            logger.exception("integration %s item failed and remains retryable: %s", name, row)
            return None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="integration-reconciliation-service"
        )

    async def stop(self) -> None:
        task = self._task
        self._stop_event.set()
        if self._subject_runtime is not None:
            await self._subject_runtime.stop()
        if self._parent_subject_runtime is not None:
            await self._parent_subject_runtime.stop()
        if self._development_subject_runtime is not None:
            await self._development_subject_runtime.stop()
        if self._reconciliation_task is not None:
            self._reconciliation_task.cancel()
            await asyncio.gather(self._reconciliation_task, return_exceptions=True)
            self._reconciliation_task = None
        if self._development_task is not None:
            self._development_task.cancel()
            await asyncio.gather(self._development_task, return_exceptions=True)
            self._development_task = None
        # Timed-out calls that ignored cancellation once get one more request;
        # they are not awaited, so a stuck one cannot hold shutdown either.
        for abandoned in [late for tasks in self._unwinding.values() for late in tasks]:
            abandoned.cancel()
        if task is not None:
            await task
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick(self._clock(), background=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("unexpected integration reconciliation tick failure")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                pass
