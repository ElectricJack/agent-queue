"""Current execution waits hold liveness and mail until outcome acknowledgement.

These queries are read-only. A terminal outcome is not an acknowledgement:
``cleared_at`` is set by the acknowledgement owner, after consuming held mail.
Registration-race audit rows already have ``cleared_at`` and grant no exemption.
"""

from sqlalchemy import and_, func, or_, select

from src.database.tables import agents, messages, sessions, tasks, wait_records


WAKE_BODY_KINDS = ("wait_outcome", "wait_suspend", "wait_decision")


def execution_waits(*, include_acknowledged=False):
    """Select only waits belonging to the current live process and claim.

Never infer authority from a task's WAITING status alone. Explicit stops, a
replacement process, a released claim and a manual pause all break the proof.
The registration receipt is immutable; delivery metadata is stored beside it.
"""
    delivery = wait_records.c.decision_inputs["delivery"]
    return select(wait_records).select_from(
        wait_records.join(sessions, sessions.c.id == wait_records.c.session_id)
        .outerjoin(tasks, and_(wait_records.c.owner_kind == "task",
                              tasks.c.id == wait_records.c.owner_id))
        .outerjoin(agents, agents.c.id == sessions.c.agent_id)
    ).where(
        wait_records.c.origin.in_(("agent", "human")),
        (or_(wait_records.c.cleared_at.is_(None),
             delivery["acknowledged_at"].astext.is_not(None))
         if include_acknowledged else wait_records.c.cleared_at.is_(None)),
        or_(wait_records.c.verification_state == "verified",
            wait_records.c.outcome == "source_unavailable"),
        delivery["instance_token"].astext == sessions.c.instance_token,
        sessions.c.state.in_(("starting", "running")),
        sessions.c.desired_state == "running",
        or_(
            and_(wait_records.c.owner_kind == "session",
                 wait_records.c.owner_id == sessions.c.id,
                 sessions.c.lifecycle == "named", sessions.c.profile_id == "supervisor",
                 or_(sessions.c.project_id == wait_records.c.project_id,
                     sessions.c.project_id.is_(None))),
            and_(wait_records.c.owner_kind == "task",
                 tasks.c.project_id == wait_records.c.project_id,
                 sessions.c.project_id == wait_records.c.project_id,
                 tasks.c.status.in_(("IN_PROGRESS", "WAITING")),
                 tasks.c.claim_epoch == wait_records.c.claim_epoch,
                 sessions.c.task_id == tasks.c.id,
                 tasks.c.assigned_agent_id == sessions.c.agent_id,
                 agents.c.current_task_id == tasks.c.id, agents.c.state == "BUSY",
                 or_(sessions.c.last_claim_epoch.is_(None),
                     sessions.c.last_claim_epoch == tasks.c.claim_epoch),
                 or_(sessions.c.lifecycle == "task",
                     and_(sessions.c.lifecycle == "pool", sessions.c.claim_phase == "active"))),
        ),
    )


def addressed_to_wait():
    """Recipient aliases shared by holding, acknowledgement and reply clocks."""
    return or_(
        and_(messages.c.to_kind == "task", wait_records.c.owner_kind == "task",
             messages.c.to_id == wait_records.c.owner_id),
        and_(messages.c.to_kind == "session",
             or_(messages.c.to_id == sessions.c.id, messages.c.to_id == sessions.c.name,
                 and_(sessions.c.lifecycle == "named", sessions.c.profile_id == "supervisor",
                      or_(messages.c.to_id == "supervisor-" + sessions.c.project_id,
                          and_(sessions.c.project_id.is_(None),
                               messages.c.to_id == "supervisor-global"))))),
    )


def message_is_held():
    """Correlated SQL guard, applied BEFORE mailbox limits and delivery writes."""
    waiting = execution_waits().where(addressed_to_wait()).correlate(messages).exists()
    return and_(waiting, or_(messages.c.body_kind.is_(None),
                            messages.c.body_kind.not_in(WAKE_BODY_KINDS)))


def reply_window_start():
    """Rebase reply clocks on ACK without rewriting message delivery history."""
    acknowledged = select(func.max(wait_records.c.cleared_at)).select_from(
        wait_records.join(sessions, sessions.c.id == wait_records.c.session_id),
    ).where(
        addressed_to_wait(),
        wait_records.c.created_at >= messages.c.delivered_at,
        wait_records.c.decision_inputs["delivery"]["acknowledged_at"].is_not(None),
        wait_records.c.cleared_at.is_not(None),
    ).correlate(messages).scalar_subquery()
    return func.greatest(messages.c.delivered_at, acknowledged)


async def active_wait_for(db, session, *, include_acknowledged=False):
    """One current-owner observation; callers may cache it for one reconcile pass."""
    async with db._engine.connect() as conn:
        row = (await conn.execute(execution_waits(include_acknowledged=include_acknowledged).where(
            sessions.c.id == session.id, sessions.c.instance_token == session.instance_token,
        ).order_by(wait_records.c.created_at.desc()).limit(1))).mappings().first()
        return dict(row) if row else None


def wake_already_delivered():
    return select(wait_records.c.id).where(
        messages.c.id == "wait:" + wait_records.c.id + ":outcome",
        messages.c.body_kind.in_(("wait_outcome", "wait_decision")),
        wait_records.c.wake_delivered_at.is_not(None),
    ).correlate(messages).exists()


def stale_wait_wake():
    """A v2 outcome cannot wake a replacement process or a released claim."""
    current = execution_waits().where(
        messages.c.id == "wait:" + wait_records.c.id + ":outcome",
        wait_records.c.outcome != "pending",
    ).correlate(messages).exists()
    suspend_current = execution_waits().where(
        messages.c.id == "wait:" + wait_records.c.id + ":human-suspend",
        wait_records.c.outcome == "pending", wait_records.c.condition_kind == "human",
        wait_records.c.sleep_decision == "hibernate",
    ).correlate(messages).exists()
    return and_(messages.c.body_kind.is_not(None), or_(
        and_(messages.c.body_kind.in_(("wait_outcome", "wait_decision")), ~current),
        and_(messages.c.body_kind == "wait_suspend",
             messages.c.id.like("wait:%:human-suspend"), ~suspend_current),
    ))
