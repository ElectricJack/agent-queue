"""Playbook V2 durable run state — snapshots, receipts (child plan §4.4).

Every durable advance of a V2 run is **one** ``immediate()`` block containing
a compare-and-set on ``playbook_v2_runs.snapshot_version`` and the insert of
exactly one receipt.  Two properties fall out of that shape and neither is
achievable with the V1 mixin's unconditional ``UPDATE``:

* a resume that raced another writer loses the CAS and raises rather than
  silently interleaving (V1's ``update_playbook_run`` is last-writer-wins);
* a replayed boundary after an ambiguous interruption is rejected by
  ``uq_playbook_step_receipts_boundary`` — by the database, not by an
  in-memory guard that a restart would have forgotten.

Size limits are checked *before* the transaction opens, so an oversized
payload never reaches the database at all (§8.2: reject, never externalize,
never truncate).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    playbook_artifacts,
    playbook_pending_events,
    playbook_step_receipts,
    playbook_v2_runs,
    playbook_waits,
)
from src.playbooks.receipts import StepReceipt
from src.playbooks.run_state import (
    DEFAULT_STATE_LIMITS,
    TERMINAL_LIFECYCLES,
    DuplicateAttempt,
    DuplicateRun,
    DuplicateWait,
    IllegalLifecycleTransition,
    PendingEventIntegrityError,
    PendingEventQuotaExceeded,
    RunIdentityMismatch,
    RunLifecycle,
    RunSnapshot,
    SnapshotVersionConflict,
    StateLimits,
    WaitOwnershipViolation,
    WaitVersionMismatch,
    check_result_size,
    deserialize_snapshot,
    serialize_snapshot,
    validate_transition,
)
from src.playbooks.waits import (
    EMPTY_WAIT_CHANGES,
    EVENT_ADDRESSABLE_WAIT_KINDS,
    MatchableEvent,
    WaitChangeSet,
    WaitClaim,
    WaitRegistration,
    WaitSpec,
    matches,
)

logger = logging.getLogger(__name__)


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _receipt_row(receipt: StepReceipt) -> dict:
    return {
        "receipt_id": receipt.receipt_id,
        "run_id": receipt.run_id,
        "artifact_sha256": receipt.artifact_sha256,
        "rule_id": receipt.rule_id,
        "step_id": receipt.step_id,
        "step_kind": receipt.step_kind,
        "receipt_kind": receipt.receipt_kind,
        "turn_index": receipt.turn_index,
        "operator_decision_id": receipt.operator_decision_id,
        "iteration": receipt.iteration,
        "attempt": receipt.attempt,
        "idempotency_key": receipt.idempotency_key,
        "snapshot_version": receipt.snapshot_version,
        "contract_fingerprint": receipt.contract_fingerprint,
        "principal": _dumps(dict(receipt.principal)),
        "inputs": _dumps(dict(receipt.inputs)),
        "result": _dumps(dict(receipt.result)),
        "outcome": receipt.outcome,
        "selected_transition": receipt.selected_transition,
        "error": receipt.error,
        "error_code": receipt.error_code,
        "tokens_in": receipt.tokens_in,
        "tokens_out": receipt.tokens_out,
        "cost_usd": receipt.cost_usd,
        "wait_id": receipt.wait_id,
        "timed_out": receipt.timed_out,
        "cancelled_at": receipt.cancelled_at,
        "started_at": receipt.started_at,
        "completed_at": receipt.completed_at,
        "duration_ms": receipt.duration_ms,
    }


#: ``playbooks.v2_max_pending_events_per_playbook`` when no config is bound.
DEFAULT_PENDING_EVENT_QUOTA = 1000

# ``resolved_by`` is an audit field, not an operator-only field: expiry is a
# deterministic system decision and must identify the component that made it.
PENDING_EVENT_EXPIRY_ACTOR = "retention_sweep"

# Inbox rows are resolved immediately because they have already entered the
# wait-delivery path.  They remain durable for the normal pending-event
# retention horizon so a wait whose registration transaction was in flight at
# arrival can still find them after it acquires the delivery lock.
WAIT_EVENT_REASON = "wait_registration"
WAIT_EVENT_RESOLVER = "wait_repository"
WAIT_EVENT_TTL_SECONDS = 7 * 86_400.0


@dataclass(frozen=True, slots=True)
class PendingEventPurge:
    """The two distinct actions performed by a pending-event sweep."""

    expired: int
    purged: int

#: One quota warning per playbook per minute — a flood is exactly the case
#: where an unthrottled warning turns a full retention table into a full disk.
_QUOTA_WARNING_INTERVAL = 60.0
_last_quota_warning: dict[str, float] = {}


def _warn_pending_quota(playbook_id: str, quota: int, now: float) -> None:
    last = _last_quota_warning.get(playbook_id)
    if last is not None and now - last < _QUOTA_WARNING_INTERVAL:
        return
    _last_quota_warning[playbook_id] = now
    logger.warning(
        "playbook %s is at its pending-event retention quota (%d); "
        "further retained events are refused until rows are resolved or purged",
        playbook_id,
        quota,
    )


def _wait_row(wait: WaitSpec, snapshot_version: int) -> dict:
    return {
        "wait_id": wait.wait_id,
        "run_id": wait.run_id,
        "step_id": wait.step_id,
        "iteration": wait.iteration,
        "kind": wait.kind,
        "event_type": wait.event_type,
        "correlation_key": wait.correlation_key,
        "match": _dumps(dict(wait.match)),
        "deadline_at": wait.deadline_at,
        "snapshot_version": snapshot_version,
        "state": "active",
        "claimed_event_id": None,
        "claimed_at": None,
        # This is when the executor decided to wait, not when its transaction
        # happened to reach the INSERT.  Inbox events older than this wait
        # must not satisfy a newly-created suspension.
        "created_at": wait.created_at,
    }


def _row_to_wait(row) -> WaitSpec:
    return WaitSpec(
        wait_id=row["wait_id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        iteration=int(row["iteration"]),
        kind=row["kind"],
        event_type=row["event_type"],
        match=json.loads(row["match"]),
        deadline_at=row["deadline_at"],
        created_at=float(row["created_at"]),
    )


def _row_to_claim(
    row, event: _InboxEvent | None, now: float, *, expired: bool
) -> WaitClaim:
    return WaitClaim(
        wait_id=row["wait_id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        iteration=int(row["iteration"]),
        kind=row["kind"],
        snapshot_version=int(row["snapshot_version"]),
        claimed_event_id=event.event_id if event is not None else None,
        claimed_at=now,
        expired=expired,
        event_type=event.event_type if event is not None else "",
        event_fields=dict(event.fields) if event is not None else {},
    )


def _row_to_pending_event(row) -> dict[str, Any]:
    """Field-for-field ``PendingEventDTO`` (§4.6) plus the audit columns."""
    return {
        "pending_event_id": row["pending_event_id"],
        "playbook_id": row["playbook_id"],
        "scope": row["scope"],
        "scope_identifier": row["scope_identifier"],
        "event_type": row["event_type"],
        "event": json.loads(row["event"]),
        "event_id": row["event_id"],
        "dedup_key": row["dedup_key"],
        "reason": row["reason"],
        "attempts": int(row["attempts"]),
        "last_error": row["last_error"],
        "received_at": row["received_at"],
        "expires_at": row["expires_at"],
        "resolved_at": row["resolved_at"],
        "resolved_by": row["resolved_by"],
        "resolution": row["resolution"],
    }


@dataclass(frozen=True, slots=True)
class _InboxEvent:
    """Canonical stored event used for every wait-matching decision."""

    pending_event_id: str
    playbook_id: str
    scope: str
    scope_identifier: str
    event_type: str
    event_id: str | None
    fields: Mapping[str, Any]
    received_at: float


def _row_to_inbox_event(row) -> _InboxEvent:
    return _InboxEvent(
        pending_event_id=row["pending_event_id"],
        playbook_id=row["playbook_id"],
        scope=row["scope"],
        scope_identifier=row["scope_identifier"],
        event_type=row["event_type"],
        event_id=row["event_id"],
        fields=json.loads(row["event"]),
        received_at=float(row["received_at"]),
    )


def _row_to_receipt(row) -> StepReceipt:
    return StepReceipt(
        receipt_id=row["receipt_id"],
        run_id=row["run_id"],
        artifact_sha256=row["artifact_sha256"],
        rule_id=row["rule_id"],
        step_id=row["step_id"],
        step_kind=row["step_kind"],
        receipt_kind=row["receipt_kind"],
        turn_index=int(row["turn_index"]),
        operator_decision_id=row["operator_decision_id"],
        outcome=row["outcome"],
        started_at=row["started_at"],
        snapshot_version=int(row["snapshot_version"]),
        iteration=int(row["iteration"]),
        attempt=int(row["attempt"]),
        idempotency_key=row["idempotency_key"],
        contract_fingerprint=row["contract_fingerprint"],
        principal=json.loads(row["principal"]),
        inputs=json.loads(row["inputs"]),
        result=json.loads(row["result"]),
        selected_transition=row["selected_transition"],
        error=row["error"],
        error_code=row["error_code"],
        tokens_in=int(row["tokens_in"]),
        tokens_out=int(row["tokens_out"]),
        cost_usd=row["cost_usd"],
        wait_id=row["wait_id"],
        timed_out=bool(row["timed_out"]),
        cancelled_at=row["cancelled_at"],
        completed_at=row["completed_at"],
        duration_ms=int(row["duration_ms"]),
    )


def _run_columns(snapshot: RunSnapshot, payload: bytes) -> dict:
    """The indexed projection of a snapshot.

    Every one of these is also inside ``snapshot``; they are duplicated into
    columns so an operator query ("which runs are paused on this artifact?")
    is an index scan rather than a JSON parse of every row.
    """
    return {
        "playbook_id": snapshot.playbook_id,
        "artifact_sha256": snapshot.artifact_sha256,
        "rule_id": snapshot.rule_id,
        "lifecycle": snapshot.lifecycle.value,
        "mode": snapshot.mode,
        "current_step_id": snapshot.current_step_id,
        "snapshot": payload.decode("utf-8"),
        "snapshot_bytes": len(payload),
        "event_type": snapshot.event_type,
        "event_id": snapshot.event_id,
        "dispatch_id": snapshot.dispatch_id,
        "parent_run_id": snapshot.parent_run_id,
        "parent_step_id": snapshot.parent_step_id,
        "deadline_at": snapshot.deadline_at,
        "cancel_requested_at": snapshot.cancel_requested_at,
        "summary": snapshot.summary,
        "error": snapshot.error,
        "error_code": snapshot.error_code,
        "updated_at": snapshot.updated_at,
        "completed_at": snapshot.completed_at,
    }


class PlaybookRunQueryMixin:
    """Implements ``RunRepository`` (and, from §4.5, ``WaitRepository``).

    One mixin rather than two because ``commit_boundary`` must apply wait
    changes on **its own** connection — splitting them would put the
    registration in a second transaction and reopen the event-arrival race
    this package exists to close.

    Expects ``self._engine`` and ``TransactionQueryMixin.immediate``.
    """

    def playbook_state_limits(self) -> StateLimits:
        """The configured size caps, or the module defaults."""
        return getattr(self, "_playbook_state_limits", None) or DEFAULT_STATE_LIMITS

    def set_playbook_state_limits(self, limits: StateLimits) -> None:
        self._playbook_state_limits = limits

    # -- runs ---------------------------------------------------------------

    async def create_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        payload = serialize_snapshot(snapshot, limits=self.playbook_state_limits())
        values = {
            "run_id": snapshot.run_id,
            "snapshot_version": snapshot.version,
            "started_at": snapshot.started_at,
            **_run_columns(snapshot, payload),
        }
        try:
            async with self.immediate() as conn:
                await conn.execute(insert(playbook_v2_runs).values(**values))
        except IntegrityError as exc:
            # ``uq_playbook_v2_runs_dispatch_rule``: one matching event
            # creates at most one run per rule.  Named rather than left as a
            # driver-specific message so the engine can report the existing
            # run as deduplicated instead of failing the dispatch.
            if snapshot.dispatch_id:
                raise DuplicateRun(snapshot.dispatch_id, snapshot.rule_id) from exc
            raise
        return snapshot

    async def find_run_for_dispatch(
        self, dispatch_id: str, rule_id: str
    ) -> RunSnapshot | None:
        """The run this (dispatch, rule) pair already has, if any."""
        stmt = select(playbook_v2_runs).where(
            playbook_v2_runs.c.dispatch_id == dispatch_id,
            playbook_v2_runs.c.rule_id == rule_id,
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).mappings().fetchone()
        if row is None:
            return None
        return deserialize_snapshot(row["snapshot"], version=int(row["snapshot_version"]))

    async def load_run(self, run_id: str) -> RunSnapshot | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(playbook_v2_runs).where(playbook_v2_runs.c.run_id == run_id)
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return deserialize_snapshot(row["snapshot"], version=int(row["snapshot_version"]))

    async def commit_boundary(
        self,
        snapshot: RunSnapshot,
        receipt: StepReceipt,
        wait_changes: WaitChangeSet = EMPTY_WAIT_CHANGES,
    ) -> RunSnapshot:
        """Advance one run by exactly one durable step.

        ``snapshot`` carries the *new* state with ``version`` still at the
        value it was loaded with; that value is the CAS expectation and the
        returned object is the one callers must keep.
        """
        limits = self.playbook_state_limits()
        # Before the transaction, so an oversized payload never reaches the
        # database and a limit breach costs nothing to roll back.
        check_result_size(snapshot.run_id, receipt.step_id, dict(receipt.result), limits=limits)
        # ``receipt.result`` is deliberately a compact/redacted audit
        # projection.  The actual step result is durable in the snapshot
        # bindings, so validate each binding independently rather than
        # trusting a caller to have used ``bind_step_output``.
        for step_id, value in snapshot.bindings.items():
            check_result_size(snapshot.run_id, step_id, value, limits=limits)
        for turn in snapshot.llm_turns:
            for message in turn.get("transcript_delta", ()):
                if message.get("role") != "user" or not isinstance(
                    message.get("content"), list
                ):
                    continue
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        check_result_size(
                            snapshot.run_id,
                            receipt.step_id,
                            block.get("content"),
                            limits=limits,
                        )
        expected = snapshot.version
        advanced = replace(snapshot, version=expected + 1)
        # The receipt is the durable record of *which* graph and rule ran, so
        # it has to name the same ones the snapshot does and the version this
        # boundary is writing.  Checked before the transaction opens: a
        # drifting receipt never reaches the insert.
        for field, want, got in (
            ("receipt.run_id", snapshot.run_id, receipt.run_id),
            ("receipt.artifact_sha256", snapshot.artifact_sha256, receipt.artifact_sha256),
            ("receipt.rule_id", snapshot.rule_id, receipt.rule_id),
            ("receipt.snapshot_version", advanced.version, receipt.snapshot_version),
        ):
            if want != got:
                raise RunIdentityMismatch(snapshot.run_id, field, want, got)
        payload = serialize_snapshot(advanced, limits=limits)

        async with self.immediate() as conn:
            current = (
                (
                    await conn.execute(
                        select(
                            playbook_v2_runs.c.lifecycle,
                            playbook_v2_runs.c.snapshot_version,
                            playbook_v2_runs.c.playbook_id,
                            playbook_v2_runs.c.artifact_sha256,
                            playbook_v2_runs.c.rule_id,
                        ).where(playbook_v2_runs.c.run_id == snapshot.run_id)
                    )
                )
                .mappings()
                .fetchone()
            )
            if current is None:
                raise SnapshotVersionConflict(snapshot.run_id, expected, None)
            # The CAS fences *when* a run advances; it says nothing about
            # what it advances into.  Without this, a same-version snapshot
            # could repoint a live run at another playbook's artifact and the
            # run's history would render against a graph it never ran.
            for field, want, got in (
                ("playbook_id", current["playbook_id"], snapshot.playbook_id),
                ("artifact_sha256", current["artifact_sha256"], snapshot.artifact_sha256),
                ("rule_id", current["rule_id"], snapshot.rule_id),
            ):
                if want != got:
                    raise RunIdentityMismatch(snapshot.run_id, field, want, got)
            validate_transition(
                snapshot.run_id, RunLifecycle(current["lifecycle"]), advanced.lifecycle
            )
            result = await conn.execute(
                update(playbook_v2_runs)
                .where(
                    playbook_v2_runs.c.run_id == snapshot.run_id,
                    playbook_v2_runs.c.snapshot_version == expected,
                )
                .values(snapshot_version=advanced.version, **_run_columns(advanced, payload))
            )
            if result.rowcount != 1:
                raise SnapshotVersionConflict(
                    snapshot.run_id, expected, int(current["snapshot_version"])
                )
            try:
                await conn.execute(insert(playbook_step_receipts).values(**_receipt_row(receipt)))
            except IntegrityError as exc:
                raise DuplicateAttempt(
                    receipt.run_id, receipt.step_id, receipt.iteration, receipt.attempt
                ) from exc
            immediate_claims = await self._apply_wait_changes(
                conn, snapshot.run_id, wait_changes, advanced.version
            )
            if immediate_claims:
                # Registration runs inside this boundary, so its result must
                # cross the same durable seam.  Otherwise the wait can be
                # marked claimed while the caller receives only a paused
                # snapshot and has no event with which to resume.  Package 4
                # consumes and clears these claims on its next boundary.
                advanced = replace(
                    advanced,
                    pending_wait_claims=advanced.pending_wait_claims + immediate_claims,
                )
                payload = serialize_snapshot(advanced, limits=limits)
                await conn.execute(
                    update(playbook_v2_runs)
                    .where(
                        playbook_v2_runs.c.run_id == snapshot.run_id,
                        playbook_v2_runs.c.snapshot_version == advanced.version,
                    )
                    .values(**_run_columns(advanced, payload))
                )
        return advanced

    async def _apply_wait_changes(
        self, conn: AsyncConnection, run_id: str, wait_changes: WaitChangeSet, version: int
    ) -> tuple[WaitClaim, ...]:
        """Applied on the boundary's own connection — see §4.5.

        Order is ``clear_run_waits`` → ``clear_wait_ids`` → ``register``, so a
        step that finishes one wait and opens another in the same boundary
        cannot trip ``uq_playbook_waits_active_step``.

        Every change is also scoped to ``run_id``: this connection holds only
        this run's CAS, so a change set naming another run's wait would move
        that run's suspension outside its own fence.
        """
        if wait_changes.is_empty:
            return ()
        # Up front, before any write: an ownership breach is a caller bug, not
        # a race, and it costs nothing to refuse it before the first UPDATE.
        for wait in wait_changes.register:
            if wait.run_id != run_id:
                raise WaitOwnershipViolation(run_id, wait.wait_id, wait.run_id)
        wait_ids = list(wait_changes.clear_wait_ids)
        if wait_ids:
            foreign = (
                (
                    await conn.execute(
                        select(playbook_waits.c.wait_id, playbook_waits.c.run_id).where(
                            playbook_waits.c.wait_id.in_(wait_ids),
                            playbook_waits.c.run_id != run_id,
                        )
                    )
                )
                .mappings()
                .fetchone()
            )
            if foreign is not None:
                raise WaitOwnershipViolation(run_id, foreign["wait_id"], foreign["run_id"])
        if wait_changes.clear_run_waits:
            await self.clear_for_run(run_id, conn=conn)
        if wait_ids:
            await conn.execute(
                update(playbook_waits)
                .where(
                    playbook_waits.c.wait_id.in_(wait_ids),
                    playbook_waits.c.run_id == run_id,
                    playbook_waits.c.state == "active",
                )
                .values(state="cleared")
            )
        immediate: list[WaitClaim] = []
        for wait in wait_changes.register:
            registration = await self.register(wait, version, conn=conn)
            if registration.matched_immediately is not None:
                immediate.append(registration.matched_immediately)
        return tuple(immediate)

    async def request_cancel(
        self, run_id: str, *, expected_version: int, reason: str, requested_by: str
    ) -> RunSnapshot:
        """Record cancellation intent under the same CAS as a boundary.

        A ``paused`` run has nothing in flight to acknowledge, so the design
        spec cancels it immediately; a ``running`` one enters ``cancelling``
        and the engine writes the acknowledgement receipt through
        ``commit_boundary``.  No receipt is written here.
        """
        now = time.time()
        limits = self.playbook_state_limits()
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(playbook_v2_runs).where(playbook_v2_runs.c.run_id == run_id)
                    )
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                raise SnapshotVersionConflict(run_id, expected_version, None)
            current = RunLifecycle(row["lifecycle"])
            if current in TERMINAL_LIFECYCLES:
                raise IllegalLifecycleTransition(
                    run_id, current.value, RunLifecycle.CANCELLED.value
                )
            target = (
                RunLifecycle.CANCELLED
                if current is RunLifecycle.PAUSED
                else RunLifecycle.CANCELLING
            )
            validate_transition(run_id, current, target)
            snapshot = deserialize_snapshot(row["snapshot"], version=expected_version)
            advanced = replace(
                snapshot,
                lifecycle=target,
                version=expected_version + 1,
                cancel_requested_at=now,
                updated_at=now,
                completed_at=now if target is RunLifecycle.CANCELLED else snapshot.completed_at,
            )
            payload = serialize_snapshot(advanced, limits=limits)
            result = await conn.execute(
                update(playbook_v2_runs)
                .where(
                    playbook_v2_runs.c.run_id == run_id,
                    playbook_v2_runs.c.snapshot_version == expected_version,
                )
                .values(
                    snapshot_version=advanced.version,
                    cancel_requested_by=requested_by,
                    cancel_reason=reason,
                    **_run_columns(advanced, payload),
                )
            )
            if result.rowcount != 1:
                raise SnapshotVersionConflict(
                    run_id, expected_version, int(row["snapshot_version"])
                )
            # A paused run owns active durable waits.  Cancelling it directly
            # must retire those waits before this transaction commits, or a
            # later event can claim a terminal run after a restart.
            if target is RunLifecycle.CANCELLED:
                await self.clear_for_run(run_id, conn=conn)
        return advanced

    async def list_runs(
        self,
        *,
        playbook_id: str | None = None,
        lifecycle: str | None = None,
        artifact_sha256: str | None = None,
        limit: int = 50,
    ) -> list[RunSnapshot]:
        stmt = select(playbook_v2_runs)
        if playbook_id is not None:
            stmt = stmt.where(playbook_v2_runs.c.playbook_id == playbook_id)
        if lifecycle is not None:
            stmt = stmt.where(playbook_v2_runs.c.lifecycle == lifecycle)
        if artifact_sha256 is not None:
            stmt = stmt.where(playbook_v2_runs.c.artifact_sha256 == artifact_sha256)
        stmt = stmt.order_by(playbook_v2_runs.c.started_at.desc()).limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [
            deserialize_snapshot(row["snapshot"], version=int(row["snapshot_version"]))
            for row in rows
        ]

    async def list_receipts(
        self, run_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[StepReceipt]:
        stmt = (
            select(playbook_step_receipts)
            .where(playbook_step_receipts.c.run_id == run_id)
            .order_by(
                playbook_step_receipts.c.snapshot_version,
                playbook_step_receipts.c.turn_index,
                playbook_step_receipts.c.started_at,
                playbook_step_receipts.c.receipt_id,
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [_row_to_receipt(row) for row in rows]

    # -- retention (§12.1) ---------------------------------------------------

    async def purge_receipts(self, before: float, *, limit: int = 1000) -> int:
        """Delete receipts of terminal runs that completed before ``before``.

        Receipts of a run that is still live are never collectable, however
        old the attempt is — the run can still be resumed and its history is
        what proves the attempt already happened.
        """
        terminal = [state.value for state in TERMINAL_LIFECYCLES]
        async with self.immediate() as conn:
            doomed = (
                (
                    await conn.execute(
                        select(playbook_step_receipts.c.receipt_id)
                        .select_from(
                            playbook_step_receipts.join(
                                playbook_v2_runs,
                                playbook_step_receipts.c.run_id == playbook_v2_runs.c.run_id,
                            )
                        )
                        .where(
                            playbook_v2_runs.c.lifecycle.in_(terminal),
                            playbook_v2_runs.c.completed_at.is_not(None),
                            playbook_v2_runs.c.completed_at < before,
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if not doomed:
                return 0
            await conn.execute(
                delete(playbook_step_receipts).where(
                    playbook_step_receipts.c.receipt_id.in_(list(doomed))
                )
            )
        return len(doomed)

    async def purge_runs(self, before: float, *, limit: int = 1000) -> int:
        """Delete terminal run snapshots (and their children) completed before ``before``.

        Three things keep a run out of the candidate set (§12.1: "never
        collected while lifecycle is not terminal, or the run is pinned"):

        * a non-terminal lifecycle — the run can still be resumed;
        * a null ``completed_at`` — a terminal row that never recorded a
          completion time has no horizon to measure against, so it is left
          for an operator rather than guessed at;
        * **pinning.**  The schema carries no ``pinned`` column (child plan
          §6.3), so the only pin this package can honour is the structural
          one: a run that a *live* run still names as its
          ``parent_run_id``.  Collecting a live sub-run's parent would strip
          the only record of why that sub-run exists, so those are held back
          until the child itself goes terminal and ages out.

        Receipts and waits are deleted explicitly rather than left to
        ``ON DELETE CASCADE``: the cascade is real on PostgreSQL but inert on
        SQLite, where ``PRAGMA foreign_keys`` is off for ordinary
        connections, so relying on it would leak both tables on the default
        development backend.
        """
        terminal = [state.value for state in TERMINAL_LIFECYCLES]
        async with self.immediate() as conn:
            live_parents = (
                select(playbook_v2_runs.c.parent_run_id)
                .where(
                    playbook_v2_runs.c.parent_run_id.is_not(None),
                    playbook_v2_runs.c.lifecycle.not_in(terminal),
                )
                .scalar_subquery()
            )
            doomed = (
                (
                    await conn.execute(
                        select(playbook_v2_runs.c.run_id)
                        .where(
                            playbook_v2_runs.c.lifecycle.in_(terminal),
                            playbook_v2_runs.c.completed_at.is_not(None),
                            playbook_v2_runs.c.completed_at < before,
                            playbook_v2_runs.c.run_id.not_in(live_parents),
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if not doomed:
                return 0
            run_ids = list(doomed)
            await conn.execute(
                delete(playbook_step_receipts).where(
                    playbook_step_receipts.c.run_id.in_(run_ids)
                )
            )
            await conn.execute(
                delete(playbook_waits).where(playbook_waits.c.run_id.in_(run_ids))
            )
            await conn.execute(
                delete(playbook_v2_runs).where(playbook_v2_runs.c.run_id.in_(run_ids))
            )
        return len(run_ids)

    # -- waits (§4.5) --------------------------------------------------------

    @asynccontextmanager
    async def _wait_conn(self, conn: AsyncConnection | None) -> AsyncIterator[AsyncConnection]:
        """Borrow the boundary's connection, or open our own transaction.

        This is the atomicity seam of §4.5: with a caller's ``conn`` the
        registration commits or rolls back with the snapshot advance, so the
        window in which a run is suspended and its wait is invisible does not
        exist.
        """
        if conn is not None:
            yield conn
            return
        async with self.immediate() as owned:
            yield owned

    async def _lock_wait_delivery(
        self,
        conn: AsyncConnection,
        playbook_id: str,
        scope: str,
        scope_identifier: str,
    ) -> None:
        """Serialize delivery and registration for one routed playbook.

        SQLite's ``immediate()`` already serializes writers.  PostgreSQL needs
        an explicit common lock because neither side has a wait row it can
        lock until after the race window has opened.
        """
        if conn.dialect.name == "postgresql":
            route = _dumps([playbook_id, scope, scope_identifier])
            await conn.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(route, 0x41515754)
                    )
                )
            )

    async def _claim_registered_wait_from_inbox(
        self,
        conn: AsyncConnection,
        *,
        wait: WaitSpec,
        playbook_id: str,
        scope: str,
        scope_identifier: str,
        snapshot_version: int,
    ) -> WaitClaim | None:
        """Claim a just-inserted wait from the first matching inbox event."""
        rows = (
            (
                await conn.execute(
                    select(playbook_pending_events)
                    .where(
                        playbook_pending_events.c.playbook_id == playbook_id,
                        playbook_pending_events.c.scope == scope,
                        playbook_pending_events.c.scope_identifier == scope_identifier,
                        playbook_pending_events.c.reason == WAIT_EVENT_REASON,
                        playbook_pending_events.c.received_at >= wait.created_at,
                        or_(
                            playbook_pending_events.c.event_type == wait.event_type,
                            wait.event_type == "",
                        ),
                    )
                    .order_by(
                        playbook_pending_events.c.received_at,
                        playbook_pending_events.c.pending_event_id,
                    )
                )
            )
            .mappings()
            .fetchall()
        )
        for row in rows:
            event = _row_to_inbox_event(row)
            if not matches(wait, event):
                continue
            claimed_at = event.received_at
            result = await conn.execute(
                update(playbook_waits)
                .where(
                    playbook_waits.c.wait_id == wait.wait_id,
                    playbook_waits.c.state == "active",
                )
                .values(
                    state="claimed",
                    claimed_event_id=event.event_id,
                    claimed_at=claimed_at,
                )
            )
            if result.rowcount == 1:
                return WaitClaim(
                    wait_id=wait.wait_id,
                    run_id=wait.run_id,
                    step_id=wait.step_id,
                    iteration=wait.iteration,
                    kind=wait.kind,
                    snapshot_version=snapshot_version,
                    claimed_event_id=event.event_id,
                    claimed_at=claimed_at,
                    event_type=event.event_type,
                    event_fields=dict(event.fields),
                )
        return None

    async def _record_wait_event(
        self, conn: AsyncConnection, event: MatchableEvent, *, now: float
    ) -> _InboxEvent:
        """Persist or retrieve the canonical event before matching waits.

        A replay with the same routed event identity always uses the original
        payload and arrival timestamp.  This keeps retransmission from
        rewriting history and satisfying a wait that did not exist when the
        event first arrived.
        """
        if event.event_id is None:
            pending_event_id = uuid.uuid4().hex
        else:
            identity = _dumps(
                [
                    event.playbook_id,
                    event.scope,
                    event.scope_identifier,
                    event.event_id,
                ]
            )
            pending_event_id = uuid.uuid5(uuid.NAMESPACE_OID, identity).hex
            existing = (
                (
                    await conn.execute(
                        select(playbook_pending_events).where(
                            playbook_pending_events.c.pending_event_id == pending_event_id
                        )
                    )
                )
                .mappings()
                .fetchone()
            )
            if existing is not None:
                return _row_to_inbox_event(existing)

        quota = self.playbook_pending_event_quota()
        retained = (
            await conn.execute(
                select(func.count())
                .select_from(playbook_pending_events)
                .where(
                    playbook_pending_events.c.playbook_id == event.playbook_id,
                    playbook_pending_events.c.reason == WAIT_EVENT_REASON,
                )
            )
        ).scalar_one()
        if int(retained) >= quota:
            _warn_pending_quota(event.playbook_id, quota, now)
            raise PendingEventQuotaExceeded(event.playbook_id, quota)

        insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
        await conn.execute(
            insert_fn(playbook_pending_events)
            .values(
                pending_event_id=pending_event_id,
                playbook_id=event.playbook_id,
                scope=event.scope,
                scope_identifier=event.scope_identifier,
                event_type=event.event_type,
                event=_dumps(dict(event.fields)),
                event_id=event.event_id,
                dedup_key="",
                reason=WAIT_EVENT_REASON,
                attempts=1,
                last_error=None,
                received_at=now,
                expires_at=now + WAIT_EVENT_TTL_SECONDS,
                resolved_at=now,
                resolved_by=WAIT_EVENT_RESOLVER,
                resolution="dispatched",
            )
            .on_conflict_do_nothing(
                index_elements=[playbook_pending_events.c.pending_event_id]
            )
        )
        row = (
            (
                await conn.execute(
                    select(playbook_pending_events).where(
                        playbook_pending_events.c.pending_event_id == pending_event_id
                    )
                )
            )
            .mappings()
            .one()
        )
        return _row_to_inbox_event(row)

    async def register(
        self, wait: WaitSpec, snapshot_version: int, *, conn: AsyncConnection | None = None
    ) -> WaitRegistration:
        """Open one durable wait and consume an already-arrived inbox match.

        ``snapshot_version`` is the version the boundary is *writing*, not the
        one it loaded: a resume that finds the two disagreeing refuses with
        ``wait_version_mismatch`` rather than resuming into a moved-on state.
        ``matched_immediately`` lets the engine continue instead of sleeping
        when ingestion won the delivery lock before this registration.
        """
        values = _wait_row(wait, snapshot_version)
        try:
            async with self._wait_conn(conn) as active:
                current = (
                    (
                        await active.execute(
                            select(
                                playbook_v2_runs.c.snapshot_version,
                                playbook_v2_runs.c.playbook_id,
                                playbook_artifacts.c.scope,
                                playbook_artifacts.c.scope_identifier,
                            )
                            .select_from(
                                playbook_v2_runs.join(
                                    playbook_artifacts,
                                    playbook_v2_runs.c.artifact_sha256
                                    == playbook_artifacts.c.artifact_sha256,
                                )
                            )
                            .where(playbook_v2_runs.c.run_id == wait.run_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .fetchone()
                )
                # A boundary-owned connection has already CAS-advanced and
                # locked the run.  Standalone registration happens just
                # before that write, so fence it to the locked next version.
                if conn is None and current is not None:
                    expected_version = int(current["snapshot_version"]) + 1
                    if snapshot_version != expected_version:
                        raise WaitVersionMismatch(
                            wait.wait_id,
                            wait.run_id,
                            expected_version,
                            snapshot_version,
                        )
                playbook_id = current["playbook_id"] if current is not None else ""
                if playbook_id:
                    await self._lock_wait_delivery(
                        active,
                        playbook_id,
                        current["scope"],
                        current["scope_identifier"],
                    )
                await active.execute(insert(playbook_waits).values(**values))
                matched = None
                if playbook_id and wait.kind == "event":
                    matched = await self._claim_registered_wait_from_inbox(
                        active,
                        wait=wait,
                        playbook_id=playbook_id,
                        scope=current["scope"],
                        scope_identifier=current["scope_identifier"],
                        snapshot_version=snapshot_version,
                    )
        except IntegrityError as exc:
            # uq_playbook_waits_active_step — one live wait per step instance.
            raise DuplicateWait(wait.run_id, wait.step_id, wait.iteration) from exc
        return WaitRegistration(wait_id=wait.wait_id, matched_immediately=matched)

    async def claim_for_event(
        self, event: MatchableEvent, *, now: float, limit: int = 100
    ) -> list[WaitClaim]:
        """Persist an event, then claim every active wait it satisfies.

        Inbox persistence happens before the wait scan in the same serialized
        transaction. If registration wins the per-playbook lock, this scan
        sees its wait; if ingestion wins, registration later sees the inbox
        row. The predicate is evaluated in Python over an index-narrowed
        candidate set (``idx_playbook_waits_match``) because ``match`` is inert JSON.
        Only event-addressable kinds are candidates at all: a timer, human, or
        agent-task wait carries no ``event_type`` and no ``match``, so without
        the kind filter it would read as "matches every event" and an
        unrelated event would resume a run before its deadline or answer.
        Candidates are keyset-paged in deterministic order so nonmatches do
        not consume ``limit``; the cap counts successful claims.  Each match
        is claimed by a CAS on ``state='active'``.  Two concurrent dispatches
        of the same event therefore produce exactly one claim per wait: the
        loser's UPDATE matches zero rows.
        """
        if limit <= 0:
            return []

        claims: list[WaitClaim] = []
        cursor: tuple[float, str] | None = None
        async with self.immediate() as conn:
            await self._lock_wait_delivery(
                conn,
                event.playbook_id,
                event.scope,
                event.scope_identifier,
            )
            canonical = await self._record_wait_event(conn, event, now=now)
            while len(claims) < limit:
                filters = [
                    playbook_waits.c.state == "active",
                    playbook_waits.c.created_at <= canonical.received_at,
                    playbook_waits.c.kind.in_(sorted(EVENT_ADDRESSABLE_WAIT_KINDS)),
                    or_(
                        playbook_waits.c.event_type == canonical.event_type,
                        playbook_waits.c.event_type == "",
                    ),
                ]
                if cursor is not None:
                    created_at, wait_id = cursor
                    filters.append(
                        or_(
                            playbook_waits.c.created_at > created_at,
                            and_(
                                playbook_waits.c.created_at == created_at,
                                playbook_waits.c.wait_id > wait_id,
                            ),
                        )
                    )
                rows = (
                    (
                        await conn.execute(
                            select(playbook_waits)
                            .select_from(
                                playbook_waits.join(
                                    playbook_v2_runs,
                                    playbook_waits.c.run_id == playbook_v2_runs.c.run_id,
                                ).join(
                                    playbook_artifacts,
                                    playbook_v2_runs.c.artifact_sha256
                                    == playbook_artifacts.c.artifact_sha256,
                                )
                            )
                            .where(*filters)
                            .where(
                                playbook_v2_runs.c.playbook_id == canonical.playbook_id,
                                playbook_artifacts.c.scope == canonical.scope,
                                playbook_artifacts.c.scope_identifier
                                == canonical.scope_identifier,
                            )
                            .order_by(playbook_waits.c.created_at, playbook_waits.c.wait_id)
                            .limit(limit)
                        )
                    )
                    .mappings()
                    .fetchall()
                )
                if not rows:
                    break
                for row in rows:
                    cursor = (row["created_at"], row["wait_id"])
                    if not matches(_row_to_wait(row), canonical):
                        continue
                    result = await conn.execute(
                        update(playbook_waits)
                        .where(
                            playbook_waits.c.wait_id == row["wait_id"],
                            playbook_waits.c.state == "active",
                        )
                        .values(
                            state="claimed",
                            claimed_event_id=canonical.event_id,
                            claimed_at=canonical.received_at,
                        )
                    )
                    if result.rowcount != 1:
                        continue
                    claims.append(
                        _row_to_claim(
                            row,
                            canonical,
                            canonical.received_at,
                            expired=False,
                        )
                    )
                    if len(claims) == limit:
                        break
        return claims

    async def expire_due(self, now: float, *, limit: int = 100) -> list[WaitClaim]:
        """Claim every active wait whose deadline has passed.

        Same CAS as :meth:`claim_for_event` with ``state='expired'``, so a
        wait that an event claims in the same instant is expired by nobody.
        """
        claims: list[WaitClaim] = []
        async with self.immediate() as conn:
            rows = (
                (
                    await conn.execute(
                        select(playbook_waits)
                        .where(
                            playbook_waits.c.state == "active",
                            playbook_waits.c.deadline_at.is_not(None),
                            playbook_waits.c.deadline_at <= now,
                        )
                        .order_by(playbook_waits.c.deadline_at, playbook_waits.c.wait_id)
                        .limit(limit)
                    )
                )
                .mappings()
                .fetchall()
            )
            for row in rows:
                result = await conn.execute(
                    update(playbook_waits)
                    .where(
                        playbook_waits.c.wait_id == row["wait_id"],
                        playbook_waits.c.state == "active",
                    )
                    .values(state="expired", claimed_at=now)
                )
                if result.rowcount != 1:
                    continue
                claims.append(_row_to_claim(row, None, now, expired=True))
        return claims

    async def clear_for_run(
        self, run_id: str, *, conn: AsyncConnection | None = None
    ) -> int:
        """Deactivate every active wait of one run; returns how many moved."""
        async with self._wait_conn(conn) as active:
            result = await active.execute(
                update(playbook_waits)
                .where(
                    playbook_waits.c.run_id == run_id,
                    playbook_waits.c.state == "active",
                )
                .values(state="cleared")
            )
        return int(result.rowcount)

    async def list_active(self, run_id: str) -> list[WaitSpec]:
        stmt = (
            select(playbook_waits)
            .where(
                playbook_waits.c.run_id == run_id,
                playbook_waits.c.state == "active",
            )
            .order_by(playbook_waits.c.created_at, playbook_waits.c.wait_id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [_row_to_wait(row) for row in rows]

    # -- pending events (§10.3) ---------------------------------------------

    def playbook_pending_event_quota(self) -> int:
        """``playbooks.v2_max_pending_events_per_playbook``, or the default."""
        configured = getattr(self, "_playbook_pending_event_quota", None)
        return DEFAULT_PENDING_EVENT_QUOTA if configured is None else configured

    def set_playbook_pending_event_quota(self, quota: int) -> None:
        self._playbook_pending_event_quota = int(quota)

    async def retain_pending_event(
        self,
        *,
        playbook_id: str,
        scope: str,
        scope_identifier: str,
        event_type: str,
        event: Mapping[str, Any],
        event_id: str | None,
        dedup_key: str,
        reason: str,
        now: float,
        ttl_seconds: float,
    ) -> str | None:
        """Retain an event whose activation is not ``ready``.

        Returns the new id, or ``None`` when ``uq_playbook_pending_events_dedup``
        rejects it as a duplicate of an unresolved event with the same
        ``dedup_key`` — deduplication is the index, never a pre-read, because
        a pre-read races.  The quota *is* a pre-read, and is allowed to be
        approximate under concurrency: it is a flood ceiling, not an
        invariant.
        """
        quota = self.playbook_pending_event_quota()
        pending_event_id = uuid.uuid4().hex
        try:
            async with self.immediate() as conn:
                unresolved = (
                    await conn.execute(
                        select(func.count())
                        .select_from(playbook_pending_events)
                        .where(
                            playbook_pending_events.c.playbook_id == playbook_id,
                            playbook_pending_events.c.resolved_at.is_(None),
                        )
                    )
                ).scalar_one()
                if int(unresolved) >= quota:
                    _warn_pending_quota(playbook_id, quota, now)
                    raise PendingEventQuotaExceeded(playbook_id, quota)
                insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
                result = await conn.execute(
                    insert_fn(playbook_pending_events)
                    .values(
                        pending_event_id=pending_event_id,
                        playbook_id=playbook_id,
                        scope=scope,
                        scope_identifier=scope_identifier,
                        event_type=event_type,
                        event=_dumps(dict(event)),
                        event_id=event_id,
                        dedup_key=dedup_key,
                        reason=reason,
                        attempts=0,
                        last_error=None,
                        received_at=now,
                        expires_at=now + ttl_seconds,
                        resolved_at=None,
                        resolved_by=None,
                        resolution=None,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            playbook_pending_events.c.playbook_id,
                            playbook_pending_events.c.dedup_key,
                        ],
                        index_where=text("resolved_at IS NULL AND dedup_key <> ''"),
                    )
                )
                if result.rowcount == 0:
                    return None
        except IntegrityError as exc:
            raise PendingEventIntegrityError(playbook_id) from exc
        return pending_event_id

    async def resolve_pending_event(
        self, pending_event_id: str, *, resolution: str, resolved_by: str, now: float
    ) -> bool:
        """CAS on ``resolved_at IS NULL`` — two operators produce one dispatch."""
        async with self.immediate() as conn:
            result = await conn.execute(
                update(playbook_pending_events)
                .where(
                    playbook_pending_events.c.pending_event_id == pending_event_id,
                    playbook_pending_events.c.resolved_at.is_(None),
                )
                .values(resolved_at=now, resolved_by=resolved_by, resolution=resolution)
            )
        return int(result.rowcount) == 1

    async def list_pending_events(
        self,
        *,
        playbook_id: str | None = None,
        include_resolved: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Retained events in arrival order — the replay order of §6.6."""
        stmt = select(playbook_pending_events)
        if playbook_id is not None:
            stmt = stmt.where(playbook_pending_events.c.playbook_id == playbook_id)
        if not include_resolved:
            stmt = stmt.where(playbook_pending_events.c.resolved_at.is_(None))
        stmt = (
            stmt.order_by(
                playbook_pending_events.c.received_at,
                playbook_pending_events.c.pending_event_id,
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [_row_to_pending_event(row) for row in rows]

    async def purge_pending_events(
        self, now: float, *, resolved_before: float, limit: int = 1000
    ) -> PendingEventPurge:
        """Expire unresolved events, then collect resolved events past their horizon.

        Expiry is an auditable resolution rather than a delete: an operator
        can inspect the original event, when it expired, and which system
        component made that decision throughout the configured retention
        window. ``resolved_before`` comes from the retention sweeper so policy
        stays in configuration rather than being hidden in this query.
        """
        async with self.immediate() as conn:
            expiring = (
                (
                    await conn.execute(
                        select(playbook_pending_events.c.pending_event_id)
                        .where(
                            playbook_pending_events.c.resolved_at.is_(None),
                            playbook_pending_events.c.expires_at <= now,
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            expired = 0
            if expiring:
                result = await conn.execute(
                    update(playbook_pending_events)
                    .where(
                        playbook_pending_events.c.pending_event_id.in_(list(expiring)),
                        playbook_pending_events.c.resolved_at.is_(None),
                        playbook_pending_events.c.expires_at <= now,
                    )
                    .values(
                        resolved_at=now,
                        resolved_by=PENDING_EVENT_EXPIRY_ACTOR,
                        resolution="expired",
                    )
                )
                expired = int(result.rowcount)

            # One bounded sweep does at most ``limit`` state changes. Newly
            # expired rows consume the budget first, so an expiry flood cannot
            # make the maintenance transaction unbounded.
            remaining = max(limit - expired, 0)
            purged = 0
            if remaining:
                doomed = (
                    (
                        await conn.execute(
                            select(playbook_pending_events.c.pending_event_id)
                            .where(
                                playbook_pending_events.c.resolved_at.is_not(None),
                                playbook_pending_events.c.resolved_at <= resolved_before,
                            )
                            .limit(remaining)
                        )
                    )
                    .scalars()
                    .all()
                )
                if doomed:
                    result = await conn.execute(
                        delete(playbook_pending_events).where(
                            playbook_pending_events.c.pending_event_id.in_(list(doomed)),
                            playbook_pending_events.c.resolved_at.is_not(None),
                            playbook_pending_events.c.resolved_at <= resolved_before,
                        )
                    )
                    purged = int(result.rowcount)
        return PendingEventPurge(expired=expired, purged=purged)
