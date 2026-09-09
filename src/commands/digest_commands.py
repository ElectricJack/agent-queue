"""Digest preview and status: the dashboard's read-only view of §8 and §9.

Two named commands, no dashboard-only business logic:

``digest_preview``
    Evaluates the current window with exactly the code path that delivery
    uses -- :func:`src.digest.aggregate.build_digest` over
    :meth:`collect_digest_activity` -- and returns either the message that
    would be sent or the reason it would stay silent.  It is a *dry* read:
    it reserves no window, sends nothing and advances no cursor, so an
    operator can press it repeatedly without consuming the next real digest.

``digest_status``
    The configured schedule and its health: destination, configuration
    generation, next evaluation, recent windows, and how many digest
    deliveries or escalation deliveries are pending, unknown or failed.

Both are installation-wide reads of one shared destination, so a
project-scoped session principal only ever sees its own project's activity;
the configured project filter is applied before any row is read.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.digest.aggregate import build_digest
from src.digest.facts import CATEGORIES
from src.digest.schedule import schedule_for, validate_settings

logger = logging.getLogger(__name__)

#: Window statuses that mean "the operator should look at this".
_ATTENTION_STATUSES = ("pending", "sending", "retry", "unknown")
_PENDING_DELIVERY_STATUSES = frozenset({"pending", "sending", "retry", "unknown"})

#: How many recent windows ``digest_status`` reports, and how far back a
#: preview looks for wording it has already said.
_RECENT_WINDOWS = 20


def _error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message}


class DigestCommandsMixin:
    """CommandHandler methods for hourly-digest preview and health."""

    def _digest_scope(
        self, schedule_projects: tuple[str, ...]
    ) -> tuple[tuple[str, ...] | None, dict[str, Any] | None]:
        """Visibility for this caller, narrowed to the configured selection.

        The configured selection defines what the destination may see (§9).
        A project-scoped session or playbook principal is narrowed further to
        its own project, and is refused outright when that project is not part
        of the selection -- it must not learn about another project's work
        through a shared-channel preview.
        """
        principal = current_principal() or TRUSTED_LOCAL
        configured = schedule_projects or None
        if principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}:
            if principal.project_id is None:
                if principal.elevated:
                    return configured, None
                return None, _error("out_of_scope", "a project-scoped principal is required")
            if configured is not None and principal.project_id not in configured:
                return None, _error(
                    "out_of_scope",
                    "this project is not part of the configured digest selection",
                )
            return (principal.project_id,), None
        return configured, None

    async def _known_project_ids(self) -> frozenset[str]:
        return frozenset(project.id for project in await self.db.list_projects())

    async def _reported_so_far(self, destination: str) -> tuple[frozenset[str], frozenset[str]]:
        """Fact keys and highlight wordings earlier windows already reported.

        Without this a preview would happily re-offer a highlight the channel
        has already seen, and disagree with the delivery it claims to predict.
        """
        keys: set[str] = set()
        highlights: set[str] = set()
        for window in await self.db.list_digest_windows(
            destination=destination, statuses=["sent"], limit=_RECENT_WINDOWS
        ):
            payload = window.get("payload") or {}
            if isinstance(payload, dict):
                keys.update(payload.get("reported_keys") or [])
                highlights.update(payload.get("reported_highlights") or [])
        return frozenset(keys), frozenset(highlights)

    async def _cmd_digest_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        """Dry-run the current digest window. Sends nothing, reserves nothing."""
        config = self.orchestrator.config.discord
        schedule = schedule_for(config)
        known = await self._known_project_ids()
        settings_errors = validate_settings(config, known)

        scope, error = self._digest_scope(schedule.project_ids)
        if error:
            return error

        now = float(args.get("now") or time.time())
        windows = await self.db.list_digest_windows(destination=schedule.destination, limit=1)
        last_end = float(windows[0]["window_end"]) if windows else None
        window = schedule.window_for(now, last_window_end=last_end)

        reported_keys, reported_highlights = await self._reported_so_far(schedule.destination)
        open_escalations = len(
            await self.db.list_escalations(states=["needs_human", "reply_received"], limit=500)
        )
        inputs = await self.db.collect_digest_activity(
            window,
            now=now,
            project_ids=scope,
            open_escalations=open_escalations,
            reported_keys=reported_keys,
            reported_highlights=reported_highlights,
        )
        categories = frozenset(c for c in schedule.categories if c in CATEGORIES)
        result = build_digest(
            inputs,
            project_ids=frozenset(scope) if scope else None,
            categories=categories or None,
            dashboard_url=args.get("dashboard_url", "") or "",
        )
        return {
            "success": True,
            "destination": schedule.destination,
            "config_generation": schedule.generation,
            "enabled": schedule.enabled,
            "window": {
                "since": window.since,
                "until": window.until,
                "catchup": window.catchup,
            },
            "would_send": result.send,
            "reason": result.reason,
            "text": result.text,
            "completed_count": result.completed_count,
            "active_count": result.active_count,
            "idle_tasks": inputs.idle_tasks,
            "open_escalations": open_escalations,
            "suppression_reason": None if result.send else result.reason,
            "settings_errors": settings_errors,
            "warnings": config.warnings(),
        }

    async def _cmd_digest_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Configured schedule, next evaluation and delivery health."""
        config = self.orchestrator.config.discord
        schedule = schedule_for(config)
        known = await self._known_project_ids()

        _, error = self._digest_scope(schedule.project_ids)
        if error:
            return error

        now = float(args.get("now") or time.time())
        recent = await self.db.list_digest_windows(
            destination=schedule.destination, limit=_RECENT_WINDOWS
        )
        last_end = float(recent[0]["window_end"]) if recent else None
        health = {status: 0 for status in _ATTENTION_STATUSES}
        for window in await self.db.list_digest_windows(
            destination=schedule.destination, statuses=list(_ATTENTION_STATUSES), limit=500
        ):
            health[window["send_status"]] = health.get(window["send_status"], 0) + 1

        escalations = await self.db.list_escalations(
            states=["needs_human", "reply_received", "resolving"], limit=500
        )
        pending_escalation_deliveries = 0
        for incident in escalations:
            deliveries = await self.db.list_escalation_deliveries(incident["id"])
            pending_escalation_deliveries += sum(
                1 for row in deliveries if row["status"] in _PENDING_DELIVERY_STATUSES
            )

        return {
            "success": True,
            "destination": schedule.destination,
            "config_generation": schedule.generation,
            "channel_id": config.channel_id,
            "digest": {
                "enabled": config.digest.enabled,
                "interval_minutes": config.digest.interval_minutes,
                "project_ids": list(schedule.project_ids),
                "categories": sorted(schedule.categories),
                "catchup_hours": config.digest.catchup_hours,
            },
            "escalation": {
                "enabled": config.escalation.enabled,
                "mention_user_ids": list(config.escalation.mention_user_ids),
                "mention_role_ids": list(config.escalation.mention_role_ids),
                "reminder_minutes": config.escalation.reminder_minutes,
                "supervisor_delivery_timeout_minutes": (
                    config.escalation.supervisor_delivery_timeout_minutes
                ),
            },
            "next_evaluation_at": schedule.next_evaluation_at(now, last_window_end=last_end),
            "last_window_end": last_end,
            "recent_windows": [
                {
                    "id": window["id"],
                    "window_start": window["window_start"],
                    "window_end": window["window_end"],
                    "send_status": window["send_status"],
                    "suppression_reason": window["suppression_reason"],
                    "is_catchup": window["is_catchup"],
                    "config_generation": window["config_generation"],
                    "attempt_count": window["attempt_count"],
                }
                for window in recent
            ],
            "delivery_health": health,
            "open_escalations": len(escalations),
            "pending_escalation_deliveries": pending_escalation_deliveries,
            "settings_errors": validate_settings(config, known),
            "warnings": config.warnings(),
        }
