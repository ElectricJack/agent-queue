"""Provider availability: the pure state machine behind "is Codex down?".

``docs/specs/provider-failover.md`` §1 (D0-D7).  Nothing here owns a clock,
touches the database or runs a subprocess: every rule is a function of the
stored row, one piece of evidence, the provider's newest usage reading and an
injected ``now``.  :class:`~src.providers.availability_service.
ProviderAvailabilityService` is the only caller that does I/O.

The unit is the **harness login** (D0): ``provider_key(harness) =
harness.base or harness.id`` — ``claude``, ``codex``, ``gemini`` — with the
vendor (``anthropic``/``openai``/``google``) carried beside it as a display
attribute and accepted by operator surfaces as an alias when unambiguous.

Six states in two halves (D1)::

    launchable   available | degraded
    unavailable  exhausted | unauthenticated | failing | disabled

``disabled`` is only ever an operator override (D6); the stored ``state`` is
the *derived* one and never holds it.  The effective state is the override
while one is active, else the derived state.

Hysteresis (D3): no single unstructured observation moves a provider out of
the launchable half.  A trip needs the provider's own structured statement
(a usage snapshot, an auth probe) or two independent signals.  Recovery
(D4) is a half-open breaker: an unavailable provider becomes ``degraded``
with ``reason_code = recovering`` and the first ``launch_success`` completes
it; a strong or medium failure while on probation returns it to the state it
came from with ``level + 1``, so the next backoff doubles.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any

__all__ = [
    "ACCOUNT_WIDE_SCOPES",
    "AVAILABLE",
    "DEGRADED",
    "DISABLED",
    "EXHAUSTED",
    "FAILING",
    "LAUNCHABLE",
    "STATES",
    "UNAUTHENTICATED",
    "UNAVAILABLE",
    "Evidence",
    "ProviderAvailability",
    "Reduction",
    "Transition",
    "UsageReading",
    "clear_override",
    "dialog_from_startup_death",
    "dialog_signal",
    "half",
    "provider_key",
    "reduce",
    "set_override",
    "usage_view",
    "vendor_of",
]

# -- states ------------------------------------------------------------------

AVAILABLE = "available"
DEGRADED = "degraded"
EXHAUSTED = "exhausted"
UNAUTHENTICATED = "unauthenticated"
FAILING = "failing"
DISABLED = "disabled"

STATES = (AVAILABLE, DEGRADED, EXHAUSTED, UNAUTHENTICATED, FAILING, DISABLED)
LAUNCHABLE = frozenset({AVAILABLE, DEGRADED})
UNAVAILABLE = frozenset({EXHAUSTED, UNAUTHENTICATED, FAILING, DISABLED})
#: Derived unavailable states, in trip precedence: a provider that is both
#: logged out and out of usage is reported as the thing a human can fix.
_DERIVED_UNAVAILABLE = (UNAUTHENTICATED, EXHAUSTED, FAILING)
_PRECEDENCE = {state: rank for rank, state in enumerate(_DERIVED_UNAVAILABLE)}

#: Override states an operator may set (D6).
OVERRIDE_STATES = (DISABLED, AVAILABLE)


def half(state: str) -> str:
    """``"launchable"`` or ``"unavailable"`` for *state*."""
    return "unavailable" if state in UNAVAILABLE else "launchable"


# -- evidence ----------------------------------------------------------------

USAGE_SNAPSHOT = "usage_snapshot"
AUTH_PROBE = "auth_probe"
STARTUP_DIALOG = "startup_dialog"
EXIT_RATE_LIMIT = "exit_rate_limit"
LAUNCH_FAILURE = "launch_failure"
LAUNCH_SUCCESS = "launch_success"
LLM_CALL = "llm_call"

EVIDENCE_KINDS = (
    USAGE_SNAPSHOT,
    AUTH_PROBE,
    STARTUP_DIALOG,
    EXIT_RATE_LIMIT,
    LAUNCH_FAILURE,
    LAUNCH_SUCCESS,
    LLM_CALL,
)

SIGNAL_AUTH = "auth"
SIGNAL_USAGE = "usage"
PROBE_AUTHENTICATED = "authenticated"
PROBE_NOT_AUTHENTICATED = "not_authenticated"
PROBE_CANNOT_TELL = "cannot_tell"
LLM_OK = "ok"
LLM_ERROR = "error"

# -- reason codes ------------------------------------------------------------

RECOVERING = "recovering"
OPERATOR_DISABLED = "operator_disabled"
OPERATOR_AVAILABLE = "operator_available"

#: Probes this close together are one observation, not two (D3 rule (c)).
_PROBE_MIN_SPACING_SECONDS = 60.0
#: ``2 ** level`` is capped well before it could overflow a float.
_MAX_LEVEL = 20

#: Scopes that describe the whole account rather than one model family.
ACCOUNT_WIDE_SCOPES = frozenset({"", "all models"})

#: Built-in dialog-name map, used only when a harness rule declares no
#: ``signal`` (D2): vault harness copies are operator-edited and do not pick
#: up shipped changes, so an un-reset vault must still classify its dialogs.
_DIALOG_SIGNAL_FALLBACK = {
    "login-required": SIGNAL_AUTH,
    "rate-limit": SIGNAL_USAGE,
    "usage-limit": SIGNAL_USAGE,
}

_QUARANTINE_DETAIL_RE = re.compile(r"quarantine dialog '([^']+)' matched")


def dialog_signal(name: str | None, declared: str | None = None) -> str | None:
    """What a quarantine dialog means: ``auth``, ``usage`` or ``None``."""
    if declared in (SIGNAL_AUTH, SIGNAL_USAGE):
        return declared
    return _DIALOG_SIGNAL_FALLBACK.get(str(name or "").strip().lower())


def dialog_from_startup_death(exc: object) -> tuple[str | None, str | None]:
    """``(dialog_name, signal)`` carried by a ``SessionDiedDuringStartup``.

    The structured ``dialog``/``signal`` attributes are read first; an
    exception raised by a provider that predates them is parsed from its
    ``detail`` text, which has named the dialog since the tmux provider
    learned quarantine rules.
    """
    name = getattr(exc, "dialog", None)
    declared = getattr(exc, "signal", None)
    if not name:
        match = _QUARANTINE_DETAIL_RE.search(str(getattr(exc, "detail", "") or ""))
        name = match.group(1) if match else None
    if not name:
        return None, None
    return str(name), dialog_signal(name, declared)


def provider_key(harness: object) -> str:
    """The availability key for *harness*: ``harness.base or harness.id`` (D0).

    A harness variant declared with ``base:`` shares its parent's login, so
    it shares its availability too.  Accepts a bare harness id string.
    """
    if isinstance(harness, str):
        return harness.strip()
    base = str(getattr(harness, "base", "") or "").strip()
    return base or str(getattr(harness, "id", "") or "").strip()


_VENDORS = {"claude": "anthropic", "codex": "openai", "gemini": "google"}


def vendor_of(harness: object) -> str:
    """The intelligence-class vendor a harness draws on, for display only."""
    declared = str(getattr(harness, "provider", "") or "").strip()
    if declared:
        return declared
    return _VENDORS.get(provider_key(harness), "")


@dataclass(frozen=True, slots=True)
class Evidence:
    """One typed observation about a provider (D2)."""

    kind: str
    signal: str | None = None
    observed_at: float = 0.0
    detail: Mapping[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    session_id: str | None = None
    project_id: str | None = None

    def as_entry(self) -> dict[str, Any]:
        """The JSON-safe ring entry stored on the row."""
        entry: dict[str, Any] = {"kind": self.kind, "at": float(self.observed_at)}
        if self.signal:
            entry["signal"] = self.signal
        for key in ("task_id", "session_id", "project_id"):
            value = getattr(self, key)
            if value:
                entry[key] = value
        if self.detail:
            entry["detail"] = dict(self.detail)
        return entry


@dataclass(frozen=True, slots=True)
class UsageReading:
    """The newest fresh reading of one limit window, as the reducer needs it."""

    window: str
    used_percent: float
    observed_at: float
    resets_at: float | None = None
    scope: str = ""

    def label(self) -> str:
        return f"{self.window} ({self.scope})" if self.scope else self.window


def usage_view(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: float,
    stale_after: float,
) -> tuple[UsageReading | None, UsageReading | None]:
    """``(account_wide, model_scoped)`` — the fullest fresh window of each kind.

    A row is ignored when it is older than *stale_after* (measured from
    ``last_seen_at``, like every other staleness rule over this table) or
    when its window has already reset: a 100 % reading whose ``resets_at``
    has passed describes a window that no longer exists, and treating it as
    live would hold a recovered provider down until the next snapshot.
    """
    account: UsageReading | None = None
    scoped: UsageReading | None = None
    for row in rows:
        seen = row.get("last_seen_at")
        seen = float(row["observed_at"] if seen is None else seen)
        if now - seen > stale_after:
            continue
        resets_at = row.get("resets_at")
        resets_at = None if resets_at is None else float(resets_at)
        if resets_at is not None and resets_at <= now:
            continue
        reading = UsageReading(
            window=str(row.get("window") or ""),
            scope=str(row.get("scope") or ""),
            used_percent=float(row["used_percent"]),
            observed_at=float(row["observed_at"]),
            resets_at=resets_at,
        )
        if reading.scope.strip().lower() in ACCOUNT_WIDE_SCOPES:
            if account is None or reading.used_percent > account.used_percent:
                account = reading
        elif scoped is None or reading.used_percent > scoped.used_percent:
            scoped = reading
    return account, scoped


# -- the row -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    """One ``provider_availability`` row (D7), plus its override."""

    provider: str
    vendor: str = ""
    #: The *derived* state; never ``disabled``.
    state: str = AVAILABLE
    reason_code: str = ""
    reason: str = ""
    since: float = 0.0
    until: float | None = None
    level: int = 0
    last_trip_at: float | None = None
    consecutive_failures: int = 0
    last_failure_at: float | None = None
    last_success_at: float | None = None
    #: Newest first, at most ``evidence.keep`` entries.
    evidence: tuple[Mapping[str, Any], ...] = ()
    override_state: str | None = None
    override_until: float | None = None
    override_by: str | None = None
    override_reason: str | None = None
    override_set_at: float | None = None
    #: Incremented on every change of *effective* state.
    generation: int = 0
    #: The unavailable state a ``recovering`` provider came from (D4).
    probation_from: str | None = None
    #: Evidence at or before this instant no longer counts toward a trip —
    #: written by ``auto`` (D6), the "I just ran ``codex login``" button.
    counters_reset_at: float | None = None
    #: When the auth probe last gave an answer (doctor's ``recovery_stuck``).
    last_probe_at: float | None = None
    #: The ``generation`` a state-change notification last went out for.
    notified_generation: int = 0
    updated_at: float = 0.0

    # -- effective view ------------------------------------------------------

    def override_active(self, now: float) -> bool:
        if not self.override_state:
            return False
        return self.override_until is None or self.override_until > now

    def effective_state(self, now: float) -> str:
        return self.override_state if self.override_active(now) else self.state

    def effective_reason_code(self, now: float) -> str:
        if self.override_active(now):
            return OPERATOR_DISABLED if self.override_state == DISABLED else OPERATOR_AVAILABLE
        return self.reason_code

    def effective_reason(self, now: float) -> str:
        if self.override_active(now):
            who = self.override_by or "operator"
            text = self.override_reason or "operator override"
            return f"{text} (set by {who})"
        return self.reason

    def effective_since(self, now: float) -> float:
        if self.override_active(now):
            return float(self.override_set_at or self.since)
        return self.since

    def effective_until(self, now: float) -> float | None:
        if self.override_active(now):
            return self.override_until
        return self.until

    def launchable(self, now: float) -> bool:
        return self.effective_state(now) in LAUNCHABLE

    def on_probation(self, now: float) -> bool:
        return (
            not self.override_active(now)
            and self.state == DEGRADED
            and self.reason_code == RECOVERING
        )

    def material(self) -> tuple:
        """Every field but ``updated_at`` — what decides whether to persist."""
        return tuple(getattr(self, f.name) for f in fields(self) if f.name != "updated_at")


@dataclass(frozen=True, slots=True)
class Transition:
    """One change of *effective* state, as the audit trail records it."""

    provider: str
    vendor: str
    from_state: str
    to_state: str
    reason_code: str
    reason: str
    since: float
    until: float | None
    generation: int
    actor: str
    override: bool
    at: float
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def half_changed(self) -> bool:
        return half(self.from_state) != half(self.to_state)

    def payload(self) -> dict[str, Any]:
        """The ``provider.state_changed`` event payload (D7)."""
        return {
            "provider": self.provider,
            "vendor": self.vendor,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "since": self.since,
            "until": self.until,
            "generation": self.generation,
            "actor": self.actor,
            "override": self.override,
        }


@dataclass(frozen=True, slots=True)
class Reduction:
    row: ProviderAvailability
    transition: Transition | None = None
    #: The row differs from the input in something other than ``updated_at``.
    changed: bool = False


# -- the reducer -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Derived:
    state: str
    reason_code: str
    reason: str
    until: float | None = None


def _cfg(config: Any, section: str, key: str, default: float) -> float:
    block = getattr(config, section, None)
    value = getattr(block, key, default) if block is not None else default
    return float(value if value is not None else default)


def _backoff(base: float, level: int, cap: float) -> float:
    return min(base * (2 ** min(max(level, 0), _MAX_LEVEL)), cap)


def _fmt_pct(value: float) -> str:
    return f"{value:g}%"


class _Ring:
    """Counting helpers over a row's evidence ring (newest first)."""

    def __init__(self, row: ProviderAvailability, now: float, window: float) -> None:
        floor = float(row.counters_reset_at or 0.0)
        self.entries = [e for e in row.evidence if float(e.get("at") or 0.0) > floor]
        self.now = now
        self.window = window
        self.since_success: list[Mapping[str, Any]] = []
        for entry in self.entries:
            if entry.get("kind") == LAUNCH_SUCCESS or (
                entry.get("kind") == LLM_CALL and entry.get("signal") == LLM_OK
            ):
                break
            self.since_success.append(entry)

    def _windowed(self, entries):
        edge = self.now - self.window
        return [e for e in entries if float(e.get("at") or 0.0) >= edge]

    def dialogs(self, signal: str, *, windowed: bool = False) -> list[Mapping[str, Any]]:
        found = [
            e
            for e in self.since_success
            if e.get("kind") == STARTUP_DIALOG and e.get("signal") == signal
        ]
        return self._windowed(found) if windowed else found

    def rate_limit_sessions(self) -> set[str]:
        exits = self._windowed([e for e in self.entries if e.get("kind") == EXIT_RATE_LIMIT])
        return {str(e.get("session_id") or f"anon-{i}") for i, e in enumerate(exits)}

    def generic_failures(self) -> list[Mapping[str, Any]]:
        return self._windowed(
            [
                e
                for e in self.since_success
                if e.get("kind") == LAUNCH_FAILURE
                or (e.get("kind") == LLM_CALL and e.get("signal") == LLM_ERROR)
            ]
        )

    def probes(self, *, after: float | None = None) -> list[Mapping[str, Any]]:
        """Auth probe answers that say something, newest first."""
        floor = float(after or 0.0)
        return [
            e
            for e in self.entries
            if e.get("kind") == AUTH_PROBE
            and e.get("signal") != PROBE_CANNOT_TELL
            and float(e.get("at") or 0.0) > floor
        ]

    def llm(self, signal: str) -> list[Mapping[str, Any]]:
        return [
            e for e in self.since_success if e.get("kind") == LLM_CALL and e.get("signal") == signal
        ]


def _exhausted_until(
    now: float, level: int, config: Any, account: UsageReading | None
) -> float:
    if account is not None and account.resets_at is not None and account.resets_at > now:
        return account.resets_at
    return now + _backoff(
        _cfg(config, "rate_limit", "cooldown_seconds", 900),
        level,
        _cfg(config, "recovery", "backoff_max_seconds", 3600),
    )


def _failing_until(now: float, level: int, config: Any) -> float:
    return now + _backoff(
        _cfg(config, "recovery", "failing_backoff_seconds", 300),
        level,
        _cfg(config, "recovery", "backoff_max_seconds", 3600),
    )


def _trip(
    row: ProviderAvailability,
    ring: _Ring,
    *,
    now: float,
    config: Any,
    account: UsageReading | None,
    peer_success_projects: frozenset[str],
    level: int,
) -> _Derived | None:
    """The unavailable state the evidence supports, or ``None`` (D3)."""
    strong = int(_cfg(config, "launch", "strong_failures_to_trip", 2))
    generic = int(_cfg(config, "launch", "generic_failures_to_trip", 5))
    exits_needed = int(_cfg(config, "rate_limit", "exits_to_trip", 2))
    exhausted_pct = _cfg(config, "usage", "exhausted_percent", 99)
    degraded_pct = _cfg(config, "usage", "degraded_percent", 85)
    usage_high = account is not None and account.used_percent >= degraded_pct

    # -- unauthenticated -------------------------------------------------
    auth_dialogs = ring.dialogs(SIGNAL_AUTH)
    # A session that made a successful call outranks a status command that
    # says otherwise: only probes newer than the last success count.
    probes = ring.probes(after=row.last_success_at)
    if auth_dialogs:
        oldest = min(float(e.get("at") or 0.0) for e in auth_dialogs)
        confirmed = [
            p
            for p in probes
            if p.get("signal") == PROBE_NOT_AUTHENTICATED and float(p.get("at") or 0.0) >= oldest
        ]
        if confirmed:
            return _Derived(
                UNAUTHENTICATED,
                "login_required",
                "a launch died on its login dialog and the login probe reports not signed in",
            )
    if len(auth_dialogs) >= strong:
        return _Derived(
            UNAUTHENTICATED,
            "login_required",
            f"{len(auth_dialogs)} consecutive launches died on the login dialog",
        )
    if (
        len(probes) >= 2
        and probes[0].get("signal") == PROBE_NOT_AUTHENTICATED
        and probes[1].get("signal") == PROBE_NOT_AUTHENTICATED
        and float(probes[0].get("at") or 0.0) - float(probes[1].get("at") or 0.0)
        >= _PROBE_MIN_SPACING_SECONDS
    ):
        return _Derived(
            UNAUTHENTICATED,
            "probe_not_authenticated",
            "the login probe reported not signed in twice in a row",
        )
    if ring.llm(SIGNAL_AUTH):
        return _Derived(UNAUTHENTICATED, "llm_auth_rejected", "the API rejected the credential")

    # -- exhausted -------------------------------------------------------
    if account is not None and account.used_percent >= exhausted_pct:
        return _Derived(
            EXHAUSTED,
            "usage_exhausted",
            f"{account.label()} window at {_fmt_pct(account.used_percent)}",
            _exhausted_until(now, level, config, account),
        )
    usage_dialogs = ring.dialogs(SIGNAL_USAGE)
    if len(usage_dialogs) >= strong or (usage_dialogs and usage_high):
        return _Derived(
            EXHAUSTED,
            "usage_dialog",
            f"{len(usage_dialogs)} launch(es) died on the usage-limit dialog"
            + (f"; {account.label()} at {_fmt_pct(account.used_percent)}" if usage_high else ""),
            _exhausted_until(now, level, config, account),
        )
    exit_sessions = ring.rate_limit_sessions()
    if len(exit_sessions) >= exits_needed or (exit_sessions and usage_high):
        return _Derived(
            EXHAUSTED,
            "rate_limited",
            f"{len(exit_sessions)} session(s) exited on a provider rate limit"
            + (f"; {account.label()} at {_fmt_pct(account.used_percent)}" if usage_high else ""),
            _exhausted_until(now, level, config, account),
        )
    llm_usage = ring.llm(SIGNAL_USAGE)
    if llm_usage:
        retry_after = (llm_usage[0].get("detail") or {}).get("retry_after")
        until = (
            float(llm_usage[0].get("at") or now) + float(retry_after)
            if retry_after
            else _exhausted_until(now, level, config, account)
        )
        return _Derived(EXHAUSTED, "llm_quota", "the API reported the quota exhausted", until)

    # -- failing ---------------------------------------------------------
    failures = ring.generic_failures()
    if len(failures) >= generic:
        projects = {str(e.get("project_id")) for e in failures if e.get("project_id")}
        llm_only = all(e.get("kind") == LLM_CALL for e in failures)
        if llm_only or len(projects) >= 2 or projects & peer_success_projects:
            return _Derived(
                FAILING,
                "launch_failures",
                f"{len(failures)} consecutive launches died during startup"
                + (f" across {len(projects)} projects" if len(projects) >= 2 else ""),
                _failing_until(now, level, config),
            )
    return None


def _launchable(
    row: ProviderAvailability,
    ring: _Ring,
    *,
    config: Any,
    account: UsageReading | None,
    scoped: UsageReading | None,
) -> _Derived:
    """``available`` or the ``degraded`` reason the evidence supports (D1/D3)."""
    generic = int(_cfg(config, "launch", "generic_failures_to_trip", 5))
    exhausted_pct = _cfg(config, "usage", "exhausted_percent", 99)
    degraded_pct = _cfg(config, "usage", "degraded_percent", 85)
    band = _cfg(config, "usage", "hysteresis_percent", 2)

    if ring.dialogs(SIGNAL_AUTH, windowed=True):
        return _Derived(
            DEGRADED,
            "suspect_auth",
            "one launch died on the login dialog; awaiting corroboration",
        )
    probes = ring.probes(after=row.last_success_at)
    if probes and probes[0].get("signal") == PROBE_NOT_AUTHENTICATED:
        return _Derived(
            DEGRADED,
            "probe_not_authenticated",
            "the login probe reported not signed in once; awaiting corroboration",
        )
    if ring.dialogs(SIGNAL_USAGE, windowed=True):
        return _Derived(
            DEGRADED,
            "suspect_usage",
            "one launch died on the usage-limit dialog; awaiting corroboration",
        )
    if ring.rate_limit_sessions():
        return _Derived(
            DEGRADED, "rate_limit_exit", "a session exited on a provider rate limit"
        )
    failures = ring.generic_failures()
    if len(failures) >= generic:
        return _Derived(
            DEGRADED,
            "launch_failures_unattributed",
            f"{len(failures)} launches died during startup in one project with no other "
            "provider launching there; not attributed to the provider",
        )
    if account is not None:
        was_high = row.state == DEGRADED and row.reason_code == "usage_high"
        threshold = degraded_pct - band if was_high else degraded_pct
        if account.used_percent >= threshold:
            return _Derived(
                DEGRADED,
                "usage_high",
                f"{account.label()} window at {_fmt_pct(account.used_percent)}",
            )
    if scoped is not None and scoped.used_percent >= exhausted_pct:
        return _Derived(
            DEGRADED,
            "model_scope_exhausted",
            f"model-scoped window {scoped.label()} at {_fmt_pct(scoped.used_percent)}",
        )
    return _Derived(AVAILABLE, "", "")


def _recovered(
    row: ProviderAvailability,
    evidence: Evidence | None,
    *,
    now: float,
    config: Any,
    account: UsageReading | None,
) -> str | None:
    """Why an unavailable derived state may go to probation now, or ``None`` (D4)."""
    kind = evidence.kind if evidence else None
    signal = evidence.signal if evidence else None
    success = kind == LAUNCH_SUCCESS or (kind == LLM_CALL and signal == LLM_OK)
    grace = _cfg(config, "recovery", "reset_grace_seconds", 60)
    degraded_pct = _cfg(config, "usage", "degraded_percent", 85)
    if row.state == EXHAUSTED:
        if row.until is not None and now >= row.until + grace:
            return "the usage window's reset time passed"
        if (
            account is not None
            and account.observed_at > row.since
            and account.used_percent < degraded_pct
        ):
            return f"{account.label()} window now at {_fmt_pct(account.used_percent)}"
        if success:
            return "a session made a successful call"
    elif row.state == FAILING:
        if row.until is not None and now >= row.until:
            return "the failure backoff passed"
        if success:
            return "a session made a successful call"
    elif row.state == UNAUTHENTICATED:
        if kind == AUTH_PROBE and signal == PROBE_AUTHENTICATED:
            return "the login probe reports signed in"
        if kind == LLM_CALL and signal == LLM_OK:
            return "the API accepted the credential"
    return None


def _probation_failure(row: ProviderAvailability, evidence: Evidence | None) -> bool:
    """A single signal that sends a recovering provider straight back (D4)."""
    if evidence is None:
        return False
    if evidence.kind in (STARTUP_DIALOG, EXIT_RATE_LIMIT):
        return True
    if evidence.kind == AUTH_PROBE and evidence.signal == PROBE_NOT_AUTHENTICATED:
        return row.probation_from == UNAUTHENTICATED
    if evidence.kind == LAUNCH_FAILURE:
        return row.probation_from == FAILING
    return evidence.kind == LLM_CALL and evidence.signal not in (LLM_OK, None)


def _return_from_probation(
    row: ProviderAvailability, level: int, now: float, config: Any, account: UsageReading | None
) -> _Derived:
    target = row.probation_from if row.probation_from in _DERIVED_UNAVAILABLE else FAILING
    reason = "the recovery canary failed; back to " + target
    if target == EXHAUSTED:
        return _Derived(target, "canary_failed", reason, _exhausted_until(now, level, config, account))
    if target == FAILING:
        return _Derived(target, "canary_failed", reason, _failing_until(now, level, config))
    return _Derived(target, "canary_failed", reason)


def _record(row: ProviderAvailability, evidence: Evidence, keep: int) -> ProviderAvailability:
    entry = evidence.as_entry()
    ring = (entry, *row.evidence)[: max(1, keep)]
    at = float(evidence.observed_at)
    updates: dict[str, Any] = {"evidence": ring}
    if evidence.kind == LAUNCH_SUCCESS or (
        evidence.kind == LLM_CALL and evidence.signal == LLM_OK
    ):
        updates["last_success_at"] = max(at, float(row.last_success_at or 0.0))
        updates["consecutive_failures"] = 0
    elif evidence.kind in (LAUNCH_FAILURE, STARTUP_DIALOG) or (
        evidence.kind == LLM_CALL and evidence.signal == LLM_ERROR
    ):
        updates["last_failure_at"] = max(at, float(row.last_failure_at or 0.0))
        if evidence.kind != STARTUP_DIALOG:
            updates["consecutive_failures"] = row.consecutive_failures + 1
    elif evidence.kind == AUTH_PROBE and evidence.signal != PROBE_CANNOT_TELL:
        updates["last_probe_at"] = at
    return replace(row, **updates)


def _last_effective(row: ProviderAvailability, now: float) -> str:
    """The effective state *row* was last seen in.

    An override that has expired but whose expiry has not been processed yet
    was still the effective state up to its ``override_until``; comparing
    against the derived state would swallow the expiry's own transition.
    Every :func:`reduce` clears an expired override, so a row still carrying
    one is exactly a row whose expiry is being processed now.
    """
    return row.override_state or row.state


def _finish(
    before: ProviderAvailability,
    after: ProviderAvailability,
    *,
    now: float,
    actor: str,
    evidence: Evidence | None,
) -> Reduction:
    """Bump ``generation`` on an effective change and build the transition."""
    from_state = _last_effective(before, now)
    to_state = after.effective_state(now)
    transition = None
    if from_state != to_state:
        after = replace(after, generation=before.generation + 1)
        detail: dict[str, Any] = {"derived_from": before.state, "derived_to": after.state}
        if evidence is not None:
            detail["evidence"] = evidence.kind
            if evidence.signal:
                detail["signal"] = evidence.signal
        transition = Transition(
            provider=after.provider,
            vendor=after.vendor,
            from_state=from_state,
            to_state=to_state,
            reason_code=after.effective_reason_code(now),
            reason=after.effective_reason(now),
            since=after.effective_since(now),
            until=after.effective_until(now),
            generation=after.generation,
            actor=actor,
            override=after.override_active(now) or bool(before.override_state),
            at=now,
            detail=detail,
        )
    changed = after.material() != before.material()
    if changed:
        after = replace(after, updated_at=now)
    return Reduction(row=after, transition=transition, changed=changed)


def _apply(row: ProviderAvailability, derived: _Derived, now: float) -> ProviderAvailability:
    if derived.state != row.state:
        return replace(
            row,
            state=derived.state,
            reason_code=derived.reason_code,
            reason=derived.reason,
            until=derived.until,
            since=now,
        )
    return replace(
        row, reason_code=derived.reason_code, reason=derived.reason, until=derived.until
    )


def reduce(
    row: ProviderAvailability,
    evidence: Evidence | None,
    *,
    now: float,
    config: Any,
    account: UsageReading | None = None,
    scoped: UsageReading | None = None,
    peer_success_projects: frozenset[str] = frozenset(),
    actor: str = "system",
) -> Reduction:
    """Fold *evidence* (or the clock alone, when ``None``) into *row*.

    *config* is a :class:`~src.config.ProviderFailoverConfig` (read by
    attribute, so a stub works).  *account*/*scoped* are the provider's
    fullest fresh account-wide and model-scoped usage windows
    (:func:`usage_view`).  *peer_success_projects* are the projects in which
    **another** provider recorded a ``launch_success`` inside
    ``launch.window_seconds`` — the attribution test for ``failing`` (D3).
    """
    before = row
    keep = int(_cfg(config, "evidence", "keep", 20))
    window = _cfg(config, "launch", "window_seconds", 600)
    flap_window = _cfg(config, "recovery", "flap_window_seconds", 3600)

    if evidence is not None:
        row = _record(row, evidence, keep)

    # Override expiry is evaluated here, on the tick (D6): the expired
    # override emits the same state-change event as any other transition.
    if row.override_state and row.override_until is not None and row.override_until <= now:
        row = replace(
            row,
            override_state=None,
            override_until=None,
            override_by=None,
            override_reason=None,
            override_set_at=None,
        )

    level = row.level
    if (
        level
        and row.last_trip_at is not None
        and now - row.last_trip_at >= flap_window
        and row.state in LAUNCHABLE
        and row.reason_code != RECOVERING
    ):
        level = 0
        row = replace(row, level=0)

    ring = _Ring(row, now, window)
    trip_kwargs = {
        "now": now,
        "config": config,
        "account": account,
        "peer_success_projects": peer_success_projects,
    }

    if row.state in _DERIVED_UNAVAILABLE:
        tripped = _trip(row, ring, level=level, **trip_kwargs)
        if tripped is not None and _PRECEDENCE[tripped.state] < _PRECEDENCE[row.state]:
            row = replace(_apply(row, tripped, now), last_trip_at=now)
        elif (why := _recovered(row, evidence, now=now, config=config, account=account)):
            # Probation is a fresh start: the evidence that tripped the
            # provider must not re-trip it on the next tick.  One failure
            # signal on probation sends it back anyway (``_probation_failure``).
            row = replace(
                row,
                state=DEGRADED,
                reason_code=RECOVERING,
                reason=f"on probation: {why}; the next successful launch completes recovery",
                until=None,
                since=now,
                probation_from=row.state,
                counters_reset_at=now,
            )
        elif (
            # A structured reset clock beats a guessed backoff.
            tripped is not None
            and tripped.state == row.state == EXHAUSTED
            and account is not None
            and account.resets_at is not None
            and account.resets_at > now
            and account.used_percent >= _cfg(config, "usage", "exhausted_percent", 99)
        ):
            row = replace(row, until=account.resets_at)
        return _finish(before, row, now=now, actor=actor, evidence=evidence)

    if row.state == DEGRADED and row.reason_code == RECOVERING:
        if _probation_failure(row, evidence):
            level = min(level + 1, _MAX_LEVEL)
            back = _return_from_probation(row, level, now, config, account)
            row = replace(
                _apply(row, back, now), level=level, last_trip_at=now, probation_from=None
            )
            return _finish(before, row, now=now, actor=actor, evidence=evidence)
        success = evidence is not None and (
            evidence.kind == LAUNCH_SUCCESS
            or (evidence.kind == LLM_CALL and evidence.signal == LLM_OK)
        )
        tripped = _trip(row, ring, level=level + (1 if evidence else 0), **trip_kwargs)
        if tripped is not None:
            if evidence is not None:
                level = min(level + 1, _MAX_LEVEL)
            row = replace(
                _apply(row, tripped, now), level=level, last_trip_at=now, probation_from=None
            )
            return _finish(before, row, now=now, actor=actor, evidence=evidence)
        if not success:
            return _finish(before, row, now=now, actor=actor, evidence=evidence)
        # The canary made it: recovery complete.
        row = replace(
            row,
            probation_from=None,
            state=AVAILABLE,
            reason_code="",
            reason="",
            until=None,
            since=now,
        )
        derived = _launchable(
            row, _Ring(row, now, window), config=config, account=account, scoped=scoped
        )
        return _finish(before, _apply(row, derived, now), now=now, actor=actor, evidence=evidence)

    tripped = _trip(row, ring, level=level, **trip_kwargs)
    if tripped is not None:
        row = replace(_apply(row, tripped, now), last_trip_at=now, probation_from=None)
    else:
        row = _apply(
            row, _launchable(row, ring, config=config, account=account, scoped=scoped), now
        )
    return _finish(before, row, now=now, actor=actor, evidence=evidence)


def set_override(
    row: ProviderAvailability,
    state: str,
    *,
    until: float | None,
    by: str,
    reason: str,
    now: float,
) -> Reduction:
    """Force *state* (``disabled`` or ``available``) until *until* (D6)."""
    if state not in OVERRIDE_STATES:
        raise ValueError(f"override state must be one of {', '.join(OVERRIDE_STATES)}")
    if state == AVAILABLE and until is None:
        raise ValueError("an 'available' override always expires")
    after = replace(
        row,
        override_state=state,
        override_until=until,
        override_by=by,
        override_reason=reason,
        override_set_at=now,
    )
    return _finish(row, after, now=now, actor=by, evidence=None)


def clear_override(
    row: ProviderAvailability,
    *,
    by: str,
    now: float,
    config: Any,
    account: UsageReading | None = None,
    scoped: UsageReading | None = None,
) -> Reduction:
    """``auto``: clear the override, reset the counters and ``level``, re-derive.

    Evidence already in the ring stops counting toward a trip.  A provider
    whose derived state was unavailable goes to probation rather than
    straight to ``available``: this is the "I just ran ``codex login``"
    button, and the canary is what proves the login stuck.
    """
    cleared = replace(
        row,
        override_state=None,
        override_until=None,
        override_by=None,
        override_reason=None,
        override_set_at=None,
        consecutive_failures=0,
        level=0,
        counters_reset_at=now,
    )
    if cleared.state in _DERIVED_UNAVAILABLE:
        cleared = replace(
            cleared,
            state=DEGRADED,
            reason_code=RECOVERING,
            reason=f"on probation: reset by {by}; the next successful launch completes recovery",
            until=None,
            since=now,
            probation_from=cleared.state,
        )
    rederived = reduce(
        cleared, None, now=now, config=config, account=account, scoped=scoped, actor=by
    ).row
    return _finish(row, replace(rederived, generation=row.generation), now=now, actor=by, evidence=None)
