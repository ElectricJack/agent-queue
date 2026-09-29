"""Command surface for durable object-script evaluation rounds.

The external scorer owns image measurements.  These commands own bounded
identity checks, reservations, task intents and the finalization hold.
"""

from __future__ import annotations

import hashlib
import json
import time

from pydantic import ValidationError
from sqlalchemy import insert, select, update

from src.commands.contracts.object_loop import (
    ObjectCheckpointReadArgs, ObjectLoopReconcileArgs, ObjectLoopStartArgs,
    ObjectScoreRecordArgs, Reservation, Variant,
)
from src.database.tables import (
    doc_review_revisions, doc_reviews, object_loops, tasks,
)
from src.object_loop.contracts import validate_score_receipt

_UNITS = ("usd", "calls", "bakes", "active_seconds")
_TERMINAL = {"COMPLETED", "BLOCKED"}


def _error(message: str) -> dict:
    return {"success": False, "error": message}


def _budget(reservation: Reservation) -> dict:
    return reservation.model_dump()


def _zero() -> dict:
    return {unit: 0 for unit in _UNITS}


def _reserve(state: dict, variants: list[Variant]) -> None:
    if not variants or len({v.variant_id for v in variants}) != len(variants):
        raise ValueError("wave needs one to three distinct variants")
    if state["round_id"] >= 8:
        raise ValueError("round cap reached")
    additional = {
        unit: (sum(getattr(v.reservation, unit) for v in variants)
               + state["score_reservation"][unit])
        for unit in _UNITS
    }
    for unit in _UNITS:
        if (state["spent"][unit] + state["reserved"][unit] +
                state["final_reserve"][unit] + additional[unit] > state["limits"][unit]):
            raise ValueError(f"{unit} budget would be overspent by sibling reservations")
    state["reserved"] = additional
    state["intent"] = {"kind": "wave", "round_id": state["round_id"],
                       "variants": [v.model_dump() for v in variants]}
    state["wave"] = [{"variant_id": v.variant_id, "task_id": None} for v in variants]
    state["score_task_id"] = None


def _charge(state: dict, spent: Reservation | None) -> None:
    # Unknown or missing usage is charged at the reservation, never zero.
    measured = _budget(spent) if spent is not None else dict(state["reserved"])
    for unit in _UNITS:
        if measured[unit] > state["reserved"][unit]:
            raise ValueError(f"{unit} spend exceeds the reserved wave budget")
        state["spent"][unit] += measured[unit]
    state["reserved"] = _zero()


def _task_settled(row) -> bool:
    return row is not None and (
        row.status in _TERMINAL
        or (row.status == "FAILED" and row.retry_count >= row.max_retries)
    )


def _same_start(state: dict, request: ObjectLoopStartArgs) -> bool:
    return (
        state["attempt_id"] == request.attempt_id
        and state["initial_incumbent_sha256"] == request.incumbent_sha256
        and all(state[key] == getattr(request, key) for key in (
            "reference_sha256", "rig_sha256", "scorer_sha256",
            "render_profile_sha256", "policy_sha256",
        ))
        and state["limits"] == _budget(request.limits)
        and state["final_reserve"] == _budget(request.final_reserve)
        and state["score_reservation"] == _budget(request.score_reservation)
        and state["mandatory_views"] == request.mandatory_views
        and state["noise_band"] == request.noise_band
        and state["max_repair_rounds"] == request.max_repair_rounds
        and state["max_plateau_rounds"] == request.max_plateau_rounds
        and state["brief_checkpoint"]["review_id"] == request.brief_review_id
        and state["brief_checkpoint"]["revision"] == request.brief_review_revision
        and state["brief_checkpoint"]["sha256"] == request.brief_review_sha256
    )


async def _approval_valid(conn, project_id: str, checkpoint: dict) -> bool:
    review = (await conn.execute(
        select(doc_reviews).where(doc_reviews.c.id == checkpoint["review_id"])
    )).mappings().first()
    if review is None or review.project_id != project_id or review.kind != "other":
        return False
    if review.state != "approved" or review.current_revision != checkpoint["revision"]:
        return False
    revision = (await conn.execute(select(doc_review_revisions).where(
        doc_review_revisions.c.review_id == review.id,
        doc_review_revisions.c.revision == checkpoint["revision"],
    ))).mappings().first()
    return bool(revision and revision.content_sha256 == checkpoint["sha256"]
                and review.decided_at is not None and review.gate_id == checkpoint["gate_id"])


async def _review_verdict(conn, state: dict) -> bool:
    checkpoint = state.get("checkpoint")
    if not checkpoint:
        return True
    return (checkpoint["candidate_sha256"] == state["incumbent_sha256"]
            and await _approval_valid(conn, state["project_id"], checkpoint))


class ObjectLoopCommandsMixin:
    async def _cmd_object_loop_start(self, args: dict) -> dict:
        try:
            request = ObjectLoopStartArgs.model_validate(args)
            if len(set(request.mandatory_views)) != len(request.mandatory_views):
                raise ValueError("mandatory views must be distinct")
            state = {
                "object_id": request.object_id, "project_id": request.project_id,
                "attempt_id": request.attempt_id, "round_id": 0,
                "incumbent_sha256": request.incumbent_sha256,
                "initial_incumbent_sha256": request.incumbent_sha256,
                "incumbent_artifact": (request.incumbent_artifact.model_dump()
                                       if request.incumbent_artifact else None),
                "incumbent_loss": None,
                "reference_sha256": request.reference_sha256, "rig_sha256": request.rig_sha256,
                "scorer_sha256": request.scorer_sha256,
                "render_profile_sha256": request.render_profile_sha256,
                "policy_sha256": request.policy_sha256,
                "brief_checkpoint": {
                    "review_id": request.brief_review_id,
                    "revision": request.brief_review_revision,
                    "sha256": request.brief_review_sha256,
                    "gate_id": None,
                },
                "mandatory_views": request.mandatory_views,
                "noise_band": request.noise_band, "limits": _budget(request.limits),
                "spent": _zero(), "reserved": _zero(),
                "final_reserve": _budget(request.final_reserve),
                "score_reservation": _budget(request.score_reservation),
                "wave": [], "intent": None,
                "score_task_id": None, "checkpoint": None, "stop_reason": None,
                "status": "active", "decision_sha256": None,
                "repair_count": 0, "plateau_count": 0,
                "max_repair_rounds": request.max_repair_rounds,
                "max_plateau_rounds": request.max_plateau_rounds,
            }
            if request.incumbent_artifact and request.incumbent_artifact.sha256 != request.incumbent_sha256:
                raise ValueError("incumbent artifact hash does not match incumbent")
            _reserve(state, request.variants)
        except (ValueError, ValidationError) as exc:
            return _error(str(exc))

        async with self.db.immediate() as conn:
            prior = (await conn.execute(select(object_loops).where(
                object_loops.c.object_id == request.object_id
            ))).mappings().first()
        if prior:
            if (prior.project_id != request.project_id or prior.epic_task_id != request.epic_task_id
                    or not _same_start(prior.state, request)):
                return _error("object id already has different fixed inputs")
            return {"success": True, "object_id": request.object_id, "version": prior.version,
                    "state": prior.state, "created": False}
        epic = await self.db.get_task(request.epic_task_id)
        if epic is None or epic.project_id != request.project_id or epic.parent_task_id:
            return _error("object epic must be a root task in this project")
        if epic.status.value in {"COMPLETED", "FAILED", "BLOCKED"}:
            return _error("object epic is terminal")

        async def bootstrap(conn, task_id, parent_id):
            if parent_id != request.epic_task_id:
                raise ValueError("finalization task escaped object epic")
            await self.db._upsert_meta(task_id, "object_experiment", {
                "object_id": request.object_id, "purpose": "finalize",
                "publish_source": False,
            }, conn=conn)
            review = (await conn.execute(select(doc_reviews).where(
                doc_reviews.c.id == request.brief_review_id,
            ).with_for_update())).mappings().first()
            if review is None or not review.gate_id:
                raise ValueError("brief review gate is missing")
            state["brief_checkpoint"]["gate_id"] = review.gate_id
            if not await _approval_valid(conn, request.project_id, state["brief_checkpoint"]):
                raise ValueError("brief review is not approved at the exact revision")
            gate_id, _created = await self.db.create_gate(
                request.project_id, "event", f"Finalize object {request.object_id}",
                await_id=f"object:{request.object_id}:terminal", waiter_task_ids=[task_id], conn=conn,
            )
            await conn.execute(insert(object_loops).values(
                object_id=request.object_id, project_id=request.project_id,
                epic_task_id=request.epic_task_id, finalization_task_id=task_id,
                terminal_gate_id=gate_id, version=1, state=state,
                created_at=time.time(), updated_at=time.time(),
            ))

        try:
            result = await self._create_task({
                "project_id": request.project_id,
                "parent_id": request.epic_task_id,
                "title": f"Finalize object {request.object_id}",
                "description": "Verify retained evaluation artifacts, checkpoints and stop reason.",
                "task_type": "chore",
                "dedup_key": f"object:{request.object_id}:finalize",
                "_after_create_on": bootstrap,
                "_created_by_kind": "object_loop",
                "_created_by_id": request.object_id,
            })
        except Exception as exc:
            # Another start may have committed the unique object row while
            # this transaction was creating its finalizer. Its task rolled
            # back with this callback; return the winning loop on replay.
            async with self.db.immediate() as conn:
                prior = (await conn.execute(select(object_loops).where(
                    object_loops.c.object_id == request.object_id,
                ))).mappings().first()
            if (prior and prior.project_id == request.project_id
                    and prior.epic_task_id == request.epic_task_id
                    and _same_start(prior.state, request)):
                return {"success": True, "object_id": request.object_id,
                        "version": prior.version, "state": prior.state, "created": False}
            return _error(f"object loop bootstrap failed: {exc}")
        if not result.get("success"):
            return result
        return {"success": True, "object_id": request.object_id, "version": 1,
                "state": state, "created": True}

    async def _cmd_object_checkpoint_read(self, args: dict) -> dict:
        try:
            request = ObjectCheckpointReadArgs.model_validate(args)
        except ValidationError as exc:
            return _error(str(exc))
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(object_loops).where(
                object_loops.c.object_id == request.object_id,
                object_loops.c.project_id == request.project_id,
            ))).mappings().first()
            if row is None:
                return _error("object loop not found in project")
            approved = await _review_verdict(conn, row.state)
            return {"success": True, "object_id": request.object_id, "version": row.version,
                    "approved": approved if row.state.get("checkpoint") else False,
                    "state": row.state}

    async def _cmd_object_loop_reconcile(self, args: dict) -> dict:
        try:
            request = ObjectLoopReconcileArgs.model_validate(args)
        except ValidationError as exc:
            return _error(str(exc))
        gate_to_release = None
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(object_loops).where(
                object_loops.c.object_id == request.object_id,
                object_loops.c.project_id == request.project_id,
            ).with_for_update())).mappings().first()
            if row is None:
                return _error("object loop not found in project")
            state = dict(row.state)
            if (state["status"] != "stopped" and not await _approval_valid(
                conn, state["project_id"], state["brief_checkpoint"]
            )):
                return {"success": True, "object_id": request.object_id, "version": row.version,
                        "state": state, "outcome": "brief_held"}
            if request.next_variants or request.stop_reason:
                if not state.get("checkpoint"):
                    return _error("continuation is only accepted at a checkpoint")
                if request.expected_version != row.version:
                    return _error("stale object loop version")
                if not await _review_verdict(conn, state):
                    return _error("checkpoint is not approved for this candidate revision")
                if request.next_variants and request.stop_reason:
                    return _error("choose next variants or a stop reason")
                state["last_approved_checkpoint"] = state["checkpoint"]
                state["checkpoint"] = None
                if request.stop_reason:
                    state["status"] = "stopped"
                    state["stop_reason"] = request.stop_reason
                else:
                    state["round_id"] += 1
                    state["last_approved_checkpoint"]["released_for_round"] = state["round_id"]
                    try:
                        _reserve(state, request.next_variants)
                    except ValueError as exc:
                        return _error(str(exc))
            if state["status"] == "stopped":
                gate_to_release = row.terminal_gate_id
            elif state.get("checkpoint") and not await _review_verdict(conn, state):
                return {"success": True, "object_id": request.object_id, "version": row.version,
                        "state": state, "outcome": "checkpoint_held"}
            elif state.get("checkpoint") and not state.get("intent"):
                return {"success": True, "object_id": request.object_id, "version": row.version,
                        "state": state, "outcome": "checkpoint_approved"}
            elif (state.get("intent") or {}).get("kind") == "wave":
                for variant, member in zip(state["intent"]["variants"], state["wave"], strict=True):
                    key = (f"object:{request.object_id}:attempt:{state['attempt_id']}:"
                           f"round:{state['round_id']}:variant:{variant['variant_id']}:candidate")
                    task_id = await self._ensure_object_child(
                        conn, row, key, variant["title"],
                        json.dumps({
                            "object_id": request.object_id, "attempt_id": state["attempt_id"],
                            "round_id": state["round_id"], "variant_id": variant["variant_id"],
                            "hypothesis": variant["hypothesis"],
                            "base_candidate_sha256": state["incumbent_sha256"],
                            "base_artifact": state["incumbent_artifact"],
                            "reference_sha256": state["reference_sha256"],
                            "rig_sha256": state["rig_sha256"],
                            "scorer_sha256": state["scorer_sha256"],
                            "render_profile_sha256": state["render_profile_sha256"],
                            "policy_sha256": state["policy_sha256"],
                            "mandatory_views": state["mandatory_views"],
                            "reservation": variant["reservation"],
                            "publication": "artifact_only",
                        }, sort_keys=True), "candidate",
                        approval_gate_id=(state.get("last_approved_checkpoint") or
                                          state["brief_checkpoint"])["gate_id"],
                    )
                    member["task_id"] = task_id
                state["intent"] = None
            elif state["wave"] and not state["score_task_id"]:
                task_rows = await self._object_wave_rows(conn, state)
                if not all(_task_settled(task_rows.get(m["task_id"])) for m in state["wave"]):
                    return {"success": True, "object_id": request.object_id, "version": row.version,
                            "state": state, "outcome": "waiting_for_wave"}
                key = (f"object:{request.object_id}:attempt:{state['attempt_id']}:"
                       f"round:{state['round_id']}:score")
                failures = [m["task_id"] for m in state["wave"]
                            if task_rows[m["task_id"]].status != "COMPLETED"]
                task_id = await self._ensure_object_child(
                    conn, row, key, f"Score object {request.object_id} round {state['round_id']}",
                    json.dumps({"object_id": request.object_id, "round_id": state["round_id"],
                                "wave": state["wave"], "exhausted_failures": failures,
                                "mandatory_views": state["mandatory_views"],
                                "publication": "artifact_only"}, sort_keys=True), "score",
                    approval_gate_id=(state.get("last_approved_checkpoint") or
                                      state["brief_checkpoint"])["gate_id"],
                )
                state["score_task_id"] = task_id
            else:
                return {"success": True, "object_id": request.object_id, "version": row.version,
                        "state": state, "outcome": "waiting_for_score"}
            await conn.execute(update(object_loops).where(object_loops.c.object_id == request.object_id)
                               .values(state=state, version=row.version + 1, updated_at=time.time()))
            version = row.version + 1
        if gate_to_release:
            await self.db.resolve_gate(gate_to_release, resolved_by="object_loop",
                                       resolution="recorded stop")
            return {"success": True, "object_id": request.object_id,
                    "version": row.version, "state": state, "outcome": "stopped"}
        return {"success": True, "object_id": request.object_id, "version": version,
                "state": state, "outcome": "reconciled"}

    async def _object_wave_rows(self, conn, state):
        ids = [member["task_id"] for member in state["wave"]]
        rows = (await conn.execute(select(tasks).where(tasks.c.id.in_(ids)))).mappings().all()
        return {row.id: row for row in rows}

    async def _ensure_object_child(
        self, conn, loop, key, title, description, purpose, *, approval_gate_id: str
    ):
        existing = (await conn.execute(select(tasks).where(
            tasks.c.project_id == loop.project_id, tasks.c.dedup_key == key,
        ).order_by(tasks.c.created_at).limit(1))).mappings().first()
        if existing is not None:
            if (existing.parent_task_id != loop.epic_task_id
                    or existing.created_by_kind != "object_loop"
                    or existing.created_by_id != loop.object_id):
                raise ValueError("object task dedup key belongs to foreign work")
            return existing.id

        async def mark(conn, task_id, parent_id):
            if parent_id != loop.epic_task_id:
                raise ValueError("object child escaped its epic")
            await self.db._upsert_meta(task_id, "object_experiment", {
                "object_id": loop.object_id, "purpose": purpose,
                "publish_source": False,
            }, conn=conn)
            await self.db.attach_gate_waiters(approval_gate_id, [task_id], conn=conn)

        result = await self._create_task({
            "project_id": loop.project_id, "parent_id": loop.epic_task_id,
            "title": title, "description": description,
            "task_type": "research", "dedup_key": key,
            "_after_create_on": mark,
            "_created_by_kind": "object_loop", "_created_by_id": loop.object_id,
        })
        if not result.get("success"):
            raise ValueError(result.get("error", "object child creation failed"))
        return result["task_id"]

    async def _cmd_object_score_record(self, args: dict) -> dict:
        try:
            request = ObjectScoreRecordArgs.model_validate(args)
        except ValidationError as exc:
            return _error(str(exc))
        fingerprint = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(object_loops).where(
                object_loops.c.object_id == request.object_id,
                object_loops.c.project_id == request.project_id,
            ).with_for_update())).mappings().first()
            if row is None:
                return _error("object loop not found in project")
            state = dict(row.state)
            if (request.action != "stop" and not await _approval_valid(
                conn, state["project_id"], state["brief_checkpoint"]
            )):
                return _error("brief review is no longer approved at the exact revision")
            approved_checkpoint = state.get("last_approved_checkpoint")
            if (approved_checkpoint and approved_checkpoint.get("released_for_round") ==
                    state["round_id"] and not await _approval_valid(
                        conn, state["project_id"], approved_checkpoint
                    )):
                return _error("checkpoint approval changed during this round")
            if state.get("decision_sha256") == fingerprint:
                return {"success": True, "object_id": request.object_id, "version": row.version,
                        "state": state, "outcome": "reused"}
            if row.version != request.expected_version:
                return _error("stale object loop version")
            if state["status"] != "active" or state["score_task_id"] != request.score_task_id:
                return _error("score task is not the current round scorer")
            scorer = (await conn.execute(select(tasks.c.status).where(
                tasks.c.id == request.score_task_id
            ))).scalar_one_or_none()
            if scorer != "COMPLETED":
                return _error("scoring task has not completed")
            wave_rows = await self._object_wave_rows(conn, state)
            if not all(_task_settled(wave_rows.get(m["task_id"])) for m in state["wave"]):
                return _error("wave has unsettled candidates")
            receipts = {receipt.variant_id: receipt for receipt in request.receipts}
            if len(receipts) != len(request.receipts):
                return _error("duplicate variant score")
            if set(receipts) - {member["variant_id"] for member in state["wave"]}:
                return _error("score belongs to a foreign variant")
            for member in state["wave"]:
                receipt = receipts.get(member["variant_id"])
                if wave_rows[member["task_id"]].status == "COMPLETED" and receipt is None:
                    return _error("completed candidate is missing its score")
                if receipt is not None:
                    try:
                        validate_score_receipt(
                            receipt, state=state, task_id=member["task_id"],
                            mandatory_views=set(state["mandatory_views"]),
                        )
                    except ValueError as exc:
                        return _error(str(exc))
            eligible = [r for r in request.receipts
                        if wave_rows[r.task_id].status == "COMPLETED"
                        and r.validity == "valid" and r.quality_pass
                        and all(r.hard_gates.values())]
            ranked = sorted(eligible, key=lambda r: (
                sum(v.loss for v in r.per_view.values()) / len(r.per_view),
                max(v.loss for v in r.per_view.values()), r.variant_id,
            ))
            winner = ranked[0] if ranked else None
            winner_loss = (sum(v.loss for v in winner.per_view.values()) / len(winner.per_view)
                           if winner else None)
            if winner is not None and (state["incumbent_loss"] is None or
                                       winner_loss + state["noise_band"] < state["incumbent_loss"]):
                state["incumbent_sha256"] = winner.candidate_sha256
                state["incumbent_artifact"] = next(
                    artifact.model_dump() for artifact in winner.artifacts
                    if artifact.sha256 == winner.candidate_sha256
                )
                state["incumbent_loss"] = winner_loss
                state["selected_variant_id"] = winner.variant_id
                state["plateau_count"] = 0
            else:
                state["selected_variant_id"] = None
                state["plateau_count"] += 1
            if not eligible:
                state["repair_count"] += 1
            try:
                # A partial receipt set or incomplete cost coverage leaves the
                # measured aggregate unverified. Charge the full reservation.
                known_spend = (len(receipts) == len(state["wave"])
                               and all(r.cost_coverage.get("known") is True
                                       for r in request.receipts))
                _charge(state, request.spent if known_spend else None)
                if request.action == "continue":
                    if state["repair_count"] > state["max_repair_rounds"]:
                        raise ValueError("repair round cap reached")
                    if state["plateau_count"] >= state["max_plateau_rounds"]:
                        raise ValueError("plateau round cap reached")
                    if not request.next_variants:
                        raise ValueError("continue requires next variants")
                    if state.get("checkpoint") and not await _review_verdict(conn, state):
                        raise ValueError("checkpoint is not approved for this candidate revision")
                    state["round_id"] += 1
                    _reserve(state, request.next_variants)
                elif request.action == "checkpoint":
                    if not (request.review_id and request.review_revision and request.review_sha256):
                        raise ValueError("checkpoint needs review id, revision and hash")
                    review = (await conn.execute(select(doc_reviews).where(
                        doc_reviews.c.id == request.review_id,
                        doc_reviews.c.project_id == request.project_id,
                        doc_reviews.c.kind == "other",
                    ))).mappings().first()
                    revision = (await conn.execute(select(doc_review_revisions).where(
                        doc_review_revisions.c.review_id == request.review_id,
                        doc_review_revisions.c.revision == request.review_revision,
                    ))).mappings().first()
                    if (not review or not review.gate_id or not revision
                            or review.current_revision != request.review_revision
                            or revision.content_sha256 != request.review_sha256):
                        raise ValueError("checkpoint review revision is missing or foreign")
                    state["checkpoint"] = {"review_id": request.review_id,
                                           "revision": request.review_revision,
                                           "sha256": request.review_sha256,
                                           "gate_id": review.gate_id,
                                           "candidate_sha256": state["incumbent_sha256"]}
                    state["intent"] = None
                else:
                    if not request.stop_reason:
                        raise ValueError("stop requires an explicit reason")
                    state["status"] = "stopped"
                    state["stop_reason"] = request.stop_reason
                    state["intent"] = None
            except ValueError as exc:
                return _error(str(exc))
            state["decision_sha256"] = fingerprint
            await conn.execute(update(object_loops).where(object_loops.c.object_id == request.object_id)
                               .values(state=state, version=row.version + 1, updated_at=time.time()))
        return {"success": True, "object_id": request.object_id, "version": row.version + 1,
                "state": state, "outcome": request.action}
