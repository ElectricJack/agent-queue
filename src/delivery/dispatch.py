"""Escalation-first, bounded dispatch across typed domain adapters."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from src.delivery.message import FrozenMessage, MessageDelivery

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120.0
MAX_BATCH = 20


class DeliveryAdapter(Protocol):
    async def deliver(self, row: Mapping[str, Any]) -> None: ...


async def dispatch_batch(
    *,
    now: float,
    limit: int,
    claim: Callable[[int], Awaitable[list[dict]]],
    adapters: Mapping[str, DeliveryAdapter],
    escalation_priority: Callable[[float], Awaitable[int]] | None = None,
    rate_guard: Callable[[], bool] | None = None,
    clock: Callable[[], float] | None = None,
) -> str | None:
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"delivery batch limit must be between 1 and {MAX_BATCH}")
    if escalation_priority is not None:
        try:
            owed = await escalation_priority(now)
        except Exception:
            logger.warning("escalation priority unavailable", exc_info=True)
            return "escalation priority unavailable"
        if owed:
            return f"{owed} escalation deliveries take priority"
    if rate_guard is not None and not rate_guard():
        return "held by the Discord invalid-request rate guard"
    for _ in range(limit):
        # Recheck between writes: new escalations/rate faults stop the batch.
        try:
            probe_now = clock() if clock is not None else now
            if escalation_priority is not None and await escalation_priority(probe_now):
                return "escalation deliveries take priority"
            if rate_guard is not None and not rate_guard():
                return "held by the Discord invalid-request rate guard"
            rows = await claim(1)
            if not rows:
                break
            row = rows[0]
            await adapters[row.get("outbox", "digest")].deliver(row)
        except Exception:
            logger.warning("delivery failed; preserving its lease if claimed", exc_info=True)
    return None


class OutboundAdapter:
    def __init__(self, db: Any, delivery: MessageDelivery, *, lease_owner: str) -> None:
        self.db, self.delivery, self.lease_owner = db, delivery, lease_owner

    async def deliver(self, row: Mapping[str, Any]) -> None:
        destination = row["destination"]
        message = FrozenMessage(
            channel_id=destination["channel_id"],
            thread_id=destination.get("thread_id"),
            text=row["payload"]["text"],
            marker=row["marker"],
            attempt_count=row["attempt_count"],
            last_error=row.get("last_error"),
            reclaimed=bool(row.get("reclaimed")),
        )
        result = await self.delivery.deliver(message)
        await self.db.finish_outbound_delivery(
            row["id"],
            lease_owner=self.lease_owner,
            status=result.status,
            now=self.delivery.clock(),
            external_receipt_id=result.receipt_id,
            next_attempt_at=result.next_attempt_at,
            last_error=result.last_error,
        )
