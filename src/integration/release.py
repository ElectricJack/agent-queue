"""Hierarchy-fenced release of a terminal root integration train."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update

from src.database.tables import (
    integration_attestation_publications,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_promotion_intents,
    integration_release_results,
    integration_repair_operations,
    integration_repair_stages,
    integration_root_intent_members,
    project_integration_leases,
    project_integration_schedules,
    projects,
    task_delivery_receipts,
)
from src.integration.live_operations import ACTIVE_OPERATION_STATES

Verdict = Literal["none", "active", "blocked", "stale"]

#: Lifecycles a batch never leaves.  ``failed`` is admitted by the schema's
#: lifecycle constraint; nothing writes it today.
ENDED_LIFECYCLES = ("aborted", "failed", "empty")
#: Lifecycles in which the train still owns its request.
LIVE_LIFECYCLES = (
    "sealing",
    "sealed",
    "building",
    "testing",
    "repairing",
    "human_blocked",
    "promoting",
    "cleanup_pending",
)
#: Promotion intents in these states have settled their remote write.
_SETTLED_INTENT_STATES = ("committed", "conflict", "superseded")
RELEASABLE = frozenset({"stale"})
RELEASED_EVENT = "integration.schedule_request_released"


class RequestChanged(RuntimeError):
    """The schedule or lease moved after it was classified."""


@dataclass(frozen=True, slots=True)
class OutstandingRequest:
    project_id: str
    verdict: Verdict
    reason: str
    request_id: str | None = None
    request_sequence: int = 0
    batch_id: str | None = None
    lifecycle: str | None = None
    cleanup_state: str | None = None
    lease: dict[str, Any] | None = None
    blockers: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["blockers"] = [dict(blocker) for blocker in self.blockers]
        return value


@dataclass(frozen=True, slots=True)
class RequestRelease:
    request_id: str
    catchup_request_id: str | None
    lease_released: bool
    schedule: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "catchup_request_id": self.catchup_request_id,
            "lease_released": self.lease_released,
            "outstanding_request_id": self.schedule["outstanding_request_id"],
        }


async def classify_outstanding_request_on(
    conn: Any,
    project_id: str,
    schedule: Any,
    *,
    now: float,
    lock: bool = False,
) -> OutstandingRequest:
    """Classify *schedule*'s outstanding request inside the caller's transaction.

    ``lock`` takes the batch and lease rows for update; a caller that will
    release the request passes it; a read-only report does not.
    """
    request_id = schedule["outstanding_request_id"] if schedule is not None else None
    sequence = int(schedule["request_sequence"]) if schedule is not None else 0
    if request_id is None:
        return OutstandingRequest(
            project_id, "none", "no sweep request is outstanding", request_sequence=sequence
        )

    def locked(statement: Any) -> Any:
        return statement.with_for_update() if lock else statement

    batch = (
        await conn.execute(
            locked(
                select(integration_batches).where(
                    integration_batches.c.project_id == project_id,
                    integration_batches.c.request_id == request_id,
                )
            )
        )
    ).mappings().one_or_none()
    lease_row = (
        await conn.execute(
            locked(
                select(project_integration_leases).where(
                    project_integration_leases.c.project_id == project_id
                )
            )
        )
    ).mappings().one_or_none()
    lease = (
        None
        if lease_row is None
        else {
            "batch_id": lease_row["batch_id"],
            "owner_id": lease_row["owner_id"],
            "fence_token": int(lease_row["fence_token"]),
            "expires_at": float(lease_row["expires_at"]),
        }
    )
    known = {
        "request_id": request_id,
        "request_sequence": sequence,
        "lease": lease,
    }

    if batch is None:
        blockers = _foreign_lease(lease, None)
        return OutstandingRequest(
            project_id, "blocked" if blockers else "active",
            "historical sweep request has no batch; operator reconciliation is required",
            blockers=blockers, **known,
        )

    known.update(
        batch_id=batch["id"],
        lifecycle=batch["lifecycle"],
        cleanup_state=batch["cleanup_state"],
    )
    lifecycle = batch["lifecycle"]
    if lifecycle in LIVE_LIFECYCLES:
        return OutstandingRequest(
            project_id,
            "active",
            f"batch {batch['id']} is {lifecycle}; the train ends its own request",
            **known,
        )
    if lifecycle == "promoted":
        if lease is not None and lease["batch_id"] == batch["id"]:
            return OutstandingRequest(
                project_id,
                "active",
                f"batch {batch['id']} promoted and still holds the project lease; "
                "integration_release ends its request",
                **known,
            )
        reason = (
            f"batch {batch['id']} promoted but no longer holds the project lease, so "
            "integration_release can never end its request"
        )
    elif lifecycle in ENDED_LIFECYCLES:
        reason = (
            f"batch {batch['id']} is {lifecycle}; only a promoted batch's release ends a "
            "request, so nothing will end this one"
        )
    else:
        return OutstandingRequest(
            project_id,
            "blocked",
            f"batch {batch['id']} has unrecognised lifecycle {lifecycle!r}",
            blockers=({"code": "lifecycle", "detail": "unrecognised batch lifecycle",
                       "ref": str(lifecycle)},),
            **known,
        )
    blockers = (*await _write_blockers_on(conn, batch["id"]), *_foreign_lease(lease, batch["id"]))
    if blockers:
        return OutstandingRequest(
            project_id,
            "blocked",
            f"{reason}; releasing it waits on: "
            + ", ".join(blocker["code"] for blocker in blockers),
            blockers=blockers,
            **known,
        )
    return OutstandingRequest(project_id, "stale", reason, **known)


async def _write_blockers_on(conn: Any, batch_id: str) -> tuple[dict[str, str], ...]:
    """Evidence that a remote write for *batch_id* may still be in flight.

    Fail closed: an ended batch with any of these is reported ``blocked``, and
    its lease is never released under it.  An attached writer on the batch's own
    integration branch is deliberately not listed -- ``cancel_preserving``
    quarantines writers there on purpose, and that branch is unique to the
    request, so it never meets the next batch.
    """
    statements = (
        (
            "live_operation",
            "a repair operation for the batch is still running",
            select(integration_repair_operations.c.id).where(
                integration_repair_operations.c.batch_id == batch_id,
                integration_repair_operations.c.state.in_(ACTIVE_OPERATION_STATES),
            ),
        ),
        (
            "ref_mutation",
            "a reserved ref mutation has no recorded outcome",
            select(integration_candidate_ref_mutations.c.id).where(
                integration_candidate_ref_mutations.c.batch_id == batch_id,
                integration_candidate_ref_mutations.c.state == "reserved",
            ),
        ),
        (
            "resolution",
            "a candidate repair resolution is reserved or pushed",
            select(integration_candidate_resolutions.c.id).where(
                integration_candidate_resolutions.c.batch_id == batch_id,
                integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
            ),
        ),
        (
            "attestation",
            "an attestation publication is reserved",
            select(integration_attestation_publications.c.id).where(
                integration_attestation_publications.c.batch_id == batch_id,
                integration_attestation_publications.c.state == "reserved",
            ),
        ),
        (
            "promotion",
            "a root promotion intent has not settled",
            select(integration_promotion_intents.c.id).where(
                integration_promotion_intents.c.root_batch_id == batch_id,
                integration_promotion_intents.c.state.not_in(_SETTLED_INTENT_STATES),
            ),
        ),
    )
    blockers = []
    for code, detail, statement in statements:
        value = (await conn.execute(statement.limit(1))).scalar_one_or_none()
        if value is not None:
            blockers.append({"code": code, "detail": detail, "ref": str(value)})
    return tuple(blockers)


async def release_outstanding_request_on(
    db: Any,
    conn: Any,
    state: OutstandingRequest,
    *,
    schedule: Any,
    now: float,
    released_by: str,
    reason: str,
) -> RequestRelease:
    """Release the request *state* classified, in the caller's transaction.

    The caller holds the project's hierarchy lock and the locked schedule row
    *state* was read from. Retirement discards pending catch-up work: the active
    train observes Git directly and no legacy publisher can consume a new sweep.
    """
    if state.verdict not in RELEASABLE or state.request_id is None:
        raise ValueError(f"a {state.verdict} request is not releasable")
    project_id = state.project_id
    lease_released = False
    lease = state.lease
    if lease is not None and state.batch_id is not None and lease["batch_id"] == state.batch_id:
        deleted = await conn.execute(
            delete(project_integration_leases).where(
                project_integration_leases.c.project_id == project_id,
                project_integration_leases.c.batch_id == lease["batch_id"],
                project_integration_leases.c.owner_id == lease["owner_id"],
                project_integration_leases.c.fence_token == lease["fence_token"],
            )
        )
        if deleted.rowcount != 1:
            raise RequestChanged("the ended batch's lease changed during release")
        lease_released = True

    catchup_request_id = None
    values: dict[str, Any] = {
        "catchup_trigger": None,
        "catchup_requested_at": None,
        "catchup_after_sequence": None,
        "outstanding_request_id": None,
        "outstanding_trigger": None,
        "outstanding_requested_at": None,
        "updated_at": now,
    }
    updated = await conn.execute(
        update(project_integration_schedules)
        .where(
            project_integration_schedules.c.project_id == project_id,
            project_integration_schedules.c.outstanding_request_id == state.request_id,
            project_integration_schedules.c.request_sequence == schedule["request_sequence"],
        )
        .values(**values)
    )
    if updated.rowcount != 1:
        raise RequestChanged("the schedule's request changed during release")
    await db.log_event(
        RELEASED_EVENT,
        project_id=project_id,
        payload=json.dumps(
            {
                "request_id": state.request_id,
                "batch_id": state.batch_id,
                "lifecycle": state.lifecycle,
                "verdict": state.verdict,
                "classification": state.reason,
                "reason": reason,
                "released_by": released_by,
                "lease_released": lease_released,
                "catchup_request_id": catchup_request_id,
                "at": now,
            }
        ),
        conn=conn,
    )
    row = (
        await conn.execute(
            select(project_integration_schedules).where(
                project_integration_schedules.c.project_id == project_id
            )
        )
    ).mappings().one()
    return RequestRelease(state.request_id, catchup_request_id, lease_released, dict(row))


def _foreign_lease(lease: dict[str, Any] | None, batch_id: str | None) -> tuple[dict[str, str], ...]:
    if lease is None or lease["batch_id"] == batch_id:
        return ()
    return ({
        "code": "lease",
        "detail": "the project lease is held by another batch",
        "ref": str(lease["batch_id"]),
    },)

EMPTY_SEAL_PREFIX = "integration-empty:"
_SWEEP_REQUEST = re.compile(r"integration-sweep:(?P<project>.+):(?P<sequence>[1-9][0-9]*)")


def empty_seal_id(request_id: str) -> str:
    """The ``batch_id`` an empty seal of *request_id* answers with."""
    return EMPTY_SEAL_PREFIX + request_id


def empty_seal_request(batch_id: str) -> tuple[str, str] | None:
    """``(project_id, request_id)`` an empty seal's id names, else ``None``."""
    if not batch_id.startswith(EMPTY_SEAL_PREFIX):
        return None
    request_id = batch_id[len(EMPTY_SEAL_PREFIX):]
    match = _SWEEP_REQUEST.fullmatch(request_id)
    return (match["project"], request_id) if match else None


async def request_ended_without_batch_on(conn, project_id: str, request_id: str) -> bool:
    """Whether *project_id*'s sweep request *request_id* ended with no batch row.

    Historical empty seals consumed their request without a row; a fenced
    release may also clear a request no seal answered. The request must have
    been minted (its sequence is the schedule's or older) and must no longer
    be outstanding. Retirement never creates another legacy sweep request.
    """
    match = _SWEEP_REQUEST.fullmatch(request_id)
    if match is None or match["project"] != project_id:
        return False
    schedule = (
        await conn.execute(
            select(
                project_integration_schedules.c.outstanding_request_id,
                project_integration_schedules.c.request_sequence,
            ).where(project_integration_schedules.c.project_id == project_id)
        )
    ).mappings().one_or_none()
    if (
        schedule is None
        or schedule["outstanding_request_id"] == request_id
        or int(match["sequence"]) > int(schedule["request_sequence"])
    ):
        return False
    batch_id = (
        await conn.execute(
            select(integration_batches.c.id).where(
                integration_batches.c.project_id == project_id,
                integration_batches.c.request_id == request_id,
            )
        )
    ).scalar_one_or_none()
    return batch_id is None


class IntegrationReleaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal[
        "released", "already_released", "empty", "wait", "stale", "invariant_error"
    ]
    project_id: str | None = None
    batch_id: str
    request_id: str | None = None
    catchup_request_id: str | None = None
    operation_id: str | None = None


class _CASLost(RuntimeError):
    pass


class IntegrationReleaseService:
    """Release one shipped train without coupling release to cleanup progress."""

    def __init__(self, db: Any):
        self.db = db

    async def release(self, batch_id: str, now: float) -> IntegrationReleaseResult:
        async with self.db._engine.connect() as conn:
            project_id = (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
            if project_id is None:
                return await self._release_empty_seal_on(conn, batch_id)
        try:
            return await self._release_locked(str(project_id), batch_id, now)
        except _CASLost:
            return await self._canonical_replay(str(project_id), batch_id)

    @staticmethod
    async def _release_empty_seal_on(conn: Any, batch_id: str) -> IntegrationReleaseResult:
        """Confirm an empty seal, which consumed its request and keeps no row.

        There is nothing to free: the seal already ended the request in the
        transaction that found the frontier empty.  Any other id with no row
        is ``stale``, as before.
        """
        named = empty_seal_request(batch_id)
        if named is None or not await request_ended_without_batch_on(conn, *named):
            return IntegrationReleaseResult(outcome="stale", batch_id=batch_id)
        project_id, request_id = named
        return IntegrationReleaseResult(
            outcome="empty", project_id=project_id, batch_id=batch_id, request_id=request_id
        )

    async def _release_locked(
        self, project_id: str, batch_id: str, now: float
    ) -> IntegrationReleaseResult:
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one_or_none()
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if project is None or batch is None or batch["project_id"] != project_id:
                return IntegrationReleaseResult(
                    outcome="stale", project_id=project_id, batch_id=batch_id
                )
            release_result = await self._one_for_update(
                conn,
                select(integration_release_results).where(
                    integration_release_results.c.batch_id == batch_id
                ),
            )
            if release_result is not None:
                return self._persisted_result(batch, release_result)
            if batch["lifecycle"] in ENDED_LIFECYCLES:
                return await self._release_ended_on(conn, project_id, dict(batch), now)

            revision = int(batch["current_revision"])
            candidate = await self._one_for_update(
                conn,
                select(integration_candidate_revisions).where(
                    integration_candidate_revisions.c.batch_id == batch_id,
                    integration_candidate_revisions.c.revision == revision,
                ),
            )
            publication = await self._one_for_update(
                conn,
                select(integration_candidate_publications).where(
                    integration_candidate_publications.c.batch_id == batch_id,
                    integration_candidate_publications.c.revision == revision,
                ),
            )
            lease = await self._one_for_update(
                conn,
                select(project_integration_leases).where(
                    project_integration_leases.c.project_id == project_id
                ),
            )
            operation = await self._one_for_update(
                conn,
                select(integration_repair_operations).where(
                    integration_repair_operations.c.batch_id == batch_id
                ),
            )
            stage = None
            if operation is not None:
                stage = await self._one_for_update(
                    conn,
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal == operation["active_stage"],
                    ),
                )
            intent = await self._one_for_update(
                conn,
                select(integration_promotion_intents).where(
                    integration_promotion_intents.c.intent_kind == "root",
                    integration_promotion_intents.c.root_batch_id == batch_id,
                    integration_promotion_intents.c.root_candidate_revision == revision,
                ),
            )
            root_mutations = await self._all_for_update(
                conn,
                select(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.batch_id == batch_id,
                    integration_candidate_ref_mutations.c.revision == revision,
                ),
            )
            members = await self._all_for_update(
                conn,
                select(integration_batch_members)
                .where(integration_batch_members.c.batch_id == batch_id)
                .order_by(integration_batch_members.c.ordinal),
            )
            results = await self._all_for_update(
                conn,
                select(integration_candidate_member_results)
                .where(
                    integration_candidate_member_results.c.batch_id == batch_id,
                    integration_candidate_member_results.c.revision == revision,
                )
                .order_by(integration_candidate_member_results.c.member_ordinal),
            )
            reservations = []
            if intent is not None:
                reservations = await self._all_for_update(
                    conn,
                    select(integration_root_intent_members)
                    .where(integration_root_intent_members.c.intent_id == intent["id"])
                    .order_by(integration_root_intent_members.c.member_ordinal),
                )
            receipts = await self._all_for_update(
                conn,
                select(task_delivery_receipts)
                .where(
                    task_delivery_receipts.c.batch_id == batch_id,
                    task_delivery_receipts.c.candidate_revision == revision,
                )
                .order_by(task_delivery_receipts.c.member_ordinal),
            )
            schedule = await self.db.lock_integration_schedule_on(
                conn,
                project_id=project_id,
                now=now,
                default_interval_seconds=300,
            )

            operation_id = operation["id"] if operation is not None else None
            if lease is None:
                if schedule["outstanding_request_id"] == batch["request_id"]:
                    # Only this release could have freed the request, and the lease
                    # it consumes is gone: free it now unless a write is unresolved.
                    released = await self._release_stale_request_on(
                        conn, project_id, schedule, now
                    )
                    if released is None:
                        return self._result("invariant_error", batch, operation_id)
                    return self._result(
                        "released", batch, operation_id, released.catchup_request_id
                    )
                return self._result(
                    "already_released",
                    batch,
                    operation_id,
                    self._replay_catchup(schedule, batch["request_id"]),
                )

            branch_owner = await self._one_for_update(
                conn,
                select(integration_branch_owners).where(
                    integration_branch_owners.c.repository_id == batch["repository_id"],
                    integration_branch_owners.c.ref == batch["integration_branch"],
                ),
            )
            if await self._has_unresolved_on(conn, batch_id, revision) or (
                branch_owner is not None
                and branch_owner["handoff_state"] == "handoff_pending"
            ):
                return self._result("wait", batch, operation_id)
            if not self._complete_shipping(
                batch,
                candidate,
                publication,
                lease,
                operation,
                stage,
                intent,
                root_mutations,
                members,
                results,
                reservations,
                receipts,
            ):
                return self._result("invariant_error", batch, operation_id)
            if schedule["outstanding_request_id"] != batch["request_id"]:
                return self._result("stale", batch, operation_id)

            catchup_request_id = None
            values: dict[str, Any] = {
                "catchup_trigger": None,
                "catchup_requested_at": None,
                "catchup_after_sequence": None,
                "outstanding_request_id": None,
                "outstanding_trigger": None,
                "outstanding_requested_at": None,
                "last_completed_sweep_at": now,
                "updated_at": now,
            }
            schedule_cas = await conn.execute(
                update(project_integration_schedules)
                .where(
                    project_integration_schedules.c.project_id == project_id,
                    project_integration_schedules.c.outstanding_request_id
                    == batch["request_id"],
                    project_integration_schedules.c.request_sequence
                    == schedule["request_sequence"],
                )
                .values(**values)
            )
            lease_cas = await conn.execute(
                delete(project_integration_leases).where(
                    project_integration_leases.c.project_id == project_id,
                    project_integration_leases.c.batch_id == batch_id,
                    project_integration_leases.c.owner_id == lease["owner_id"],
                    project_integration_leases.c.fence_token == lease["fence_token"],
                )
            )
            if schedule_cas.rowcount != 1 or lease_cas.rowcount != 1:
                raise _CASLost
            await conn.execute(
                insert(integration_release_results).values(
                    batch_id=batch_id,
                    project_id=project_id,
                    request_id=batch["request_id"],
                    operation_id=operation_id,
                    catchup_request_id=catchup_request_id,
                    released_at=now,
                )
            )
            return self._result(
                "released", batch, operation_id, catchup_request_id
            )

    async def _release_ended_on(
        self, conn: Any, project_id: str, batch: dict[str, Any], now: float
    ) -> IntegrationReleaseResult:
        """Free the request of a batch that stopped without shipping.

        An empty seal consumes its own request; an aborted one never did, and
        with no promotion to release it the schedule kept it forever.  Free it
        here when nothing can still write for the batch (``wait`` otherwise).
        The outcome still describes the batch: an aborted train is ``stale``,
        not released -- ``integration.schedule_request_released`` records that
        its request was freed.
        """
        outcome = "empty" if batch["lifecycle"] == "empty" else "stale"
        operation_id = (
            await conn.execute(
                select(integration_repair_operations.c.id).where(
                    integration_repair_operations.c.batch_id == batch["id"]
                )
            )
        ).scalar_one_or_none()
        schedule = await self.db.lock_integration_schedule_on(
            conn,
            project_id=project_id,
            now=now,
            default_interval_seconds=300,
        )
        if schedule["outstanding_request_id"] != batch["request_id"]:
            return self._result(outcome, batch, operation_id)
        released = await self._release_stale_request_on(conn, project_id, schedule, now)
        if released is None:
            return self._result("wait", batch, operation_id)
        return self._result(outcome, batch, operation_id, released.catchup_request_id)

    async def _release_stale_request_on(
        self, conn: Any, project_id: str, schedule: dict[str, Any], now: float
    ) -> RequestRelease | None:
        state = await classify_outstanding_request_on(
            conn, project_id, schedule, now=now, lock=True
        )
        if state.verdict != "stale":
            return None
        try:
            return await release_outstanding_request_on(
                self.db,
                conn,
                state,
                schedule=schedule,
                now=now,
                released_by="integration_release",
                reason=state.reason,
            )
        except RequestChanged as exc:
            raise _CASLost from exc

    async def _canonical_replay(
        self, project_id: str, batch_id: str
    ) -> IntegrationReleaseResult:
        async with self.db._engine.connect() as conn:
            batch = (
                await conn.execute(
                    select(integration_batches).where(integration_batches.c.id == batch_id)
                )
            ).mappings().one_or_none()
            operation_id = (
                await conn.execute(
                    select(integration_repair_operations.c.id).where(
                        integration_repair_operations.c.batch_id == batch_id
                    )
                )
            ).scalar_one_or_none()
            release_result = (
                await conn.execute(
                    select(integration_release_results).where(
                        integration_release_results.c.batch_id == batch_id
                    )
                )
            ).mappings().one_or_none()
            schedule = (
                await conn.execute(
                    select(project_integration_schedules).where(
                        project_integration_schedules.c.project_id == project_id
                    )
                )
            ).mappings().one_or_none()
        if batch is None:
            return IntegrationReleaseResult(outcome="stale", batch_id=batch_id)
        if batch["lifecycle"] in ENDED_LIFECYCLES:
            if schedule is not None and schedule["outstanding_request_id"] == batch["request_id"]:
                return self._result("wait", batch, operation_id)
            return self._result(
                "empty" if batch["lifecycle"] == "empty" else "stale", batch, operation_id
            )
        if release_result is not None:
            return self._persisted_result(batch, release_result)
        if schedule is None or schedule["outstanding_request_id"] == batch["request_id"]:
            return self._result("invariant_error", batch, operation_id)
        return self._result(
            "already_released",
            batch,
            operation_id,
            self._replay_catchup(schedule, batch["request_id"]),
        )

    @staticmethod
    async def _one_for_update(conn: Any, statement: Any) -> dict[str, Any] | None:
        row = (await conn.execute(statement.with_for_update())).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    async def _all_for_update(conn: Any, statement: Any) -> list[dict[str, Any]]:
        rows = (await conn.execute(statement.with_for_update())).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    async def _has_unresolved_on(conn: Any, batch_id: str, revision: int) -> bool:
        checks = (
            select(integration_candidate_ref_mutations.c.id).where(
                integration_candidate_ref_mutations.c.batch_id == batch_id,
                integration_candidate_ref_mutations.c.revision == revision,
                integration_candidate_ref_mutations.c.state == "reserved",
            ),
            select(integration_candidate_resolutions.c.id).where(
                integration_candidate_resolutions.c.batch_id == batch_id,
                integration_candidate_resolutions.c.revision == revision,
                integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
            ),
            select(integration_attestation_publications.c.id).where(
                integration_attestation_publications.c.batch_id == batch_id,
                integration_attestation_publications.c.revision == revision,
                integration_attestation_publications.c.state == "reserved",
            ),
        )
        for statement in checks:
            if (await conn.execute(statement.with_for_update())).scalar_one_or_none() is not None:
                return True
        return False

    @staticmethod
    def _complete_shipping(
        batch: Any,
        candidate: dict[str, Any] | None,
        publication: dict[str, Any] | None,
        lease: dict[str, Any],
        operation: dict[str, Any] | None,
        stage: dict[str, Any] | None,
        intent: dict[str, Any] | None,
        mutations: list[dict[str, Any]],
        members: list[dict[str, Any]],
        results: list[dict[str, Any]],
        reservations: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
    ) -> bool:
        root_main = [row for row in mutations if row["purpose"] == "root_main"]
        count = len(members)
        expected_ordinals = list(range(count))
        return bool(
            batch["lifecycle"] == "promoted"
            and batch["final_main_sha"] is not None
            and candidate is not None
            and candidate["state"] == "promoted"
            and candidate["head_sha"] == batch["tested_candidate_sha"]
            and publication is not None
            and publication["state"] == "pr_published"
            and publication["head_sha"] == candidate["head_sha"]
            and operation is not None
            and operation["target_kind"] == "batch"
            and operation["batch_id"] == batch["id"]
            and operation["episode_id"] == batch["id"]
            and operation["state"] == "completed"
            and stage is not None
            and stage["state"] == "passed"
            and intent is not None
            and intent["state"] == "committed"
            and intent["operation_key"] == operation["id"]
            and intent["prepared_sha"] == candidate["head_sha"]
            and intent["remote_evidence"]
            == {"kind": "authenticated_main", "remote_sha": batch["final_main_sha"]}
            and lease["batch_id"] == batch["id"]
            and lease["repository_id"] == batch["repository_id"]
            and lease["owner_id"] == intent["project_lease_owner_id"]
            and int(lease["fence_token"]) == int(intent["project_lease_fence_token"])
            and len(root_main) == 1
            and root_main[0]["state"] == "applied"
            and root_main[0]["desired_sha"] == candidate["head_sha"]
            and root_main[0]["remote_sha"] == candidate["head_sha"]
            and count > 0
            and [int(row["ordinal"]) for row in members] == expected_ordinals
            and [int(row["member_ordinal"]) for row in results] == expected_ordinals
            and [int(row["member_ordinal"]) for row in reservations] == expected_ordinals
            and [int(row["member_ordinal"]) for row in receipts] == expected_ordinals
            and all(row["result"] == "applied" for row in results)
            and [row["receipt_id"] for row in reservations] == [row["id"] for row in receipts]
        )

    @staticmethod
    def _replay_catchup(schedule: Any, released_request_id: str) -> str | None:
        current = schedule["outstanding_request_id"]
        return str(current) if current is not None and current != released_request_id else None

    @staticmethod
    def _result(
        outcome: str,
        batch: Any,
        operation_id: str | None,
        catchup_request_id: str | None = None,
    ) -> IntegrationReleaseResult:
        return IntegrationReleaseResult(
            outcome=outcome,
            project_id=batch["project_id"],
            batch_id=batch["id"],
            request_id=batch["request_id"],
            catchup_request_id=catchup_request_id,
            operation_id=operation_id,
        )

    @staticmethod
    def _persisted_result(batch: Any, result: Any) -> IntegrationReleaseResult:
        if (
            result["project_id"] != batch["project_id"]
            or result["request_id"] != batch["request_id"]
        ):
            return IntegrationReleaseResult(
                outcome="invariant_error",
                project_id=batch["project_id"],
                batch_id=batch["id"],
                request_id=batch["request_id"],
            )
        return IntegrationReleaseResult(
            outcome="already_released",
            project_id=result["project_id"],
            batch_id=result["batch_id"],
            request_id=result["request_id"],
            catchup_request_id=result["catchup_request_id"],
            operation_id=result["operation_id"],
        )


__all__ = ["IntegrationReleaseResult", "IntegrationReleaseService"]
