"""Leased, restart-safe outbox consumer. Notifications carry no evidence."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import replace

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

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

    async def _dispatch(self, conn, event, *, project_id=None):
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
            result = await self._commit(dispatcher, conn, dict(row), event)
            retry_after = max(retry_after, result.get("retry_after", 0))
        return retry_after

    @staticmethod
    async def _commit(dispatcher, conn, row, event, **kwargs):
        result = await dispatcher.commit(conn, row, event, **kwargs)
        if not isinstance(result, dict) or result.get("success") is not True:
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

    async def _unobserved_prefix(self, conn, position, watermark):
        """Retire only an irrelevant observation prefix, under the graph fence.

        Task retirement and provider recovery still run without subscriptions.
        """
        from src.waits.recovery import TERMINAL_TASK_STATUSES

        rows = (await conn.execute(select(
            wait_source_events.c.seq, wait_source_events.c.source_key, wait_source_events.c.payload,
            self._hint_subscription().label("subscribed"),
        ).where(
            wait_source_events.c.seq > position, wait_source_events.c.seq <= watermark,
        ).order_by(wait_source_events.c.seq).limit(self.batch_size))).mappings()
        prefix = []
        for row in rows:
            key = row["source_key"]
            if row["subscribed"] or key.startswith("resource_invalidation:provider:"):
                break
            if key.startswith("task:"):
                if row["payload"].get("status") in TERMINAL_TASK_STATUSES:
                    break
            elif not key.startswith((
                "ci_check:", "ci_run:", "message:", "dependency:", "batch:",
                "job:", "playbook_condition:", "resource_invalidation:",
            )):
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
            ).values(last_seq=max(cursor["last_seq"], attempt["event"]["seq"]),
                     updated_at=self.clock()))
        for seq in attempt["seqs"]:
            self._retry_due[seq] = self.clock() + self.retry_seconds
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
            wait_source_events.c.dispatched_at.is_not(None),
        ).correlate(wait_records).scalar_subquery()
        recoverable = select(latest).where(
            wait_records.c.outcome == "pending",
            wait_records.c.verification_state == "verified",
            wait_records.c.condition_kind.in_(("ci_check", "ci_run")),
        )
        return (await conn.execute(select(wait_source_events).where(
            wait_source_events.c.seq.in_(recoverable),
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
        recovery = {"position": None, "attempted": set()}
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
            except TimeoutError:
                if not budget.expired():
                    raise
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
            if result.get("needs_admission"):
                # _drain_one has released its transaction and graph fence.
                # Fleet preparation is needed only for a real resource observer,
                # never an idle tick or an unsubscribed invalidation backlog.
                return count + await self._drain_with_admission(watermark)
            retry_pending = result["retry_pending"]
            processed, retry_after = result["processed"], result["retry_after"]
            if retry_after:
                for seq in processed:
                    self._retry_due[seq] = self.clock() + retry_after
                logger.warning("wait.source_observation_deferred seq=%s source_key=%s "
                               "retry_after=%s age_seconds=%.1f",
                               processed[-1], result["event"]["source_key"], retry_after,
                               max(0, self.clock() - result["event"]["occurred_at"]))
            if result["retry"]:
                retries += 1
                recovery["attempted"].update(processed)
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
        async with self.db._engine.begin() as conn:
            from src.waits.loops import prepare_progress

            position = await conn.scalar(select(wait_listener_cursors.c.last_seq).where(
                wait_listener_cursors.c.listener_key == "source",
            ))
            self._retry_due = {seq: due for seq, due in self._retry_due.items()
                               if due > self.clock()}
            if recovery["position"] is None:
                recovery["position"] = position or 0
            # Forward events precede retries so a provider outage cannot
            # hold later messages or task transitions behind its retry budget.
            candidate = None
            coalesced = []
            rows = (await conn.execute(select(wait_source_events).where(
                wait_source_events.c.seq > (position or 0),
                wait_source_events.c.seq <= watermark,
            ).order_by(wait_source_events.c.seq).limit(self.batch_size))).mappings().all()
            if rows:
                candidate = rows[0]
                key = self._coalesce_key(candidate)
                if key is not None:
                    for row in rows:
                        if self._coalesce_key(row) != key:
                            break
                        coalesced.append(row["seq"])
                        candidate = row
            retry = None
            if candidate is None and retry_pending:
                retry = (await conn.execute(select(wait_source_events).where(
                    wait_source_events.c.seq <= (position or 0),
                    wait_source_events.c.seq <= watermark,
                    wait_source_events.c.dispatched_at.is_(None),
                    wait_source_events.c.seq.not_in(self._retry_due),
                    wait_source_events.c.seq.not_in(recovery["attempted"]),
                ).order_by(wait_source_events.c.seq).limit(1))).mappings().first()
                if retry is None:
                    # Keep the undispatched partial index usable; combining
                    # these paths with OR scans the full outbox prefix.
                    retry = await self._dispatched_ci_retry(
                        conn, min(recovery["position"], watermark), recovery["attempted"],
                    )
                # New failures in this pass already receive a backoff;
                # do not rescan the retry index for every forward event.
                retry_pending = retry is not None
            candidate = candidate or retry
            if candidate:
                attempt.update(position=position, event=dict(candidate), retry=bool(retry),
                               seqs=coalesced or [candidate["seq"]])
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
                    wait_records.c.owner_kind == "task", wait_records.c.outcome == "pending",
                    wait_records.c.verification_state == "verified", matching,
                    wait_records.c.source_watermark_seq < candidate["seq"],
                ).order_by(wait_records.c.id))).mappings().all()
                await prepare_progress(conn, owners)
            # Scheduler reconciliation can hold the graph fence longer than
            # one renewal interval. Yield without locking the lease row so
            # renewal stays live; the durable cursor remains replayable.
            try:
                await lock_graph(conn, queued=True, timeout=self.graph_wait_seconds)
            except WaitError as exc:
                if exc.code != "wait.graph_busy":
                    raise
                self._graph_busy = True
                return None
            self._graph_busy = False
            cursor = await self.locked_lease(conn)
            # Another drain may have advanced while Git was observed. Do
            # not dispatch a different event with the wrong progress heads.
            if cursor["last_seq"] != position:
                return {"raced": True}
            # Relevant events commit separately; irrelevant prefixes retire
            # in bounded batches. Renewal cadence never defines lock scope.
            async with asyncio.timeout(self.lease_seconds):
                retry_after = 0
                processed = [] if retry else await self._unobserved_prefix(
                    conn, cursor["last_seq"], watermark)
                if not processed:
                    if candidate is None:
                        return None
                    event = candidate
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
                    retry_after = await self._dispatch(conn, dict(event)) or 0
                    processed = coalesced or [event["seq"]]
                if cursor["lease_expires_at"] <= self.clock():
                    raise LeaseLost("wait source lease expired during dispatch")
                if not retry_after:
                    await conn.execute(update(wait_source_events).where(
                        wait_source_events.c.seq.in_(processed),
                    ).values(dispatched_at=self.clock()))
                await conn.execute(update(wait_listener_cursors).where(
                    wait_listener_cursors.c.listener_key == "source",
                    wait_listener_cursors.c.lease_owner == self.owner,
                ).values(last_seq=max(cursor["last_seq"], processed[-1]),
                         updated_at=self.clock()))
        return {"processed": processed, "retry_after": retry_after,
                "event": dict(candidate) if candidate else None, "retry": bool(retry),
                "retry_pending": retry_pending}

    async def redispatch(self, args, *, principal, scope, handler):
        """Reobserve one durable source within an explicitly authorized project."""
        from src.database.tables import events, projects
        from src.waits.loops import prepare_progress

        project_id = args["project_id"]
        async with asyncio.timeout(self.event_budget_seconds), self._tick_lock:
            if not await self.service.lease():
                raise WaitError("wait.unavailable", "listener lease belongs to another instance")
            async with self.db._engine.begin() as conn:
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
                owners = (await conn.execute(select(wait_records).where(
                    wait_records.c.project_id == project_id,
                    wait_records.c.owner_kind == "task", wait_records.c.outcome == "pending",
                    or_(wait_records.c.source_key == event["source_key"],
                        wait_records.c.condition_kind == "resource"),
                ))).mappings().all()
                await prepare_progress(conn, owners)
                await lock_graph(conn, queued=True, timeout=self.graph_wait_seconds)
                await self.locked_lease(conn)
                retry_after = await self._dispatch(conn, dict(event), project_id=project_id) or 0
                # Observation-only sources can retire without subscribers.
                # Other sources retain their forward/retry lifecycle ownership.
                retired = []
                if event["source_key"].startswith(_OBSERVATION_ONLY_PREFIXES):
                    retired = await self._unobserved_events(conn, event["seq"], source=event)
                    if retired:
                        await conn.execute(update(wait_source_events).where(
                            wait_source_events.c.seq.in_(retired),
                        ).values(dispatched_at=self.clock()))
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
            self.changed.set()
            return {"success": True, "result": {
                "source_seq": event["seq"], "project_id": project_id,
                "retry_after": retry_after, "redispatched": True,
                "retired_events": len(retired),
            }}

    async def report_lag(self):
        """Count retained retries as well as unread events, without taking graph locks."""
        from src.database.tables import messages
        from src.waits.sources import insert_message_with_source_event

        now = self.clock()
        threshold = self.service.orch.config.wait_policy.listener_lag_events
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
                await insert_message_with_source_event(conn, insert(messages).values(
                    id=f"wait-listener:lag:{oldest}:{project_id}", project_id=project_id,
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
