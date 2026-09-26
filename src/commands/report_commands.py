"""Report request, paged brief and supervisor submission commands."""

from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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
    async def _cmd_morning_report_tick(self, args: dict[str, Any]) -> dict[str, Any]:
        """Reserve/recover one zoned daily snapshot; finalize expired fallback."""
        from src.reports.fallback import build_fallback
        from src.reports.morning import collect_morning_evidence
        from src.reports.schedule import next_due, planned_at

        principal = current_principal()
        if principal is None or principal.kind not in (
            PrincipalKind.SERVICE,
            PrincipalKind.PLAYBOOK,
        ):
            return _error("out_of_scope", "only the report service or system playbook may tick")
        if principal.project_id is not None:
            return _error("out_of_scope", "the morning schedule is install-wide")
        config = self.orchestrator.config.reports
        errors = config.validate()
        if errors:
            return _error("report.invalid", "; ".join(str(error) for error in errors))
        now = float(args["now"]) if args.get("now") is not None else time.time()
        if not math.isfinite(now):
            return _error("report.invalid", "now must be finite")
        if not config.morning.enabled:
            cancelled = await self.db.cancel_pending_morning_reports(now=now)
            return {
                "success": True,
                "report_id": None,
                "state": "disabled",
                "reason": "disabled",
                "next_due_at": None,
                "cancelled": cancelled,
            }
        snapshot = {**asdict(config.morning), "timezone": config.timezone}
        day = datetime.fromtimestamp(now, ZoneInfo(config.timezone)).date()
        planned = planned_at(day, config.morning.time, config.timezone)
        due = next_due(now, config.morning.time, config.timezone)
        pending = await self.db.list_morning_reports(
            limit=100, states=("building", "failed", "ready", "authoring")
        )
        row = None
        reason = "before_schedule"
        if now >= planned:
            row, reason = await self.db.reserve_morning_report(
                local_date=day.isoformat(), planned_at=planned, config=snapshot, now=now
            )
            if row and all(item["id"] != row["id"] for item in pending):
                pending.append(row)
            if reason == "timezone_guard":
                from src.reports.schedule import ZONE_CHANGE_GUARD_SECONDS

                latest = await self.db.latest_morning_reservation()
                due = next_due(
                    now,
                    config.morning.time,
                    config.timezone,
                    after=latest["created_at"] + ZONE_CHANGE_GUARD_SECONDS,
                )
        for candidate in sorted(pending, key=lambda item: item["planned_at"]):
            if candidate["state"] in ("building", "failed"):
                owner = uuid.uuid4().hex
                claimed = await self.db.claim_morning_build(candidate["id"], owner=owner, now=now)
                if claimed:
                    context = claimed["build_context"]
                    try:
                        result = await collect_morning_evidence(
                            self.db,
                            self.orchestrator.git,
                            window=context["window"],
                            now=claimed["created_at"],
                            project_ids=tuple(claimed["config_snapshot"]["project_ids"]) or None,
                            previous_heads=context["previous_heads"],
                            reported_keys=frozenset(context["reported_keys"]),
                        )
                        selected = set(claimed["config_snapshot"]["project_ids"])
                        if selected and selected != {p["id"] for p in result["brief"]["projects"]}:
                            raise ValueError("unknown configured project selection")
                        await self.db.store_morning_build(
                            claimed["id"],
                            owner=owner,
                            result=result,
                            fallback=build_fallback(result["brief"]),
                            now=now,
                        )
                    except Exception:
                        await self.db.fail_morning_build(claimed["id"], owner=owner)
            await self.db.finalize_morning_fallback(candidate["id"], now=now)
        await self.db.prune_morning_reports(now=now)
        if row:
            row = await self.db.get_morning_report(row["id"])
        return {
            "success": True,
            "report_id": row["id"] if row else None,
            "state": row["state"] if row else "waiting",
            "reason": row["reason"] if row else reason,
            "next_due_at": due,
            "cancelled": 0,
        }

    def _morning_read_project(self) -> str | None:
        principal = current_principal()
        return principal.project_id if principal and principal.enforced else None

    def _morning_read_allowed(self, row: dict) -> bool:
        principal = current_principal()
        if principal and principal.enforced and not principal.project_id and not principal.elevated:
            return False
        project_id = self._morning_read_project()
        if project_id:
            projects = (row.get("brief") or {}).get("projects", [])
            configured = row["config_snapshot"]["project_ids"]
            return project_id in (configured or [project["id"] for project in projects])
        return True

    def _morning_read_value(self, row: dict) -> dict:
        import copy

        content = copy.deepcopy(row["report"] or row["fallback"])
        project_id = self._morning_read_project()
        if content and project_id:
            content["projects"] = [p for p in content["projects"] if p["id"] == project_id]
            content["global_facts"] = []
            content["summary"] = f"Morning report for {project_id}."
            content["coverage"]["gaps"] = [
                gap
                for gap in content["coverage"]["gaps"]
                if (not gap["source"].startswith("git:") or gap["source"] == f"git:{project_id}")
                and gap["source"] not in ("providers", "digests")
            ]
        return {
            "id": row["id"],
            "state": row["state"],
            "reason": row["reason"],
            "local_date": row["local_date"],
            "timezone": row["timezone"],
            "planned_at": row["planned_at"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "brief_hash": row["brief_hash"],
            "created_at": row["created_at"],
            "finalized_at": row["finalized_at"],
            "author_deadline": row["author_deadline"],
            "report": content,
            "is_fallback": row["report"] is None
            or row["reason"] in ("author_deadline", "no_changes"),
        }

    async def _cmd_report_get(self, args: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.get_morning_report(str(args.get("report_id") or ""))
        if row is None or not self._morning_read_allowed(row):
            return _error("not_found", "report not found in this project scope")
        return {"success": True, "report": self._morning_read_value(row)}

    async def _cmd_report_list(self, args: dict[str, Any]) -> dict[str, Any]:
        offset, limit = int(args.get("offset", 0)), int(args.get("limit", 50))
        if offset < 0 or not 1 <= limit <= 100:
            return _error("invalid_pagination", "offset must be nonnegative and limit 1–100")
        rows = await self.db.list_morning_reports(
            limit=limit, offset=offset, project_id=self._morning_read_project()
        )
        return {
            "success": True,
            "reports": [
                self._morning_read_value(row) for row in rows if self._morning_read_allowed(row)
            ],
        }

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
                else preview_until(now, config.timezone, config.morning.time)
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
