"""PostgreSQL CTEs: source allocation, fence and NOTIFY without extra round trips."""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import Text, cast, column, func, literal, select, true, union_all, values
from sqlalchemy.dialects.postgresql import JSONB, insert

_SEQUENCE_LOCK = 0x415157414954


def _insert_sources(source_rows):
    from src.database.tables import wait_source_events

    # MATERIALIZED is required: the shared lock must run before identity
    # allocation, including for a multi-row insert and concurrent watermark.
    fence = select(func.pg_advisory_xact_lock_shared(_SEQUENCE_LOCK)).cte(
        "wait_source_fence",
    ).prefix_with("MATERIALIZED")
    names = ("id", "source_key", "source_generation", "source_revision", "occurred_at",
             "committed_at", "outcome_ref", "payload_hash", "payload")
    rows = select(
        literal("wse-") + cast(func.gen_random_uuid(), Text),
        *[source_rows.c[name] for name in names[1:]],
    ).select_from(source_rows.join(fence, true()))
    return insert(wait_source_events).from_select(names, rows).on_conflict_do_nothing(
        constraint="uq_wait_source_events_revision",
    ).returning(wait_source_events.c.seq).cte("wait_sources_inserted")


def _notified(inserted):
    # One notification per statement is enough: its sequence prompts a drain,
    # never supplies the drain's committed high watermark. No insert => silence.
    notified = select(func.pg_notify(
        "aq_wait_source", cast(func.max(inserted.c.seq), Text),
    )).select_from(inserted).having(func.count() > 0).cte(
        "wait_sources_notified",
    ).prefix_with("MATERIALIZED")
    return select(func.count()).select_from(notified).scalar_subquery() >= 0


def source_batch_statement(events):
    from src.database.tables import wait_source_events
    from src.waits.sources import payload_hash

    now = time.time()
    names = ("source_key", "source_generation", "source_revision", "occurred_at",
             "committed_at", "outcome_ref", "payload_hash", "payload")
    rows = []
    for event in events:
        if (not event["source_key"] or not event["revision"] or not event["outcome_ref"]
                or event["generation"] < 0):
            raise ValueError("source identity, nonnegative generation and revision are required")
        rows.append((event["source_key"], event["generation"], event["revision"],
                     now if event.get("occurred_at") is None else event["occurred_at"], now,
                     event["outcome_ref"], payload_hash(event["payload"]), event["payload"]))
    source_rows = values(*[
        column(name, wait_source_events.c[name].type) for name in names
    ]).data(rows).cte("wait_source_rows")
    inserted = _insert_sources(source_rows)
    return select(inserted.c.seq).where(_notified(inserted))


def _flat_payload(fields):
    """Hash canonical JSON for these flat string-valued status/dependency facts."""
    args = [item for key, value in fields.items() for item in (key, value)]
    payload = func.jsonb_build_object(*args, type_=JSONB)
    # Known flat string/null fields, with the same sorted keys and UTF-8 JSON
    # escaping as payload_hash. Do not apply this shortcut to nested payloads.
    canonical = literal("{")
    for index, (key, value) in enumerate(sorted(fields.items())):
        canonical += literal(("," if index else "") + json.dumps(key) + ":")
        canonical += func.coalesce(cast(func.to_jsonb(value), Text), "null")
    canonical += literal("}")
    digest = func.encode(func.sha256(func.convert_to(canonical, "UTF8")), "hex")
    return payload, digest


def task_transition_statement(statement, previous_status):
    """Keep a guarded task UPDATE, its task/edge events and NOTIFY one statement."""
    from src.database.tables import task_dependencies, tasks

    changed = statement.returning(*tasks.c).cte("wait_task_changed")
    revision = uuid.uuid4().hex
    now = time.time()
    payload, digest = _flat_payload({
        "project_id": changed.c.project_id, "task_id": changed.c.id,
        "previous_status": literal(previous_status), "status": changed.c.status,
    })
    task_rows = select(
        (literal("task:") + changed.c.id).label("source_key"),
        func.coalesce(changed.c.claim_epoch, 0).label("source_generation"),
        literal(revision).label("source_revision"), changed.c.updated_at.label("occurred_at"),
        literal(now).label("committed_at"),
        (literal("task:") + changed.c.id).label("outcome_ref"),
        digest.label("payload_hash"), payload.label("payload"),
    ).where(changed.c.status != previous_status)
    edge = task_dependencies
    edge_payload, edge_digest = _flat_payload({
        "task_id": edge.c.task_id, "depends_on_task_id": changed.c.id,
        "dep_type": edge.c.dep_type, "action": literal("status_changed"),
    })
    edge_rows = select(
        literal("dependency:") + edge.c.task_id + ":" + changed.c.id + ":" + edge.c.dep_type,
        func.coalesce(changed.c.claim_epoch, 0), literal(revision), changed.c.updated_at,
        literal(now), literal("task:") + edge.c.task_id, edge_digest, edge_payload,
    ).select_from(changed.join(edge, edge.c.depends_on_task_id == changed.c.id)).where(
        changed.c.status != previous_status,
    )
    source_rows = union_all(task_rows, edge_rows).cte("wait_source_rows")
    inserted = _insert_sources(source_rows)
    result = select(changed).where(_notified(inserted))
    if previous_status == "READY":
        from sqlalchemy import update
        from src.database.tables import admission_reservations

        cancelled = update(admission_reservations).where(
            admission_reservations.c.task_id == changed.c.id,
            changed.c.status != "READY",
            admission_reservations.c.state.in_(("pending", "overdue", "granted")),
        ).values(state="cancelled").cte("wait_reservations_cancelled")
        result = result.add_cte(cancelled)
    return result
