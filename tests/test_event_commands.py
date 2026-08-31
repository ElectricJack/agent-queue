"""Command-boundary tests for :class:`~src.commands.event_commands.EventCommandsMixin`.

`tests/test_log_access.py` exercises the ``_tail_log_lines`` helper and then
re-implements the filtering in the test — it would still pass if
``_cmd_read_logs`` were replaced with ``pass``.  These tests dispatch the real
commands (test-coverage plan, commands 14–17).
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

import pytest


def _line(**fields) -> str:
    entry = {
        "timestamp": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "level": "info",
        "event": "something happened",
    }
    entry.update(fields)
    return json.dumps(entry)


def _iso(seconds_ago: float) -> str:
    return (
        datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    ).isoformat()


@pytest.fixture
async def log_handler(command_handler_factory, tmp_path):
    handler = await command_handler_factory()
    log_file = tmp_path / "logs" / "agent-queue.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler.config.logging.log_file = str(log_file)
    return handler


def _write_log(handler, lines: list[str]) -> Path:
    path = Path(handler.config.logging.log_file)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 14: read_logs filter intersection at the command boundary
# ---------------------------------------------------------------------------


async def test_read_logs_applies_level_time_context_and_pattern_at_command_boundary(log_handler):
    match = _line(
        level="error",
        event="disk pressure detected",
        component="scheduler",
        task_id="t1",
        project_id="p1",
        timestamp=_iso(60),
    )
    _write_log(
        log_handler,
        [
            match,
            # below the level threshold
            _line(
                level="info",
                event="disk pressure detected",
                component="scheduler",
                task_id="t1",
                project_id="p1",
                timestamp=_iso(60),
            ),
            # outside the time window
            _line(
                level="error",
                event="disk pressure detected",
                component="scheduler",
                task_id="t1",
                project_id="p1",
                timestamp=_iso(7200),
            ),
            # wrong component
            _line(
                level="error",
                event="disk pressure detected",
                component="watcher",
                task_id="t1",
                project_id="p1",
                timestamp=_iso(60),
            ),
            # wrong task
            _line(
                level="error",
                event="disk pressure detected",
                component="scheduler",
                task_id="t2",
                project_id="p1",
                timestamp=_iso(60),
            ),
            # wrong project
            _line(
                level="error",
                event="disk pressure detected",
                component="scheduler",
                task_id="t1",
                project_id="p2",
                timestamp=_iso(60),
            ),
            # pattern miss
            _line(
                level="error",
                event="everything is fine",
                component="scheduler",
                task_id="t1",
                project_id="p1",
                timestamp=_iso(60),
            ),
            # malformed JSONL line — skipped, not fatal
            "{ this is not json",
            "",
        ],
    )

    result = await log_handler._cmd_read_logs(
        {
            "level": "error",
            "since": "5m",
            "component": "scheduler",
            "task_id": "t1",
            "project_id": "p1",
            "pattern": "DISK PRESSURE",  # case-insensitive substring
        }
    )

    assert result["count"] == 1
    assert result["entries"] == [json.loads(match)]
    assert result["level_filter"] == "error"
    assert result["log_file"] == log_handler.config.logging.log_file


async def test_read_logs_limit_caps_returned_entries(log_handler):
    _write_log(log_handler, [_line(level="error", event=f"boom {i}") for i in range(10)])

    result = await log_handler._cmd_read_logs({"level": "error", "limit": 3})

    assert result["count"] == 3
    assert len(result["entries"]) == 3


# ---------------------------------------------------------------------------
# 15: read_logs error branches
# ---------------------------------------------------------------------------


async def test_read_logs_reports_missing_file_and_bad_since(log_handler):
    missing = Path(log_handler.config.logging.log_file)
    assert not missing.exists()

    result = await log_handler._cmd_read_logs({})
    assert result == {"error": f"Log file not found: {missing}"}

    _write_log(log_handler, [_line(level="error", event="boom")])

    assert await log_handler._cmd_read_logs({"since": "5x"}) == {
        "error": "Unknown time unit 'x'. Use s, m, h, or d."
    }
    assert await log_handler._cmd_read_logs({"since": "manym"}) == {
        "error": "Invalid number in 'manym'"
    }


async def test_read_logs_falls_back_to_the_data_dir_when_no_log_file_configured(
    command_handler_factory,
):
    handler = await command_handler_factory()
    handler.config.logging.log_file = ""

    result = await handler._cmd_read_logs({})

    expected = Path(handler.config.data_dir) / "logs" / "agent-queue.log"
    assert result == {"error": f"Log file not found: {expected}"}


# ---------------------------------------------------------------------------
# 16: chat analyzer metrics window coercion
# ---------------------------------------------------------------------------


async def test_chat_analyzer_metrics_coerces_window_and_rejects_noninteger(
    command_handler_factory, monkeypatch
):
    handler = await command_handler_factory()
    calls: list[dict] = []

    async def _fake_stats(*, project_id=None, since=None):
        calls.append({"project_id": project_id, "since": since})
        return {"total": 2, "pending": 1, "accepted": 1}

    monkeypatch.setattr(handler.db, "get_analyzer_suggestion_stats", _fake_stats)

    # Default window: 24h → a timestamp roughly 24h in the past.
    before = time.time()
    default = await handler._cmd_get_chat_analyzer_metrics({})
    assert default["since_hours"] == 24
    assert default["project_id"] is None
    assert default["total"] == 2
    assert calls[-1]["since"] == pytest.approx(before - 24 * 3600, abs=5)

    # 0 disables the window entirely — no timestamp is sent.
    zero = await handler._cmd_get_chat_analyzer_metrics({"since_hours": 0, "project_id": "p1"})
    assert zero["since_hours"] == 0
    assert zero["project_id"] == "p1"
    assert calls[-1] == {"project_id": "p1", "since": None}

    # Negative behaves like 0 (lifetime).
    await handler._cmd_get_chat_analyzer_metrics({"since_hours": -5})
    assert calls[-1]["since"] is None

    # Numeric strings are coerced.
    await handler._cmd_get_chat_analyzer_metrics({"since_hours": "6"})
    assert calls[-1]["since"] == pytest.approx(time.time() - 6 * 3600, abs=5)

    # Non-integer input errors without ever hitting the database.
    call_count = len(calls)
    for bad in ("soon", None, [1]):
        result = await handler._cmd_get_chat_analyzer_metrics({"since_hours": bad})
        assert set(result) == {"error"}
        assert "since_hours must be an integer" in result["error"]
    assert len(calls) == call_count


# ---------------------------------------------------------------------------
# 17: recent events relative-since conversion and filter forwarding
# ---------------------------------------------------------------------------


async def test_recent_events_converts_relative_since_and_forwards_all_filters(
    command_handler_factory, monkeypatch
):
    handler = await command_handler_factory()
    calls: list[dict] = []

    async def _fake_recent(**kwargs):
        calls.append(kwargs)
        return [{"id": "e1"}]

    monkeypatch.setattr(handler.db, "get_recent_events", _fake_recent)

    result = await handler._cmd_get_recent_events(
        {
            "limit": 5,
            "event_type": "task.completed",
            "since": "2h",
            "project_id": "p1",
            "agent_id": "a1",
            "task_id": "t1",
        }
    )

    assert result == {"events": [{"id": "e1"}]}
    sent = calls[-1]
    assert sent["limit"] == 5
    assert sent["event_type"] == "task.completed"
    assert sent["project_id"] == "p1"
    assert sent["agent_id"] == "a1"
    assert sent["task_id"] == "t1"
    assert sent["since"] == pytest.approx(time.time() - 2 * 3600, abs=5)

    # Defaults: limit 10, every filter None, no since conversion.
    await handler._cmd_get_recent_events({})
    assert calls[-1] == {
        "limit": 10,
        "event_type": None,
        "since": None,
        "project_id": None,
        "agent_id": None,
        "task_id": None,
    }
