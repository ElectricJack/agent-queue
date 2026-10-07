"""Read-only display projection: epic implementation progress vs delivery.

An epic's ``N/N`` child count is implementation progress; it never says the
work shipped. A stored ``PAUSED`` on a managed parent is usually the
integration checkpoint's hold rather than a person's. This module answers
"where is this epic's delivery, who has to act, and since when" for the
dashboard, without persisting anything, changing a task's lifecycle or
authorizing an action.

It is deliberately not a state machine. Every fact comes from an existing
read: collection readiness (:meth:`ParentEpisodeRecords.readiness_on`), claim
eligibility (:func:`claim_frontier_predicates`), live operations, branch
reservations, root delivery receipts, operator holds and — for what git says
about the epic's *current* completion right now — the request-scoped
:class:`~src.integration.delivery_observer.DeliveryObserver` every other
delivery consumer already asks. :func:`classify_epic_delivery` is a pure
function over those facts, so every state is testable from fixtures. Design:
docs/superpowers/specs/2026-10-02-epic-delivery-status-design.md.

The canonical git answer is gathered outside every transaction and its
identity rechecked afterwards, exactly as
:class:`~src.integration.delivery_observer.DeliveryView` requires, so this
view never invents a receipt and never persists a git answer. What it reads
here is what the archive guard, the settlement path and the publisher read:
one question, one answer.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import JSON, exists, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from src.config import SessionsConfig
from src.database.queries.claim_queries import (
    FRONTIER_PREDICATE_DETAILS,
    claim_frontier_predicates,
)
from src.database.queries.integration_train_queries import (
    _ACTIVE_BATCH_LIFECYCLES,
    _root_delivery_receipt_conditions,
)
from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.database.tables import (
    events,
    gates,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    repos,
    sessions,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_labels,
    task_metadata,
    tasks,
)
from src.integration.live_operations import ACTIVE_OPERATION_STATES
from src.integration.records import ParentEpisodeRecords

logger = logging.getLogger(__name__)

#: Integration modes whose parents carry durable delivery evidence.
TRACKED_MODES = frozenset({"hierarchy", "train"})
TERMINAL_CHILD_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
#: Gate types a person has to resolve.
APPROVAL_GATE_TYPES = frozenset({"human", "review"})
#: Session states that can still be doing work (``stalled`` is derived).
LIVE_SESSION_STATES = ("starting", "running", "draining")
#: Readiness costs a handful of statements per epic, so a single response
#: evaluates it for at most this many epics; the rest report ``unavailable``.
MAX_READINESS_PER_READ = 32

#: What the view reads of an operation; ``policy_snapshot`` feeds readiness.
_OPERATION_COLUMNS = tuple(
    integration_repair_operations.c[name]
    for name in (
        "id", "target_kind", "batch_id", "parent_task_id", "episode_id", "active_stage",
        "state", "verifier_task_id", "policy_snapshot", "created_at", "updated_at",
    )
)
#: A stage's dossier and subjects can be large; the view needs none of them.
_STAGE_COLUMNS = tuple(
    integration_repair_stages.c[name]
    for name in (
        "operation_id", "ordinal", "state", "repair_task_id", "writer_kind", "started_at",
        "deadline_at", "completed_at",
    )
)

_BATCH_RUNNING = frozenset({"building", "testing", "promoting"})
_BATCH_QUEUED = frozenset({"sealing", "sealed"})


@dataclass(frozen=True)
class WorkerFacts:
    """A worker task the epic's delivery waits on (verifier or repair writer)."""

    id: str
    status: str | None = None  # None: the task is gone (archived or deleted)
    updated_at: float | None = None
    profile_id: str | None = None
    session: Mapping[str, Any] | None = None  # the newest live session bound to it
    blocked_terminal: str | None = None
    needs_attention: str | None = None
    #: Frontier predicates that exclude this task while it is READY.
    exclusions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanonicalDelivery:
    """What git says about this epic's *current* completion, right now.

    Filled only from a request-scoped
    :class:`~src.integration.delivery_observer.DeliveryObserver` answer whose
    identity and target were rechecked after the fetch
    (:meth:`~src.integration.delivery_observer.DeliveryView.verified_on`), so
    it never describes a generation the epic no longer has. ``state`` is a
    :class:`~src.integration.delivery_truth.DeliveryState` value as text:
    ``contained``, ``no_change``, ``pending``, ``settled``, ``no_artifact`` or ``unknown``.

    ``acceptance`` is wording, never the proof: git already established
    containment, and it names a recorded operator adoption only when that
    adoption explicitly accepted a source which is not an ancestor.
    """

    state: str
    reason: str
    source: str | None = None
    target_ref: str | None = None
    target_oid: str | None = None
    completion_id: str | None = None
    parent_generation: int | None = None
    acceptance: str | None = None
    settled_reason: str | None = None
    #: Epoch seconds the identity itself records (the verified parent
    #: completion, or the leaf completion).
    since: float | None = None
    #: The identity or target moved after the observation, so the answer
    #: describes a question that is no longer the current one.
    changed: bool = False


@dataclass(frozen=True)
class EpicFacts:
    task_id: str
    status: str
    updated_at: float | None = None
    resume_after: float | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    parent_status: str | None = None
    mode: str = "disabled"
    children: tuple[Mapping[str, Any], ...] = ()
    manual_hold: bool = False
    hold_labels: tuple[str, ...] = ()
    approval_gates: tuple[Mapping[str, Any], ...] = ()
    checkpoint: Mapping[str, Any] | None = None
    #: The live repair operation of the checkpoint's current episode.
    operation: Mapping[str, Any] | None = None
    collection_cancelled: bool = False
    stage: Mapping[str, Any] | None = None
    owner: Mapping[str, Any] | None = None
    verifier: WorkerFacts | None = None
    writer: WorkerFacts | None = None
    readiness: Mapping[str, Any] | None = None
    readiness_error: str | None = None
    receipt: Mapping[str, Any] | None = None
    stale_receipt: Mapping[str, Any] | None = None
    batch: Mapping[str, Any] | None = None
    batch_operation: Mapping[str, Any] | None = None
    batch_writer: WorkerFacts | None = None
    #: Git's own answer for this epic's current completion; ``None`` when no
    #: observer is registered or git had nothing to say about it.
    canonical: CanonicalDelivery | None = None


def lease_ttl_from(config: Any) -> float:
    """``sessions.lease_ttl_seconds`` from a config, or the shipped default."""
    ttl = getattr(getattr(config, "sessions", None), "lease_ttl_seconds", None)
    if isinstance(ttl, bool) or not isinstance(ttl, int | float):
        return float(SessionsConfig.lease_ttl_seconds)
    return float(ttl)


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------


def _humanize(status: str) -> str:
    return status.replace("_", " ").capitalize()


def _hold(facts: EpicFacts) -> str | None:
    if facts.manual_hold or facts.hold_labels:
        return "operator"
    if facts.status != "PAUSED":
        return None
    if facts.resume_after:
        return "backoff"
    if facts.mode in TRACKED_MODES and facts.checkpoint is not None:
        return "integration"
    return None


_DISPLAY = {
    "queued": "Queued",
    "integrating": "Integrating",
    "verifying": "Verifying",
    "blocked": "Delivery blocked",
    "awaiting_approval": "Awaiting approval",
    "paused": "Paused",
    "delivered": "Delivered",
    "unknown": "Delivery unknown",
}


def _display_status(state: str, facts: EpicFacts, hold: str | None) -> str:
    if state == "paused" and not facts.manual_hold:
        return "Held"
    if state in _DISPLAY:
        return _DISPLAY[state]
    # implementing / not_tracked keep the stored lifecycle, except that a
    # PAUSED nobody chose never reads as "Paused".
    if facts.status == "PAUSED" and hold != "operator":
        return "In progress" if state == "implementing" else "Waiting"
    return _humanize(facts.status)


def _ref(kind: str, ref_id: str | None, label: str) -> dict[str, Any]:
    return {"kind": kind, "id": ref_id, "label": label}


def _task_link(task_id: str, label: str) -> dict[str, Any]:
    return _ref("task", task_id, label)


def _max_ts(*values: Any) -> float | None:
    present = [float(value) for value in values if value is not None]
    return max(present) if present else None


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _short(value: Any) -> str:
    """A git SHA to 12 characters and a UUID to 8, as a label shows them."""
    text = str(value or "")
    if len(text) == 40:
        return text[:12]
    if len(text) == 36 and text.count("-") == 4:
        return text[:8]
    return text


def classify_epic_delivery(
    facts: EpicFacts, *, now: float, lease_ttl: float = SessionsConfig.lease_ttl_seconds
) -> dict[str, Any]:
    """Map one epic's facts to its display result (see the design spec's order)."""
    hold = _hold(facts)
    children = facts.children
    completed = sum(1 for child in children if child.get("status") == "COMPLETED")
    links: list[dict[str, Any]] = []
    operation = facts.operation

    def result(
        state: str,
        label: str,
        *,
        reason: str | None = None,
        responsible: dict[str, Any] | None = None,
        since: float | None = None,
        evidence: str = "current",
        remedy: str | None = None,
        extra: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        merged: list[dict[str, Any]] = []
        for link in [*extra, *links]:
            if link not in merged:
                merged.append(dict(link))
        return {
            "state": state,
            "label": label,
            "display_status": _display_status(state, facts, hold),
            "hold": hold,
            "reason": reason,
            "remedy": remedy,
            "responsible": responsible,
            "since": since,
            "links": merged,
            "evidence": evidence,
            "implementation_completed": completed,
            "implementation_total": len(children),
        }

    if operation is not None:
        links.append(_ref("operation", operation["id"], f"Operation {_short(operation['id'])}"))

    # 1. Delivered: git's own answer for the epic's current completion, or a
    #    receipt binding the current head.  A canonical adoption, a direct
    #    publish and a train receipt all land here, so an epic the publisher
    #    delivered while no train batch ever existed stops reading as
    #    "waiting for the next train".
    adopted = _canonical_delivered(facts, result=result)
    if adopted is not None:
        return adopted
    if facts.receipt is not None:
        receipt = facts.receipt
        if receipt.get("target_task_id"):
            label = "Delivered to parent epic"
            reason = f"Collected into {receipt['target_task_id']}"
            extra = [_task_link(receipt["target_task_id"], "Parent epic")]
        else:
            branch = str(receipt.get("target_branch") or "").removeprefix("refs/heads/")
            label = "Delivered"
            reason = f"Delivered to {branch or 'the default branch'}"
            if receipt.get("batch_id"):
                reason += f" by train batch {_short(receipt['batch_id'])}"
            extra = (
                [_ref("batch", receipt["batch_id"], f"Batch {_short(receipt['batch_id'])}")]
                if receipt.get("batch_id") else []
            )
        return result("delivered", label, reason=reason, since=receipt.get("created_at"), extra=extra)

    # 2. An operator hold is the only user-facing "Paused".
    if facts.manual_hold or facts.hold_labels:
        if facts.manual_hold:
            label, reason = "Paused by operator", "Paused manually; Resume releases it"
        else:
            label, reason = "Held by operator", f"Withheld by label {', '.join(facts.hold_labels)}"
        return result(
            "paused", label, reason=reason,
            responsible=_ref("operator", None, "Operator"), since=facts.updated_at,
        )

    # 3. A person has to decide.
    if facts.approval_gates:
        gate = facts.approval_gates[0]
        return result(
            "awaiting_approval", "Awaiting approval",
            reason=str(gate.get("title") or gate.get("question") or "An approval gate is open"),
            responsible=_ref("operator", None, "Operator"), since=gate.get("created_at"),
            extra=[_ref("gate", gate["id"], f"Gate {gate['id']}")],
        )
    for live_operation, scope in ((operation, "Integration"), (facts.batch_operation, "Train")):
        if live_operation is not None and live_operation.get("state") == "human_required":
            return result(
                "awaiting_approval", f"{scope} awaiting operator decision",
                reason=f"Repair operation {_short(live_operation['id'])} needs a human decision",
                responsible=_ref("operator", None, "Operator"),
                since=live_operation.get("updated_at"),
                remedy=(f"aq integration status {facts.project_id}" if facts.project_id
                        else f"aq task show {facts.task_id}"),
                extra=[_ref("operation", live_operation["id"], f"Operation {_short(live_operation['id'])}")],
            )
    if facts.batch is not None and facts.batch.get("lifecycle") == "human_blocked":
        return result(
            "awaiting_approval", "Train awaiting operator decision",
            reason=f"Train batch {_short(facts.batch['id'])} is held for a human",
            responsible=_ref("operator", None, "Operator"), since=facts.batch.get("updated_at"),
            extra=[_ref("batch", facts.batch["id"], f"Batch {_short(facts.batch['id'])}")],
        )

    # 4. Implementation still running.
    open_children = [child for child in children if child.get("status") not in TERMINAL_CHILD_STATUSES]
    if open_children:
        return result(
            "implementing", "Implementation in progress",
            reason=f"{len(open_children)} of {len(children)} tasks still open",
            since=_max_ts(*(child.get("updated_at") for child in children)),
        )

    # 5. No durable *train* evidence for this project's mode.
    if facts.mode not in TRACKED_MODES:
        # A development/observe project still records delivery canonically:
        # git answers whether the completed epic's own work is on the
        # target, and that answer outranks "no per-epic delivery record".
        unproven = _canonical_unproven(facts, result=result)
        if unproven is not None:
            return unproven
        reason = f"Integration mode {facts.mode} records no per-epic delivery"
        if hold == "backoff":
            reason = "Paused for a backoff; resumes automatically"
        return result(
            "not_tracked", "Delivery not tracked", reason=reason,
            since=facts.updated_at, evidence="untracked",
        )

    # 6. Train / hierarchy delivery.
    active = _active_work(facts, now=now, lease_ttl=lease_ttl, result=result)
    if active is not None:
        return active

    failed = [child for child in children if child.get("status") == "FAILED"]
    if failed:
        return result(
            "blocked", "Integration blocked - failed child work",
            reason=f"{', '.join(child['id'] for child in failed)} failed; the epic cannot be "
            "collected until the work is retried or disposed",
            responsible=_ref("operator", None, "Operator"),
            since=_max_ts(*(child.get("updated_at") for child in failed)),
            extra=[_task_link(child["id"], child.get("title") or child["id"]) for child in failed],
        )

    checkpoint = facts.checkpoint
    if checkpoint is None:
        if facts.status == "COMPLETED":
            # Finished before the train tracked it (or in another mode): the
            # train never owed it a receipt, so its silence is not a blocker.
            return result(
                "not_tracked", "Completed outside the train",
                reason="No integration checkpoint was recorded, so the train holds no "
                "delivery record for this epic",
                since=facts.updated_at, evidence="untracked",
            )
        return result(
            "unknown", "Delivery evidence unavailable",
            reason="No integration checkpoint is recorded for this epic",
            since=facts.updated_at, evidence="unavailable",
        )

    if facts.collection_cancelled and (operation is None):
        if facts.status == "COMPLETED":
            return result(
                "not_tracked", "Completed outside the train",
                reason="Its collection was cancelled and it completed outside the train; "
                "no train receipt records its delivery",
                since=_max_ts(facts.updated_at, checkpoint.get("updated_at")),
                evidence="untracked",
            )
        return result(
            "unknown", "Delivery not recorded",
            reason="This epic's collection was cancelled; no delivery receipt binds its head",
            since=_max_ts(facts.updated_at, checkpoint.get("updated_at")),
            evidence="unavailable",
        )

    if facts.status == "COMPLETED":
        return _completed_epic(facts, result=result)

    if operation is None:
        if checkpoint.get("episode_id") is None:
            return result(
                "queued", "Waiting for collection to start",
                reason="All tasks are complete; the integration collector has not opened collection",
                responsible=_ref("system", None, "Integration collector"),
                since=_max_ts(checkpoint.get("updated_at"), *(c.get("updated_at") for c in children)),
            )
        return result(
            "unknown", "Delivery evidence unavailable",
            reason="The checkpoint names a collection episode with no live operation",
            since=checkpoint.get("updated_at"), evidence="unavailable",
        )

    if facts.readiness_error is not None:
        return result(
            "unknown", "Delivery evidence unavailable",
            reason=facts.readiness_error, since=checkpoint.get("updated_at"),
            evidence="unavailable",
        )

    blocked = _readiness_blockers(facts, result=result)
    if blocked is not None:
        return blocked

    return _verification_and_repair(facts, result=result)


def _active_work(facts: EpicFacts, *, now: float, lease_ttl: float, result) -> dict | None:
    """Integrating / Verifying need a live, recently active session."""
    candidates = [
        (facts.writer, "integrating", "Integrating", "Integration"),
        (facts.verifier, "verifying", "Verifying aggregate", "Verification"),
        (facts.batch_writer, "integrating", "Integrating - train repair", "Train repair"),
    ]
    for worker, state, label, noun in candidates:
        if worker is None or worker.session is None:
            continue
        session = worker.session
        last = session.get("last_activity") or session.get("started_at")
        who = _ref(
            "session", session.get("id"),
            f"{session.get('profile_id') or 'Session'} {_short(session.get('id'))}".strip(),
        )
        link = _task_link(worker.id, f"{noun} task {worker.id}")
        if lease_ttl > 0 and last is not None and now - float(last) > lease_ttl:
            return result(
                "unknown", f"{noun} status stale",
                reason=f"Session {_short(session.get('id'))} has shown no activity for "
                f"{_fmt_age(now - float(last))} (lease {_fmt_age(lease_ttl)})",
                responsible=who, since=last, evidence="stale", extra=[link],
            )
        detail = (
            f"Repair stage {facts.stage['ordinal']} writer is working"
            if worker is facts.writer and facts.stage is not None
            else f"{noun} task {worker.id} is being worked"
        )
        return result(state, label, reason=detail, responsible=who, since=last, extra=[link])
    return None


def _completed_epic(facts: EpicFacts, *, result) -> dict:
    checkpoint = facts.checkpoint or {}
    if facts.stale_receipt is not None:
        stale = facts.stale_receipt
        return result(
            "unknown", "Delivery evidence stale",
            reason=f"The newest delivery receipt binds head {_short(stale.get('reviewed_head_sha'))}, "
            f"not the epic's current head {_short(_current_head(checkpoint))}",
            since=stale.get("created_at"), evidence="stale",
        )
    batch = facts.batch
    if batch is not None:
        lifecycle = batch.get("lifecycle")
        link = _ref("batch", batch["id"], f"Batch {_short(batch['id'])}")
        train = _ref("system", None, "Integration train")
        if lifecycle in _BATCH_RUNNING:
            label = "Promoting to main" if lifecycle == "promoting" else "Integrating to main"
            return result(
                "integrating", label,
                reason=f"Train batch {_short(batch['id'])} is {lifecycle}",
                responsible=train, since=batch.get("updated_at"), extra=[link],
            )
        if lifecycle in _BATCH_QUEUED:
            return result(
                "queued", "Queued in train batch",
                reason=f"Train batch {_short(batch['id'])} is {lifecycle}",
                responsible=train, since=batch.get("updated_at"), extra=[link],
            )
        if lifecycle == "repairing":
            writer = facts.batch_writer
            if writer is not None and writer.status == "READY":
                return _queued_or_excluded(
                    writer, "Train repair", facts, result=result, since=batch.get("updated_at"),
                    extra=[link],
                )
            return result(
                "blocked", "Integration blocked - train repair has no live writer",
                reason=f"Train batch {_short(batch['id'])} is repairing and nothing is working on it",
                responsible=train, since=batch.get("updated_at"), extra=[link],
            )
        return result(
            "queued", "Finishing train cleanup",
            reason=f"Train batch {_short(batch['id'])} is {lifecycle}",
            responsible=train, since=batch.get("updated_at"), extra=[link],
        )
    # Nothing is running on it, so git has the last word: a verified,
    # canonically adopted epic is delivered, an epic whose current work is
    # provably not on the target is still owed, and an epic git cannot
    # account for says so instead of claiming the train owes it nothing.
    unproven = _canonical_unproven(facts, result=result)
    if unproven is not None:
        return unproven
    since = _max_ts(facts.updated_at, checkpoint.get("updated_at"))
    if facts.parent_task_id:
        parent_link = [_task_link(facts.parent_task_id, "Parent epic")]
        if facts.parent_status in TERMINAL_CHILD_STATUSES:
            return result(
                "not_tracked", "Completed outside the train",
                reason=f"{facts.parent_task_id} finished without collecting it; no train "
                "receipt records its delivery",
                since=since, evidence="untracked", extra=parent_link,
            )
        return result(
            "queued", "Waiting for parent collection",
            reason=f"Complete; {facts.parent_task_id} has not collected it yet",
            responsible=_ref("system", None, "Integration collector"),
            since=since, extra=parent_link,
        )
    if not _train_verified(checkpoint):
        return result(
            "not_tracked", "Completed outside the train",
            reason="No train verification or receipt records this epic's delivery",
            since=since, evidence="untracked",
        )
    return result(
        "queued", "Waiting for the next train",
        reason="Verified and complete; no train batch has picked it up yet",
        responsible=_ref("system", None, "Integration train"),
        since=since,
    )


#: Why git could not account for a completion, as the sentence a card shows.
#: The keys are :class:`~src.integration.delivery_truth.DeliveryState` reasons;
#: anything unlisted (an observer or snapshot failure) is named as it stands.
_UNPROVEN_REASONS = {
    "missing_git_provenance": (
        "no exact source is retained in git for this completion, so nothing can prove "
        "what {target} received from it"
    ),
    "scope_mismatch": "this work names another repository, so {target} cannot prove it",
    "invalid_parent_completion": (
        "no current verified parent completion binds this epic's head to a source"
    ),
    "parent_provenance_mismatch": (
        "the source retained in git differs from the verified parent completion"
    ),
    "missing_or_ambiguous_source": (
        "git could not locate an exact source for this completion"
    ),
    "missing_target": "{target} does not exist, so nothing can be delivered to it",
}


def _target_name(canonical: CanonicalDelivery) -> str:
    return str(canonical.target_ref or "the delivery target").removeprefix("refs/heads/")


def _canonical_delivered(facts: EpicFacts, *, result) -> dict | None:
    """Git's own delivered answer, or ``None`` when it has none.

    ``contained``, ``no_change`` and ``settled`` are canonical states that mean this
    epic's current work does not owe its target anything. Each names the exact
    identity they speak for — the verified parent completion's generation and
    source, or the leaf completion's retained source — and a recorded operator
    acceptance of a non-ancestor source is named, never silently absorbed.
    """
    canonical = facts.canonical
    if canonical is None or canonical.changed or canonical.state not in {
        "contained", "no_change", "settled",
    }:
        return None
    target = _target_name(canonical)
    if canonical.state == "settled":
        settled = canonical.settled_reason or canonical.reason.removeprefix("settled: ")
        return result(
            "delivered", "Delivered - not owed to this target",
            reason=f"Recorded as not owed to {target}: {settled}",
            since=canonical.since or facts.updated_at,
        )
    source = f"source {_short(canonical.source)}" if canonical.source else "its recorded source"
    if canonical.parent_generation is not None:
        detail = f"Verified generation {canonical.parent_generation} {source}"
    else:
        detail = f"Completion {_short(canonical.completion_id)} {source}"
    detail += f" is in {target}"
    if canonical.target_oid:
        detail += f" at {_short(canonical.target_oid)}"
    if canonical.acceptance == "operator_equivalent":
        detail += " (operator accepted this source as equivalent)"
    return result("delivered", "Delivered", reason=detail, since=canonical.since or facts.updated_at)


def _canonical_unproven(facts: EpicFacts, *, result) -> dict | None:
    """Replace a "nothing is owed" reading with what git actually says.

    It only speaks for a completed epic with nothing working on it, so an
    operator hold, an open gate, live integration work, an active train batch
    and a failing child all keep their ordinary statuses: this override
    applies exactly where the projection would otherwise conclude that
    delivery is settled by the absence of evidence.
    """
    canonical = facts.canonical
    if canonical is None or facts.status != "COMPLETED":
        return None
    if canonical.state == "pending":
        observed = f" as observed at {_short(canonical.target_oid)}" if canonical.target_oid else ""
        return result(
            "queued", "Delivery pending",
            reason=f"Complete; its {_source_phrase(canonical)} is not in "
            f"{_target_name(canonical)}{observed} yet",
            responsible=_ref("system", None, "Integration publisher"),
            since=canonical.since or facts.updated_at,
        )
    if canonical.state != "unknown":
        # ``no_artifact`` is an organizational container with nothing to prove.
        return None
    if canonical.changed:
        return result(
            "unknown", "Delivery evidence stale",
            reason="Its completion or delivery target changed after git was asked, so "
            "the earlier answer no longer describes this epic",
            since=facts.updated_at, evidence="stale",
        )
    reason = _unknown_reason(canonical, facts)
    return result(
        "unknown", "Delivery evidence unavailable", reason=reason,
        remedy=_unknown_remedy(canonical.reason, facts), since=facts.updated_at,
        evidence="unavailable",
    )


def _source_phrase(canonical: CanonicalDelivery) -> str:
    """What git locates for this completion, as a sentence fragment."""
    if canonical.parent_generation is not None:
        return f"verified generation {canonical.parent_generation} source {_short(canonical.source)}"
    if canonical.completion_id:
        return f"completion {_short(canonical.completion_id)} source {_short(canonical.source)}"
    return "recorded source"


def _unknown_reason(canonical: CanonicalDelivery, facts: EpicFacts) -> str:
    if canonical.reason == "snapshot_unavailable":
        return "Delivery evidence not loaded yet; a background delivery observation will refresh it"
    target = _target_name(canonical)
    template = _UNPROVEN_REASONS.get(canonical.reason)
    if template is not None:
        detail = template.format(target=target)
    elif canonical.reason.startswith(("observer_error", "snapshot_git_error")):
        detail = f"git could not be observed ({canonical.reason})"
    else:
        detail = f"git cannot account for this completion ({canonical.reason})"
    return (
        f"Delivery to {target} is unproven: {detail}; nothing is recorded that delivered "
        "it and nothing in this view can prove otherwise"
    )


def _unknown_remedy(reason: str, facts: EpicFacts) -> str | None:
    if reason == "missing_git_provenance" and facts.project_id:
        return f"aq task show {facts.task_id}"
    return None


def _train_verified(checkpoint: Mapping[str, Any]) -> bool:
    """The parent identity a train batch admits (``eligible_root_page_on``)."""
    return bool(
        checkpoint.get("verified_sha")
        and checkpoint.get("verified_sha") == checkpoint.get("checkpoint_sha")
        and checkpoint.get("verified_generation") == checkpoint.get("generation")
        and checkpoint.get("last_completed_operation_id")
    )


def _readiness_blockers(facts: EpicFacts, *, result) -> dict | None:
    readiness = facts.readiness or {}
    blockers = list(readiness.get("blockers") or [])
    if not blockers:
        return None
    checkpoint = facts.checkpoint or {}
    children = {child["id"]: child for child in facts.children}
    frozen = checkpoint.get("state") in {"verifying", "integration_ready"}
    missing = [
        children[item["task_id"]]
        for item in blockers
        if item.get("reason") == "receipt_missing"
        and children.get(item["task_id"], {}).get("status") == "COMPLETED"
    ]
    if missing:
        names = ", ".join(child["id"] for child in missing)
        child_links = [_task_link(child["id"], child.get("title") or child["id"]) for child in missing]
        since = _max_ts(*(child.get("updated_at") for child in missing))
        if frozen:
            return result(
                "blocked", "Integration blocked - final fix not collected",
                reason=f"{names} completed after the aggregate was frozen for verification; "
                "no delivery receipt collects it",
                responsible=_ref("operator", None, "Operator"), since=since,
                remedy=f"aq integration reopen-collection {facts.task_id}",
                extra=child_links,
            )
        return result(
            "queued", "Collecting child work",
            reason=f"Waiting for the integration collector to collect {names}",
            responsible=_ref("system", None, "Integration collector"), since=since,
            extra=child_links,
        )
    first = blockers[0]
    labels = {
        "receipt_chain": (
            "Integration blocked - receipt chain broken",
            "delivery receipts do not chain to the epic's head",
        ),
        "origin_mismatch": (
            "Integration blocked - child origin mismatch",
            "the child's branch origin does not match this epic's branch",
        ),
        "failed_aggregate_head_unchanged": (
            "Verification failed - no new fix collected",
            (
                "the last aggregate verification failed at this head; a fix must be collected "
                "before it is verified again"
            ),
        ),
        "failed_child": (
            "Integration blocked - failed child work",
            "a child failed and blocks collection",
        ),
    }
    label, detail = labels.get(
        str(first.get("reason")),
        (f"Integration blocked - {str(first.get('reason')).replace('_', ' ')}", "collection is blocked"),
    )
    target = str(first.get("task_id") or facts.task_id)
    return result(
        "blocked", label, reason=f"{target}: {detail}",
        responsible=_ref("operator", None, "Operator"),
        since=_max_ts(checkpoint.get("updated_at"), (children.get(target) or {}).get("updated_at")),
        extra=[_task_link(target, "Blocking task")] if target != facts.task_id else [],
    )


def _queued_or_excluded(
    worker: WorkerFacts, noun: str, facts: EpicFacts, *, result, since: float | None,
    extra: Iterable[dict] = (),
) -> dict:
    link = _task_link(worker.id, f"{noun} task {worker.id}")
    links = [link, *extra]
    if not worker.exclusions:
        if worker.profile_id is None:
            return result(
                "queued", f"{noun} queued - waiting for routing",
                reason=f"{worker.id} is READY but the router has not chosen its profile",
                responsible=_ref("system", None, "Router"), since=since, extra=links,
            )
        return result(
            "queued", f"{noun} queued - waiting for a worker",
            reason=f"{worker.id} is READY and claimable",
            responsible=_ref("system", None, "Worker pool"), since=since, extra=links,
        )
    owner = facts.owner
    if (
        "origin_not_materialized" in worker.exclusions
        and owner is not None
        and owner.get("owner_id") != worker.id
    ):
        holder = _holder_label(owner, facts.stage)
        return result(
            "blocked", f"{noun} blocked - branch handoff required",
            reason=f"{holder} still holds the branch reservation ({owner.get('handoff_state')}); "
            f"the {noun.lower()} task cannot claim the branch until it is handed off",
            responsible=_ref("task", owner.get("owner_id"), holder),
            since=_max_ts(since, owner.get("updated_at")),
            extra=[*links, _task_link(str(owner.get("owner_id")), f"Reservation holder {owner.get('owner_id')}")],
        )
    predicate = worker.exclusions[0]
    return result(
        "blocked", f"{noun} blocked - not claimable",
        reason=f"{worker.id}: {FRONTIER_PREDICATE_DETAILS.get(predicate, predicate)}",
        responsible=_ref("system", None, "Claim frontier"), since=since, extra=links,
    )


def _holder_label(owner: Mapping[str, Any], stage: Mapping[str, Any] | None) -> str:
    owner_id = owner.get("owner_id")
    if stage is not None and owner_id and owner_id == stage.get("repair_task_id"):
        return f"Repair stage {stage.get('ordinal')}"
    role = str(owner.get("owner_role") or "owner").replace("_", " ")
    return f"{role.capitalize()} {owner_id}"


def _verification_and_repair(facts: EpicFacts, *, result) -> dict:
    checkpoint = facts.checkpoint or {}
    operation = facts.operation or {}
    verifier = facts.verifier
    since_checkpoint = checkpoint.get("updated_at")
    if verifier is not None:
        since = _max_ts(verifier.updated_at, since_checkpoint)
        link = _task_link(verifier.id, f"Verifier task {verifier.id}")
        if verifier.status is None:
            return result(
                "unknown", "Delivery evidence unavailable",
                reason=f"Verifier task {verifier.id} is no longer in the queue",
                since=since_checkpoint, evidence="unavailable",
            )
        if verifier.status == "READY":
            return _queued_or_excluded(verifier, "Verification", facts, result=result, since=since)
        if verifier.status in {"ASSIGNED", "IN_PROGRESS", "WAITING_INPUT"}:
            return result(
                "unknown", "Verification status stale",
                reason=f"{verifier.id} is {verifier.status} but no live session holds it",
                since=since, evidence="stale", extra=[link],
            )
        if verifier.status in {"BLOCKED", "FAILED"}:
            detail = verifier.blocked_terminal or verifier.needs_attention or verifier.status
            return result(
                "blocked", "Verification blocked - verifier failed",
                reason=f"{verifier.id} is {verifier.status} ({str(detail).replace('_', ' ')})",
                responsible=_ref("task", verifier.id, f"Verifier {verifier.id}"),
                since=since, extra=[link],
            )
        if verifier.status == "COMPLETED":
            return result(
                "queued", "Recording verification result",
                reason=f"{verifier.id} closed; the collector has not recorded its result yet",
                responsible=_ref("system", None, "Integration collector"), since=since,
                extra=[link],
            )
        return result(
            "queued", "Waiting for verification to start",
            reason=f"{verifier.id} is {verifier.status}",
            responsible=_ref("system", None, "Integration collector"), since=since, extra=[link],
        )

    stage = facts.stage
    writer = facts.writer
    if writer is not None and writer.status == "READY":
        return _queued_or_excluded(
            writer, "Integration repair", facts, result=result,
            since=_max_ts(writer.updated_at, (stage or {}).get("started_at")),
        )
    if stage is not None and stage.get("state") in {"expired", "failed", "cancelled"}:
        return result(
            "blocked", f"Integration blocked - repair stage {stage.get('ordinal')} {stage.get('state')}",
            reason=f"Operation {_short(operation.get('id'))} is {operation.get('state')} and its "
            f"active repair stage {stage.get('state')} with nothing working on it",
            responsible=_ref("system", None, "Integration repair"),
            since=_max_ts(stage.get("completed_at"), operation.get("updated_at")),
            extra=[_task_link(writer.id, f"Repair task {writer.id}")] if writer is not None else [],
        )
    if checkpoint.get("state") == "awaiting_children":
        return result(
            "queued", "Collection complete - verification pending",
            reason="Every child is collected; the verifier has not been filed yet",
            responsible=_ref("system", None, "Integration collector"), since=since_checkpoint,
        )
    if operation.get("state") == "escalated":
        return result(
            "blocked", "Integration repair escalated",
            reason=f"Operation {_short(operation.get('id'))} escalated to stage "
            f"{operation.get('active_stage')} with nothing working on it",
            responsible=_ref("system", None, "Integration repair"),
            since=operation.get("updated_at"),
        )
    return result(
        "unknown", "Delivery state unrecognized",
        reason=f"Checkpoint {checkpoint.get('state')}, operation {operation.get('state')}",
        since=since_checkpoint, evidence="unavailable",
    )


def _current_head(checkpoint: Mapping[str, Any]) -> str | None:
    return checkpoint.get("verified_sha") or checkpoint.get("checkpoint_sha")


def unavailable_result(*, reason: str) -> dict[str, Any]:
    """The answer for an epic whose facts could not be read at all."""
    return {
        "state": "unknown",
        "label": "Delivery evidence unavailable",
        "display_status": "Delivery unknown",
        "hold": None,
        "reason": reason,
        "remedy": None,
        "responsible": None,
        "since": None,
        "links": [],
        "evidence": "unavailable",
        "implementation_completed": 0,
        "implementation_total": 0,
    }


# ---------------------------------------------------------------------------
# Fact gathering
# ---------------------------------------------------------------------------


def _canonical_from(evidence, *, acceptance: str | None = None) -> CanonicalDelivery:
    """One verified git answer, flattened for the classifier.

    The verified parent completion, when it is what the answer speaks for, is
    the identity to show: its generation and immutable source locate the exact
    work that ``load_delivery_requests`` bound to the current checkpoint.
    """
    request = evidence.request
    parent = request.parent_completion
    return CanonicalDelivery(
        state=str(evidence.state),
        reason=evidence.reason,
        source=evidence.source_oid or (parent.source_oid if parent else None),
        target_ref=request.target_ref,
        target_oid=evidence.target_oid,
        completion_id=request.completion_id,
        parent_generation=parent.generation if parent else None,
        acceptance=acceptance,
        settled_reason=request.settled_reason,
        since=request.completed_at,
    )


def _moved_canonical(view, task_id: str, epic: EpicFacts) -> CanonicalDelivery:
    """What an identity that changed after the observation is worth: nothing."""
    target = view.targets.get(task_id)
    oid = next(
        (
            snapshot.target_oid for snapshot in view.snapshots
            if target is not None
            and (snapshot.repository_id, snapshot.target_ref)
            == (target.repository_id, target.target_ref)
        ),
        None,
    )
    return CanonicalDelivery(
        state="unknown", reason="changed_during_observation",
        target_ref=target.target_ref if target is not None else None,
        target_oid=oid, changed=True, since=epic.updated_at,
    )


#: Newest recorded operator adoptions one read considers for their acceptance
#: wording. An adoption is an exceptional, reasoned act, so the newest of them
#: are the ones a card can still be describing.
ADOPTION_SCAN_LIMIT = 200


async def _recorded_acceptance_on(conn, facts: Mapping[str, EpicFacts], verified) -> dict[str, str]:
    """``operator_equivalent`` adoptions recorded for exactly this source.

    One indexed statement over the recorded adoptions of the projects this read
    covers, newest first, matched here against the sources git proved. It is
    wording, never the proof: git already established containment, and this
    names the operator's explicit equivalence acceptance when the recorded
    source was not an ancestor. A recorded operation can never turn pending or
    unknown work into delivered work, because only a satisfied answer is asked
    about here.
    """
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB

    wanted = {
        (task_id, evidence.source_oid)
        for task_id, evidence in verified.items()
        if evidence.satisfied and evidence.state != "no_artifact" and evidence.source_oid
    }
    if not wanted:
        return {}
    payload = cast(events.c.payload, JSONB)
    rows = (await conn.execute(
        select(events.c.payload).where(
            events.c.event_type == "development.operation",
            events.c.project_id.in_(sorted({
                facts[task_id].project_id for task_id, _source in wanted if facts[task_id].project_id
            })),
            payload["evidence"]["kind"].as_string() == "operator_accepted",
        ).order_by(events.c.id.desc()).limit(ADOPTION_SCAN_LIMIT)
    )).scalars()
    found: dict[str, str] = {}
    for raw in rows:
        try:
            record = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        for member in record.get("manifest") or []:
            if not isinstance(member, dict):
                continue
            pair = (member.get("task_id"), member.get("source_sha"))
            acceptance = member.get("acceptance")
            if pair in wanted and acceptance and pair[0] not in found:
                found[pair[0]] = str(acceptance)
    return found


class EpicDeliveryProjection:
    """Batched, read-only fact gathering for :func:`classify_epic_delivery`."""

    def __init__(
        self, db, *, clock=time.time, lease_ttl: float | None = None, delivery: Any = None,
        cached_only: bool = False,
    ) -> None:
        self.db = db
        self.clock = clock
        self.lease_ttl = float(SessionsConfig.lease_ttl_seconds if lease_ttl is None else lease_ttl)
        # Canonical git delivery truth, registered by the daemon
        # (:meth:`~src.database.queries.archive_queries.set_delivery_observer`).
        # Without one nothing is claimed about delivery beyond the train
        # evidence below, so an unobserved daemon keeps today's reading.
        self.delivery = delivery if delivery is not None else getattr(db, "_delivery_observer", None)
        self.cached_only = cached_only

    async def for_tasks(self, task_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Return ``{task_id: result}`` for every given task that has children.

        Never raises: a read failure reports each epic as ``unknown`` with
        ``evidence: unavailable`` so a graph or task read still succeeds.
        """
        ids = sorted({task_id for task_id in task_ids if task_id})
        if not ids:
            return {}
        try:
            async with self.db._engine.connect() as conn:
                facts = await self.facts_on(conn, ids)
        except Exception:  # a display read never fails its caller
            logger.warning("epic delivery projection failed for %d task(s)", len(ids), exc_info=True)
            return {}
        facts = await self._canonical_on(facts)
        now = self.clock()
        out: dict[str, dict[str, Any]] = {}
        for task_id, epic in facts.items():
            try:
                out[task_id] = classify_epic_delivery(epic, now=now, lease_ttl=self.lease_ttl)
            except Exception:  # one malformed row must not hide the rest
                logger.warning("epic delivery classification failed for %s", task_id, exc_info=True)
                out[task_id] = unavailable_result(reason="Delivery evidence could not be classified")
        return out

    async def _canonical_on(self, facts: dict[str, EpicFacts]) -> dict[str, EpicFacts]:
        """Add git's answer for each completed epic, asked outside every transaction.

        The observer fetches its own isolated stores and opens its own
        connections, so no publisher lock and no projection read is held
        across it. Each identity it evaluated is then rechecked on a fresh
        read (:meth:`DeliveryView.verified_on`); one that was reopened,
        re-completed or re-targeted meanwhile is reported stale rather than
        answered for the question it was not asked, and one git never
        evaluated at all (no designated repository) makes no claim here.

        A read-only surface may reuse a snapshot the observer fetched within
        its read window (:data:`DeliveryObserver.READ_MAX_AGE`), so a card can
        lag a just-landed delivery by that much; what it reports is always
        pinned to the exact target OID its reason names.

        The candidates are the COMPLETED epics of one read — the same bound
        :class:`~src.integration.status.IntegrationStatusService` puts on its
        own delivery read — so a graph response observes at most the epics it
        is already drawing.
        """
        observer = self.delivery
        if observer is None or not facts:
            return facts
        candidates = sorted(
            task_id for task_id, epic in facts.items() if epic.status == "COMPLETED"
        )
        if not candidates:
            return facts
        try:
            # A read-only surface, so a snapshot fetched within the observer's
            # read window is reused rather than refetched per poll; a guarded
            # writer is the only thing that must fetch.
            view = await observer.observe(
                candidates, max_age=getattr(observer, "READ_MAX_AGE", 0.0),
                **({"cached_only": True} if self.cached_only else {}),
            )
            async with self.db._engine.connect() as conn:
                verified = await view.verified_on(conn, candidates)
                acceptance = await _recorded_acceptance_on(conn, facts, verified)
        except Exception:  # no canonical answer is still a usable answer
            logger.warning(
                "epic delivery observation failed for %d epic(s)", len(candidates), exc_info=True
            )
            return facts
        out = dict(facts)
        for task_id in candidates:
            evidence = verified.get(task_id)
            if evidence is None:
                if view.get(task_id) is None:
                    continue
                canonical = _moved_canonical(view, task_id, facts[task_id])
            else:
                canonical = _canonical_from(evidence, acceptance=acceptance.get(task_id))
            if canonical.state == "no_artifact":
                continue  # an organizational container has nothing to prove
            out[task_id] = replace(facts[task_id], canonical=canonical)
        return out

    async def facts_on(self, conn, ids: list[str]) -> dict[str, EpicFacts]:
        # One statement for every fact an untracked project needs: the
        # children, the operator holds and the open approval gates come back
        # as aggregates of each row, so a graph response over a project
        # without train evidence pays a single extra round trip.
        parent = tasks.alias("epic_delivery_parent")
        child = tasks.alias("epic_delivery_child")
        children_json = (
            select(
                func.json_agg(
                    aggregate_order_by(
                        func.json_build_object(
                            "id", child.c.id,
                            "status", child.c.status,
                            "updated_at", child.c.updated_at,
                            "title", child.c.title,
                        ),
                        child.c.id,
                    ),
                    type_=JSON,
                )
            )
            .where(child.c.parent_task_id == tasks.c.id)
            .scalar_subquery()
        )
        manual_hold = exists(
            select(literal(1)).where(
                task_metadata.c.task_id == tasks.c.id, task_metadata.c.key == "manual_pause"
            )
        )
        hold_labels = (
            select(
                func.array_agg(aggregate_order_by(task_labels.c.label, task_labels.c.label))
            )
            .where(task_labels.c.task_id == tasks.c.id, task_labels.c.label.like("hold:%"))
            .scalar_subquery()
        )
        approval_gates = (
            select(
                func.json_agg(
                    aggregate_order_by(
                        func.json_build_object(
                            "id", gates.c.id,
                            "title", gates.c.title,
                            "question", gates.c.question,
                            "created_at", gates.c.created_at,
                        ),
                        gates.c.created_at,
                        gates.c.id,
                    ),
                    type_=JSON,
                )
            )
            .select_from(task_gates.join(gates, gates.c.id == task_gates.c.gate_id))
            .where(
                task_gates.c.task_id == tasks.c.id,
                gates.c.status == "open",
                gates.c.gate_type.in_(sorted(APPROVAL_GATE_TYPES)),
            )
            .scalar_subquery()
        )
        rows = {
            row["id"]: dict(row)
            for row in (
                await conn.execute(
                    select(
                        tasks.c.id, tasks.c.status, tasks.c.updated_at, tasks.c.resume_after,
                        tasks.c.parent_task_id, tasks.c.project_id,
                        parent.c.status.label("parent_status"),
                        projects.c.hierarchical_integration_mode.label("mode"),
                        projects.c.integration_repository_id,
                        children_json.label("children"),
                        manual_hold.label("manual_hold"),
                        hold_labels.label("hold_labels"),
                        approval_gates.label("approval_gates"),
                    )
                    .select_from(
                        tasks.join(projects, projects.c.id == tasks.c.project_id).outerjoin(
                            parent, parent.c.id == tasks.c.parent_task_id
                        )
                    )
                    .where(tasks.c.id.in_(ids))
                )
            ).mappings()
            if row["children"]
        }
        if not rows:
            return {}
        epic_ids = list(rows)
        children = {task_id: list(row["children"]) for task_id, row in rows.items()}

        tracked = [task_id for task_id in epic_ids if rows[task_id]["mode"] in TRACKED_MODES]
        tracked_data = await self._tracked_facts_on(conn, tracked, rows, children) if tracked else {}

        out: dict[str, EpicFacts] = {}
        for task_id in epic_ids:
            row = rows[task_id]
            out[task_id] = EpicFacts(
                task_id=task_id,
                status=row["status"],
                updated_at=row["updated_at"],
                resume_after=row["resume_after"],
                project_id=row["project_id"],
                parent_task_id=row["parent_task_id"],
                parent_status=row["parent_status"],
                mode=row["mode"] or "disabled",
                children=tuple(children[task_id]),
                manual_hold=bool(row["manual_hold"]),
                hold_labels=tuple(row["hold_labels"] or ()),
                approval_gates=tuple(row["approval_gates"] or ()),
                **tracked_data.get(task_id, {}),
            )
        return out

    async def _tracked_facts_on(self, conn, ids, rows, children) -> dict[str, dict[str, Any]]:
        checkpoints = {
            row["task_id"]: dict(row)
            for row in (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id.in_(ids)
                    )
                )
            ).mappings()
        }
        parent_operations: dict[str, list[dict[str, Any]]] = {}
        for row in (
            await conn.execute(
                select(*_OPERATION_COLUMNS).where(
                    integration_repair_operations.c.parent_task_id.in_(ids)
                ).order_by(integration_repair_operations.c.created_at)
            )
        ).mappings():
            parent_operations.setdefault(row["parent_task_id"], []).append(dict(row))

        # Root delivery: the same receipt rules eligibility and dependency
        # satisfaction use, against each epic's designated repository.
        receipts: dict[str, list[dict[str, Any]]] = {}
        for row in (
            await conn.execute(
                select(task_delivery_receipts)
                .select_from(
                    task_delivery_receipts.join(
                        tasks, tasks.c.id == task_delivery_receipts.c.source_task_id
                    )
                    .join(projects, projects.c.id == tasks.c.project_id)
                    .join(repos, repos.c.id == projects.c.integration_repository_id)
                )
                .where(
                    task_delivery_receipts.c.source_task_id.in_(ids),
                    *_root_delivery_receipt_conditions(
                        projects.c.integration_repository_id, repos.c.default_branch
                    ),
                )
                .order_by(task_delivery_receipts.c.created_at.desc(), task_delivery_receipts.c.id)
            )
        ).mappings():
            receipts.setdefault(row["source_task_id"], []).append(dict(row))
        nested = [task_id for task_id in ids if rows[task_id]["parent_task_id"]]
        if nested:
            for row in (
                await conn.execute(
                    select(task_delivery_receipts)
                    .where(
                        task_delivery_receipts.c.source_task_id.in_(nested),
                        task_delivery_receipts.c.target_task_id.is_not(None),
                        task_delivery_receipts.c.disposition.in_(("code", "noop")),
                    )
                    .order_by(task_delivery_receipts.c.created_at.desc(), task_delivery_receipts.c.id)
                )
            ).mappings():
                if row["target_task_id"] == rows[row["source_task_id"]]["parent_task_id"]:
                    receipts.setdefault(row["source_task_id"], []).append(dict(row))

        batches: dict[str, dict[str, Any]] = {}
        for row in (
            await conn.execute(
                select(
                    integration_batch_members.c.task_id, integration_batches.c.id,
                    integration_batches.c.lifecycle, integration_batches.c.updated_at,
                )
                .select_from(
                    integration_batch_members.join(
                        integration_batches,
                        integration_batches.c.id == integration_batch_members.c.batch_id,
                    )
                )
                .where(
                    integration_batch_members.c.task_id.in_(ids),
                    integration_batches.c.lifecycle.in_(_ACTIVE_BATCH_LIFECYCLES),
                )
                .order_by(integration_batches.c.created_at.desc())
            )
        ).mappings():
            batches.setdefault(row["task_id"], dict(row))
        batch_operations: dict[str, dict[str, Any]] = {}
        if batches:
            for row in (
                await conn.execute(
                    select(*_OPERATION_COLUMNS).where(
                        integration_repair_operations.c.batch_id.in_(
                            sorted({batch["id"] for batch in batches.values()})
                        ),
                        integration_repair_operations.c.state.in_(ACTIVE_OPERATION_STATES),
                    )
                )
            ).mappings():
                batch_operations[row["batch_id"]] = dict(row)

        # The current episode's operation per epic, live or cancelled.
        live: dict[str, dict[str, Any]] = {}
        cancelled: set[str] = set()
        for task_id in ids:
            checkpoint = checkpoints.get(task_id)
            episode = checkpoint.get("episode_id") if checkpoint else None
            for operation in parent_operations.get(task_id, []):
                if episode is not None and operation["episode_id"] != episode:
                    continue
                if operation["state"] in ACTIVE_OPERATION_STATES:
                    live[task_id] = operation
                elif operation["state"] == "cancelled" and episode is not None:
                    cancelled.add(task_id)
        operations = [*live.values(), *batch_operations.values()]
        stages = {
            (row["operation_id"], row["ordinal"]): dict(row)
            for row in (
                await conn.execute(
                    select(*_STAGE_COLUMNS).where(
                        integration_repair_stages.c.operation_id.in_(
                            [operation["id"] for operation in operations]
                        )
                    )
                )
            ).mappings()
        } if operations else {}

        keys = [
            (checkpoint["repository_id"], checkpoint["branch"])
            for checkpoint in checkpoints.values()
            if checkpoint.get("repository_id") and checkpoint.get("branch")
        ]
        owners: dict[tuple[str, str], dict[str, Any]] = {}
        if keys:
            for row in (
                await conn.execute(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.ref.in_(sorted({ref for _repo, ref in keys})),
                        integration_branch_owners.c.handoff_state != "released",
                    )
                )
            ).mappings():
                owners[(row["repository_id"], row["ref"])] = dict(row)

        def active_stage(operation):
            if operation is None:
                return None
            return stages.get((operation["id"], operation["active_stage"]))

        worker_ids = set()
        for operation in operations:
            if operation.get("verifier_task_id"):
                worker_ids.add(operation["verifier_task_id"])
            stage = active_stage(operation)
            if stage is not None and stage.get("repair_task_id"):
                worker_ids.add(stage["repair_task_id"])
        workers = await self._workers_on(conn, sorted(worker_ids)) if worker_ids else {}

        readiness: dict[str, dict[str, Any]] = {}
        readiness_errors: dict[str, str] = {}
        evaluated = 0
        for task_id in ids:
            checkpoint = checkpoints.get(task_id)
            operation = live.get(task_id)
            kids = children.get(task_id, [])
            if (
                checkpoint is None
                or operation is None
                or checkpoint.get("episode_id") is None
                or any(child["status"] not in TERMINAL_CHILD_STATUSES for child in kids)
            ):
                continue
            if evaluated >= MAX_READINESS_PER_READ:
                readiness_errors[task_id] = "Readiness was not evaluated: too many epics in one read"
                continue
            evaluated += 1
            try:
                # A savepoint keeps one epic's failed read from aborting the
                # transaction every later statement in this response shares.
                async with conn.begin_nested():
                    readiness[task_id] = await ParentEpisodeRecords(self.db).readiness_on(
                        conn,
                        parent=rows[task_id],
                        project={},
                        checkpoint=checkpoint,
                        operation=operation,
                    )
            except Exception as exc:  # reported on that epic only
                logger.warning("epic readiness read failed for %s", task_id, exc_info=True)
                readiness_errors[task_id] = f"Collection readiness could not be read: {exc}"

        out: dict[str, dict[str, Any]] = {}
        for task_id in ids:
            checkpoint = checkpoints.get(task_id)
            operation = live.get(task_id)
            stage = active_stage(operation)
            batch = batches.get(task_id)
            batch_operation = batch_operations.get(batch["id"]) if batch else None
            batch_stage = active_stage(batch_operation)
            receipt, stale = _select_receipt(receipts.get(task_id, []), checkpoint)
            owner = (
                owners.get((checkpoint["repository_id"], checkpoint["branch"]))
                if checkpoint else None
            )
            out[task_id] = {
                "checkpoint": checkpoint,
                "operation": operation,
                "collection_cancelled": task_id in cancelled and operation is None,
                "stage": stage,
                "owner": owner,
                "verifier": workers.get(operation.get("verifier_task_id")) if operation else None,
                "writer": workers.get(stage.get("repair_task_id")) if stage else None,
                "readiness": readiness.get(task_id),
                "readiness_error": readiness_errors.get(task_id),
                "receipt": receipt,
                "stale_receipt": stale,
                "batch": batch,
                "batch_operation": batch_operation,
                "batch_writer": (
                    workers.get(batch_stage.get("repair_task_id")) if batch_stage else None
                ),
            }
        return out

    async def _workers_on(self, conn, worker_ids: list[str]) -> dict[str, WorkerFacts]:
        found = {
            row["id"]: dict(row)
            for row in (
                await conn.execute(
                    select(
                        tasks.c.id, tasks.c.status, tasks.c.updated_at, tasks.c.profile_id,
                    ).where(tasks.c.id.in_(worker_ids))
                )
            ).mappings()
        }
        meta: dict[str, dict[str, Any]] = {}
        for task_id, key, value in (
            await conn.execute(
                select(task_metadata.c.task_id, task_metadata.c.key, task_metadata.c.value).where(
                    task_metadata.c.task_id.in_(worker_ids),
                    task_metadata.c.key.in_((TERMINAL_BLOCKED_META_KEY, "needs_attention")),
                )
            )
        ).all():
            meta.setdefault(task_id, {})[key] = _decode(value)
        live_sessions: dict[str, dict[str, Any]] = {}
        for row in (
            await conn.execute(
                select(
                    sessions.c.id, sessions.c.task_id, sessions.c.state, sessions.c.profile_id,
                    sessions.c.started_at, sessions.c.last_activity,
                )
                .where(
                    sessions.c.task_id.in_(worker_ids),
                    sessions.c.state.in_(LIVE_SESSION_STATES),
                    sessions.c.ended_at.is_(None),
                )
                .order_by(sessions.c.started_at.desc())
            )
        ).mappings():
            live_sessions.setdefault(row["task_id"], dict(row))
        ready = [task_id for task_id, row in found.items() if row["status"] == "READY"]
        exclusions: dict[str, tuple[str, ...]] = {}
        if ready:
            predicates = claim_frontier_predicates()
            for row in (
                await conn.execute(
                    select(
                        tasks.c.id, *(predicate.label(name) for name, predicate in predicates.items())
                    ).where(tasks.c.id.in_(ready))
                )
            ).mappings():
                exclusions[row["id"]] = tuple(name for name in predicates if not row[name])
        out: dict[str, WorkerFacts] = {}
        for task_id in worker_ids:
            row = found.get(task_id)
            if row is None:
                out[task_id] = WorkerFacts(id=task_id)
                continue
            out[task_id] = WorkerFacts(
                id=task_id,
                status=row["status"],
                updated_at=row["updated_at"],
                profile_id=row["profile_id"],
                session=live_sessions.get(task_id),
                blocked_terminal=_text(meta.get(task_id, {}).get(TERMINAL_BLOCKED_META_KEY)),
                needs_attention=_text(meta.get(task_id, {}).get("needs_attention")),
                exclusions=exclusions.get(task_id, ()),
            )
        return out


def _select_receipt(
    candidates: list[dict[str, Any]], checkpoint: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(current, stale)``: a receipt binding the current head, else the newest other."""
    if not candidates:
        return None, None
    heads = {
        head for head in (
            (checkpoint or {}).get("verified_sha"), (checkpoint or {}).get("checkpoint_sha"),
        ) if head
    }
    for receipt in candidates:
        if receipt.get("reviewed_head_sha") in heads:
            return receipt, None
    return None, candidates[0]


def _decode(value: Any) -> Any:
    import json

    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _text(value: Any) -> str | None:
    if value is None or value is False:
        return None
    return value if isinstance(value, str) else str(value)
