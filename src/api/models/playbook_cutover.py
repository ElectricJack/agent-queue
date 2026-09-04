"""Typed response models for the Playbook V1 drain and runtime cutover surface.

Package 7 of the Playbook V2 roadmap
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md`` §3.2-§3.5).

Kept out of ``playbook_migration.py`` for the same reason that module is kept
out of ``playbook_v2.py``: readiness (Package 6) and operational state
(Package 7) are two different lifetimes with two different blast radii, and the
modules that describe them are asserted separately.

Conventions match ``playbook_v2.py``: strict models, POSIX-second floats.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from src.api.models.playbook_v2 import V2Model


class V1RunSummaryDTO(V2Model):
    """One active V1 run, as the drain sees it.

    ``ownership`` is the field the whole drain turns on: ``live`` means a
    coroutine still owns the run and it can finish by itself, ``orphaned``
    means the row outlived the process that started it and only an operator
    write will ever clear it.  ``options`` is never empty — ``cancel`` is
    always available.
    """

    run_id: str
    playbook_id: str
    playbook_version: int
    status: str
    current_node: str | None = None
    started_at: float
    age_seconds: float
    paused_at: float | None = None
    waiting_for_event: str | None = None
    event_id: str | None = None
    project_id: str | None = None
    ownership: Literal["live", "orphaned"]
    options: list[Literal["wait", "resolve", "cancel"]]


class CutoverEventDTO(V2Model):
    """One row of the append-only cutover audit."""

    event_id: str
    kind: str
    at: float
    actor: str
    reason: str
    detail: dict[str, Any] = {}


class PlaybookV1DrainStatusResponse(V2Model):
    """``drained`` is a conjunction, deliberately: admission closed *and* no
    active run.  A zero count on its own is a snapshot — a run can start
    immediately after it is read — so it is never the gate by itself."""

    success: bool = True
    generated_at: float
    admission: Literal["open", "closed"]
    closed_at: float | None = None
    closed_by: str | None = None
    active: list[V1RunSummaryDTO] = []
    live_count: int
    orphaned_count: int
    oldest_age_seconds: float | None = None
    drained: bool
    error: str | None = None


class PlaybookV1AdmissionResponse(V2Model):
    """The drain snapshot as it stands after an admission change."""

    success: bool
    event: CutoverEventDTO | None = None
    generated_at: float | None = None
    admission: Literal["open", "closed"] | None = None
    closed_at: float | None = None
    closed_by: str | None = None
    active: list[V1RunSummaryDTO] = []
    live_count: int | None = None
    orphaned_count: int | None = None
    oldest_age_seconds: float | None = None
    drained: bool | None = None
    error: str | None = None


class PlaybookV1RunCancelResponse(V2Model):
    """A cancel is only reported successful once the row is terminal *and* the
    coroutine that could have overwritten it is gone."""

    success: bool
    run_id: str | None = None
    ownership: Literal["live", "orphaned"] | None = None
    status: str | None = None
    completed_at: float | None = None
    error: str | None = None


class PlaybookCutoverGateStatusResponse(V2Model):
    """Mechanical readiness recomputed from source on every call."""

    success: bool = True
    generated_at: float | None = None
    runtime: Literal["v1", "v2"] | None = None
    ready: bool = False
    #: One ``{check, observed, pass, blocking?}`` row per readiness check.
    #: Untyped for the same reason the window measures are: ``pass`` is a
    #: Python keyword and an aliased field would round-trip under the wrong key.
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []
    can_switch: bool = False
    error: str | None = None


class PlaybookCutoverSwitchResponse(V2Model):
    success: bool
    runtime: Literal["v1", "v2"] | None = None
    event: CutoverEventDTO | None = None
    error: str | None = None
    #: Present when a switch to v2 was refused, so the operator sees what is
    #: still holding it without a
    #: second call.
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []
    #: The drain snapshot, present on the same refusals.
    generated_at: float | None = None
    admission: Literal["open", "closed"] | None = None
    active: list[V1RunSummaryDTO] = []
    live_count: int | None = None
    orphaned_count: int | None = None
    oldest_age_seconds: float | None = None
    drained: bool | None = None
    closed_at: float | None = None
    closed_by: str | None = None


class CutoverWindowDTO(V2Model):
    """The observation window (§3.5): wall clock, coverage and volume.

    ``since``/``until``/``observed_at`` are the durable bounds every measure
    was read over — ``since`` is the ``switched_to_v2`` audit row's timestamp,
    never a clock the daemon could have restarted.
    """

    switched_at: float | None = None
    since: float | None = None
    until: float | None = None
    observed_at: float | None = None
    elapsed_seconds: float | None = None
    wall_clock_ok: bool = False
    wall_clock_gate_seconds: float
    coverage_ok: bool = False
    #: Enabled playbooks with no V2 run since the switch.
    coverage_missing: list[str] = []
    enabled_playbooks: list[str] = []
    volume_ok: bool = False
    volume_gate_runs: int
    v2_run_count: int = 0
    v2_runs_by_playbook: dict[str, int] = {}
    #: When the last ``window_coverage_rehearsal`` ran, if one has.
    rehearsal_at: float | None = None
    closed_at: float | None = None


class PlaybookCutoverWindowStatusResponse(V2Model):
    success: bool = True
    generated_at: float | None = None
    runtime: Literal["v1", "v2"] | None = None
    admission: Literal["open", "closed"] | None = None
    #: One ``{measure, name, source, observed, gate, pass, blocking?}`` row
    #: each.  Left untyped because ``pass`` is a Python keyword and an
    #: aliased field would round-trip under the wrong key; the same shape
    #: ``PlaybookCutoverReportResponse`` uses for its evidence rows.
    measures: list[dict[str, Any]] = []
    window: CutoverWindowDTO | None = None
    blocking_reasons: list[str] = []
    #: ``source: error`` for every evidence source that could not be read.
    #: Each one also fails the measures it feeds; listed here so the operator
    #: sees the read failure itself and not only its consequences.
    evidence_errors: list[str] = []
    can_close: bool = False
    error: str | None = None


class PlaybookCutoverWindowCloseResponse(V2Model):
    success: bool
    event: CutoverEventDTO | None = None
    #: Every measure that stands in the way, recomputed from source by the
    #: refusing call itself rather than read from a cached verdict.
    blocking_reasons: list[str] = []
    measures: list[dict[str, Any]] = []
    window: CutoverWindowDTO | None = None
    evidence_errors: list[str] = []
    error: str | None = None


class PlaybookCutoverWindowRehearsalResponse(V2Model):
    """One synthetic live dispatch per enabled playbook, recorded in the audit."""

    success: bool
    event: CutoverEventDTO | None = None
    #: Every enabled, ready playbook the rehearsal addressed.
    playbooks: list[str] = []
    #: ``playbook_id -> run ids`` the rehearsal started.
    runs: dict[str, list[str]] = {}
    #: Playbooks whose synthetic event produced no run (a guard rejected it,
    #: or the dispatch failed — see ``errors``).  Coverage is still measured
    #: from the run table, so these stay uncovered until real traffic arrives.
    uncovered: list[str] = []
    errors: dict[str, str] = {}
    error: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "playbook_v1_drain_status": PlaybookV1DrainStatusResponse,
    "playbook_v1_admission_close": PlaybookV1AdmissionResponse,
    "playbook_v1_admission_open": PlaybookV1AdmissionResponse,
    "playbook_v1_run_cancel": PlaybookV1RunCancelResponse,
    "playbook_cutover_gate_status": PlaybookCutoverGateStatusResponse,
    "playbook_cutover_switch": PlaybookCutoverSwitchResponse,
    "playbook_cutover_window_status": PlaybookCutoverWindowStatusResponse,
    "playbook_cutover_window_rehearsal": PlaybookCutoverWindowRehearsalResponse,
    "playbook_cutover_window_close": PlaybookCutoverWindowCloseResponse,
}
