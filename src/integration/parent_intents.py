"""Durable continuation facts emitted by current parent promotion primitives.

Subject visits own retries. This module has no autonomous intent scan or writer.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import select

from src.database.tables import integration_outbox
from src.integration.green_continuation import continuation_delay
from src.integration.outbox import RETRY_EXHAUSTED_PREFIX, enqueue_integration_event

logger = logging.getLogger(__name__)

#: An intent younger than this may still be inside its own ``delivery.ready`` run.
GRACE_SECONDS = 60.0


def _settled_at(row: Any) -> float | None:
    """When a continuation stopped being pending, or ``None`` while it still is.

    A quarantined row (``retry_budget_exhausted:``) keeps ``delivered_at`` unset
    because no consumer accepted it, but the outbox will not retry it either: it
    settled as a failed delivery at its deadline, which quarantine leaves in
    ``available_at``.
    """
    if row["delivered_at"] is not None:
        return float(row["delivered_at"])
    if (row["last_error"] or "").startswith(RETRY_EXHAUSTED_PREFIX):
        return float(row["available_at"])
    return None


async def enqueue_parent_continuation_on(conn, *, intent, fence, now) -> bool:
    """Pace wakeups durably under the caller's collector and intent locks."""
    prefix = f"parent-intent:{intent['id']}:{fence.owner_id}:{fence.token}:"
    emitted = (await conn.execute(
        select(integration_outbox).where(
            integration_outbox.c.dedup_key.startswith(prefix, autoescape=True),
        ).order_by(integration_outbox.c.available_at, integration_outbox.c.id)
    )).mappings().all()
    settled = [_settled_at(row) for row in emitted]
    if None in settled:
        return False
    if emitted:
        due = max(
            float(emitted[-1]["available_at"]) + continuation_delay(len(emitted)),
            settled[-1] + GRACE_SECONDS,
        )
        if now < due:
            return False
    key = prefix + str(len(emitted))
    await enqueue_integration_event(
        conn,
        event_id="parent-continuation-" + hashlib.sha256(key.encode()).hexdigest(),
        dedup_key=key,
        project_id=intent["project_id"],
        event_type="delivery.ready",
        available_at=now,
        payload={
            "operation_id": intent["operation_key"],
            "operation_key": intent["operation_key"],
            "source_task_id": intent["source_task_id"],
            "source_head": intent["source_head"],
            "source_base": intent["source_base"],
            "expected_target": intent["expected_target"],
            "fence": fence.model_dump(mode="json"),
        },
    )
    return True
