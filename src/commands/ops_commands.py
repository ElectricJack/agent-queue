"""Ops commands mixin — health checks (``doctor``) and cost rollups.

Implements ``docs/specs/implementation/trust-and-ops.md`` §5.4 and §6.3.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging
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

    def _pool_provider_unavailable(self, profile, now: float) -> dict | None:
        """``provider_unavailable`` for a pool row, or ``None`` (provider-failover D13)."""
        availability = getattr(self.orchestrator, "provider_availability", None)
        if availability is None or profile is None:
            return None
        provider = availability.provider_for_profile(profile)
        if not availability.suppresses(provider, now):
            return None
        row = availability.row(provider)
        return {
            "provider": provider,
            "state": availability.effective_state(provider, now),
            "reason": row.effective_reason(now) if row is not None else "",
            "until": row.effective_until(now) if row is not None else None,
        }

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
        from src.pool_claims import pool_claim_loop_stall_seconds
        from src.scheduler import PoolProjectSupply
        from src.sessions.input_prompts import find_awaiting_input_sessions

        view = (args.get("project_id") or "").strip() or None
        measurement = await self.orchestrator._measure_pools()
        now = time.time()
        sessions_by_profile: dict[str, list] = {}
        pool_sessions = measurement.pool_sessions
        measured_profiles = {key.profile_id for key in measurement.supply}
        for session in pool_sessions:
            if session.project_id is None or session.state == "stopped":
                continue
            if view is not None and session.project_id != view:
                continue
            if session.profile_id not in measured_profiles:
                continue
            sessions_by_profile.setdefault(session.profile_id, []).append(session)

        awaiting_input = await find_awaiting_input_sessions(
            pool_sessions,
            now=now,
            stall_seconds=pool_claim_loop_stall_seconds(self.config.swarm),
            harness_registry=self.orchestrator.harness_registry,
            providers=self.orchestrator.session_providers,
            config=self.config,
        )
        awaiting_by_session = {finding.session.id: finding for finding in awaiting_input}

        # Task-lifecycle sessions are intentionally excluded from sizing, but
        # silently omitting a live session that consumes the same class and
        # harness made a zero-busy pool look healthy while all worktree slots
        # were held elsewhere.  Associate those sessions with every pool that
        # can serve their execution route without folding them into supply.
        all_profiles = {profile.id: profile for profile in measurement.all_profiles}
        pool_routes: dict[tuple[str, str], list[str]] = {}
        for key, profile in measurement.profiles.items():
            harness = str(getattr(profile, "harness", "") or "").strip()
            default_class = str(getattr(profile, "default_class", "") or "").strip()
            if harness and default_class:
                pool_routes.setdefault((harness, default_class), []).append(key.profile_id)
        outside_by_profile: dict[str, list[dict]] = {}
        task_sessions = await self.db.list_sessions(lifecycle="task", live_only=True)
        routed_sessions = []
        for session in task_sessions:
            if view is not None and session.project_id != view:
                continue
            profile = all_profiles.get(session.profile_id)
            harness = str(session.harness or getattr(profile, "harness", "") or "").strip()
            intelligence_class = str(
                session.intelligence_class or getattr(profile, "default_class", "") or ""
            ).strip()
            matched = pool_routes.get((harness, intelligence_class), [])
            if matched:
                routed_sessions.append((session, harness, intelligence_class, matched))
        task_titles = await self.db.get_task_titles(
            [
                session.task_id
                for sessions in sessions_by_profile.values()
                for session in sessions
                if session.task_id is not None
            ]
            + [
                session.task_id
                for session, _harness, _intelligence_class, _matched in routed_sessions
                if session.task_id is not None
            ]
        )
        for session, harness, intelligence_class, matched in routed_sessions:
            detail = {
                "session_id": session.id,
                "project_id": session.project_id,
                "profile_id": session.profile_id,
                "harness": harness,
                "intelligence_class": intelligence_class,
                "name": session.name,
                "state": session.state,
                "task_id": session.task_id,
                "task_title": task_titles.get(session.task_id),
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
            service_tier = None
            if getattr(profile, "harness", None) == "codex":
                from src.sessions.spec import _is_codex_cli, codex_service_tier_for

                harness = self.orchestrator.harness_registry.get("codex")
                if harness is not None and _is_codex_cli(harness):
                    class_config = self.orchestrator.session_spec_builder._resolve_class_config(
                        profile, harness, None
                    )
                    service_tier = codex_service_tier_for(profile, class_config)

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
                idle_since = session.last_activity or session.started_at
                blocked = awaiting_by_session.get(session.id)
                instance = {
                    "session_id": session.id,
                    # A worker's workspace fixes its project at launch, so
                    # this is the only place the binding stays visible.
                    "project_id": session.project_id,
                    "name": session.name,
                    "state": "blocked_on_input" if blocked is not None else session.state,
                    "task_id": session.task_id,
                    "task_title": task_titles.get(session.task_id),
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
                if blocked is not None:
                    instance.update(
                        input_prompt=blocked.signature.name,
                        unchanged_seconds=round(blocked.unchanged_seconds, 1),
                    )
                instances.append(instance)

            pools.append(
                {
                    "profile_id": key.profile_id,
                    "service_tier": service_tier,
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
                    "blocked_on_input": sum(
                        finding.session.profile_id == key.profile_id
                        for finding in awaiting_input
                    ),
                    "projects": projects,
                    "instances": instances,
                    "outside_pools": outside_by_profile.get(key.profile_id, []),
                    "provider_unavailable": self._pool_provider_unavailable(profile, now),
                }
            )

        # The measurement walks active projects, so a pool with no project to
        # place a worker in has no row there -- and a fresh install, whose
        # first pools exist before its first project, was told "No worker
        # pools configured."  A configured pool is listed regardless, sized
        # by its bounds, with nothing running and no project placement yet.
        if view is None:
            measured = {row["profile_id"] for row in pools}
            for profile in sorted(all_profiles.values(), key=lambda p: p.id):
                if (
                    ":" in profile.id
                    or getattr(profile, "lifecycle", "task") != "pool"
                    or profile.id in measured
                ):
                    continue
                pools.append(
                    {
                        "profile_id": profile.id,
                        "enabled": getattr(profile, "enabled", True),
                        "min_active": getattr(profile, "min_active", None) or 0,
                        "max_active": getattr(profile, "max_active", None),
                        "min_per_project": getattr(profile, "min_per_project", None) or 0,
                        "desired": 0,
                        "running_idle": 0,
                        "running_busy": 0,
                        "starting": 0,
                        "draining": 0,
                        "ready": 0,
                        "blocked_on_input": 0,
                        "projects": [],
                        "instances": [],
                        "outside_pools": [],
                        "note": "no active project yet; workers start when a project has ready tasks",
                    }
                )
            pools.sort(key=lambda row: row["profile_id"])
        return {"success": True, "pools": pools}

    async def _cmd_pool_set_lifecycle(self, args: dict) -> dict:
        """Set the global profile lifecycle, draining former pool sessions."""
        from src.commands.pool_admin import command_response, set_pool_lifecycle

        return command_response(await set_pool_lifecycle(self, args))

    async def _cmd_pool_set_enabled(self, args: dict) -> dict:
        """Set the global pool profile's operator kill-switch."""
        from src.commands.pool_admin import command_response, set_pool_enabled

        return command_response(await set_pool_enabled(self, args))

    async def _cmd_pool_scale(self, args: dict) -> dict:
        """Set global pool bounds, optionally stopping surplus idle workers now."""
        from src.commands.pool_admin import command_response, set_pool_bounds

        return command_response(await set_pool_bounds(self, args))
