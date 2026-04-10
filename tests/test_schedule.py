"""Tests for schedule matching logic."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.schedule import (
    _cron_field_matches,
    _matches_days_of_month,
    _matches_days_of_week,
    _matches_times,
    describe_schedule,
    format_next_run,
    matches_schedule,
    next_run_time,
    parse_schedule,
)


# ---------------------------------------------------------------------------
# Time matching
# ---------------------------------------------------------------------------


class TestTimeMatching:
    def test_exact_time_match(self):
        """Time matches when current time equals target."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert _matches_times(["02:00"], now, tolerance_seconds=60)

    def test_within_tolerance(self):
        """Time matches when within tolerance window."""
        now = datetime(2026, 3, 23, 2, 0, 45, tzinfo=timezone.utc)
        assert _matches_times(["02:00"], now, tolerance_seconds=60)

    def test_outside_tolerance(self):
        """Time does not match when outside tolerance window."""
        now = datetime(2026, 3, 23, 2, 2, 0, tzinfo=timezone.utc)
        assert not _matches_times(["02:00"], now, tolerance_seconds=60)

    def test_multiple_times(self):
        """Matches any of multiple specified times."""
        now = datetime(2026, 3, 23, 14, 30, 0, tzinfo=timezone.utc)
        assert _matches_times(["02:00", "14:30"], now, tolerance_seconds=60)

    def test_no_match_multiple(self):
        """No match when current time doesn't match any target."""
        now = datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc)
        assert not _matches_times(["02:00", "14:30"], now, tolerance_seconds=60)

    def test_midnight_tolerance(self):
        """Handles midnight wrap-around within tolerance."""
        now = datetime(2026, 3, 23, 23, 59, 30, tzinfo=timezone.utc)
        assert _matches_times(["00:00"], now, tolerance_seconds=60)

    def test_invalid_time_format(self):
        """Invalid time format is skipped gracefully."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not _matches_times(["invalid"], now, tolerance_seconds=60)

    def test_larger_tolerance(self):
        """Larger tolerance window captures more times."""
        now = datetime(2026, 3, 23, 2, 4, 0, tzinfo=timezone.utc)
        assert not _matches_times(["02:00"], now, tolerance_seconds=60)
        assert _matches_times(["02:00"], now, tolerance_seconds=300)


# ---------------------------------------------------------------------------
# Day-of-week matching
# ---------------------------------------------------------------------------


class TestDayOfWeekMatching:
    def test_match_by_name(self):
        """Match day by short name."""
        # 2026-03-23 is a Monday
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_week(["mon"], now)

    def test_match_by_full_name(self):
        """Match day by full name."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_week(["monday"], now)

    def test_no_match(self):
        """No match when day doesn't match."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        assert not _matches_days_of_week(["tue", "wed"], now)

    def test_match_by_integer(self):
        """Match day by integer (0=Monday)."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        assert _matches_days_of_week([0], now)

    def test_multiple_days(self):
        """Match any of multiple days."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        assert _matches_days_of_week(["mon", "wed", "fri"], now)

    def test_case_insensitive(self):
        """Day names are case-insensitive."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_week(["Mon"], now)
        assert _matches_days_of_week(["MONDAY"], now)

    def test_weekend(self):
        """Saturday and Sunday matching."""
        sat = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)  # Saturday
        assert _matches_days_of_week(["sat"], sat)
        assert _matches_days_of_week([5], sat)
        sun = datetime(2026, 3, 29, 12, 0, 0, tzinfo=timezone.utc)  # Sunday
        assert _matches_days_of_week(["sun"], sun)


# ---------------------------------------------------------------------------
# Day-of-month matching
# ---------------------------------------------------------------------------


class TestDayOfMonthMatching:
    def test_match(self):
        now = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_month([1, 15], now)

    def test_no_match(self):
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert not _matches_days_of_month([1, 15], now)

    def test_single_day(self):
        now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_month([1], now)

    def test_end_of_month(self):
        now = datetime(2026, 3, 31, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_month([31], now)


# ---------------------------------------------------------------------------
# Cron field matching
# ---------------------------------------------------------------------------


class TestCronFieldMatching:
    def test_wildcard(self):
        assert _cron_field_matches("*", 5, 0, 59)

    def test_exact(self):
        assert _cron_field_matches("5", 5, 0, 59)
        assert not _cron_field_matches("5", 6, 0, 59)

    def test_range(self):
        assert _cron_field_matches("1-5", 3, 0, 59)
        assert not _cron_field_matches("1-5", 6, 0, 59)

    def test_step(self):
        assert _cron_field_matches("*/15", 0, 0, 59)
        assert _cron_field_matches("*/15", 15, 0, 59)
        assert _cron_field_matches("*/15", 30, 0, 59)
        assert not _cron_field_matches("*/15", 10, 0, 59)

    def test_list(self):
        assert _cron_field_matches("1,3,5", 3, 0, 59)
        assert not _cron_field_matches("1,3,5", 4, 0, 59)

    def test_range_with_step(self):
        assert _cron_field_matches("1-10/2", 1, 0, 59)
        assert _cron_field_matches("1-10/2", 3, 0, 59)
        assert not _cron_field_matches("1-10/2", 2, 0, 59)
        assert not _cron_field_matches("1-10/2", 11, 0, 59)


# ---------------------------------------------------------------------------
# Full schedule matching
# ---------------------------------------------------------------------------


class TestScheduleMatching:
    def test_empty_schedule(self):
        """Empty schedule always matches."""
        assert matches_schedule({})

    def test_time_only(self):
        """Schedule with only time constraint."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"times": ["02:00"]}, now=now)

    def test_day_only(self):
        """Schedule with only day constraint."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)  # Monday
        assert matches_schedule({"days_of_week": ["mon"]}, now=now)

    def test_combined_time_and_day(self):
        """Schedule with both time and day (AND logic)."""
        # Monday at 2am
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"times": ["02:00"], "days_of_week": ["mon"]}, now=now)

    def test_combined_wrong_day(self):
        """Time matches but day doesn't → no match."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)  # Monday
        assert not matches_schedule({"times": ["02:00"], "days_of_week": ["tue"]}, now=now)

    def test_combined_wrong_time(self):
        """Day matches but time doesn't → no match."""
        now = datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc)  # Monday
        assert not matches_schedule({"times": ["02:00"], "days_of_week": ["mon"]}, now=now)

    def test_cron_simple(self):
        """Cron expression: every day at 2:00."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"cron": "0 2 * * *"}, now=now)

    def test_cron_wrong_time(self):
        """Cron expression doesn't match current time."""
        now = datetime(2026, 3, 23, 3, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 2 * * *"}, now=now)

    def test_cron_weekday_only(self):
        """Cron expression: weekdays only at 2am."""
        # Monday at 2am
        mon = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"cron": "0 2 * * 0-4"}, now=mon)

        # Saturday at 2am
        sat = datetime(2026, 3, 28, 2, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 2 * * 0-4"}, now=sat)

    def test_dedup_same_minute(self):
        """Cron dedup: don't fire twice in the same minute."""
        now = datetime(2026, 3, 23, 2, 0, 30, tzinfo=timezone.utc)
        last = datetime(2026, 3, 23, 2, 0, 5, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 2 * * *"}, now=now, last_run=last)

    def test_dedup_different_minute(self):
        """Cron fires if last run was in a different minute."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        last = datetime(2026, 3, 22, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"cron": "0 2 * * *"}, now=now, last_run=last)

    def test_time_dedup_same_window(self):
        """Time-based dedup: don't fire twice in the same time window."""
        now = datetime(2026, 3, 23, 2, 0, 30, tzinfo=timezone.utc)
        last = datetime(2026, 3, 23, 2, 0, 10, tzinfo=timezone.utc)
        assert not matches_schedule(
            {"times": ["02:00"]}, now=now, last_run=last, tolerance_seconds=60
        )

    def test_time_dedup_different_window(self):
        """Fires at next matching time window."""
        now = datetime(2026, 3, 23, 14, 30, 0, tzinfo=timezone.utc)
        last = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"times": ["02:00", "14:30"]}, now=now, last_run=last)

    def test_naive_datetime_handled(self):
        """Naive datetimes are treated as UTC."""
        now = datetime(2026, 3, 23, 2, 0, 0)  # no tzinfo
        assert matches_schedule({"times": ["02:00"]}, now=now)

    def test_every_third_day_at_3am(self):
        """Complex: 'every 3rd of month at 3 AM'."""
        now = datetime(2026, 3, 3, 3, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"times": ["03:00"], "days_of_month": [3]}, now=now)
        # Wrong day
        now2 = datetime(2026, 3, 4, 3, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"times": ["03:00"], "days_of_month": [3]}, now=now2)


# ---------------------------------------------------------------------------
# Cron expression matching (full 5-field)
# ---------------------------------------------------------------------------


class TestCronExpression:
    def test_every_15_minutes(self):
        """*/15 * * * * — every 15 minutes."""
        assert matches_schedule(
            {"cron": "*/15 * * * *"},
            now=datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert matches_schedule(
            {"cron": "*/15 * * * *"},
            now=datetime(2026, 3, 23, 10, 15, 0, tzinfo=timezone.utc),
        )
        assert not matches_schedule(
            {"cron": "*/15 * * * *"},
            now=datetime(2026, 3, 23, 10, 7, 0, tzinfo=timezone.utc),
        )

    def test_specific_month(self):
        """0 0 1 1 * — midnight on Jan 1."""
        jan1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"cron": "0 0 1 1 *"}, now=jan1)

        mar1 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 0 1 1 *"}, now=mar1)

    def test_invalid_cron(self):
        """Invalid cron expression returns False."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "invalid"}, now=now)

    def test_list_in_cron(self):
        """0 2 * * 0,2,4 — Mon/Wed/Fri at 2am."""
        mon = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)  # Monday=0
        assert matches_schedule({"cron": "0 2 * * 0,2,4"}, now=mon)

        tue = datetime(2026, 3, 24, 2, 0, 0, tzinfo=timezone.utc)  # Tuesday=1
        assert not matches_schedule({"cron": "0 2 * * 0,2,4"}, now=tue)


# ---------------------------------------------------------------------------
# parse_schedule / describe_schedule
# ---------------------------------------------------------------------------


class TestUtilities:
    def test_parse_schedule_present(self):
        trigger = {"type": "periodic", "interval_seconds": 3600, "schedule": {"times": ["02:00"]}}
        assert parse_schedule(trigger) == {"times": ["02:00"]}

    def test_parse_schedule_absent(self):
        trigger = {"type": "periodic", "interval_seconds": 3600}
        assert parse_schedule(trigger) is None

    def test_describe_time(self):
        desc = describe_schedule({"times": ["02:00", "14:30"]})
        assert "02:00" in desc
        assert "14:30" in desc

    def test_describe_days(self):
        desc = describe_schedule({"days_of_week": ["mon", "fri"]})
        assert "mon" in desc
        assert "fri" in desc

    def test_describe_cron(self):
        desc = describe_schedule({"cron": "0 2 * * *"})
        assert "Cron:" in desc

    def test_describe_empty(self):
        desc = describe_schedule({})
        assert "No schedule" in desc

    def test_describe_none(self):
        desc = describe_schedule(None)
        assert "No schedule" in desc


# ---------------------------------------------------------------------------
# Integration: schedule in hook tick (via HookEngine)
# ---------------------------------------------------------------------------


# TestHookEngineScheduleIntegration removed (playbooks spec §13 Phase 3).
# HookEngine has been removed; periodic automation is handled by playbooks
# via TimerService.


# ---------------------------------------------------------------------------
# Additional edge cases — time matching
# ---------------------------------------------------------------------------


class TestTimeMatchingEdgeCases:
    def test_empty_times_list(self):
        """Empty times list should not match."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not _matches_times([], now, tolerance_seconds=60)

    def test_time_with_leading_zeros(self):
        """Time strings with leading zeros."""
        now = datetime(2026, 3, 23, 0, 5, 0, tzinfo=timezone.utc)
        assert _matches_times(["00:05"], now, tolerance_seconds=60)

    def test_23_59_boundary(self):
        """Time near end of day."""
        now = datetime(2026, 3, 23, 23, 59, 0, tzinfo=timezone.utc)
        assert _matches_times(["23:59"], now, tolerance_seconds=60)

    def test_zero_tolerance(self):
        """Zero tolerance means only exact second match works."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert _matches_times(["02:00"], now, tolerance_seconds=0)
        now2 = datetime(2026, 3, 23, 2, 0, 1, tzinfo=timezone.utc)
        assert not _matches_times(["02:00"], now2, tolerance_seconds=0)

    def test_malformed_time_colon_only(self):
        """Malformed time like ':' should be skipped."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not _matches_times([":"], now, tolerance_seconds=60)

    def test_time_with_seconds_ignored(self):
        """Time string '02:00:30' — extra parts after HH:MM should not crash."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        # Should parse '02' and '00' from first two parts
        assert _matches_times(["02:00:30"], now, tolerance_seconds=60)

    def test_out_of_range_hour(self):
        """Hour > 23 should not match."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not _matches_times(["25:00"], now, tolerance_seconds=60)

    def test_out_of_range_minute(self):
        """Minute > 59 should not match."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not _matches_times(["02:70"], now, tolerance_seconds=60)


# ---------------------------------------------------------------------------
# Additional edge cases — day-of-week
# ---------------------------------------------------------------------------


class TestDayOfWeekEdgeCases:
    def test_empty_days_list(self):
        """Empty days list should not match."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert not _matches_days_of_week([], now)

    def test_integer_string(self):
        """Day given as string representation of integer."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)  # Monday=0
        assert _matches_days_of_week(["0"], now)

    def test_unknown_day_name(self):
        """Unknown day name should not match."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert not _matches_days_of_week(["notaday"], now)

    def test_mixed_types(self):
        """Mix of integer and string days."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        assert _matches_days_of_week([4, "mon"], now)  # fri or mon

    def test_all_days(self):
        """All days of the week should match any day."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        all_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        assert _matches_days_of_week(all_days, now)


# ---------------------------------------------------------------------------
# Additional edge cases — cron field matching
# ---------------------------------------------------------------------------


class TestCronFieldEdgeCases:
    def test_invalid_step_value(self):
        """Non-integer step should not match."""
        assert not _cron_field_matches("*/abc", 5, 0, 59)

    def test_step_zero_in_range(self):
        """Step with range but invalid step value."""
        assert not _cron_field_matches("1-10/abc", 3, 0, 59)

    def test_invalid_range(self):
        """Non-integer range bounds."""
        assert not _cron_field_matches("a-b", 3, 0, 59)

    def test_invalid_exact(self):
        """Non-integer exact value."""
        assert not _cron_field_matches("abc", 5, 0, 59)

    def test_complex_list(self):
        """List with ranges and steps."""
        # "1,5-10,*/30" — 1 matches, or 5-10, or multiples of 30
        assert _cron_field_matches("1,5-10,*/30", 1, 0, 59)
        assert _cron_field_matches("1,5-10,*/30", 7, 0, 59)
        assert _cron_field_matches("1,5-10,*/30", 30, 0, 59)
        assert not _cron_field_matches("1,5-10,*/30", 20, 0, 59)

    def test_range_boundary_values(self):
        """Range start and end are inclusive."""
        assert _cron_field_matches("5-10", 5, 0, 59)
        assert _cron_field_matches("5-10", 10, 0, 59)
        assert not _cron_field_matches("5-10", 4, 0, 59)
        assert not _cron_field_matches("5-10", 11, 0, 59)


# ---------------------------------------------------------------------------
# Additional edge cases — full schedule matching
# ---------------------------------------------------------------------------


class TestScheduleMatchingEdgeCases:
    def test_none_schedule_fields(self):
        """Schedule with only unknown fields = always matches."""
        assert matches_schedule({"unknown_field": "value"})

    def test_cron_too_few_fields(self):
        """Cron with fewer than 5 fields returns False."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 2 *"}, now=now)

    def test_cron_too_many_fields(self):
        """Cron with more than 5 fields returns False."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 2 * * * extra"}, now=now)

    def test_cron_empty_string(self):
        """Empty cron expression returns False."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": ""}, now=now)

    def test_days_of_month_and_week_combined(self):
        """Both days_of_month and days_of_week must match (AND)."""
        # 2026-03-23 is a Monday, day 23
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"days_of_week": ["mon"], "days_of_month": [23]}, now=now)
        # Right day of week, wrong day of month
        assert not matches_schedule({"days_of_week": ["mon"], "days_of_month": [15]}, now=now)

    def test_all_three_constraints(self):
        """Time + day_of_week + day_of_month all must match."""
        now = datetime(2026, 3, 23, 14, 30, 0, tzinfo=timezone.utc)  # Mon, day 23
        assert matches_schedule(
            {"times": ["14:30"], "days_of_week": ["mon"], "days_of_month": [23]},
            now=now,
        )
        # Wrong time
        assert not matches_schedule(
            {"times": ["10:00"], "days_of_week": ["mon"], "days_of_month": [23]},
            now=now,
        )

    def test_cron_with_schedule_fields_uses_cron(self):
        """If 'cron' key is present, other fields are ignored (cron takes precedence)."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        # Cron matches, but days_of_week would not (if it were checked)
        assert matches_schedule({"cron": "0 2 * * *", "days_of_week": ["sun"]}, now=now)

    def test_time_dedup_across_days(self):
        """Last run on different day does not block current match."""
        now = datetime(2026, 3, 24, 2, 0, 0, tzinfo=timezone.utc)
        last = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"times": ["02:00"]}, now=now, last_run=last)


# ---------------------------------------------------------------------------
# next_run_time
# ---------------------------------------------------------------------------


class TestNextRunTime:
    def test_cron_every_hour(self):
        """Next run for '0 * * * *' is the next whole hour."""
        now = datetime(2026, 3, 23, 10, 30, 0, tzinfo=timezone.utc)
        nxt = next_run_time({"cron": "0 * * * *"}, now=now)
        assert nxt is not None
        assert nxt.hour == 11
        assert nxt.minute == 0

    def test_cron_specific_time(self):
        """Next run for a specific daily cron."""
        now = datetime(2026, 3, 23, 3, 0, 0, tzinfo=timezone.utc)
        nxt = next_run_time({"cron": "0 2 * * *"}, now=now)
        assert nxt is not None
        # Should be 2am next day
        assert nxt.day == 24
        assert nxt.hour == 2

    def test_time_schedule_next(self):
        """Next run for a time-based schedule."""
        now = datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc)
        nxt = next_run_time({"times": ["14:30"]}, now=now)
        assert nxt is not None
        assert nxt.hour == 14
        # Within tolerance window (±60s), so might be 14:29 or 14:30
        assert nxt.minute in (29, 30)
        assert nxt.day == 23  # Same day

    def test_time_already_passed_today(self):
        """If time already passed today, should return tomorrow."""
        now = datetime(2026, 3, 23, 15, 1, 0, tzinfo=timezone.utc)
        nxt = next_run_time({"times": ["14:30"]}, now=now)
        assert nxt is not None
        assert nxt.day == 24

    def test_day_of_week_constraint(self):
        """Next run respects day-of-week."""
        # 2026-03-23 is Monday. Schedule for Wednesday only.
        now = datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc)
        nxt = next_run_time({"times": ["14:00"], "days_of_week": ["wed"]}, now=now)
        assert nxt is not None
        assert nxt.weekday() == 2  # Wednesday

    def test_empty_schedule_returns_none(self):
        """Empty schedule = pure interval, no deterministic next time."""
        assert next_run_time({}) is None

    def test_invalid_cron_returns_none(self):
        """Invalid cron: no match found within lookahead."""
        nxt = next_run_time({"cron": "invalid"}, max_lookahead_hours=1)
        assert nxt is None

    def test_none_schedule_returns_none(self):
        """None-like empty schedule returns None."""
        assert next_run_time({}) is None


# ---------------------------------------------------------------------------
# format_next_run
# ---------------------------------------------------------------------------


class TestFormatNextRun:
    def test_none_input(self):
        assert format_next_run(None) == "no upcoming run"

    def test_future_time(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2, minutes=15)
        result = format_next_run(future)
        assert "in" in result
        assert "2h" in result

    def test_imminent_time(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        result = format_next_run(future)
        assert "in" in result
        assert "5m" in result or "4m" in result  # Timing tolerance

    def test_past_time_shows_overdue(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = format_next_run(past)
        assert "overdue" in result

    def test_days_shown_for_far_future(self):
        future = datetime.now(timezone.utc) + timedelta(days=3, hours=5)
        result = format_next_run(future)
        assert "3d" in result


# ---------------------------------------------------------------------------
# describe_schedule additional tests
# ---------------------------------------------------------------------------


class TestDescribeScheduleExtended:
    def test_days_of_month(self):
        desc = describe_schedule({"days_of_month": [1, 15]})
        assert "1" in desc
        assert "15" in desc
        assert "day(s)" in desc

    def test_combined_times_and_days(self):
        desc = describe_schedule({"times": ["02:00"], "days_of_week": ["mon"]})
        assert "02:00" in desc
        assert "mon" in desc

    def test_all_fields(self):
        desc = describe_schedule(
            {
                "times": ["09:00"],
                "days_of_week": ["mon"],
                "days_of_month": [1],
            }
        )
        assert "09:00" in desc
        assert "mon" in desc
        assert "1" in desc

    def test_integer_days_in_description(self):
        desc = describe_schedule({"days_of_week": [0, 4]})
        assert "0" in desc
        assert "4" in desc
