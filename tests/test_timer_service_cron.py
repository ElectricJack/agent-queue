"""Tests for ``cron.HH:MM`` wall-clock triggers in the timer service.

Covers:
  - parse_cron / extract_cron_targets
  - cron fires once per local day, once the wall clock hits the target
  - cron does not re-fire later the same day
  - cron fires again the next day
  - daemon restart does not re-fire a trigger already fired today (persistence)
  - daemon started late (after target) still fires same-day
  - DST "fall back" (two 01:30s) does not cause a double-fire
  - time_until_next() works for cron triggers
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.timer_service import (
    TimerService,
    extract_cron_targets,
    parse_cron,
)


# ---------------------------------------------------------------------------
# parse_cron tests
# ---------------------------------------------------------------------------


class TestParseCron:
    def test_morning(self):
        assert parse_cron("cron.07:00") == (7, 0)

    def test_evening(self):
        assert parse_cron("cron.17:30") == (17, 30)

    def test_midnight(self):
        assert parse_cron("cron.00:00") == (0, 0)

    def test_last_minute(self):
        assert parse_cron("cron.23:59") == (23, 59)

    def test_hour_out_of_range(self):
        assert parse_cron("cron.24:00") is None

    def test_minute_out_of_range(self):
        assert parse_cron("cron.07:60") is None

    def test_single_digit_hour(self):
        """Two-digit zero-padding is required."""
        assert parse_cron("cron.7:00") is None

    def test_trailing_seconds(self):
        assert parse_cron("cron.07:00:00") is None

    def test_not_cron_prefix(self):
        assert parse_cron("timer.07:00") is None

    def test_empty(self):
        assert parse_cron("") is None


class TestExtractCronTargets:
    def test_mixed(self):
        triggers = ["git.commit", "cron.08:00", "timer.30m", "cron.17:30"]
        assert extract_cron_targets(triggers) == {
            "cron.08:00": (8, 0),
            "cron.17:30": (17, 30),
        }

    def test_skips_invalid(self):
        triggers = ["cron.99:00", "cron.08:00"]
        assert extract_cron_targets(triggers) == {"cron.08:00": (8, 0)}

    def test_empty(self):
        assert extract_cron_targets([]) == {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_playbook_manager(triggers: list[str] | None = None) -> MagicMock:
    manager = MagicMock()
    manager.get_all_triggers.return_value = triggers or []
    return manager


def _make_bus() -> AsyncMock:
    return AsyncMock()


def _make_service(
    triggers: list[str],
    *,
    state_path: str | None = None,
) -> TimerService:
    bus = _make_bus()
    manager = _make_playbook_manager(triggers)
    return TimerService(
        event_bus=bus,
        playbook_manager=manager,
        state_path=state_path,
    )


def _fake_local(year: int, month: int, day: int, hour: int, minute: int) -> _dt.datetime:
    """Build a local-aware datetime for patching ``_now_local``."""
    return _dt.datetime(year, month, day, hour, minute, 0).astimezone()


# ---------------------------------------------------------------------------
# Firing behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_fires_at_or_after_target(monkeypatch):
    service = _make_service(["cron.08:00"])
    service.start()

    # 07:59 — before target, no fire.
    service._now_local = lambda: _fake_local(2026, 4, 19, 7, 59)
    assert await service.tick() == 0
    service._bus.emit.assert_not_called()

    # 08:00 — target reached, fire once.
    service._now_local = lambda: _fake_local(2026, 4, 19, 8, 0)
    assert await service.tick() == 1
    service._bus.emit.assert_awaited_once()
    trigger, payload = service._bus.emit.call_args.args
    assert trigger == "cron.08:00"
    assert payload["interval"] == "08:00"


@pytest.mark.asyncio
async def test_cron_does_not_refire_same_day():
    service = _make_service(["cron.08:00"])
    service.start()

    service._now_local = lambda: _fake_local(2026, 4, 19, 8, 0)
    await service.tick()

    # 08:05 same day — should not fire again.
    service._now_local = lambda: _fake_local(2026, 4, 19, 8, 5)
    count = await service.tick()
    assert count == 0
    assert service._bus.emit.await_count == 1


@pytest.mark.asyncio
async def test_cron_fires_next_day():
    service = _make_service(["cron.08:00"])
    service.start()

    service._now_local = lambda: _fake_local(2026, 4, 19, 8, 0)
    await service.tick()

    # Same trigger, next day 08:00 — should fire again.
    service._now_local = lambda: _fake_local(2026, 4, 20, 8, 0)
    count = await service.tick()
    assert count == 1
    assert service._bus.emit.await_count == 2


@pytest.mark.asyncio
async def test_missed_morning_still_fires_same_day():
    """Daemon started at 08:30, target was 08:00 — should still fire today."""
    service = _make_service(["cron.08:00"])
    service.start()

    service._now_local = lambda: _fake_local(2026, 4, 19, 8, 30)
    count = await service.tick()
    assert count == 1


@pytest.mark.asyncio
async def test_dst_fallback_no_double_fire():
    """Fall-back: 01:30 happens twice. Date-based dedup catches it."""
    service = _make_service(["cron.01:30"])
    service.start()

    # First 01:30 (DST still active)
    service._now_local = lambda: _fake_local(2026, 11, 1, 1, 30)
    assert await service.tick() == 1

    # Second 01:30 (after fall-back) — same local date, same wall time.
    service._now_local = lambda: _fake_local(2026, 11, 1, 1, 30)
    assert await service.tick() == 0
    assert service._bus.emit.await_count == 1


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_persisted_across_restart(tmp_path: Path):
    state_path = str(tmp_path / "timer_state.json")

    # First daemon run: fire at 08:00 on 2026-04-19.
    service1 = _make_service(["cron.08:00"], state_path=state_path)
    service1.start()
    service1._now_local = lambda: _fake_local(2026, 4, 19, 8, 0)
    assert await service1.tick() == 1
    service1.stop()

    # File was written.
    assert Path(state_path).exists()
    data = json.loads(Path(state_path).read_text())
    assert data["cron_last_fired_date"]["cron.08:00"] == "2026-04-19"

    # Second daemon run same day — should NOT re-fire.
    service2 = _make_service(["cron.08:00"], state_path=state_path)
    service2.start()
    service2._now_local = lambda: _fake_local(2026, 4, 19, 9, 0)
    assert await service2.tick() == 0
    service2._bus.emit.assert_not_called()


@pytest.mark.asyncio
async def test_state_missing_file_starts_fresh(tmp_path: Path):
    state_path = str(tmp_path / "does_not_exist.json")
    service = _make_service(["cron.08:00"], state_path=state_path)
    # Should not raise.
    service.start()
    assert service._cron_last_fired_date == {}


@pytest.mark.asyncio
async def test_state_corrupt_file_starts_fresh(tmp_path: Path):
    state_path = tmp_path / "timer_state.json"
    state_path.write_text("{not valid json")
    service = _make_service(["cron.08:00"], state_path=str(state_path))
    service.start()
    assert service._cron_last_fired_date == {}


# ---------------------------------------------------------------------------
# Rebuild + trigger-change detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_tracks_new_cron_trigger():
    service = _make_service(["cron.08:00"])
    service.start()
    assert "cron.08:00" in service.active_cron_targets
    assert "cron.17:30" not in service.active_cron_targets

    service._playbook_manager.get_all_triggers.return_value = [
        "cron.08:00",
        "cron.17:30",
    ]
    service.rebuild()

    assert service.active_cron_targets == {
        "cron.08:00": (8, 0),
        "cron.17:30": (17, 30),
    }


@pytest.mark.asyncio
async def test_rebuild_drops_fired_date_for_removed_trigger():
    service = _make_service(["cron.08:00"])
    service.start()
    service._cron_last_fired_date["cron.08:00"] = _dt.date(2026, 4, 19)

    # Trigger removed from playbook set.
    service._playbook_manager.get_all_triggers.return_value = []
    service.rebuild()

    assert service.active_cron_targets == {}
    assert "cron.08:00" not in service._cron_last_fired_date


@pytest.mark.asyncio
async def test_tick_autorebuilds_on_cron_change():
    service = _make_service(["cron.08:00"])
    service.start()
    assert service.cron_count == 1

    service._playbook_manager.get_all_triggers.return_value = ["cron.09:00"]
    # Before target so nothing should emit — but rebuild should run.
    service._now_local = lambda: _fake_local(2026, 4, 19, 7, 0)
    await service.tick()

    assert service.active_cron_targets == {"cron.09:00": (9, 0)}


# ---------------------------------------------------------------------------
# time_until_next for cron
# ---------------------------------------------------------------------------


def test_time_until_next_cron_before_target():
    service = _make_service(["cron.08:00"])
    service.start()
    service._now_local = lambda: _fake_local(2026, 4, 19, 7, 30)

    remaining = service.time_until_next("cron.08:00")
    assert remaining == pytest.approx(30 * 60, abs=1)


def test_time_until_next_cron_after_target_fired_points_to_tomorrow():
    service = _make_service(["cron.08:00"])
    service.start()
    service._cron_last_fired_date["cron.08:00"] = _dt.date(2026, 4, 19)
    service._now_local = lambda: _fake_local(2026, 4, 19, 8, 30)

    remaining = service.time_until_next("cron.08:00")
    # Should be ~23h30m.
    assert remaining == pytest.approx(23 * 3600 + 30 * 60, abs=5)


def test_time_until_next_unknown_returns_none():
    service = _make_service(["cron.08:00"])
    service.start()
    assert service.time_until_next("cron.12:00") is None
