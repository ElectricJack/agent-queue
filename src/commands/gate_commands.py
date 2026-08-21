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
        try:
            gate_id = await self.db.create_gate(
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

        return {"success": True, "gate_id": gate_id, "gate": payload}

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
        """Resolve a gate (idempotent) and report unblocked waiters."""
        gate_id = args.get("gate_id")
        if not gate_id:
            return {"success": False, "error": "gate_id is required"}
        resolved_by = args.get("resolved_by")
        if not resolved_by:
            return {"success": False, "error": "resolved_by is required"}

        gate = await self.db.get_gate(str(gate_id))
        if gate is None:
            return {"success": False, "error": f"gate '{gate_id}' not found"}

        flipped = await self.db.resolve_gate(
            str(gate_id),
            resolved_by=str(resolved_by),
            resolution=str(args.get("resolution") or ""),
        )

        payload = {
            "gate_id": str(gate_id),
            "project_id": gate["project_id"],
            "resolved_by": str(resolved_by),
            "resolution": str(args.get("resolution") or ""),
            "unblocked_task_ids": sorted(flipped),
            "gate_type": gate["gate_type"],
        }
        try:
            await self.orchestrator.bus.emit("gate.resolved", payload)
        except Exception:
            logger.debug("gate_resolve: bus emit failed", exc_info=True)
        try:
            await self.db.log_event(
                "gate.resolved",
                project_id=gate["project_id"],
                payload=str(gate_id),
            )
        except Exception:
            logger.debug("gate_resolve: log_event failed", exc_info=True)

        return {
            "success": True,
            "gate_id": str(gate_id),
            "unblocked_task_ids": sorted(flipped),
        }
