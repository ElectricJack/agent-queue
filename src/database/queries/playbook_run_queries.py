"""Playbook V2 durable run state — snapshots, receipts (child plan §4.4).

Every durable advance of a V2 run is **one** ``immediate()`` block containing
a compare-and-set on ``playbook_v2_runs.snapshot_version`` and the insert of
exactly one receipt.  Two properties fall out of that shape and neither is
achievable with the V1 mixin's unconditional ``UPDATE``:

* a resume that raced another writer loses the CAS and raises rather than
  silently interleaving (V1's ``update_playbook_run`` is last-writer-wins);
* a replayed attempt after an ambiguous interruption is rejected by
  ``uq_playbook_step_receipts_attempt`` — by the database, not by an
  in-memory guard that a restart would have forgotten.

Size limits are checked *before* the transaction opens, so an oversized
payload never reaches the database at all (§8.2: reject, never externalize,
never truncate).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import playbook_step_receipts, playbook_v2_runs
from src.playbooks.receipts import StepReceipt
from src.playbooks.run_state import (
    DEFAULT_STATE_LIMITS,
    TERMINAL_LIFECYCLES,
    DuplicateAttempt,
    IllegalLifecycleTransition,
    RunLifecycle,
    RunSnapshot,
    SnapshotVersionConflict,
    StateLimits,
    check_result_size,
    deserialize_snapshot,
    serialize_snapshot,
    validate_transition,
)
from src.playbooks.waits import EMPTY_WAIT_CHANGES, WaitChangeSet

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


def _row_to_receipt(row) -> StepReceipt:
    return StepReceipt(
        receipt_id=row["receipt_id"],
        run_id=row["run_id"],
        artifact_sha256=row["artifact_sha256"],
        rule_id=row["rule_id"],
        step_id=row["step_id"],
        step_kind=row["step_kind"],
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
        async with self.immediate() as conn:
            await conn.execute(insert(playbook_v2_runs).values(**values))
        return snapshot

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
        if receipt.run_id != snapshot.run_id:
            raise ValueError(
                f"receipt belongs to run {receipt.run_id}, not {snapshot.run_id}"
            )
        limits = self.playbook_state_limits()
        # Before the transaction, so an oversized payload never reaches the
        # database and a limit breach costs nothing to roll back.
        check_result_size(snapshot.run_id, receipt.step_id, dict(receipt.result), limits=limits)
        expected = snapshot.version
        advanced = replace(snapshot, version=expected + 1)
        payload = serialize_snapshot(advanced, limits=limits)

        async with self.immediate() as conn:
            current = (
                (
                    await conn.execute(
                        select(
                            playbook_v2_runs.c.lifecycle, playbook_v2_runs.c.snapshot_version
                        ).where(playbook_v2_runs.c.run_id == snapshot.run_id)
                    )
                )
                .mappings()
                .fetchone()
            )
            if current is None:
                raise SnapshotVersionConflict(snapshot.run_id, expected, None)
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
            await self._apply_wait_changes(conn, wait_changes, advanced.version)
        return advanced

    async def _apply_wait_changes(self, conn, wait_changes: WaitChangeSet, version: int) -> None:
        """Applied on the boundary's own connection — see §4.5.

        Replaced by the real implementation when the wait tables land; until
        then a non-empty change set is a programming error rather than a
        silently dropped registration.
        """
        if not wait_changes.is_empty:
            raise NotImplementedError("durable waits are not available in this revision")

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
                playbook_step_receipts.c.started_at, playbook_step_receipts.c.receipt_id
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
