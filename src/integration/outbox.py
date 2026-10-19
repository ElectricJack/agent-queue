"""Transactional outbox delivery for correctness-critical integration events."""

from __future__ import annotations

import logging
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, insert, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.exc import IntegrityError

from src.database.tables import integration_outbox, integration_outbox_artifact_pins
from src.integration.models import DEFAULT_INTEGRATION_MAX_WAIT_SECONDS


AcceptIntegrationEvent = Callable[[str, dict[str, Any], str], Awaitable[bool]]
ProjectMaxWait = Callable[[str], Awaitable[float]]
logger = logging.getLogger(__name__)

PROMOTION_EVENT_TYPES = frozenset({
    "promotion.source_settled", "promotion.request_due", "promotion.hotfix_completed",
    "promotion.intent_due", "promotion.delivered",
})

# Held and receipt wakeups must retain delivery intent until policy accepts
# them. Scope/release notifications are observable but have no required action.
BATCH_EVENT_TYPES = frozenset({
    "batch.held", "batch.receipt_recorded", "batch.needs_scope", "batch.released",
})
ACTIVE_OUTBOX_EVENT_TYPES = PROMOTION_EVENT_TYPES | BATCH_EVENT_TYPES

# Optional notifications may settle only after proving no enabled consumer;
# always offer them to reviewed and operator-installed consumers before sinking.
UNSUBSCRIBED_EVENT_TYPES = frozenset({
    *PROMOTION_EVENT_TYPES,
    "batch.needs_scope",
    "batch.released",
    "integration.root_delivered",
    "integration.human_blocked",
    "integration.cleanup_pending",
    "task.integration_configuration_blocked",
    "integration.branch_materialization_pending",
})
DEFAULT_MAX_WAIT_SECONDS = DEFAULT_INTEGRATION_MAX_WAIT_SECONDS
RETRY_EXHAUSTED_PREFIX = "retry_budget_exhausted: "


@dataclass(frozen=True, slots=True)
class AcceptanceState:
    manifest: tuple[dict[str, str], ...] | None
    cursor: int


class DestinationArtifactUnavailable(RuntimeError):
    """A captured activation artifact disappeared before it could be pinned."""


class NoIntegrationEventConsumer(RuntimeError):
    """The consumer proved no subscription matches, rather than failing acceptance.

    Adapters may raise this only after ruling out incomplete fanout, missing
    artifacts, disabled runtime and unavailable frozen operation routes. A
    plain False remains retryable because it carries no such evidence.
    """


async def load_acceptance_state(db: Any, event_id: str) -> AcceptanceState:
    """Read the frozen destination manifest and its durable continuation."""
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(
            integration_outbox.c.destination_manifest, integration_outbox.c.acceptance_cursor,
        ).where(integration_outbox.c.id == event_id))).mappings().one()
    raw = row["destination_manifest"]
    manifest = None if raw is None else tuple(dict(item) for item in raw)
    return AcceptanceState(manifest=manifest, cursor=int(row["acceptance_cursor"]))


def _pending_event(event_id: str, *, attempts: int | None = None):
    """Build every outbox mutation with the same pending-delivery guard."""
    statement = update(integration_outbox).where(
        integration_outbox.c.id == event_id, integration_outbox.c.delivered_at.is_(None))
    return statement if attempts is None else statement.where(
        integration_outbox.c.attempts == attempts)


async def freeze_destination_manifest(db: Any, event_id: str,
                                      manifest: list[dict[str, str]]) -> AcceptanceState:
    """Persist the first non-empty destination snapshot; concurrent retries reuse it."""
    if not manifest:
        raise ValueError("an empty integration destination manifest is not frozen")
    try:
        async with db.immediate() as conn:
            row = (await conn.execute(select(integration_outbox).where(
                integration_outbox.c.id == event_id,
                integration_outbox.c.delivered_at.is_(None),
            ).with_for_update())).mappings().one()
            existing = row["destination_manifest"]
            if existing is not None:
                return AcceptanceState(manifest=tuple(dict(item) for item in existing),
                                       cursor=int(row["acceptance_cursor"]))
            artifact_shas = sorted({destination["artifact_sha256"] for destination in manifest})
            await conn.execute(
                insert(integration_outbox_artifact_pins),
                [{"event_id": event_id, "artifact_sha256": sha} for sha in artifact_shas],
            )
            result = await conn.execute(
                _pending_event(event_id).where(integration_outbox.c.destination_manifest.is_(None))
                .values(destination_manifest=manifest)
            )
            if int(result.rowcount) != 1:
                raise RuntimeError("integration destination manifest lost its row lock")
            return AcceptanceState(manifest=tuple(dict(item) for item in manifest),
                                   cursor=int(row["acceptance_cursor"]))
    except IntegrityError as exc:
        raise DestinationArtifactUnavailable(event_id) from exc


async def advance_acceptance_cursor(db: Any, event_id: str, *, expected: int,
                                    accepted: int) -> AcceptanceState:
    """Advance only after the complete page is durable; never regress the cursor."""
    if accepted < expected:
        raise ValueError("integration acceptance cursor cannot regress")
    async with db.immediate() as conn:
        await conn.execute(
            _pending_event(event_id).where(integration_outbox.c.acceptance_cursor == expected)
            .values(acceptance_cursor=accepted)
        )
    return await load_acceptance_state(db, event_id)


async def enqueue_integration_event(
    conn: AsyncConnection, *, event_id: str, dedup_key: str, project_id: str,
    event_type: str, payload: dict, available_at: float,
) -> None:
    """Insert an event on the caller's transaction, idempotently by domain key."""
    if not event_id or not dedup_key or not project_id or not event_type:
        raise ValueError("event_id, dedup_key, project_id, and event_type are required")
    body = dict(payload)
    if body.get("project_id", project_id) != project_id:
        raise ValueError("payload project_id does not match the outbox project")
    if body.get("event_id", event_id) != event_id:
        raise ValueError("payload event_id does not match the outbox event")
    body.update(project_id=project_id, event_id=event_id)

    statement = pg_insert(integration_outbox).values(
        id=event_id, dedup_key=dedup_key, project_id=project_id, event_type=event_type,
        payload=body, available_at=available_at, attempts=0, created_at=time.time(),
    )
    await conn.execute(statement.on_conflict_do_nothing())
    row = (await conn.execute(select(integration_outbox).where(or_(
        integration_outbox.c.id == event_id, integration_outbox.c.dedup_key == dedup_key,
    )))).mappings().one()
    identity = (row["id"], row["dedup_key"], row["project_id"], row["event_type"])
    if identity != (event_id, dedup_key, project_id, event_type) or dict(row["payload"]) != body:
        raise ValueError("integration event identity was reused with different content")


async def enqueue_batch_event(
    conn: AsyncConnection, batch: dict, event_type: str, *,
    generation: str | int = 0, now: float | None = None,
) -> str:
    """Persist a batch wakeup on the transaction that records its domain state.

    A generation is a durable retry token, not caller authority. Replays keep
    the same identity and payload, including after reviewer reassignment.
    Released notifications attest the frozen release rather than later edits.
    """
    if event_type not in BATCH_EVENT_TYPES:
        raise ValueError(f"not a batch event: {event_type}")
    released = event_type == "batch.released"
    revision = batch["release_revision"] if released else batch["structure_revision"]
    digest = batch["release_digest"] if released else batch["structure_digest"]
    if revision is None or digest is None:
        raise ValueError("a batch event requires its recorded revision and digest")
    token = hashlib.sha256(json.dumps([
        event_type, batch["project_id"], batch["batch_id"], revision, digest, generation,
    ]).encode()).hexdigest()
    event_id = f"batch-event-{token}"
    # The first enqueue owns the immutable payload. Reviewer assignment may
    # change without a structure revision, but retrying an intent cannot.
    if (await conn.execute(select(integration_outbox.c.id).where(
        integration_outbox.c.id == event_id,
    ))).first():
        return event_id
    await enqueue_integration_event(
        conn, event_id=event_id, dedup_key=f"batch:{token}",
        project_id=batch["project_id"], event_type=event_type,
        payload={"project_id": batch["project_id"], "batch_id": batch["batch_id"],
                 "structure_revision": revision, "structure_digest": digest,
                 "reviewer_task_id": batch["reviewer_task_id"]},
        available_at=time.time() if now is None else now,
    )
    return event_id


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
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        project_max_wait: ProjectMaxWait | None = None,
    ) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("retry delays must be positive")
        self._validate_max_wait(max_wait_seconds)
        self._db, self._accept_event = db, accept_event
        self._page_size = page_size
        self._retry_base_seconds, self._retry_max_seconds = retry_base_seconds, retry_max_seconds
        self._clock = clock
        self._before_dispatch = before_dispatch
        self._max_wait_seconds, self._project_max_wait = max_wait_seconds, project_max_wait
        self._cursor: tuple[float, str] | None = None
        self._cycle_end: tuple[float, str] | None = None

    async def dispatch_due(self, now: float, *, event_types: frozenset[str] | None = None) -> int:
        """Try at most one page of due events and return the acknowledged count."""
        rows = await self._page(now, event_types=event_types)
        if rows:
            self._cursor = (float(rows[-1]["available_at"]), rows[-1]["id"])

        delivered = 0
        for row in rows:
            started = self._clock()
            try:
                if self._before_dispatch is not None:
                    await self._before_dispatch(row["project_id"])
                accepted = await self._accept_event(
                    row["event_type"], dict(row["payload"]), row["id"])
                if not accepted:
                    await self._retry(
                        row, now=self._clock(),
                        error="no enabled matching playbook durably accepted the event",
                    )
                    continue
                if await self._acknowledge(row["id"], now=self._clock()):
                    delivered += 1
            except NoIntegrationEventConsumer as exc:
                if (row["event_type"] in UNSUBSCRIBED_EVENT_TYPES
                        and await self._sink_unsubscribed(row, now=self._clock())):
                    continue
                await self._retry(row, now=self._clock(), error=f"{type(exc).__name__}: {exc}")
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

    async def _page(self, now: float, *, event_types: frozenset[str] | None = None) -> list[Any]:
        """Freeze a scan boundary so retrying rows cannot monopolize later pages."""
        key = tuple_(integration_outbox.c.available_at, integration_outbox.c.id)
        retryable = or_(
            integration_outbox.c.last_error.is_(None),
            ~integration_outbox.c.last_error.startswith(RETRY_EXHAUSTED_PREFIX, autoescape=True),
        )
        due = select(integration_outbox).where(
            integration_outbox.c.delivered_at.is_(None),
            integration_outbox.c.available_at <= now,
            retryable,
        )
        if event_types is not None:
            due = due.where(integration_outbox.c.event_type.in_(sorted(event_types)))
        async with self._db._engine.connect() as conn:
            for _ in range(2):
                if self._cycle_end is None:
                    end = (await conn.execute(
                        due.with_only_columns(
                            integration_outbox.c.available_at, integration_outbox.c.id)
                        .order_by(integration_outbox.c.available_at.desc(),
                                  integration_outbox.c.id.desc()).limit(1)
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
            await conn.execute(delete(integration_outbox_artifact_pins).where(
                integration_outbox_artifact_pins.c.event_id == event_id))
            result = await conn.execute(
                _pending_event(event_id).values(
                    delivered_at=now, attempts=integration_outbox.c.attempts + 1, last_error=None)
            )
        return int(result.rowcount) == 1

    async def _sink_unsubscribed(self, row: Any, *, now: float) -> bool:
        """Record durable sink acceptance only when no destination was captured.

        A False consumer response can also mean a partially accepted fanout.
        Check the current manifest under the update lock rather than trusting
        the page's snapshot; those deliveries must retain their pins and retry.
        """
        reason = "unsubscribed: no enabled matching playbook durably accepted the event"
        async with self._db.immediate() as conn:
            result = await conn.execute(
                _pending_event(row["id"], attempts=row["attempts"])
                .where(integration_outbox.c.destination_manifest.is_(None))
                .values(delivered_at=now, attempts=integration_outbox.c.attempts + 1,
                        last_error=reason)
            )
        sunk = int(result.rowcount) == 1
        if sunk:
            logger.warning(
                "integration outbox sink event=%s type=%s project=%s reason=%s",
                row["id"], row["event_type"], row["project_id"], reason,
            )
        return sunk

    @staticmethod
    def _validate_max_wait(max_wait: float) -> None:
        if not math.isfinite(max_wait) or max_wait <= 0:
            raise ValueError("max_wait must be finite and positive")

    async def replay(self, event_id: str, *, now: float) -> bool:
        """Requeue a quarantined event after recovery, retaining receipt identity.

        This does not reset the original age budget. The recovered consumer
        gets one more acceptance attempt; a failure is quarantined again.
        Call through the owning CommandHandler recovery path, never as an
        alternative acknowledgment of an event whose consumer failed.
        """
        async with self._db.immediate() as conn:
            result = await conn.execute(
                _pending_event(event_id).where(integration_outbox.c.last_error.startswith(
                    RETRY_EXHAUSTED_PREFIX, autoescape=True))
                .values(available_at=now, last_error=None)
            )
        return int(result.rowcount) == 1

    async def _retry(self, row: Any, *, now: float, error: str) -> None:
        max_wait = self._max_wait_seconds
        if self._project_max_wait is not None:
            max_wait = await self._project_max_wait(row["project_id"])
        self._validate_max_wait(max_wait)
        deadline = float(row["created_at"]) + max_wait
        attempts = int(row["attempts"]) + 1
        exponent = min(max(attempts - 1, 0), 62)
        delay = min(self._retry_max_seconds, self._retry_base_seconds * (2**exponent))
        exhausted = now >= deadline
        retry_at = min(now + delay, deadline)
        if exhausted:
            # Leave delivered_at unset and every frozen destination/pin intact:
            # this is an explicit failed delivery, not consumer acceptance.
            error = f"{RETRY_EXHAUSTED_PREFIX}max_wait={max_wait:g}s deadline={deadline:.3f}; {error}"
        async with self._db.immediate() as conn:
            result = await conn.execute(
                _pending_event(row["id"], attempts=row["attempts"])
                .values(attempts=attempts, available_at=retry_at, last_error=error[:2000])
            )
        if int(result.rowcount) != 1:
            return
        if exhausted:
            logger.error(
                "integration outbox quarantined event=%s type=%s project=%s attempts=%s reason=%s",
                row["id"], row["event_type"], row["project_id"], attempts, error,
            )
        elif attempts == 1 or attempts & (attempts - 1) == 0:
            logger.warning(
                "integration outbox retry event=%s type=%s project=%s attempts=%s "
                "retry_at=%.3f reason=%s",
                row["id"], row["event_type"], row["project_id"], attempts, retry_at, error,
            )
