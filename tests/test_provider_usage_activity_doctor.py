"""A broken quota feed must not be mistaken for an idle fleet."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.doctor.provider_checks import ACTIVITY_GAP_CHECK_ID, _check_usage_activity_gap


def session(harness="codex", *, started=1000, activity=5000, id="live"):
    return SimpleNamespace(
        id=id, harness=harness, provider="tmux", project_id="p",
        started_at=started, last_activity=activity,
    )


def snapshot(provider="codex", *, observed=1000, seen=None):
    return {"provider": provider, "observed_at": observed, "last_seen_at": seen}


async def run(sessions, snapshots, *, config=None, handler=None):
    db = SimpleNamespace(
        latest_provider_usage=AsyncMock(return_value=snapshots),
        list_sessions=AsyncMock(return_value=sessions),
    )
    result = await _check_usage_activity_gap(DoctorContext(config=config, db=db, handler=handler))
    db.list_sessions.assert_awaited_once_with(live_only=True)
    return result


def test_activity_check_is_registered_and_report_only():
    check = next(c for c in default_registry().checks() if c.id == ACTIVITY_GAP_CHECK_ID)
    assert check.owner == "provider-usage"
    assert check.fix is None


@pytest.mark.parametrize("gap, severity", [(1799, Severity.OK), (1800, Severity.OK),
                                         (1801, Severity.WARN)])
async def test_warns_only_when_snapshot_trails_activity_over_thirty_minutes(gap, severity):
    result = await run([session(activity=1000 + gap)], [snapshot()])
    assert result.severity == severity
    if severity is Severity.WARN:
        assert result.data["gaps"][0]["provider"] == "codex"
        assert result.data["gaps"][0]["gap_seconds"] == gap


async def test_unchanged_percentage_recently_confirmed_is_healthy():
    result = await run([session()], [snapshot(seen=4900)])
    assert result.severity == Severity.OK


async def test_newest_confirmation_across_windows_is_used():
    result = await run([session()], [snapshot(observed=100), snapshot(seen=5000)])
    assert result.severity == Severity.OK


async def test_old_snapshot_with_no_live_sessions_is_not_a_fault():
    assert (await run([], [snapshot(observed=1)])).severity == Severity.OK


async def test_idle_session_activity_before_latest_snapshot_is_healthy():
    assert (await run([session(activity=900)], [snapshot()])).severity == Severity.OK


@pytest.mark.parametrize("activity, severity", [(2800, Severity.OK), (2801, Severity.WARN)])
async def test_absent_snapshot_allows_initial_thirty_minutes(activity, severity):
    result = await run([session(activity=activity)], [])
    assert result.severity == severity
    if severity is Severity.WARN:
        assert "no usage snapshot" in result.detail
        assert result.data["gaps"][0]["last_seen_at"] is None


async def test_newest_live_session_activity_is_compared_per_provider():
    result = await run([
        session(activity=1000, id="idle"), session(activity=5000, id="busy"),
        session("claude", activity=3000), session("gemini", activity=9000),
    ], [snapshot(), snapshot("claude", observed=3000)])
    assert result.severity == Severity.WARN
    assert [(g["provider"], g["session_id"]) for g in result.data["gaps"]] == [("codex", "busy")]


async def test_provider_without_a_quota_feed_is_excluded():
    assert (await run([session("gemini")], [])).severity == Severity.OK


async def test_inherited_harness_shares_its_base_providers_snapshot():
    registry = SimpleNamespace(get=Mock(return_value=SimpleNamespace(id="fast-codex", base="codex")))
    handler = SimpleNamespace(orchestrator=SimpleNamespace(harness_registry=registry))
    result = await run([session("fast-codex")], [snapshot()], handler=handler)
    assert result.severity == Severity.WARN
    assert result.data["gaps"][0]["provider"] == "codex"


async def test_disabled_claude_probe_does_not_warn_about_intentional_absence():
    config = SimpleNamespace(providers=SimpleNamespace(
        claude=SimpleNamespace(usage_probe_enabled=False),
    ))
    assert (await run([session("claude")], [], config=config)).severity == Severity.OK


async def test_missing_database_is_unknown():
    result = await _check_usage_activity_gap(DoctorContext(config=None))
    assert result.severity == Severity.INFO


async def test_failed_reads_are_unknown():
    db = SimpleNamespace(latest_provider_usage=AsyncMock(side_effect=RuntimeError("unavailable")))
    result = await _check_usage_activity_gap(DoctorContext(config=None, db=db))
    assert result.severity == Severity.INFO
