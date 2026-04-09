"""Tests for the timer service (roadmap 5.3.7).

Tests cover:
  - Interval parsing (parse_interval, extract_timer_intervals)
  - TimerService lifecycle (start, stop, rebuild)
  - Tick-based event emission with monotonic clock control
  - Automatic trigger change detection (playbooks compiled/removed)
  - Minimum interval enforcement (1 minute)
  - Arbitrary interval support
  - Payload format (tick_time, interval)
  - time_until_next() helper
  - Edge cases: no playbooks, no timer triggers, multiple playbooks same interval
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.timer_service import (
    TimerService,
    extract_timer_intervals,
    parse_interval,
)


# ---------------------------------------------------------------------------
# parse_interval tests
# ---------------------------------------------------------------------------


class TestParseInterval:
    """Tests for the parse_interval() utility function."""

    def test_minutes(self):
        assert parse_interval("timer.30m") == 1800.0

    def test_hours(self):
        assert parse_interval("timer.4h") == 14400.0

    def test_one_minute(self):
        """1 minute is the minimum allowed interval."""
        assert parse_interval("timer.1m") == 60.0

    def test_one_hour(self):
        assert parse_interval("timer.1h") == 3600.0

    def test_24_hours(self):
        assert parse_interval("timer.24h") == 86400.0

    def test_large_interval(self):
        """Arbitrary large intervals are supported."""
        assert parse_interval("timer.168h") == 168 * 3600.0  # 1 week

    def test_sub_minute_rejected(self):
        """Intervals below 1 minute return None."""
        assert parse_interval("timer.0m") is None

    def test_zero_hours_rejected(self):
        assert parse_interval("timer.0h") is None

    def test_not_timer_prefix(self):
        assert parse_interval("git.commit") is None

    def test_invalid_format_no_unit(self):
        assert parse_interval("timer.30") is None

    def test_invalid_format_no_number(self):
        assert parse_interval("timer.m") is None

    def test_invalid_format_text(self):
        assert parse_interval("timer.thirty") is None

    def test_empty_string(self):
        assert parse_interval("") is None

    def test_timer_dot_only(self):
        assert parse_interval("timer.") is None

    def test_negative_not_matched(self):
        """Regex doesn't match negative numbers."""
        assert parse_interval("timer.-5m") is None

    def test_decimal_not_matched(self):
        """Only integer intervals are supported."""
        assert parse_interval("timer.1.5h") is None


class TestExtractTimerIntervals:
    """Tests for extract_timer_intervals() utility."""

    def test_mixed_triggers(self):
        triggers = ["git.commit", "timer.30m", "task.completed", "timer.4h"]
        result = extract_timer_intervals(triggers)
        assert result == {"timer.30m": 1800.0, "timer.4h": 14400.0}

    def test_no_timer_triggers(self):
        triggers = ["git.commit", "task.completed"]
        result = extract_timer_intervals(triggers)
        assert result == {}

    def test_empty_list(self):
        assert extract_timer_intervals([]) == {}

    def test_all_timer_triggers(self):
        triggers = ["timer.5m", "timer.1h", "timer.24h"]
        result = extract_timer_intervals(triggers)
        assert len(result) == 3
        assert result["timer.5m"] == 300.0
        assert result["timer.1h"] == 3600.0
        assert result["timer.24h"] == 86400.0

    def test_invalid_timer_triggers_skipped(self):
        triggers = ["timer.0m", "timer.30m", "timer.invalid"]
        result = extract_timer_intervals(triggers)
        assert result == {"timer.30m": 1800.0}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_playbook_manager(triggers: list[str] | None = None) -> MagicMock:
    """Create a mock PlaybookManager with configurable triggers."""
    manager = MagicMock()
    manager.get_all_triggers.return_value = triggers or []
    return manager


def _make_event_bus() -> AsyncMock:
    """Create a mock EventBus."""
    bus = AsyncMock()
    return bus


# ---------------------------------------------------------------------------
# TimerService lifecycle tests
# ---------------------------------------------------------------------------


class TestTimerServiceLifecycle:
    """Tests for start/stop/rebuild behavior."""

    def test_start_with_no_timer_playbooks(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["git.commit", "task.completed"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()

        assert service._running is True
        assert service.interval_count == 0
        assert service.active_intervals == {}

    def test_start_with_timer_playbooks(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m", "timer.4h", "git.commit"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()

        assert service._running is True
        assert service.interval_count == 2
        assert "timer.30m" in service.active_intervals
        assert "timer.4h" in service.active_intervals
        assert service.active_intervals["timer.30m"] == 1800.0
        assert service.active_intervals["timer.4h"] == 14400.0

    def test_stop(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()
        assert service.interval_count == 1

        service.stop()

        assert service._running is False
        assert service.interval_count == 0
        assert service.active_intervals == {}

    def test_rebuild_preserves_last_fire_times(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m", "timer.4h"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()

        # Record a fake last fire time for timer.30m
        service._last_fire["timer.30m"] = 12345.0

        # Rebuild (simulating a playbook recompilation)
        service.rebuild()

        # timer.30m should keep its fire time, timer.4h gets a new one
        assert service._last_fire["timer.30m"] == 12345.0
        assert service._last_fire["timer.4h"] != 12345.0

    def test_rebuild_adds_new_interval(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()
        assert service.interval_count == 1

        # Simulate playbook with new timer being added
        manager.get_all_triggers.return_value = ["timer.30m", "timer.1h"]
        service.rebuild()

        assert service.interval_count == 2
        assert "timer.1h" in service.active_intervals

    def test_rebuild_removes_old_interval(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m", "timer.4h"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()
        assert service.interval_count == 2

        # Simulate playbook removal — only timer.30m remains
        manager.get_all_triggers.return_value = ["timer.30m"]
        service.rebuild()

        assert service.interval_count == 1
        assert "timer.4h" not in service.active_intervals


# ---------------------------------------------------------------------------
# Tick and event emission tests
# ---------------------------------------------------------------------------


class TestTimerServiceTick:
    """Tests for the tick() method and event emission."""

    @pytest.mark.asyncio
    async def test_tick_not_running_emits_nothing(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        # Not started — should emit nothing
        count = await service.tick()
        assert count == 0
        bus.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_no_intervals_emits_nothing(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["git.commit"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        count = await service.tick()
        assert count == 0
        bus.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_before_interval_emits_nothing(self):
        """No emission when the interval hasn't elapsed yet."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        # Immediately after start, 30 minutes haven't passed
        count = await service.tick()
        assert count == 0
        bus.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_emits_after_interval_elapsed(self):
        """Event is emitted when the interval has elapsed."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        # Fast-forward: set last_fire to 61 seconds ago
        import time

        service._last_fire["timer.1m"] = time.monotonic() - 61

        count = await service.tick()
        assert count == 1
        bus.emit.assert_called_once()

        # Verify the event type and payload
        call_args = bus.emit.call_args
        assert call_args[0][0] == "timer.1m"
        payload = call_args[0][1]
        assert "tick_time" in payload
        assert payload["interval"] == "1m"

    @pytest.mark.asyncio
    async def test_tick_payload_format(self):
        """Verify the payload matches spec: tick_time (ISO) and interval (string)."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.5m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        service._last_fire["timer.5m"] = time.monotonic() - 301  # 5min + 1s

        await service.tick()

        payload = bus.emit.call_args[0][1]
        assert payload["interval"] == "5m"
        # tick_time should be an ISO 8601 string with timezone
        assert "T" in payload["tick_time"]
        assert "+" in payload["tick_time"] or "Z" in payload["tick_time"]

    @pytest.mark.asyncio
    async def test_tick_multiple_intervals_only_elapsed_fire(self):
        """Only intervals that have elapsed should fire."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m", "timer.30m", "timer.4h"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        now = time.monotonic()
        # 1m: elapsed (2 minutes ago)
        service._last_fire["timer.1m"] = now - 120
        # 30m: not elapsed (10 minutes ago)
        service._last_fire["timer.30m"] = now - 600
        # 4h: not elapsed (1 hour ago)
        service._last_fire["timer.4h"] = now - 3600

        count = await service.tick()
        assert count == 1

        call_args = bus.emit.call_args
        assert call_args[0][0] == "timer.1m"

    @pytest.mark.asyncio
    async def test_tick_updates_last_fire_time(self):
        """After emission, the last_fire time should be updated to now."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        old_fire = time.monotonic() - 120
        service._last_fire["timer.1m"] = old_fire

        await service.tick()

        # last_fire should be updated to approximately now
        assert service._last_fire["timer.1m"] > old_fire
        assert service._last_fire["timer.1m"] >= time.monotonic() - 1

    @pytest.mark.asyncio
    async def test_tick_wont_double_fire(self):
        """After firing, the same interval shouldn't fire again on the next tick."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        service._last_fire["timer.1m"] = time.monotonic() - 120

        # First tick fires
        count1 = await service.tick()
        assert count1 == 1

        bus.reset_mock()

        # Second tick shouldn't fire (only ~0s since last fire)
        count2 = await service.tick()
        assert count2 == 0
        bus.emit.assert_not_called()


# ---------------------------------------------------------------------------
# Automatic trigger change detection
# ---------------------------------------------------------------------------


class TestTriggerChangeDetection:
    """Tests for automatic rebuild when playbook triggers change."""

    @pytest.mark.asyncio
    async def test_tick_detects_new_timer_trigger(self):
        """When a playbook with a timer trigger is compiled, tick detects it."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["git.commit"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        assert service.interval_count == 0

        # Simulate playbook compilation adding a timer trigger
        manager.get_all_triggers.return_value = ["git.commit", "timer.30m"]

        await service.tick()

        assert service.interval_count == 1
        assert "timer.30m" in service.active_intervals

    @pytest.mark.asyncio
    async def test_tick_detects_removed_timer_trigger(self):
        """When a playbook is removed, tick detects the trigger change."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m", "timer.4h"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        assert service.interval_count == 2

        # Simulate playbook removal — only timer.4h remains
        manager.get_all_triggers.return_value = ["timer.4h"]

        await service.tick()

        assert service.interval_count == 1
        assert "timer.30m" not in service.active_intervals
        assert "timer.4h" in service.active_intervals

    @pytest.mark.asyncio
    async def test_tick_no_rebuild_when_triggers_unchanged(self):
        """No rebuild when triggers haven't changed."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        with patch.object(service, "rebuild") as mock_rebuild:
            await service.tick()
            mock_rebuild.assert_not_called()


# ---------------------------------------------------------------------------
# time_until_next tests
# ---------------------------------------------------------------------------


class TestTimeUntilNext:
    """Tests for the time_until_next() helper."""

    def test_unknown_trigger_returns_none(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager([])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        assert service.time_until_next("timer.30m") is None

    def test_returns_positive_remaining_time(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # Set last fire to 10 minutes ago — should have ~20 minutes left
        service._last_fire["timer.30m"] = time.monotonic() - 600

        remaining = service.time_until_next("timer.30m")
        assert remaining is not None
        # Should be approximately 1200s (20 min), allow some tolerance
        assert 1195.0 <= remaining <= 1205.0

    def test_returns_zero_when_elapsed(self):
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        service._last_fire["timer.1m"] = time.monotonic() - 120  # 2 min ago

        remaining = service.time_until_next("timer.1m")
        assert remaining == 0.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_all_intervals_fire_simultaneously(self):
        """Multiple intervals can fire in the same tick."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m", "timer.5m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        now = time.monotonic()
        service._last_fire["timer.1m"] = now - 120  # Elapsed
        service._last_fire["timer.5m"] = now - 600  # Elapsed

        count = await service.tick()
        assert count == 2
        assert bus.emit.call_count == 2

        # Both event types should have been emitted
        emitted_types = {call.args[0] for call in bus.emit.call_args_list}
        assert emitted_types == {"timer.1m", "timer.5m"}

    @pytest.mark.asyncio
    async def test_stop_prevents_emission(self):
        """After stop(), tick() should not emit anything."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        service._last_fire["timer.1m"] = time.monotonic() - 120

        service.stop()

        count = await service.tick()
        assert count == 0
        bus.emit.assert_not_called()

    def test_new_interval_waits_full_cycle(self):
        """New intervals should wait one full cycle before first firing."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # The last_fire for the new interval should be approximately now
        fire_time = service._last_fire["timer.30m"]
        assert abs(fire_time - time.monotonic()) < 1.0

    @pytest.mark.asyncio
    async def test_rebuild_during_running(self):
        """Calling rebuild() while running should work correctly."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        # Add a new trigger
        manager.get_all_triggers.return_value = ["timer.1m", "timer.5m"]
        service.rebuild()

        assert service.interval_count == 2
        assert service._running is True

    @pytest.mark.asyncio
    async def test_hour_interval_payload(self):
        """Verify hour-based intervals produce correct payload."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.4h"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        service._last_fire["timer.4h"] = time.monotonic() - (4 * 3600 + 1)

        await service.tick()

        payload = bus.emit.call_args[0][1]
        assert payload["interval"] == "4h"

    def test_active_intervals_is_copy(self):
        """active_intervals property should return a copy, not a reference."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        intervals = service.active_intervals
        intervals["timer.99m"] = 9999.0

        # Internal state should not be modified
        assert "timer.99m" not in service._intervals
