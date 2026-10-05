"""Gate commands mixin — create, list, show, and resolve work-graph gates.

Implements docs/specs/implementation/work-graph.md §5.  Every method returns
a ``dict`` — domain data on success, ``{"success": False, "error": ...}``
on failure.  The gate mutation queries live in
:mod:`src.database.queries.gate_queries`; this module is the operator
surface and the audit-event emitter.
"""

from __future__ import annotations

import logging
import time

from src.reviews.gates import (
    REVIEW_GATE_WITHDRAWN_RESOLUTION,
    close_withdrawn_gate,
    retire_gate_escalation,
    review_gate_resolution_is_terminal,
)

logger = logging.getLogger(__name__)


class GateCommandsMixin:
    """Gate command methods mixed into ``CommandHandler``."""

    async def _cmd_gate_create(self, args: dict) -> dict:
        """Create a new gate; optionally attach it to waiter tasks.

        Required args: ``project_id``, ``gate_type``, ``title``.
        Optional args: ``question``, ``await_id``, ``timeout_at``,
        ``waiter_task_ids`` (list).
        """
        project_id = args.get("project_id")
        if not project_id:
            return {"success": False, "error": "project_id is required"}
        gate_type = args.get("gate_type")
        if not gate_type:
            return {"success": False, "error": "gate_type is required"}
        title = args.get("title")
        if not title:
            return {"success": False, "error": "title is required"}

        waiters = args.get("waiter_task_ids") or ()
        if isinstance(waiters, str):
            waiters = [waiters]

        # A ``routing`` gate means "this task is waiting for its router".
        # Only a route write resolves one (``task_route_apply`` or the
        # override), so a gate on a task that already has a profile is never
        # resolved: it sits open until the task finishes and expires as "all
        # waiters terminal", which reads as a task that ran unrouted when in
        # fact it was routed.
        if str(gate_type) == "routing" and waiters:
            unrouted = []
            for w in waiters:
                try:
                    t = await self.db.get_task(str(w))
                except Exception:
                    # Cannot tell — attach the gate. A spurious gate is a
                    # smaller problem than an unrouted task running silently.
                    unrouted.append(str(w))
                    continue
                if t is None or not (getattr(t, "profile_id", "") or "").strip():
                    unrouted.append(str(w))
            if not unrouted:
                logger.debug(
                    "gate_create: every waiter already has a profile — no routing gate"
                )
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "all waiter tasks are already routed",
                    "gate_id": None,
                    "created": False,
                }
            waiters = unrouted

        try:
            gate_id, was_created = await self.db.create_gate(
                project_id=str(project_id),
                gate_type=str(gate_type),
                title=str(title),
                question=str(args.get("question") or ""),
                await_id=args.get("await_id"),
                timeout_at=args.get("timeout_at"),
                waiter_task_ids=[str(w) for w in waiters],
                **({"unrouted_only": True} if str(gate_type) == "routing" and waiters else {}),
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("gate_create: could not create gate")
            return {"success": False, "error": str(exc)}

        if gate_id is None:
            return {
                "success": True, "skipped": True,
                "reason": "all waiter tasks are already routed",
                "gate_id": None, "created": False,
            }

        # Audit + bus emit — payload matches ``gate.created`` schema.
        payload = {
            "gate_id": gate_id,
            "gate_type": str(gate_type),
            "project_id": str(project_id),
            "title": str(title),
            "question": str(args.get("question") or ""),
            "await_id": args.get("await_id"),
            "timeout_at": args.get("timeout_at"),
            "waiter_task_ids": list(waiters),
        }
        # Only emit ``gate.created`` when a NEW gate was inserted; if
        # dedup returned an existing open gate, downstream subscribers
        # already saw the original create event.
        if was_created:
            await self._emit_gate_created(payload)

        return {
            "success": True,
            "gate_id": gate_id,
            "gate": payload,
            "was_created": was_created,
        }

    async def _emit_gate_created(self, payload: dict) -> None:
        """Publish only after the gate and blocked projection have committed."""
        try:
            await self.orchestrator.bus.emit("gate.created", payload)
        except Exception:
            logger.debug("gate_create: bus emit failed", exc_info=True)
        try:
            await self.db.log_event(
                "gate.created", project_id=payload["project_id"], payload=payload["gate_id"],
            )
        except Exception:
            logger.debug("gate_create: log_event failed", exc_info=True)

    async def _emit_admitted_routing_gates(self, task_id: str) -> None:
        """Initial task admission owns the event; pipeline dedup will not emit it."""
        for gate in await self.db.get_gates_for_task(task_id):
            if gate["gate_type"] != "routing":
                continue
            await self._emit_gate_created({
                "gate_id": gate["id"], "gate_type": gate["gate_type"],
                "project_id": gate["project_id"], "title": gate["title"],
                "question": gate["question"], "await_id": gate["await_id"],
                "timeout_at": gate["timeout_at"],
                "waiter_task_ids": sorted(await self.db.get_gate_waiters(gate["id"])),
            })

    async def _cmd_gate_list(self, args: dict) -> dict:
        """List gates, optionally filtered by project/status/type/task."""
        gates = await self.db.list_gates(
            project_id=args.get("project_id"),
            status=args.get("status"),
            gate_type=args.get("gate_type"),
            task_id=args.get("task_id"),
        )
        return {"success": True, "gates": gates}

    async def _cmd_gate_show(self, args: dict) -> dict:
        """Return one gate + its waiter task ids."""
        gate_id = args.get("gate_id")
        if not gate_id:
            return {"success": False, "error": "gate_id is required"}
        gate = await self.db.get_gate(str(gate_id))
        if gate is None:
            return {"success": False, "error": f"gate '{gate_id}' not found"}
        waiters = await self.db.get_gate_waiters(str(gate_id))
        return {"success": True, "gate": gate, "waiters": sorted(waiters)}

    async def _close_withdrawn_review_gate(
        self, gate_id: str, *, review_id: str, resolved_by: str
    ) -> dict:
        """Resolve a withdrawn review's gate ``withdrawn``, holding its waiters.

        The same domain write ``aq review withdraw`` makes
        (:func:`src.reviews.gates.close_withdrawn_gate`), reached from the
        operator's door for the rows the live path cannot reach — a gate
        orphaned before this existed, or one a direct database edit left open.
        Waiters are held, not released, for the same reason they are on the
        withdrawal path: the document was never approved.
        """
        now = time.time()
        closure = await close_withdrawn_gate(self.db, gate_id, resolved_by=resolved_by)
        retired = await retire_gate_escalation(
            self.db, gate_id, now=now, review_id=review_id,
            reason=f"{resolved_by} closed the gate of a withdrawn review",
        )
        await self._announce_withdrawn_gate(closure, retired, resolved_by=resolved_by)
        result = closure.resolution
        return {
            "success": True,
            "gate_id": gate_id,
            "review_id": review_id,
            "resolution": REVIEW_GATE_WITHDRAWN_RESOLUTION,
            "held_task_ids": list(closure.held_task_ids),
            "unblocked_task_ids": sorted(result.flipped) if result is not None else [],
            "retired_escalation_ids": [str(row["id"]) for row in retired],
        }

    async def _announce_withdrawn_gate(self, closure, retired, *, resolved_by: str) -> None:
        """Post-commit announcements for a withdrawn review's gate.

        The mirror of the orchestrator's ``_resolve_gate_and_emit`` for a write
        that already committed on the caller's transaction: the blocked flips
        and ready notifications, the ``gate.resolved`` bus + audit events, and
        one ``escalation.updated.v1`` per retired escalation so the Discord
        card and the dashboards collapse with the question it asked.
        """
        result = closure.resolution
        if result is None or not result.resolved:
            return
        await self.db.announce_gate_resolution(result)
        await self.orchestrator._announce_gate_resolved(
            closure.gate_id,
            resolved_by=resolved_by,
            resolution=REVIEW_GATE_WITHDRAWN_RESOLUTION,
            flipped=set(result.flipped),
        )
        for row in retired:
            await self._emit_escalation(
                "escalation.updated.v1",
                {
                    "escalation_id": row["id"],
                    "project_id": row["project_id"],
                    "task_id": row.get("task_id"),
                    "state": row["state"],
                    "revision": row["revision"],
                    "terminal_outcome": row.get("terminal_outcome"),
                },
            )

    async def _refuse_live_review_gate(self, gate: dict) -> dict | None:
        """``None`` when *gate*'s review can no longer decide; the refusal otherwise.

        A ``review`` gate is resolved by the review, not by the operator: while
        the review can still be approved (``in_review``,
        ``changes_requested``, and ``rejected`` — the author resubmits into
        ``in_review`` against this same gate) resolving it here would release
        its waiters onto a document nobody approved.  So the refusal names the
        command that does decide.  Once the review is terminal the gate is an
        orphan this command is the designated control for.
        """
        terminal, review_id = await review_gate_resolution_is_terminal(self.db, str(gate["id"]))
        if terminal:
            return None
        return {
            "success": False,
            "error_code": "review_gate",
            "error": (
                "review gates are decided in the review: "
                f"aq review decide --review-id {review_id or gate.get('await_id')} "
                "--revision <n> --decision approve"
            ),
        }

    async def _cmd_gate_resolve(self, args: dict) -> dict:
        """Resolve a gate (idempotent) and report unblocked waiters.

        Routes through the orchestrator's ``_resolve_gate_and_emit`` helper
        so the operator-driven path emits the same ``gate.resolved`` +
        ``task.blocked``/``task.unblocked`` bus events that the sweep path
        does.  Playbooks that subscribe to blocked-flip events fire whether
        the gate is resolved by ``aq gate resolve`` or by ``_sweep_gates``.

        A ``review`` gate is the one type the operator may only close once its
        review can no longer decide (``_refuse_live_review_gate``); anything
        else — ``routing``, an ``integration-subject:`` await — has its own
        narrower door below.
        """
        gate_id = args.get("gate_id")
        if not gate_id:
            return {"success": False, "error": "gate_id is required"}
        resolved_by = args.get("resolved_by")
        if not resolved_by:
            return {"success": False, "error": "resolved_by is required"}

        gate = await self.db.get_gate(str(gate_id))
        if gate is None:
            return {"success": False, "error": f"gate '{gate_id}' not found"}

        if gate["gate_type"] == "review":
            refusal = await self._refuse_live_review_gate(gate)
            if refusal is not None:
                return refusal
            # A review that can no longer decide (withdrawn, or approved with
            # its gate somehow still open) leaves an orphan nothing else can
            # close, so the operator gets the one control that does.  The
            # resolution is not the caller's to pick: the review's own state is
            # the authority on how its gate closes, so a withdrawal can never
            # be made to read as an approval by passing ``--resolution``.
            review_id = str(gate.get("await_id") or "")
            review = await self.db.get_review(review_id)
            if str((review or {}).get("state") or "") == "withdrawn":
                return await self._close_withdrawn_review_gate(
                    str(gate_id), review_id=review_id, resolved_by=str(resolved_by)
                )
            flipped = await self.orchestrator._resolve_gate_and_emit(
                str(gate_id), resolved_by=str(resolved_by), resolution="approved",
            )
            return {
                "success": True,
                "gate_id": str(gate_id),
                "review_id": review_id,
                "resolution": "approved",
                "unblocked_task_ids": sorted(flipped or set()),
            }

        # dv2 phase 1: ``routing`` gates carry a pinned cross-phase
        # contract — only a route write resolves them: the project's router
        # (``task_route_apply``) or the audited emergency override
        # (``task_route_override``, mandatory routing §7).  Refuse the
        # generic path so operators can't half-resolve a routing gate and
        # leave the task un-routed for the runner.
        if gate["gate_type"] == "routing":
            return {
                "success": False,
                "error": (
                    "routing gates resolve when the task is routed: by the project's "
                    "router (task_route_apply), or in an emergency by "
                    "`aq task route-override --task-id <id> --profile-id <profile> "
                    "--reason \"...\"`"
                ),
            }

        if str(gate.get("await_id") or "").startswith("integration-subject:"):
            # The human identity is server-owned. A generic resolve or an
            # agent-supplied resolved_by cannot create an integration approval.
            from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
            from src.integration.gates import GatePrimitives
            from src.integration.subjects import Subject

            principal = current_principal() or TRUSTED_LOCAL
            if principal.kind is not PrincipalKind.LOCAL:
                return {"success": False, "error": "a verified human operator is required"}
            subject_id = gate["await_id"].removeprefix("integration-subject:")
            row = await self.db.get_integration_subject(subject_id)
            if row is None:
                return {"success": False, "error": "integration subject is missing"}
            result = await GatePrimitives(self.db).answer(
                Subject.from_row(row),
                str(gate_id),
                choice=str(args.get("resolution") or ""),
                answered_by="human:local-operator",
                verified_human=True,
            )
            return {
                "success": not result.is_unknown,
                "gate_id": str(gate_id),
                "outcome": result.outcome,
                "reason": result.reason,
                "unblocked_task_ids": [],
            }

        # Shared helper on the orchestrator: resolves the gate, emits
        # ``gate.resolved`` + audit row, and — critically — calls
        # ``_emit_blocked_flips`` so ``task.unblocked`` fires on the bus.
        flipped = await self.orchestrator._resolve_gate_and_emit(
            str(gate_id),
            resolved_by=str(resolved_by),
            resolution=str(args.get("resolution") or ""),
        )

        return {
            "success": True,
            "gate_id": str(gate_id),
            "unblocked_task_ids": sorted(flipped or set()),
        }
