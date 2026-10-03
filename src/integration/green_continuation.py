"""Durable promotion continuations for exact-green root candidates.

CI emits one ``integration.candidate_green`` fact per exact evidence identity.
When that fact's promotion waited (the branch was still held by an attached
repair writer, the project lease was short, a reconciliation was in flight),
re-observing the same evidence dedups to the already-delivered event and the
batch stays green forever.  This module owns the paced replacement wakeup:

* a fresh continuation is keyed by the *promotion fingerprint* — exact
  candidate, evidence, branch-owner fence and project-lease fence — so a real
  authority change (a closed writer handing the branch back) always yields a
  new event, and a replayed or duplicated tick never does;
* within one fingerprint, re-emission backs off exponentially up to
  :data:`MAX_BACKOFF_SECONDS` and never gives up: a promotion refused for a
  transient reason (a writer still attached, a short lease) is re-driven at
  least once an hour until it promotes or its authority changes, so a lost
  or refused wakeup cannot leave a green batch waiting for nothing;
* nothing here builds, tests, dispatches a repair, or supplies evidence.  The
  event names only the subject; ``integration_promote_main`` re-derives every
  authority from durable state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, literal, select, tuple_

from src.database.tables import (
    integration_batches,
    integration_candidate_revisions,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
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


class GreenPromotionReconciler:
    """Re-drive exact-green root batches whose promotion wakeup was lost.

    Each tick reads a bounded page of batches whose current revision is
    green, whose active stage awaits completion and which have no root
    promotion intent.  For each it first returns a closed repair writer's
    branch to the collector (which itself enqueues the continuation), then,
    only when the durable snapshot is promotable right now, re-emits a
    paced continuation.  Stale, human-gated, attached-writer and
    already-promoting batches are reported and left alone.
    """

    def __init__(
        self,
        db: Any,
        *,
        promotion: Any,
        repair: Any | None = None,
        clock: Callable[[], float] = time.time,
        page_size: int = 50,
    ) -> None:
        if page_size <= 0:
            raise ValueError("green continuation page size must be positive")
        if repair is None:
            from src.integration.repair import RepairService

            repair = RepairService(db, clock=clock)
        self.db = db
        self.promotion = promotion
        self.repair = repair
        self.clock = clock
        self.page_size = page_size
        self._reported: dict[str, str] = {}
        self._cursor: tuple[float, str] | None = None

    async def tick(self, now: float) -> None:
        page = await self._candidate_page(after=self._cursor, limit=self.page_size)
        if not page and self._cursor is not None:
            page = await self._candidate_page(after=None, limit=self.page_size)
        # Fairness advances on what was scanned: a refused batch never pins
        # the page while later green batches wait.
        self._cursor = (page[-1]["updated_at"], page[-1]["id"]) if page else None
        batch_ids = [row["id"] for row in page]
        for batch_id in batch_ids:
            try:
                result = await self.reconcile(batch_id, now=now)
            except Exception:
                logger.exception("green promotion continuation failed for %s", batch_id)
                continue
            self._report(batch_id, result)
        if self._cursor is None or len(batch_ids) < self.page_size:
            self._reported = {
                batch_id: summary
                for batch_id, summary in self._reported.items()
                if batch_id in batch_ids
            }

    async def candidate_batches(self, *, limit: int) -> list[str]:
        """Exact-green awaiting-completion root batches with no root intent."""
        return [row["id"] for row in await self._candidate_page(after=None, limit=limit)]

    async def _candidate_page(
        self, *, after: tuple[float, str] | None, limit: int
    ) -> list[dict[str, Any]]:
        intent = (
            select(integration_promotion_intents.c.id)
            .where(
                integration_promotion_intents.c.intent_kind == "root",
                integration_promotion_intents.c.root_batch_id == integration_batches.c.id,
                integration_promotion_intents.c.root_candidate_revision
                == integration_batches.c.current_revision,
            )
            .exists()
        )
        statement = (
            select(integration_batches.c.id, integration_batches.c.updated_at)
            .select_from(
                integration_batches.join(
                    integration_candidate_revisions,
                    and_(
                        integration_candidate_revisions.c.batch_id == integration_batches.c.id,
                        integration_candidate_revisions.c.revision
                        == integration_batches.c.current_revision,
                    ),
                )
                .join(
                    integration_repair_operations,
                    and_(
                        integration_repair_operations.c.batch_id == integration_batches.c.id,
                        integration_repair_operations.c.episode_id == integration_batches.c.id,
                    ),
                )
                .join(
                    integration_repair_stages,
                    and_(
                        integration_repair_stages.c.operation_id
                        == integration_repair_operations.c.id,
                        integration_repair_stages.c.ordinal
                        == integration_repair_operations.c.active_stage,
                    ),
                )
            )
            .where(
                integration_batches.c.lifecycle == "testing",
                integration_candidate_revisions.c.state == "green",
                integration_repair_operations.c.target_kind == "batch",
                integration_repair_operations.c.state.in_(("active", "escalated")),
                integration_repair_stages.c.state == "awaiting_completion",
                ~intent,
            )
            .order_by(integration_batches.c.updated_at, integration_batches.c.id)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(
                tuple_(integration_batches.c.updated_at, integration_batches.c.id)
                > tuple_(literal(after[0]), literal(after[1]))
            )
        async with self.db._engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(statement)).mappings().all()]

    async def reconcile(self, batch_id: str, *, now: float | None = None) -> dict[str, Any]:
        """Advance one batch at most one step; every result names its reason."""
        observed_at = self.clock() if now is None else now
        handoff = await self.repair.return_green_delegate_branch(
            batch_id, emit_continuation=True, now=observed_at
        )
        if handoff is not None:
            return {
                "outcome": "handed_off",
                "batch_id": batch_id,
                "reason": (
                    f"closed repair writer returned the branch to collector "
                    f"{handoff.owner_id} at fence {handoff.token}"
                ),
            }
        async with self.db.immediate() as conn:
            project_id = (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
            if project_id is None:
                return {"outcome": "stale", "batch_id": batch_id, "reason": "batch is gone"}
            await self.db.lock_hierarchy_project(conn, project_id)
            readiness = await self.promotion.readiness_on(conn, project_id, batch_id)
            if readiness["intent_exists"]:
                return {
                    "outcome": "promoting",
                    "batch_id": batch_id,
                    "reason": "a root promotion intent already owns this revision",
                }
            if readiness["blocker"] is not None:
                outcome, reason = readiness["blocker"]
                return {
                    "outcome": "blocked",
                    "batch_id": batch_id,
                    "promotion_outcome": outcome,
                    "reason": reason,
                }
            state = readiness["state"]
            revision = int(readiness["revision"])
            candidate = state["revision"]
            delivered = await self._green_deliveries_on(conn, project_id, batch_id, revision)
            if any(row["delivered_at"] is None for row in delivered):
                return {
                    "outcome": "pending",
                    "batch_id": batch_id,
                    "reason": "a green continuation is still queued for delivery",
                }
            if not delivered:
                # CI persists green before it publishes the attestation and
                # emits the fact; that publisher owns the first wakeup.
                return {
                    "outcome": "pending",
                    "batch_id": batch_id,
                    "reason": "no green fact has been delivered for this revision yet",
                }
            latest_delivery = max(float(row["delivered_at"]) for row in delivered)
            if observed_at < latest_delivery + DELIVERY_GRACE_SECONDS:
                return {
                    "outcome": "pending",
                    "batch_id": batch_id,
                    "reason": "the latest green fact is still within its delivery grace",
                }
            fingerprint = promotion_fingerprint(
                operation_id=state["operation"]["id"],
                batch_id=batch_id,
                revision=revision,
                head_sha=candidate["head_sha"],
                evidence_id=candidate["ci_evidence_id"],
                owner=state["owner"],
                lease=state["lease"],
            )
            emitted = await continuation_rows_on(
                conn,
                project_id=project_id,
                batch_id=batch_id,
                revision=revision,
                fingerprint=fingerprint,
            )
            generation = len(emitted)
            if emitted:
                # ``available_at`` is the emitter's service clock, like ``now``.
                due = float(emitted[-1]["available_at"]) + continuation_delay(generation)
                if observed_at < due:
                    return {
                        "outcome": "backoff",
                        "batch_id": batch_id,
                        "generation": generation,
                        "reason": (
                            f"{generation} promotion continuations for this exact authority "
                            f"have not promoted; the next is due at {due:.0f}"
                        ),
                    }
            event_id = await enqueue_green_continuation_on(
                conn,
                project_id=project_id,
                operation_id=state["operation"]["id"],
                batch_id=batch_id,
                revision=revision,
                head_sha=candidate["head_sha"],
                fingerprint=fingerprint,
                generation=generation,
                now=observed_at,
            )
        return {
            "outcome": "continued",
            "batch_id": batch_id,
            "event_id": event_id,
            "generation": generation,
            "reason": "exact green candidate is promotable and has no promotion intent",
        }

    @staticmethod
    async def _green_deliveries_on(conn, project_id, batch_id, revision):
        rows = (
            await conn.execute(
                select(integration_outbox.c.id, integration_outbox.c.delivered_at).where(
                    integration_outbox.c.project_id == project_id,
                    integration_outbox.c.event_type == GREEN_EVENT_TYPE,
                    integration_outbox.c.payload["batch_id"].as_string() == batch_id,
                    integration_outbox.c.payload["revision"].as_integer() == int(revision),
                )
            )
        ).mappings().all()
        return [dict(row) for row in rows]

    def _report(self, batch_id: str, result: dict[str, Any]) -> None:
        """Log a batch's continuation state once per change, not once per tick."""
        summary = f"{result.get('outcome')}: {result.get('reason')}"
        if self._reported.get(batch_id) == summary:
            return
        self._reported[batch_id] = summary
        level = (
            logging.WARNING
            if int(result.get("generation") or 0) >= WARN_AFTER_GENERATIONS
            else logging.INFO
        )
        logger.log(level, "green root batch %s promotion continuation %s", batch_id, summary)
