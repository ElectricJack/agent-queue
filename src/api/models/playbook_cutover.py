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


class CutoverAuthorizationDTO(V2Model):
    """One G2 signature: who the server saw (``actor``) and who the human
    declared themselves to be (``signed_by``), in one of the two roles."""

    event_id: str | None = None
    at: float | None = None
    actor: str | None = None
    role: Literal["author", "release_operator"] | None = None
    signed_by: str | None = None


class PlaybookCutoverGateStatusResponse(V2Model):
    """Readiness, the current G1 sign-off and the G2 signatures, recomputed
    from source on every call.  ``can_switch`` is the conjunction."""

    success: bool = True
    generated_at: float | None = None
    runtime: Literal["v1", "v2"] | None = None
    ready: bool = False
    #: One ``{check, observed, pass, blocking?}`` row per readiness check.
    #: Untyped for the same reason the window measures are: ``pass`` is a
    #: Python keyword and an aliased field would round-trip under the wrong key.
    checks: list[dict[str, Any]] = []
    drain_signoff: CutoverEventDTO | None = None
    authorizations: list[CutoverAuthorizationDTO] = []
    blocking_reasons: list[str] = []
    can_switch: bool = False
    error: str | None = None


class PlaybookCutoverDrainSignoffResponse(V2Model):
    success: bool
    event: CutoverEventDTO | None = None
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []
    error: str | None = None


class PlaybookCutoverAuthorizeResponse(V2Model):
    success: bool
    event: CutoverEventDTO | None = None
    drain_signoff_event_id: str | None = None
    authorizations: list[CutoverAuthorizationDTO] = []
    blocking_reasons: list[str] = []
    can_switch: bool = False
    error: str | None = None


class PlaybookCutoverSwitchResponse(V2Model):
    success: bool
    runtime: Literal["v1", "v2"] | None = None
    event: CutoverEventDTO | None = None
    error: str | None = None
    #: Present when a switch to v2 was refused: the readiness table and the
    #: gate reasons, so the operator sees what is still holding it without a
    #: second call.
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []
    authorizations: list[CutoverAuthorizationDTO] = []
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
    switched_at: float | None = None
    elapsed_seconds: float | None = None
    wall_clock_ok: bool = False
    wall_clock_gate_seconds: float
    volume_gate_runs: int
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
    can_close: bool = False
    error: str | None = None


class PlaybookCutoverWindowCloseResponse(V2Model):
    success: bool
    event: CutoverEventDTO | None = None
    #: Every measure that stands in the way, recomputed from source by the
    #: refusing call itself rather than read from a cached verdict.
    blocking_reasons: list[str] = []
    measures: list[dict[str, Any]] = []
    error: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "playbook_v1_drain_status": PlaybookV1DrainStatusResponse,
    "playbook_v1_admission_close": PlaybookV1AdmissionResponse,
    "playbook_v1_admission_open": PlaybookV1AdmissionResponse,
    "playbook_v1_run_cancel": PlaybookV1RunCancelResponse,
    "playbook_cutover_gate_status": PlaybookCutoverGateStatusResponse,
    "playbook_cutover_drain_signoff": PlaybookCutoverDrainSignoffResponse,
    "playbook_cutover_authorize": PlaybookCutoverAuthorizeResponse,
    "playbook_cutover_switch": PlaybookCutoverSwitchResponse,
    "playbook_cutover_window_status": PlaybookCutoverWindowStatusResponse,
    "playbook_cutover_window_close": PlaybookCutoverWindowCloseResponse,
}
