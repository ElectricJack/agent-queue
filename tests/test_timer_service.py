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
        """No emission when the interval hasn't elapsed since last fire."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # Simulate: timer just fired — 30 minutes haven't passed since last fire
        service._last_fire["timer.30m"] = time.monotonic()

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

    def test_new_interval_via_rebuild_waits_full_cycle(self):
        """Intervals added via rebuild() (not start()) wait one full cycle."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["git.commit"])  # No timers initially
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        # Add a timer playbook during runtime
        manager.get_all_triggers.return_value = ["git.commit", "timer.30m"]
        service.rebuild()

        import time

        # The last_fire for the newly-added interval should be approximately now
        # (meaning it waits one full cycle before first firing)
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


# ---------------------------------------------------------------------------
# Roadmap 5.3.10 — Timer service integration tests
# ---------------------------------------------------------------------------


class TestRoadmap5310:
    """Roadmap 5.3.10: Timer service spec compliance tests.

    Per [[playbooks#7. Event System]] Timer Service:
      (a) 30m timer receives synthetic events every 30 minutes
      (b) interval tolerance +/- 5 seconds
      (c) minimum 1-minute interval — sub-minute rejected
      (d) multiple intervals fire at independent cadences
      (e) timer continues firing (recurring) after each cycle
      (f) timer stops when playbook removed/disabled
      (g) restart resumes from config — fires immediately if overdue
    """

    # -- (a) playbook with trigger timer.30m receives timer event every 30 min --

    @pytest.mark.asyncio
    async def test_30m_timer_fires_every_30_minutes(self):
        """A playbook subscribed to timer.30m receives events at 30m cadence."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        now = time.monotonic()

        # Simulate 30 minutes elapsed since last fire
        service._last_fire["timer.30m"] = now - 1800  # 30 min ago

        count = await service.tick()
        assert count == 1

        call_args = bus.emit.call_args
        assert call_args[0][0] == "timer.30m"
        payload = call_args[0][1]
        assert payload["interval"] == "30m"
        assert "tick_time" in payload

    @pytest.mark.asyncio
    async def test_30m_timer_fires_second_cycle(self):
        """Timer.30m fires again after another 30 minutes (not just once)."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # First fire: 30m elapsed
        service._last_fire["timer.30m"] = time.monotonic() - 1800
        count1 = await service.tick()
        assert count1 == 1

        bus.reset_mock()

        # Second fire: another 30m elapsed
        service._last_fire["timer.30m"] = time.monotonic() - 1800
        count2 = await service.tick()
        assert count2 == 1
        bus.emit.assert_called_once()
        assert bus.emit.call_args[0][0] == "timer.30m"

    # -- (b) timer interval respected within tolerance (+/- 5 seconds) --

    @pytest.mark.asyncio
    async def test_interval_tolerance_fires_at_exact_interval(self):
        """Timer fires when exactly the interval has elapsed."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.5m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # Exactly 5 minutes (300s) elapsed
        service._last_fire["timer.5m"] = time.monotonic() - 300

        count = await service.tick()
        assert count == 1

    @pytest.mark.asyncio
    async def test_interval_tolerance_fires_within_5s_late(self):
        """Timer fires when up to 5s past the interval (within tolerance)."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.5m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # 5 minutes + 5 seconds elapsed — within tolerance
        service._last_fire["timer.5m"] = time.monotonic() - 305

        count = await service.tick()
        assert count == 1

    @pytest.mark.asyncio
    async def test_interval_tolerance_does_not_fire_early(self):
        """Timer does NOT fire when interval hasn't fully elapsed."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.5m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # 4 minutes 54 seconds elapsed — 6s early, outside tolerance
        service._last_fire["timer.5m"] = time.monotonic() - 294

        count = await service.tick()
        assert count == 0
        bus.emit.assert_not_called()

    # -- (c) minimum 1-minute interval — sub-minute rejected --

    def test_sub_minute_timer_30s_rejected(self):
        """timer.30s is rejected — 's' is not a valid unit."""
        assert parse_interval("timer.30s") is None

    def test_sub_minute_timer_45s_rejected(self):
        """timer.45s is rejected — seconds unit not supported."""
        assert parse_interval("timer.45s") is None

    def test_sub_minute_timer_0m_rejected(self):
        """timer.0m is rejected — 0 minutes is below minimum."""
        assert parse_interval("timer.0m") is None

    @pytest.mark.asyncio
    async def test_sub_minute_timer_not_tracked(self):
        """Playbooks with sub-minute triggers are ignored by the timer service."""
        bus = _make_event_bus()
        # Include triggers that would be sub-minute if parsed
        manager = _make_playbook_manager(["timer.30s", "timer.0m", "timer.5m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        # Only timer.5m should be tracked (the others are invalid)
        assert service.interval_count == 1
        assert "timer.5m" in service.active_intervals
        assert "timer.30s" not in service.active_intervals
        assert "timer.0m" not in service.active_intervals

    # -- (d) multiple intervals fire at independent cadences --

    @pytest.mark.asyncio
    async def test_multiple_intervals_independent_cadence(self):
        """Different timer intervals fire independently at their own cadence."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m", "timer.5m", "timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        now = time.monotonic()

        # After 2 minutes: only 1m should have fired (5m and 30m not yet)
        service._last_fire["timer.1m"] = now - 120  # 2min ago — elapsed
        service._last_fire["timer.5m"] = now - 120  # 2min ago — not elapsed (needs 5m)
        service._last_fire["timer.30m"] = now - 120  # 2min ago — not elapsed (needs 30m)

        count = await service.tick()
        assert count == 1
        assert bus.emit.call_args[0][0] == "timer.1m"

        bus.reset_mock()

        # After 6 minutes: 1m and 5m should fire, 30m still not
        now2 = time.monotonic()
        service._last_fire["timer.1m"] = now2 - 90  # 1.5min ago — elapsed
        service._last_fire["timer.5m"] = now2 - 360  # 6min ago — elapsed
        service._last_fire["timer.30m"] = now2 - 360  # 6min ago — not elapsed

        count = await service.tick()
        assert count == 2
        emitted_types = {call.args[0] for call in bus.emit.call_args_list}
        assert emitted_types == {"timer.1m", "timer.5m"}
        assert "timer.30m" not in emitted_types

        bus.reset_mock()

        # After 31 minutes: all three should fire
        now3 = time.monotonic()
        service._last_fire["timer.1m"] = now3 - 90  # elapsed
        service._last_fire["timer.5m"] = now3 - 360  # elapsed
        service._last_fire["timer.30m"] = now3 - 1860  # 31min — elapsed

        count = await service.tick()
        assert count == 3
        emitted_types = {call.args[0] for call in bus.emit.call_args_list}
        assert emitted_types == {"timer.1m", "timer.5m", "timer.30m"}

    # -- (e) timer continues firing after playbook run completes (recurring) --

    @pytest.mark.asyncio
    async def test_timer_recurring_fires_repeatedly(self):
        """Timer keeps firing on each interval — it is not one-shot."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # Simulate 5 consecutive firing cycles
        for cycle in range(5):
            service._last_fire["timer.1m"] = time.monotonic() - 61
            count = await service.tick()
            assert count == 1, f"Timer should fire on cycle {cycle + 1}"

        # Total emissions across all cycles
        assert bus.emit.call_count == 5

    @pytest.mark.asyncio
    async def test_timer_recurring_not_affected_by_playbook_completion(self):
        """Simulated playbook completion does not stop the timer.

        The timer service fires events independently of playbook run status.
        Even after a playbook run triggered by a timer event completes, the
        timer continues to fire at its interval.
        """
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.5m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        import time

        # First fire
        service._last_fire["timer.5m"] = time.monotonic() - 301
        count1 = await service.tick()
        assert count1 == 1

        # (Playbook would run and complete here — timer service doesn't care)

        bus.reset_mock()

        # Second fire after another interval — timer is still active
        service._last_fire["timer.5m"] = time.monotonic() - 301
        count2 = await service.tick()
        assert count2 == 1
        bus.emit.assert_called_once()
        assert bus.emit.call_args[0][0] == "timer.5m"

    # -- (f) timer stops firing when playbook is removed/disabled --

    @pytest.mark.asyncio
    async def test_timer_stops_when_playbook_removed(self):
        """When the only playbook using a timer trigger is removed, the timer stops."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m", "git.commit"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        assert service.interval_count == 1
        assert "timer.30m" in service.active_intervals

        # Simulate playbook removal — timer trigger disappears
        manager.get_all_triggers.return_value = ["git.commit"]

        import time

        service._last_fire["timer.30m"] = time.monotonic() - 1801  # overdue

        # tick() detects the trigger change and rebuilds
        await service.tick()

        # Timer.30m should no longer be tracked
        assert service.interval_count == 0
        assert "timer.30m" not in service.active_intervals
        # No emission for the removed timer
        bus.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_timer_stops_when_playbook_disabled(self):
        """Disabling a playbook removes its timer trigger (same as removal)."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.5m", "timer.1h"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        assert service.interval_count == 2

        # Simulate one playbook being disabled — its trigger disappears
        manager.get_all_triggers.return_value = ["timer.1h"]

        import time

        service._last_fire["timer.5m"] = time.monotonic() - 600  # would be overdue

        await service.tick()

        # Only timer.1h should remain
        assert service.interval_count == 1
        assert "timer.5m" not in service.active_intervals
        assert "timer.1h" in service.active_intervals

    @pytest.mark.asyncio
    async def test_removed_timer_does_not_fire_again(self):
        """After removal, even if clock advances, the removed timer never fires."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)
        service.start()

        # Fire once
        import time

        service._last_fire["timer.1m"] = time.monotonic() - 61
        count = await service.tick()
        assert count == 1

        bus.reset_mock()

        # Remove the playbook
        manager.get_all_triggers.return_value = []
        await service.tick()  # triggers rebuild

        assert service.interval_count == 0

        bus.reset_mock()

        # Even with further ticks, nothing fires
        count = await service.tick()
        assert count == 0
        bus.emit.assert_not_called()

    # -- (g) system restart resumes timers from config, fires immediately --

    @pytest.mark.asyncio
    async def test_restart_fires_immediately(self):
        """After a restart (start()), all timers fire on the first tick.

        Per spec: 'system restart resumes timers from configuration
        (not from last fire time — fires immediately if overdue).'
        Since fire times are not persisted, all timers are treated as
        overdue on startup.
        """
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m", "timer.4h"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()

        # First tick after start — both should fire immediately
        count = await service.tick()
        assert count == 2

        emitted_types = {call.args[0] for call in bus.emit.call_args_list}
        assert emitted_types == {"timer.30m", "timer.4h"}

    @pytest.mark.asyncio
    async def test_restart_resumes_from_configuration(self):
        """After stop+start, timers are rebuilt from playbook configuration."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.5m", "timer.1h"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()
        assert service.interval_count == 2

        service.stop()
        assert service.interval_count == 0

        # Change configuration while stopped
        manager.get_all_triggers.return_value = ["timer.10m", "timer.2h"]

        service.start()

        # Should have the new intervals, not the old ones
        assert service.interval_count == 2
        assert "timer.10m" in service.active_intervals
        assert "timer.2h" in service.active_intervals
        assert "timer.5m" not in service.active_intervals
        assert "timer.1h" not in service.active_intervals

    @pytest.mark.asyncio
    async def test_restart_does_not_use_persisted_fire_times(self):
        """Restart ignores previous fire times — always fires immediately."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.30m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        # First start + fire
        service.start()
        count = await service.tick()
        assert count == 1  # fires immediately on startup

        bus.reset_mock()

        # Stop and restart
        service.stop()
        service.start()

        # Should fire immediately again — no memory of previous fire time
        count = await service.tick()
        assert count == 1
        assert bus.emit.call_args[0][0] == "timer.30m"

    @pytest.mark.asyncio
    async def test_restart_after_interval_wait_still_fires_immediately(self):
        """Even a short-interval timer fires immediately on restart."""
        bus = _make_event_bus()
        manager = _make_playbook_manager(["timer.1m"])
        service = TimerService(event_bus=bus, playbook_manager=manager)

        service.start()

        # First tick fires immediately (startup behavior)
        count = await service.tick()
        assert count == 1

        bus.reset_mock()

        # Immediately tick again — should NOT fire (only ~0s since last fire)
        count = await service.tick()
        assert count == 0

        bus.reset_mock()

        # Restart
        service.stop()
        service.start()

        # Fires immediately again on restart
        count = await service.tick()
        assert count == 1
