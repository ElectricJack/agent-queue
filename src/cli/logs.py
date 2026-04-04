"""``aq logs`` — tail and filter JSONL log files.

Reads the daemon's JSONL log file directly (no running daemon required).
Supports filtering by level, task, project, component, plugin, and more.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
]

# Base log structure fields (not shown as key=value context)
_BASE_FIELDS = {
    "timestamp",
    "level",
    "logger",
    "event",
    "message",
}


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


def _matches_filters(entry: dict, filters: dict) -> bool:
    """Check if a log entry matches all active filters."""
    # Level filter (>= threshold)
    if filters.get("level"):
        entry_level = _LEVEL_PRIORITY.get(entry.get("level", "").lower(), 0)
        threshold = _LEVEL_PRIORITY.get(filters["level"].lower(), 0)
        if entry_level < threshold:
            return False

    # Since filter
    if filters.get("since"):
        ts = entry.get("timestamp", "")
        try:
            entry_dt = datetime.fromisoformat(ts)
            if entry_dt < filters["since"]:
                return False
        except (ValueError, TypeError):
            pass

    # Exact-match field filters
    for field in ("task_id", "project_id", "component", "plugin", "command"):
        if filters.get(field) and entry.get(field) != filters[field]:
            return False

    return True


def _format_entry(console: Console, entry: dict) -> None:
    """Render a single log entry with Rich colors matching structlog dev output."""
    ts = entry.get("timestamp", "")
    # Shorten ISO timestamp to HH:MM:SS
    if "T" in ts:
        ts = ts.split("T")[1][:8]

    level = entry.get("level", "info").lower()
    logger_name = entry.get("logger", "")
    message = entry.get("event") or entry.get("message", "")

    # Shorten 'src.foo.bar' to 'foo.bar'
    if logger_name.startswith("src."):
        logger_name = logger_name[4:]

    # Build the Rich text line
    line = Text()

    # Timestamp (dim)
    line.append(ts, style="dim")
    line.append(" ")

    # Level (colored, padded) — matches structlog ConsoleRenderer style
    level_style = _LEVEL_COLORS.get(level, "")
    line.append(f"[{level:<8s}]", style=level_style)
    line.append(" ")

    # Message first (bold for errors, normal otherwise)
    if level in ("error", "critical"):
        line.append(message, style="bold")
    else:
        line.append(message)

    # Logger name (dim, in brackets after message)
    line.append(" ")
    line.append(f"[{logger_name}]", style="dim blue")

    # Context fields (key=value pairs, colored)
    ctx_parts: list[tuple[str, str]] = []
    for field in _CONTEXT_FIELDS:
        val = entry.get(field)
        if val:
            ctx_parts.append((field, str(val)))

    # Extra fields not in known lists
    for key, val in sorted(entry.items()):
        if key not in _BASE_FIELDS and key not in _CONTEXT_FIELDS and key != "logger":
            ctx_parts.append((key, str(val)))

    if ctx_parts:
        line.append(" ")
        for i, (k, v) in enumerate(ctx_parts):
            if i > 0:
                line.append(" ", style="dim")
            line.append(f"{k}=", style="dim green")
            line.append(v, style="green")

    console.print(line, highlight=False)


def _tail_lines(filepath: str, n: int) -> list[str]:
    """Read the last N lines from a file efficiently."""
    try:
        with open(filepath, "rb") as f:
            # Seek to end
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []

            # Read chunks from end until we have enough lines
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
                    # Merge the last partial line from previous chunk
                    chunk_lines[-1] = chunk_lines[-1] + lines[0]
                    lines = chunk_lines + lines[1:]
                else:
                    lines = chunk_lines

            # Filter empty lines and decode
            decoded = [raw.decode("utf-8", errors="replace") for raw in lines if raw.strip()]
            return decoded[-n:]
    except FileNotFoundError:
        return []


def _follow(filepath: str, console: Console, filters: dict, as_json: bool) -> None:
    """Follow a log file, printing new lines as they appear."""
    try:
        with open(filepath) as f:
            # Seek to end
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    entry = _parse_line(line)
                    if entry and _matches_filters(entry, filters):
                        if as_json:
                            click.echo(line.rstrip())
                        else:
                            _format_entry(console, entry)
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
    since: str | None,
    as_json: bool,
    no_color: bool,
    log_file: str | None,
) -> None:
    """Tail and filter daemon logs.

    Reads the daemon's JSONL log file directly. Use --json to pipe to jq.

    \b
    Examples:
      aq logs                          # follow with colors
      aq logs -F -n 100                # last 100 lines, no follow
      aq logs --level ERROR            # errors only
      aq logs --task-id swift-dawn     # single task
      aq logs --component api          # API route logs
      aq logs --plugin github-issues   # plugin logs
      aq logs --since 5m --json | jq . # last 5min as JSON
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

    filters = {
        "level": level,
        "task_id": task_id,
        "project_id": project_id,
        "component": component,
        "plugin": plugin,
        "command": command,
        "since": _parse_since(since) if since else None,
    }

    # Show recent lines
    recent = _tail_lines(filepath, lines)
    for raw_line in recent:
        entry = _parse_line(raw_line)
        if entry and _matches_filters(entry, filters):
            if as_json:
                click.echo(raw_line.rstrip())
            else:
                _format_entry(console, entry)

    # Follow mode
    if follow:
        if not as_json:
            console.print("[dim]--- following (Ctrl+C to stop) ---[/dim]")
        _follow(filepath, console, filters, as_json)
