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
                cost = (
                    (row.get("input_tokens") or 0) * entry.input_per_mtok / 1_000_000
                    + (row.get("output_tokens") or 0) * entry.output_per_mtok / 1_000_000
                )
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
        """Supply/demand/bounds snapshot for every worker pool.  Backs ``aq pool status``."""
        project_ids = {args["project_id"]} if args.get("project_id") else None
        supply, demand, bounds, _profiles, _caps, _projects = (
            await self.orchestrator._measure_pools(project_ids)
        )
        now = time.time()
        pools = []
        for key in sorted(supply, key=lambda k: (k.project_id, k.profile_id)):
            sup, (lo, hi) = supply[key], bounds[key]
            want = sup.running_busy + demand.get(key, 0)
            desired = max(lo, want) if hi is None else min(max(lo, want), hi)
            desired = max(desired, sup.running_busy + sup.starting)
            row = {
                "project_id": key.project_id,
                "profile_id": key.profile_id,
                "min_active": lo,
                "max_active": hi,
                "desired": desired,
                "running_idle": sup.running_idle,
                "running_busy": sup.running_busy,
                "starting": sup.starting,
                "draining": sup.draining,
                "ready": demand.get(key, 0),
            }
            until = self.orchestrator._pool_quarantine.get((key.project_id, key.profile_id))
            if until and until > now:
                row["quarantined_until"] = until
            pools.append(row)
        return {"success": True, "pools": pools}

    async def _write_pool_bounds(
        self, project_id: str, profile_id: str, min_active: int | None, max_active: int | None
    ):
        """Persist new min/max bounds on the effective pool profile for *project_id*.

        Prefers a project-scoped override row (``project:{project_id}:{profile_id}``)
        when one exists, else the system-wide row.  There is no vault writer for
        ``min_active``/``max_active`` today — ``agent_profile_to_markdown``
        (the function ``aq profile set`` / ``_cmd_edit_profile`` uses) has no
        ``lifecycle``/``min_active``/``max_active`` parameters, so this updates
        the ``agent_profiles`` DB row directly via ``db.update_profile``. A
        vault re-sync of the same profile (the watcher, or a future ``aq
        profile set`` call) will overwrite this value once a vault writer for
        pool bounds exists — that is a known gap, not a bug in this command.
        """
        scoped_id = f"project:{project_id}:{profile_id}"
        target = await self.db.get_profile(scoped_id)
        if target is None or getattr(target, "lifecycle", "task") != "pool":
            target = await self.db.get_profile(profile_id)
        if target is None or getattr(target, "lifecycle", "task") != "pool":
            return None

        updates: dict = {}
        if min_active is not None:
            updates["min_active"] = min_active
        if max_active is not None:
            updates["max_active"] = max_active
        if updates:
            await self.db.update_profile(target.id, **updates)

        import dataclasses

        return dataclasses.replace(
            target,
            min_active=min_active if min_active is not None else target.min_active,
            max_active=max_active if max_active is not None else target.max_active,
        )

    async def _cmd_pool_scale(self, args: dict) -> dict:
        """Set a pool profile's min/max active-session bounds.  Backs ``aq pool scale``."""
        project_id, profile_id = args.get("project_id"), args.get("profile_id")
        if not project_id or not profile_id:
            return {"success": False, "error": "project_id and profile_id are required"}
        lo, hi = args.get("min"), args.get("max")
        if lo is not None and lo < 0:
            return {"success": False, "error": "min must be >= 0"}
        if hi is not None and hi < 1:
            return {"success": False, "error": "max must be >= 1"}
        if lo is not None and hi is not None and lo > hi:
            return {"success": False, "error": "min must be <= max"}
        profile = await self._write_pool_bounds(project_id, profile_id, lo, hi)
        if profile is None:
            return {"success": False, "error": f"no pool profile '{profile_id}' for {project_id}"}
        terminated: list[str] = []
        if args.get("now") and hi is not None:
            live = [
                s
                for s in await self.db.list_sessions(lifecycle="pool", project_id=project_id)
                if s.profile_id == profile_id and s.state in ("running", "stalled")
            ]
            idle = sorted((s for s in live if not s.task_id), key=lambda s: s.started_at or 0)
            for s in idle[: max(0, len(live) - hi)]:
                await self.orchestrator._terminate_pool_session(s, reason="scaled")
                terminated.append(s.id)
        return {
            "success": True,
            "project_id": project_id,
            "profile_id": profile_id,
            "min_active": profile.min_active,
            "max_active": profile.max_active,
            "terminated": terminated,
        }
