"""Daemon-only collaboration expiry and retention command boundary."""

from __future__ import annotations

import time

from src.commands.principal import (
    ExecutionPrincipal,
    PrincipalKind,
    current_principal,
    principal_context,
)


class CollaborationLifecycleMixin:
    async def _cmd_reconcile_collaborations(self, args):
        principal = current_principal()
        if principal is None or principal.kind != PrincipalKind.SERVICE:
            return {
                "success": False,
                "error_code": "collaboration.out_of_scope",
                "error": "only the daemon may reconcile collaborations",
            }
        now = args.get("now")
        result = await self.db.reconcile_collaborations(now=time.time() if now is None else now)
        return {"success": True, **result}


class CollaborationReconciler:
    """Use the command handler with a server-derived service principal."""

    def __init__(self, handler):
        self.handler = handler

    async def tick(self, *, now: float | None = None) -> dict:
        with principal_context(ExecutionPrincipal.service("collaborations")):
            return await self.handler.execute("reconcile_collaborations", {"now": now})
