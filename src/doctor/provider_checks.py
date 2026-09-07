"""``providers.*`` doctor checks — is the quota feed still telling the truth?

The provider-usage feature (``docs/superpowers/specs/2026-09-07-provider-usage-design.md``)
puts each provider's own account of its limit windows on the Metrics tab.
Codex's half needs no supervision: its numbers ride in on transcript lines
the watcher already reads, and a Codex reading that stops moving means an
idle fleet, which T6 renders honestly as ``stale``.

Claude's half is the fragile one.  It depends on a ten-minute playbook timer
calling ``provider_usage_probe``, which shells out to ``claude -p "/usage"``
and reads percentages out of *English prose*.  Two things can quietly rot:
the timer can stop firing, and the CLI's wording can move under the regex in
:mod:`src.providers.claude_usage`.  Both leave the dashboard showing a number
that is merely old rather than wrong, which is precisely the failure nobody
notices.  This check is what notices.

Why probe *health*, not the newest snapshot
-------------------------------------------

The obvious implementation — "warn when the newest ``source='probe'`` row is
older than the horizon" — is wrong, and wrong in the direction that produces
false alarms.  ``record_provider_usage`` deliberately drops a reading equal
to the newest row already stored for its series, so an account sitting at 45%
for six hours writes *one* row and then nothing.  A perfectly healthy probe
firing every ten minutes would look six hours stale.

So freshness is read from the verdict the probe records on **every** run,
success or failure (``record_probe_health`` / ``read_probe_health``), and the
snapshots are used only to fill in the percentages an OK line reports.

Report-only, by design
----------------------

There is no ``fix``.  Nothing here is safely repairable by a machine: a
stalled timer means activating or unbreaking a playbook, and an unparsed body
means a human reading the CLI's new wording and editing a regex.  A ``--fix``
that "helpfully" ran a probe would paper over exactly the stalled timer the
check exists to surface.
"""

from __future__ import annotations

import time
from typing import Any

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "provider-usage"

CHECK_ID = "providers.claude_usage"

#: Fallback horizon when the config object predates ``stale_after_seconds``.
#: Matches :class:`~src.config.ClaudeProviderConfig`'s default so a test
#: passing a bare stub sees production's number.
DEFAULT_STALE_AFTER_SECONDS = 1500

#: Probe outcomes that are facts about the box rather than faults: no CLI
#: installed, or an API-key account that has no subscription window to
#: report.  ``INFO`` says so without ever failing CI.
_BENIGN_OUTCOMES = {"unavailable", "not_applicable"}


def _claude_config(ctx: DoctorContext) -> Any:
    providers = getattr(ctx.config, "providers", None)
    return getattr(providers, "claude", None)


def _stale_after(claude: Any) -> float:
    raw = getattr(claude, "stale_after_seconds", None)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_STALE_AFTER_SECONDS)
    return seconds if seconds > 0 else float(DEFAULT_STALE_AFTER_SECONDS)


def _age(seconds: float) -> str:
    """A duration a human reads at a glance, not a float of seconds."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _series_label(row: dict) -> str:
    scope = str(row.get("scope") or "")
    window = str(row.get("window") or "?")
    return f"{window} ({scope})" if scope else window


def _percentages(rows: list[dict]) -> str:
    """``session 5%, week (all models) 45%, week (Fable) 81%``.

    Ordered by series name rather than by ``observed_at`` so the OK line
    reads the same from one run to the next; an operator comparing two
    ``aq doctor`` outputs should be diffing numbers, not row order.
    """
    parts = [
        f"{_series_label(row)} {float(row['used_percent']):g}%"
        for row in sorted(rows, key=_series_label)
    ]
    return ", ".join(parts)


async def _latest_claude_rows(ctx: DoctorContext) -> list[dict]:
    """Newest snapshot per Claude series, or ``[]`` if the read fails.

    A check must not turn a reporting problem into a traceback: the health
    verdict is the part that decides the severity, and the percentages are
    decoration on the detail line.
    """
    try:
        rows = await ctx.db.latest_provider_usage("claude")
    except Exception:  # noqa: BLE001 - pragma: no cover; decoration must not break the verdict
        return []
    return [dict(row) for row in rows or []]


async def _check_claude_usage(ctx: DoctorContext) -> CheckResult:
    """Report on the health of the Claude ``/usage`` probe.

    Severity ladder, most specific first:

    * ``INFO`` — no database, or the probe is disabled, or the box has no
      Claude CLI / an API-key account with no window to report.
    * ``WARN`` — no probe has ever run; the last probe's body did not parse;
      the last probe failed outright; the last probe is older than
      ``providers.claude.stale_after_seconds``.
    * ``OK`` — a recent probe that parsed, with the current percentages on
      the detail line.
    """
    if ctx.db is None:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="database not initialised — probe health unknown",
        )

    claude = _claude_config(ctx)
    if claude is not None and not getattr(claude, "usage_probe_enabled", True):
        # Disabled is a decision an operator made, not a fault.  Reporting it
        # as OK rather than WARN is the difference between a check that stays
        # useful and one everybody learns to ignore.
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.OK,
            detail="providers.claude.usage_probe_enabled is false — probe disabled",
            data={"enabled": False},
        )

    try:
        health = await ctx.db.read_probe_health("claude")
    except Exception:  # noqa: BLE001 - pragma: no cover; an unreadable verdict is "no probe"
        health = None

    if not health:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            detail=(
                "no claude /usage probe has run — activate the "
                "provider-usage-probe playbook, or run "
                "`aq run provider_usage_probe` once to confirm the CLI answers"
            ),
            data={"enabled": True, "probe_ran": False},
        )

    now = time.time()
    ts = float(health.get("ts") or 0.0)
    age = max(0.0, now - ts) if ts else None
    horizon = _stale_after(claude)
    outcome = str(health.get("outcome") or "unknown")
    data: dict[str, Any] = {
        "enabled": True,
        "probe_ran": True,
        "outcome": outcome,
        "ok": bool(health.get("ok")),
        "unparsed": bool(health.get("unparsed")),
        "age_seconds": None if age is None else round(age, 3),
        "stale_after_seconds": horizon,
    }

    # Wording first, staleness second.  A probe that has been returning
    # unparsed bodies for a day is both, and "the regex needs a human" is the
    # sentence that gets it fixed; the age rides along in the same line so
    # nothing is lost by ordering them this way.
    if health.get("unparsed"):
        suffix = f" (last probe {_age(age)} ago)" if age is not None else ""
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            detail=(
                "the last claude /usage probe returned nothing our parser "
                "recognised — the CLI's wording moved; fix the limit-line "
                f"regex in src/providers/claude_usage.py{suffix}"
            ),
            data=data,
        )

    if not health.get("ok"):
        error = str(health.get("error") or outcome)
        suffix = f" ({_age(age)} ago)" if age is not None else ""
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            detail=f"the last claude /usage probe failed{suffix}: {error}",
            data=data,
        )

    if outcome in _BENIGN_OUTCOMES:
        detail = str(health.get("detail") or "").strip()
        explain = {
            "unavailable": "the claude CLI is not installed on this box",
            "not_applicable": "this account has no subscription window to report",
        }[outcome]
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail=f"{explain} — no Claude usage to show{f' ({detail})' if detail else ''}",
            data=data,
        )

    if age is None or age > horizon:
        seen = "never" if age is None else f"{_age(age)} ago"
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            detail=(
                f"the claude /usage probe last ran {seen}, past the "
                f"{_age(horizon)} horizon — the provider-usage-probe timer is "
                "not firing and the dashboard's numbers are frozen"
            ),
            data=data,
        )

    rows = await _latest_claude_rows(ctx)
    data["series"] = len(rows)
    percentages = _percentages(rows)
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.OK,
        detail=(
            f"claude /usage probed {_age(age)} ago: {percentages}"
            if percentages
            else f"claude /usage probed {_age(age)} ago; no snapshots stored yet"
        ),
        data=data,
    )


def provider_checks() -> list[DoctorCheck]:
    # Report-only: no ``fix``.  See the module docstring — a stalled timer and
    # a moved CLI wording both need a human, and an automatic probe would hide
    # the former.
    return [DoctorCheck(id=CHECK_ID, run=_check_claude_usage, owner=OWNER)]


#: Snapshot for call-sites (tests, ad-hoc scripts) that want the list without
#: building a full :class:`~src.doctor.runner.DoctorRegistry`.
CHECKS = provider_checks()

_BY_ID = {c.id: c for c in CHECKS}


async def run_check(db, check_id: str, *, config=None) -> CheckResult:
    """Run one provider check directly against *db* (no registry needed).

    There is no ``repair`` parameter on purpose: none of these checks has a
    ``fix``, and offering the argument would imply one exists.
    """
    check = _BY_ID[check_id]
    return await check.run(DoctorContext(config=config, db=db))


__all__ = ["CHECKS", "CHECK_ID", "OWNER", "provider_checks", "run_check"]
