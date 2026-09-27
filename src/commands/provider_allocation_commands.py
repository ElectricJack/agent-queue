"""Provider allocation commands mixin for CommandHandler.

The operator surface of provider-level worker allocation
(``projects/agent-queue/specs/provider-worker-allocation-controls.md``).
``provider_allocation_status`` is the read: every ordinary worker profile
grouped by harness provider key, with pool supply, live sessions, explicit
pins, manual agents, project preferences and the provider-wide configured
ceiling.  ``provider_allocation_preview`` answers what one allocation request
would do, with the token apply consumes.  ``provider_allocation_apply``
carries out a reviewed preview: the profile edits through the pool-admin
helpers with compensation, the project preference, the drain and one
``provider.allocation_changed`` audit event.  The snapshot, its redaction, the
pure preview and the issued-token registry live in
:mod:`src.providers.allocation`; this mixin resolves the caller's scope and
sequences the writes.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class ProviderAllocationCommandsMixin:
    """Mixin that adds provider allocation status, preview and apply to CommandHandler."""

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
            preview_registry,
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
        preview = plan_allocation_preview(snapshot, request)
        if preview.get("success"):
            preview_registry(orchestrator).issue(preview["preview_token"], preview["request"])
        return preview

    async def _cmd_provider_allocation_apply(self, args: dict) -> dict[str, Any]:
        """Apply one reviewed provider allocation preview (spec §Apply algorithm).

        Takes only the token of a preview this daemon issued, never a
        request: the request is the one the token was issued for.  Apply
        rebuilds that preview and refuses ``preview_stale`` -- returning the
        fresh preview, whose token is then appliable -- once anything the
        preview observed has changed, so the applied set is always the
        previewed set.

        Profile edits run through the pool-admin helpers behind ``aq pool
        set-lifecycle`` and ``aq pool scale``, in profile-id order, with the
        allocation ``request_id`` on every ``pool.*`` event; a failure
        compensates every earlier edit and is reported, never summarized as
        success.  Then the project preference (the queued ``class_only``
        READY tasks move to the preferred provider's equivalent rung through
        the operator re-route path), then the drain: ``graceful`` marks
        stopped and lets busy work finish, ``idle-now`` also terminates idle
        workers now, ``interrupt-busy`` also interrupts exactly the
        authorized busy set.  One ``provider.allocation_changed`` event
        records it all.

        Args:
            preview_token: The token ``provider_allocation_preview`` returned.
            authorize_busy_interrupt: For ``drain: interrupt-busy``, exactly
                the preview's busy set -- session ids, or the ids of the tasks
                they run.  Refused for any other drain.
            allow_pinned_wait: Acknowledge pinned READY work left on profiles
                leaving the pool, if the preview did not already.

        Returns:
            ``status`` ``applied`` (``success``), ``rolled_back`` or
            ``partial``, the ``request_id``, ``profiles`` rows (``applied``,
            ``failed``, ``rolled_back``, ``rollback_failed`` or ``skipped``,
            each with before and after), the ``preference`` change and
            re-placed tasks, ``session_actions``, ``pinned`` tasks,
            ``manual_agents`` warnings and the audit ``event_id``.  A refusal
            carries ``error_code`` (``preview_unknown``, ``preview_stale``,
            ``pinned_wait_unacknowledged``, ``busy_authorization_*``) and,
            where one exists, the current ``preview``.
        """
        from src.providers.allocation import (
            build_allocation_snapshot,
            busy_authorization_error,
            plan_allocation_preview,
            preview_registry,
        )

        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "_measure_pools"):
            return {"success": False, "error": "orchestrator is not running"}
        refusal = self._allocation_worker_refusal("provider_allocation_apply")
        if refusal is not None:
            return refusal
        token = str(args.get("preview_token") or "").strip()
        if not token:
            return {"success": False, "error": "preview_token is required"}
        allow = args.get("allow_pinned_wait")
        if allow is not None and not isinstance(allow, bool):
            return {"success": False, "error": "allow_pinned_wait must be a boolean"}
        authorized = args.get("authorize_busy_interrupt")
        if isinstance(authorized, str):
            authorized = authorized.replace(",", " ").split()
        elif authorized is not None and not isinstance(authorized, list | tuple):
            return {
                "success": False,
                "error": "authorize_busy_interrupt must be a list of session or task ids",
            }

        registry = preview_registry(orchestrator)
        async with registry.lock:
            request = registry.lookup(token)
            if request is None:
                return {
                    "success": False,
                    "error_code": "preview_unknown",
                    "error": "preview token is unknown or expired (tokens are single-use and "
                    "last until the daemon restarts); run provider_allocation_preview again",
                }
            refusal = self._allocation_scope_refusal(request)
            if refusal is not None:
                return refusal
            snapshot = await build_allocation_snapshot(orchestrator)
            preview = plan_allocation_preview(snapshot, request)
            if not preview.get("success"):
                registry.consume(token)
                return {
                    "success": False,
                    "error_code": "preview_stale",
                    "error": f"preview_stale: the previewed request no longer applies: "
                    f"{preview.get('error')}",
                    "preview": None,
                }
            if preview["preview_token"] != token:
                registry.consume(token)
                registry.issue(preview["preview_token"], preview["request"])
                return {
                    "success": False,
                    "error_code": "preview_stale",
                    "error": "preview_stale: the fleet changed since the preview; review the "
                    "fresh preview and apply its token",
                    "preview": preview,
                }
            waiting = [
                warning for warning in preview["warnings"]
                if warning["blocking"] and not (warning["acknowledged"] or allow)
            ]
            if waiting:
                return {
                    "success": False,
                    "error_code": "pinned_wait_unacknowledged",
                    "error": "; ".join(warning["message"] for warning in waiting),
                    "preview": preview,
                }
            busy_error = busy_authorization_error(preview, authorized)
            if busy_error is not None:
                code, message = busy_error
                return {
                    "success": False, "error_code": code, "error": message, "preview": preview,
                }
            registry.consume(token)
            return await self._apply_allocation(orchestrator, preview)

    async def _apply_allocation(self, orchestrator, preview: dict) -> dict[str, Any]:
        """Carry out a verified *preview*: profiles, preference, drains, one audit event."""
        from src.providers.allocation import ALLOCATION_EVENT

        request = preview["request"]
        request_id = f"alloc-{uuid.uuid4().hex[:12]}"
        correlation = {"request_id": request_id}
        actor = self._provider_actor()
        session_actions: list[dict[str, Any]] = []
        warnings = list(preview["warnings"])
        error: str | None = None
        status = "applied"

        # 5. Profiles, in profile-id order (the preview's order), through the helpers.
        rows: list[dict[str, Any]] = []
        failed = False
        for planned in (row for row in preview["profiles"] if row["changed"]):
            entry: dict[str, Any] = {
                "profile_id": planned["profile_id"],
                "status": "skipped",
                "changed_fields": planned["changed_fields"],
                "before": planned["before"],
                "after": planned["after"],
            }
            rows.append(entry)
            if failed:
                continue
            outcome = await self._allocate_profile(planned, request, correlation)
            entry["_restore"] = outcome["restore"]
            session_actions.extend(outcome["session_actions"])
            if outcome["success"]:
                entry["status"] = "applied"
                continue
            entry["status"] = "failed"
            entry["error"] = outcome["error"]
            error = f"{planned['profile_id']}: {outcome['error']}"
            failed = True

        # 6. The project preference, and the queued work it re-places.
        preference = None
        if not failed and preview["preference"] is not None:
            preference = dict(preview["preference"])
            try:
                if preference["changed"]:
                    await self.db.update_project(
                        preference["project_id"], preferred_provider=preference["after"]
                    )
                preference["applied"] = True
            except Exception as exc:  # compensated below, like a profile failure
                logger.warning("provider allocation %s: preference write failed", request_id,
                               exc_info=True)
                preference["applied"] = False
                error = f"project preference: {type(exc).__name__}: {exc}"
                failed = True
            if not failed and preference["mode"] == "prefer":
                preference["placement"] = await self._place_preferred_work(
                    orchestrator, preference["project_id"], preview["provider"], actor
                )
                if preference["placement"]["errors"]:
                    # The preference stands; a move that could not be made is reported.
                    status = "partial"
                    error = "re-placing queued work: " + "; ".join(
                        preference["placement"]["errors"]
                    )

        if failed:
            compensated = await self._compensate(rows, correlation)
            status = "rolled_back" if compensated else "partial"
        else:
            # 7. Drains beyond what the helpers did (spec §Drain semantics).
            drained, drain_errors = await self._drain_allocation(orchestrator, preview,
                                                                 correlation)
            session_actions.extend(drained)
            if drain_errors:
                status = "partial"
                error = "; ".join([*([error] if error else []), *drain_errors])
        for entry in rows:
            entry.pop("_restore", None)

        # 8. One durable audit event.
        payload = {
            "provider": preview["provider"],
            "vendor": preview.get("vendor") or "",
            "request_id": request_id,
            "actor": actor,
            "status": status,
            "preview_token": preview["preview_token"],
            "request": request,
            "profiles": rows,
            "ceiling": preview["ceiling"],
            "preference": preference,
            "session_actions": session_actions,
            "pinned": preview["pinned"],
            "manual_agents": preview["manual_agents"],
            "warnings": warnings,
            "error": error,
        }
        event_id = None
        try:
            event_id = await self.db.log_event(
                ALLOCATION_EVENT,
                project_id=preference["project_id"] if preference else None,
                payload=json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
            )
        except Exception:
            logger.warning("provider allocation %s: audit event not recorded", request_id,
                           exc_info=True)
            warnings.append(
                {"code": "audit_unrecorded", "blocking": False, "acknowledged": False,
                 "message": "the provider.allocation_changed event could not be written",
                 "subjects": [request_id]}
            )
        # The bus stamps its own delivery keys on what it is handed: give it a copy.
        await orchestrator.bus.emit(ALLOCATION_EVENT, dict(payload))

        result: dict[str, Any] = {
            "success": status == "applied",
            "status": status,
            **{key: value for key, value in payload.items() if key != "error"},
            "event_id": event_id,
        }
        if status != "applied":
            result["error_code"] = f"allocation_{status}"
            result["error"] = (
                f"provider allocation {status.replace('_', ' ')}: {error}"
                if error else f"provider allocation {status.replace('_', ' ')}"
            )
        return result

    async def _allocate_profile(
        self, planned: dict, request: dict, correlation: dict
    ) -> dict[str, Any]:
        """One profile's edit: the lifecycle, then the bounds, as the pool commands write them.

        ``restore`` is the row a compensation puts back: the first helper's
        ``before`` (every pool key, ``max_claims_per_session`` included), or
        the preview's when no helper got as far as reading it.
        """
        from src.commands import pool_admin

        profile_id = planned["profile_id"]
        before, after = planned["before"], planned["after"]
        outcome: dict[str, Any] = {
            "success": True,
            "error": None,
            "restore": {"id": profile_id, **before},
            "session_actions": [],
        }
        steps: list[tuple[Any, dict]] = []
        if before["lifecycle"] != after["lifecycle"]:
            steps.append((pool_admin.set_pool_lifecycle,
                          {"profile_id": profile_id, "lifecycle": after["lifecycle"]}))
        bounds = request["bounds"]
        if bounds is not None and after["lifecycle"] == "pool":
            # ``now`` is ``aq pool scale --now``: an immediate drain stops the
            # idle excess at once, a graceful one leaves it to the sizer.
            scale = {"profile_id": profile_id, "now": request["drain"] != "graceful"}
            if "min" in bounds:
                scale["min"] = bounds["min"]
            if "max" in bounds:
                scale["max"] = bounds["max"]
            steps.append((pool_admin.set_pool_bounds, scale))
        first = True
        for helper, helper_args in steps:
            try:
                result = await helper(self, helper_args, correlation=correlation)
            except Exception as exc:
                logger.warning("provider allocation: %s failed on %s", helper.__name__,
                               profile_id, exc_info=True)
                result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            if first and result.get("before"):
                outcome["restore"] = dict(result["before"])
            first = False
            outcome["session_actions"].extend(result.get("session_actions") or [])
            if not result.get("success"):
                outcome["success"] = False
                outcome["error"] = str(result.get("error") or f"{helper.__name__} failed")
                break
        return outcome

    async def _compensate(self, rows: list[dict], correlation: dict) -> bool:
        """Undo every attempted profile edit, newest first; True when all were undone.

        A failed row is restored too: its lifecycle may have landed before
        its bounds failed.  Each row records ``compensated`` and, when the
        compensating write itself fails, ``compensation_error``.
        """
        from src.commands import pool_admin

        complete = True
        for entry in reversed(rows):
            if entry["status"] not in ("applied", "failed"):
                continue
            try:
                result = await pool_admin.restore_pool_profile(
                    self, entry["_restore"], correlation=correlation
                )
            except Exception as exc:
                logger.warning("provider allocation: compensation failed on %s",
                               entry["profile_id"], exc_info=True)
                result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            entry["compensated"] = bool(result.get("success"))
            if result.get("success"):
                if entry["status"] == "applied":
                    entry["status"] = "rolled_back"
                continue
            complete = False
            entry["compensation_error"] = str(result.get("error") or "compensation failed")
            if entry["status"] == "applied":
                entry["status"] = "rollback_failed"
        return complete

    async def _drain_allocation(
        self, orchestrator, preview: dict, correlation: dict
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Terminate the idle (``idle-now``) or interrupt the busy (``interrupt-busy``) now.

        Only for profiles leaving the pool: ``set_pool_lifecycle`` has marked
        every live session of them stopped (so none claims again), and a
        lowered bound's idle excess went with ``set_pool_bounds``'s ``now``.
        What is left is the preview's ``terminate`` / ``interrupt`` sessions,
        stopped through the orchestrator's pool teardown -- the one path that
        releases the task back to READY, the claim, the workspace and emits
        ``pool.session_drained`` -- never by killing a process here.  A
        session that went busy since the preview is left to finish.
        """
        leaving = {
            row["profile_id"]
            for row in preview["profiles"]
            if row["changed"] and row["before"]["lifecycle"] == "pool"
            and row["after"]["lifecycle"] == "task"
        }
        actions: list[dict[str, Any]] = []
        errors: list[str] = []
        for planned in preview["sessions"]:
            action = planned["action"]
            if planned["profile_id"] not in leaving or action not in ("terminate", "interrupt"):
                continue
            reason = "allocation_interrupt" if action == "interrupt" else "allocation_idle_now"
            record = {
                "project_id": planned["project_id"],
                "profile_id": planned["profile_id"],
                "session_id": planned["session_id"],
                "action": action,
                "reason": reason,
            }
            session = await self.db.get_session(planned["session_id"])
            if session is None or session.state not in ("starting", "running", "draining"):
                actions.append({**record, "action": "none", "reason": "already_stopped"})
                continue
            if action == "terminate" and (session.task_id or session.claim_phase):
                actions.append({**record, "action": "stop_after_task", "reason": "became_busy"})
                continue
            try:
                await orchestrator._terminate_pool_session(
                    session, reason=reason, correlation=correlation
                )
            except Exception as exc:
                logger.warning("provider allocation: could not stop %s", session.id,
                               exc_info=True)
                errors.append(f"{session.id}: {type(exc).__name__}: {exc}")
                actions.append({**record, "error": f"{type(exc).__name__}: {exc}"})
                continue
            actions.append(record)
        return actions, errors

    async def _place_preferred_work(
        self, orchestrator, project_id: str, provider: str, actor: str
    ) -> dict[str, Any]:
        """Move the project's queued ``class_only`` READY work to *provider* (Decision A3).

        Each task goes to its class's equivalent rung on the preferred
        provider through the operator re-route path
        (``ProviderRerouteService.sweep`` with ``task_ids`` and
        ``to_profile``), so every move is a recorded ``task_reroutes`` row
        that ``aq provider reroute-undo --batch`` reverses, within the
        per-task limits.  ``pinned`` and ``preferred`` tasks, tasks already on
        the provider, unrouted tasks (the routing boundary derives theirs)
        and anything claimed or running are untouched.  A class with no
        launchable rung on the provider holds (``preferred_provider_unavailable``):
        the task never spills to a third provider.
        """
        from types import SimpleNamespace

        from src.providers.intent import CLASS_ONLY, effective_intent
        from src.providers.reroute import equivalent_rung

        placement: dict[str, Any] = {
            "applied": False, "moved": [], "held": [], "skipped": [], "batch_ids": [],
            "errors": [],
        }
        service = getattr(orchestrator, "provider_reroute", None)
        if service is None:
            placement["detail"] = "provider failover is not running"
            return placement
        try:
            ctx = await service.context()
            sources = sorted(pid for pid, rung in ctx.rungs.items() if rung.provider != provider)
            rows = await self.db.list_reroute_candidates(sources) if sources else []
        except Exception as exc:
            logger.warning("provider allocation: queued work unreadable", exc_info=True)
            placement["errors"].append(f"queued work unreadable: {type(exc).__name__}: {exc}")
            return placement
        allow_degraded = bool(
            getattr(getattr(ctx.config, "reroute", None), "allow_degraded_target", False)
        )
        groups: dict[str, list[str]] = {}
        for row in rows:
            if row.get("project_id") != project_id or row.get("status") != "READY":
                continue
            intent = effective_intent(
                SimpleNamespace(provider_intent=row.get("provider_intent"),
                                profile_id=row.get("profile_id"))
            )
            if intent != CLASS_ONLY:
                continue
            rung = ctx.rungs.get(row["profile_id"])
            class_id = str(row.get("intelligence_class") or "").strip() or (
                rung.class_id if rung else ""
            )
            target = None
            if class_id:
                target, _exists = equivalent_rung(
                    ctx, class_id, provider, allow_degraded=allow_degraded
                )
            if target is None:
                placement["held"].append(
                    {
                        "task_id": row["id"],
                        "project_id": project_id,
                        "from_profile_id": row["profile_id"],
                        "kind": "preferred_provider_unavailable",
                        "detail": f"provider {provider} has no launchable "
                        f"{class_id or 'matching'} rung",
                    }
                )
                continue
            groups.setdefault(target.profile_id, []).append(row["id"])
        applied = True
        batches: set[str] = set()
        for to_profile in sorted(groups):
            try:
                result = await service.sweep(
                    task_ids=groups[to_profile], to_profile=to_profile, actor=actor
                )
            except Exception as exc:
                logger.warning("provider allocation: re-placing onto %s failed", to_profile,
                               exc_info=True)
                placement["errors"].append(f"{to_profile}: {type(exc).__name__}: {exc}")
                applied = False
                continue
            applied = applied and bool(result.get("applied"))
            placement["moved"].extend(result.get("moved") or [])
            placement["held"].extend(result.get("held") or [])
            placement["skipped"].extend(result.get("skipped") or [])
            batches.update(result.get("batch_ids") or [])
        placement["applied"] = applied
        placement["batch_ids"] = sorted(batches)
        return placement
