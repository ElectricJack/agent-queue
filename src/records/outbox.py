"""Bounded, leased record delivery, independent of task and integration scheduling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    projects,
    record_consumer_receipts,
    record_outbox,
    knowledge_redactions,
)
from src.records.models import RecordError

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 10
LEASE_SECONDS = 30


def receipt_digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def lease_matches(row, event, now) -> bool:
    return bool(
        row
        and row["delivered_at"] is None
        and row["lease_token"] == event["lease_token"]
        and row["lease_until"] > now
    )


class RecordOutbox:
    def __init__(self, db, config, *, exporter=None, clock=None, interval=1.0):
        self.db = db
        self._config = config
        self.exporter = exporter
        self.clock = clock or (lambda: datetime.now(UTC))
        self.interval = interval
        self._slots = asyncio.Semaphore(2)
        self._tick_lock = asyncio.Lock()
        self._task = None

    @property
    def config(self):
        return self._config() if callable(self._config) else self._config

    async def claim_due(self, *, limit=2) -> list[dict]:
        cfg = self.config
        now = self.clock()
        destinations = ["audit"] + (["export"] if cfg.export.enabled else [])
        enabled_scopes = [f"project:{p}" for p in cfg.enabled_projects]
        if cfg.global_enabled:
            enabled_scopes.append("global")
        async with self.db.immediate() as conn:
            rows = (
                (
                    await conn.execute(
                        select(record_outbox)
                        .where(
                            record_outbox.c.delivered_at.is_(None),
                            record_outbox.c.available_at <= now,
                            record_outbox.c.attempts < MAX_ATTEMPTS,
                            or_(
                                record_outbox.c.lease_until.is_(None),
                                record_outbox.c.lease_until <= now,
                            ),
                            or_(
                                record_outbox.c.destination == "purge",
                                and_(
                                    cfg.enabled,
                                    record_outbox.c.destination.in_(destinations),
                                    record_outbox.c.scope_key.in_(enabled_scopes),
                                    or_(
                                        record_outbox.c.scope_key == "global",
                                        select(projects.c.id)
                                        .where(
                                            record_outbox.c.scope_key == "project:" + projects.c.id
                                        )
                                        .exists(),
                                    ),
                                ),
                            ),
                        )
                        .order_by(
                            case((record_outbox.c.destination == "purge", 0), else_=1),
                            record_outbox.c.available_at,
                            record_outbox.c.event_id,
                        )
                        .limit(max(1, min(limit, 50)))
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            claimed = []
            for row in rows:
                values = dict(
                    lease_token=uuid4(),
                    lease_until=now + timedelta(seconds=LEASE_SECONDS),
                    attempts=row["attempts"] + 1,
                )
                await conn.execute(
                    update(record_outbox)
                    .where(record_outbox.c.event_id == row["event_id"])
                    .values(**values)
                )
                claimed.append({**row, **values})
        return claimed

    async def acknowledge(self, event, result) -> bool:
        now = self.clock()
        async with self.db.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(record_outbox)
                        .where(record_outbox.c.event_id == event["event_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not lease_matches(row, event, now):
                return False
            await conn.execute(
                pg_insert(record_consumer_receipts)
                .values(
                    consumer=event["destination"],
                    event_id=event["event_id"],
                    processed_at=now,
                    result_digest=receipt_digest(result),
                )
                .on_conflict_do_nothing()
            )
            await conn.execute(
                update(record_outbox)
                .where(record_outbox.c.event_id == event["event_id"])
                .values(delivered_at=now, lease_token=None, lease_until=None, last_error_code=None)
            )
            if event["destination"] == "purge":
                redaction_id = event["payload"].get("redaction_id")
                from uuid import UUID

                await conn.execute(
                    select(knowledge_redactions.c.redaction_id)
                    .where(
                        knowledge_redactions.c.redaction_id == UUID(redaction_id),
                    )
                    .with_for_update()
                )
                pending = await conn.scalar(
                    select(record_outbox.c.event_id)
                    .where(
                        record_outbox.c.destination == "purge",
                        record_outbox.c.payload["redaction_id"].astext == redaction_id,
                        record_outbox.c.delivered_at.is_(None),
                    )
                    .limit(1)
                )
                if not pending:
                    await conn.execute(
                        update(knowledge_redactions)
                        .where(
                            knowledge_redactions.c.redaction_id == UUID(redaction_id),
                        )
                        .values(
                            completed_at=now,
                            cleanup_state={
                                "export": "purged",
                                "artifact": "purged",
                                "index": "absent",
                            },
                        )
                    )
        return True

    async def fail(self, event, code):
        now = self.clock()
        async with self.db.immediate() as conn:
            await conn.execute(
                update(record_outbox)
                .where(
                    record_outbox.c.event_id == event["event_id"],
                    record_outbox.c.lease_token == event["lease_token"],
                    record_outbox.c.lease_until > now,
                    record_outbox.c.delivered_at.is_(None),
                )
                .values(
                    lease_token=None,
                    lease_until=None,
                    last_error_code=code,
                    available_at=now + timedelta(seconds=min(300, 2 ** (event["attempts"] - 1))),
                )
            )

    async def deliver(self, event):
        async with self._slots:
            try:
                async with asyncio.timeout(5):
                    cfg = self.config
                    if event["destination"] == "purge":
                        from src.knowledge.redaction import purge_event

                        result = await purge_event(self.db, self.exporter, event)
                        return await self.acknowledge(event, result)
                    if not cfg.enabled or event["scope_key"] not in {
                        *[f"project:{p}" for p in cfg.enabled_projects],
                        *(["global"] if cfg.global_enabled else []),
                    }:
                        raise RecordError("knowledge.disabled")
                    if event["destination"] == "export":
                        if not cfg.export.enabled or self.exporter is None:
                            raise RecordError("record.export_disabled")
                        result = await self.exporter.deliver(event)
                    elif event["destination"] == "audit":
                        # The outbox is the durable audit envelope; its receipt
                        # and lease acknowledgment commit together, without a
                        # provider, plugin or transient EventBus dependency.
                        result = {"event_id": str(event["event_id"]), "state": "audited"}
                    else:
                        raise RecordError("record.destination_unavailable")
                    return await self.acknowledge(event, result)
            except RecordError as exc:
                await self.fail(event, exc.code)
            except TimeoutError:
                await self.fail(event, "record.delivery_timeout")
            except Exception:
                # Exception text may contain private content or filesystem paths.
                await self.fail(event, "record.delivery_failed")
        return False

    async def tick(self):
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            async with asyncio.timeout(15):
                events = await self.claim_due()
                await asyncio.gather(*(self.deliver(event) for event in events))

    async def replay(self, *, event_id, dry_run=True):
        """Operator-selected replay never steals a live lease or rewinds delivery."""
        async with self.db.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(record_outbox)
                        .where(record_outbox.c.event_id == event_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            eligible = bool(
                row
                and row["delivered_at"] is None
                and (row["lease_until"] is None or row["lease_until"] <= self.clock())
            )
            if eligible and not dry_run:
                await conn.execute(
                    update(record_outbox)
                    .where(record_outbox.c.event_id == event_id)
                    .values(
                        attempts=0,
                        available_at=self.clock(),
                        lease_token=None,
                        lease_until=None,
                        last_error_code=None,
                    )
                )
            return {"success": True, "dry_run": dry_run, "eligible": eligible}

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="record-outbox")

    async def _run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.warning("Record delivery unavailable; intents retained")
            await asyncio.sleep(self.interval)

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
