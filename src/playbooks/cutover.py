"""Operational state of the Playbook V1 → V2 cutover: admission, drain, switch.

Playbook V2 Package 7
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md`` §3.2-§3.4).

This module is deliberately **not** :mod:`src.playbooks.migration`.  That one
answers "is the fleet ready?" and is read-only by contract; this one is
operational state with a much larger blast radius — it can refuse new runs,
kill live ones, and change which runtime the whole fleet uses.  Two lifetimes,
two blast radii, two modules.  The dependency is one-way — ``cutover`` imports
``migration``, never the reverse — because a readiness report that depended on
operational state, or vice versa, could not be trusted as evidence for a gate.
``tests/test_playbook_cutover.py::test_cutover_does_not_import_engine_or_create_cycle``
is the ratchet.

Nothing here imports :mod:`src.playbooks.engine` at module scope: the drain has
to work on a daemon whose playbook subsystem is paused, which is exactly the
fleet that most needs draining.

**Why the drain needs an ownership classification at all.**  V1 run state lives
in two places that can disagree: the durable ``playbook_runs`` row, and
``PlaybookManager._running``, an in-memory ``dict[str, asyncio.Task]``.  Runs
are dispatched fire-and-forget and nothing reconciles the two on startup, so a
daemon restart mid-run leaves a ``running`` row that no code path will ever move
to a terminal status.  "Reach zero active V1 runs" is therefore unreachable by
waiting — the orphans need an operator's terminal write, and the operator needs
to be told which rows those are.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from src.playbooks.migration import MigrationInventory  # noqa: F401  (§2.1 one-way edge)

logger = logging.getLogger(__name__)

#: The two ``playbook_runs`` statuses that are not terminal.  A drain is over
#: when no row holds either.
ACTIVE_RUN_STATUSES: tuple[str, ...] = ("running", "paused")

#: How long :func:`cancel_v1_run` waits for a cancelled coroutine to actually
#: stop before it gives up and leaves the row untouched (§3.3.1).
CANCEL_JOIN_TIMEOUT: float = 30.0

#: Minimum length of the mandatory ``--reason`` on every operator write in this
#: package.  A drain with no stated reason is refused, not defaulted.
MIN_CUTOVER_REASON_LENGTH: int = 10

V1RunOwnership = Literal["live", "orphaned"]
#: ``"live"``     — ``run_id`` is in ``PlaybookManager.running_runs()``; a
#:                  coroutine owns it and it can still finish on its own.
#: ``"orphaned"`` — the row says running/paused but no coroutine does; the
#:                  process that started it is gone.  Only a terminal write
#:                  clears it.

V1RunOption = Literal["wait", "resolve", "cancel"]
#: ``"wait"``    — offered only for ``live``; the operator lets it finish.
#: ``"resolve"`` — offered only for ``paused``; routes to the existing
#:                 ``resume_playbook`` command with the human's input.
#: ``"cancel"``  — offered always; ``playbook_v1_run_cancel``.

PlaybookRuntime = Literal["v1", "v2"]

#: The two signatures gate G2 needs (§3.9): the person who wrote the change
#: and the person releasing it, and they must be two different people.
CUTOVER_AUTHORIZATION_ROLES: tuple[str, ...] = ("author", "release_operator")

#: Event kinds that end one cutover attempt and start the next.  A G1 sign-off
#: older than any of these authorised a fleet state that no longer exists —
#: the rollback happened for a reason — so it does not carry over.
CYCLE_BOUNDARY_EVENT_KINDS: tuple[str, ...] = (
    "switched_to_v2",
    "rolled_back_to_v1",
    "v1_admission_reopened",
)

#: Shortest ``signed_by`` accepted.  One character is not a name.
MIN_SIGNER_LENGTH: int = 2


@dataclass(frozen=True, slots=True)
class V1RunSummary:
    """One active V1 run as the drain sees it."""

    run_id: str
    playbook_id: str
    playbook_version: int
    status: str
    current_node: str | None
    started_at: float
    age_seconds: float
    paused_at: float | None
    waiting_for_event: str | None
    event_id: str | None
    project_id: str | None
    ownership: V1RunOwnership
    options: tuple[V1RunOption, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "playbook_id": self.playbook_id,
            "playbook_version": self.playbook_version,
            "status": self.status,
            "current_node": self.current_node,
            "started_at": self.started_at,
            "age_seconds": self.age_seconds,
            "paused_at": self.paused_at,
            "waiting_for_event": self.waiting_for_event,
            "event_id": self.event_id,
            "project_id": self.project_id,
            "ownership": self.ownership,
            "options": list(self.options),
        }


@dataclass(frozen=True, slots=True)
class DrainStatus:
    """A snapshot of how far the V1 drain has got.

    ``drained`` is deliberately a conjunction: zero active runs *and* admission
    closed.  Zero alone is not a gate — a run can start a millisecond later —
    which is why gate G1 requires admission closed before the drain is read.
    """

    generated_at: float
    admission: Literal["open", "closed"]
    closed_at: float | None
    closed_by: str | None
    active: tuple[V1RunSummary, ...]
    live_count: int
    orphaned_count: int
    oldest_age_seconds: float | None
    drained: bool

    def to_dict(self) -> dict[str, Any]:
        """Stable key order; the API DTO and the CLI both render this."""
        return {
            "generated_at": self.generated_at,
            "admission": self.admission,
            "closed_at": self.closed_at,
            "closed_by": self.closed_by,
            "active": [run.to_dict() for run in self.active],
            "live_count": self.live_count,
            "orphaned_count": self.orphaned_count,
            "oldest_age_seconds": self.oldest_age_seconds,
            "drained": self.drained,
        }


# ---------------------------------------------------------------------------
# Selectors — read per dispatch, never cached
# ---------------------------------------------------------------------------


def playbook_runtime(config: Any) -> PlaybookRuntime:
    """Which runtime every entry point must dispatch through.

    Reconciled against the live tree (§3.8): the child plan specified a
    ``PlaybooksConfig.runtime`` field, but the switch actually shipped in
    Packages 4-6 as the boolean ``playbooks.v2_engine`` and all six §1.4 entry
    points already consult it through
    :func:`src.playbooks.services.v2_engine_enabled`.  This function is the
    plan's name for the answer that flag already gives; it deliberately does
    **not** introduce a second, competing selector, which is exactly the
    "reimplementation of an earlier package's interface" §3.8 forbids.
    """
    from src.playbooks.services import v2_engine_enabled

    return "v2" if v2_engine_enabled(config) else "v1"


def v1_admission_closed(config: Any) -> bool:
    """Whether new V1 runs are refused.

    Independent of :func:`playbook_runtime` on purpose.  Draining happens while
    the fleet is still on V1, and a rollback flips the runtime back *without*
    reopening admission — ``runtime="v1"`` with admission closed is the
    supported rollback state: existing runs resume, no new ones start.
    """
    playbooks = getattr(config, "playbooks", None)
    return str(getattr(playbooks, "v1_admission", "open")) == "closed"


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


def _project_id_of(trigger_event: Any) -> str | None:
    """The ``project_id`` a run was triggered for, when the payload records one."""
    raw = trigger_event
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("project_id")
    return str(value) if value else None


def _options_for(status: str, ownership: V1RunOwnership) -> tuple[V1RunOption, ...]:
    """What the operator may do with one run.  Never empty."""
    options: list[V1RunOption] = []
    if ownership == "live":
        options.append("wait")
    if status == "paused":
        options.append("resolve")
    options.append("cancel")
    return tuple(options)


async def drain_status(
    *,
    db: Any,
    manager: Any,
    config: Any,
    clock: Callable[[], float] = time.time,
) -> DrainStatus:
    """Snapshot every non-terminal V1 run, classified by ownership.

    ``manager=None`` means **orphaned, not unknown.**  A caller with no manager
    — a CLI against a stopped daemon, a test — cannot prove a coroutine exists,
    and for a cutover gate the safe answer is the one that requires an operator
    decision.  The opposite default would let ``drained`` go true because
    nobody was looking.

    Ownership is computed once per snapshot, from a single
    ``set(manager.running_runs())`` read rather than per row: a run completing
    mid-scan would otherwise be able to appear in neither set.
    """
    now = clock()
    owned: set[str] = set()
    if manager is not None:
        try:
            owned = set(manager.running_runs())
        except Exception:  # pragma: no cover - a manager that cannot answer
            logger.exception("PlaybookManager.running_runs() failed; treating all rows orphaned")
            owned = set()

    rows: list[Any] = []
    for status in ACTIVE_RUN_STATUSES:
        rows.extend(await db.list_playbook_runs(status=status, limit=1000))

    summaries: list[V1RunSummary] = []
    for row in rows:
        ownership: V1RunOwnership = "live" if row.run_id in owned else "orphaned"
        started_at = float(row.started_at or 0.0)
        summaries.append(
            V1RunSummary(
                run_id=row.run_id,
                playbook_id=row.playbook_id,
                playbook_version=int(row.playbook_version or 0),
                status=row.status,
                current_node=row.current_node,
                started_at=started_at,
                age_seconds=max(0.0, now - started_at),
                paused_at=row.paused_at,
                waiting_for_event=row.waiting_for_event,
                event_id=row.event_id,
                project_id=_project_id_of(row.trigger_event),
                ownership=ownership,
                options=_options_for(row.status, ownership),
            )
        )
    summaries.sort(key=lambda s: (s.started_at, s.run_id))

    closed_at: float | None = None
    closed_by: str | None = None
    latest = getattr(db, "latest_playbook_cutover_event", None)
    if latest is not None:
        event = await latest("v1_admission_closed")
        if event is not None:
            closed_at = event["at"]
            closed_by = event["actor"]

    admission = "closed" if v1_admission_closed(config) else "open"
    return DrainStatus(
        generated_at=now,
        admission=admission,
        closed_at=closed_at if admission == "closed" else None,
        closed_by=closed_by if admission == "closed" else None,
        active=tuple(summaries),
        live_count=sum(1 for s in summaries if s.ownership == "live"),
        orphaned_count=sum(1 for s in summaries if s.ownership == "orphaned"),
        oldest_age_seconds=summaries[0].age_seconds if summaries else None,
        drained=admission == "closed" and not summaries,
    )


def v1_latency_baseline(runs: list[Any]) -> dict[str, float | int | None]:
    """The V1 latency the cutover thresholds are anchored to.

    Recorded during the drain rather than invented: there is no production V2
    baseline to compare against, so every latency gate in §3.5 is expressed as
    a multiple of what V1 actually did on this fleet.  Computed from the drained
    rows' own timestamps, which is all the evidence a drain leaves behind.
    """
    # ``is not None``, not truthiness: a 0.0 timestamp is a real value, and
    # dropping it would quietly shrink the sample the gates are anchored to.
    durations = sorted(
        float(r.completed_at) - float(r.started_at)
        for r in runs
        if getattr(r, "completed_at", None) is not None
        and getattr(r, "started_at", None) is not None
    )
    resumes = sorted(
        float(r.completed_at) - float(r.paused_at)
        for r in runs
        if getattr(r, "completed_at", None) is not None
        and getattr(r, "paused_at", None) is not None
    )
    return {
        "sample_size": len(durations),
        "dispatch_p95": _p95(durations),
        "resume_p95": _p95(resumes),
    }


def _p95(values: list[float]) -> float | None:
    """95th percentile in seconds, or ``None`` for an empty sample.

    Nearest-rank, not interpolated: with the handful of samples a drain
    produces, an interpolated percentile invents precision the data does not
    have.
    """
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(0.95 * len(values)) - 1))
    return values[index]


# ---------------------------------------------------------------------------
# Gates G1 and G2 (§3.9) — pure functions over the audit log
# ---------------------------------------------------------------------------


def readiness_check(
    name: str,
    *,
    observed: Any,
    passed: bool,
    blocking: str | None = None,
) -> dict[str, Any]:
    """One row of the switch-readiness table: ``{check, observed, pass, blocking?}``.

    The same shape for every evidence source, so the sign-off event can carry
    the table verbatim and an auditor reads one format.  ``blocking`` is set
    only when ``pass`` is false; a passing row has nothing to say.
    """
    row: dict[str, Any] = {"check": name, "observed": observed, "pass": bool(passed)}
    if not passed:
        row["blocking"] = blocking or f"{name} did not pass"
    return row


def normalize_signer(name: Any) -> str:
    """The comparison key for a human's name: trimmed, single-spaced, casefolded.

    Empty when *name* is not a usable name.  Two signatures are from the same
    person when their keys are equal — ``"Alice"`` and ``"  alice "`` are one
    signer, not two.
    """
    if not isinstance(name, str):
        return ""
    collapsed = " ".join(name.split())
    if len(collapsed) < MIN_SIGNER_LENGTH:
        return ""
    return collapsed.casefold()


def display_signer(name: Any) -> str:
    """The name as recorded: trimmed and single-spaced, case preserved."""
    return " ".join(name.split()) if isinstance(name, str) else ""


def _event_order(event: dict[str, Any]) -> tuple[float, str]:
    return (float(event.get("at") or 0.0), str(event.get("event_id") or ""))


def current_drain_signoff(events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The ``drain_completed`` row that authorises the *current* attempt, if any.

    The latest sign-off counts only when no cycle boundary
    (:data:`CYCLE_BOUNDARY_EVENT_KINDS`) was recorded after it.  Ties are
    resolved against the sign-off: a boundary at the same instant invalidates
    it, because a gate that guesses in its own favour is not a gate.
    """
    signoff: dict[str, Any] | None = None
    boundary: dict[str, Any] | None = None
    for event in events:
        kind = event.get("kind")
        if kind == "drain_completed":
            if signoff is None or _event_order(event) > _event_order(signoff):
                signoff = event
        elif kind in CYCLE_BOUNDARY_EVENT_KINDS and (
            boundary is None or _event_order(event) > _event_order(boundary)
        ):
            boundary = event
    if signoff is None:
        return None
    if boundary is not None and _event_order(boundary) >= _event_order(signoff):
        return None
    return signoff


@dataclass(frozen=True, slots=True)
class AuthorizationStatus:
    """Where gate G2 stands for one drain sign-off."""

    drain_signoff_event_id: str | None
    authorizations: tuple[dict[str, Any], ...]
    satisfied: bool
    blocking_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "drain_signoff_event_id": self.drain_signoff_event_id,
            "authorizations": [dict(a) for a in self.authorizations],
            "satisfied": self.satisfied,
            "blocking_reasons": list(self.blocking_reasons),
        }


def authorization_summary(event: dict[str, Any]) -> dict[str, Any]:
    """The operator-facing view of one ``cutover_authorized`` row."""
    detail = event.get("detail") or {}
    return {
        "event_id": event.get("event_id"),
        "at": event.get("at"),
        "actor": event.get("actor"),
        "role": detail.get("role"),
        "signed_by": detail.get("signed_by"),
    }


def authorization_status(
    signoff: dict[str, Any] | None, events: Sequence[dict[str, Any]]
) -> AuthorizationStatus:
    """Evaluate G2 against the audit log.

    Only ``cutover_authorized`` rows whose ``detail.drain_signoff_event_id``
    names *signoff* count: a signature authorises one specific sign-off, not
    "whatever the fleet looks like now".  Satisfied when every role in
    :data:`CUTOVER_AUTHORIZATION_ROLES` has a signature and no two roles were
    signed by the same person (:func:`normalize_signer`).
    """
    if signoff is None:
        return AuthorizationStatus(
            None, (), False, ("no current drain sign-off (G1) to authorize",)
        )
    signoff_id = str(signoff.get("event_id") or "")
    matching = sorted(
        (
            e
            for e in events
            if e.get("kind") == "cutover_authorized"
            and str((e.get("detail") or {}).get("drain_signoff_event_id") or "") == signoff_id
        ),
        key=_event_order,
    )
    authorizations = tuple(authorization_summary(e) for e in matching)

    by_role: dict[str, dict[str, Any]] = {}
    for auth in authorizations:
        role = str(auth.get("role") or "")
        if role in CUTOVER_AUTHORIZATION_ROLES and role not in by_role:
            by_role[role] = auth

    reasons: list[str] = []
    missing = [role for role in CUTOVER_AUTHORIZATION_ROLES if role not in by_role]
    if missing:
        have = (
            ", ".join(
                f"{role}={by_role[role]['signed_by']}"
                for role in CUTOVER_AUTHORIZATION_ROLES
                if role in by_role
            )
            or "none"
        )
        reasons.append(
            "G2 needs an authorization from each of "
            + ", ".join(CUTOVER_AUTHORIZATION_ROLES)
            + f"; missing: {', '.join(missing)} (have: {have})"
        )
    signers = [normalize_signer(by_role[role].get("signed_by")) for role in by_role]
    if len(by_role) == len(CUTOVER_AUTHORIZATION_ROLES) and len(set(signers)) < len(signers):
        reasons.append(
            "G2 requires two distinct people; the same person signed as " + " and ".join(by_role)
        )
    return AuthorizationStatus(
        drain_signoff_event_id=signoff_id,
        authorizations=authorizations,
        satisfied=not reasons,
        blocking_reasons=tuple(reasons),
    )
