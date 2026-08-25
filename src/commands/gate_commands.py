"""Gate commands mixin — create, list, show, and resolve work-graph gates.

Implements docs/specs/implementation/work-graph.md §5.  Every method returns
a ``dict`` — domain data on success, ``{"success": False, "error": ...}``
on failure.  The gate mutation queries live in
:mod:`src.database.queries.gate_queries`; this module is the operator
surface and the audit-event emitter.
"""

from __future__ import annotations

import logging

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

        # A ``routing`` gate means "this task still needs a profile". Attaching
        # one to a task that already has a profile creates work that cannot be
        # done: the default pipeline then ensures a triage task, an agent
        # starts, finds nothing unrouted, and closes — once per created task.
        # The gate itself is never resolved either; it sits open until the task
        # finishes and it expires as "all waiters terminal", which reads as a
        # task that ran unrouted when in fact it was routed at creation.
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
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("gate_create: could not create gate")
            return {"success": False, "error": str(exc)}

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
            try:
                await self.orchestrator.bus.emit("gate.created", payload)
            except Exception:
                logger.debug("gate_create: bus emit failed", exc_info=True)
            try:
                await self.db.log_event(
                    "gate.created",
                    project_id=str(project_id),
                    payload=gate_id,
                )
            except Exception:
                logger.debug("gate_create: log_event failed", exc_info=True)

        return {
            "success": True,
            "gate_id": gate_id,
            "gate": payload,
            "was_created": was_created,
        }

    async def _cmd_gate_list(self, args: dict) -> dict:
        """List gates, optionally filtered by project/status/type."""
        gates = await self.db.list_gates(
            project_id=args.get("project_id"),
            status=args.get("status"),
            gate_type=args.get("gate_type"),
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

    async def _cmd_gate_resolve(self, args: dict) -> dict:
        """Resolve a gate (idempotent) and report unblocked waiters.

        Routes through the orchestrator's ``_resolve_gate_and_emit`` helper
        so the operator-driven path emits the same ``gate.resolved`` +
        ``task.blocked``/``task.unblocked`` bus events that the sweep path
        does.  Playbooks that subscribe to blocked-flip events fire whether
        the gate is resolved by ``aq gate resolve`` or by ``_sweep_gates``.
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

        # dv2 phase 1: ``routing`` gates carry a pinned cross-phase
        # contract — ONLY ``task_route`` writes the profile/class/workspace
        # fields on the task and then resolves the gate.  Refuse the
        # generic path so operators can't half-resolve a routing gate and
        # leave the task un-routed for the runner.
        if gate["gate_type"] == "routing":
            return {
                "success": False,
                "error": (
                    "routing gates can only be resolved via task_route; "
                    "call task_route(task_id, profile_id, ...) instead"
                ),
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
