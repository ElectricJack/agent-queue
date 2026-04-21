"""Tests for ``aq logs`` filter flags and context windowing."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.cli.logs import (
    _ContextEmitter,
    _exception_summary,
    _has_exception,
    _matches_filters,
)


def _entry(**kwargs) -> dict:
    base = {
        "event": "hello",
        "level": "info",
        "logger": "src.test",
        "timestamp": "2026-04-21T12:00:00.000000Z",
    }
    base.update(kwargs)
    return base


def _write_log(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


# ── _matches_filters ───────────────────────────────────────────────────


def test_grep_filter_matches_event_field():
    pat = re.compile(r"not.?found", re.IGNORECASE)
    assert _matches_filters(_entry(event="404 Not Found"), {"grep": pat}) is True
    assert _matches_filters(_entry(event="everything is fine"), {"grep": pat}) is False


def test_grep_filter_falls_back_to_message_field():
    pat = re.compile(r"boom")
    assert _matches_filters(_entry(event=None, message="boom"), {"grep": pat}) is True


def test_exception_filter_requires_exc_info():
    no_exc = _entry()
    with_exc = _entry(exc_info=["<class 'ValueError'>", "ValueError('x')", "<tb>"])
    with_exception_str = _entry(exception="Traceback...\nValueError: bad")

    assert _matches_filters(no_exc, {"exception": True}) is False
    assert _matches_filters(with_exc, {"exception": True}) is True
    assert _matches_filters(with_exception_str, {"exception": True}) is True


def test_exception_filter_rejects_empty_exc_info():
    assert _has_exception(_entry(exc_info=[])) is False
    assert _has_exception(_entry(exc_info=None)) is False


def test_request_id_and_run_id_exact_match():
    entry = _entry(request_id="abc123", run_id="run-xyz")
    assert _matches_filters(entry, {"request_id": "abc123"}) is True
    assert _matches_filters(entry, {"request_id": "other"}) is False
    assert _matches_filters(entry, {"run_id": "run-xyz"}) is True
    assert _matches_filters(entry, {"run_id": "run-zzz"}) is False


def test_combined_filters_all_must_match():
    entry = _entry(event="boom", level="error", task_id="t-1")
    filters = {
        "grep": re.compile("boom"),
        "level": "error",
        "task_id": "t-1",
    }
    assert _matches_filters(entry, filters) is True

    filters["task_id"] = "other"
    assert _matches_filters(entry, filters) is False


# ── _exception_summary ─────────────────────────────────────────────────


def test_exception_summary_from_exc_info_tuple():
    entry = _entry(
        exc_info=[
            "<class 'sqlalchemy.exc.IntegrityError'>",
            "IntegrityError('duplicate key value')",
            "<traceback object at 0x1234>",
        ]
    )
    assert _exception_summary(entry) == "IntegrityError: duplicate key value"


def test_exception_summary_from_formatted_string():
    entry = _entry(
        exception="Traceback (most recent call last):\n  File ...\nValueError: bad input"
    )
    assert _exception_summary(entry) == "ValueError: bad input"


def test_exception_summary_none_when_missing():
    assert _exception_summary(_entry()) is None


def test_exception_summary_handles_unrecognized_class_repr():
    # Some loggers write "exc_info": [..., "<class 'X'>", "<class 'X'>", ...]
    entry = _entry(exc_info=["nonstandard", "MysteryBoom('oh no')", "<tb>"])
    summary = _exception_summary(entry)
    assert summary is not None
    assert "oh no" in summary


# ── _ContextEmitter ────────────────────────────────────────────────────


def _collect():
    emitted: list[tuple[str, bool]] = []

    def emit(raw_line: str, entry: dict, is_match: bool) -> None:
        emitted.append((raw_line, is_match))

    return emitted, emit


def test_context_emitter_zero_context_emits_only_matches():
    emitted, emit = _collect()
    e = _ContextEmitter(0, emit)
    for i, matches in enumerate([False, True, False, True, False]):
        e.feed(f"line{i}", {}, matches)

    assert emitted == [("line1", True), ("line3", True)]


def test_context_emitter_emits_before_and_after():
    emitted, emit = _collect()
    e = _ContextEmitter(2, emit)
    # sequence: 5 non-matches, match, 3 non-matches
    for i in range(5):
        e.feed(f"b{i}", {}, False)
    e.feed("M", {}, True)
    for i in range(3):
        e.feed(f"a{i}", {}, False)

    # Expect the 2 most recent non-matches before M, the match, and 2 after
    assert emitted == [
        ("b3", False),
        ("b4", False),
        ("M", True),
        ("a0", False),
        ("a1", False),
    ]


def test_context_emitter_inserts_separator_between_distant_matches():
    emitted, emit = _collect()
    e = _ContextEmitter(1, emit)
    # match, non, non (after window closes), non, non, match
    e.feed("M1", {}, True)
    for i in range(4):
        e.feed(f"n{i}", {}, False)
    e.feed("M2", {}, True)

    # Expect: M1, 1 after (n0), separator "--", buffered "n3" as before, M2
    raw_lines = [r for r, _ in emitted]
    assert raw_lines == ["M1", "n0", "--", "n3", "M2"]


def test_context_emitter_overlapping_matches_no_duplicates():
    emitted, emit = _collect()
    e = _ContextEmitter(2, emit)
    # Two matches within the after-window: no separator, no duplicates
    e.feed("M1", {}, True)
    e.feed("n0", {}, False)
    e.feed("M2", {}, True)
    e.feed("n1", {}, False)

    raw_lines = [r for r, _ in emitted]
    assert raw_lines == ["M1", "n0", "M2", "n1"]


# ── CLI end-to-end ─────────────────────────────────────────────────────


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    entries = [
        _entry(event="startup"),
        _entry(event="task began", task_id="t-1"),
        _entry(event="404 Not Found", level="error", task_id="t-1"),
        _entry(
            event="Error executing task",
            level="error",
            task_id="t-1",
            exc_info=[
                "<class 'ValueError'>",
                "ValueError('bad input')",
                "<traceback>",
            ],
        ),
        _entry(event="task done", task_id="t-1"),
        _entry(event="idle"),
    ]
    f = tmp_path / "test.log"
    _write_log(f, entries)
    return f


def test_cli_grep_filter(log_file: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "logs",
            "-F",
            "--log-file",
            str(log_file),
            "--grep",
            "Not Found",
            "--no-color",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "404 Not Found" in result.output
    assert "startup" not in result.output
    assert "idle" not in result.output


def test_cli_exception_filter_shows_summary(log_file: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "logs",
            "-F",
            "--log-file",
            str(log_file),
            "--exception",
            "--no-color",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Error executing task" in result.output
    assert "ValueError: bad input" in result.output
    assert "404 Not Found" not in result.output


def test_cli_invalid_regex_rejected(log_file: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["logs", "-F", "--log-file", str(log_file), "--grep", "[unclosed"],
    )
    assert result.exit_code != 0
    assert "Invalid --grep regex" in result.output


def test_cli_json_mode_only_emits_matches(log_file: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "logs",
            "-F",
            "--log-file",
            str(log_file),
            "--exception",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    out_lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(out_lines) == 1
    parsed = json.loads(out_lines[0])
    assert parsed["event"] == "Error executing task"


def test_cli_context_includes_surrounding_lines(log_file: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "logs",
            "-F",
            "--log-file",
            str(log_file),
            "--grep",
            "Not Found",
            "-C",
            "1",
            "--no-color",
        ],
    )
    assert result.exit_code == 0, result.output
    # Line before ("task began"), the match, and line after ("Error executing task")
    assert "task began" in result.output
    assert "404 Not Found" in result.output
    assert "Error executing task" in result.output
    # Far-away lines excluded
    assert "startup" not in result.output
    assert "idle" not in result.output
