"""Transactional outbox delivery for correctness-critical integration events."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import integration_outbox


AcceptIntegrationEvent = Callable[[str, dict[str, Any], str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class AcceptanceState:
    manifest: tuple[dict[str, str], ...] | None
    cursor: int


async def load_acceptance_state(db: Any, event_id: str) -> AcceptanceState:
    """Read the frozen destination manifest and its durable continuation."""
    async with db._engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(
                        integration_outbox.c.destination_manifest,
                        integration_outbox.c.acceptance_cursor,
                    ).where(integration_outbox.c.id == event_id)
                )
            )
            .mappings()
            .one()
        )
    raw = row["destination_manifest"]
    manifest = None if raw is None else tuple(dict(item) for item in raw)
    return AcceptanceState(manifest=manifest, cursor=int(row["acceptance_cursor"]))


async def freeze_destination_manifest(
    db: Any, event_id: str, manifest: list[dict[str, str]]
) -> AcceptanceState:
    """Persist the first non-empty destination snapshot; concurrent retries reuse it."""
    if not manifest:
        raise ValueError("an empty integration destination manifest is not frozen")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_outbox)
            .where(
                integration_outbox.c.id == event_id,
                integration_outbox.c.delivered_at.is_(None),
                integration_outbox.c.destination_manifest.is_(None),
            )
            .values(destination_manifest=manifest)
        )
    return await load_acceptance_state(db, event_id)


async def advance_acceptance_cursor(
    db: Any, event_id: str, *, expected: int, accepted: int
) -> AcceptanceState:
    """Advance only after the complete page is durable; never regress the cursor."""
    if accepted < expected:
        raise ValueError("integration acceptance cursor cannot regress")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_outbox)
            .where(
                integration_outbox.c.id == event_id,
                integration_outbox.c.delivered_at.is_(None),
                integration_outbox.c.acceptance_cursor == expected,
            )
            .values(acceptance_cursor=accepted)
        )
    return await load_acceptance_state(db, event_id)


async def enqueue_integration_event(
    conn: AsyncConnection,
    *,
    event_id: str,
    dedup_key: str,
    project_id: str,
    event_type: str,
    payload: dict,
    available_at: float,
) -> None:
    """Insert an event on the caller's transaction, idempotently by domain key."""
    if not event_id or not dedup_key or not project_id or not event_type:
        raise ValueError("event_id, dedup_key, project_id, and event_type are required")
    body = dict(payload)
    if body.get("project_id", project_id) != project_id:
        raise ValueError("payload project_id does not match the outbox project")
    if body.get("event_id", event_id) != event_id:
        raise ValueError("payload event_id does not match the outbox event")
    body["project_id"] = project_id
    body["event_id"] = event_id

    insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    statement = insert_fn(integration_outbox).values(
        id=event_id,
        dedup_key=dedup_key,
        project_id=project_id,
        event_type=event_type,
        payload=body,
        available_at=available_at,
        attempts=0,
        created_at=time.time(),
    )
    await conn.execute(statement.on_conflict_do_nothing())
    row = (
        (
            await conn.execute(
                select(integration_outbox).where(
                    or_(
                        integration_outbox.c.id == event_id,
                        integration_outbox.c.dedup_key == dedup_key,
                    )
                )
            )
        )
        .mappings()
        .one()
    )
    identity = (row["id"], row["dedup_key"], row["project_id"], row["event_type"])
    if identity != (event_id, dedup_key, project_id, event_type) or dict(row["payload"]) != body:
        raise ValueError("integration event identity was reused with different content")


class IntegrationOutbox:
    """Deliver one bounded page, acknowledging only durable consumer acceptance."""

    def __init__(
        self,
        db: Any,
        accept_event: AcceptIntegrationEvent,
        *,
        page_size: int = 100,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 300.0,
    ) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("retry delays must be positive")
        self._db = db
        self._accept_event = accept_event
        self._page_size = page_size
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    async def dispatch_due(self, now: float) -> int:
        """Try at most one page of due events and return the acknowledged count."""
        async with self._db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(integration_outbox)
                        .where(
                            integration_outbox.c.delivered_at.is_(None),
                            integration_outbox.c.available_at <= now,
                        )
                        .order_by(
                            integration_outbox.c.available_at,
                            integration_outbox.c.id,
                        )
                        .limit(self._page_size)
                    )
                )
                .mappings()
                .all()
            )

        delivered = 0
        for row in rows:
            try:
                accepted = await self._accept_event(
                    row["event_type"], dict(row["payload"]), row["id"]
                )
                if not accepted:
                    await self._retry(
                        row,
                        now=now,
                        error="no enabled matching playbook durably accepted the event",
                    )
                    continue
                if await self._acknowledge(row["id"], now=now):
                    delivered += 1
            except Exception as exc:  # retryable I/O/consumer failure; process exits still escape
                await self._retry(row, now=now, error=f"{type(exc).__name__}: {exc}")
        return delivered

    async def _acknowledge(self, event_id: str, *, now: float) -> bool:
        async with self._db.immediate() as conn:
            result = await conn.execute(
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == event_id,
                    integration_outbox.c.delivered_at.is_(None),
                )
                .values(
                    delivered_at=now,
                    attempts=integration_outbox.c.attempts + 1,
                    last_error=None,
                )
            )
        return int(result.rowcount) == 1

    async def _retry(self, row: Any, *, now: float, error: str) -> None:
        attempts = int(row["attempts"]) + 1
        exponent = min(max(attempts - 1, 0), 62)
        delay = min(self._retry_max_seconds, self._retry_base_seconds * (2**exponent))
        async with self._db.immediate() as conn:
            await conn.execute(
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == row["id"],
                    integration_outbox.c.delivered_at.is_(None),
                    integration_outbox.c.attempts == row["attempts"],
                )
                .values(
                    attempts=attempts,
                    available_at=now + delay,
                    last_error=error[:2000],
                )
            )
