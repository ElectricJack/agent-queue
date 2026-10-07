"""Transactional outbox delivery for correctness-critical integration events."""

from __future__ import annotations

import logging
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

# Reviewed inventory C.10: these notifications have no shipped playbook
# consumer. Always offer them to operator-installed consumers before sinking.
UNSUBSCRIBED_EVENT_TYPES = frozenset({
    "promotion.source_settled", "promotion.request_due", "promotion.hotfix_completed",
    "promotion.intent_due", "promotion.delivered",
    "integration.root_delivered",
    "integration.human_blocked",
    "integration.cleanup_pending",
    "task.integration_configuration_blocked",
    "integration.branch_materialization_pending",
})
DEFAULT_MAX_WAIT_SECONDS = DEFAULT_INTEGRATION_MAX_WAIT_SECONDS
RETRY_EXHAUSTED_PREFIX = "retry_budget_exhausted: "


def settled_at(row: Any) -> float | None:
    """When an outbox row stopped being a pending delivery, or ``None`` while it is.

    A quarantined row keeps ``delivered_at`` unset because no consumer accepted
    it, but the outbox will not retry it either: it settled as a failed delivery
    at its deadline, which quarantine leaves in ``available_at``. Callers that
    pace their own continuations must not read an undelivered row as still
    queued, or a single quarantined event stalls their series until a manual
    replay. The row itself stays for inspection and replay.
    """
    if row["delivered_at"] is not None:
        return float(row["delivered_at"])
    if (row["last_error"] or "").startswith(RETRY_EXHAUSTED_PREFIX):
        return float(row["available_at"])
    return None


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
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        project_max_wait: ProjectMaxWait | None = None,
    ) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("retry delays must be positive")
        self._validate_max_wait(max_wait_seconds)
        self._db = db
        self._accept_event = accept_event
        self._page_size = page_size
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._clock = clock
        self._before_dispatch = before_dispatch
        self._max_wait_seconds = max_wait_seconds
        self._project_max_wait = project_max_wait
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
            except NoIntegrationEventConsumer as exc:
                if (
                    row["event_type"] in UNSUBSCRIBED_EVENT_TYPES
                    and await self._sink_unsubscribed(row, now=self._clock())
                ):
                    continue
                await self._retry(
                    row, now=self._clock(), error=f"{type(exc).__name__}: {exc}",
                )
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
        retryable = or_(
            integration_outbox.c.last_error.is_(None),
            ~integration_outbox.c.last_error.startswith(RETRY_EXHAUSTED_PREFIX, autoescape=True),
        )
        due = select(integration_outbox).where(
            integration_outbox.c.delivered_at.is_(None),
            integration_outbox.c.available_at <= now,
            retryable,
        )
        async with self._db._engine.connect() as conn:
            for _ in range(2):
                if self._cycle_end is None:
                    end = (await conn.execute(
                        select(integration_outbox.c.available_at, integration_outbox.c.id)
                        .where(
                            integration_outbox.c.delivered_at.is_(None),
                            integration_outbox.c.available_at <= now,
                            retryable,
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

    async def _sink_unsubscribed(self, row: Any, *, now: float) -> bool:
        """Record durable sink acceptance only when no destination was captured.

        A False consumer response can also mean a partially accepted fanout.
        Check the current manifest under the update lock rather than trusting
        the page's snapshot; those deliveries must retain their pins and retry.
        """
        reason = "unsubscribed: no enabled matching playbook durably accepted the event"
        async with self._db.immediate() as conn:
            result = await conn.execute(
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == row["id"],
                    integration_outbox.c.delivered_at.is_(None),
                    integration_outbox.c.destination_manifest.is_(None),
                    integration_outbox.c.attempts == row["attempts"],
                )
                .values(
                    delivered_at=now,
                    attempts=integration_outbox.c.attempts + 1,
                    last_error=reason,
                )
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
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == event_id,
                    integration_outbox.c.delivered_at.is_(None),
                    integration_outbox.c.last_error.startswith(
                        RETRY_EXHAUSTED_PREFIX, autoescape=True
                    ),
                )
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
            error = (
                f"{RETRY_EXHAUSTED_PREFIX}max_wait={max_wait:g}s deadline={deadline:.3f}; {error}"
            )
        async with self._db.immediate() as conn:
            result = await conn.execute(
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == row["id"],
                    integration_outbox.c.delivered_at.is_(None),
                    integration_outbox.c.attempts == row["attempts"],
                )
                .values(
                    attempts=attempts,
                    available_at=retry_at,
                    last_error=error[:2000],
                )
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
