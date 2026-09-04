"""The rollback observation window, evaluated from evidence (Package 7 §3.5).

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md``
§3.5 fixes sixteen acceptance measures and three window conditions — wall
clock, coverage, volume — that ``playbook_cutover_window_close`` must see
satisfied before the V1 runtime may be deleted.  This module is the pure half
of that gate: it takes a :class:`WindowEvidence` the command collected from
the database and returns the measured table, the window, and every reason
the window cannot close.  Nothing here does I/O, which is what lets T-12
plant one failing source per measure and prove the close names it.

Two rules shape every evaluator:

* **Fail closed.**  A source that could not be read (``errors``) fails every
  measure it feeds with ``evidence unreadable``.  A denominator of zero is
  reported as *not measured*, never as a zero failure rate — an idle fleet
  proves nothing about budget paths it never exercised.
* **Say what was observed, and when.**  Every row carries ``observed`` and
  ``observed_at`` so the close event records the evidence it was closed on,
  not just a verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.playbooks.cutover import _p95 as p95
from src.playbooks.migration import validate_parity_evidence

#: Wall-clock floor for the observation window (§3.5).
WINDOW_MIN_SECONDS: float = 72 * 3600.0

#: Volume floor, rehearsal runs included: an idle fleet reaches 72 h having
#: proved nothing.
WINDOW_MIN_V2_RUNS: int = 200

#: Measure 5 — at most one snapshot-version conflict per this many boundaries.
CONFLICTS_PER_BOUNDARIES: int = 10_000

#: Measure 6 — the V2 dispatch p95 may exceed the V1 baseline by this factor,
#: and never this absolute ceiling.
DISPATCH_BASELINE_MULTIPLIER: float = 1.25
DISPATCH_GATE_MS: float = 1000.0

#: Measure 7.
RESUME_GATE_MS: float = 5000.0

#: Measures 8 and 9 — LLM failure share of LLM steps.
LLM_FAILURE_GATE: float = 0.01

#: Measure 12.
GRAPH_LATENCY_GATE_MS: int = 300

#: Measure 13 — the G2 threshold; the window-close gate records the review
#: but does not re-threshold it (§3.5 "—").
DASHBOARD_TTI_GATE_MS: float = 1500.0

#: Measures 14 and 15.
PENDING_COUNT_GATE: int = 5
PENDING_AGE_GATE_SECONDS: float = 24 * 3600.0

#: The operator-visible pending-event reasons; ``wait_registration`` rows are
#: the wait inbox, not a backlog.
PENDING_EVENT_REASONS: tuple[str, ...] = (
    "stale_contract",
    "invalid_artifact",
    "disabled",
    "unavailable",
    "question_required",
)

MEASURE_NAMES: dict[int, str] = {
    1: "shadow rule-selection agreement",
    2: "command-argument agreement after canonicalisation",
    3: "unexplained terminal-outcome differences",
    4: "authorization denials by command and profile",
    5: "duplicate receipt / snapshot-version conflicts",
    6: "event->run dispatch latency p95",
    7: "wait-resume latency p95",
    8: "LLM budget failures",
    9: "structured-output failures",
    10: "agent-task orphan rate",
    11: "agent-task cancellation rate",
    12: "graph API latency p95",
    13: "dashboard semantic-tab time-to-interactive",
    14: "pending-event count",
    15: "pending-event maximum age",
    16: "active V1 runs",
}

#: Where each row's evidence comes from, as the operator reads it.
MEASURE_SOURCES: dict[int, str] = {
    1: "tests/fixtures/playbooks/v2/parity-report.json, field rules_selected, bound to the "
       "active artifact",
    2: "parity-report.json, field commands",
    3: "parity-report.json, field terminal",
    4: "events table, capability.denied rows since the switch (shadow=false)",
    5: "events table, playbook.snapshot_conflict rows since the switch, over "
       "playbook_step_receipts boundaries",
    6: "playbook_v2_runs.started_at - event._received_at, since the switch; baseline from "
       "the switched_to_v2 event",
    7: "playbook_waits.claimed_at - playbook_pending_events.received_at of the causing event",
    8: "playbook_step_receipts, step_kind=llm, error_code=budget_exceeded",
    9: "playbook_step_receipts, step_kind=llm, error_code=invalid_output",
    10: "playbook_waits, kind=agent_task, active past 2x (deadline_at - created_at)",
    11: "playbook_step_receipts, step_kind=agent_task, outcome=cancelled, by run",
    12: "live playbook_v2_graph probes against the largest enabled artifact",
    13: "window_coverage_rehearsal event, dashboard_tti_ms (manual scenario review)",
    14: "playbook_pending_events, unresolved, operator-visible reasons",
    15: "playbook_pending_events, oldest unresolved received_at",
    16: "DrainStatus.active",
}

#: The evidence sources each measure reads; an unreadable one fails the row.
_MEASURE_SOURCE_KEYS: dict[int, tuple[str, ...]] = {
    1: ("parity", "activations"),
    2: ("parity", "activations"),
    3: ("parity", "activations"),
    4: ("denials",),
    5: ("conflicts", "receipts"),
    6: ("dispatch_latency",),
    7: ("resume_latency",),
    8: ("receipts",),
    9: ("receipts",),
    10: ("waits", "receipts"),
    11: ("cancellations", "receipts"),
    12: ("graph", "activations"),
    13: ("rehearsal",),
    14: ("pending",),
    15: ("pending",),
    16: ("v1_runs",),
}

_PARITY_FIELD_BY_MEASURE: dict[int, str] = {1: "rules_selected", 2: "commands", 3: "terminal"}


@dataclass(frozen=True, slots=True)
class WindowEvidence:
    """Everything the window is judged on, as read from source by the command.

    A ``None`` in a source field means it could not be read, and ``errors``
    names why under the source's key (see ``_MEASURE_SOURCE_KEYS``).
    ``dashboard_tti`` and ``v1_baseline`` are different: ``None`` there means
    *not recorded*, which is a failing observation rather than a failed read.
    """

    now: float
    switched_at: float | None
    parity: Mapping[str, Any] | None
    enabled_activations: Sequence[Mapping[str, Any]] | None
    v2_runs_by_playbook: Mapping[str, int] | None
    denials: Sequence[Mapping[str, Any]] | None
    conflicts: Sequence[Mapping[str, Any]] | None
    dispatch_latencies_ms: Sequence[float] | None
    resume_latencies_ms: Sequence[float] | None
    v1_baseline: Mapping[str, Any] | None
    step_counts: Sequence[Mapping[str, Any]] | None
    agent_task_orphans: Sequence[Mapping[str, Any]] | None
    agent_task_cancellations: Sequence[Mapping[str, Any]] | None
    graph_latencies_ms: Sequence[float] | None
    graph_target: str | None
    dashboard_tti: Mapping[str, Any] | None
    pending: Mapping[str, Any] | None
    active_v1_runs: int | None
    rehearsal: Mapping[str, Any] | None
    errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WindowVerdict:
    measures: list[dict[str, Any]]
    window: dict[str, Any]
    blocking_reasons: list[str]
    evidence_errors: list[str]

    @property
    def can_close(self) -> bool:
        return not self.blocking_reasons


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _row(
    number: int,
    observed: Any,
    gate: str,
    passed: bool,
    *,
    now: float,
    blocking: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "measure": number,
        "name": MEASURE_NAMES[number],
        "source": MEASURE_SOURCES[number],
        "observed": observed,
        "gate": gate,
        "pass": passed,
        "observed_at": now,
    }
    if not passed:
        row["blocking"] = blocking or "failed its gate"
    return row


def _unreadable(number: int, evidence: WindowEvidence, gate: str) -> dict[str, Any] | None:
    """The fail-closed row for a measure whose source could not be read."""
    broken = [key for key in _MEASURE_SOURCE_KEYS[number] if key in evidence.errors]
    if not broken:
        return None
    detail = "; ".join(f"{key}: {evidence.errors[key]}" for key in broken)
    return _row(
        number, None, gate, False, now=evidence.now, blocking=f"evidence unreadable — {detail}"
    )


def _parity_rows(evidence: WindowEvidence) -> list[dict[str, Any]]:
    gate = "0 unexplained findings; report bound to the active artifact"
    rows: list[dict[str, Any]] = []
    for number in (1, 2, 3):
        broken = _unreadable(number, evidence, gate)
        if broken is not None:
            rows.append(broken)
    if rows:
        return rows

    parity = evidence.parity or {}
    activations = [
        {
            "playbook_id": row.get("playbook_id"),
            "artifact_sha256": row.get("active_artifact_sha256") or row.get("artifact_sha256"),
            "scope": row.get("scope"),
            "scope_identifier": row.get("scope_identifier"),
        }
        for row in (evidence.enabled_activations or ())
    ]
    problems = validate_parity_evidence({**parity, "recorded": True}, artifacts=activations)

    unexplained: dict[str, int] = {}
    for event in parity.get("events") or ():
        for finding in event.get("findings") or () if isinstance(event, Mapping) else ():
            if isinstance(finding, Mapping) and finding.get("classification") == "unexplained":
                key = str(finding.get("field"))
                unexplained[key] = unexplained.get(key, 0) + 1

    for number, parity_field in _PARITY_FIELD_BY_MEASURE.items():
        count = unexplained.get(parity_field, 0)
        observed = {
            "unexplained": count,
            "observations": parity.get("observations"),
            "artifact_sha256": parity.get("artifact_sha256"),
        }
        if problems:
            rows.append(
                _row(number, observed, gate, False, now=evidence.now, blocking="; ".join(problems))
            )
        elif count:
            rows.append(
                _row(
                    number, observed, gate, False, now=evidence.now,
                    blocking=f"{count} unexplained {parity_field} finding(s); gate 0",
                )
            )
        else:
            rows.append(_row(number, observed, gate, True, now=evidence.now))
    return rows


def _denials_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = "0 over the window (shadow denials reported, not gated)"
    broken = _unreadable(4, evidence, gate)
    if broken is not None:
        return broken
    by_command: dict[str, dict[str, int]] = {}
    shadow = 0
    total = 0
    for denial in evidence.denials or ():
        if denial.get("shadow"):
            shadow += 1
            continue
        total += 1
        command = str(denial.get("command") or "?")
        profile = str(denial.get("profile_id") or "-")
        by_command.setdefault(command, {})
        by_command[command][profile] = by_command[command].get(profile, 0) + 1
    observed = {"total": total, "shadow": shadow, "by_command": by_command}
    if total:
        grouped = ", ".join(
            f"{command}/{profile}: {count}"
            for command, profiles in sorted(by_command.items())
            for profile, count in sorted(profiles.items())
        )
        return _row(
            4, observed, gate, False, now=evidence.now,
            blocking=f"authorization denials: {total} ({grouped}); gate 0 over the window",
        )
    return _row(4, observed, gate, True, now=evidence.now)


def _count_steps(
    evidence: WindowEvidence,
    *,
    step_kind: str | None = None,
    receipt_kind: str | None = "step",
    outcome: str | None = None,
    error_code: str | None = None,
) -> int:
    total = 0
    for row in evidence.step_counts or ():
        if step_kind is not None and row.get("step_kind") != step_kind:
            continue
        if receipt_kind is not None and row.get("receipt_kind") != receipt_kind:
            continue
        if outcome is not None and row.get("outcome") != outcome:
            continue
        if error_code is not None and row.get("error_code") != error_code:
            continue
        total += int(row.get("count") or 0)
    return total


def _conflicts_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = (
        f"<= 1 per {CONFLICTS_PER_BOUNDARIES:,} boundaries; each conflict is recorded in the "
        "close event for the operator's written reason to explain"
    )
    broken = _unreadable(5, evidence, gate)
    if broken is not None:
        return broken
    conflicts = list(evidence.conflicts or ())
    boundaries = _count_steps(evidence, receipt_kind=None)
    observed = {
        "conflicts": len(conflicts),
        "boundaries": boundaries,
        "runs": sorted({str(c.get("run_id")) for c in conflicts if c.get("run_id")}),
    }
    if len(conflicts) * CONFLICTS_PER_BOUNDARIES > boundaries:
        return _row(
            5, observed, gate, False, now=evidence.now,
            blocking=(
                f"snapshot-version conflicts: {len(conflicts)} in {boundaries} boundaries "
                f"(gate <= 1 per {CONFLICTS_PER_BOUNDARIES:,})"
            ),
        )
    return _row(5, observed, gate, True, now=evidence.now)


def _dispatch_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = (
        f"<= {DISPATCH_BASELINE_MULTIPLIER} x v1_baseline.dispatch_p95 and "
        f"<= {DISPATCH_GATE_MS:.0f} ms"
    )
    broken = _unreadable(6, evidence, gate)
    if broken is not None:
        return broken
    samples = sorted(float(v) for v in evidence.dispatch_latencies_ms or ())
    baseline_s = (evidence.v1_baseline or {}).get("dispatch_p95")
    baseline_ms = float(baseline_s) * 1000.0 if baseline_s is not None else None
    observed_p95 = p95(samples)
    observed = {"p95_ms": observed_p95, "samples": len(samples), "baseline_p95_ms": baseline_ms}
    if baseline_ms is None:
        return _row(
            6, observed, gate, False, now=evidence.now,
            blocking="no v1 baseline recorded (no switched_to_v2 event carries v1_baseline)",
        )
    if observed_p95 is None:
        return _row(
            6, observed, gate, False, now=evidence.now,
            blocking="no dispatch latency samples in the window (no v2 run carries "
                     "event._received_at)",
        )
    ceiling = min(DISPATCH_BASELINE_MULTIPLIER * baseline_ms, DISPATCH_GATE_MS)
    if observed_p95 > ceiling:
        return _row(
            6, observed, gate, False, now=evidence.now,
            blocking=(
                f"dispatch latency p95 {observed_p95:.0f}ms exceeds gate "
                f"(<= {DISPATCH_BASELINE_MULTIPLIER} x baseline {baseline_ms:.0f}ms, "
                f"<= {DISPATCH_GATE_MS:.0f}ms)"
            ),
        )
    return _row(6, observed, gate, True, now=evidence.now)


def _resume_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = f"<= {RESUME_GATE_MS:.0f} ms from the causing event"
    broken = _unreadable(7, evidence, gate)
    if broken is not None:
        return broken
    samples = sorted(float(v) for v in evidence.resume_latencies_ms or ())
    observed_p95 = p95(samples)
    observed = {"p95_ms": observed_p95, "samples": len(samples)}
    if observed_p95 is None:
        return _row(
            7, observed, gate, False, now=evidence.now,
            blocking="no wait-resume samples in the window (no claimed event wait)",
        )
    if observed_p95 > RESUME_GATE_MS:
        return _row(
            7, observed, gate, False, now=evidence.now,
            blocking=(
                f"wait-resume latency p95 {observed_p95:.0f}ms exceeds gate "
                f"(<= {RESUME_GATE_MS:.0f}ms)"
            ),
        )
    return _row(7, observed, gate, True, now=evidence.now)


def _llm_failure_row(evidence: WindowEvidence, number: int, error_code: str, label: str) -> dict:
    gate = f"<= {LLM_FAILURE_GATE:.0%} of LLM steps"
    broken = _unreadable(number, evidence, gate)
    if broken is not None:
        return broken
    steps = _count_steps(evidence, step_kind="llm")
    failures = _count_steps(evidence, step_kind="llm", error_code=error_code)
    rate = (failures / steps) if steps else None
    observed = {"failures": failures, "llm_steps": steps, "rate": rate}
    if not steps:
        return _row(
            number, observed, gate, False, now=evidence.now,
            blocking="no LLM steps observed in the window; a rate over nothing is not a measure",
        )
    if rate > LLM_FAILURE_GATE:
        return _row(
            number, observed, gate, False, now=evidence.now,
            blocking=(
                f"{label}: {failures} of {steps} LLM steps ({rate:.1%}; "
                f"gate <= {LLM_FAILURE_GATE:.0%})"
            ),
        )
    return _row(number, observed, gate, True, now=evidence.now)


def _orphan_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = "0 agent-task steps with no terminal receipt after 2 x the step timeout"
    broken = _unreadable(10, evidence, gate)
    if broken is not None:
        return broken
    orphans = list(evidence.agent_task_orphans or ())
    steps = _count_steps(evidence, step_kind="agent_task", receipt_kind=None)
    observed = {
        "orphans": len(orphans),
        "agent_task_steps": steps,
        "rate": (len(orphans) / steps) if steps else None,
        "runs": sorted({str(o.get("run_id")) for o in orphans if o.get("run_id")}),
    }
    if not steps and not orphans:
        return _row(
            10, observed, gate, False, now=evidence.now,
            blocking="no agent-task steps observed in the window",
        )
    if orphans:
        return _row(
            10, observed, gate, False, now=evidence.now,
            blocking=(
                f"agent-task orphans: {len(orphans)} step(s) with no terminal receipt after "
                "2x the step timeout (gate 0)"
            ),
        )
    return _row(10, observed, gate, True, now=evidence.now)


def _cancellation_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = "reported, no gate"
    broken = _unreadable(11, evidence, gate)
    if broken is not None:
        return broken
    steps = _count_steps(evidence, step_kind="agent_task")
    cancellations = list(evidence.agent_task_cancellations or ())
    observed = {
        "cancelled": len(cancellations),
        "agent_task_steps": steps,
        "rate": (len(cancellations) / steps) if steps else None,
        "runs": sorted({str(c.get("run_id")) for c in cancellations if c.get("run_id")}),
    }
    return _row(11, observed, gate, True, now=evidence.now)


def _graph_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = f"<= {GRAPH_LATENCY_GATE_MS} ms p95 against the largest enabled artifact"
    broken = _unreadable(12, evidence, gate)
    if broken is not None:
        return broken
    samples = sorted(float(v) for v in evidence.graph_latencies_ms or ())
    observed_p95 = p95(samples)
    observed = {"target": evidence.graph_target, "samples": len(samples), "p95_ms": observed_p95}
    if evidence.graph_target is None:
        return _row(
            12, observed, gate, False, now=evidence.now,
            blocking="no enabled artifact to probe",
        )
    if observed_p95 is None:
        return _row(
            12, observed, gate, False, now=evidence.now,
            blocking=f"no graph latency samples against {evidence.graph_target}",
        )
    if observed_p95 > GRAPH_LATENCY_GATE_MS:
        return _row(
            12, observed, gate, False, now=evidence.now,
            blocking=(
                f"graph API latency p95 {observed_p95:.0f}ms exceeds gate "
                f"(<= {GRAPH_LATENCY_GATE_MS}ms) against {evidence.graph_target}"
            ),
        )
    return _row(12, observed, gate, True, now=evidence.now)


def _dashboard_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = (
        f"<= {DASHBOARD_TTI_GATE_MS:.0f} ms at the switch (G2); recorded, not "
        "re-thresholded, at window close"
    )
    broken = _unreadable(13, evidence, gate)
    if broken is not None:
        return broken
    review = evidence.dashboard_tti
    if review is None or review.get("ms") is None:
        return _row(
            13, None, gate, False, now=evidence.now,
            blocking=(
                "dashboard semantic-tab time-to-interactive not recorded; pass "
                "dashboard_tti_ms to playbook_cutover_window_rehearsal"
            ),
        )
    return _row(13, dict(review), gate, True, now=evidence.now)


def _pending_rows(evidence: WindowEvidence) -> list[dict[str, Any]]:
    count_gate = f"<= {PENDING_COUNT_GATE}"
    age_gate = f"< {PENDING_AGE_GATE_SECONDS:.0f} s"
    rows = []
    broken = _unreadable(14, evidence, count_gate)
    if broken is not None:
        rows.append(broken)
        rows.append(_unreadable(15, evidence, age_gate))
        return rows
    pending = evidence.pending or {}
    count = int(pending.get("count") or 0)
    oldest = pending.get("oldest_received_at")
    max_age = max(0.0, evidence.now - float(oldest)) if oldest is not None else 0.0
    count_observed = {"count": count}
    if count > PENDING_COUNT_GATE:
        rows.append(
            _row(
                14, count_observed, count_gate, False, now=evidence.now,
                blocking=f"pending events: {count} (gate <= {PENDING_COUNT_GATE})",
            )
        )
    else:
        rows.append(_row(14, count_observed, count_gate, True, now=evidence.now))
    age_observed = {"max_age_seconds": max_age, "oldest_received_at": oldest}
    if max_age >= PENDING_AGE_GATE_SECONDS:
        rows.append(
            _row(
                15, age_observed, age_gate, False, now=evidence.now,
                blocking=(
                    f"oldest pending event is {max_age:.0f}s old "
                    f"(gate < {PENDING_AGE_GATE_SECONDS:.0f}s)"
                ),
            )
        )
    else:
        rows.append(_row(15, age_observed, age_gate, True, now=evidence.now))
    return rows


def _v1_runs_row(evidence: WindowEvidence) -> dict[str, Any]:
    gate = "0 — hard"
    broken = _unreadable(16, evidence, gate)
    if broken is not None:
        return broken
    active = int(evidence.active_v1_runs or 0)
    if active:
        return _row(
            16, active, gate, False, now=evidence.now,
            blocking=f"{active} active v1 run(s) (gate 0)",
        )
    return _row(16, active, gate, True, now=evidence.now)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def _window(evidence: WindowEvidence) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    switched_at = evidence.switched_at
    elapsed = (evidence.now - switched_at) if switched_at is not None else None
    wall_clock_ok = bool(elapsed is not None and elapsed >= WINDOW_MIN_SECONDS)

    runs = dict(evidence.v2_runs_by_playbook or {})
    run_count = sum(int(v) for v in runs.values())
    enabled = sorted(
        {
            str(row.get("playbook_id"))
            for row in (evidence.enabled_activations or ())
            if row.get("playbook_id")
        }
    )
    readable = "v2_runs" not in evidence.errors and "activations" not in evidence.errors
    missing = [pid for pid in enabled if not runs.get(pid)] if readable else enabled
    coverage_ok = readable and not missing
    volume_ok = readable and run_count >= WINDOW_MIN_V2_RUNS

    if switched_at is None:
        reasons.append("no switched_to_v2 event — the window has not started")
    elif not wall_clock_ok:
        reasons.append(
            f"observation window is {elapsed:.0f}s old; {WINDOW_MIN_SECONDS:.0f}s required"
        )
    if not readable:
        detail = "; ".join(
            f"{key}: {evidence.errors[key]}"
            for key in ("v2_runs", "activations")
            if key in evidence.errors
        )
        reasons.append(f"coverage and volume: evidence unreadable — {detail}")
    else:
        for playbook_id in missing:
            reasons.append(
                f"coverage: playbook '{playbook_id}' has dispatched "
                f"{runs.get(playbook_id, 0)} v2 runs since the switch"
            )
        if not volume_ok:
            reasons.append(f"v2 run volume is {run_count}; {WINDOW_MIN_V2_RUNS} required")

    rehearsal = evidence.rehearsal or {}
    window = {
        "switched_at": switched_at,
        "since": switched_at,
        "until": evidence.now,
        "observed_at": evidence.now,
        "elapsed_seconds": elapsed,
        "wall_clock_ok": wall_clock_ok,
        "wall_clock_gate_seconds": WINDOW_MIN_SECONDS,
        "coverage_ok": coverage_ok,
        "coverage_missing": missing,
        "enabled_playbooks": enabled,
        "volume_ok": volume_ok,
        "volume_gate_runs": WINDOW_MIN_V2_RUNS,
        "v2_run_count": run_count,
        "v2_runs_by_playbook": runs,
        "rehearsal_at": rehearsal.get("at"),
        "closed_at": None,
    }
    return window, reasons


def evaluate_window(evidence: WindowEvidence) -> WindowVerdict:
    """The §3.5 table, the window, and every reason the window cannot close.

    Blocking reasons are ordered window conditions first, then measures in
    number order, so an operator reads the cheapest fix first.
    """
    measures: list[dict[str, Any]] = []
    measures.extend(_parity_rows(evidence))
    measures.append(_denials_row(evidence))
    measures.append(_conflicts_row(evidence))
    measures.append(_dispatch_row(evidence))
    measures.append(_resume_row(evidence))
    measures.append(_llm_failure_row(evidence, 8, "budget_exceeded", "LLM budget failures"))
    measures.append(_llm_failure_row(evidence, 9, "invalid_output", "structured-output failures"))
    measures.append(_orphan_row(evidence))
    measures.append(_cancellation_row(evidence))
    measures.append(_graph_row(evidence))
    measures.append(_dashboard_row(evidence))
    measures.extend(_pending_rows(evidence))
    measures.append(_v1_runs_row(evidence))
    measures.sort(key=lambda row: row["measure"])

    window, reasons = _window(evidence)
    reasons.extend(
        f"measure {row['measure']} ({row['name']}): {row['blocking']}"
        for row in measures
        if not row["pass"]
    )
    errors = [f"{key}: {value}" for key, value in sorted(evidence.errors.items())]
    return WindowVerdict(
        measures=measures, window=window, blocking_reasons=reasons, evidence_errors=errors
    )
