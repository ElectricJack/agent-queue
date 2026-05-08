"""Event commands mixin — recent events, token usage, log access."""

from __future__ import annotations

import datetime
import json
import logging
import os
import time

from src.commands.helpers import (
    _parse_relative_time,
    _tail_log_lines,
    _LEVEL_PRIORITY,
)

logger = logging.getLogger(__name__)


class EventCommandsMixin:
    """Event command methods mixed into CommandHandler."""

    # -----------------------------------------------------------------------
    # Events and token usage -- observability into system activity and
    # LLM token consumption, broken down by project, task, or agent.
    # -----------------------------------------------------------------------

    async def _cmd_list_event_triggers(self, args: dict) -> dict:
        """List event types that can trigger a playbook.

        Filters out ``notify.*`` (transport-only) and dynamically-generated
        ``timer.*`` / ``cron.*`` types (callers construct those themselves
        via a dedicated picker). Returned entries are grouped by the prefix
        before the first dot so the UI can categorize.
        """
        from src.event_schemas import EVENT_SCHEMAS

        events: list[dict[str, str]] = []
        for name in sorted(EVENT_SCHEMAS.keys()):
            if name.startswith(("notify.", "timer.", "cron.")):
                continue
            category, _, _ = name.partition(".")
            events.append({"name": name, "category": category or "other"})
        return {"events": events, "count": len(events)}

    async def _cmd_get_recent_events(self, args: dict) -> dict:
        limit = args.get("limit", 10)
        event_type = args.get("event_type")
        since = args.get("since")
        project_id = args.get("project_id")
        agent_id = args.get("agent_id")
        task_id = args.get("task_id")

        # Parse relative time strings (e.g. "5m", "1h", "2d") into Unix epoch
        since_ts: float | None = None
        if since:
            since_ts = _parse_relative_time(since)

        events = await self.db.get_recent_events(
            limit=limit,
            event_type=event_type,
            since=since_ts,
            project_id=project_id,
            agent_id=agent_id,
            task_id=task_id,
        )
        return {"events": events}

    async def _cmd_get_token_usage(self, args: dict) -> dict:
        project_id = args.get("project_id")
        task_id = args.get("task_id")

        result = await self.db.get_token_breakdown(task_id=task_id, project_id=project_id)
        if task_id:
            return {"task_id": task_id, **result}
        if project_id:
            return {"project_id": project_id, **result}
        return result

    async def _cmd_token_audit(self, args: dict) -> dict:
        days = args.get("days", 7)
        project_id = args.get("project_id")
        return await self.db.get_token_audit(days=days, project_id=project_id)

    async def _cmd_get_chat_analyzer_metrics(self, args: dict) -> dict:
        """Return aggregated metrics for chat-analyzer suggestion outcomes.

        Phase 8 of the chat-analyzer suggestion-quality overhaul: every
        gate that suppresses a suggestion (confidence, dedup, in-flight,
        dismiss-cooldown) writes a ``status="suppressed"`` row tagged
        with the gate name. This command rolls those rows up alongside
        the regular pending/accepted/dismissed counts so an admin can
        answer "how often is each gate firing? what fraction of posted
        suggestions does the user accept?" without scraping logs.

        Args:
            project_id: Optional project filter. ``None`` (the default)
                aggregates across every project.
            since_hours: Time window. Defaults to ``24``. Pass ``0`` to
                disable the window and return lifetime totals.

        Returns a dict with the per-status counts (``total``, ``pending``,
        ``accepted``, ``dismissed``, ``auto_executed``, ``suppressed``),
        ``accept_rate`` and ``dismiss_rate`` (``None`` when no
        suggestions have been resolved yet), and
        ``suppression_count_by_gate`` keyed by gate label.
        """
        project_id = args.get("project_id")
        since_hours = args.get("since_hours", 24)

        try:
            since_hours_int = int(since_hours)
        except (TypeError, ValueError):
            return {"error": f"since_hours must be an integer, got {since_hours!r}"}

        # since_hours <= 0 → no window (lifetime metrics).
        since_ts: float | None = None
        if since_hours_int > 0:
            since_ts = time.time() - (since_hours_int * 3600)

        stats = await self.db.get_analyzer_suggestion_stats(
            project_id=project_id, since=since_ts
        )

        return {
            "project_id": project_id,
            "since_hours": since_hours_int,
            **stats,
        }

    async def _cmd_read_logs(self, args: dict) -> dict:
        """Read and filter the daemon's JSONL log file.

        Supports filtering by severity level, time range, and context fields.
        Returns parsed log entries as structured dicts.
        """
        level = (args.get("level") or "info").lower()
        since = args.get("since")
        limit = args.get("limit", 100)
        component = args.get("component")
        task_id = args.get("task_id")
        project_id = args.get("project_id")
        pattern = args.get("pattern")

        # Resolve the log file path from config
        log_file = self.config.logging.log_file or os.path.join(
            self.config.data_dir, "logs", "agent-queue.log"
        )

        if not os.path.isfile(log_file):
            return {"error": f"Log file not found: {log_file}"}

        # Build threshold from level
        threshold = _LEVEL_PRIORITY.get(level, 20)

        # Parse relative time if provided
        since_dt = None
        if since:
            try:
                since_ts = _parse_relative_time(since)
                since_dt = datetime.datetime.fromtimestamp(since_ts, tz=datetime.timezone.utc)
            except ValueError as exc:
                return {"error": str(exc)}

        # Read the file from the end (tail) and filter
        raw_lines = _tail_log_lines(log_file, max_scan=limit * 10)

        entries: list[dict] = []
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Level filter (>= threshold)
            entry_level = _LEVEL_PRIORITY.get(entry.get("level", "").lower(), 0)
            if entry_level < threshold:
                continue

            # Time filter
            if since_dt:
                ts = entry.get("timestamp", "")
                try:
                    entry_dt = datetime.datetime.fromisoformat(ts)
                    if entry_dt < since_dt:
                        continue
                except (ValueError, TypeError):
                    pass

            # Context-field exact-match filters
            if component and entry.get("component") != component:
                continue
            if task_id and entry.get("task_id") != task_id:
                continue
            if project_id and entry.get("project_id") != project_id:
                continue

            # Pattern (substring) filter on the event/message field
            if pattern:
                msg = entry.get("event") or entry.get("message", "")
                if pattern.lower() not in msg.lower():
                    continue

            entries.append(entry)
            if len(entries) >= limit:
                break

        return {
            "log_file": log_file,
            "level_filter": level,
            "count": len(entries),
            "entries": entries,
        }
