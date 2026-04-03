"""Structured logging with correlation IDs for the agent queue system.

Provides JSON-lines structured logging via a custom ``StructuredFormatter``
and task-level correlation context using ``contextvars``.  When configured,
all log output is emitted as single-line JSON objects with consistent fields
(timestamp, level, logger, message, plus any extra context).

The ``CorrelationContext`` class manages per-task context that is automatically
attached to every log record within that task's execution scope.  This enables
filtering and tracing logs for a specific task_id, project_id, or cycle across
all components without manual threading of IDs.

Usage::

    from src.logging_config import setup_logging, CorrelationContext

    # At startup
    setup_logging(config.logging)

    # In task execution
    with CorrelationContext(task_id="swift-dawn", project_id="my-project"):
        logger.info("Starting task execution")
        # All log records within this block include task_id and project_id

See ``LoggingConfig`` in ``src/config.py`` for available configuration options.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any


# ── Correlation context (contextvars-based) ──────────────────────────────

_correlation_task_id: ContextVar[str | None] = ContextVar("correlation_task_id", default=None)
_correlation_project_id: ContextVar[str | None] = ContextVar("correlation_project_id", default=None)
_correlation_cycle_id: ContextVar[str | None] = ContextVar("correlation_cycle_id", default=None)
_correlation_component: ContextVar[str | None] = ContextVar("correlation_component", default=None)
_correlation_hook_id: ContextVar[str | None] = ContextVar("correlation_hook_id", default=None)
_correlation_agent_id: ContextVar[str | None] = ContextVar("correlation_agent_id", default=None)
_correlation_command: ContextVar[str | None] = ContextVar("correlation_command", default=None)


class CorrelationContext:
    """Context manager that sets correlation fields on log records.

    Fields are stored in ``contextvars`` so they automatically propagate
    through ``await`` chains within the same task.  On exit the previous
    values are restored (supports nesting).

    Example::

        with CorrelationContext(task_id="swift-dawn", project_id="acme"):
            logger.info("Processing")  # includes task_id, project_id
    """

    # Map of field names to their context variables, used for set/reset
    _FIELDS: dict[str, ContextVar[str | None]] = {}  # populated after class body

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
    ):
        self._values: dict[str, str] = {}
        if task_id is not None:
            self._values["task_id"] = task_id
        if project_id is not None:
            self._values["project_id"] = project_id
        if cycle_id is not None:
            self._values["cycle_id"] = cycle_id
        if component is not None:
            self._values["component"] = component
        if hook_id is not None:
            self._values["hook_id"] = hook_id
        if agent_id is not None:
            self._values["agent_id"] = agent_id
        if command is not None:
            self._values["command"] = command
        self._tokens: list = []

    def __enter__(self) -> CorrelationContext:
        for name, value in self._values.items():
            var = self._FIELDS[name]
            self._tokens.append((name, var.set(value)))
        return self

    def __exit__(self, *exc: Any) -> None:
        for name, token in reversed(self._tokens):
            self._FIELDS[name].reset(token)
        self._tokens.clear()


# Populate the field→contextvar mapping after the class is defined
CorrelationContext._FIELDS = {
    "task_id": _correlation_task_id,
    "project_id": _correlation_project_id,
    "cycle_id": _correlation_cycle_id,
    "component": _correlation_component,
    "hook_id": _correlation_hook_id,
    "agent_id": _correlation_agent_id,
    "command": _correlation_command,
}


def get_correlation_context() -> dict[str, str]:
    """Return current correlation fields as a dict (non-None values only)."""
    ctx: dict[str, str] = {}
    for name, var in CorrelationContext._FIELDS.items():
        val = var.get()
        if val is not None:
            ctx[name] = val
    return ctx


# ── Structured JSON formatter ────────────────────────────────────────────


class StructuredFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    Output fields:
    - ``timestamp`` — ISO 8601 UTC
    - ``level`` — log level name (INFO, WARNING, etc.)
    - ``logger`` — logger name (usually module path)
    - ``message`` — the formatted log message
    - ``task_id``, ``project_id``, ``cycle_id``, ``component``,
      ``hook_id``, ``agent_id``, ``command`` — from ``CorrelationContext``
      (omitted when not set)
    - Any extra fields passed via ``logger.info("msg", extra={...})``

    When ``include_source`` is True (default for DEBUG level configs),
    ``filename`` and ``lineno`` are included.
    """

    # Fields from LogRecord that we handle explicitly or want to exclude
    _SKIP_FIELDS = frozenset(
        {
            "name",
            "msg",
            "args",
            "created",
            "relativeCreated",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "filename",
            "module",
            "pathname",
            "levelname",
            "levelno",
            "msecs",
            "thread",
            "threadName",
            "process",
            "processName",
            "taskName",
            "message",
        }
    )

    def __init__(self, include_source: bool = False):
        super().__init__()
        self._include_source = include_source

    def format(self, record: logging.LogRecord) -> str:
        # Build the base entry
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add correlation context
        entry.update(get_correlation_context())

        # Add source location if configured
        if self._include_source:
            entry["filename"] = record.filename
            entry["lineno"] = record.lineno

        # Add exception info if present
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)

        if record.stack_info:
            entry["stack_info"] = record.stack_info

        # Add any extra fields that aren't standard LogRecord attributes
        for key, value in record.__dict__.items():
            if key not in self._SKIP_FIELDS and not key.startswith("_"):
                entry[key] = value

        return json.dumps(entry, default=str, ensure_ascii=False)


class HumanReadableFormatter(logging.Formatter):
    """Human-readable formatter that includes correlation context.

    Format: ``TIMESTAMP LEVEL [logger] [task_id=X project_id=Y] message``

    Used when ``structured_logging.format`` is set to ``"text"`` (the default)
    for easier local development and debugging.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Build correlation tag
        ctx = get_correlation_context()
        ctx_parts = [f"{k}={v}" for k, v in ctx.items()]
        ctx_str = f" [{' '.join(ctx_parts)}]" if ctx_parts else ""

        msg = record.getMessage()
        base = f"{ts} {record.levelname:<8} [{record.name}]{ctx_str} {msg}"

        if record.exc_info and record.exc_info[1] is not None:
            base += "\n" + self.formatException(record.exc_info)

        return base


# ── Setup function ───────────────────────────────────────────────────────


def setup_logging(
    *,
    level: str = "INFO",
    format: str = "text",
    include_source: bool = False,
) -> None:
    """Configure the root logger with structured or human-readable output.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        format: Output format — ``"json"`` for JSON-lines, ``"text"`` for
            human-readable (default).
        include_source: Include filename/lineno in JSON output.
    """
    root = logging.getLogger()

    # Remove existing handlers to avoid duplicate output
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)

    if format == "json":
        handler.setFormatter(StructuredFormatter(include_source=include_source))
    else:
        handler.setFormatter(HumanReadableFormatter())

    log_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(log_level)
    handler.setLevel(log_level)
    root.addHandler(handler)

    # Quiet down noisy third-party loggers
    for noisy in ("discord", "discord.http", "discord.gateway", "aiohttp"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))

    # Attach the Discord rate-guard log handler so we can count 429
    # responses that discord.py retries internally (never reaching our
    # application code).  Uses a lazy import to avoid circular deps at
    # module load time.
    from src.discord.rate_guard import DiscordHTTPLogHandler, get_tracker

    discord_http_logger = logging.getLogger("discord.http")
    # Avoid duplicate handlers on repeated setup_logging() calls
    if not any(isinstance(h, DiscordHTTPLogHandler) for h in discord_http_logger.handlers):
        discord_http_logger.addHandler(DiscordHTTPLogHandler(get_tracker()))
