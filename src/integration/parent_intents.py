"""Advance parent delivery intents that a lost or refused event left unresolved.

A child's delivery into its parent branch is one promotion intent
(``intent_kind = 'child'``): reserved, prepared (the exact merge commit built
and pinned), pushed, committed.  A target has at most one unresolved intent and
collection queues no further delivery while one exists, so an intent whose
``delivery.ready`` run ended on ``target_moved`` because the collector's fence
changed, or whose push outcome was lost, stops the whole parent.  Before this
pass only a hand replay of that same event moved it (stop point B7).

On each visit, after a grace for the run that may still be in flight:

1. ``integration_reconcile_promotion`` finalizes an intent whose exact prepared
   commit (or reserved resolution) is already on the target.
2. A ``prepared`` intent whose push has not been applied is replayed through
   ``delivery_promote`` with its own frozen identity and the *current*
   reserved collector fence: the same expected-old push of the same prepared
   commit the original run would have made.  A target held by any other
   writer is a wait, and an intent whose collection operation has ended or
   whose parent an operator paused is left alone.
3. A diverged, unapplied attempt is superseded under that same fence after
   fresh read-back. A ``reserved`` intent emits a durable ``delivery.ready``
   continuation so its playbook can rebuild it and own any conflict repair.

Every call goes through the command handler as a service principal, which
re-derives authority, fences and Git state.  Visits back off per intent.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from src.database.tables import integration_outbox, integration_repair_operations, task_metadata
from src.integration.green_continuation import continuation_delay
from src.integration.models import BranchKey
from src.integration.outbox import enqueue_integration_event
from src.integration.ownership import BranchOwnership
from src.integration.service import RetryBackoff

logger = logging.getLogger(__name__)

#: An intent younger than this may still be inside its own ``delivery.ready`` run.
GRACE_SECONDS = 60.0
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 600.0
#: Backoff entries for intents not visited for this long are forgotten.
FORGET_AFTER_SECONDS = 86_400.0

_SETTLED_STATES = frozenset({"committed", "conflict", "superseded"})
#: Outcomes that are progress or an expected wait; anything else is logged as a warning.
_QUIET_OUTCOMES = frozenset({
    "applied", "promoted", "already_promoted", "settled", "waiting", "continued", "superseded",
})


async def enqueue_parent_continuation_on(conn, *, intent, fence, now) -> bool:
    """Pace wakeups durably under the caller's collector and intent locks."""
    prefix = f"parent-intent:{intent['id']}:{fence.owner_id}:{fence.token}:"
    emitted = (await conn.execute(
        select(integration_outbox).where(
            integration_outbox.c.dedup_key.startswith(prefix, autoescape=True),
        ).order_by(integration_outbox.c.available_at, integration_outbox.c.id)
    )).mappings().all()
    if any(row["delivered_at"] is None for row in emitted):
        return False
    if emitted:
        last = emitted[-1]
        due = max(
            float(last["available_at"]) + continuation_delay(len(emitted)),
            float(last["delivered_at"]) + GRACE_SECONDS,
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


class ParentIntentReconciler:
    """Re-drive one unresolved parent delivery intent per visit, at most one step."""

    def __init__(
        self,
        db: Any,
        *,
        commands: Callable[[], Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self._commands = commands
        self.clock = clock
        self._backoff = RetryBackoff(base=RETRY_BASE_SECONDS, ceiling=RETRY_MAX_SECONDS)
        self._reported: dict[str, str] = {}
        self._pruned_at = 0.0

    async def reconcile(self, row: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        observed_at = self.clock() if now is None else now
        intent_id = row["id"]
        if row.get("intent_kind") == "root":
            return self._result("declined", intent_id, "root intents promote main elsewhere")
        updated_at = float(row.get("updated_at") or 0.0)
        if observed_at < updated_at + GRACE_SECONDS:
            return self._result("pending", intent_id, "the intent's own run may still be active")
        if not self._backoff.due(intent_id, (row.get("state"), updated_at), observed_at):
            return self._result("backoff", intent_id, "the previous visit was refused recently")
        handler = self._commands()
        if handler is None:
            return self._result("not_ready", intent_id, "the command handler is not wired yet")
        result = await self._advance(handler, intent_id)
        self._backoff.record(intent_id, observed_at, result["outcome"])
        self._report(intent_id, result)
        if observed_at - self._pruned_at > 3600.0:
            self._pruned_at = observed_at
            self._backoff.forget_idle(before=observed_at - FORGET_AFTER_SECONDS)
            self._reported = {
                key: value for key, value in self._reported.items() if key in self._backoff
            }
        return result

    async def _advance(self, handler: Any, intent_id: str) -> dict[str, Any]:
        from src.commands.principal import ExecutionPrincipal, principal_context

        intent = await self.db.get_integration_promotion_intent(intent_id)
        if intent is None or intent["state"] in _SETTLED_STATES:
            return self._result("settled", intent_id, "the intent is no longer unresolved")
        state = intent["state"]
        with principal_context(ExecutionPrincipal.service("integration-parent-intent")):
            reconciled = await handler._cmd_integration_reconcile_promotion(
                {"intent_id": intent_id}
            )
            if reconciled.get("success"):
                return self._result(
                    reconciled.get("outcome", "applied"), intent_id,
                    "promotion reconciliation advanced the intent",
                )
            if state == "resolution_reserved" and reconciled.get("outcome") == "not_applied":
                return self._result(
                    "waiting", intent_id, "its repair writer has not pushed the resolution yet"
                )
            if state not in {"reserved", "prepared"} or reconciled.get("outcome") not in {
                "not_applied", "invariant_error",
            }:
                return self._result(
                    reconciled.get("outcome") or "error",
                    intent_id,
                    reconciled.get("error") or "promotion reconciliation refused",
                )
            hold = await self._hold_reason(intent)
            if hold is not None:
                return self._result("held", intent_id, hold)
            target = BranchKey(
                repository_id=intent["repository_id"], branch=intent["target_branch"]
            )
            owner = await BranchOwnership(self.db).get_owner(target)
            if (
                owner is None
                or owner["owner_role"] != "collector"
                or owner["handoff_state"] != "reserved"
            ):
                holder = (
                    "no owner"
                    if owner is None
                    else f"{owner['owner_id']} ({owner['owner_role']}, {owner['handoff_state']})"
                )
                return self._result(
                    "busy", intent_id, f"{target.branch} is held by {holder}, not its collector"
                )
            if state == "reserved" or reconciled.get("outcome") == "invariant_error":
                recovered = await handler._cmd_integration_reconcile_promotion({
                    "intent_id": intent_id,
                    "fence": {
                        "target": target.model_dump(mode="json"),
                        "owner_id": owner["owner_id"],
                        "token": int(owner["fence_token"]),
                    },
                })
                return self._result(
                    recovered.get("outcome") or "error", intent_id,
                    recovered.get("error") or "recovered under the current collector fence",
                )
            promoted = await handler._cmd_delivery_promote(
                {
                    "operation_key": intent["operation_key"],
                    "source_task_id": intent["source_task_id"],
                    "source_head": intent["source_head"],
                    "source_base": intent["source_base"],
                    "expected_target": intent["expected_target"],
                    "fence": {
                        "target": target.model_dump(mode="json"),
                        "owner_id": owner["owner_id"],
                        "token": int(owner["fence_token"]),
                    },
                }
            )
        return self._result(
            promoted.get("outcome") or "error",
            intent_id,
            promoted.get("error")
            or f"replayed delivery under collector fence {owner['fence_token']}",
        )

    async def _hold_reason(self, intent: dict[str, Any]) -> str | None:
        """Name what keeps a replay from writing: an ended operation or an operator pause."""
        async with self.db._engine.connect() as conn:
            operation_state = (
                await conn.execute(
                    select(integration_repair_operations.c.state).where(
                        integration_repair_operations.c.id == intent["operation_key"]
                    )
                )
            ).scalar_one_or_none()
            paused = (
                await conn.execute(
                    select(task_metadata.c.task_id).where(
                        task_metadata.c.task_id == intent["target_task_id"],
                        task_metadata.c.key == "manual_pause",
                    )
                )
            ).first()
        if operation_state not in {"active", "escalated"}:
            return f"collection operation {intent['operation_key']} is {operation_state}"
        if paused is not None:
            return f"parent {intent['target_task_id']} is paused by an operator"
        return None

    @staticmethod
    def _result(outcome: str, intent_id: str, reason: str) -> dict[str, Any]:
        return {"outcome": outcome, "intent_id": intent_id, "reason": reason}

    def _report(self, intent_id: str, result: dict[str, Any]) -> None:
        """Log an intent's state once per change, not once per visit."""
        summary = f"{result['outcome']}: {result['reason']}"
        if self._reported.get(intent_id) == summary:
            return
        self._reported[intent_id] = summary
        level = logging.INFO if result["outcome"] in _QUIET_OUTCOMES else logging.WARNING
        logger.log(level, "parent delivery intent %s %s", intent_id, summary)
