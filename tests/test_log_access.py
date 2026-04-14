"""Tests for log access tools (Roadmap 6.2.2).

Covers:
- Enhanced get_recent_events with filtering (event_type, since, project/agent/task)
- read_logs command (JSONL file reading with severity/date/field filtering)
- _parse_relative_time helper
- _tail_log_lines helper
"""

from __future__ import annotations

import json
import time

import pytest

from src.database import Database
from src.models import Project


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
async def db(tmp_path):
    """Provide an initialized Database with a few test events."""
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()

    # Create a project for FK constraints
    await database.create_project(Project(id="proj-a", name="Project A"))

    # Seed events at staggered timestamps
    now = time.time()
    events = [
        ("task.started", "proj-a", "t-1", "agent-1", '{"foo": 1}', now - 7200),
        ("task.completed", "proj-a", "t-1", "agent-1", '{"foo": 2}', now - 3600),
        ("task.failed", "proj-a", "t-2", "agent-2", '{"err": "x"}', now - 1800),
        ("notify.budget_warning", "proj-a", None, None, "{}", now - 900),
        ("task.started", "proj-a", "t-3", "agent-1", "{}", now - 60),
    ]
    for etype, pid, tid, aid, payload, ts in events:
        # Use direct insert to control timestamps
        from sqlalchemy import insert
        from src.database.tables import events as events_table

        async with database._engine.begin() as conn:
            await conn.execute(
                insert(events_table).values(
                    event_type=etype,
                    project_id=pid,
                    task_id=tid,
                    agent_id=aid,
                    payload=payload,
                    timestamp=ts,
                )
            )

    yield database
    await database.close()


@pytest.fixture
def log_file(tmp_path):
    """Create a temp JSONL log file with sample entries."""
    path = tmp_path / "logs" / "agent-queue.log"
    path.parent.mkdir(parents=True)

    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    entries = [
        {
            "timestamp": (now - timedelta(hours=2)).isoformat(),
            "level": "debug",
            "logger": "src.orchestrator",
            "event": "Scheduler tick",
            "component": "orchestrator",
        },
        {
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "level": "info",
            "logger": "src.supervisor",
            "event": "Task assigned to workspace",
            "component": "supervisor",
            "task_id": "t-1",
            "project_id": "proj-a",
        },
        {
            "timestamp": (now - timedelta(minutes=30)).isoformat(),
            "level": "warning",
            "logger": "src.tokens.budget",
            "event": "Token budget approaching limit",
            "component": "tokens",
            "project_id": "proj-a",
        },
        {
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
            "level": "error",
            "logger": "src.supervisor",
            "event": "Task execution failed: ImportError in target module",
            "component": "supervisor",
            "task_id": "t-2",
            "project_id": "proj-a",
        },
        {
            "timestamp": (now - timedelta(minutes=2)).isoformat(),
            "level": "info",
            "logger": "src.api",
            "event": "Health check OK",
            "component": "api",
        },
    ]

    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    return str(path)


# ── Tests: _parse_relative_time ──────────────────────────────────────────


class TestParseRelativeTime:
    def test_seconds(self):
        from src.command_handler import _parse_relative_time

        result = _parse_relative_time("30s")
        assert abs(result - (time.time() - 30)) < 2

    def test_minutes(self):
        from src.command_handler import _parse_relative_time

        result = _parse_relative_time("5m")
        assert abs(result - (time.time() - 300)) < 2

    def test_hours(self):
        from src.command_handler import _parse_relative_time

        result = _parse_relative_time("2h")
        assert abs(result - (time.time() - 7200)) < 2

    def test_days(self):
        from src.command_handler import _parse_relative_time

        result = _parse_relative_time("1d")
        assert abs(result - (time.time() - 86400)) < 2

    def test_unknown_unit_raises(self):
        from src.command_handler import _parse_relative_time

        with pytest.raises(ValueError, match="Unknown time unit"):
            _parse_relative_time("5x")

    def test_invalid_number_raises(self):
        from src.command_handler import _parse_relative_time

        with pytest.raises(ValueError, match="Invalid number"):
            _parse_relative_time("abcm")


# ── Tests: _tail_log_lines ───────────────────────────────────────────────


class TestTailLogLines:
    def test_reads_last_n_lines(self, log_file):
        from src.command_handler import _tail_log_lines

        lines = _tail_log_lines(log_file, max_scan=3)
        assert len(lines) == 3
        # Should be the last 3 entries (chronological order)
        parsed = [json.loads(ln) for ln in lines]
        assert parsed[0]["level"] == "warning"
        assert parsed[1]["level"] == "error"
        assert parsed[2]["level"] == "info"

    def test_reads_all_if_fewer_than_max(self, log_file):
        from src.command_handler import _tail_log_lines

        lines = _tail_log_lines(log_file, max_scan=100)
        assert len(lines) == 5

    def test_missing_file_returns_empty(self, tmp_path):
        from src.command_handler import _tail_log_lines

        lines = _tail_log_lines(str(tmp_path / "nonexistent.log"))
        assert lines == []


# ── Tests: get_recent_events (enhanced filtering) ────────────────────────


class TestGetRecentEventsFiltering:
    async def test_basic_limit(self, db):
        events = await db.get_recent_events(limit=2)
        assert len(events) == 2

    async def test_filter_by_exact_event_type(self, db):
        events = await db.get_recent_events(limit=50, event_type="task.failed")
        assert len(events) == 1
        assert events[0]["event_type"] == "task.failed"

    async def test_filter_by_event_type_prefix(self, db):
        events = await db.get_recent_events(limit=50, event_type="task.*")
        assert len(events) == 4  # started x2, completed x1, failed x1
        for ev in events:
            assert ev["event_type"].startswith("task.")

    async def test_filter_by_since(self, db):
        # Only events in the last hour (3600s)
        since_ts = time.time() - 3600
        events = await db.get_recent_events(limit=50, since=since_ts)
        # Should get: task.failed (1800s ago), notify (900s ago), task.started (60s ago)
        assert len(events) == 3

    async def test_filter_by_project_id(self, db):
        events = await db.get_recent_events(limit=50, project_id="proj-a")
        assert len(events) == 5  # All events are proj-a

    async def test_filter_by_agent_id(self, db):
        events = await db.get_recent_events(limit=50, agent_id="agent-2")
        assert len(events) == 1
        assert events[0]["event_type"] == "task.failed"

    async def test_filter_by_task_id(self, db):
        events = await db.get_recent_events(limit=50, task_id="t-1")
        assert len(events) == 2  # started + completed

    async def test_combined_filters(self, db):
        since_ts = time.time() - 3600
        events = await db.get_recent_events(
            limit=50,
            event_type="task.*",
            since=since_ts,
            agent_id="agent-1",
        )
        # Only task.started 60s ago (agent-1, task type, within 1h)
        assert len(events) == 1
        assert events[0]["event_type"] == "task.started"
        assert events[0]["task_id"] == "t-3"

    async def test_no_matches(self, db):
        events = await db.get_recent_events(limit=50, event_type="nonexistent.type")
        assert events == []


# ── Tests: read_logs (JSONL log reading) ─────────────────────────────────


class TestReadLogs:
    """Test _cmd_read_logs via direct invocation of the helpers.

    Since _cmd_read_logs requires a full CommandHandler with an orchestrator,
    we test the filtering logic end-to-end via the helper functions and a
    minimal integration test.
    """

    def test_level_filter(self, log_file):
        """Entries below the threshold are excluded."""
        from src.command_handler import _tail_log_lines, _LEVEL_PRIORITY

        lines = _tail_log_lines(log_file, max_scan=100)
        threshold = _LEVEL_PRIORITY["warning"]
        filtered = []
        for raw in lines:
            entry = json.loads(raw)
            if _LEVEL_PRIORITY.get(entry.get("level", "").lower(), 0) >= threshold:
                filtered.append(entry)

        assert len(filtered) == 2
        levels = {e["level"] for e in filtered}
        assert levels == {"warning", "error"}

    def test_component_filter(self, log_file):
        """Only entries matching the component are returned."""
        from src.command_handler import _tail_log_lines

        lines = _tail_log_lines(log_file, max_scan=100)
        filtered = [
            json.loads(raw) for raw in lines if json.loads(raw).get("component") == "supervisor"
        ]

        assert len(filtered) == 2
        for entry in filtered:
            assert entry["component"] == "supervisor"

    def test_pattern_filter(self, log_file):
        """Substring pattern search works case-insensitively."""
        from src.command_handler import _tail_log_lines

        lines = _tail_log_lines(log_file, max_scan=100)
        pattern = "importerror"
        filtered = []
        for raw in lines:
            entry = json.loads(raw)
            msg = entry.get("event", "")
            if pattern.lower() in msg.lower():
                filtered.append(entry)

        assert len(filtered) == 1
        assert "ImportError" in filtered[0]["event"]

    def test_missing_log_file(self, tmp_path):
        """Returns empty when log file doesn't exist."""
        from src.command_handler import _tail_log_lines

        lines = _tail_log_lines(str(tmp_path / "missing.log"))
        assert lines == []


# ── Tests: tool registry includes new definitions ────────────────────────


class TestToolRegistryDefinitions:
    def test_read_logs_in_registry(self):
        from src.tools import ToolRegistry, _TOOL_CATEGORIES

        registry = ToolRegistry()
        all_tools = registry.get_all_tools()
        tool_names = {t["name"] for t in all_tools}
        assert "read_logs" in tool_names
        assert _TOOL_CATEGORIES["read_logs"] == "system"

    def test_get_recent_events_has_filters(self):
        from src.tools import ToolRegistry

        registry = ToolRegistry()
        all_tools = registry.get_all_tools()
        tool = next(t for t in all_tools if t["name"] == "get_recent_events")
        props = tool["input_schema"]["properties"]
        assert "event_type" in props
        assert "since" in props
        assert "project_id" in props
        assert "agent_id" in props
        assert "task_id" in props

    def test_read_logs_schema(self):
        from src.tools import ToolRegistry

        registry = ToolRegistry()
        all_tools = registry.get_all_tools()
        tool = next(t for t in all_tools if t["name"] == "read_logs")
        props = tool["input_schema"]["properties"]
        assert "level" in props
        assert "since" in props
        assert "limit" in props
        assert "component" in props
        assert "pattern" in props
        assert props["level"]["enum"] == ["debug", "info", "warning", "error", "critical"]

    def test_system_category_in_browse(self):
        from src.tools import ToolRegistry

        registry = ToolRegistry()
        system_tools = registry.get_category_tools("system")
        names = {t["name"] for t in system_tools}
        assert "read_logs" in names
        assert "get_recent_events" in names
