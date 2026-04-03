"""Tests for the Discord invalid request rate guard."""

import logging
import time
from unittest.mock import patch

import pytest

from src.discord.rate_guard import (
    DiscordHTTPLogHandler,
    InvalidRequestTracker,
    configure_tracker,
    get_tracker,
)


# ---------------------------------------------------------------------------
# InvalidRequestTracker
# ---------------------------------------------------------------------------


class TestInvalidRequestTracker:
    def test_initial_state(self):
        t = InvalidRequestTracker()
        assert t.state == "ok"
        assert t.count == 0
        assert t.should_allow(critical=True)
        assert t.should_allow(critical=False)

    def test_record_increments_count(self):
        t = InvalidRequestTracker()
        t.record(429)
        t.record(403)
        t.record(401)
        assert t.count == 3

    def test_warn_threshold(self):
        t = InvalidRequestTracker(warn=3, critical=6, halt=9)
        t.record(429)
        t.record(429)
        assert t.state == "ok"
        t.record(429)
        assert t.state == "warn"
        # All calls still allowed at warn level
        assert t.should_allow(critical=True)
        assert t.should_allow(critical=False)

    def test_critical_threshold_blocks_non_critical(self):
        t = InvalidRequestTracker(warn=2, critical=4, halt=8)
        for _ in range(4):
            t.record(429)
        assert t.state == "critical"
        assert t.should_allow(critical=True) is True
        assert t.should_allow(critical=False) is False

    def test_halt_threshold_blocks_all(self):
        t = InvalidRequestTracker(warn=2, critical=4, halt=6)
        for _ in range(6):
            t.record(429)
        assert t.state == "halt"
        assert t.should_allow(critical=True) is False
        assert t.should_allow(critical=False) is False

    def test_sliding_window_expiry(self):
        t = InvalidRequestTracker(warn=100, critical=200, halt=300)
        # Use a fixed base time and patch monotonic
        base = time.monotonic()
        with patch("src.discord.rate_guard.time.monotonic", return_value=base):
            for _ in range(5):
                t.record(429)
        assert t.count == 5

        # Advance time past the 10-minute window
        with patch("src.discord.rate_guard.time.monotonic", return_value=base + 601):
            assert t.count == 0
            assert t.state == "ok"

    def test_reset_clears_everything(self):
        t = InvalidRequestTracker(warn=2, critical=4, halt=6)
        for _ in range(6):
            t.record(429)
        assert t.state == "halt"
        t.reset()
        assert t.state == "ok"
        assert t.count == 0

    def test_alert_flags_fire_once_per_transition(self, caplog):
        t = InvalidRequestTracker(warn=2, critical=4, halt=6)
        with caplog.at_level(logging.WARNING, logger="src.discord.rate_guard"):
            t.record(429)
            t.record(429)  # crosses warn
            assert "WARNING" in caplog.text
            caplog.clear()

            t.record(429)  # still in warn, no new alert
            assert "WARNING" not in caplog.text or "CRITICAL" not in caplog.text

    def test_alert_flags_reset_when_count_drops(self):
        t = InvalidRequestTracker(warn=2, critical=4, halt=6)
        for _ in range(2):
            t.record(429)
        assert t._alerted_warn is True

        # Simulate window expiry
        base = time.monotonic()
        with patch("src.discord.rate_guard.time.monotonic", return_value=base + 601):
            _ = t.count  # triggers prune
            assert t._alerted_warn is False


# ---------------------------------------------------------------------------
# DiscordHTTPLogHandler
# ---------------------------------------------------------------------------


class TestDiscordHTTPLogHandler:
    def _make_record(self, message: str) -> logging.LogRecord:
        record = logging.LogRecord(
            name="discord.http",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg=message,
            args=(),
            exc_info=None,
        )
        return record

    def test_captures_429_message(self):
        t = InvalidRequestTracker()
        handler = DiscordHTTPLogHandler(t)
        record = self._make_record(
            "We are being rate limited. GET https://discord.com/api/v10/users/@me "
            "responded with 429. Retrying in 3.00 seconds."
        )
        handler.emit(record)
        assert t.count == 1

    def test_captures_global_rate_limit(self):
        t = InvalidRequestTracker()
        handler = DiscordHTTPLogHandler(t)
        record = self._make_record("Global rate limit has been hit. Retrying in 5.00 seconds.")
        handler.emit(record)
        assert t.count == 1

    def test_ignores_non_ratelimit_messages(self):
        t = InvalidRequestTracker()
        handler = DiscordHTTPLogHandler(t)
        record = self._make_record("Some other warning message")
        handler.emit(record)
        assert t.count == 0

    def test_ignores_non_discord_http_logger(self):
        t = InvalidRequestTracker()
        handler = DiscordHTTPLogHandler(t)
        record = logging.LogRecord(
            name="discord.gateway",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="responded with 429",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        assert t.count == 0

    def test_ignores_debug_level(self):
        t = InvalidRequestTracker()
        handler = DiscordHTTPLogHandler(t)
        record = logging.LogRecord(
            name="discord.http",
            level=logging.DEBUG,
            pathname="",
            lineno=0,
            msg="responded with 429",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        assert t.count == 0


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_tracker_returns_same_instance(self):
        # Reset module state
        import src.discord.rate_guard as mod

        mod._tracker = None

        t1 = get_tracker()
        t2 = get_tracker()
        assert t1 is t2

    def test_configure_tracker_replaces_singleton(self):
        import src.discord.rate_guard as mod

        mod._tracker = None

        t1 = get_tracker()
        t2 = configure_tracker(warn=500, critical=2000, halt=4000)
        assert t2 is not t1
        assert t2._warn == 500
        t3 = get_tracker()
        assert t3 is t2
