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
from src.waits.resource_sources import (
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

    def __init__(self, service, *, clock=time.time, owner=None, connect=None):
        self.service = service
        self.db = service.orch.db
        self.clock = clock
        self.owner = owner or str(uuid.uuid4())
        self.connect = connect or self._connect
        self.changed = asyncio.Event()
        self.deadlines_changed = asyncio.Event()
        self._tick_lock = asyncio.Lock()

    async def _connect(self):
        import asyncpg

        # A dedicated connection, never a checked-out SQLAlchemy pool member.
        url = self.db._engine.url
        return await asyncpg.connect(
            user=url.username, password=url.password, host=url.host, port=url.port,
            database=url.database, **dict(url.query),
        )

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
        dispatcher = self.service.dispatcher
        lifecycle = getattr(self.service, "lifecycle", None)
        if lifecycle is not None and getattr(
            self.service.orch.config.wait_policy, 'hibernation_enabled', False,
        ):
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
            return
        rows = (await conn.execute(select(wait_records).where(
            wait_records.c.outcome == "pending",
            wait_records.c.verification_state == "verified",
            wait_records.c.source_key == event["source_key"],
            wait_records.c.source_generation == event["source_generation"],
            wait_records.c.source_watermark_seq < event["seq"],
        ).order_by(wait_records.c.id))).mappings().all()
        if rows and dispatcher is None:
            raise ListenerUnavailable("WakeDispatcher is required")
        for row in rows:
            await self._commit(dispatcher, conn, dict(row), event)

    @staticmethod
    async def _commit(dispatcher, conn, row, event, **kwargs):
        result = await dispatcher.commit(conn, row, event, **kwargs)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ListenerUnavailable(f"wait dispatch refused: {result}")

    async def drain(self, watermark):
        """Called only at the command boundary, with a prefetched fenced bound."""
        count = 0
        while True:
            async with self.db._engine.begin() as conn:
                cursor = await self.locked_lease(conn)
                # Bound a batch below the renewal interval so a slow adapter
                # cannot keep an expired leader writing or starve its renewal.
                async with asyncio.timeout(self.renewal_seconds):
                    await conn.execute(select(func.pg_advisory_xact_lock(
                        REGISTRATION_LOCK,
                    )))
                    rows = (await conn.execute(select(wait_source_events).where(
                        wait_source_events.c.seq > cursor["last_seq"],
                        wait_source_events.c.seq <= watermark,
                    ).order_by(wait_source_events.c.seq).limit(self.batch_size))).mappings().all()
                    if not rows:
                        return count
                    for event in rows:
                        await self._dispatch(conn, dict(event))
                    if cursor["lease_expires_at"] <= self.clock():
                        raise LeaseLost("wait source lease expired during dispatch")
                    await conn.execute(update(wait_source_events).where(
                        wait_source_events.c.seq.in_([row["seq"] for row in rows]),
                    ).values(dispatched_at=self.clock()))
                    await conn.execute(update(wait_listener_cursors).where(
                        wait_listener_cursors.c.listener_key == "source",
                        wait_listener_cursors.c.lease_owner == self.owner,
                    ).values(last_seq=rows[-1]["seq"], updated_at=self.clock()))
            count += len(rows)
            if not await self.service.lease():
                raise LeaseLost("wait source lease lost between batches")

    async def tick(self):
        async with self._tick_lock:
            if not self.service.enabled or not await self.service.lease():
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
                await asyncio.wait_for(self.changed.wait(), self.safety_seconds)
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
