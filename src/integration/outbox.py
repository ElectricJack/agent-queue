"""Transactional outbox delivery for correctness-critical integration events."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, insert, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.exc import IntegrityError

from src.database.tables import integration_outbox, integration_outbox_artifact_pins


AcceptIntegrationEvent = Callable[[str, dict[str, Any], str], Awaitable[bool]]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AcceptanceState:
    manifest: tuple[dict[str, str], ...] | None
    cursor: int


class DestinationArtifactUnavailable(RuntimeError):
    """A captured activation artifact disappeared before it could be pinned."""


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
    try:
        async with db.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_outbox)
                        .where(
                            integration_outbox.c.id == event_id,
                            integration_outbox.c.delivered_at.is_(None),
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            existing = row["destination_manifest"]
            if existing is not None:
                return AcceptanceState(
                    manifest=tuple(dict(item) for item in existing),
                    cursor=int(row["acceptance_cursor"]),
                )
            artifact_shas = sorted({destination["artifact_sha256"] for destination in manifest})
            await conn.execute(
                insert(integration_outbox_artifact_pins),
                [
                    {"event_id": event_id, "artifact_sha256": artifact_sha256}
                    for artifact_sha256 in artifact_shas
                ],
            )
            result = await conn.execute(
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == event_id,
                    integration_outbox.c.delivered_at.is_(None),
                    integration_outbox.c.destination_manifest.is_(None),
                )
                .values(destination_manifest=manifest)
            )
            if int(result.rowcount) != 1:
                raise RuntimeError("integration destination manifest lost its row lock")
            return AcceptanceState(
                manifest=tuple(dict(item) for item in manifest),
                cursor=int(row["acceptance_cursor"]),
            )
    except IntegrityError as exc:
        raise DestinationArtifactUnavailable(event_id) from exc


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

    insert_fn = pg_insert
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
        clock: Callable[[], float] = time.time,
        before_dispatch: Callable[[str], Awaitable[None]] | None = None,
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
        self._clock = clock
        self._before_dispatch = before_dispatch
        self._cursor: tuple[float, str] | None = None
        self._cycle_end: tuple[float, str] | None = None

    async def dispatch_due(self, now: float) -> int:
        """Try at most one page of due events and return the acknowledged count."""
        rows = await self._page(now)
        if rows:
            self._cursor = (float(rows[-1]["available_at"]), rows[-1]["id"])

        delivered = 0
        for row in rows:
            started = self._clock()
            try:
                if self._before_dispatch is not None:
                    await self._before_dispatch(row["project_id"])
                accepted = await self._accept_event(
                    row["event_type"], dict(row["payload"]), row["id"]
                )
                if not accepted:
                    await self._retry(
                        row,
                        now=self._clock(),
                        error="no enabled matching playbook durably accepted the event",
                    )
                    continue
                if await self._acknowledge(row["id"], now=self._clock()):
                    delivered += 1
            except Exception as exc:  # retryable I/O/consumer failure; process exits still escape
                await self._retry(row, now=self._clock(), error=f"{type(exc).__name__}: {exc}")
            finally:
                elapsed = self._clock() - started
                if elapsed > 5.0:
                    logger.warning(
                        "integration slow outbox event=%s type=%s project=%s elapsed=%.3fs",
                        row["id"], row["event_type"], row["project_id"], elapsed,
                    )
        return delivered

    async def _page(self, now: float) -> list[Any]:
        """Freeze a scan boundary so retrying rows cannot monopolize later pages."""
        key = tuple_(integration_outbox.c.available_at, integration_outbox.c.id)
        due = select(integration_outbox).where(
            integration_outbox.c.delivered_at.is_(None),
            integration_outbox.c.available_at <= now,
        )
        async with self._db._engine.connect() as conn:
            for _ in range(2):
                if self._cycle_end is None:
                    end = (await conn.execute(
                        select(integration_outbox.c.available_at, integration_outbox.c.id)
                        .where(
                            integration_outbox.c.delivered_at.is_(None),
                            integration_outbox.c.available_at <= now,
                        )
                        .order_by(
                            integration_outbox.c.available_at.desc(), integration_outbox.c.id.desc()
                        )
                        .limit(1)
                    )).first()
                    if end is None:
                        return []
                    self._cycle_end = (float(end[0]), end[1])
                statement = due.where(key <= self._cycle_end)
                if self._cursor is not None:
                    statement = statement.where(key > self._cursor)
                rows = (await conn.execute(
                    statement.order_by(integration_outbox.c.available_at, integration_outbox.c.id)
                    .limit(self._page_size)
                )).mappings().all()
                if rows:
                    return rows
                self._cursor = self._cycle_end = None
        return []

    async def _acknowledge(self, event_id: str, *, now: float) -> bool:
        async with self._db.immediate() as conn:
            await conn.execute(
                delete(integration_outbox_artifact_pins).where(
                    integration_outbox_artifact_pins.c.event_id == event_id
                )
            )
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
        if attempts == 1 or attempts & (attempts - 1) == 0:
            logger.warning(
                "integration outbox retry event=%s type=%s project=%s attempts=%s "
                "retry_at=%.3f reason=%s",
                row["id"], row["event_type"], row["project_id"], attempts, now + delay, error,
            )
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
