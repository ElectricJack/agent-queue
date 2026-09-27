"""Provider allocation commands mixin for CommandHandler.

The operator surface of provider-level worker allocation
(``projects/agent-queue/specs/provider-worker-allocation-controls.md``).
``provider_allocation_status`` is the read: every ordinary worker profile
grouped by harness provider key, with pool supply, live sessions, explicit
pins, manual agents, project preferences and the provider-wide configured
ceiling.  ``provider_allocation_preview`` answers what one allocation request
would do, with the token apply consumes.  The snapshot, its redaction and the
pure preview live in :mod:`src.providers.allocation`; this mixin only resolves
the caller's scope.
"""

from __future__ import annotations

from typing import Any


class ProviderAllocationCommandsMixin:
    """Mixin that adds provider allocation reads and previews to CommandHandler."""

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

    def _allocation_worker_refusal(self, command: str) -> dict | None:
        """Refuse a task-scoped worker token before it learns anything about the request.

        The HTTP scope layer already refuses it (the command is not in
        ``AGENT_COMMAND_SET``); this repeats the rule for any other caller.
        """
        scope = self._current_scope or {}
        if scope.get("kind") == "session" and not scope.get("elevated"):
            return {
                "success": False,
                "error": f"out of scope: {command} requires an operator or supervisor",
            }
        return None

    def _allocation_scope_refusal(self, request: dict) -> dict | None:
        """Refuse *request* unless the caller holds the scope it needs (spec §Authorization).

        The local operator and the global admin may preview anything.  A
        per-project supervisor is a project admin: it may change only its own
        project's preference, never a global profile, and never interrupt busy
        work.  Worker tokens are :meth:`_allocation_worker_refusal`'s.
        """
        from src.providers.allocation import OPERATOR_SCOPE, allocation_scope

        scoped, project = self._allocation_scope_project()
        if not scoped:
            return None
        if allocation_scope(request) == OPERATOR_SCOPE:
            if request["drain"] == "interrupt-busy":
                reason = "drain interrupt-busy requires operator scope"
            else:
                reason = (
                    "a lifecycle or bounds change edits global profiles and requires "
                    "operator scope"
                )
            return {"success": False, "error": f"out of scope: {reason}"}
        receive = request["receive_new_work"]
        if receive is None or project is None or receive["project_id"] != project:
            return {
                "success": False,
                "error": "out of scope: a project-scoped caller may change only its own "
                "project's preference",
            }
        return None

    async def _cmd_provider_allocation_preview(self, args: dict) -> dict[str, Any]:
        """What one provider allocation request would change, and its preview token.

        Read-only: nothing is written.  Apply consumes the token and refuses it
        once anything the preview observed has changed.

        Args:
            provider: A provider key (``codex``) or vendor (``openai``).
            profile_ids: Narrow the selection to these ordinary worker
                profiles of the provider; omitted or null selects all of them.
                A profile of another provider, a control profile or an empty
                list is refused.
            participation: ``pool`` or ``task`` -- the lifecycle every
                selected profile gets.
            bounds: ``{"min": N, "max": N | null}`` per selected pool
                profile, validated as ``aq pool scale`` validates;
                ``max: null`` (or ``"unbounded"``) removes the ceiling.
            receive_new_work: ``{"project_id": ..., "mode": "prefer" |
                "clear"}`` -- the project's preferred provider for unpinned work.
            drain: ``graceful`` (default), ``idle-now`` or ``interrupt-busy``.
            allow_pinned_wait: Acknowledge pinned READY work left on profiles
                leaving the pool.

        Returns:
            The canonical ``request``, ``required_scope``, before and after
            ``profiles``, the provider ``ceiling`` before and after,
            ``project_limits``, ``sessions`` with their action, the ``busy``
            set, ``pinned`` tasks, ``manual_agents``, the ``preference``
            change, ``warnings``, ``blocked`` and ``preview_token``.  A lifecycle
            or bounds change and ``interrupt-busy`` need operator scope; a
            preference-only change needs project-admin scope for that project.
        """
        from src.providers.allocation import (
            AllocationRequestError,
            build_allocation_snapshot,
            normalize_allocation_request,
            plan_allocation_preview,
        )

        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "_measure_pools"):
            return {"success": False, "error": "orchestrator is not running"}
        refusal = self._allocation_worker_refusal("provider_allocation_preview")
        if refusal is not None:
            return refusal
        try:
            request = normalize_allocation_request(args)
        except AllocationRequestError as exc:
            return {"success": False, "error": str(exc)}
        refusal = self._allocation_scope_refusal(request)
        if refusal is not None:
            return refusal
        snapshot = await build_allocation_snapshot(orchestrator)
        return plan_allocation_preview(snapshot, request)
