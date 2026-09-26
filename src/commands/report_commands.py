"""Report request, paged brief and supervisor submission commands."""

from __future__ import annotations

import math
import re
import time
from typing import Any

from src.commands.principal import PrincipalKind, current_principal
from src.digest.dispatch import marker_for
from src.digest.render import MAX_CHARS, sanitise
from src.digest.schedule import schedule_for

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SHIP_ASSERTION = re.compile(r"\b(?:shipped|landed|released to main)\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message}


def _clean_prose(raw: str) -> str:
    without_controls = _CONTROL.sub("", raw)
    return "\n".join(filter(None, (sanitise(line) for line in without_controls.splitlines())))


class ReportCommandsMixin:
    async def _cmd_morning_report_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        """Collect evidence without reserving, persisting, waking or sending."""
        from src.reports.morning import collect_morning_evidence, preview_until, report_window

        config = self.orchestrator.config.reports
        principal = current_principal()
        requested = args.get("project_ids")
        if requested is not None and (
            not isinstance(requested, list)
            or not requested
            or len(requested) > 100
            or any(not isinstance(item, str) or not item for item in requested)
        ):
            return _error("report.invalid", "project_ids must contain 1–100 project ids")
        morning = getattr(config, "morning", None)
        configured = tuple(getattr(morning, "project_ids", ()) or ())
        scope = tuple(sorted(set(requested))) if requested is not None else configured or None
        if configured and scope is not None and not set(scope) <= set(configured):
            return _error("out_of_scope", "project selection exceeds configured visibility")
        if principal is not None and principal.kind in (
            PrincipalKind.SESSION,
            PrincipalKind.PLAYBOOK,
        ):
            if principal.project_id is not None:
                if (scope is not None and principal.project_id not in scope) or (
                    requested is not None and set(requested) != {principal.project_id}
                ):
                    return _error("out_of_scope", "preview belongs to another project")
                scope = (principal.project_id,)
            elif not principal.elevated:
                return _error("out_of_scope", "a project-scoped principal is required")
        try:
            now = float(args["now"]) if args.get("now") is not None else time.time()
            if not math.isfinite(now):
                raise ValueError("now must be finite")
            until = (
                float(args["until"])
                if args.get("until") is not None
                else preview_until(now, config.timezone)
            )
            since = float(args["since"]) if args.get("since") is not None else None
            window = report_window(since, until, int(args.get("max_lookback_hours", 72)))
            result = await collect_morning_evidence(
                self.db, self.orchestrator.git, window=window, now=now, project_ids=scope
            )
        except (TypeError, ValueError, OverflowError):
            return _error("report.invalid", "invalid UTC window or report settings")
        except Exception:
            return _error("report.source_unavailable", "could not establish the read-only snapshot")
        if scope is not None and set(scope) != {row["id"] for row in result["brief"]["projects"]}:
            return _error("report.invalid", "unknown configured project selection")
        return {"success": True, **result}

    async def _report_reader_allowed(self, row: dict[str, Any]) -> bool:
        principal = current_principal()
        if principal is None or principal.kind == PrincipalKind.LOCAL:
            return True
        if (
            principal.kind != PrincipalKind.SESSION
            or principal.session_id != row["author_session_id"]
        ):
            return False
        session = await self.db.get_session(principal.session_id)
        return bool(
            session is not None
            and session.instance_token == principal.session_instance_token
            and session.id == "supervisor-global"
            and session.lifecycle == "named"
        )

    async def _cmd_report_request(self, args: dict[str, Any]) -> dict[str, Any]:
        """Queue a single durable supervisor wake for a reserved request."""
        principal = current_principal()
        if principal is None or principal.kind != PrincipalKind.SERVICE:
            return _error("out_of_scope", "only the report service may request an author turn")
        request_id = str(args.get("request_id") or "")
        row = await self.db.request_report(request_id, now=time.time())
        if row is None:
            return _error("report.closed", "report request is missing or closed")
        return {
            "success": True,
            "request_id": request_id,
            "state": row["state"],
            "message_id": row["request_message_id"],
            "deadline": row["deadline"],
        }

    async def _cmd_report_brief(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = str(args.get("request_id") or "")
        row = await self.db.get_report_request(request_id)
        if row is None:
            return _error("not_found", "report request not found")
        if not await self._report_reader_allowed(row):
            return _error("out_of_scope", "this report belongs to another supervisor launch")
        offset = int(args.get("offset") or 0)
        limit = int(args.get("limit") or 20)
        if offset < 0 or not 1 <= limit <= 100:
            return _error("invalid_pagination", "offset must be nonnegative and limit 1–100")
        brief = dict(row["brief"])
        facts = brief.pop("facts", [])
        active = brief.pop("active", [])
        return {
            "success": True,
            "request_id": request_id,
            "state": row["state"],
            "deadline": row["deadline"],
            "version": row["version"],
            "brief_hash": row["brief_hash"],
            "brief": brief,
            "facts": facts[offset : offset + limit],
            "active": active[offset : offset + limit],
            "total_facts": len(facts),
            "total_active": len(active),
        }

    async def _cmd_report_submit(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = str(args.get("request_id") or "")
        row = await self.db.get_report_request(request_id)
        if row is None:
            return _error("report.closed", "report request not found")
        if not await self._report_reader_allowed(row):
            return _error("out_of_scope", "this report belongs to another supervisor launch")
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is not None and row["kind"] == "hourly":
            current = schedule_for(orchestrator.config.discord)
            policy = orchestrator.config.reports.hourly
            if (
                not policy.enabled
                or not policy.full_fleet_visibility
                or current.project_ids
                or current.destination != row["destination"]
            ):
                return _error("report.closed", "report visibility changed")
        raw = str(args.get("text") or "")
        if not raw.strip():
            return _error("report.invalid", "report text is empty")
        if _URL.search(raw):
            return _error("report.invalid", "report links are inserted by the server")
        evidence_refs = args.get("evidence_refs") or []
        if not isinstance(evidence_refs, list) or any(
            not isinstance(ref, str) for ref in evidence_refs
        ):
            return _error("report.invalid", "evidence_refs must be a list of strings")
        allowed = {fact["key"]: fact for fact in row["brief"].get("facts", [])}
        if any(ref not in allowed for ref in evidence_refs):
            return _error("report.invalid", "unknown or out-of-scope evidence reference")
        if _SHIP_ASSERTION.search(raw) and not any(
            ref.startswith("delivery:") for ref in evidence_refs
        ):
            return _error("report.invalid", "shipment claims require delivery evidence")
        text = _clean_prose(raw)
        dashboard_footer = (
            str(row["brief"].get("dashboard_url") or "").strip()
            or str(row["brief"].get("dashboard_notice") or "").strip()
        )
        if dashboard_footer:
            text = f"{text}\n{sanitise(dashboard_footer)}"
        total = f"{text}\n{marker_for(row['owner_ref'])}"
        if not text.strip() or len(total) > MAX_CHARS:
            return _error("report.invalid", "report exceeds 1,200 characters after rendering")
        try:
            expected_version = int(args["expected_version"])
        except (KeyError, TypeError, ValueError):
            return _error("report.invalid", "expected_version is required")
        changed = await self.db.submit_hourly_report(
            request_id,
            brief_hash=str(args.get("brief_hash") or ""),
            expected_version=expected_version,
            text=text,
            evidence_refs=evidence_refs,
            source_links=[allowed[ref]["source_url"] for ref in evidence_refs],
            now=time.time(),
        )
        if changed is None:
            return _error("report.closed", "report request closed, stale or already claimed")
        return {
            "success": True,
            "request_id": request_id,
            "window_id": row["owner_ref"],
            "state": changed["state"],
            "version": changed["version"],
        }
