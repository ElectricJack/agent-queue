"""Ops commands mixin — health checks (``doctor``) and cost rollups.

Implements ``docs/specs/implementation/trust-and-ops.md`` §5.4 and §6.3.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _parse_since(raw: str | None) -> float | None:
    """Parse ``"7d"`` / ``"12h"`` / ``"YYYY-MM-DD"`` into a unix timestamp.

    Returns ``None`` when *raw* is empty (meaning "all time").  Raises
    ``ValueError`` on an unparseable value so the caller can report it.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "w": 604800}
    if len(text) > 1 and text[-1].lower() in units and text[:-1].isdigit():
        return time.time() - int(text[:-1]) * units[text[-1].lower()]
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(
            f"unrecognised 'since' value {raw!r}: expected e.g. '7d', '12h' or 'YYYY-MM-DD'"
        ) from exc
    return parsed.timestamp()


class OpsCommandsMixin:
    """Ops command methods mixed into CommandHandler."""

    # -----------------------------------------------------------------------
    # doctor
    # -----------------------------------------------------------------------

    @property
    def doctor_registry(self):
        """The daemon-wide :class:`~src.doctor.runner.DoctorRegistry`, if any.

        Constructed in ``src/main.py`` and attached to the orchestrator so
        every ``CommandHandler`` built from it (API, MCP, supervisor) sees the
        same set of registered checks.  ``None`` in minimal contexts (tests,
        CLI-only) — doctor then reports "not configured" rather than crashing.
        """
        explicit = getattr(self, "_doctor_registry", None)
        if explicit is not None:
            return explicit
        return getattr(self.orchestrator, "doctor_registry", None)

    async def _cmd_doctor(self, args: dict) -> dict:
        """Run the health-check catalog and summarise the result.

        Args:
            fix: When True, run the ``fix`` of each failing fixable check and
                re-run it, reporting the post-fix severity.
            checks: Optional list of check ids to run (default: all).

        Returns:
            ``{"success": True, "checks": [...], "summary": {...},
            "exit_code": 0|1|2}``.  Exit codes follow design §5.6: errors → 2,
            warns → 1, otherwise 0; the CLI maps its own transport failure to 3.
        """
        from src.doctor.models import DoctorContext
        from src.doctor.runner import run_doctor

        registry = self.doctor_registry
        if registry is None:
            return {
                "success": False,
                "error": "doctor registry not configured on this handler",
                "checks": [],
                "summary": {"ok": 0, "info": 0, "warn": 0, "error": 0, "fixes_applied": 0},
                "exit_code": 3,
            }

        only = args.get("checks") or None
        if isinstance(only, str):
            only = [c.strip() for c in only.split(",") if c.strip()]

        ctx = DoctorContext(config=self.config, db=self.db, handler=self)
        try:
            result = await run_doctor(registry, ctx, fix=bool(args.get("fix")), only=only)
        except Exception as exc:
            # The runner isolates individual checks, but a bug in the runner
            # itself (or a check that returns a CheckResult with an unexpected
            # id) must not take the command down: doctor is what an operator
            # reaches for when things are already broken.
            logger.exception("doctor runner crashed")
            return {
                "success": False,
                "error": f"doctor runner failed: {type(exc).__name__}: {exc}",
                "checks": [],
                "summary": {"ok": 0, "info": 0, "warn": 0, "error": 0, "fixes_applied": 0},
                "exit_code": 3,
            }
        result["success"] = True
        return result

    # -----------------------------------------------------------------------
    # costs
    # -----------------------------------------------------------------------

    async def _cmd_get_costs(self, args: dict) -> dict:
        """Roll the token ledger up into money using ``pricing:`` from config.

        Args:
            project_id: Restrict to one project.
            since: ``"7d"`` / ``"12h"`` / ``"YYYY-MM-DD"``; omitted = all time.
            group_by: ``"project"`` (default), ``"profile"`` or ``"day"``.

        Returns:
            ``{"success": True, "rows": [...], "total_cost_usd": float,
            "unpriced_tokens": int, "pricing_models": [...]}``.

        Honesty rule (design §7): a row is priced only when it carries both a
        ``model`` that matches a pricing entry **and** an input/output split.
        Everything else counts toward ``unpriced_tokens`` with ``cost_usd``
        left null — the ledger is never priced at a guessed rate.

        The rule applies *within* a row too.  ``get_cost_rollup`` buckets by
        ``(group, model)``, so one bucket can hold both split and unsplit
        ledger entries; pricing the bucket off its split sum would leave the
        unsplit tokens counted in neither ``cost_usd`` nor
        ``unpriced_tokens``.  Each row therefore reports its own
        ``unpriced_tokens`` — ``tokens_used`` minus the split that was
        actually priced — and those roll into the total.
        """
        group_by = args.get("group_by") or "project"
        if group_by not in ("project", "profile", "day"):
            return {"error": f"group_by must be project, profile or day (got {group_by!r})"}

        try:
            since_ts = _parse_since(args.get("since"))
        except ValueError as exc:
            return {"error": str(exc)}

        project_id = args.get("project_id") or self._active_project_id

        try:
            rollup = await self.db.get_cost_rollup(
                project_id=project_id,
                since_ts=since_ts,
                group_by=group_by,
            )
        except Exception as exc:
            logger.exception("cost rollup failed")
            return {"error": f"cost rollup failed: {type(exc).__name__}: {exc}"}

        pricing = self.config.pricing
        rows: list[dict] = []
        total_cost = 0.0
        unpriced = 0

        for row in rollup:
            model = row.get("model")
            entry = pricing.match(model) if model else None
            split_tokens = (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0)
            total_tokens = row.get("tokens_used", 0) or 0
            cost: float | None = None
            if entry is not None and split_tokens:
                cost = (row.get("input_tokens") or 0) * entry.input_per_mtok / 1_000_000 + (
                    row.get("output_tokens") or 0
                ) * entry.output_per_mtok / 1_000_000
                total_cost += cost
                # Tokens in this bucket that carried no split are not covered
                # by `cost` — count them as unpriced rather than losing them.
                row_unpriced = max(0, total_tokens - split_tokens)
            else:
                row_unpriced = total_tokens
            unpriced += row_unpriced
            rows.append(
                {
                    **row,
                    "cost_usd": cost,
                    "unpriced_tokens": row_unpriced,
                    "pricing_model": entry.model if entry else None,
                }
            )

        return {
            "success": True,
            "rows": rows,
            "group_by": group_by,
            "project_id": project_id,
            "since": since_ts,
            "total_cost_usd": round(total_cost, 6),
            "unpriced_tokens": unpriced,
            "pricing_models": [m.model for m in pricing.models],
        }

    # -----------------------------------------------------------------------
    # hierarchy preflight
    # -----------------------------------------------------------------------

    async def _cmd_db_preflight_hierarchy(self, args: dict) -> dict:
        """Dry-run hierarchy canonicalisation; commit the rejects report (spec §17)."""
        import os
        import uuid

        from src.database import hierarchy_migration as hm

        run_id = uuid.uuid4().hex[:12]
        holder: dict = {}

        def _run(sync_conn):
            plan = hm.canonicalise(sync_conn)
            hm.persist_rejects(sync_conn, run_id, plan.rejects)
            holder["plan"] = plan

        async with self.db._engine.begin() as conn:
            await conn.run_sync(_run)
        plan = holder["plan"]
        report = os.path.join(
            os.path.expanduser(self.config.data_dir), "logs", f"hierarchy-preflight-{run_id}.json"
        )
        hm.write_report(report, run_id, plan)
        return {
            "success": not plan.rejects,
            "run_id": run_id,
            "parents_resolved": len(plan.parents),
            "rejects": [r.__dict__ for r in plan.rejects],
            "report_path": report,
        }

    # -----------------------------------------------------------------------
    # worker pools — sizing and bounds (swarm-work-model §11)
    # -----------------------------------------------------------------------

    async def _cmd_pool_status(self, args: dict) -> dict:
        """Supply/demand/bounds snapshot for every worker pool.  Backs ``aq pool status``.

        One row per **profile** — a pool is a fleet of durable workers shared
        across every project, and sizing is fleet-wide (global-worker-pools
        §6.1).  The per-project detail an operator still needs ("where are my
        workers actually running", "why is this project not growing") is
        nested in ``projects`` rather than dropped, and quarantine lives
        there because a quarantine was never a property of a global pool.

        ``project_id`` is retained as a **view filter** over ``projects`` and
        ``instances``, never as pool identity: bounds, ``desired`` and the
        top-level counters stay fleet-wide whether or not one is passed,
        because filtering them would misreport the pool the sizer acts on.
        """
        from src.scheduler import PoolProjectSupply

        view = (args.get("project_id") or "").strip() or None
        measurement = await self.orchestrator._measure_pools()
        now = time.time()
        sessions_by_profile: dict[str, list] = {}
        for session in await self.db.list_sessions(lifecycle="pool"):
            if session.project_id is None or session.state == "stopped":
                continue
            if view is not None and session.project_id != view:
                continue
            sessions_by_profile.setdefault(session.profile_id, []).append(session)

        # Task-lifecycle sessions are intentionally excluded from sizing, but
        # silently omitting a live session that consumes the same class and
        # harness made a zero-busy pool look healthy while all worktree slots
        # were held elsewhere.  Associate those sessions with every pool that
        # can serve their execution route without folding them into supply.
        all_profiles = {profile.id: profile for profile in await self.db.list_profiles()}
        pool_routes: dict[tuple[str, str], list[str]] = {}
        for key, profile in measurement.profiles.items():
            harness = str(getattr(profile, "harness", "") or "").strip()
            default_class = str(getattr(profile, "default_class", "") or "").strip()
            if harness and default_class:
                pool_routes.setdefault((harness, default_class), []).append(key.profile_id)
        outside_by_profile: dict[str, list[dict]] = {}
        for session in await self.db.list_sessions(lifecycle="task", live_only=True):
            if view is not None and session.project_id != view:
                continue
            profile = all_profiles.get(session.profile_id)
            harness = str(session.harness or getattr(profile, "harness", "") or "").strip()
            intelligence_class = str(
                session.intelligence_class or getattr(profile, "default_class", "") or ""
            ).strip()
            matched = pool_routes.get((harness, intelligence_class), [])
            if not matched:
                continue
            task = await self.db.get_task(session.task_id) if session.task_id else None
            detail = {
                "session_id": session.id,
                "project_id": session.project_id,
                "profile_id": session.profile_id,
                "harness": harness,
                "intelligence_class": intelligence_class,
                "name": session.name,
                "state": session.state,
                "task_id": session.task_id,
                "task_title": task.title if task is not None else None,
                "started_at": session.started_at,
            }
            for profile_id in matched:
                outside_by_profile.setdefault(profile_id, []).append(detail)

        pools = []
        for key in sorted(measurement.supply, key=lambda k: k.profile_id):
            sup = measurement.supply[key]
            lo, hi = measurement.bounds[key]
            ready = measurement.demand.get(key, 0)
            # The same arithmetic ``size_pools`` runs, on the same fleet-wide
            # numbers, so what an operator reads here is what the sizer will
            # converge on next tick.
            want = sup.running_busy + ready
            desired = max(lo, want) if hi is None else min(max(lo, want), hi)
            desired = max(desired, sup.running_busy + sup.starting)
            profile = measurement.profiles.get(key)

            projects = []
            for cand in sorted(
                measurement.candidates.get(key, []), key=lambda c: c.project_id
            ):
                if view is not None and cand.project_id != view:
                    continue
                local = sup.by_project.get(cand.project_id) or PoolProjectSupply()
                until, reason = self.orchestrator._pool_quarantine_state(
                    cand.project_id, key.profile_id, now
                )
                projects.append(
                    {
                        "project_id": cand.project_id,
                        "ready": cand.ready,
                        "running_idle": local.running_idle,
                        "running_busy": local.running_busy,
                        "starting": local.starting,
                        "draining": local.draining,
                        "max_concurrent_agents": cand.project_cap,
                        "workspace_capacity": cand.workspace_capacity,
                        # A deadline on its own left an operator staring at a
                        # pool that will not grow with nothing to act on.
                        "quarantined_until": until,
                        "quarantined_reason": reason if until else None,
                    }
                )
            if view is not None and not projects:
                # The filter named a project this pool has no standing in;
                # reporting global bounds under it would be misleading.
                continue

            instances = []
            for session in sessions_by_profile.get(key.profile_id, []):
                task = await self.db.get_task(session.task_id) if session.task_id else None
                idle_since = session.last_activity or session.started_at
                instances.append(
                    {
                        "session_id": session.id,
                        # A worker's workspace fixes its project at launch, so
                        # this is the only place the binding stays visible.
                        "project_id": session.project_id,
                        "name": session.name,
                        "state": session.state,
                        "task_id": session.task_id,
                        "task_title": task.title if task is not None else None,
                        "idle_seconds": (
                            max(0.0, now - idle_since)
                            if session.task_id is None and session.claim_phase is None
                            else None
                        ),
                        "started_at": session.started_at,
                        "quarantine_reason": (
                            session.end_reason if session.state == "quarantined" else None
                        ),
                    }
                )

            pools.append(
                {
                    "profile_id": key.profile_id,
                    # An operator kill-switch on the (global) profile.  Disabled
                    # pools keep their row — that is how the dashboard offers the
                    # toggle that turns them back on — and are sized to zero.
                    "enabled": getattr(profile, "enabled", True),
                    "min_active": lo,
                    "max_active": hi,
                    "min_per_project": getattr(profile, "min_per_project", None) or 0,
                    "desired": desired,
                    "running_idle": sup.running_idle,
                    "running_busy": sup.running_busy,
                    "starting": sup.starting,
                    "draining": sup.draining,
                    "ready": ready,
                    "projects": projects,
                    "instances": instances,
                    "outside_pools": outside_by_profile.get(key.profile_id, []),
                }
            )
        return {"success": True, "pools": pools}

    def _system_profile_path(self, agent_type: str) -> str:
        """Vault path of the system profile markdown for *agent_type*."""
        return os.path.join(self.config.data_dir, "vault", "agent-types", agent_type, "profile.md")

    async def _pool_profile_target(self, profile_id: str, *, require_pool: bool):
        """Resolve the (global) profile a pool edit applies to."""
        target = await self.db.get_profile(profile_id)
        if target is None:
            return None
        if require_pool and getattr(target, "lifecycle", "task") != "pool":
            return None
        return target

    async def _write_pool_profile_config(self, profile_id: str, updates: dict, *,
                                         require_pool: bool):
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

        target = await self._pool_profile_target(profile_id, require_pool=require_pool)
        if target is None:
            return None

        if updates:
            await self.db.update_profile(target.id, **updates)
        await self._write_pool_profile_config_to_vault(target.id, updates)
        return dataclasses.replace(target, **updates)

    async def _write_pool_profile_config_to_vault(self, profile_id: str, updates: dict) -> None:
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

        path = Path(self._system_profile_path(profile_id))
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
            markdown, self.db, source_path=str(path), fallback_id=profile_id
        )
        if not result.success:
            logger.warning(
                "pool profile edit: wrote %s but the DB re-sync failed: %s", path, result.errors
            )

    async def _cmd_pool_set_lifecycle(self, args: dict) -> dict:
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
        if lifecycle == "pool" and not getattr(self.config.swarm, "enabled", True):
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
        profile = await self._write_pool_profile_config(
            profile_id, updates, require_pool=False
        )
        if profile is None:
            return {"success": False, "error": f"no profile '{profile_id}'"}
        if lifecycle == "task":
            # Do not let workers from the former pool take another task while
            # the reconciler drains them.  Active tasks retain their session
            # until their normal close/release path completes.  The profile is
            # global, so every project's pool for it drains.
            for session in await self.db.list_sessions(lifecycle="pool", live_only=True):
                if session.profile_id != profile_id:
                    continue
                await self.db.update_session(session.id, desired_state="stopped")
                await self.orchestrator.bus.emit(
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
        project_ids = [project.id for project in await self.db.list_projects()]
        for project_id in project_ids:
            await self.orchestrator.bus.emit(
                "pool.lifecycle_changed",
                {
                    "project_id": project_id,
                    "profile_id": profile_id,
                    "lifecycle": lifecycle,
                },
            )
        return {
            "success": True,
            "profile_id": profile_id,
            "lifecycle": lifecycle,
            "warnings": [],
        }

    async def _cmd_pool_set_enabled(self, args: dict) -> dict:
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
        profile = await self._write_pool_profile_config(
            profile_id, {"enabled": enabled}, require_pool=True
        )
        if profile is None:
            return {"success": False, "error": f"no pool profile '{profile_id}'"}
        for project in await self.db.list_projects():
            await self.orchestrator.bus.emit(
                "pool.enabled_changed",
                {
                    "project_id": project.id,
                    "profile_id": profile_id,
                    "enabled": enabled,
                },
            )
        return {
            "success": True,
            "profile_id": profile_id,
            "enabled": enabled,
            "warnings": [],
        }

    async def _cmd_pool_scale(self, args: dict) -> dict:
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
        target = await self._pool_profile_target(profile_id, require_pool=True)
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
        profile = await self._write_pool_profile_config(
            profile_id, updates, require_pool=True
        )

        # Sizing is fleet-wide, but two ceilings still bound what a single
        # project may hold: its own ``max_concurrent_agents``, and the
        # box-wide cap the sizer applies across every pool.  Report the
        # smallest of the three, which is the number that actually applies.
        global_cap = self.orchestrator._pool_global_cap()
        project_caps = []
        effective_by_project: dict[str, int | None] = {}
        for project in await self.db.list_projects():
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
        if args.get("now"):
            sessions = await self.db.list_sessions(lifecycle="pool")
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
                    await self.orchestrator._terminate_pool_session(s, reason="scaled")
                    terminated.append(s.id)

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
            await self.orchestrator.bus.emit(
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
        return response
