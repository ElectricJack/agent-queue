"""Structured logging with correlation context for the agent queue system.

Uses ``structlog`` with a processor pipeline that supports three output modes:

- **dev** — Rich-colored console output with aligned columns (default)
- **json** — Single-line JSON objects for log aggregation / ``jq``
- **plain** — Human-readable text without ANSI escape codes (for piping)

All existing ``logging.getLogger(__name__)`` loggers are bridged through
the structlog pipeline via ``ProcessorFormatter``, so context fields bound
with ``CorrelationContext`` (or ``structlog.contextvars``) appear on every
log line automatically — no per-file changes required.

Usage::

    from src.logging_config import setup_logging, CorrelationContext

    # At startup
    setup_logging(level="INFO", format="dev")

    # In task execution
    with CorrelationContext(task_id="swift-dawn", project_id="my-project"):
        logger.info("Starting task execution")
        # All log records within this block include task_id and project_id

See ``LoggingConfig`` in ``src/config.py`` for available configuration options.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from typing import Any

import structlog


# ── Correlation context (structlog contextvars) ────────────────────────


class CorrelationContext:
    """Context manager that binds correlation fields to log records.

    Wraps ``structlog.contextvars.bound_contextvars`` for backward
    compatibility with existing call sites.  Fields are stored in
    ``contextvars`` so they automatically propagate through ``await``
    chains within the same task.  On exit the previous values are
    restored (supports nesting).

    Example::

        with CorrelationContext(task_id="swift-dawn", project_id="acme"):
            logger.info("Processing")  # includes task_id, project_id
    """

    def __init__(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        cycle_id: str | None = None,
        component: str | None = None,
        hook_id: str | None = None,
        agent_id: str | None = None,
        command: str | None = None,
        **extra: str | None,
    ):
        self._kwargs = {
            k: v
            for k, v in {
                "task_id": task_id,
                "project_id": project_id,
                "cycle_id": cycle_id,
                "component": component,
                "hook_id": hook_id,
                "agent_id": agent_id,
                "command": command,
                **extra,
            }.items()
            if v is not None
        }
        self._ctx: contextmanager | None = None

    def __enter__(self) -> CorrelationContext:
        self._ctx = structlog.contextvars.bound_contextvars(**self._kwargs)
        self._ctx.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._ctx is not None:
            self._ctx.__exit__(*exc)
            self._ctx = None


def get_correlation_context() -> dict[str, Any]:
    """Return current correlation fields as a dict (non-None values only)."""
    return structlog.contextvars.get_contextvars()


# ── Shared processors ──────────────────────────────────────────────────


def _build_shared_processors(include_source: bool = False) -> list:
    """Build the processor chain shared by structlog and stdlib bridge."""
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    if include_source:
        processors.append(
            structlog.processors.CallsiteParameterAdder(
                [
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.LINENO,
                ]
            )
        )
    return processors


def _shorten_timestamp(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Shorten ISO timestamp to HH:MM:SS for console output."""
    ts = event_dict.get("timestamp", "")
    if isinstance(ts, str) and "T" in ts:
        event_dict["timestamp"] = ts.split("T")[1][:8]
    return event_dict


def _shorten_logger_name(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Strip 'src.' prefix from logger names for conciseness."""
    name = event_dict.get("logger", "")
    if isinstance(name, str) and name.startswith("src."):
        event_dict["logger"] = name[4:]
    return event_dict


class _TemplateRenderer:
    """Render log lines using a user-defined format template.

    Template fields are ``{name}`` placeholders that expand to values from
    the structlog event dict.  Grouped fields like ``[{component}:{project_id}]``
    collapse gracefully — if all fields in a bracket group are missing, the
    entire bracket group (including delimiters) is removed.

    Special field names:

    - ``{event}`` or ``{message}`` — the log message
    - ``{level}`` — log level (info, warning, error, ...)
    - ``{timestamp}`` — already shortened to HH:MM:SS by upstream processor
    - ``{logger}`` — logger name (already shortened, without ``src.`` prefix)
    - ``{*}`` — all remaining context fields as ``key=value`` pairs

    Example templates::

        {timestamp} [{level}] {event} [{logger}:{lineno}] [{component}:{project_id}]
        {level} {event} [{component}] {*}
        [{level}] {event} {*}

    ANSI colors are applied based on the ``colors`` flag.
    """

    # ANSI color codes for levels
    _LEVEL_ANSI = {
        "debug": "\033[2m",  # dim
        "info": "\033[32;1m",  # green bold
        "warning": "\033[33;1m",  # yellow bold
        "error": "\033[31;1m",  # red bold
        "critical": "\033[37;1;41m",  # white bold on red
    }
    _RESET = "\033[0m"
    _DIM = "\033[2m"
    _CYAN = "\033[36m"
    _MAGENTA = "\033[35m"

    def __init__(self, template: str, colors: bool = True):
        self._template = template
        self._colors = colors

    def __call__(self, logger: Any, method_name: str, event_dict: dict[str, Any]) -> str:
        template = self._template
        used_keys: set[str] = set()

        # Collect all {field} references in the template
        import re

        field_refs = set(re.findall(r"\{(\w+)\}", template))

        # "message" is an alias for "event" (structlog's key)
        if "message" in field_refs:
            field_refs.discard("message")
            field_refs.add("event")
            template = template.replace("{message}", "{event}")

        # Track which keys we substitute
        used_keys.update(field_refs)
        used_keys.discard("*")

        # Build substitution dict
        subs: dict[str, str] = {}
        for key in field_refs:
            if key == "*":
                continue
            val = event_dict.get(key)
            if val is not None:
                sval = str(val)
                # Apply colors to specific fields
                if self._colors:
                    if key == "level":
                        color = self._LEVEL_ANSI.get(sval, "")
                        sval = f"{color}{sval:<8s}{self._RESET}"
                    elif key == "timestamp":
                        sval = f"{self._DIM}{sval}{self._RESET}"
                    elif key in ("logger", "lineno", "filename"):
                        sval = f"{self._DIM}{sval}{self._RESET}"
                    elif key == "event":
                        level = event_dict.get("level", "")
                        if level in ("error", "critical"):
                            sval = f"\033[1m{sval}{self._RESET}"
                subs[key] = sval
            else:
                subs[key] = ""

        # Expand {*} with remaining context fields
        if "*" in self._template or "{*}" in template:
            skip = used_keys | {"event", "level", "timestamp", "logger", "_record"}
            remaining = []
            for k, v in event_dict.items():
                if k not in skip and not k.startswith("_") and v is not None:
                    if self._colors:
                        remaining.append(
                            f"{self._CYAN}{k}{self._RESET}={self._MAGENTA}{v}{self._RESET}"
                        )
                    else:
                        remaining.append(f"{k}={v}")
            subs["*"] = " ".join(remaining)

        # Substitute fields
        result = template
        for key, val in subs.items():
            result = result.replace(f"{{{key}}}", val)

        # Collapse empty bracket groups: [::] or [:] or [] → removed
        result = re.sub(r"\[[:\s]*\]", "", result)
        # Clean up multiple spaces
        result = re.sub(r"  +", " ", result).strip()

        return result


def _get_console_processors(
    fmt: str,
    console_format: str = "",
    console_columns: str = "",
) -> list:
    """Return the processor chain for console output."""
    processors: list = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if fmt != "json":
        # Shorten timestamps and logger names for human-readable modes
        processors.append(_shorten_timestamp)
        processors.append(_shorten_logger_name)
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    elif console_format:
        # User-defined format template — overrides default renderer
        colors = fmt != "plain"
        processors.append(_TemplateRenderer(console_format, colors=colors))
    elif fmt == "plain":
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    else:
        # "dev" (or "text" backward compat)
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    return processors


# ── Setup function ──────────────────────────────────────────────────────


def setup_logging(
    *,
    level: str = "INFO",
    format: str = "dev",
    include_source: bool = False,
    log_file: str = "",
    log_file_max_bytes: int = 50_000_000,
    log_file_backup_count: int = 5,
    console_format: str = "",
) -> None:
    """Configure the root logger with structlog-powered output.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        format: Output format — ``"dev"`` for colored Rich output,
            ``"json"`` for JSON-lines, ``"plain"`` for uncolored text.
            ``"text"`` is accepted as a backward-compatible alias for ``"dev"``.
        include_source: Include filename/lineno in output.
        log_file: Path for JSONL log file.  Empty string disables file output.
        log_file_max_bytes: Max bytes per log file before rotation.
        log_file_backup_count: Number of rotated log files to keep.
        console_format: Custom format template for dev/plain output.
            Uses ``{field}`` placeholders.  Empty string uses the default
            structlog ConsoleRenderer.  Example:
            ``"{timestamp} [{level}] {event} [{logger}:{lineno}] [{component}:{project_id}]"``
    """
    # Normalize backward-compat alias
    if format == "text":
        format = "dev"

    log_level = getattr(logging, level.upper(), logging.INFO)

    # Clear structlog contextvars from any previous setup
    structlog.contextvars.clear_contextvars()

    # Build shared processor chain
    shared_processors = _build_shared_processors(include_source=include_source)

    # Configure structlog for structlog-native loggers
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ── Root stdlib logger ──────────────────────────────────────────
    root = logging.getLogger()

    # Remove existing handlers to avoid duplicate output
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Console handler (stderr) — uses the user's chosen format
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=_get_console_processors(format, console_format=console_format),
            foreign_pre_chain=shared_processors,
        )
    )
    console_handler.setLevel(log_level)
    root.addHandler(console_handler)

    # File handler (JSONL) — always JSON regardless of console mode
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=log_file_max_bytes,
            backupCount=log_file_backup_count,
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ],
                foreign_pre_chain=shared_processors,
            )
        )
        file_handler.setLevel(log_level)
        root.addHandler(file_handler)

    root.setLevel(log_level)

    # Quiet down noisy third-party loggers
    for noisy in ("discord", "discord.http", "discord.gateway", "aiohttp", "uvicorn"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))

    # Attach the Discord rate-guard log handler so we can count 429
    # responses that discord.py retries internally.  Lazy import to
    # avoid circular deps at module load time.
    from src.discord.rate_guard import DiscordHTTPLogHandler, get_tracker

    discord_http_logger = logging.getLogger("discord.http")
    # Avoid duplicate handlers on repeated setup_logging() calls
    if not any(isinstance(h, DiscordHTTPLogHandler) for h in discord_http_logger.handlers):
        discord_http_logger.addHandler(DiscordHTTPLogHandler(get_tracker()))
