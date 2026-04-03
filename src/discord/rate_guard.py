"""Discord invalid request rate guard.

Discord temporarily bans IPs that accumulate 10,000 invalid responses
(401, 403, 429) within a 10-minute sliding window.  This module provides:

- ``InvalidRequestTracker`` — a sliding-window counter with circuit-breaker
  thresholds (warn / critical / halt) that tells callers whether it is safe
  to make another Discord API call.

- ``DiscordHTTPLogHandler`` — a ``logging.Handler`` attached to the
  ``discord.http`` logger that intercepts discord.py's internal rate-limit
  retry warnings.  Each logged "responded with 429" message represents a
  real 429 response that counts toward the 10,000 threshold — even though
  our application code never sees it (discord.py retries internally).

- ``get_tracker()`` — module-level singleton accessor so every component
  shares a single counter.

Usage::

    from src.discord.rate_guard import get_tracker

    tracker = get_tracker()

    # Before making a Discord API call:
    if not tracker.should_allow(critical=False):
        return None  # silently drop non-critical call

    # After catching a Discord error:
    tracker.record(exc.status)

See https://discord.com/developers/docs/topics/rate-limits for details on
the invalid request limit.
"""

from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

# Discord's invalid request window and threshold
_WINDOW_SECONDS = 600  # 10 minutes
_DEFAULT_WARN = 1000
_DEFAULT_CRITICAL = 5000
_DEFAULT_HALT = 8000


class InvalidRequestTracker:
    """Sliding-window counter for Discord invalid responses (401/403/429).

    Tracks timestamps of invalid responses and provides threshold-based
    circuit-breaker states.  Thread-safe for single-threaded asyncio use
    (all access is from the event loop).

    Parameters
    ----------
    warn:
        Count at which a WARNING is logged (once per transition).
    critical:
        Count at which non-critical API calls should be dropped.
    halt:
        Count at which ALL API calls should be blocked.
    """

    def __init__(
        self,
        *,
        warn: int = _DEFAULT_WARN,
        critical: int = _DEFAULT_CRITICAL,
        halt: int = _DEFAULT_HALT,
    ) -> None:
        self._warn = warn
        self._critical = critical
        self._halt = halt
        self._events: deque[float] = deque()
        # Track which threshold alerts have been emitted so we log once
        # per transition rather than on every record() call.
        self._alerted_warn = False
        self._alerted_critical = False
        self._alerted_halt = False

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, status_code: int) -> None:
        """Record an invalid response (401, 403, or 429).

        Prunes expired entries and checks thresholds.
        """
        now = time.monotonic()
        self._prune(now)
        self._events.append(now)
        self._check_thresholds()

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    @property
    def count(self) -> int:
        """Number of invalid responses in the current 10-minute window."""
        self._prune(time.monotonic())
        return len(self._events)

    @property
    def state(self) -> str:
        """Current state: ``"ok"`` | ``"warn"`` | ``"critical"`` | ``"halt"``."""
        c = self.count
        if c >= self._halt:
            return "halt"
        if c >= self._critical:
            return "critical"
        if c >= self._warn:
            return "warn"
        return "ok"

    def should_allow(self, *, critical: bool = True) -> bool:
        """Pre-flight check: is it safe to make a Discord API call?

        Parameters
        ----------
        critical:
            ``True`` for essential calls (task notifications, thread creation).
            ``False`` for non-essential calls (progress updates, chunk overflow).

        Returns ``False`` when:
        - Count >= halt threshold (all calls blocked)
        - Count >= critical threshold AND ``critical=False``
        """
        c = self.count
        if c >= self._halt:
            return False
        if c >= self._critical and not critical:
            return False
        return True

    def reset(self) -> None:
        """Clear all recorded events and alert flags."""
        self._events.clear()
        self._alerted_warn = False
        self._alerted_critical = False
        self._alerted_halt = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        """Remove entries older than the 10-minute window."""
        cutoff = now - _WINDOW_SECONDS
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        # Reset alert flags when we drop below their thresholds so they
        # can fire again on a subsequent spike.
        c = len(self._events)
        if c < self._warn:
            self._alerted_warn = False
        if c < self._critical:
            self._alerted_critical = False
        if c < self._halt:
            self._alerted_halt = False

    def _check_thresholds(self) -> None:
        """Emit log messages on threshold transitions (once per crossing)."""
        c = len(self._events)
        if c >= self._halt and not self._alerted_halt:
            self._alerted_halt = True
            logger.critical(
                "Discord rate guard HALT: %d invalid requests in 10 min "
                "(threshold %d). ALL Discord API calls blocked until "
                "window expires.",
                c,
                self._halt,
            )
        elif c >= self._critical and not self._alerted_critical:
            self._alerted_critical = True
            logger.error(
                "Discord rate guard CRITICAL: %d invalid requests in 10 min "
                "(threshold %d). Non-critical Discord API calls will be "
                "dropped.",
                c,
                self._critical,
            )
        elif c >= self._warn and not self._alerted_warn:
            self._alerted_warn = True
            logger.warning(
                "Discord rate guard WARNING: %d invalid requests in 10 min "
                "(threshold %d). Approaching Discord's 10,000 limit.",
                c,
                self._warn,
            )


class DiscordHTTPLogHandler(logging.Handler):
    """Intercepts discord.py's internal rate-limit warning logs.

    discord.py logs every 429 response at WARNING level on the
    ``discord.http`` logger with messages like::

        We are being rate limited. GET https://... responded with 429.
        Retrying in 3.00 seconds.

    Each of these represents a real 429 from Discord that counts toward
    the 10,000 invalid request threshold — even though our application
    code never sees it (discord.py retries internally up to 5 times).

    This handler pattern-matches those messages and feeds them to the
    ``InvalidRequestTracker``.
    """

    def __init__(self, tracker: InvalidRequestTracker) -> None:
        super().__init__(level=logging.WARNING)
        self._tracker = tracker

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name != "discord.http":
                return
            if record.levelno < logging.WARNING:
                return
            msg = record.getMessage()
            if "responded with 429" in msg or "Global rate limit" in msg:
                self._tracker.record(429)
        except Exception:
            pass  # Never break logging


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_tracker: InvalidRequestTracker | None = None


def get_tracker() -> InvalidRequestTracker:
    """Return the module-level singleton tracker.

    The tracker is created on first access with default thresholds.
    Call ``configure_tracker()`` early in startup to override thresholds
    from config before any recording begins.
    """
    global _tracker
    if _tracker is None:
        _tracker = InvalidRequestTracker()
    return _tracker


def configure_tracker(
    *,
    warn: int = _DEFAULT_WARN,
    critical: int = _DEFAULT_CRITICAL,
    halt: int = _DEFAULT_HALT,
) -> InvalidRequestTracker:
    """Create (or reconfigure) the singleton tracker with custom thresholds.

    Should be called once during startup before the bot connects.
    """
    global _tracker
    _tracker = InvalidRequestTracker(warn=warn, critical=critical, halt=halt)
    return _tracker
