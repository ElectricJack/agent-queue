"""``aq logs`` — tail and filter JSONL log files.

Reads the daemon's JSONL log file directly (no running daemon required).
Supports filtering by level, task, project, component, plugin, regex, and
exceptions, with optional context lines around matches.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import click
from rich.console import Console
from rich.text import Text

from .app import cli

_LEVEL_PRIORITY = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}

# Level display styles for Rich
_LEVEL_COLORS = {
    "debug": "dim",
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "critical": "bold white on red",
}

# Context fields shown in display order; others are appended after
_CONTEXT_FIELDS = [
    "component",
    "command",
    "platform",
    "plugin",
    "task_id",
    "project_id",
    "hook_id",
    "agent_id",
    "route",
    "request_id",
    "cycle_id",
    "run_id",
]

# Base log structure fields (not shown as key=value context)
_BASE_FIELDS = {
    "timestamp",
    "level",
    "logger",
    "event",
    "message",
}

# Fields that get their own special rendering (never shown as key=value)
_SPECIAL_FIELDS = {"exc_info", "exception", "stack_info"}


def _default_log_path() -> str:
    """Return the default log file path."""
    return os.path.expanduser("~/.agent-queue/logs/agent-queue.log")


def _parse_since(since: str) -> datetime:
    """Parse a relative time string like '5m', '1h', '30s' into a datetime."""
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    if not since:
        return datetime.min.replace(tzinfo=timezone.utc)

    unit = since[-1].lower()
    if unit not in units:
        raise click.BadParameter(f"Unknown time unit '{unit}'. Use s, m, h, or d.")
    try:
        value = int(since[:-1])
    except ValueError:
        raise click.BadParameter(f"Invalid number in '{since}'")

    delta = timedelta(**{units[unit]: value})
    return datetime.now(timezone.utc) - delta


def _parse_line(line: str) -> dict | None:
    """Parse a JSONL line, returning None on failure."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _has_exception(entry: dict) -> bool:
    """Return True when an entry carries exception/traceback data."""
    if entry.get("exception"):
        return True
    exc = entry.get("exc_info")
    # structlog-written exc_info is a list/tuple; bool(None) or empty == False
    if isinstance(exc, (list, tuple)):
        return len(exc) > 0
    return bool(exc)


def _matches_filters(entry: dict, filters: dict) -> bool:
    """Check if a log entry matches all active filters."""
    if filters.get("level"):
        entry_level = _LEVEL_PRIORITY.get(entry.get("level", "").lower(), 0)
        threshold = _LEVEL_PRIORITY.get(filters["level"].lower(), 0)
        if entry_level < threshold:
            return False

    if filters.get("since"):
        ts = entry.get("timestamp", "")
        try:
            entry_dt = datetime.fromisoformat(ts)
            if entry_dt < filters["since"]:
                return False
        except (ValueError, TypeError):
            pass

    for field in (
        "task_id",
        "project_id",
        "component",
        "plugin",
        "command",
        "request_id",
        "run_id",
    ):
        if filters.get(field) and entry.get(field) != filters[field]:
            return False

    if filters.get("exception") and not _has_exception(entry):
        return False

    pattern: re.Pattern | None = filters.get("grep")
    if pattern is not None:
        haystack = str(entry.get("event") or entry.get("message") or "")
        if not pattern.search(haystack):
            return False

    return True


def _exception_summary(entry: dict) -> str | None:
    """Return a single-line ``ExcClass: message`` summary, or None.

    Prefers the ``exception`` string when present (structlog's formatted
    traceback — take the final line).  Falls back to the ``exc_info`` list
    of ``[class_repr, exc_repr, tb_repr]`` written by JSONRenderer.
    """
    formatted = entry.get("exception")
    if isinstance(formatted, str) and formatted.strip():
        # Last non-empty line of a traceback is the `ExcClass: msg` line
        for line in reversed(formatted.splitlines()):
            stripped = line.strip()
            if stripped:
                return stripped
        return None

    exc = entry.get("exc_info")
    if not isinstance(exc, (list, tuple)) or len(exc) < 2:
        return None

    cls_raw = str(exc[0])
    msg_raw = str(exc[1])

    # "<class 'foo.bar.Baz'>" → "Baz"
    cls_match = re.search(r"<class '([^']+)'>", cls_raw)
    if cls_match:
        cls_name = cls_match.group(1).rsplit(".", 1)[-1]
    else:
        cls_name = cls_raw

    # "Baz('hello')" → "hello"   (common repr form)
    msg_match = re.match(rf"^{re.escape(cls_name)}\((.*)\)$", msg_raw, re.DOTALL)
    if msg_match:
        msg = msg_match.group(1).strip()
        # Strip surrounding quotes if a single string arg
        if (msg.startswith("'") and msg.endswith("'")) or (
            msg.startswith('"') and msg.endswith('"')
        ):
            msg = msg[1:-1]
    else:
        msg = msg_raw

    return f"{cls_name}: {msg}"


def _format_entry(console: Console, entry: dict, dim: bool = False) -> None:
    """Render a single log entry with Rich colors matching structlog dev output.

    If ``dim`` is True the whole line is rendered dim — used for context
    lines that surround a match.
    """
    ts = entry.get("timestamp", "")
    if "T" in ts:
        ts = ts.split("T")[1][:8]

    level = entry.get("level", "info").lower()
    logger_name = entry.get("logger", "")
    message = entry.get("event") or entry.get("message", "")

    if logger_name.startswith("src."):
        logger_name = logger_name[4:]

    line = Text()

    line.append(ts, style="dim")
    line.append(" ")

    level_style = "dim" if dim else _LEVEL_COLORS.get(level, "")
    line.append(f"[{level:<8s}]", style=level_style)
    line.append(" ")

    if dim:
        line.append(message, style="dim")
    elif level in ("error", "critical"):
        line.append(message, style="bold")
    else:
        line.append(message)

    line.append(" ")
    line.append(f"[{logger_name}]", style="dim blue")

    ctx_parts: list[tuple[str, str]] = []
    for field in _CONTEXT_FIELDS:
        val = entry.get(field)
        if val:
            ctx_parts.append((field, str(val)))

    for key, val in sorted(entry.items()):
        if (
            key not in _BASE_FIELDS
            and key not in _CONTEXT_FIELDS
            and key not in _SPECIAL_FIELDS
            and key != "logger"
        ):
            ctx_parts.append((key, str(val)))

    if ctx_parts:
        line.append(" ")
        for i, (k, v) in enumerate(ctx_parts):
            if i > 0:
                line.append(" ", style="dim")
            kv_style = "dim" if dim else "dim green"
            val_style = "dim" if dim else "green"
            line.append(f"{k}=", style=kv_style)
            line.append(v, style=val_style)

    console.print(line, highlight=False)

    summary = _exception_summary(entry)
    if summary:
        exc_line = Text()
        exc_line.append("         ↳ ", style="dim")
        exc_line.append(summary, style="dim red" if dim else "red")
        console.print(exc_line, highlight=False)


class _ContextEmitter:
    """Emit matching records with N lines of surrounding context.

    Maintains a ring buffer of ``N`` most recent non-matching records.
    When a match arrives, flushes the buffer (as context-before), emits the
    match, and then emits the next ``N`` records (as context-after).

    When ``context`` is 0 this collapses to "emit only matches".
    """

    def __init__(self, context: int, emit_fn: Callable[[str, dict, bool], None]):
        self._context = context
        self._emit = emit_fn
        self._before: deque[tuple[str, dict]] = deque(maxlen=context) if context > 0 else deque()
        self._after_remaining = 0
        self._separator_needed = False

    def feed(self, raw_line: str, entry: dict, matches: bool) -> None:
        if matches:
            if self._separator_needed and self._context > 0:
                # We had a previous match whose "after" window closed before
                # this new match's buffer filled — mark a gap for readability
                self._emit("--", {}, False)
            if self._context > 0:
                while self._before:
                    buf_raw, buf_entry = self._before.popleft()
                    self._emit(buf_raw, buf_entry, False)
            self._emit(raw_line, entry, True)
            self._after_remaining = self._context
            self._separator_needed = False
        elif self._after_remaining > 0:
            self._emit(raw_line, entry, False)
            self._after_remaining -= 1
            if self._after_remaining == 0:
                self._separator_needed = True
        elif self._context > 0:
            self._before.append((raw_line, entry))


def _make_emit(console: Console, as_json: bool) -> Callable[[str, dict, bool], None]:
    def emit(raw_line: str, entry: dict, is_match: bool) -> None:
        if as_json:
            # In JSON mode we just print matches; context lines are skipped
            # to keep the output pipeable. Separators ("--") are also skipped.
            if is_match:
                click.echo(raw_line.rstrip())
            return
        if raw_line == "--" and not entry:
            console.print(Text("--", style="dim"))
            return
        _format_entry(console, entry, dim=not is_match)

    return emit


def _tail_lines(filepath: str, n: int) -> list[str]:
    """Read the last N lines from a file efficiently."""
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []

            chunk_size = min(8192, size)
            lines: list[bytes] = []
            pos = size

            while len(lines) <= n and pos > 0:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                chunk_lines = chunk.split(b"\n")

                if lines:
                    chunk_lines[-1] = chunk_lines[-1] + lines[0]
                    lines = chunk_lines + lines[1:]
                else:
                    lines = chunk_lines

            decoded = [raw.decode("utf-8", errors="replace") for raw in lines if raw.strip()]
            return decoded[-n:]
    except FileNotFoundError:
        return []


def _follow(
    filepath: str,
    emitter: _ContextEmitter,
    filters: dict,
    console: Console,
) -> None:
    """Follow a log file, feeding new lines through the context emitter."""
    try:
        with open(filepath) as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    entry = _parse_line(line)
                    if entry is None:
                        continue
                    emitter.feed(line, entry, _matches_filters(entry, filters))
                else:
                    time.sleep(0.2)
    except FileNotFoundError:
        console.print(f"[red]Log file not found:[/red] {filepath}")
        console.print("Is the daemon running? Start it with: [bold]./run.sh start[/bold]")
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass


@cli.command("logs")
@click.option("-n", "--lines", default=50, help="Number of recent lines to show.")
@click.option(
    "-f/-F",
    "--follow/--no-follow",
    default=True,
    help="Follow log output (default: on).",
)
@click.option("--level", default=None, help="Minimum log level (DEBUG, INFO, WARNING, ERROR).")
@click.option("--task-id", default=None, help="Filter by task ID.")
@click.option("--project-id", default=None, help="Filter by project ID.")
@click.option(
    "--component",
    default=None,
    help="Filter by component (api, hooks, supervisor, orchestrator, etc.).",
)
@click.option("--plugin", default=None, help="Filter by plugin name.")
@click.option("--command", default=None, help="Filter by command name.")
@click.option("--request-id", default=None, help="Filter by request ID.")
@click.option("--run-id", default=None, help="Filter by playbook/supervisor run ID.")
@click.option(
    "--grep",
    "grep_pattern",
    default=None,
    help="Regex matched against the event/message field.",
)
@click.option(
    "--exception",
    is_flag=True,
    default=False,
    help="Only show records with exception/traceback data.",
)
@click.option(
    "-C",
    "--context",
    default=0,
    type=int,
    help="Show N lines of context around each match (dimmed).",
)
@click.option("--since", default=None, help="Show logs since (e.g. 5m, 1h, 30s, 2d).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSONL (for piping to jq).")
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    help="Disable colored output.",
)
@click.option(
    "--log-file",
    default=None,
    help=f"Log file path (default: {_default_log_path()}).",
)
def logs_cmd(
    lines: int,
    follow: bool,
    level: str | None,
    task_id: str | None,
    project_id: str | None,
    component: str | None,
    plugin: str | None,
    command: str | None,
    request_id: str | None,
    run_id: str | None,
    grep_pattern: str | None,
    exception: bool,
    context: int,
    since: str | None,
    as_json: bool,
    no_color: bool,
    log_file: str | None,
) -> None:
    """Tail and filter daemon logs.

    Reads the daemon's JSONL log file directly. Use --json to pipe to jq.

    \b
    Examples:
      aq logs                              # follow with colors
      aq logs -F -n 100                    # last 100 lines, no follow
      aq logs --level ERROR                # errors only
      aq logs --task-id swift-dawn         # single task
      aq logs --grep '404|timeout'         # regex over message
      aq logs --exception -F               # just exceptions
      aq logs --grep NotFound -C 3         # 3 lines of context around matches
      aq logs --run-id bb8e481e-7df        # single playbook run
      aq logs --since 5m --json | jq .     # last 5min as JSON
    """
    filepath = log_file or _default_log_path()

    if as_json:
        console = Console(stderr=True)
    elif no_color:
        console = Console(no_color=True, highlight=False)
    else:
        console = Console(force_terminal=True, highlight=False)

    if not Path(filepath).exists():
        console.print(f"[red]Log file not found:[/red] {filepath}")
        console.print("The daemon writes logs here once started. Run: [bold]./run.sh start[/bold]")
        raise SystemExit(1)

    try:
        grep_re = re.compile(grep_pattern) if grep_pattern else None
    except re.error as err:
        raise click.BadParameter(f"Invalid --grep regex: {err}")

    if context < 0:
        raise click.BadParameter("--context must be >= 0")

    filters = {
        "level": level,
        "task_id": task_id,
        "project_id": project_id,
        "component": component,
        "plugin": plugin,
        "command": command,
        "request_id": request_id,
        "run_id": run_id,
        "grep": grep_re,
        "exception": exception,
        "since": _parse_since(since) if since else None,
    }

    emit = _make_emit(console, as_json)
    emitter = _ContextEmitter(context, emit)

    recent = _tail_lines(filepath, lines)
    for raw_line in recent:
        entry = _parse_line(raw_line)
        if entry is None:
            continue
        emitter.feed(raw_line, entry, _matches_filters(entry, filters))

    if follow:
        if not as_json:
            console.print("[dim]--- following (Ctrl+C to stop) ---[/dim]")
        _follow(filepath, emitter, filters, console)
