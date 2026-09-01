"""Command surface for authenticated mandatory triage."""

from __future__ import annotations

from src.triage.models import RoutingChoice


class TriageCommandsMixin:
    async def _triage_principal(self):
        return await self._triage_service.authenticate(self._authenticated_request_scope)

    async def _cmd_triage_options(self, args: dict) -> dict:
        principal = await self._triage_principal()
        if isinstance(principal, dict):
            return principal
        return await self._triage_service.options(principal)

    async def _cmd_task_route(self, args: dict) -> dict:
        principal = await self._triage_principal()
        if isinstance(principal, dict):
            return principal
        if "execution_type_key" not in args and "profile_id" in args:
            return {
                "success": False,
                "code": "migration_required",
                "error": (
                    "profile-only routing is no longer supported; call triage_options and send "
                    "execution_type_key, expected_revision, and reason"
                ),
            }
        choice = RoutingChoice(
            task_id=args.get("task_id"),
            execution_type_key=args.get("execution_type_key"),
            expected_revision=args.get("expected_revision"),
            reason=args.get("reason"),
        )
        return await self._triage_service.complete(principal, choice)

    async def _cmd_triage_defer(self, args: dict) -> dict:
        principal = await self._triage_principal()
        if isinstance(principal, dict):
            return principal
        return await self._triage_service.defer(
            principal,
            args.get("task_id"),
            args.get("expected_revision"),
            args.get("reason"),
        )
