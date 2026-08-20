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
        result = await run_doctor(registry, ctx, fix=bool(args.get("fix")), only=only)
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
            has_split = bool(row.get("input_tokens") or row.get("output_tokens"))
            cost: float | None = None
            if entry is not None and has_split:
                cost = (
                    row["input_tokens"] * entry.input_per_mtok / 1_000_000
                    + row["output_tokens"] * entry.output_per_mtok / 1_000_000
                )
                total_cost += cost
            else:
                unpriced += row.get("tokens_used", 0)
            rows.append(
                {
                    **row,
                    "cost_usd": cost,
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
