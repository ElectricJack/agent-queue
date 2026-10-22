"""Resource producer invalidations and the explicit typed reobservation bridge.

Producer identities describe mutations, never admission condition identities.
The latter are opaque and come only from a verified, persisted condition.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Text, cast, func, or_, select

RESOURCE_BLOCKERS = frozenset({
    "slot", "workspace", "provider", "backoff", "budget", "policy_hold",
    "branch_owner", "batch_hold",
})
# A pool observation is a new admission snapshot, including time-dependent
# predicates. Broad invalidation is deliberate; only typed reobservation decides.
RESOURCE_INVALIDATIONS = {
    "pool": RESOURCE_BLOCKERS,
    "config": RESOURCE_BLOCKERS,
    "workspace": frozenset({"slot", "workspace", "branch_owner"}),
    "provider": frozenset({"slot", "provider", "backoff", "budget"}),
}


def resource_source_key(blocker: str, identity: str) -> str:
    if blocker not in RESOURCE_BLOCKERS or not isinstance(identity, str) or not identity.strip():
        raise ValueError("a typed admission blocker and its opaque identity are required")
    return f"resource:{blocker}:{identity}"


def resource_invalidation(*, producer, identity, revision, outcome_ref, payload,
                          project_id=None, occurred_at=None):
    """Build an unconditional outbox write, using the owning mutation's revision."""
    if (not isinstance(producer, str) or producer not in RESOURCE_INVALIDATIONS
            or not isinstance(identity, str) or not identity):
        raise ValueError("a resource producer and mutation identity are required")
    return dict(
        source_key=f"resource_invalidation:{producer}:{identity}", generation=0,
        revision=revision, outcome_ref=outcome_ref, occurred_at=occurred_at,
        payload={"producer": producer, "identity": identity, "project_id": project_id,
                 "facts": payload},
    )


def _invalidation(event):
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    producer, identity = payload.get("producer"), payload.get("identity")
    if (not isinstance(producer, str) or producer not in RESOURCE_INVALIDATIONS
            or not isinstance(identity, str) or not identity):
        return None
    if (event.get("source_key") != f"resource_invalidation:{producer}:{identity}"
            or type(event.get("source_generation")) is not int
            or event["source_generation"] != 0
            or not isinstance(event.get("source_revision"), str)
            or not event["source_revision"]
            or type(event.get("seq")) is not int or event["seq"] <= 0):
        return None
    return payload


async def resource_invalidation_candidates(conn, event, *, after_id="", limit=200):
    """Page canonical, scoped subscriptions for one committed invalidation.

    These are candidates, never satisfaction evidence. The listener must page
    through all results and reobserve them through the registry/dispatcher.
    Event revisions are opaque replay keys, not a permanent condition equality.
    """
    from src.database.tables import wait_records, wait_source_events
    from src.waits.sources import payload_hash

    payload = _invalidation(event)
    if payload is None:
        return []
    if not 1 <= limit <= 1000:
        raise ValueError("resource invalidation page size must be 1..1000")
    condition = wait_records.c.condition
    context = condition["_verified"]
    blocker, identity = condition["blocker"].astext, condition["identity"].astext
    # Authenticate the bridge input against the durable row, including replay
    # revision and payload. A notification, stale copy or forged revision alone
    # can never enter the typed dispatch path.
    committed = select(wait_source_events.c.seq).where(
        wait_source_events.c.seq == event["seq"],
        wait_source_events.c.source_key == event["source_key"],
        wait_source_events.c.source_generation == 0,
        wait_source_events.c.source_revision == event["source_revision"],
        wait_source_events.c.payload_hash == payload_hash(dict(payload)),
        wait_source_events.c.payload == dict(payload),
    ).exists()
    stmt = select(wait_records).where(
        committed,
        wait_records.c.id > after_id, wait_records.c.condition_kind == "resource",
        wait_records.c.outcome == "pending", wait_records.c.verification_state == "verified",
        wait_records.c.source_generation == 0, wait_records.c.source_watermark_seq < event["seq"],
        condition["kind"].astext == "resource", condition["generation"].astext == "0",
        blocker.in_(RESOURCE_INVALIDATIONS[payload["producer"]]),
        func.jsonb_typeof(condition["identity"]) == "string", identity != "",
        wait_records.c.source_key == "resource:" + blocker + ":" + identity,
        context["project_id"].astext == wait_records.c.project_id,
        context["owner_kind"].astext == wait_records.c.owner_kind,
        context["owner_id"].astext == wait_records.c.owner_id,
        func.jsonb_typeof(context["source_class"]) == "string",
        context["source_class"].astext != "",
        or_(wait_records.c.session_id.is_(None),
            context["session_id"].astext == wait_records.c.session_id),
        or_(wait_records.c.claim_epoch.is_(None),
            context["claim_epoch"].astext == cast(wait_records.c.claim_epoch, Text)),
    )
    if payload.get("project_id") is not None:
        stmt = stmt.where(wait_records.c.project_id == payload["project_id"])
    rows = (await conn.execute(stmt.order_by(wait_records.c.id).limit(limit))).mappings().all()
    return [dict(row) for row in rows]


async def reobserve_resource_candidates(conn, candidates, *, verify):
    """Return fresh typed evidence only; the dispatcher owns outcome CAS/delivery.

    ``verify(conn, wait_row)`` must check the current owner/project/session fences
    and call registry.verify with a service Scope and Condition.from_record.
    Missing/stale predicates must return None or unsatisfied/unavailable evidence.
    The caller keeps its normal source/owner locking and outcome CAS protocol.
    """
    result = []
    for row in candidates:
        evidence = await verify(conn, row)
        if evidence is None:
            continue
        context = evidence.condition.context
        observation = evidence.observation
        if (evidence.source.identity != row["source_key"] or evidence.source.generation != 0
                or observation.state != "satisfied" or not observation.revision
                or not observation.result_ref):
            continue
        if any(context.get(name) != row[name] for name in (
            "project_id", "owner_kind", "owner_id", "session_id", "claim_epoch",
        )):
            continue
        result.append((row, evidence))
    return result
