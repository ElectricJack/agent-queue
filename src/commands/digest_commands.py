"""Digest preview, status, and the §4 supervisor-authored window.

Four named commands, no dashboard-only business logic:

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
    deliveries or escalation deliveries are pending, unknown or failed.  Its
    ``intake`` block counts the inbound Discord messages the gateway ignored
    in the last hour, by reason code.

``digest_facts`` / ``digest_post`` / ``digest_request``
    Phase P3 of *Discord as a chat extension of the supervisor* (§4): the
    daemon keeps the window, freezes the facts and holds it; the supervisor
    reads those facts and posts three sentences.  ``digest_facts`` is the
    bounded read of one window's frozen evidence, ``digest_post`` the single
    CAS write of its body (rendered inside the §3.2 budget with the needs-you
    link appended by the daemon), and ``digest_request`` the minute
    reconciliation that hands held windows to the supervisor's inbox.  All
    three exist only while ``discord.digest.supervisor_authored`` is on.

``digest_preview`` and ``digest_status`` are installation-wide reads of one
shared destination, so a project-scoped session principal only ever sees its
own project's activity; the configured project filter is applied before any row
is read.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.digest.aggregate import build_digest
from src.digest.dispatch import reported_so_far
from src.digest.facts import CATEGORIES
from src.digest.schedule import provider_facts_enabled, schedule_for, validate_settings
from src.digest.supervisor import render_authored_digest
from src.discord.intake_diagnostics import empty_snapshot

logger = logging.getLogger(__name__)

#: Window statuses that mean "the operator should look at this".
_ATTENTION_STATUSES = ("pending", "sending", "retry", "unknown")
_PENDING_DELIVERY_STATUSES = frozenset({"pending", "sending", "retry", "unknown"})

#: How many recent windows ``digest_status`` reports, and how far back a
#: preview looks for wording it has already said.
_RECENT_WINDOWS = 20

#: A body that carries its own link is refused: §3.1 gives every post exactly
#: one link, and the daemon appends the needs-you page itself.
_BODY_URL = re.compile(r"https?://\S+", re.IGNORECASE)


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

    async def _reported_so_far(
        self, destination: str, generation: int
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Fact keys and highlight wordings earlier windows already reported.

        Without this a preview would happily re-offer a highlight the channel
        has already seen, and disagree with the delivery it claims to predict.
        The dispatcher reads the same generation-bounded history through the
        same helper.
        """
        return await reported_so_far(
            self.db,
            destination,
            generation=generation,
            limit=_RECENT_WINDOWS,
        )

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
        windows = await self.db.list_digest_windows(
            destination=schedule.destination, config_generation=schedule.generation, limit=1
        )
        last_end = float(windows[0]["window_end"]) if windows else None
        window = schedule.window_for(now, last_window_end=last_end)

        reported_keys, reported_highlights = await self._reported_so_far(
            schedule.destination, schedule.generation
        )
        open_escalations = len(
            await self.db.list_escalations(
                project_ids=list(scope) if scope is not None else None,
                states=["needs_human", "reply_received"],
                limit=500,
            )
        )
        inputs = await self.db.collect_digest_activity(
            window,
            now=now,
            project_ids=scope,
            open_escalations=open_escalations,
            reported_keys=reported_keys,
            reported_highlights=reported_highlights,
            provider_facts=provider_facts_enabled(self.orchestrator.config),
        )
        categories = frozenset(c for c in schedule.categories if c in CATEGORIES)
        # The same resolver a new delivery renders with, so the preview's
        # footer is the delivery's.  An explicit ``dashboard_url`` remains a
        # caller's what-if override.
        dashboard_url = str(args.get("dashboard_url") or "")
        dashboard_notice = ""
        if not dashboard_url:
            resolver = getattr(self.orchestrator, "dashboard_links", None)
            if resolver is not None:
                link = await resolver.resolve()
                dashboard_url, dashboard_notice = link.url, link.unavailable_notice
        result = build_digest(
            inputs,
            project_ids=frozenset(scope) if scope else None,
            categories=categories or None,
            dashboard_url=dashboard_url,
            dashboard_notice=dashboard_notice,
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

        scope, error = self._digest_scope(schedule.project_ids)
        if error:
            return error

        now = float(args.get("now") or time.time())
        recent = await self.db.list_digest_windows(
            destination=schedule.destination,
            config_generation=schedule.generation,
            limit=_RECENT_WINDOWS,
        )
        last_end = float(recent[0]["window_end"]) if recent else None
        health = {status: 0 for status in _ATTENTION_STATUSES}
        for window in await self.db.list_digest_windows(
            destination=schedule.destination,
            config_generation=schedule.generation,
            statuses=list(_ATTENTION_STATUSES),
            limit=500,
        ):
            health[window["send_status"]] = health.get(window["send_status"], 0) + 1

        escalations = await self.db.list_escalations(
            project_ids=list(scope) if scope is not None else None,
            states=["needs_human", "reply_received", "resolving"],
            limit=500,
        )
        pending_escalation_deliveries = 0
        for incident in escalations:
            deliveries = await self.db.list_escalation_deliveries(incident["id"])
            pending_escalation_deliveries += sum(
                1 for row in deliveries if row["status"] in _PENDING_DELIVERY_STATUSES
            )

        bot = getattr(self.orchestrator, "_discord_bot", None)
        cutover = getattr(bot, "_cutover_report", None)
        cutover_status = cutover.as_dict() if cutover is not None else None
        warnings = list(config.warnings())
        if cutover is not None and cutover.status != "complete":
            warnings.append(cutover.summary())
        # In-memory and installation-wide: codes and counts only, no project data.
        diagnostics = getattr(bot, "_intake_diagnostics", None)
        intake = diagnostics.snapshot() if diagnostics is not None else empty_snapshot()

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
                "supervisor_authored": schedule.supervisor_authored,
                "cadence_minutes": config.digest.cadence_minutes,
                "author_fallback_minutes": config.digest.author_fallback_minutes,
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
            "cutover": cutover_status,
            "intake": intake,
            "settings_errors": validate_settings(config, known),
            "warnings": warnings,
        }

    # -- §4 the supervisor-authored window --------------------------------
    def _digest_author_allowed(self) -> bool:
        """Who may read a window's facts and post its body.

        The author is the supervisor: the live global launch, or an install-wide
        service/playbook principal (the playbook's own reconciliation, and an
        operator reading along).  A *project*-scoped session is refused rather
        than narrowed -- the brief is the fleet's frozen evidence for a window the
        whole destination shares, and there is no honest per-project projection
        of it at read time.
        """
        principal = current_principal()
        if principal is None or principal.kind == PrincipalKind.LOCAL:
            return True
        if principal.kind in (PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK):
            return principal.project_id is None
        return bool(
            principal.kind == PrincipalKind.SESSION
            and principal.elevated
            and principal.project_id is None
        )

    async def _digest_window_for(self, args: dict[str, Any]) -> tuple[Any, Any] | None:
        """The window and author request this call names.

        ``--since`` is how the supervisor reads a window: the wake message names
        the start, and the start is the window's identity inside its generation.
        With no ``--since`` the newest evaluated window answers, so a recovery
        read does not need the number.
        """
        schedule = schedule_for(self.orchestrator.config.discord)
        if args.get("since") is None:
            window = await self.db.latest_digest_window(
                destination=schedule.destination, generation=schedule.generation
            )
            if window is None:
                return None
            return await self.db.find_digest_request(
                destination=schedule.destination,
                generation=schedule.generation,
                window_start=float(window["window_start"]),
            )
        try:
            start = float(args["since"])
        except (TypeError, ValueError):
            return None
        return await self.db.find_digest_request(
            destination=schedule.destination, generation=schedule.generation, window_start=start
        )

    async def _cmd_digest_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        """The frozen facts for one window, exactly as the author is woken."""
        if not self._digest_author_allowed():
            return _error(
                "out_of_scope",
                "digest facts are the supervisor's author turn; a project-scoped "
                "session cannot read the fleet window",
            )
        schedule = schedule_for(self.orchestrator.config.discord)
        if not schedule.supervisor_authored:
            return _error(
                "digest.disabled",
                "discord.digest.supervisor_authored is off; the deterministic digest "
                "is what posts and it has no author window",
            )
        found = await self._digest_window_for(args)
        if found is None:
            return _error("not_found", "no digest window at that start in this generation")
        window, request = found
        if not request:
            return _error(
                "not_found",
                "that window was not held for an author: it was suppressed, already "
                "posted, or predates supervisor authoring",
            )
        return {
            "success": True,
            "request_id": request["id"],
            "window_id": window["id"],
            "state": request["state"],
            "deadline": float(request["deadline"]),
            "seconds_remaining": max(
                0.0, float(request["deadline"]) - float(args.get("now") or time.time())
            ),
            "facts": dict(request["brief"] or {}),
            "facts_hash": request["brief_hash"],
        }

    async def _cmd_digest_post(self, args: dict[str, Any]) -> dict[str, Any]:
        """Submit the supervisor's three sentences for one held window."""
        if not self._digest_author_allowed():
            return _error(
                "out_of_scope", "only the supervisor author, or an install-wide service, may post"
            )
        schedule = schedule_for(self.orchestrator.config.discord)
        if not schedule.supervisor_authored:
            return _error(
                "digest.disabled",
                "discord.digest.supervisor_authored is off; there is no author window to post",
            )
        if args.get("window") is None:
            return _error("digest.invalid", "window is required: pass the window start")
        found = await self._digest_window_for(args)
        if found is None:
            return _error("not_found", "no digest window at that start in this generation")
        window, request = found
        if not request:
            return _error("not_found", "that window was never held for an author")
        if request["state"] not in ("reserved", "requested"):
            # ``submitted`` is a window already posted; ``fallback`` and
            # ``cancelled`` are windows the daemon owns.  None of them is this
            # author's to write, and a window posts exactly once.
            return _error(
                "digest.closed",
                f"window {window['id']} is {request['state']}; a window posts once and is "
                "never edited afterwards",
            )
        body = str(args.get("body") or "")
        if not body.strip():
            return _error("digest.invalid", "digest body is empty")
        if _BODY_URL.search(body):
            return _error("digest.invalid", "digest links are inserted by the daemon")
        dashboard_url, dashboard_notice = "", ""
        resolver = getattr(self.orchestrator, "dashboard_links", None)
        if resolver is not None:
            link = await resolver.resolve()
            dashboard_url, dashboard_notice = link.url, link.unavailable_notice
        try:
            text = render_authored_digest(body, base_url=dashboard_url, notice=dashboard_notice)
        except ValueError as exc:
            return _error("digest.invalid", str(exc))
        changed = await self.db.submit_digest_post(request["id"], text=text, now=time.time())
        if changed is None:
            return _error(
                "digest.closed",
                "the window is no longer yours: it was claimed for delivery or its deadline passed",
            )
        return {
            "success": True,
            "request_id": changed["id"],
            "window_id": window["id"],
            "state": changed["state"],
            "version": changed["version"],
            "text": text,
            "characters": len(text),
        }

    async def _cmd_digest_request(self, args: dict[str, Any]) -> dict[str, Any]:
        """Reconcile held digest windows into supervisor author turns (service only)."""
        principal = current_principal()
        if not (
            principal
            and principal.kind in (PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK)
            and principal.project_id is None
        ):
            return _error(
                "out_of_scope", "only the install-wide digest service/playbook may request"
            )
        now = float(args.get("now") or time.time())
        schedule = schedule_for(self.orchestrator.config.discord)
        if not (schedule.enabled and schedule.supervisor_authored):
            # The flag went off with windows still held: release them to the
            # deterministic digest rather than leaving them silent until a
            # cadence that no longer exists.
            return {
                "success": True,
                "requested": 0,
                "cancelled": await self.db.cancel_digest_requests(now=now),
            }
        requested = 0
        for row in await self.db.list_reserved_digest_requests(now=now):
            if await self.db.request_report(str(row["id"]), now=now) is not None:
                requested += 1
        return {"success": True, "requested": requested, "cancelled": 0}
