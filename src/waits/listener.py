"""Leased, restart-safe outbox consumer. Notifications carry no evidence."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.queries.wait_queries import REGISTRATION_LOCK, verify_owner
from src.database.tables import sessions, wait_listener_cursors, wait_records, wait_source_events
from src.waits.conditions import Condition, Scope, WaitError
from src.waits.admission import admission_pass
from src.waits.graph import track_graph_lock
from src.waits.resource_sources import (
    RESOURCE_INVALIDATIONS,
    _invalidation,
    reobserve_resource_candidates,
    resource_invalidation_candidates,
)

logger = logging.getLogger(__name__)


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

    async def _dispatch(self, conn, event):
        if event["source_key"].startswith("task:"):
            from src.waits.recovery import TERMINAL_TASK_STATUSES, retire_terminal_task_waits

            if event["payload"].get("status") in TERMINAL_TASK_STATUSES:
                await retire_terminal_task_waits(
                    conn, event["source_key"].removeprefix("task:"), now=self.clock(),
                )
        dispatcher = self.service.dispatcher
        lifecycle = getattr(self.service, "lifecycle", None)
        if lifecycle is not None:
            await lifecycle.resume.recovery_event(conn, event)
        if event["source_key"].startswith("resource_invalidation:"):
            after = ""
            while candidates := await resource_invalidation_candidates(
                conn, event, after_id=after, limit=self.batch_size,
            ):
                if dispatcher is None:
                    raise ListenerUnavailable("WakeDispatcher is required")
                for row, evidence in await reobserve_resource_candidates(
                    conn, candidates, verify=self.verify_resource,
                ):
                    # Dispatcher repeats owner/source locks and fresh observation;
                    # bridge evidence is a candidate, never a committed outcome.
                    await self._commit(dispatcher, conn, row, event, evidence=evidence)
                after = candidates[-1]["id"]
            return 0
        rows = (await conn.execute(select(wait_records).where(
            wait_records.c.outcome == "pending",
            wait_records.c.verification_state == "verified",
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

    async def _unobserved_prefix(self, conn, position, watermark):
        """Retire only a contiguous irrelevant prefix, under the graph fence.

        CI checks have no consumer side effects without a subscription. Pool
        snapshots only invalidate resource waits. In particular, task retirement
        and provider recovery must still run even when no wait matches directly.
        Broad resource matching is conservative: dispatch rechecks canonical scope.
        """
        subscribed = select(wait_records.c.id).where(
            wait_records.c.outcome == "pending",
            wait_records.c.verification_state == "verified",
            wait_records.c.source_watermark_seq < wait_source_events.c.seq,
            or_(
                (wait_records.c.source_key == wait_source_events.c.source_key)
                & (wait_records.c.source_generation == wait_source_events.c.source_generation),
                wait_source_events.c.source_key.startswith("resource_invalidation:pool:")
                & (wait_records.c.condition_kind == "resource"),
            ),
        ).exists()
        rows = (await conn.execute(select(
            wait_source_events.c.seq, wait_source_events.c.source_key,
            subscribed.label("subscribed"),
        ).where(
            wait_source_events.c.seq > position, wait_source_events.c.seq <= watermark,
        ).order_by(wait_source_events.c.seq).limit(self.batch_size))).mappings()
        prefix = []
        for row in rows:
            if row["subscribed"] or not row["source_key"].startswith((
                "ci_check:", "resource_invalidation:pool:",
            )):
                break
            prefix.append(row["seq"])
        return prefix

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
    async def drain(self, watermark):
        """Called only at the command boundary, with a prefetched fenced bound."""
        count = retries = 0
        retry_pending = True
        recovery_position = None
        attempted = set()
        while True:
            async with self.db._engine.begin() as conn:
                from src.waits.loops import prepare_progress

                position = await conn.scalar(select(wait_listener_cursors.c.last_seq).where(
                    wait_listener_cursors.c.listener_key == "source",
                ))
                self._retry_due = {seq: due for seq, due in self._retry_due.items()
                                   if due > self.clock()}
                if recovery_position is None:
                    recovery_position = position or 0
                # Retry earlier skipped observations without holding later
                # messages or task transitions behind a provider outage.
                retry = None
                if retry_pending and retries < self.retry_batch_size:
                    retry = (await conn.execute(select(wait_source_events).where(
                        wait_source_events.c.seq <= (position or 0),
                        wait_source_events.c.seq <= watermark,
                        wait_source_events.c.dispatched_at.is_(None),
                        wait_source_events.c.seq.not_in(self._retry_due),
                        wait_source_events.c.seq.not_in(attempted),
                    ).order_by(wait_source_events.c.seq).limit(1))).mappings().first()
                    if retry is None:
                        # Keep the undispatched partial index usable; combining
                        # these paths with OR scans the full outbox prefix.
                        retry = await self._dispatched_ci_retry(
                            conn, min(recovery_position, watermark), attempted,
                        )
                    # New failures in this pass already receive a backoff;
                    # do not rescan the retry index for every forward event.
                    retry_pending = retry is not None
                candidate = retry
                if candidate is None:
                    candidate = (await conn.execute(select(wait_source_events).where(
                        wait_source_events.c.seq > (position or 0),
                        wait_source_events.c.seq <= watermark,
                    ).order_by(wait_source_events.c.seq).limit(1))).mappings().first()
                if candidate:
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
                self._graph_busy = not await conn.scalar(select(
                    func.pg_try_advisory_xact_lock(REGISTRATION_LOCK)))
                if self._graph_busy:
                    return count
                track_graph_lock(conn)
                cursor = await self.locked_lease(conn)
                # Another drain may have advanced while Git was observed. Do
                # not dispatch a different event with the wrong progress heads.
                if cursor["last_seq"] != position:
                    continue
                # Relevant events commit separately; irrelevant prefixes retire
                # in bounded batches. Renewal cadence never defines lock scope.
                async with asyncio.timeout(self.lease_seconds):
                    retry_after = 0
                    processed = [] if retry else await self._unobserved_prefix(
                        conn, cursor["last_seq"], watermark)
                    if not processed:
                        rows = [retry] if retry else (await conn.execute(
                            select(wait_source_events).where(
                                wait_source_events.c.seq > cursor["last_seq"],
                                wait_source_events.c.seq <= watermark,
                            ).order_by(wait_source_events.c.seq).limit(1))).mappings().all()
                        if not rows:
                            return count
                        event = rows[0]
                        retry_after = await self._dispatch(conn, dict(event)) or 0
                        processed = [event["seq"]]
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
            if retry_after:
                self._retry_due[processed[-1]] = self.clock() + retry_after
                logger.warning(
                    "wait.source_observation_deferred seq=%s source_key=%s "
                    "retry_after=%s age_seconds=%.1f",
                    event["seq"], event["source_key"], retry_after,
                    max(0, self.clock() - event["occurred_at"]),
                )
            if retry:
                retries += 1
                attempted.add(retry["seq"])
                if retry["dispatched_at"] is not None and not retry_after:
                    # An unsatisfied observation is legitimate. Bound repeated
                    # recovery visits without requiring a new source revision.
                    self._retry_due[retry["seq"]] = self.clock() + self.safety_seconds
                if not retry_after:
                    logger.info("wait.source_observation_retried seq=%s", retry["seq"])
            count += len(processed)
            if not await self.service.lease():
                raise LeaseLost("wait source lease lost between batches")

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
                                       self.renewal_seconds if self._graph_busy
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
                # Subscribe before watermark/drain, then handle live hints.
                if not await self.tick():
                    await connection.close()
                    connection = None
                    await asyncio.sleep(self.renewal_seconds)
                    continue
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
