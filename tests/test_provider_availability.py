"""The pure availability reducer (provider-failover D1-D6), on a fake clock.

Every rule here is one row of the spec's D3 (trip) or D4 (recovery) tables,
or one of the consequences D3 spells out: one flaky launch yields at most
``degraded``, the 2026-09-20 incident trips in two launches, a broken
repository does not take a provider down, a model-scoped window is visible
but not acted on.
"""

from __future__ import annotations

import pytest

from src.config import ProviderFailoverConfig
from src.providers.availability import (
    AUTH_PROBE,
    AVAILABLE,
    DEGRADED,
    DISABLED,
    EXHAUSTED,
    EXIT_RATE_LIMIT,
    FAILING,
    LAUNCH_FAILURE,
    LAUNCH_SUCCESS,
    LLM_CALL,
    PROBE_AUTHENTICATED,
    PROBE_CANNOT_TELL,
    PROBE_NOT_AUTHENTICATED,
    RECOVERING,
    STARTUP_DIALOG,
    UNAUTHENTICATED,
    Evidence,
    ProviderAvailability,
    UsageReading,
    clear_override,
    dialog_from_startup_death,
    dialog_signal,
    half,
    provider_key,
    reduce,
    set_override,
    usage_view,
)

T0 = 1_789_958_640.0


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def tick(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class Driver:
    """Feed evidence through the reducer the way the service does."""

    def __init__(self, provider: str = "codex", config: ProviderFailoverConfig | None = None):
        self.clock = Clock()
        self.config = config or ProviderFailoverConfig()
        self.row = ProviderAvailability(provider=provider, vendor="openai", since=T0)
        self.transitions = []
        self.account: UsageReading | None = None
        self.scoped: UsageReading | None = None
        self.peers: frozenset[str] = frozenset()

    def feed(self, kind=None, signal=None, *, after: float = 1.0, **kw):
        now = self.clock.tick(after)
        evidence = None
        if kind is not None:
            evidence = Evidence(kind=kind, signal=signal, observed_at=now, **kw)
        result = reduce(
            self.row,
            evidence,
            now=now,
            config=self.config,
            account=self.account,
            scoped=self.scoped,
            peer_success_projects=self.peers,
        )
        self.row = result.row
        if result.transition is not None:
            self.transitions.append(result.transition)
        return result

    def tick(self, seconds: float):
        return self.feed(None, after=seconds)

    @property
    def state(self) -> str:
        return self.row.effective_state(self.clock.now)

    def dialog(self, signal="auth", project="p1", **kw):
        return self.feed(STARTUP_DIALOG, signal, project_id=project, **kw)

    def probe(self, answer, **kw):
        return self.feed(AUTH_PROBE, answer, **kw)

    def success(self, project="p1", **kw):
        return self.feed(LAUNCH_SUCCESS, project_id=project, **kw)

    def failure(self, project="p1", **kw):
        return self.feed(LAUNCH_FAILURE, project_id=project, **kw)


# -- helpers -----------------------------------------------------------------


def test_half():
    assert half(AVAILABLE) == half(DEGRADED) == "launchable"
    for state in (EXHAUSTED, UNAUTHENTICATED, FAILING, DISABLED):
        assert half(state) == "unavailable"


def test_provider_key_uses_base_then_id():
    class H:
        def __init__(self, id, base=""):
            self.id, self.base = id, base

    assert provider_key(H("codex")) == "codex"
    assert provider_key(H("codex-fast", base="codex")) == "codex"
    assert provider_key("claude") == "claude"


def test_dialog_signal_prefers_the_declared_signal_then_the_name_map():
    assert dialog_signal("login-required") == "auth"
    assert dialog_signal("rate-limit") == "usage"
    assert dialog_signal("usage-limit") == "usage"
    assert dialog_signal("trust-directory") is None
    assert dialog_signal("custom-wall", "auth") == "auth"


def test_dialog_from_startup_death_reads_structured_fields_then_detail_text():
    class Structured:
        dialog = "sign-in-wall"
        signal = "auth"
        detail = ""

    class Legacy:
        detail = "quarantine dialog 'login-required' matched during startup"

    class Plain:
        detail = "process died before start"

    assert dialog_from_startup_death(Structured()) == ("sign-in-wall", "auth")
    assert dialog_from_startup_death(Legacy()) == ("login-required", "auth")
    assert dialog_from_startup_death(Plain()) == (None, None)


def test_usage_view_skips_stale_and_already_reset_windows_and_splits_scopes():
    now = T0
    rows = [
        {"window": "primary", "scope": "", "used_percent": 40, "observed_at": now - 10,
         "last_seen_at": now - 10, "resets_at": now + 3600},
        {"window": "secondary", "scope": "", "used_percent": 100, "observed_at": now - 10,
         "last_seen_at": now - 10, "resets_at": now - 1},  # already reset
        {"window": "week", "scope": "all models", "used_percent": 70, "observed_at": now - 5,
         "last_seen_at": now - 5, "resets_at": None},
        {"window": "week", "scope": "Opus", "used_percent": 100, "observed_at": now - 5,
         "last_seen_at": now - 5, "resets_at": None},
        {"window": "session", "scope": "", "used_percent": 99, "observed_at": now - 9999,
         "last_seen_at": now - 9999, "resets_at": None},  # stale
    ]
    account, scoped = usage_view(rows, now=now, stale_after=1500)
    assert (account.window, account.used_percent) == ("week", 70)
    assert (scoped.scope, scoped.used_percent) == ("Opus", 100)


# -- D3: one flaky launch yields at most degraded -----------------------------


def test_one_login_dialog_alone_is_only_degraded():
    d = Driver()
    d.dialog()
    assert d.state == DEGRADED
    assert d.row.reason_code == "suspect_auth"
    # available -> degraded is a change of effective state, but not of half:
    # it is audited, and it moves no task.
    assert d.row.generation == 1
    assert d.transitions[-1].to_state == DEGRADED
    assert not d.transitions[-1].half_changed


def test_one_stray_429_exit_is_only_degraded():
    d = Driver()
    d.feed(EXIT_RATE_LIMIT, "usage", session_id="s1", project_id="p1")
    assert d.state == DEGRADED
    assert d.row.reason_code == "rate_limit_exit"


def test_uncorroborated_signal_expires_after_the_window():
    d = Driver()
    d.dialog()
    assert d.state == DEGRADED
    d.tick(d.config.launch.window_seconds + 1)
    assert d.state == AVAILABLE


def test_launch_success_clears_an_uncorroborated_dialog():
    d = Driver()
    d.dialog()
    d.success()
    assert d.state == AVAILABLE
    assert d.row.consecutive_failures == 0


# -- D3: unauthenticated -------------------------------------------------------


def test_incident_trips_in_one_launch_when_the_probe_confirms():
    """2026-09-20: login dialog, then ``codex login status`` says not logged in."""
    d = Driver()
    d.dialog()
    d.probe(PROBE_NOT_AUTHENTICATED)
    assert d.state == UNAUTHENTICATED
    assert d.row.reason_code == "login_required"
    assert d.row.until is None
    assert d.transitions[-1].to_state == UNAUTHENTICATED


def test_incident_trips_on_the_second_launch_when_the_probe_cannot_tell():
    d = Driver()
    d.dialog()
    d.probe(PROBE_CANNOT_TELL)
    assert d.state == DEGRADED
    d.dialog(after=30)
    assert d.state == UNAUTHENTICATED
    assert "2 consecutive" in d.row.reason


def test_two_dialogs_trip_whatever_the_probe_says():
    """A token can be present and dead."""
    d = Driver()
    d.dialog()
    d.probe(PROBE_AUTHENTICATED)
    assert d.state == DEGRADED
    d.dialog()
    assert d.state == UNAUTHENTICATED


def test_a_success_between_dialogs_resets_the_count():
    d = Driver()
    d.dialog()
    d.success()
    d.dialog()
    assert d.state == DEGRADED


def test_cannot_tell_is_never_evidence():
    d = Driver()
    for _ in range(5):
        d.probe(PROBE_CANNOT_TELL, after=120)
    assert d.state == AVAILABLE


def test_two_scheduled_probes_a_minute_apart_trip():
    d = Driver()
    d.probe(PROBE_NOT_AUTHENTICATED)
    assert d.state == DEGRADED
    d.probe(PROBE_NOT_AUTHENTICATED, after=30)
    assert d.state == DEGRADED  # too close together to be two observations
    d.probe(PROBE_NOT_AUTHENTICATED, after=60)
    assert d.state == UNAUTHENTICATED
    assert d.row.reason_code == "probe_not_authenticated"


def test_a_session_success_outranks_one_probe():
    d = Driver()
    d.probe(PROBE_NOT_AUTHENTICATED)
    d.success()
    assert d.state == AVAILABLE


# -- D3: exhausted -------------------------------------------------------------


def test_account_wide_snapshot_at_the_threshold_exhausts_until_its_reset():
    d = Driver()
    d.account = UsageReading("primary", 100.0, observed_at=T0, resets_at=T0 + 7200)
    d.tick(1)
    assert d.state == EXHAUSTED
    assert d.row.until == T0 + 7200
    assert d.row.reason_code == "usage_exhausted"


def test_snapshot_without_a_reset_clock_uses_the_cooldown_backoff():
    d = Driver()
    d.account = UsageReading("primary", 99.5, observed_at=T0)
    d.tick(1)
    assert d.state == EXHAUSTED
    assert d.row.until == pytest.approx(d.clock.now + d.config.rate_limit.cooldown_seconds)


def test_high_snapshot_is_degraded_not_exhausted_with_hysteresis():
    d = Driver()
    d.account = UsageReading("primary", 90.0, observed_at=T0)
    d.tick(1)
    assert (d.state, d.row.reason_code) == (DEGRADED, "usage_high")
    d.account = UsageReading("primary", 84.0, observed_at=T0 + 1)
    d.tick(1)
    assert d.state == DEGRADED  # inside the 2-point band
    d.account = UsageReading("primary", 82.5, observed_at=T0 + 2)
    d.tick(1)
    assert d.state == AVAILABLE


def test_model_scoped_window_is_degraded_never_exhausted():
    d = Driver("claude")
    d.scoped = UsageReading("week", 100.0, observed_at=T0, scope="Opus")
    d.tick(1)
    assert (d.state, d.row.reason_code) == (DEGRADED, "model_scope_exhausted")
    assert "Opus" in d.row.reason


def test_two_usage_dialogs_exhaust():
    d = Driver("claude")
    d.dialog("usage")
    assert d.state == DEGRADED
    d.dialog("usage")
    assert d.state == EXHAUSTED
    assert d.row.reason_code == "usage_dialog"


def test_one_usage_dialog_plus_a_high_snapshot_exhausts():
    d = Driver("claude")
    d.account = UsageReading("session", 90.0, observed_at=T0, resets_at=T0 + 3600)
    d.dialog("usage")
    assert d.state == EXHAUSTED
    assert d.row.until == T0 + 3600


def test_rate_limit_exits_from_two_sessions_exhaust_one_session_twice_does_not():
    d = Driver()
    d.feed(EXIT_RATE_LIMIT, "usage", session_id="s1")
    d.feed(EXIT_RATE_LIMIT, "usage", session_id="s1")
    assert d.state == DEGRADED
    d.feed(EXIT_RATE_LIMIT, "usage", session_id="s2")
    assert d.state == EXHAUSTED
    assert d.row.reason_code == "rate_limited"


def test_rate_limit_exits_outside_the_window_do_not_combine():
    d = Driver()
    d.feed(EXIT_RATE_LIMIT, "usage", session_id="s1")
    d.feed(EXIT_RATE_LIMIT, "usage", session_id="s2", after=d.config.launch.window_seconds + 5)
    assert d.state == DEGRADED


# -- D3: failing and attribution -------------------------------------------------


def test_generic_failures_in_one_project_with_no_peer_are_unattributed():
    """A broken repository does not take a provider down."""
    d = Driver()
    for _ in range(d.config.launch.generic_failures_to_trip):
        d.failure("p1")
    assert (d.state, d.row.reason_code) == (DEGRADED, "launch_failures_unattributed")


def test_generic_failures_across_two_projects_fail():
    d = Driver()
    for i in range(d.config.launch.generic_failures_to_trip):
        d.failure(f"p{i % 2}")
    assert d.state == FAILING
    assert d.row.until == pytest.approx(d.clock.now + d.config.recovery.failing_backoff_seconds)


def test_generic_failures_fail_when_another_provider_launched_in_the_same_project():
    d = Driver()
    d.peers = frozenset({"p1"})
    for _ in range(d.config.launch.generic_failures_to_trip):
        d.failure("p1")
    assert d.state == FAILING


def test_fewer_than_the_threshold_is_not_even_degraded():
    d = Driver()
    for _ in range(d.config.launch.generic_failures_to_trip - 1):
        d.failure("p1")
    assert d.state == AVAILABLE
    assert d.row.consecutive_failures == d.config.launch.generic_failures_to_trip - 1


# -- D4: recovery ----------------------------------------------------------------


def _unauthenticated() -> Driver:
    d = Driver()
    d.dialog()
    d.dialog()
    assert d.state == UNAUTHENTICATED
    return d


def test_unauthenticated_recovers_to_probation_on_an_authenticated_probe():
    d = _unauthenticated()
    d.probe(PROBE_AUTHENTICATED)
    assert (d.state, d.row.reason_code) == (DEGRADED, RECOVERING)
    assert d.row.probation_from == UNAUTHENTICATED
    # The old dialogs must not re-trip it on the next tick.
    d.tick(5)
    assert (d.state, d.row.reason_code) == (DEGRADED, RECOVERING)


def test_probation_completes_on_the_first_success():
    d = _unauthenticated()
    d.probe(PROBE_AUTHENTICATED)
    d.success()
    assert d.state == AVAILABLE
    assert d.row.probation_from is None


def test_one_failure_on_probation_returns_with_a_doubled_backoff():
    d = Driver()
    d.account = UsageReading("primary", 100.0, observed_at=T0)
    d.tick(1)
    assert d.state == EXHAUSTED
    first_until = d.row.until
    d.account = None
    d.tick(first_until - d.clock.now + d.config.recovery.reset_grace_seconds)
    assert (d.state, d.row.reason_code) == (DEGRADED, RECOVERING)
    d.dialog("usage")
    assert d.state == EXHAUSTED
    assert d.row.level == 1
    assert d.row.until == pytest.approx(d.clock.now + 2 * d.config.rate_limit.cooldown_seconds)


def test_backoff_is_capped():
    d = Driver()
    d.row = ProviderAvailability(provider="codex", level=10, since=T0)
    for _ in range(d.config.launch.generic_failures_to_trip):
        d.failure(f"p{d.clock.now}")
    assert d.state == FAILING
    assert d.row.until == pytest.approx(d.clock.now + d.config.recovery.backoff_max_seconds)


def test_level_decays_after_a_quiet_flap_window():
    d = Driver()
    d.row = ProviderAvailability(provider="codex", level=3, last_trip_at=T0, since=T0)
    d.tick(d.config.recovery.flap_window_seconds + 1)
    assert d.row.level == 0


def test_exhausted_recovers_early_on_a_fresh_low_snapshot():
    d = Driver("claude")
    d.dialog("usage")
    d.dialog("usage")
    assert d.state == EXHAUSTED
    d.account = UsageReading("session", 12.0, observed_at=d.clock.now + 1)
    d.tick(2)
    assert (d.state, d.row.reason_code) == (DEGRADED, RECOVERING)


def test_a_running_session_success_moves_exhausted_to_probation():
    d = Driver()
    d.feed(EXIT_RATE_LIMIT, "usage", session_id="s1")
    d.feed(EXIT_RATE_LIMIT, "usage", session_id="s2")
    assert d.state == EXHAUSTED
    d.success()
    assert (d.state, d.row.reason_code) == (DEGRADED, RECOVERING)


def test_success_does_not_clear_unauthenticated():
    d = _unauthenticated()
    d.success()
    assert d.state == UNAUTHENTICATED


def test_failing_recovers_on_the_clock():
    d = Driver()
    for i in range(d.config.launch.generic_failures_to_trip):
        d.failure(f"p{i % 2}")
    assert d.state == FAILING
    d.tick(d.config.recovery.failing_backoff_seconds + 1)
    assert (d.state, d.row.reason_code) == (DEGRADED, RECOVERING)
    d.failure("p1")
    assert d.state == FAILING
    assert d.row.level == 1


def test_unauthenticated_outranks_exhausted():
    d = Driver()
    d.account = UsageReading("primary", 100.0, observed_at=T0, resets_at=T0 + 9000)
    d.tick(1)
    assert d.state == EXHAUSTED
    d.dialog()
    d.dialog()
    assert d.state == UNAUTHENTICATED


# -- D6: operator override -------------------------------------------------------


def test_disabled_override_forces_unavailable_and_expires_with_a_transition():
    d = Driver()
    result = set_override(
        d.row, DISABLED, until=d.clock.now + 60, by="human:cli", reason="rotating", now=d.clock.now
    )
    d.row = result.row
    assert d.state == DISABLED
    assert result.transition.to_state == DISABLED
    assert result.transition.actor == "human:cli"
    gen = d.row.generation
    d.tick(61)
    assert d.state == AVAILABLE
    assert d.row.override_state is None
    assert d.transitions[-1].from_state == DISABLED
    assert d.transitions[-1].to_state == AVAILABLE
    assert d.row.generation == gen + 1


def test_available_override_beats_evidence_but_evidence_keeps_being_derived():
    d = _unauthenticated()
    d.row = set_override(
        d.row, AVAILABLE, until=d.clock.now + 600, by="human:cli", reason="false positive",
        now=d.clock.now,
    ).row
    assert d.state == AVAILABLE
    assert d.row.state == UNAUTHENTICATED  # derived state still recorded
    d.tick(601)
    assert d.state == UNAUTHENTICATED


def test_available_override_must_expire():
    with pytest.raises(ValueError):
        set_override(
            ProviderAvailability(provider="codex"), AVAILABLE, until=None, by="x", reason="",
            now=T0,
        )


def test_auto_clears_resets_counters_and_puts_an_unavailable_provider_on_probation():
    d = _unauthenticated()
    d.row = set_override(
        d.row, DISABLED, until=None, by="human:cli", reason="x", now=d.clock.now
    ).row
    result = clear_override(d.row, by="human:cli", now=d.clock.tick(1), config=d.config)
    d.row = result.row
    assert d.row.override_state is None
    assert (d.state, d.row.reason_code) == (DEGRADED, RECOVERING)
    assert d.row.level == 0
    # The dialogs recorded before ``auto`` no longer count.
    d.tick(1)
    assert d.state == DEGRADED
    d.dialog()
    assert d.state == UNAUTHENTICATED  # but one fresh failure on probation does


def test_auto_still_rederives_a_structured_exhaustion():
    d = Driver()
    d.account = UsageReading("primary", 100.0, observed_at=T0, resets_at=T0 + 5000)
    d.tick(1)
    d.row = clear_override(
        d.row, by="human:cli", now=d.clock.tick(1), config=d.config, account=d.account
    ).row
    assert d.state == EXHAUSTED


# -- generation --------------------------------------------------------------------


def test_generation_moves_only_on_effective_change():
    d = Driver()
    d.success()
    d.success()
    assert d.row.generation == 0
    d.dialog()
    gen = d.row.generation
    d.dialog(after=0.5)  # still unavailable-bound: trips
    assert d.row.generation == gen + 1
    d.dialog()  # already unauthenticated: no change
    assert d.row.generation == gen + 1


def test_evidence_ring_is_bounded_newest_first():
    d = Driver()
    for _ in range(d.config.evidence.keep + 10):
        d.success()
    assert len(d.row.evidence) == d.config.evidence.keep
    assert d.row.evidence[0]["at"] == d.clock.now


# -- direct path (D13a) ----------------------------------------------------------


def test_llm_auth_rejection_is_unauthenticated_at_once():
    d = Driver("llm")
    d.feed(LLM_CALL, "auth")
    assert d.state == UNAUTHENTICATED


def test_llm_quota_honours_retry_after():
    d = Driver("llm")
    d.feed(LLM_CALL, "usage", detail={"retry_after": 120})
    assert d.state == EXHAUSTED
    assert d.row.until == pytest.approx(d.clock.now + 120)


def test_llm_transport_errors_fail_without_attribution():
    d = Driver("llm")
    for _ in range(d.config.launch.generic_failures_to_trip):
        d.feed(LLM_CALL, "error")
    assert d.state == FAILING
