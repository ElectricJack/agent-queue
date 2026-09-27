"""Shared validated pool edits behind CommandHandler's pool commands.

Allocation callers use the same helpers as individual pool commands. Successful
results retain the command response and add ``before`` / ``after`` configuration
rows and ``session_actions`` for audit and compensating writes. Validation failures
retain the command's exact error dict. An absent bounds key leaves it unchanged;
explicit ``max: None`` removes the ceiling.

Optional correlation metadata is added to events without overriding domain fields.
Vault persistence retains the commands' DB-first, logged-failure behavior.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)


def command_response(result: dict) -> dict:
    """Keep the public single-profile commands' existing response shape."""
    return {
        key: value
        for key, value in result.items()
        if key not in {"before", "after", "session_actions"}
    }


def _profile_row(profile) -> dict:
    return {
        field: getattr(profile, field, None)
        for field in (
            "id",
            "lifecycle",
            "enabled",
            "min_active",
            "max_active",
            "min_per_project",
            "max_claims_per_session",
        )
    }


def _with_audit(response: dict, before, after, session_actions: list[dict]) -> dict:
    return {
        **response,
        "before": _profile_row(before),
        "after": _profile_row(after),
        "session_actions": session_actions,
    }


def _session_action(session, action: str, reason: str) -> dict:
    return {
        "project_id": session.project_id,
        "profile_id": session.profile_id,
        "session_id": session.id,
        "action": action,
        "reason": reason,
    }


async def _emit(handler, correlation, event_type: str, payload: dict) -> None:
    await handler.orchestrator.bus.emit(event_type, {**(correlation or {}), **payload})


def _system_profile_path(handler, agent_type: str) -> str:
    """Vault path of the system profile markdown for *agent_type*."""
    return os.path.join(handler.config.data_dir, "vault", "agent-types", agent_type, "profile.md")


async def _pool_profile_target(handler, profile_id: str, *, require_pool: bool):
    """Resolve the (global) profile a pool edit applies to."""
    target = await handler.db.get_profile(profile_id)
    if target is None:
        return None
    if require_pool and getattr(target, "lifecycle", "task") != "pool":
        return None
    return target


async def _write_pool_profile_config(
    handler, profile_id: str, updates: dict, *, require_pool: bool
):
    """Persist profile config, with the vault as source of truth.

    The vault ``## Config`` block is the source of truth (swarm spec §14),
    so operator edits are written into the **system** profile markdown at
    ``vault/agent-types/<id>/profile.md`` and then synced back into the
    ``agent_profiles`` row.  Writing only the DB row would let a later
    vault sync silently revert the operator edit.

    The DB row is also updated directly and first, so the very next
    orchestrator tick sees the change even if the sync is slow.
    """
    import dataclasses

    target = await _pool_profile_target(handler, profile_id, require_pool=require_pool)
    if target is None:
        return None, None

    if updates:
        await handler.db.update_profile(target.id, **updates)
    await _write_pool_profile_config_to_vault(handler, target.id, updates)
    return target, dataclasses.replace(target, **updates)


async def _write_pool_profile_config_to_vault(handler, profile_id: str, updates: dict) -> None:
    """Merge *updates* into the system profile's ``## Config`` and re-sync.

    Failures are logged, never raised: the DB row has already been updated
    by the caller, so a read-only vault degrades ``pool scale`` to the old
    (non-durable) behaviour rather than failing the command outright.
    """
    if not updates:
        return
    from pathlib import Path

    from src.profiles.parser import update_config_keys
    from src.profiles.sync import sync_profile_text_to_db

    path = Path(_system_profile_path(handler, profile_id))
    try:
        markdown = path.read_text(encoding="utf-8") if path.is_file() else ""
        markdown = update_config_keys(markdown, updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    except OSError:
        logger.warning(
            "pool profile edit: could not write vault profile %s — changes applied to the "
            "agent_profiles row only, and will revert on the next vault sync",
            path,
            exc_info=True,
        )
        return

    result = await sync_profile_text_to_db(
        markdown, handler.db, source_path=str(path), fallback_id=profile_id
    )
    if not result.success:
        logger.warning(
            "pool profile edit: wrote %s but the DB re-sync failed: %s", path, result.errors
        )


async def set_pool_lifecycle(
    handler,
    args: dict,
    *,
    correlation: Mapping[str, object] | None = None,
) -> dict:
    """Set a profile's task/pool lifecycle.  Backs ``aq pool set-lifecycle``.

    The lifecycle is a property of the profile and therefore global: the
    same durable worker serves every project, and sizing is global to
    match — one pool per profile, fleet-wide.  A project's
    ``max_concurrent_agents`` bounds only how much of that fleet it may
    hold at once, as a placement input.
    """
    profile_id = args.get("profile_id")
    lifecycle = args.get("lifecycle")
    if not profile_id:
        return {"success": False, "error": "profile_id is required"}
    if lifecycle not in {"task", "pool"}:
        return {"success": False, "error": "lifecycle must be task or pool"}
    if lifecycle == "pool" and not getattr(handler.config.swarm, "enabled", True):
        return {
            "success": False,
            "error": "cannot set lifecycle to pool while swarm.enabled is false",
        }
    # The sizing knobs are pool-only configuration.  Clear them together
    # with the lifecycle change so the durable profile can be re-synced
    # by the profile parser (which deliberately rejects those keys on a
    # task profile).
    updates = {"lifecycle": lifecycle}
    if lifecycle == "task":
        updates.update(
            min_active=None,
            max_active=None,
            max_claims_per_session=None,
        )
        # ``min_per_project`` is the per-project warm floor and just as
        # pool-only as its siblings.  Cleared conditionally because the
        # field lands in a separate change: naming a column the profile
        # dataclass does not have yet would fail the update outright.
        from src.models import AgentProfile

        if hasattr(AgentProfile, "min_per_project"):
            updates["min_per_project"] = None
    before, profile = await _write_pool_profile_config(
        handler, profile_id, updates, require_pool=False
    )
    if profile is None:
        return {"success": False, "error": f"no profile '{profile_id}'"}
    session_actions = []
    if lifecycle == "task":
        # Do not let workers from the former pool take another task while
        # the reconciler drains them.  Active tasks retain their session
        # until their normal close/release path completes.  The profile is
        # global, so every project's pool for it drains.
        for session in await handler.db.list_sessions(lifecycle="pool", live_only=True):
            if session.profile_id != profile_id:
                continue
            await handler.db.update_session(session.id, desired_state="stopped")
            session_actions.append(_session_action(session, "drain", "lifecycle_changed"))
            await _emit(
                handler,
                correlation,
                "pool.session_drained",
                {
                    "project_id": session.project_id,
                    "profile_id": profile_id,
                    "session_id": session.id,
                    "name": session.name,
                    "reason": "lifecycle_changed",
                },
            )
    # The profile edit is global, but lifecycle events are project-routed.
    # Fan out to every project even when a compatibility caller supplied
    # ``project_id``: that input does not make the profile configuration
    # local, and every payload must satisfy the project event schema.
    project_ids = [project.id for project in await handler.db.list_projects()]
    for project_id in project_ids:
        await _emit(
            handler,
            correlation,
            "pool.lifecycle_changed",
            {
                "project_id": project_id,
                "profile_id": profile_id,
                "lifecycle": lifecycle,
            },
        )
    return _with_audit(
        {
            "success": True,
            "profile_id": profile_id,
            "lifecycle": lifecycle,
            "warnings": [],
        },
        before,
        profile,
        session_actions,
    )


async def set_pool_enabled(
    handler,
    args: dict,
    *,
    correlation: Mapping[str, object] | None = None,
) -> dict:
    """Turn a pool profile on or off.  Backs ``aq pool set-enabled``.

    ``enabled`` is the operator kill-switch for a whole pool profile: the
    profile keeps its definition, its vault markdown and its
    ``pool_status`` row, so a disabled pool stays visible and can be
    switched back on.  What changes is eligibility for *new* work —

    * sizing: ``_measure_pools`` reports bounds ``(0, 0)`` for a disabled
      profile, so the sizer drains idle workers.  ``desired`` is still
      floored at ``busy + starting``, so a worker mid-task keeps its
      session and finishes the task it holds;
    * claims: ``task_claim`` answers ``drain_requested`` for a disabled
      profile, so a busy worker takes no further task after this one.

    Like every other pool edit the switch lives on the (global) system
    profile and therefore applies to every project's pool for it.
    """
    profile_id = args.get("profile_id")
    enabled = args.get("enabled")
    if not profile_id:
        return {"success": False, "error": "profile_id is required"}
    if not isinstance(enabled, bool):
        return {"success": False, "error": "enabled must be a boolean"}
    before, profile = await _write_pool_profile_config(
        handler, profile_id, {"enabled": enabled}, require_pool=True
    )
    if profile is None:
        return {"success": False, "error": f"no pool profile '{profile_id}'"}
    for project in await handler.db.list_projects():
        await _emit(
            handler,
            correlation,
            "pool.enabled_changed",
            {
                "project_id": project.id,
                "profile_id": profile_id,
                "enabled": enabled,
            },
        )
    return _with_audit(
        {
            "success": True,
            "profile_id": profile_id,
            "enabled": enabled,
            "warnings": [],
        },
        before,
        profile,
        [],
    )


async def set_pool_bounds(
    handler,
    args: dict,
    *,
    correlation: Mapping[str, object] | None = None,
) -> dict:
    """Set a pool profile's min/max active-session bounds.  Backs ``aq pool scale``.

    Bounds live on the (global) system profile and apply to every project's
    pool for that profile; each project's ``max_concurrent_agents`` still
    caps its own pool at runtime, which is what ``project_caps`` reports.
    """
    profile_id = args.get("profile_id")
    if not profile_id:
        return {"success": False, "error": "profile_id is required"}
    has_min, has_max = "min" in args, "max" in args
    lo, hi = args.get("min"), args.get("max")
    if not has_min and not has_max:
        return {"success": False, "error": "nothing to change: pass min and/or max"}
    target = await _pool_profile_target(handler, profile_id, require_pool=True)
    if target is None:
        return {"success": False, "error": f"no pool profile '{profile_id}'"}
    min_active = lo if has_min else target.min_active
    max_active = hi if has_max else target.max_active
    if min_active is None or min_active < 0:
        return {"success": False, "error": "min must be >= 0"}
    if max_active is not None and max_active < 1:
        return {"success": False, "error": "max must be >= 1"}
    if max_active is not None and max_active < min_active:
        return {"success": False, "error": "max must be >= min"}
    updates = {}
    if has_min:
        updates["min_active"] = lo
    if has_max:
        updates["max_active"] = hi
    before, profile = await _write_pool_profile_config(
        handler, profile_id, updates, require_pool=True
    )

    # Sizing is fleet-wide, but two ceilings still bound what a single
    # project may hold: its own ``max_concurrent_agents``, and the
    # box-wide cap the sizer applies across every pool.  Report the
    # smallest of the three, which is the number that actually applies.
    global_cap = handler.orchestrator._pool_global_cap()
    project_caps = []
    effective_by_project: dict[str, int | None] = {}
    for project in await handler.db.list_projects():
        cap = getattr(project, "max_concurrent_agents", None)
        ceilings = [c for c in (profile.max_active, cap, global_cap) if c is not None]
        effective = min(ceilings) if ceilings else None
        effective_by_project[project.id] = effective
        project_caps.append(
            {
                "project_id": project.id,
                "max_concurrent_agents": cap,
                "effective_max_active": effective,
            }
        )

    terminated: list[str] = []
    session_actions = []
    if args.get("now"):
        sessions = await handler.db.list_sessions(lifecycle="pool")
        by_project: dict[str, list] = {}
        for s in sessions:
            if s.profile_id == profile_id and s.state in ("running", "stalled"):
                by_project.setdefault(s.project_id, []).append(s)
        for project_id, live in by_project.items():
            effective = effective_by_project.get(project_id, profile.max_active)
            if effective is None:
                continue
            idle = sorted((s for s in live if not s.task_id), key=lambda s: s.started_at or 0)
            for s in idle[: max(0, len(live) - effective)]:
                kwargs = {"correlation": correlation} if correlation else {}
                await handler.orchestrator._terminate_pool_session(s, reason="scaled", **kwargs)
                terminated.append(s.id)
                session_actions.append(_session_action(s, "terminate", "scaled"))

    response = {
        "success": True,
        "profile_id": profile_id,
        "min_active": profile.min_active,
        "max_active": profile.max_active,
        "project_caps": project_caps,
        "terminated": terminated,
        "warnings": [],
    }
    for project_cap in project_caps:
        await _emit(
            handler,
            correlation,
            "pool.bounds_changed",
            {
                "project_id": project_cap["project_id"],
                "profile_id": profile_id,
                "min_active": profile.min_active,
                "max_active": profile.max_active,
                "project_cap": project_cap["max_concurrent_agents"],
                "effective_max_active": project_cap["effective_max_active"],
            },
        )
    return _with_audit(response, before, profile, session_actions)


async def restore_pool_profile(
    handler,
    before: Mapping[str, object],
    *,
    correlation: Mapping[str, object] | None = None,
) -> dict:
    """Put a profile back to *before* (a helper's ``before`` row).  Compensates an allocation.

    Built from the same helpers, so the vault, the database row and the
    ``pool.*`` events land exactly as the single-profile commands would
    write them: the lifecycle first (``set_pool_lifecycle``), then the pool
    bounds (``set_pool_bounds``), then the pool-only keys a move to ``task``
    cleared.  Only fields that differ are written, so restoring an unchanged
    profile is a no-op.  Sessions a lifecycle change already marked stopped
    stay stopped: a drain is not undone, and the caller reports it.
    """
    profile_id = str(before["id"])
    current = await handler.db.get_profile(profile_id)
    if current is None:
        return {"success": False, "error": f"no profile '{profile_id}'"}
    lifecycle = before.get("lifecycle") or "task"
    if getattr(current, "lifecycle", "task") != lifecycle:
        result = await set_pool_lifecycle(
            handler, {"profile_id": profile_id, "lifecycle": lifecycle}, correlation=correlation
        )
        if not result.get("success"):
            return result
        current = await handler.db.get_profile(profile_id)
    if lifecycle == "pool":
        bounds: dict[str, object] = {}
        if current.max_active != before.get("max_active"):
            bounds["max"] = before.get("max_active")
        restore_min = current.min_active != before.get("min_active")
        if restore_min and before.get("min_active") is not None:
            bounds["min"] = before.get("min_active")
        if bounds:
            result = await set_pool_bounds(
                handler, {"profile_id": profile_id, **bounds}, correlation=correlation
            )
            if not result.get("success"):
                return result
        rest = {
            field: before.get(field)
            for field in ("min_per_project", "max_claims_per_session")
            if field in before and getattr(current, field, None) != before.get(field)
        }
        if restore_min and before.get("min_active") is None:
            # ``pool scale`` refuses an unset min; the row had none, so write it back as it was.
            rest["min_active"] = None
        if rest:
            await _write_pool_profile_config(handler, profile_id, rest, require_pool=True)
    restored = await handler.db.get_profile(profile_id)
    return {"success": True, "profile_id": profile_id, "after": _profile_row(restored)}
