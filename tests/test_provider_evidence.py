"""Provider availability collectors and the service around the reducer.

``docs/specs/provider-failover.md`` D2 (evidence sources), D4 (recovery and
the canary), D5 (the auth probe), D6 (override), D7 (state that survives a
restart, the transition log, ``provider.state_changed``) and D19's state
half (one idempotent message per change of half).  Real PostgreSQL, a fake
clock and an injected auth probe -- no CLI, no LLM.
"""

from __future__ import annotations

import pytest

from src.config import AppConfig
from src.database import Database
from src.models import AgentProfile, Project, SessionRecord
from src.providers.availability import (
    AVAILABLE,
    DEGRADED,
    DISABLED,
    EXHAUSTED,
    LAUNCH_FAILURE,
    LAUNCH_SUCCESS,
    RECOVERING,
    STARTUP_DIALOG,
    UNAUTHENTICATED,
)
from src.providers.availability_service import ProviderAvailabilityService
from src.providers.snapshot import ProviderUsageSnapshot
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.provider import SessionDiedDuringStartup
from tests.db_fixtures import lease_dsn

T0 = 1_789_958_640.0


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))

    def of(self, event_type):
        return [data for kind, data in self.events if kind == event_type]


class Probe:
    """An injected auth probe answering from a script."""

    def __init__(self, answer="authenticated"):
        self.answer = answer
        self.calls: list[str] = []

    async def __call__(self, provider, timeout):
        self.calls.append(provider)
        return self.answer


@pytest.fixture
async def db():
    database = Database(lease_dsn("provider-evidence.db"))
    await database.initialize()
    await database.create_profile(AgentProfile(id="std-codex", name="c", harness="codex"))
    await database.create_profile(AgentProfile(id="std-claude", name="c", harness="claude"))
    yield database
    await database.close()


def registry() -> HarnessRegistry:
    reg = HarnessRegistry()
    reg.upsert(Harness(id="codex", name="codex", command="codex"))
    reg.upsert(Harness(id="claude", name="claude", command="claude"))
    reg.upsert(Harness(id="codex-fast", name="codex-fast", command="codex", base="codex"))
    return reg


@pytest.fixture
def env(db):
    clock = Clock()
    bus = Bus()
    probe = Probe()
    config = AppConfig()
    service = ProviderAvailabilityService(
        db=db,
        config_getter=lambda: config,
        bus=bus,
        harness_registry=registry(),
        probe=probe,
        clock=clock,
    )
    half_changes = []

    async def on_half(transition):
        half_changes.append(transition)

    service.on_half_change = on_half

    class Env:
        pass

    e = Env()
    e.db, e.clock, e.bus, e.probe, e.config, e.service = db, clock, bus, probe, config, service
    e.half_changes = half_changes
    return e


def login_death(name="s-1"):
    return SessionDiedDuringStartup(
        name,
        detail="quarantine dialog 'login-required' matched during startup",
        dialog="login-required",
        signal="auth",
    )


# -- keys ------------------------------------------------------------------------


def test_a_based_harness_shares_its_parents_provider(env):
    assert env.service.provider_for_harness("codex-fast") == "codex"
    assert env.service.provider_for_harness("codex") == "codex"
    assert env.service.provider_for_harness("unknown-cli") == "unknown-cli"


def test_vendor_alias_resolves_to_the_provider_key(env):
    assert env.service.resolve_provider("openai") == "codex"
    assert env.service.resolve_provider("anthropic") == "claude"
    assert env.service.resolve_provider("codex") == "codex"


# -- startup deaths (D2) ----------------------------------------------------------


async def test_the_incident_trips_in_one_launch_when_the_probe_confirms(env):
    """2026-09-20: a login dialog triggers ``codex login status``; it says no."""
    env.probe.answer = "not_authenticated"
    provider, signal = await env.service.record_startup_death(
        login_death(), harness="codex", project_id="p1", task_id="t1"
    )
    assert (provider, signal) == ("codex", "auth")
    assert env.service.effective_state("codex") == DEGRADED
    await env.service.wait_for_probes()
    assert env.probe.calls == ["codex"]
    assert env.service.effective_state("codex") == UNAUTHENTICATED
    assert env.service.suppresses("codex")
    admitted, why = env.service.admit_launch("codex")
    assert not admitted and "unauthenticated" in why


async def test_the_incident_trips_on_the_second_launch_when_the_probe_cannot_tell(env):
    env.probe.answer = "cannot_tell"
    await env.service.record_startup_death(login_death("s-1"), harness="codex", project_id="p1")
    await env.service.wait_for_probes()
    assert env.service.effective_state("codex") == DEGRADED
    env.clock.tick(30)
    await env.service.record_startup_death(login_death("s-2"), harness="codex", project_id="p1")
    assert env.service.effective_state("codex") == UNAUTHENTICATED


async def test_a_legacy_detail_string_still_classifies_the_dialog(env):
    exc = SessionDiedDuringStartup(
        "s", detail="quarantine dialog 'login-required' matched during startup"
    )
    _provider, signal = await env.service.record_startup_death(exc, harness="codex")
    assert signal == "auth"
    assert env.service.row("codex").evidence[0]["kind"] == STARTUP_DIALOG


async def test_an_unlabelled_startup_death_is_only_a_weak_launch_failure(env):
    exc = SessionDiedDuringStartup("s", detail="process died before start")
    _provider, signal = await env.service.record_startup_death(
        exc, harness="codex", project_id="p1"
    )
    assert signal is None
    row = env.service.row("codex")
    assert row.evidence[0]["kind"] == LAUNCH_FAILURE
    assert row.state == AVAILABLE
    assert env.probe.calls == []  # only an auth dialog asks the probe


async def test_attribution_of_a_startup_death(env):
    generic = SessionDiedDuringStartup("s", detail="process died before start")
    assert env.service.attributes_startup_death(login_death(), "codex")
    assert not env.service.attributes_startup_death(generic, "codex")
    env.probe.answer = "not_authenticated"
    await env.service.record_startup_death(login_death(), harness="codex")
    await env.service.wait_for_probes()
    # Once the provider is down, every startup death is its fault.
    assert env.service.attributes_startup_death(generic, "codex")


# -- persistence, events, notification (D7, D19) ----------------------------------


async def test_state_and_transitions_survive_a_restart(env):
    env.probe.answer = "not_authenticated"
    await env.service.record_startup_death(login_death(), harness="codex", project_id="p1")
    await env.service.wait_for_probes()
    transitions = await env.db.list_provider_transitions("codex")
    assert [t["to_state"] for t in transitions] == [UNAUTHENTICATED, DEGRADED]
    assert transitions[0]["from_state"] == DEGRADED

    fresh = ProviderAvailabilityService(
        db=env.db, config_getter=lambda: env.config, harness_registry=registry(),
        probe=env.probe, clock=env.clock,
    )
    await fresh.load()
    assert fresh.effective_state("codex") == UNAUTHENTICATED
    assert fresh.row("codex").generation == env.service.row("codex").generation


async def test_state_changes_are_announced_and_evidence_is_not_news(env):
    await env.service.record("codex", LAUNCH_SUCCESS, project_id="p1")
    await env.service.record("codex", LAUNCH_SUCCESS, project_id="p1")
    assert env.bus.of("provider.state_changed") == []
    env.probe.answer = "not_authenticated"
    await env.service.record_startup_death(login_death(), harness="codex", project_id="p1")
    await env.service.wait_for_probes()
    changed = env.bus.of("provider.state_changed")
    assert [(c["from_state"], c["to_state"]) for c in changed] == [
        (AVAILABLE, DEGRADED),
        (DEGRADED, UNAUTHENTICATED),
    ]
    assert changed[-1]["provider"] == "codex" and changed[-1]["vendor"] == "openai"
    # Only the change of half is a dashboard notification and a message.
    notices = env.bus.of("notify.provider_state")
    assert len(notices) == 1 and notices[0]["to_state"] == UNAUTHENTICATED
    assert "codex login" in notices[0]["remediation"]
    assert [t.to_state for t in env.half_changes] == [UNAUTHENTICATED]


async def test_notification_is_idempotent_per_generation(env):
    await env.db.create_project(Project(id="p1", name="p1", default_profile_id="std-codex"))
    env.probe.answer = "not_authenticated"
    await env.service.record_startup_death(login_death(), harness="codex", project_id="p1")
    await env.service.wait_for_probes()
    first = await env.service.notify_state_change("codex")
    assert first["outcome"] == "notified"
    assert len(first["message_ids"]) == 2
    again = await env.service.notify_state_change("codex")
    assert again["outcome"] == "already_notified"
    inbox = await env.db.list_messages(to_kind="user", to_id="dashboard")
    assert len([m for m in inbox if "codex" in (m.subject or "")]) == 1
    body = inbox[0].body
    assert "unauthenticated" in body and "aq provider status codex" in body


async def test_a_launchable_change_is_not_a_half_change_notification(env):
    await env.service.record_startup_death(login_death(), harness="codex")  # -> degraded
    result = await env.service.notify_state_change("codex")
    assert result["outcome"] == "not_a_half_change"


# -- usage snapshots (D3/D4) -------------------------------------------------------


async def test_an_exhausted_snapshot_trips_until_its_reset_then_recovers(env):
    await env.db.record_provider_usage([
        ProviderUsageSnapshot(
            provider="codex", window="primary", used_percent=100.0,
            observed_at=env.clock.now, source="transcript", resets_at=env.clock.now + 3600,
        )
    ])
    await env.service.tick()
    row = env.service.row("codex")
    assert row.effective_state(env.clock.now) == EXHAUSTED
    assert row.until == T0 + 3600
    # The audit ring records the reading that did it.
    assert row.evidence[0]["kind"] == "usage_snapshot"

    env.clock.tick(3600 + env.config.provider_failover.recovery.reset_grace_seconds)
    await env.service.tick()
    row = env.service.row("codex")
    assert (row.state, row.reason_code) == (DEGRADED, RECOVERING)
    await env.service.record("codex", LAUNCH_SUCCESS, project_id="p1")
    assert env.service.effective_state("codex") == AVAILABLE


async def test_a_stale_snapshot_never_trips(env):
    stale = env.clock.now - env.config.providers.codex_stale_after_seconds - 60
    await env.db.record_provider_usage([
        ProviderUsageSnapshot(
            provider="codex", window="primary", used_percent=100.0,
            observed_at=stale, source="transcript",
        )
    ])
    await env.service.tick()
    assert env.service.effective_state("codex") == AVAILABLE


async def test_rate_limit_exits_from_two_sessions_exhaust(env):
    for sid in ("s1", "s2"):
        session = SessionRecord(
            id=sid, project_id="p1", profile_id="std-codex", harness="codex",
            provider="fake", name=sid, lifecycle="task", state="running",
            work_dir="/tmp", epoch="e", instance_token="i", started_at=T0,
        )
        await env.service.record_rate_limit_exit(session, reason="usage limit reached")
    assert env.service.effective_state("codex") == EXHAUSTED


# -- launch_success from the session token (D2) -----------------------------------


async def test_a_sessions_first_authenticated_call_is_launch_success_once(env):
    await env.db.create_project(Project(id="p1", name="p1"))
    await env.db.create_session(SessionRecord(
        id="sess-1", project_id="p1", profile_id="std-codex", harness="codex",
        provider="fake", name="n", lifecycle="pool", state="running",
        work_dir="/tmp", epoch="e", instance_token="i", started_at=T0,
    ))
    await env.service.note_session_authenticated("sess-1")
    await env.service.note_session_authenticated("sess-1")
    successes = [e for e in env.service.row("codex").evidence if e["kind"] == LAUNCH_SUCCESS]
    assert len(successes) == 1
    assert successes[0]["session_id"] == "sess-1"
    assert env.service.row("codex").last_success_at == T0


async def test_the_token_store_hook_reports_each_validated_session(env):
    from src.api.auth import SessionTokenStore

    seen = []
    store = SessionTokenStore(env.db)
    store.on_session_seen = seen.append
    token = await store.mint(session_id="sess-9", task_id=None, project_id=None)
    assert (await store.validate(token)).session_id == "sess-9"
    assert await store.validate(token) is not None
    assert seen == ["sess-9", "sess-9"]  # the service dedups; the store reports

    def broken(_sid):
        raise RuntimeError("boom")

    store.on_session_seen = broken
    assert await store.validate(token) is not None  # never breaks auth


# -- recovery of unauthenticated (D4, D5) --------------------------------------------


async def test_unauthenticated_recovers_through_the_recovery_probe_and_a_canary(env):
    env.probe.answer = "not_authenticated"
    await env.service.record_startup_death(login_death(), harness="codex", project_id="p1")
    await env.service.wait_for_probes()
    assert env.service.effective_state("codex") == UNAUTHENTICATED

    env.probe.answer = "authenticated"
    env.clock.tick(env.config.provider_failover.recovery.auth_probe_interval_seconds + 1)
    await env.service.tick()
    await env.service.wait_for_probes()
    row = env.service.row("codex")
    assert (row.state, row.reason_code) == (DEGRADED, RECOVERING)

    # The canary: one launch admitted, the next refused until it succeeds.
    assert env.service.admit_launch("codex") == (True, None)
    admitted, why = env.service.admit_launch("codex")
    assert not admitted and "canary" in why
    await env.service.record("codex", LAUNCH_SUCCESS, project_id="p1")
    assert env.service.effective_state("codex") == AVAILABLE
    assert env.service.admit_launch("codex") == (True, None)
    assert env.service.admit_launch("codex") == (True, None)


async def test_recheck_runs_the_probe_now(env):
    env.probe.answer = "not_authenticated"
    result = await env.service.recheck("codex")
    assert result["probe"] == "not_authenticated"
    assert env.service.row("codex").last_probe_at == T0


async def test_the_background_probe_runs_on_its_interval_while_launchable(env):
    await env.service.tick()  # seeds rows for tracked providers
    await env.service.wait_for_probes()
    calls = len(env.probe.calls)
    assert calls >= 2  # claude and codex, both tracked through profiles
    await env.service.tick()
    await env.service.wait_for_probes()
    assert len(env.probe.calls) == calls  # not due again yet
    env.clock.tick(env.config.provider_failover.auth_probe.interval_seconds + 1)
    await env.service.tick()
    await env.service.wait_for_probes()
    assert len(env.probe.calls) == calls * 2


async def test_interval_zero_disables_the_background_probe(env):
    env.config.provider_failover.auth_probe.interval_seconds = 0
    await env.service.tick()
    await env.service.wait_for_probes()
    assert env.probe.calls == []


# -- override (D6) ---------------------------------------------------------------------


async def test_an_override_is_persisted_announced_and_expires(env):
    result = await env.service.set_state(
        "codex", DISABLED, by="human:cli", reason="rotating", until=T0 + 60
    )
    assert result.transition.to_state == DISABLED
    assert env.service.suppresses("codex")
    changed = env.bus.of("provider.state_changed")[-1]
    assert changed["override"] is True and changed["actor"] == "human:cli"
    env.clock.tick(61)
    await env.service.tick()
    assert env.service.effective_state("codex") == AVAILABLE
    last = (await env.db.list_provider_transitions("codex", limit=1))[0]
    assert (last["from_state"], last["to_state"]) == (DISABLED, AVAILABLE)


async def test_auto_puts_an_unavailable_provider_on_probation(env):
    env.probe.answer = "not_authenticated"
    await env.service.record_startup_death(login_death(), harness="codex")
    await env.service.wait_for_probes()
    await env.service.set_state("codex", "auto", by="human:cli")
    row = env.service.row("codex")
    assert (row.state, row.reason_code) == (DEGRADED, RECOVERING)
    assert row.counters_reset_at == T0


# -- modes (D22) --------------------------------------------------------------------------


async def test_observe_mode_tracks_state_but_suppresses_nothing(env):
    env.config.provider_failover.mode = "observe"
    env.probe.answer = "not_authenticated"
    await env.service.record_startup_death(login_death(), harness="codex")
    await env.service.wait_for_probes()
    assert env.service.is_unavailable("codex")
    assert not env.service.suppresses("codex")
    assert env.service.admit_launch("codex") == (True, None)
    assert env.service.suppressed_providers() == frozenset()


async def test_off_mode_records_nothing(env):
    env.config.provider_failover.mode = "off"
    assert await env.service.record("codex", STARTUP_DIALOG, "auth") is None
    assert env.service.row("codex") is None
    assert await env.service.tick() == []
