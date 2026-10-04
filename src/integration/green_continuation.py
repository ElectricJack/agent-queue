"""Exact promotion identities and idempotent facts for repair handoff.

Root Subject visits own retries. There is no separate root continuation loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select

from src.database.tables import (
    integration_outbox,
)
from src.integration.outbox import enqueue_integration_event

logger = logging.getLogger(__name__)

GREEN_EVENT_TYPE = "integration.candidate_green"
CONTINUATION_PREFIX = "integration-green-continuation"
#: First re-emission waits this long after the previous continuation; each
#: further generation doubles it (60, 120, 240, ... seconds) up to the ceiling.
RETRY_BASE_SECONDS = 60.0
#: The longest wait between two continuations of one fingerprint.
MAX_BACKOFF_SECONDS = 3600.0
#: Generations after which a still-refused promotion is reported as a warning
#: (it keeps being retried; this is only the point where a person should look).
WARN_AFTER_GENERATIONS = 6
#: A delivered green fact gets this long to reach ``prepare`` before the
#: reconciler treats its promotion as lost.
DELIVERY_GRACE_SECONDS = 60.0


def promotion_fingerprint(
    *,
    operation_id: str,
    batch_id: str,
    revision: int,
    head_sha: str,
    evidence_id: str,
    owner: dict[str, Any] | None,
    lease: dict[str, Any] | None,
) -> str:
    """Hash every authority a root promotion binds; fences are monotonic."""
    identity = json.dumps(
        {
            "operation_id": operation_id,
            "batch_id": batch_id,
            "revision": int(revision),
            "head_sha": head_sha,
            "evidence_id": evidence_id,
            "owner": (
                [owner["owner_id"], owner["owner_role"], int(owner["fence_token"])]
                if owner is not None
                else None
            ),
            "lease": (
                [lease["owner_id"], int(lease["fence_token"])] if lease is not None else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def continuation_delay(generation: int) -> float:
    """Seconds between continuation ``generation`` and the next one."""
    exponent = min(max(int(generation) - 1, 0), 32)
    return min(RETRY_BASE_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS)


def _dedup_prefix(batch_id: str, revision: int, fingerprint: str) -> str:
    return f"{CONTINUATION_PREFIX}:{batch_id}:{int(revision)}:{fingerprint}:"


async def continuation_rows_on(
    conn, *, project_id: str, batch_id: str, revision: int, fingerprint: str
) -> list[dict[str, Any]]:
    """Return the continuations already emitted for one fingerprint, oldest first."""
    rows = (
        await conn.execute(
            select(
                integration_outbox.c.id,
                integration_outbox.c.dedup_key,
                integration_outbox.c.available_at,
                integration_outbox.c.delivered_at,
            )
            .where(
                integration_outbox.c.project_id == project_id,
                integration_outbox.c.event_type == GREEN_EVENT_TYPE,
                integration_outbox.c.dedup_key.startswith(
                    _dedup_prefix(batch_id, revision, fingerprint), autoescape=True
                ),
            )
            .order_by(integration_outbox.c.available_at, integration_outbox.c.id)
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def enqueue_green_continuation_on(
    conn,
    *,
    project_id: str,
    operation_id: str,
    batch_id: str,
    revision: int,
    head_sha: str,
    fingerprint: str,
    generation: int,
    now: float,
) -> str:
    """Enqueue one idempotent green continuation on the caller's transaction."""
    dedup_key = _dedup_prefix(batch_id, revision, fingerprint) + str(int(generation))
    event_id = f"{CONTINUATION_PREFIX}-" + hashlib.sha256(dedup_key.encode()).hexdigest()
    await enqueue_integration_event(
        conn,
        event_id=event_id,
        dedup_key=dedup_key,
        project_id=project_id,
        event_type=GREEN_EVENT_TYPE,
        payload={
            "operation_id": operation_id,
            "batch_id": batch_id,
            "revision": int(revision),
            "head_sha": head_sha,
        },
        available_at=now,
    )
    return event_id
