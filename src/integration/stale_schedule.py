"""Recognise, and release, a train sweep request that nothing will ever end.

``project_integration_schedules.outstanding_request_id`` names the one sweep a
train project has in flight.  ``IntegrationScheduler.mark_due`` coalesces every
later trigger into it, and ``IntegrationReleaseService`` is the only thing that
clears it -- for a batch that *promoted* and still holds the project lease.
Every other ending left it in place: an operator abort, a development
``cancel_preserving``, a seal that never produced a batch.  On 2026-09-24
agent-queue's schedule still named the request of a batch aborted on
2026-09-09, so every flush answered ``coalesced`` and no sweep ran for two weeks.

This module is the single answer to "can this request still end on its own?".
The scheduler, the release service, both abort paths, the operator control and
``aq doctor --check integration.stale_schedule`` all read the same verdict:

``none``       no request is outstanding.
``active``     its batch is live, or promoted and still holding the lease that
               release consumes: the train will end the request itself.
``in_flight``  no batch yet, and the ``integration.sweep_due`` event is still
               waiting for a playbook to accept it.
``unsealed``   no batch, the event was accepted, and the seal's grace period has
               not passed.  Only an operator may release it.
``blocked``    nothing will end it, but its batch still carries unresolved
               external-write evidence, or another batch holds the project
               lease.  Releasing it could orphan a remote write.
``stale``      nothing will ever end it.  The scheduler releases it on its next
               pass.

Releasing never touches Git or the batch row.  It clears the schedule's request
(or turns a recorded catch-up into the next request, exactly as release does),
deletes the ended batch's own fenced lease, and records the reason as an
``integration.schedule_request_released`` event.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import delete, select, update

from src.database.tables import (
    integration_attestation_publications,
    integration_batches,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_operations,
    project_integration_leases,
    project_integration_schedules,
    projects,
)
from src.integration.live_operations import ACTIVE_OPERATION_STATES
from src.integration.outbox import enqueue_integration_event

logger = logging.getLogger(__name__)

Verdict = Literal["none", "active", "in_flight", "unsealed", "blocked", "stale"]

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
#: The root train's first step seals an accepted ``integration.sweep_due``.  An
#: hour is twelve default sweep intervals: long past a queued playbook run,
#: short enough that a seal which failed is recovered the same working session.
UNSEALED_GRACE_SECONDS = 60 * 60
#: Verdicts an operator may release.  The scheduler releases only ``stale``.
OPERATOR_RELEASABLE = frozenset({"stale", "unsealed"})
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
    #: The request's ``integration.sweep_due`` outbox row, when no batch exists.
    event: dict[str, Any] | None = None
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
    release the request passes it, a read-only report does not.
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
        verdict, reason, event = await _unsealed_verdict_on(conn, request_id, now)
        blockers = _foreign_lease(lease, None)
        if blockers and verdict in OPERATOR_RELEASABLE:
            return OutstandingRequest(
                project_id, "blocked", reason, event=event, blockers=blockers, **known
            )
        return OutstandingRequest(project_id, verdict, reason, event=event, **known)

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


async def _unsealed_verdict_on(
    conn: Any, request_id: str, now: float
) -> tuple[Verdict, str, dict[str, Any] | None]:
    row = (
        await conn.execute(
            select(
                integration_outbox.c.delivered_at,
                integration_outbox.c.attempts,
                integration_outbox.c.last_error,
            ).where(integration_outbox.c.id == request_id)
        )
    ).mappings().one_or_none()
    if row is None:
        return "stale", (
            "no batch was sealed and no integration.sweep_due event exists for the request, "
            "so nothing will ever seal it"
        ), None
    event = {
        "delivered_at": row["delivered_at"],
        "attempts": int(row["attempts"]),
        "last_error": row["last_error"],
    }
    if event["delivered_at"] is None:
        detail = "integration.sweep_due is still waiting for a playbook to accept it"
        if event["last_error"]:
            detail += f" ({event['attempts']} attempt(s); last error: {event['last_error']})"
        return "in_flight", detail, event
    age = max(0.0, now - float(event["delivered_at"]))
    if age < UNSEALED_GRACE_SECONDS:
        return "unsealed", (
            f"integration.sweep_due was accepted {int(age)}s ago but no batch is sealed yet"
        ), event
    return "stale", (
        f"integration.sweep_due was accepted {int(age)}s ago and no batch was ever sealed"
    ), event


def _foreign_lease(lease: dict[str, Any] | None, batch_id: str | None) -> tuple[dict[str, str], ...]:
    if lease is None or lease["batch_id"] == batch_id:
        return ()
    return ({
        "code": "lease",
        "detail": "the project lease is held by another batch",
        "ref": str(lease["batch_id"]),
    },)


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
    *state* was read from.  A recorded catch-up becomes the next request, as it
    does on release; otherwise the schedule is left with no request, and the
    trigger that follows schedules normally.
    """
    if state.verdict not in OPERATOR_RELEASABLE or state.request_id is None:
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

    project = (
        await conn.execute(
            select(
                projects.c.hierarchical_integration_desired_mode,
                projects.c.hierarchical_integration_draining,
            ).where(projects.c.id == project_id)
        )
    ).mappings().one_or_none()
    sequence = int(schedule["request_sequence"])
    values: dict[str, Any] = {
        "catchup_trigger": None,
        "catchup_requested_at": None,
        "catchup_after_sequence": None,
        "updated_at": now,
    }
    catchup_request_id = None
    if (
        schedule["catchup_trigger"] is not None
        and project is not None
        and not project["hierarchical_integration_draining"]
        and project["hierarchical_integration_desired_mode"] == "train"
    ):
        sequence += 1
        catchup_request_id = f"integration-sweep:{project_id}:{sequence}"
        values.update(
            request_sequence=sequence,
            outstanding_request_id=catchup_request_id,
            outstanding_trigger=schedule["catchup_trigger"],
            outstanding_requested_at=schedule["catchup_requested_at"],
        )
    else:
        values.update(
            outstanding_request_id=None,
            outstanding_trigger=None,
            outstanding_requested_at=None,
        )
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
    if catchup_request_id is not None:
        await enqueue_integration_event(
            conn,
            event_id=catchup_request_id,
            dedup_key=catchup_request_id,
            project_id=project_id,
            event_type="integration.sweep_due",
            payload={"project_id": project_id, "operation_id": catchup_request_id},
            available_at=now,
        )
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


async def release_stale_request(
    db: Any,
    project_id: str,
    *,
    now: float,
    released_by: str,
    reason: str,
    dry_run: bool = False,
    expected_request_id: str | None = None,
    releasable: frozenset[str] = frozenset({"stale"}),
) -> dict[str, Any]:
    """Classify, and unless *dry_run* release, one project's outstanding request.

    Opens its own transaction and takes the hierarchy lock first, the order
    every other schedule writer uses.  ``releasable`` widens what may be
    released; the operator control passes :data:`OPERATOR_RELEASABLE`.
    """
    async with db.immediate() as conn:
        await db.lock_hierarchy_project(conn, project_id)
        exists = (
            await conn.execute(select(projects.c.id).where(projects.c.id == project_id))
        ).scalar_one_or_none()
        if exists is None:
            return {"outcome": "not_found", "project_id": project_id, "dry_run": dry_run}
        schedule = (
            await conn.execute(
                select(project_integration_schedules)
                .where(project_integration_schedules.c.project_id == project_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        state = await classify_outstanding_request_on(
            conn, project_id, schedule, now=now, lock=not dry_run
        )
        result: dict[str, Any] = {
            "project_id": project_id,
            "request_id": state.request_id,
            "request_sequence": state.request_sequence,
            "batch_id": state.batch_id,
            "state": state.verdict,
            "reason": state.reason,
            "blockers": [dict(blocker) for blocker in state.blockers],
            "evidence": state.as_dict(),
            "dry_run": dry_run,
        }
        if expected_request_id is not None and expected_request_id != state.request_id:
            return {"outcome": "changed", **result}
        if state.verdict == "blocked":
            return {"outcome": "blocked", **result}
        if state.verdict not in releasable:
            return {"outcome": "nothing_to_clear", **result}
        if dry_run:
            return {"outcome": "would_clear", **result}
        released = await release_outstanding_request_on(
            db,
            conn,
            state,
            schedule=schedule,
            now=now,
            released_by=released_by,
            reason=reason,
        )
        return {"outcome": "cleared", **result, "release": released.as_dict()}


async def release_ended_batch_request(
    db: Any, batch_id: str, *, now: float, released_by: str, reason: str
) -> dict[str, Any] | None:
    """After *batch_id* ended without shipping, free its request if nothing else will.

    Runs in its own transaction once the ending has committed, so it takes the
    hierarchy lock in the usual order.  Best effort: a failure is logged and the
    scheduler frees the request on its next pass.  Returns ``None`` when the
    schedule no longer names the batch's request.
    """
    try:
        async with db._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        integration_batches.c.project_id, integration_batches.c.request_id
                    ).where(integration_batches.c.id == batch_id)
                )
            ).one_or_none()
        if row is None:
            return None
        result = await release_stale_request(
            db,
            str(row.project_id),
            now=now,
            released_by=released_by,
            reason=reason,
            expected_request_id=str(row.request_id),
        )
    except Exception:
        logger.exception("integration: could not free the request of ended batch %s", batch_id)
        return None
    if result["outcome"] in {"changed", "not_found"}:
        return None
    summary = {
        key: result[key]
        for key in ("outcome", "request_id", "state", "reason", "blockers", "release")
        if key in result
    }
    return summary


__all__ = [
    "ENDED_LIFECYCLES",
    "LIVE_LIFECYCLES",
    "OPERATOR_RELEASABLE",
    "RELEASED_EVENT",
    "UNSEALED_GRACE_SECONDS",
    "OutstandingRequest",
    "RequestChanged",
    "RequestRelease",
    "classify_outstanding_request_on",
    "release_ended_batch_request",
    "release_outstanding_request_on",
    "release_stale_request",
]
