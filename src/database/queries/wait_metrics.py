"""Read-only wait accounting shared by the sampler and ``wait graph --costs``.

All intervals are clipped to the report window. A requested suspension is not
a released slot: only a continuation's proven ``stopped_at`` starts that clock.
"""

from sqlalchemy import and_, func, or_, select

from src.database.tables import (
    admission_reservations as reservations,
    continuations,
    events,
    messages,
    projects,
    sessions,
    tasks,
    wait_escalations as escalations,
    wait_measurements as measurements,
    wait_priority_projection as priority,
    wait_records as waits,
)


def _scope(table, project_id):
    return table.c.project_id == project_id if project_id is not None else True


def _seconds(start, end, since, now):
    return func.greatest(0.0, func.least(end, now) - func.greatest(start, since))


async def slot_accounting(conn, *, now, window_seconds=3600, project_id=None):
    """Observed occupied/released seconds, without joining one-to-many rows."""
    since = now - window_seconds
    # A retry may create several reservations. The first actual grant ends
    # the released interval; a pending request splits condition from capacity.
    reservation = (
        select(
            reservations.c.continuation_id,
            func.min(reservations.c.created_at).label("requested"),
            func.min(reservations.c.granted_at).label("granted"),
        )
        .where(reservations.c.continuation_id.is_not(None))
        .group_by(
            reservations.c.continuation_id,
        )
        .subquery()
    )
    released_end = func.coalesce(reservation.c.granted, now)
    split = func.least(func.coalesce(reservation.c.requested, released_end), released_end)
    released = (
        (
            await conn.execute(
                select(
                    func.coalesce(
                        func.sum(_seconds(continuations.c.stopped_at, split, since, now)), 0
                    ).label("condition"),
                    func.coalesce(
                        func.sum(
                            _seconds(
                                func.greatest(continuations.c.stopped_at, split),
                                released_end,
                                since,
                                now,
                            )
                        ),
                        0,
                    ).label("capacity"),
                )
                .select_from(
                    continuations.join(waits, waits.c.id == continuations.c.wait_id).outerjoin(
                        reservation, reservation.c.continuation_id == continuations.c.id
                    )
                )
                .where(
                    _scope(waits, project_id),
                    continuations.c.stopped_at <= now,
                    released_end >= since,
                )
            )
        )
        .mappings()
        .one()
    )

    stopped = (
        select(
            continuations.c.wait_id,
            func.min(continuations.c.stopped_at).label("stopped"),
        )
        .group_by(continuations.c.wait_id)
        .subquery()
    )
    # Eviction changes sleep_decision in place. suspension_requested_at proves
    # that this interval began in place; never infer it from a ceiling anchor
    # (an anchor may predate this individual wait).
    was_in_place = waits.c.decision_inputs["suspension_requested_at"].as_float().is_not(None)
    occupied_end = func.coalesce(stopped.c.stopped, waits.c.cleared_at, waits.c.resolved_at, now)
    occupied = await conn.scalar(
        select(
            func.coalesce(
                func.sum(
                    _seconds(
                        waits.c.created_at,
                        occupied_end,
                        since,
                        now,
                    )
                ),
                0,
            )
        )
        .select_from(waits.outerjoin(stopped, stopped.c.wait_id == waits.c.id))
        .where(
            _scope(waits, project_id),
            waits.c.session_id.is_not(None),
            (waits.c.sleep_decision == "in_place") | was_in_place,
            waits.c.created_at <= now,
            occupied_end >= since,
        )
    )
    return {
        "in_place_slot_seconds_1h": float(occupied),
        "released_slot_seconds_1h": {k: float(v) for k, v in released.items()},
    }


async def wait_metrics_snapshot(conn, *, now, window_seconds=3600, project_id=None):
    """Grouped PostgreSQL reads; pre-migration databases have no wait group."""
    if await conn.scalar(select(func.to_regclass("wait_records"))) is None:
        return None
    since = now - window_seconds
    scope = _scope(waits, project_id)
    pending = and_(scope, waits.c.outcome == "pending", waits.c.created_at <= now)
    rows = (
        (
            await conn.execute(
                select(
                    waits.c.sleep_decision,
                    waits.c.condition_kind,
                    func.count().label("count"),
                    func.min(waits.c.created_at).label("oldest"),
                )
                .where(pending)
                .group_by(waits.c.sleep_decision, waits.c.condition_kind)
            )
        )
        .mappings()
        .all()
    )

    def count(pred):
        return sum(r["count"] for r in rows if pred(r))

    human = [r["oldest"] for r in rows if r["condition_kind"] == "human"]
    scheduler = (
        await conn.execute(
            select(
                waits.c.source_key,
                func.count().label("count"),
            )
            .where(pending, waits.c.origin == "scheduler")
            .group_by(waits.c.source_key)
        )
    ).all()
    incident_scope = select(waits.c.id).where(scope)
    incident_rows = (
        (
            await conn.execute(
                select(
                    func.count().filter(escalations.c.decided_at.is_(None)).label("pending"),
                    func.count()
                    .filter(escalations.c.raised_at.between(since, now))
                    .label("raised"),
                    func.count()
                    .filter(escalations.c.decided_at.between(since, now))
                    .label("decided"),
                    func.percentile_cont(0.5)
                    .within_group(escalations.c.decided_at - escalations.c.raised_at)
                    .filter(escalations.c.decided_at.between(since, now))
                    .label("seconds"),
                ).where(escalations.c.wait_id.in_(incident_scope))
            )
        )
        .mappings()
        .one()
    )
    event_rows = dict(
        (
            await conn.execute(
                select(events.c.event_type, func.count())
                .where(
                    _scope(events, project_id),
                    events.c.timestamp.between(since, now),
                    events.c.event_type.in_(
                        (
                            "wait.loop_detected",
                            "wait.evicted",
                            "wait.ceiling",
                            "wait.resume_launch_failed",
                            "wait.other_wake",
                        )
                    ),
                )
                .group_by(events.c.event_type)
            )
        ).all()
    )

    # Delivery, rather than satisfaction, counts wakes. The same outcome can
    # sit queued for capacity for hours before it reaches a process.
    wake_at = func.coalesce(
        waits.c.wake_delivered_at, waits.c.decision_inputs["delivery"]["acknowledged_at"].as_float()
    )
    supervisor_decision = waits.c.decision_inputs["supervisor_decision"]["decision"].astext
    wake_rows = (
        (
            await conn.execute(
                select(
                    waits.c.outcome,
                    supervisor_decision.label("decision"),
                    func.count().label("count"),
                )
                .where(scope, wake_at.between(since, now))
                .group_by(
                    waits.c.outcome,
                    supervisor_decision,
                )
            )
        )
        .mappings()
        .all()
    )
    wakes = dict.fromkeys(("outcome", "decision", "lift", "launch_failed", "other"), 0)
    for row in wake_rows:
        key = (
            "lift"
            if row["outcome"] == "lifted_by_supervisor"
            else "decision"
            if row["decision"] == "wake"
            else "outcome"
            if row["outcome"] in {"satisfied", "source_unavailable"}
            else "other"
        )
        wakes[key] += row["count"]
    wakes["launch_failed"] = event_rows.get("wait.resume_launch_failed", 0)
    wakes["other"] += event_rows.get("wait.other_wake", 0)
    # Count ordinary mail actually delivered during an unresolved wait. The
    # holding guard should make this zero; a violated guard must be visible.
    wakes["other"] += await conn.scalar(
        select(func.count(func.distinct(messages.c.id)))
        .select_from(
            waits.join(sessions, sessions.c.id == waits.c.session_id).join(
                messages,
                or_(
                    and_(
                        messages.c.to_kind == "session",
                        messages.c.to_id.in_((sessions.c.id, sessions.c.name)),
                    ),
                    and_(
                        messages.c.to_kind == "task",
                        waits.c.owner_kind == "task",
                        messages.c.to_id == waits.c.owner_id,
                    ),
                    and_(
                        messages.c.to_kind == "profile", messages.c.to_id == sessions.c.profile_id
                    ),
                ),
            )
        )
        .where(
            scope,
            messages.c.project_id == waits.c.project_id,
            messages.c.delivered_at.between(since, now),
            messages.c.delivered_at >= waits.c.created_at,
            messages.c.delivered_at < func.coalesce(waits.c.resolved_at, now),
            or_(
                messages.c.body_kind.is_(None),
                messages.c.body_kind.not_in(("wait_suspend", "wait_outcome", "wait_decision")),
            ),
        )
    )

    recent = (
        measurements.c.recorded_at.between(now - 30 * 86400, now),
        _scope(measurements, project_id),
    )
    cost = (
        measurements.c.teardown_seconds
        + measurements.c.setup_seconds
        + measurements.c.rebuild_seconds
    )
    cost_columns = [
        func.percentile_cont(q)
        .within_group(cost)
        .filter(
            measurements.c.hibernated.is_(True),
        )
        .label(name)
        for q, name in ((0.5, "resume_cost_p50"), (0.95, "resume_cost_p95"))
    ]
    cost_rows = (
        (
            await conn.execute(
                select(
                    measurements.c.project_id,
                    *cost_columns,
                    *[
                        func.percentile_cont(0.5).within_group(measurements.c[name]).label(name)
                        for name in (
                            "summary_tokens",
                            "resume_tokens",
                            "teardown_seconds",
                            "setup_seconds",
                            "rebuild_seconds",
                        )
                    ],
                )
                .where(*recent)
                .group_by(measurements.c.project_id)
            )
        )
        .mappings()
        .all()
    )
    durations = (
        (
            await conn.execute(
                select(
                    measurements.c.project_id,
                    measurements.c.condition_kind,
                    measurements.c.source_class,
                    func.percentile_cont(0.5)
                    .within_group(measurements.c.waited_seconds)
                    .label("wait_duration_p50"),
                )
                .where(*recent)
                .group_by(
                    measurements.c.project_id,
                    measurements.c.condition_kind,
                    measurements.c.source_class,
                )
            )
        )
        .mappings()
        .all()
    )
    overall = (await conn.execute(select(*cost_columns).where(*recent))).mappings().one()
    by_project = {
        pid: {"resume_cost_p50": None, "resume_cost_p95": None, "durations": []}
        for pid in (
            await conn.execute(
                select(projects.c.id).where(
                    projects.c.id == project_id if project_id else True,
                )
            )
        ).scalars()
    }
    for row in cost_rows:
        by_project[row["project_id"]].update({k: v for k, v in row.items() if k != "project_id"})
    for row in durations:
        by_project[row["project_id"]]["durations"].append(
            {k: v for k, v in row.items() if k != "project_id"}
        )

    reservation_rows = (
        (
            await conn.execute(
                select(
                    func.count()
                    .filter(reservations.c.state == "pending")
                    .label("reservations_pending"),
                    func.count()
                    .filter(
                        reservations.c.state.in_(("pending", "overdue")),
                        reservations.c.deadline_at < now,
                    )
                    .label("reservations_overdue"),
                    func.count()
                    .filter(
                        reservations.c.deadline_at.between(since, now),
                        func.coalesce(reservations.c.granted_at, now) > reservations.c.deadline_at,
                    )
                    .label("deadline_breaches_1h"),
                ).where(_scope(reservations, project_id))
            )
        )
        .mappings()
        .one()
    )
    donors = await conn.scalar(
        select(func.count())
        .select_from(
            priority.join(
                tasks,
                priority.c.task_id == tasks.c.id,
            )
        )
        .where(priority.c.donor_task_id.is_not(None), _scope(tasks, project_id))
    )
    unexplained = await conn.scalar(
        select(func.count())
        .select_from(waits)
        .where(
            pending,
            waits.c.verification_state != "verified",
        )
    )
    slots = await slot_accounting(
        conn, now=now, window_seconds=window_seconds, project_id=project_id
    )
    day = await slot_accounting(conn, now=now, window_seconds=86400, project_id=project_id)
    wait_seconds = await conn.scalar(
        select(
            func.coalesce(
                func.sum(
                    _seconds(
                        waits.c.created_at,
                        func.coalesce(waits.c.resolved_at, now),
                        since,
                        now,
                    )
                ),
                0,
            )
        ).where(
            scope,
            waits.c.session_id.is_not(None),
            waits.c.created_at <= now,
            func.coalesce(waits.c.resolved_at, now) >= since,
        )
    )
    overhead = measurements.c.summary_tokens + measurements.c.resume_tokens
    overhead_row = (
        (
            await conn.execute(
                select(
                    func.count().label("count"),
                    func.count(overhead).label("complete"),
                    func.sum(overhead).label("tokens"),
                ).where(
                    _scope(measurements, project_id),
                    measurements.c.hibernated.is_(True),
                    measurements.c.recorded_at.between(since, now),
                )
            )
        )
        .mappings()
        .one()
    )
    unmeasured = await conn.scalar(
        select(func.count())
        .select_from(waits)
        .where(
            scope,
            waits.c.sleep_decision == "hibernate",
            waits.c.created_at <= now,
            func.coalesce(waits.c.resolved_at, now) >= since,
            ~select(measurements.c.id).where(measurements.c.wait_id == waits.c.id).exists(),
        )
    )
    return {
        "pending_in_place": count(lambda r: r["sleep_decision"] == "in_place"),
        "pending_hibernated": count(lambda r: r["sleep_decision"] == "hibernate"),
        "pending_human": count(lambda r: r["condition_kind"] == "human"),
        "escalated": incident_rows["pending"],
        "scheduler_by_reason": dict(scheduler),
        **slots,
        "released_slot_seconds_24h": day["released_slot_seconds_1h"],
        **dict(overall),
        "by_project": by_project,
        "wakes_1h": wakes,
        "evicted_1h": event_rows.get("wait.evicted", 0),
        "ceiling_1h": event_rows.get("wait.ceiling", 0),
        "loops_1h": event_rows.get("wait.loop_detected", 0),
        "escalations_1h": incident_rows["raised"],
        "decisions_1h": incident_rows["decided"],
        "decision_seconds_p50": incident_rows["seconds"],
        "human_pending": count(lambda r: r["condition_kind"] == "human"),
        "human_age_max_seconds": now - min(human) if human else None,
        "priority": {"donors": donors, **dict(reservation_rows), "unexplained": unexplained},
        "wait_seconds_1h": float(wait_seconds),
        "overhead_tokens_1h": (
            float(overhead_row["tokens"] or 0)
            if not unmeasured and overhead_row["count"] == overhead_row["complete"]
            else None
        ),
        "tokens_saved_counterfactual": None,
    }
