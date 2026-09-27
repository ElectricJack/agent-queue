"""Provider allocation commands mixin for CommandHandler.

The operator surface of provider-level worker allocation
(``projects/agent-queue/specs/provider-worker-allocation-controls.md``).
``provider_allocation_status`` is the read: every ordinary worker profile
grouped by harness provider key, with pool supply, live sessions, explicit
pins, manual agents, project preferences and the provider-wide configured
ceiling.  The snapshot and its redaction live in
:mod:`src.providers.allocation`; this mixin only resolves the caller's scope.
"""

from __future__ import annotations

from typing import Any


class ProviderAllocationCommandsMixin:
    """Mixin that adds provider allocation reads to CommandHandler."""

    def _allocation_scope_project(self) -> tuple[bool, str | None]:
        """``(scoped, project)``: whether the caller is limited to one project.

        A local operator and a global supervisor (elevated, no project) see
        every project.  Any other scope is limited to its own project, and a
        scope carrying none sees no project's detail at all.
        """
        scope = self._current_scope or {}
        if not scope or scope.get("kind") == "local":
            return False, None
        if scope.get("elevated") and scope.get("project_id") is None:
            return False, None
        return True, scope.get("project_id")

    async def _cmd_provider_allocation_status(self, args: dict) -> dict[str, Any]:
        """Every ordinary worker profile grouped by provider, with what runs on it.

        Args:
            project_id: Narrow the per-project detail (project rows, sessions,
                task ids) to one project.  Fleet-wide counts, bounds and the
                ceiling are unchanged.  A project-scoped caller is always
                narrowed to its own project and may not name another.
            provider: Limit to one provider key (``codex``) or vendor
                (``openai``).

        Returns:
            ``providers`` (each with ``profiles``, ``supply``, ``ceiling``,
            ``manual_agents``, pin counts and ``last_allocation``),
            ``projects`` with each routing preference, ``diagnostics`` for
            every profile bulk control never selects, and ``redacted`` when
            the caller's scope hid other projects.
        """
        from src.providers.allocation import (
            build_allocation_snapshot,
            resolve_provider,
            status_view,
        )

        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "_measure_pools"):
            return {"success": False, "error": "orchestrator is not running"}
        view = str(args.get("project_id") or "").strip() or None
        scoped, scope_project = self._allocation_scope_project()
        if scoped and view is not None and view != scope_project:
            return {
                "success": False,
                "error": "out of scope: provider_allocation_status reads only the caller's "
                "project",
            }
        if view is not None and await self.db.get_project(view) is None:
            return {"success": False, "error": f"Project '{view}' not found"}
        if scoped:
            view = scope_project
            visible = frozenset({scope_project}) if scope_project else frozenset()
        else:
            visible = frozenset({view}) if view else None

        snapshot = await build_allocation_snapshot(orchestrator)
        provider = None
        if args.get("provider"):
            provider = resolve_provider(snapshot, args["provider"])
            if provider is None:
                known = ", ".join(row["provider"] for row in snapshot["providers"]) or "none"
                return {
                    "success": False,
                    "error": f"unknown provider {args['provider']!r}; known providers: {known}",
                }
        return status_view(
            snapshot,
            visible_projects=visible,
            provider=provider,
            project_id=view,
            redacted=scoped,
        )
