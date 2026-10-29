"""Leased, restart-safe outbox consumer. Notifications carry no evidence."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from src.database.queries.wait_queries import verify_owner
from src.database.tables import sessions, wait_listener_cursors, wait_records, wait_source_events
from src.waits.conditions import Condition, Scope, WaitError
from src.waits.admission import admission_pass
from src.waits.graph import lock_graph
from src.waits.resource_sources import (
    RESOURCE_INVALIDATIONS,
    _invalidation,
    reobserve_resource_candidates,
    resource_invalidation_candidates,
)

logger = logging.getLogger(__name__)

_OBSERVATION_ONLY_PREFIXES = (
    "ci_check:", "resource_invalidation:pool:", "resource_invalidation:config:",
    "resource_invalidation:workspace:",
)


class _DrainTiming:
    """Measure fenced work through transaction completion, including failed attempts."""

    def __init__(self, watermark):
        self.watermark = watermark
        self.event = {}
        self.started = None
        self.seconds = dict.fromkeys((
            "lease", "prefix_retirement", "admission_recheck", "dispatch", "outcome_writes",
        ), 0.0)

    @asynccontextmanager
    async def transaction(self, engine):
        try:
            async with engine.begin() as conn:
                yield conn
        finally:
            self.report()

    @contextmanager
    def step(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            self.seconds[name] += time.monotonic() - started

    def report(self):
        if self.started is None:
            return
        held = time.monotonic() - self.started
        if held >= 1:
            logger.warning(
                "wait.source_lock_slow source_seq=%s source_key=%s watermark=%s held_seconds=%.3f "
                "lease_seconds=%.3f prefix_retirement_seconds=%.3f "
                "admission_recheck_seconds=%.3f dispatch_seconds=%.3f "
                "outcome_writes_seconds=%.3f transaction_end_seconds=%.3f",
                self.event.get("seq"), self.event.get("source_key"), self.watermark,
                held, *self.seconds.values(),
                max(0, held - sum(self.seconds.values())),
            )


class ListenerUnavailable(RuntimeError):
    """A downstream lifecycle owner is absent; leave the cursor unchanged."""


class LeaseLost(RuntimeError):
    pass


class WaitSourceListener:
    lease_seconds = 30
    renewal_seconds = 10
    safety_seconds = 60
    batch_size = 200
    retry_batch_size = 20
    event_budget_seconds = 5
    drain_budget_seconds = 5
    retry_seconds = 30
    graph_wait_seconds = 0.25

    def __init__(self, service, *, clock=time.time, owner=None, connect=None):
        self.service = service
        self.db = service.orch.db
        self.clock = clock
        self.owner = owner or str(uuid.uuid4())
        self.connect = connect or self._connect
        self.changed = asyncio.Event()
        self.deadlines_changed = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._graph_busy = False
        # The outbox's undispatched stamp is durable retry ownership. This
        # process-local cadence only avoids hammering a temporarily unavailable
        # provider; restart makes retained events immediately eligible again.
        self._retry_due = {}
        self._ci_progress_due = {}
        # Keep fairness across budget-limited passes, even if each pass fits
        # only one attempt. Restart gives an aged retry the first turn.
        self._retry_turn = True

    async def _connect(self):
        import asyncpg

        # A dedicated connection, never a checked-out SQLAlchemy pool member.
        url = self.db._engine.url
        kwargs = {
            key: value for key, value in {
                "user": url.username, "password": url.password, "host": url.host,
                "port": url.port, "database": url.database,
            }.items() if value is not None
        }
        # Query hosts also support Unix socket directories when URL host is absent.
        kwargs.update(url.query)
        return await asyncpg.connect(**kwargs)

    async def lease(self, *, release=False):
        now = self.clock()
        async with self.db._engine.begin() as conn:
            if release:
                await conn.execute(update(wait_listener_cursors).where(
                    wait_listener_cursors.c.listener_key == "source",
                    wait_listener_cursors.c.lease_owner == self.owner,
                ).values(lease_owner=None, lease_expires_at=None, updated_at=now))
                return True
            stmt = insert(wait_listener_cursors).values(
                listener_key="source", last_seq=0, lease_owner=self.owner,
                lease_expires_at=now + self.lease_seconds, updated_at=now,
            ).on_conflict_do_update(
                index_elements=[wait_listener_cursors.c.listener_key],
                set_={"lease_owner": self.owner, "lease_expires_at": now + self.lease_seconds,
                      "updated_at": now},
                where=or_(wait_listener_cursors.c.lease_owner.is_(None),
                          wait_listener_cursors.c.lease_expires_at <= now,
                          wait_listener_cursors.c.lease_owner == self.owner),
            ).returning(wait_listener_cursors.c.listener_key)
            return (await conn.scalar(stmt)) is not None

    async def locked_lease(self, conn):
        row = (await conn.execute(select(wait_listener_cursors).where(
            wait_listener_cursors.c.listener_key == "source",
        ).with_for_update())).mappings().first()
        if (not row or row["lease_owner"] != self.owner
                or row["lease_expires_at"] <= self.clock()):
            raise LeaseLost("wait source lease lost")
        return row

    async def verify_resource(self, conn, row):
        """Recheck current ownership, then the canonical typed admission predicate."""
        session = None
        if row["session_id"]:
            session = (await conn.execute(select(sessions).where(
                sessions.c.id == row["session_id"],
            ))).mappings().first()
            if not session:
                return None
        scope = Scope(
            row["project_id"], row["owner_kind"], row["owner_id"], row["session_id"],
            session["instance_token"] if session else None, row["claim_epoch"],
            principal="agent" if session else "service", elevated=row["owner_kind"] == "session",
        )
        try:
            scope = await verify_owner(conn, scope)
            return await self.service.registry.verify(conn, Condition.from_record(row["condition"]),
                                                       replace(scope, principal="service"))
        except WaitError:
            return None

    async def _dispatch(self, conn, event, *, project_id=None, observations=None):
        if event["source_key"].startswith("task:"):
            from src.waits.recovery import TERMINAL_TASK_STATUSES, retire_terminal_task_waits

            if event["payload"].get("status") in TERMINAL_TASK_STATUSES:
                if project_id is not None:
                    from src.database.tables import tasks
                    owner_project = await conn.scalar(select(tasks.c.project_id).where(
                        tasks.c.id == event["source_key"].removeprefix("task:")))
                    if owner_project != project_id:
                        raise WaitError("wait.out_of_scope", "source belongs to another project")
                await retire_terminal_task_waits(
                    conn, event["source_key"].removeprefix("task:"), now=self.clock(),
                )
        dispatcher = self.service.dispatcher
        lifecycle = getattr(self.service, "lifecycle", None)
        if lifecycle is not None:
            await lifecycle.resume.recovery_event(conn, event, project_id=project_id)
        if event["source_key"].startswith("resource_invalidation:"):
            after = ""
            while candidates := await resource_invalidation_candidates(
                conn, event, after_id=after, limit=self.batch_size,
            ):
                if dispatcher is None:
                    raise ListenerUnavailable("WakeDispatcher is required")
                next_after = candidates[-1]["id"]
                candidates = [row for row in candidates
                              if project_id is None or row["project_id"] == project_id]
                for row, evidence in await reobserve_resource_candidates(
                    conn, candidates, verify=self.verify_resource,
                ):
                    # Dispatcher repeats owner/source locks and fresh observation;
                    # bridge evidence is a candidate, never a committed outcome.
                    await self._commit(dispatcher, conn, row, event, evidence=evidence)
                after = next_after
            return 0
        rows = (await conn.execute(select(wait_records).where(
            wait_records.c.outcome == "pending",
            wait_records.c.verification_state == "verified",
            or_(project_id is None, wait_records.c.project_id == project_id),
            wait_records.c.source_key == event["source_key"],
            wait_records.c.source_generation == event["source_generation"],
            wait_records.c.source_watermark_seq < event["seq"],
        ).order_by(wait_records.c.id))).mappings().all()
        if rows and dispatcher is None:
            raise ListenerUnavailable("WakeDispatcher is required")
        retry_after = 0
        for row in rows:
            kwargs = {"observations": observations} if observations is not None else {}
            result = await self._commit(dispatcher, conn, dict(row), event, **kwargs)
            retry_after = max(retry_after, result.get("retry_after", 0))
        return retry_after

    @staticmethod
    async def _commit(dispatcher, conn, row, event, **kwargs):
        result = await dispatcher.commit(conn, row, event, **kwargs)
        if not isinstance(result, dict) or result.get("success") is not True:
            if isinstance(result, dict) and result.get("error_code") == "wait.graph_busy":
                raise WaitError("wait.graph_busy", result.get("error", "graph busy"))
            raise ListenerUnavailable(f"wait dispatch refused: {result}")
        return result

    @staticmethod
    def _hint_subscription():
        # Broad resource matching is conservative: dispatch rechecks canonical
        # scope. Provider hints still require lifecycle recovery without waits.
        return select(wait_records.c.id).where(
            wait_records.c.outcome == "pending",
            wait_records.c.verification_state == "verified",
            wait_records.c.source_watermark_seq < wait_source_events.c.seq,
            or_(
                (wait_records.c.source_key == wait_source_events.c.source_key)
                & (wait_records.c.source_generation == wait_source_events.c.source_generation),
                wait_source_events.c.source_key.startswith("resource_invalidation:", autoescape=True)
                & (wait_records.c.condition_kind == "resource"),
            ),
        ).exists()

    async def _unobserved_prefix(self, conn, rows):
        """Plan unlocked, then recheck only the planned event window under the fence.

        Group subscriptions once instead of correlating a subscriber scan with
        each event. Minimum watermarks preserve the exact EXISTS semantics,
        including the conservative cross-project resource subscription check.
        Task retirement and provider recovery always require dispatch.
        """
        from src.waits.recovery import TERMINAL_TASK_STATUSES

        window = []
        for row in rows[:self.batch_size]:
            key = row["source_key"]
            if key.startswith("resource_invalidation:provider:"):
                break
            if key.startswith("task:"):
                if row["payload"].get("status") in TERMINAL_TASK_STATUSES:
                    break
            elif not key.startswith((
                "ci_check:", "ci_run:", "message:", "dependency:", "batch:",
                "job:", "playbook_condition:", "resource_invalidation:",
            )):
                break
            window.append(row)
        if not window:
            return []
        keys = sorted({(row["source_key"], row["source_generation"]) for row in window
                       if not (row["source_key"].startswith((
                           "ci_check:", "resource_invalidation:pool:",
                       )) and row["dispatched_at"] is not None)})
        watermarks = {}
        if keys:
            subscriptions = await conn.execute(select(
                wait_records.c.source_key, wait_records.c.source_generation,
                func.min(wait_records.c.source_watermark_seq),
            ).where(
                wait_records.c.outcome == "pending",
                wait_records.c.verification_state == "verified",
                tuple_(wait_records.c.source_key, wait_records.c.source_generation).in_(keys),
            ).group_by(wait_records.c.source_key, wait_records.c.source_generation))
            watermarks = {(key, generation): seq for key, generation, seq in subscriptions}
        resource_watermark = None
        if any(row["source_key"].startswith("resource_invalidation:") for row in window):
            resource_watermark = await conn.scalar(select(
                func.min(wait_records.c.source_watermark_seq),
            ).where(
                wait_records.c.outcome == "pending",
                wait_records.c.verification_state == "verified",
                wait_records.c.condition_kind == "resource",
            ))
        prefix = []
        for row in window:
            if row["source_key"].startswith((
                "ci_check:", "resource_invalidation:pool:",
            )) and row["dispatched_at"] is not None:
                prefix.append(row["seq"])
                continue
            subscribed = watermarks.get((row["source_key"], row["source_generation"]))
            if subscribed is not None and subscribed < row["seq"]:
                break
            if (row["source_key"].startswith("resource_invalidation:")
                    and resource_watermark is not None and resource_watermark < row["seq"]):
                break
            prefix.append(row["seq"])
        return prefix

    async def _unobserved_events(self, conn, watermark, *, source=None):
        """Find observation-only events that no verified subscriber needs.

        Call under GRAPH_LOCK before stamping: registration may have subscribed
        while the listener waited for the fence. Check all projects even for a
        project-scoped supervisor recovery.
        """
        query = select(wait_source_events.c.seq).where(
            wait_source_events.c.seq <= watermark,
            wait_source_events.c.dispatched_at.is_(None),
            or_(*(wait_source_events.c.source_key.startswith(prefix, autoescape=True)
                  for prefix in _OBSERVATION_ONLY_PREFIXES)),
            ~self._hint_subscription(),
        )
        if source is not None:
            query = query.where(
                wait_source_events.c.source_key == source["source_key"],
                wait_source_events.c.source_generation == source["source_generation"],
            )
        return list(await conn.scalars(query.order_by(
            wait_source_events.c.seq,
        ).limit(self.batch_size)))

    async def _retire_unused_retries(self, watermark):
        """Bounded cleanup behind the cursor, independent of observation backoff."""
        async with self.db._engine.begin() as conn:
            position = await conn.scalar(select(wait_listener_cursors.c.last_seq).where(
                wait_listener_cursors.c.listener_key == "source",
            ))
            watermark = min(position or 0, watermark)
            if not await self._unobserved_events(conn, watermark):
                return 0
            try:
                await lock_graph(conn, queued=True, timeout=self.graph_wait_seconds)
            except WaitError as exc:
                if exc.code != "wait.graph_busy":
                    raise
                self._graph_busy = True
                return 0
            await self.locked_lease(conn)
            processed = await self._unobserved_events(conn, watermark)
            if processed:
                await conn.execute(update(wait_source_events).where(
                    wait_source_events.c.seq.in_(processed),
                ).values(dispatched_at=self.clock()))
            await self.locked_lease(conn)
        for seq in processed:
            self._retry_due.pop(seq, None)
        if len(processed) == self.batch_size:
            self.changed.set()
        return len(processed)

    @staticmethod
    def _coalesce_key(event):
        if event["source_key"].startswith("ci_check:"):
            return (event["source_key"], event["source_generation"])
        payload = _invalidation(event)
        if payload and payload["producer"] in {"pool", "config", "workspace"}:
            return (payload["producer"], payload.get("project_id"))
        return None

    async def _defer(self, attempt):
        """After rollback, retain the source row and CAS only the forward cursor."""
        if not attempt:
            return False
        async with self.db._engine.begin() as conn:
            cursor = await self.locked_lease(conn)
            if cursor["last_seq"] != attempt["position"]:
                return False
            await conn.execute(update(wait_listener_cursors).where(
                wait_listener_cursors.c.listener_key == "source",
                wait_listener_cursors.c.lease_owner == self.owner,
            ).values(last_seq=max(cursor["last_seq"], attempt["forward_seq"]),
                     updated_at=self.clock()))
        for seq in attempt["seqs"]:
            self._retry_due[seq] = self.clock() + self.retry_seconds
        self._retry_turn = not attempt["retry"]
        logger.warning("wait.source_dispatch_deferred seq=%s source_key=%s",
                       attempt["event"]["seq"], attempt["event"]["source_key"])
        return True

    async def _dispatched_ci_retry(self, conn, watermark, attempted):
        # Older daemons stamped events dispatched even when fresh observation
        # failed. The stamp is not a per-wait receipt. Start from pending
        # subscriptions and their indexed source histories, not all history.
        latest = select(func.max(wait_source_events.c.seq)).where(
            wait_source_events.c.source_key == wait_records.c.source_key,
            wait_source_events.c.source_generation == wait_records.c.source_generation,
            wait_source_events.c.seq > wait_records.c.source_watermark_seq,
            wait_source_events.c.seq <= watermark,
        ).correlate(wait_records).scalar_subquery()
        recoverable = select(latest).where(
            wait_records.c.outcome == "pending",
            wait_records.c.verification_state == "verified",
            wait_records.c.condition_kind.in_(("ci_check", "ci_run")),
        )
        return (await conn.execute(select(wait_source_events).where(
            wait_source_events.c.seq.in_(recoverable),
            wait_source_events.c.dispatched_at.is_not(None),
            wait_source_events.c.seq.not_in(self._retry_due),
            wait_source_events.c.seq.not_in(attempted),
        ).order_by(wait_source_events.c.seq).limit(1))).mappings().first()

    @admission_pass("service.scheduler.reader")
    async def _drain_with_admission(self, watermark):
        return await self.drain(watermark, admission_ready=True)

    async def drain(self, watermark, *, admission_ready=False):
        """Bound each transaction; retain slow observations for a later retry pass."""
        count = retries = 0
        retry_pending = True
        recovery = {"attempted": set()}
        started = asyncio.get_running_loop().time()
        retirement = asyncio.timeout(self.event_budget_seconds)
        try:
            async with retirement:
                count += await self._retire_unused_retries(watermark)
        except TimeoutError:
            if not retirement.expired():
                raise
            logger.warning("wait.unused_retry_retirement_deferred")
        attempted = bool(count)
        while True:
            if attempted and asyncio.get_running_loop().time() - started >= self.drain_budget_seconds:
                # Refresh the watermark promptly even when many old sources
                # each exhaust their attempt budget. Keep the live loop awake.
                self.changed.set()
                return count
            # An empty retirement scan is housekeeping, not source progress.
            # Even a slow scan must leave room for one forward/retry attempt.
            attempted = True
            attempt = {}
            budget = asyncio.timeout(self.event_budget_seconds)
            try:
                async with budget:
                    result = await self._drain_one(
                        watermark, retry_pending and retries < self.retry_batch_size, attempt, recovery,
                        admission_ready=admission_ready)
            except WaitError as exc:
                if exc.code != "wait.graph_busy":
                    raise
                # The attempt transaction has rolled back. Preserve the cursor
                # and let the live loop retry without cancelling sibling tasks.
                self._graph_busy = True
                return count
            except (DBAPIError, TimeoutError) as exc:
                if isinstance(exc, DBAPIError):
                    sqlstate = getattr(exc.orig, "sqlstate", None)
                    if sqlstate not in {"40P01", "40001"}:
                        raise
                    event = attempt.get("event", {})
                    logger.warning("wait.source_transaction_conflict seq=%s source_key=%s "
                                   "sqlstate=%s", event.get("seq"), event.get("source_key"),
                                   sqlstate)
                elif not budget.expired():
                    raise
                # _drain_one has exited its transaction: a PostgreSQL conflict
                # aborts the whole attempt just as the work budget does. Retain
                # its source for fresh observation while later events proceed.
                if not await self._defer(attempt):
                    return count
                retries += bool(attempt["retry"])
                if not await self.service.lease():
                    raise LeaseLost("wait source lease lost after deferred dispatch")
                continue
            if result is None:
                return count
            if result.get("raced"):
                continue
            if result.get("needs_preparation"):
                self.changed.set()
                return count
            if result.get("needs_admission"):
                # _drain_one has released its transaction and graph fence.
                # Fleet preparation is needed only for a real resource observer,
                # never an idle tick or an unsubscribed invalidation backlog.
                return count + await self._drain_with_admission(watermark)
            retry_pending = result["retry_pending"]
            processed, retry_after = result["processed"], result["retry_after"]
            recovery["attempted"].update(processed)
            if retry_after:
                for seq in processed:
                    self._retry_due[seq] = self.clock() + retry_after
                logger.warning("wait.source_observation_deferred seq=%s source_key=%s "
                               "retry_after=%s age_seconds=%.1f",
                               processed[-1], result["event"]["source_key"], retry_after,
                               max(0, self.clock() - result["event"]["occurred_at"]))
            if result["retry"]:
                retries += 1
                if result["event"]["dispatched_at"] is not None and not retry_after:
                    # An unsatisfied observation is legitimate. Bound repeated
                    # recovery visits without requiring a new source revision.
                    self._retry_due[processed[-1]] = self.clock() + self.safety_seconds
                if not retry_after:
                    logger.info("wait.source_observation_retried seq=%s", processed[-1])
            count += len(processed)
            if not await self.service.lease():
                raise LeaseLost("wait source lease lost between batches")

    async def _drain_one(self, watermark, retry_pending, attempt, recovery, *, admission_ready=False):
        timing = _DrainTiming(watermark)
        async with timing.transaction(self.db._engine) as conn:
            from src.waits.loops import prepare_progress

            position = await conn.scalar(select(wait_listener_cursors.c.last_seq).where(
                wait_listener_cursors.c.listener_key == "source",
            ))
            self._retry_due = {seq: due for seq, due in self._retry_due.items()
                               if due > self.clock()}
            retry_query = select(wait_source_events).where(
                wait_source_events.c.seq <= (position or 0),
                wait_source_events.c.seq <= watermark,
                wait_source_events.c.dispatched_at.is_(None),
                wait_source_events.c.seq.not_in(self._retry_due),
                wait_source_events.c.seq.not_in(recovery["attempted"]),
            ).order_by(wait_source_events.c.seq).limit(1)
            retry = None
            if retry_pending and self._retry_turn:
                # Forward-first alone starves retained observations when every
                # pass exhausts its budget before catching up with new events.
                retry = (await conn.execute(retry_query.where(
                    wait_source_events.c.committed_at <= self.clock() - self.safety_seconds,
                ))).mappings().first()
            candidate = None
            coalesced = []
            rows = []
            if retry is None:
                rows = (await conn.execute(select(wait_source_events).where(
                    wait_source_events.c.seq > (position or 0),
                    wait_source_events.c.seq <= watermark,
                ).order_by(wait_source_events.c.seq).limit(self.batch_size))).mappings().all()
            prefix = await self._unobserved_prefix(conn, rows)
            prefix_rows = rows[:len(prefix)]
            if rows:
                candidate = rows[0]
                key = self._coalesce_key(candidate)
                if key is not None:
                    for row in rows:
                        if self._coalesce_key(row) != key:
                            break
                        coalesced.append(row["seq"])
                        candidate = row
            if candidate is None and retry is None and retry_pending:
                retry = (await conn.execute(retry_query)).mappings().first()
                if retry is None:
                    # Keep the undispatched partial index usable; combining
                    # these paths with OR scans the full outbox prefix.
                    retry = await self._dispatched_ci_retry(
                        conn, watermark, recovery["attempted"],
                    )
                # New failures in this pass already receive a backoff;
                # do not rescan the retry index for every forward event.
                retry_pending = retry is not None
            candidate = candidate or retry
            forward_seq = candidate["seq"] if candidate else position
            observations = None
            progress_delay = 0
            if (not prefix and candidate and candidate["source_key"].startswith("ci_check:")
                    and (retry or candidate["dispatched_at"] is None)):
                # CI hints have no per-event lifecycle effect. Reobserve the newest
                # committed hint even across unrelated events and retained retries.
                same_source = (wait_source_events.c.source_key == candidate["source_key"]) & (
                    wait_source_events.c.source_generation == candidate["source_generation"]
                ) & (wait_source_events.c.seq <= watermark)
                latest = (await conn.execute(select(wait_source_events).where(
                    same_source, wait_source_events.c.seq >= candidate["seq"],
                ).order_by(wait_source_events.c.seq.desc()).limit(1))).mappings().one()
                candidate = latest
                coalesced = list(await conn.scalars(select(wait_source_events.c.seq).where(
                    same_source, wait_source_events.c.dispatched_at.is_(None),
                ).order_by(wait_source_events.c.seq)))
                if candidate["seq"] not in coalesced:
                    coalesced.append(candidate["seq"])
            pool = _invalidation(candidate) if candidate and not prefix else None
            if pool and pool["producer"] == "pool":
                # Pool hints have no per-event lifecycle effects. Retained
                # retries and interleaved forward hints can share one fresh
                # observation, without moving the cursor past unrelated work.
                same_pool = (
                    (wait_source_events.c.source_key == candidate["source_key"])
                    & (wait_source_events.c.source_generation == candidate["source_generation"])
                    & (wait_source_events.c.seq <= watermark)
                    & (wait_source_events.c.payload["producer"].astext == "pool")
                    & (wait_source_events.c.payload["identity"].astext == pool["identity"])
                    & wait_source_events.c.payload["project_id"].astext.is_not_distinct_from(
                        pool.get("project_id"))
                )
                latest = (await conn.execute(select(wait_source_events).where(
                    same_pool, wait_source_events.c.seq >= candidate["seq"],
                ).order_by(wait_source_events.c.seq.desc()).limit(1))).mappings().one()
                candidate = latest
                superseded = await conn.scalars(select(wait_source_events.c.seq).where(
                    same_pool, wait_source_events.c.dispatched_at.is_(None),
                ))
                coalesced = sorted(set(coalesced) | set(superseded) | {candidate["seq"]})
            if candidate and not prefix:
                attempt.update(position=position, event=dict(candidate), retry=bool(retry),
                               forward_seq=forward_seq, seqs=coalesced or [candidate["seq"]])
                if candidate["source_key"].startswith("resource_invalidation:"):
                    payload = _invalidation(candidate) or {}
                    matching = (wait_records.c.condition_kind == "resource") & (
                        wait_records.c.condition["blocker"].astext.in_(
                            RESOURCE_INVALIDATIONS.get(payload.get("producer"), ()),
                        )
                    )
                    if payload.get("project_id") is not None:
                        matching &= wait_records.c.project_id == payload["project_id"]
                else:
                    matching = (wait_records.c.source_key == candidate["source_key"]) & (
                        wait_records.c.source_generation == candidate["source_generation"]
                    )
                owners = (await conn.execute(select(wait_records).where(
                    wait_records.c.outcome == "pending",
                    wait_records.c.verification_state == "verified", matching,
                    wait_records.c.source_watermark_seq < candidate["seq"],
                ).order_by(wait_records.c.id))).mappings().all()
                payload = candidate["payload"]
                if candidate["source_key"].startswith("ci_check:") and (
                    payload.get("conclusion") in {"pending", "in_progress", "queued"}
                    or payload.get("state") == "unsatisfied"
                ):
                    progress_delay = max(0, self._ci_progress_due.get(
                        self._coalesce_key(candidate), 0) - self.clock())
                if candidate["source_key"].startswith("ci_check:"):
                    # A lookahead may leave the newest retained hint ahead of
                    # the cursor. Crossing it later must honor the same backoff.
                    progress_delay = max(progress_delay, self._retry_due.get(
                        candidate["seq"], 0) - self.clock())
                if (candidate["dispatched_at"] is None or retry) and not progress_delay:
                    await prepare_progress(conn, owners)
                    prepare = getattr(self.service.dispatcher, "prepare_observations", None)
                    if prepare is not None and candidate["source_key"].startswith((
                        "ci_check:", "ci_run:", "pull_request:",
                    )):
                        observations = await prepare(conn, owners)
            if prefix:
                # A retirement attempt owns only this bounded forward window.
                # It needs no owner progress or remote observation preparation.
                attempt.update(position=position, event=dict(candidate), retry=False,
                               forward_seq=prefix[-1], seqs=prefix)
            elif candidate is None:
                self._graph_busy = False
                return None
            # Scheduler reconciliation can hold the graph fence longer than
            # one renewal interval. Yield without locking the lease row so
            # renewal stays live; the durable cursor remains replayable.
            timing.event = candidate
            try:
                await lock_graph(conn, queued=True, timeout=self.graph_wait_seconds)
            except WaitError as exc:
                if exc.code != "wait.graph_busy":
                    raise
                self._graph_busy = True
                return None
            timing.started = time.monotonic()
            self._graph_busy = False
            with timing.step("lease"):
                cursor = await self.locked_lease(conn)
            # Another drain may have advanced while Git was observed. Do
            # not dispatch a different event with the wrong progress heads.
            if cursor["last_seq"] != position:
                return {"raced": True}
            # Relevant events commit separately; irrelevant prefixes retire
            # in bounded batches. Renewal cadence never defines lock scope.
            async with asyncio.timeout(self.lease_seconds):
                retry_after = 0
                with timing.step("prefix_retirement"):
                    processed = [] if retry else await self._unobserved_prefix(conn, prefix_rows)
                advance_seq = processed[-1] if processed else forward_seq
                if not processed:
                    if candidate is None:
                        return None
                    event = candidate
                    with timing.step("admission_recheck"):
                        if not admission_ready and event["source_key"].startswith((
                            "resource_invalidation:", "resource:",
                        )) and await conn.scalar(select(wait_records.c.id).where(
                            wait_records.c.outcome == "pending",
                            wait_records.c.verification_state == "verified",
                            wait_records.c.condition_kind == "resource",
                            wait_records.c.source_watermark_seq < event["seq"],
                        ).limit(1)):
                            # Recheck under GRAPH_LOCK, including subscriptions that
                            # committed while this attempt waited for the fence.
                            return {"needs_admission": True}
                    if prefix:
                        # A subscriber invalidated the entire retirement plan.
                        # Keep the cursor replayable and prepare its observation
                        # on the next tick, outside the graph fence.
                        return {"needs_preparation": True}
                    kwargs = {"observations": observations} if observations is not None else {}
                    with timing.step("dispatch"):
                        retry_after = progress_delay or await self._dispatch(
                            conn, dict(event), **kwargs) or 0
                    processed = coalesced or [event["seq"]]
                if cursor["lease_expires_at"] <= self.clock():
                    raise LeaseLost("wait source lease expired during dispatch")
                stamped = processed
                if retry_after:
                    # Superseded CI hints need no independent retry. Keep only the
                    # newest event as durable observation ownership.
                    stamped = [seq for seq in processed if seq != candidate["seq"]
                               and candidate["source_key"].startswith("ci_check:")]
                with timing.step("outcome_writes"):
                    if stamped:
                        await conn.execute(update(wait_source_events).where(
                            wait_source_events.c.seq.in_(stamped),
                        ).values(dispatched_at=self.clock()))
                    await conn.execute(update(wait_listener_cursors).where(
                        wait_listener_cursors.c.listener_key == "source",
                        wait_listener_cursors.c.lease_owner == self.owner,
                    ).values(last_seq=max(cursor["last_seq"], advance_seq),
                             updated_at=self.clock()))
        if observations and not retry_after and candidate["source_key"].startswith("ci_check:"):
            if all(getattr(item.observation, "state", None) == "unsatisfied"
                   for item in observations.values()):
                self._ci_progress_due[self._coalesce_key(candidate)] = (
                    self.clock() + self.safety_seconds)
            else:
                self._ci_progress_due.pop(self._coalesce_key(candidate), None)
        self._ci_progress_due = {key: due for key, due in self._ci_progress_due.items()
                                 if due > self.clock()}
        self._retry_turn = not bool(retry)
        return {"processed": processed, "retry_after": retry_after,
                "event": dict(candidate) if candidate else None, "retry": bool(retry),
                "retry_pending": retry_pending}

    async def _redispatch_event(self, conn, args):
        from src.database.tables import projects

        project_id = args["project_id"]
        if not await conn.scalar(select(projects.c.id).where(projects.c.id == project_id)):
            raise WaitError("wait.out_of_scope", "project not found")
        event = (await conn.execute(select(wait_source_events).where(
            wait_source_events.c.seq == args["source_seq"],
        ))).mappings().first()
        if event is None:
            raise WaitError("wait.source_unresolved", "source event not found")
        payload_project = event["payload"].get("project_id")
        if payload_project is not None and payload_project != project_id:
            raise WaitError("wait.out_of_scope", "source belongs to another project")
        return event

    @staticmethod
    def _redispatch_status(event, project_id):
        if event["dispatched_at"] is not None:
            return {"success": True, "result": {
                "source_seq": event["seq"], "project_id": project_id,
                "outcome": "already_dispatched", "dispatched_at": event["dispatched_at"],
                "redispatched": False, "retired_events": 0,
            }}

    @asynccontextmanager
    async def _redispatch_budget(self, args):
        started = time.monotonic()
        budget = asyncio.timeout(self.event_budget_seconds)
        work = {"phase": "listener_lock", "dispatch_attempts": 0, "candidate_waits": 0}
        try:
            async with budget:
                yield work
        except TimeoutError as exc:
            message = ("source re-dispatch exceeded its work budget" if budget.expired()
                       else f"source re-dispatch timed out during {work['phase']}")
            raise WaitError(
                "wait.unavailable", message, source_seq=args["source_seq"],
                budget_seconds=self.event_budget_seconds,
                elapsed_seconds=time.monotonic() - started, **work,
            ) from exc

    async def redispatch(self, args, *, principal, scope, handler):
        """Reobserve one durable source within an explicitly authorized project."""
        from src.database.tables import events
        from src.waits.loops import prepare_progress

        project_id = args["project_id"]
        # A dispatched stamp needs no observation, leadership or graph work.
        # Validate scope first so the fast path cannot reveal foreign sources.
        async with self.db._engine.connect() as conn:
            event = await self._redispatch_event(conn, args)
            if result := self._redispatch_status(event, project_id):
                return result
        async with self._redispatch_budget(args) as work, self._tick_lock:
            async with self.db._engine.begin() as conn:
                work["phase"] = "source_recheck"
                event = await self._redispatch_event(conn, args)
                if result := self._redispatch_status(event, project_id):
                    return result
                work["phase"] = "lease"
                if not await self.service.lease():
                    raise WaitError("wait.unavailable", "listener lease belongs to another instance")
                work["phase"] = "owner_preparation"
                owners = (await conn.execute(select(wait_records).where(
                    wait_records.c.project_id == project_id,
                    wait_records.c.owner_kind == "task", wait_records.c.outcome == "pending",
                    or_(wait_records.c.source_key == event["source_key"],
                        wait_records.c.condition_kind == "resource"),
                ))).mappings().all()
                work["candidate_waits"] = len(owners)
                await prepare_progress(conn, owners)
                observations = None
                prepare = getattr(self.service.dispatcher, "prepare_observations", None)
                if prepare is not None and event["source_key"].startswith((
                    "ci_check:", "ci_run:", "pull_request:",
                )):
                    work["phase"] = "observation_preparation"
                    observations = await prepare(conn, owners)
                work["phase"] = "graph_lock"
                await lock_graph(conn, queued=True, timeout=self.graph_wait_seconds)
                await self.locked_lease(conn)
                kwargs = {"observations": observations} if observations is not None else {}
                work["phase"] = "dispatch"
                work["dispatch_attempts"] += 1
                retry_after = await self._dispatch(
                    conn, dict(event), project_id=project_id, **kwargs) or 0
                # Observation-only sources can retire without subscribers.
                # Other sources retain their forward/retry lifecycle ownership.
                work["phase"] = "retirement"
                retired = []
                if event["source_key"].startswith(_OBSERVATION_ONLY_PREFIXES):
                    retired = await self._unobserved_events(conn, event["seq"], source=event)
                    if retired:
                        await conn.execute(update(wait_source_events).where(
                            wait_source_events.c.seq.in_(retired),
                        ).values(dispatched_at=self.clock()))
                work["phase"] = "audit"
                await conn.execute(insert(events).values(
                    event_type="wait.source_redispatched", project_id=project_id,
                    timestamp=self.clock(), payload=json.dumps({
                        "source_seq": event["seq"], "source_key": event["source_key"],
                        "note": args["note"], "retry_after": retry_after,
                        "retired_events": len(retired),
                        "supervisor_session_id": getattr(principal, "session_id", None),
                    }),
                ))
                await self.locked_lease(conn)
                work["phase"] = "transaction_commit"
            self.changed.set()
            return {"success": True, "result": {
                "source_seq": event["seq"], "project_id": project_id,
                "retry_after": retry_after, "redispatched": True,
                "retired_events": len(retired),
            }}

    async def report_lag(self):
        """Count retained retries as well as unread events, without taking graph locks."""
        from src.database.tables import messages

        now = self.clock()
        policy = self.service.orch.config.wait_policy
        threshold = policy.listener_lag_events
        bucket = int(now // (policy.listener_lag_alert_minutes * 60))
        async with self.db._engine.begin() as conn:
            pending, oldest, committed = (await conn.execute(select(
                func.count(), func.min(wait_source_events.c.seq),
                func.min(wait_source_events.c.committed_at),
            ).where(wait_source_events.c.dispatched_at.is_(None)))).one()
            age = max(0, now - committed) if committed is not None else 0
            if pending <= threshold and age <= 120:
                return
            logger.warning("wait.listener_lag pending_events=%s oldest_seq=%s age_seconds=%.1f",
                           pending, oldest, age)
            projects = (await conn.execute(select(wait_records.c.project_id).where(
                wait_records.c.outcome == "pending",
                wait_records.c.verification_state == "verified",
            ).distinct())).scalars().all()
            for project_id in projects:
                # Diagnostics must not feed the outbox backlog they report.
                # The persisted bucket key also deduplicates across restarts.
                await conn.execute(insert(messages).values(
                    id=f"wait-listener:lag:{bucket}:{project_id}", project_id=project_id,
                    from_kind="system", from_id="waits-listener", to_kind="session",
                    to_id=f"supervisor-{project_id}", body_kind="wait_listener_lag",
                    subject="Wait source listener is falling behind", created_at=now,
                    body=json.dumps({
                        "pending_events": pending, "oldest_seq": oldest, "age_seconds": age,
                        "next_step": "Inspect graph contention and source dispatch failures; "
                                     "aq wait source-redispatch can reobserve one event.",
                    }),
                ).on_conflict_do_nothing(index_elements=[messages.c.id]))

    async def tick(self):
        async with self._tick_lock:
            if not await self.service.processing_enabled() or not await self.service.lease():
                return False
            # Own short READ COMMITTED connection, before graph/owner/source locks.
            watermark = await self.db.wait_source_watermark()
            await self.service.drain(watermark)
            await self.service.timers.rearm()
            self.deadlines_changed.set()
            return True

    def _notification(self, connection, pid, channel, payload):
        if channel == "aq_wait_deadline":
            self.deadlines_changed.set()
        else:
            self.changed.set()

    async def _renew(self):
        while True:
            await asyncio.sleep(self.renewal_seconds)
            if not await self.service.lease():
                raise LeaseLost("wait source lease lost during renewal")

    async def _live(self, connection):
        while True:
            self.changed.clear()
            if connection.is_closed():
                raise ConnectionError("wait listener disconnected")
            await self.tick()
            if connection.is_closed():
                raise ConnectionError("wait listener disconnected")
            try:
                await asyncio.wait_for(self.changed.wait(),
                                       self.graph_wait_seconds if self._graph_busy
                                       else self.safety_seconds)
            except TimeoutError:
                pass

    async def run(self):
        backoff = 1
        failed = False
        while True:
            connection = None
            try:
                connection = await self.connect()
                await connection.add_listener("aq_wait_source", self._notification)
                await connection.add_listener("aq_wait_deadline", self._notification)
                connection.add_termination_listener(lambda _: self.changed.set())
                if not await self.service.processing_enabled() or not await self.service.lease():
                    await connection.close()
                    connection = None
                    await asyncio.sleep(self.renewal_seconds)
                    continue
                # Renewal starts before the first drain, including startup replay.
                failed, backoff = False, 1
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._renew())
                    group.create_task(self._live(connection))
                    group.create_task(self.service.timers.run())
            except asyncio.CancelledError:
                raise
            except Exception:
                if not failed:
                    logger.warning("wait.listener_restarted", exc_info=True)
                    failed = True
            finally:
                if connection is not None:
                    await connection.close()
                await self.service.lease(release=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
