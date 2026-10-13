"""``messages`` table CRUD and delivery queries.

Implements docs/specs/implementation/supervisor-agent.md §4.  Mixed into the
SQLite and PostgreSQL adapters like every other ``queries/*.py`` module;
expects ``self._engine``.

House rules observed here:

- Timestamps are Float epoch seconds set by *this* layer, never by the
  database (spec §3.3 — no dialect-specific ``now()`` defaults).
- ``archive_after_inject`` is stored as integer 0/1, matching
  ``tasks.is_plan_subtask``.
- :meth:`MessageQueriesMixin.mark_delivered` is a compare-and-set: it only
  matches rows whose ``delivered_at IS NULL``.  That single predicate is the
  idempotency guard against duplicate delivery receipts when a recipient
  read and an inbox/prime inject race (spec §4, §13). Notification reservation
  has its own compare-and-set and leaves the body pending.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import and_, case, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import messages, projects, sessions, tasks
from src.models import Message


GLOBAL_SUPERVISOR_BACKLOG_THRESHOLD = 50


def _global_supervisor_mailbox():
    """Include historical logical addresses and the named launch's UUID."""
    global_sessions = select(sessions.c.id).where(
        sessions.c.profile_id == "supervisor", sessions.c.lifecycle == "named",
        sessions.c.project_id.is_(None),
    )
    return and_(
        messages.c.to_kind == "session",
        or_(messages.c.to_id.in_(("supervisor-global", "n-supervisor--global")),
            messages.c.to_id.in_(global_sessions)),
    )


def _global_backlog_snapshot():
    """Count and oldest identity come from the same database snapshot."""
    return select(messages.c.id, func.count().over().label("total")).where(
        _global_supervisor_mailbox(),
        messages.c.read_at.is_(None), messages.c.archived_at.is_(None),
    ).order_by(messages.c.created_seq.asc()).limit(1)


def _new_message_id() -> str:
    """Generate a ``msg-<uuid>`` primary key."""
    return f"msg-{uuid.uuid4().hex}"


def _row_to_message(row) -> Message:
    """Convert a ``messages`` row mapping to a :class:`~src.models.Message`."""
    return Message(
        id=row["id"],
        project_id=row["project_id"],
        from_kind=row["from_kind"],
        from_id=row["from_id"],
        to_kind=row["to_kind"],
        to_id=row["to_id"],
        body=row["body"],
        subject=row["subject"],
        thread_id=row["thread_id"],
        priority=row["priority"],
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
        read_at=row["read_at"],
        archive_after_inject=bool(row["archive_after_inject"]),
        archived_at=row["archived_at"],
        reply_to_id=row["reply_to_id"],
        via=row["via"],
        body_kind=row["body_kind"],
        pane_open=row["pane_open"],
        created_seq=row["created_seq"],
        last_notified_at=row["last_notified_at"],
        notification_attempts=row["notification_attempts"],
    )


class MessageQueriesMixin:
    """Query mixin for ``messages``.  Expects ``self._engine``."""

    async def _supervisor_recipient(self, conn, to_kind, to_id, project_id):
        """Resolve opt-in routing without changing the sender's project context."""
        recipient_project = None
        if to_kind == "profile" and to_id == "supervisor":
            recipient_project = project_id
        elif to_kind == "session":
            for prefix in ("supervisor-", "n-supervisor--"):
                if to_id.startswith(prefix):
                    suffix = to_id[len(prefix):]
                    if suffix != "global":
                        recipient_project = suffix
                    break
        if recipient_project:
            enabled = (await conn.execute(select(projects.c.supervisor_enabled).where(
                projects.c.id == recipient_project,
            ))).scalar_one_or_none()
            if enabled is False:
                return "session", "supervisor-global"
        return to_kind, to_id

    async def redirect_disabled_supervisor_messages(self, *, conn=None) -> int:
        """Route unread event notices and old backlog to the global supervisor.

        Internal producers can insert notices in their own transaction. Delivery
        and inbox commands apply this same policy before consuming those rows.
        Delivered but unread messages become pending again, so an abandoned
        supervisor turn cannot swallow a worker's report.
        """
        disabled_recipient = select(projects.c.id).where(
            projects.c.supervisor_enabled.is_(False),
            or_(
                and_(
                    messages.c.to_kind == "session",
                    projects.c.id != "global",
                    messages.c.to_id.in_([
                        "supervisor-" + projects.c.id,
                        "n-supervisor--" + projects.c.id,
                    ]),
                ),
                and_(
                    messages.c.to_kind == "profile", messages.c.to_id == "supervisor",
                    messages.c.project_id == projects.c.id,
                ),
            ),
        ).exists()
        stmt = update(messages).where(
            messages.c.read_at.is_(None), messages.c.archived_at.is_(None),
            disabled_recipient,
        ).values(to_kind="session", to_id="supervisor-global", delivered_at=None, via=None)
        if conn is not None:
            return (await conn.execute(stmt)).rowcount
        async with self._engine.begin() as owned:
            return (await owned.execute(stmt)).rowcount

    async def create_message(
        self,
        *,
        project_id: str | None,
        from_kind: str,
        from_id: str,
        to_kind: str,
        to_id: str,
        body: str,
        subject: str | None = None,
        thread_id: str | None = None,
        priority: int = 100,
        archive_after_inject: bool = False,
        reply_to_id: str | None = None,
        body_kind: str | None = None,
        pane_open: str | None = None,
    ) -> Message:
        """Insert one message row and return the materialised dataclass."""
        msg = Message(
            id=_new_message_id(),
            project_id=project_id,
            from_kind=from_kind,
            from_id=from_id,
            to_kind=to_kind,
            to_id=to_id,
            body=body,
            subject=subject,
            thread_id=thread_id,
            priority=priority,
            created_at=time.time(),
            archive_after_inject=archive_after_inject,
            reply_to_id=reply_to_id,
            body_kind=body_kind,
            pane_open=pane_open,
        )
        async with self._engine.begin() as conn:
            msg.to_kind, msg.to_id = await self._supervisor_recipient(
                conn, msg.to_kind, msg.to_id, msg.project_id,
            )
            result = await conn.execute(
                insert(messages).values(
                    id=msg.id,
                    project_id=msg.project_id,
                    from_kind=msg.from_kind,
                    from_id=msg.from_id,
                    to_kind=msg.to_kind,
                    to_id=msg.to_id,
                    thread_id=msg.thread_id,
                    subject=msg.subject,
                    body=msg.body,
                    priority=msg.priority,
                    created_at=msg.created_at,
                    delivered_at=None,
                    read_at=None,
                    archive_after_inject=int(msg.archive_after_inject),
                    archived_at=None,
                    reply_to_id=msg.reply_to_id,
                    via=None,
                    body_kind=msg.body_kind,
                    pane_open=msg.pane_open,
                ).returning(messages.c.created_seq)
            )
            msg.created_seq = result.scalar_one()
        return msg

    async def get_message(self, message_id: str) -> Message | None:
        """Fetch one message by id, or ``None``."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(messages).where(messages.c.id == message_id))
            row = result.mappings().fetchone()
            return _row_to_message(row) if row else None

    async def get_pending_messages(
        self,
        to_kind: str,
        to_id: str,
        *,
        limit: int = 50,
        notification_at: float | None = None,
        notification_delays: tuple[float, ...] = (),
        newest_first: bool = False,
    ) -> list[Message]:
        """Undelivered, unarchived messages for one recipient.

        Ordered by ``priority`` ascending (lower delivers first) then
        ``created_at`` ascending for body injection. Notification callers can
        select newest first within a priority to surface current work.

        The notification caller supplies its retry delays. Apply eligibility
        before the limit so unread mail in backoff cannot starve newer mail.
        """
        stmt = (
            select(messages)
            .where(
                and_(
                    messages.c.to_kind == to_kind,
                    messages.c.to_id == to_id,
                    messages.c.delivered_at.is_(None),
                    messages.c.archived_at.is_(None),
                )
            )
            .order_by(
                messages.c.priority.asc(),
                messages.c.created_at.desc() if newest_first else messages.c.created_at.asc(),
                messages.c.created_seq.desc() if newest_first else messages.c.created_seq.asc(),
            )
            .limit(limit)
        )
        if notification_at is not None and notification_delays:
            delay = case(
                *((messages.c.notification_attempts <= index + 1, value)
                  for index, value in enumerate(notification_delays[:-1])),
                else_=notification_delays[-1],
            )
            stmt = stmt.where(or_(
                messages.c.body_kind == "schedule_prompt",
                messages.c.last_notified_at.is_(None),
                messages.c.last_notified_at + delay <= notification_at,
            ))
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [_row_to_message(r) for r in result.mappings().fetchall()]

    async def expire_finished_task_notices(self) -> int:
        """Archive obsolete internal notices without fabricating a recipient read.

        These producers use exact subject prefixes followed by the full task id.
        Never infer a task from free-form user mail or expire an unfinished task.
        """
        prefixes = (
            ("task_recovery", "Task recovery: "),
            ("stale_open", "Stale BLOCKED task: "),
            ("stale_open", "Stale PAUSED task: "),
        )
        subject_task = case(*(
            (and_(messages.c.body_kind == kind, messages.c.subject.startswith(prefix)),
             func.substr(messages.c.subject, len(prefix) + 1))
            for kind, prefix in prefixes
        ))
        unfinished = select(tasks.c.id).where(
            tasks.c.id == subject_task,
            tasks.c.project_id == messages.c.project_id,
            tasks.c.status != "COMPLETED",
        ).exists()
        async with self._engine.begin() as conn:
            result = await conn.execute(update(messages).where(
                messages.c.from_kind == "system",
                messages.c.read_at.is_(None), messages.c.archived_at.is_(None),
                subject_task.is_not(None), ~unfinished,
            ).values(archived_at=time.time(), via="expired-task-notice"))
            return int(result.rowcount or 0)

    async def alert_global_supervisor_backlog(self) -> Message | None:
        """Notify the user once per oldest unread backlog identity, across restarts."""
        async with self._engine.begin() as conn:
            backlog = (await conn.execute(_global_backlog_snapshot())).mappings().first()
            if backlog is None or backlog["total"] <= GLOBAL_SUPERVISOR_BACKLOG_THRESHOLD:
                return None
            oldest, count = backlog["id"], backlog["total"]
            result = await conn.execute(pg_insert(messages).values(
                id=f"msg-{uuid.uuid5(uuid.NAMESPACE_URL, f'aq:global-backlog:{oldest}').hex}",
                project_id=None, from_kind="system", from_id="delivery-engine",
                to_kind="user", to_id="dashboard", priority=50,
                body_kind="delivery_alert", created_at=time.time(),
                subject="Global supervisor unread backlog",
                body=(f"The global supervisor has {count} unread messages "
                      f"(threshold {GLOBAL_SUPERVISOR_BACKLOG_THRESHOLD}); oldest is {oldest}. "
                      "Inspect its authentication and delivery path. "
                      "These messages remain unread; this alert does not acknowledge them."),
            ).on_conflict_do_nothing(index_elements=[messages.c.id]).returning(messages))
            row = result.mappings().first()
            return _row_to_message(row) if row else None

    async def get_consumed_messages(
        self,
        to_kind: str,
        to_id: str,
        *,
        limit: int = 50,
    ) -> list[Message]:
        """Rows this recipient already consumed — the re-read path.

        :meth:`get_pending_messages` is the delivery queue: it answers "what
        has not been handed over yet".  A worker whose inject consumed
        delivery before its own output rendered (a crashed harness parser, a
        truncated pane, a killed turn) has a pending queue of zero and no way
        back to the bodies it already burned.  This is that way back.

        A row counts as consumed once ``delivered_at`` or ``archived_at`` is
        set.  Archived rows are included deliberately: ``archive_after_inject``
        sweeps them in the same pass that marks them delivered, so those are
        precisely the bodies a crashed inject hides from every other read.

        Ordered like the pending queue (``priority`` then ``created_at``) so a
        re-read reads in the same order the worker would have seen them.
        """
        stmt = (
            select(messages)
            .where(
                and_(
                    messages.c.to_kind == to_kind,
                    messages.c.to_id == to_id,
                    or_(
                        messages.c.delivered_at.is_not(None),
                        messages.c.archived_at.is_not(None),
                    ),
                )
            )
            .order_by(messages.c.priority.asc(), messages.c.created_at.asc())
            .limit(limit)
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [_row_to_message(r) for r in result.mappings().fetchall()]

    async def get_pending_recipients(self) -> list[tuple[str, str, str | None]]:
        """Distinct ``(to_kind, to_id, project_id)`` with pending messages.

        Read-only — the delivery engine enumerates these to know whose
        inboxes to work each cascade pass without paging every row through
        Python. Rows are grouped so a recipient still receives one nudge
        even if they have several pending messages across projects.
        """
        stmt = (
            select(messages.c.to_kind, messages.c.to_id, messages.c.project_id)
            .where(
                and_(
                    messages.c.delivered_at.is_(None),
                    messages.c.archived_at.is_(None),
                )
            )
            .distinct()
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [(r[0], r[1], r[2]) for r in result.fetchall()]

    async def mark_delivered(self, message_id: str, *, via: str | None = None) -> bool:
        """Compare-and-set the delivery stamp.

        Returns ``True`` only when *this* call claimed the message — the
        ``delivered_at IS NULL AND archived_at IS NULL`` predicate makes
        the second (racing) caller return ``False`` without overwriting
        the first delivery's stamp or ``via`` marker, and prevents an
        archived row from ever being claimed for delivery (spec §11.1
        binding resolution).
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(messages)
                .where(
                    and_(
                        messages.c.id == message_id,
                        messages.c.delivered_at.is_(None),
                        messages.c.archived_at.is_(None),
                    )
                )
                .values(delivered_at=time.time(), via=via)
            )
            return result.rowcount > 0

    async def mark_read(self, message_id: str) -> bool:
        """Compare-and-set the read stamp.  ``False`` if already read."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(messages)
                .where(
                    and_(
                        messages.c.id == message_id,
                        messages.c.read_at.is_(None),
                    )
                )
                .values(
                    read_at=time.time(),
                    delivered_at=func.coalesce(messages.c.delivered_at, time.time()),
                )
            )
            return result.rowcount > 0

    async def claim_message_notification(
        self, message_id: str, *, now: float, retry_delay: float,
    ) -> bool:
        """Reserve an attempt before touching the terminal, without consuming mail."""
        async with self._engine.begin() as conn:
            result = await conn.execute(update(messages).where(
                messages.c.id == message_id,
                messages.c.delivered_at.is_(None),
                messages.c.read_at.is_(None),
                messages.c.archived_at.is_(None),
                or_(messages.c.last_notified_at.is_(None),
                    messages.c.last_notified_at <= now - retry_delay),
            ).values(
                last_notified_at=now,
                notification_attempts=messages.c.notification_attempts + 1,
                via="nudge",
            ))
            return result.rowcount > 0

    async def escalate_unread_message(self, message_id: str) -> Message | None:
        """Copy an unanswered notification to supervision or the user exactly once.

        Lock the source against a racing acknowledgment; deterministic alert IDs
        make retries and daemon restarts idempotent. Never consume the source.
        """
        async with self._engine.begin() as conn:
            source = (await conn.execute(select(messages).where(
                messages.c.id == message_id,
                messages.c.delivered_at.is_(None),
                messages.c.read_at.is_(None),
                messages.c.archived_at.is_(None),
                messages.c.notification_attempts >= 3,
            ).with_for_update())).mappings().first()
            if source is None or source["body_kind"] == "delivery_alert":
                return None
            global_recipient = (await conn.execute(select(messages.c.id).where(
                messages.c.id == message_id, _global_supervisor_mailbox(),
            ))).scalar_one_or_none() is not None
            alert_identity = f"aq:delivery-alert:{message_id}"
            if global_recipient:
                backlog = (await conn.execute(_global_backlog_snapshot())).mappings().one()
                # Share the threshold alert's identity: a broken global mailbox
                # cannot generate one user alert for every unread source row.
                alert_identity = f"aq:global-backlog:{backlog['id']}"
            result = await conn.execute(pg_insert(messages).values(
                id=f"msg-{uuid.uuid5(uuid.NAMESPACE_URL, alert_identity).hex}",
                project_id=source["project_id"],
                from_kind="system", from_id="delivery-engine",
                to_kind="user" if global_recipient else "session",
                to_id="dashboard" if global_recipient else "supervisor-global",
                priority=source["priority"],
                body_kind="delivery_alert", created_at=time.time(),
                subject=f"Unread message: {source['subject'] or message_id}",
                body=(f"Message {message_id} to {source['to_kind']}:{source['to_id']} "
                      f"has not been read after {source['notification_attempts']} notification "
                      "attempts. Inspect the recipient's authentication and delivery path. "
                      "The original remains pending. Original body:\n\n" + source["body"]),
            ).on_conflict_do_nothing(index_elements=[messages.c.id]).returning(messages))
            row = result.mappings().first()
            return _row_to_message(row) if row else None

    async def get_collaboration_read_messages(
        self, thread_id: str, *, project_id: str, task_id: str, message_ids: list[str],
    ) -> list[Message]:
        """Recipient copies whose bodies or thread notices were returned by a read."""
        copies = [or_(messages.c.id == key, messages.c.id.startswith(key + ":"))
                  for key in message_ids]
        visible = messages.c.body_kind.in_(("collaboration_invite", "collaboration_closed"))
        if copies:
            visible = or_(visible, and_(messages.c.body_kind == "collaboration", or_(*copies)))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(select(messages).where(
                messages.c.project_id == project_id, messages.c.thread_id == thread_id,
                messages.c.to_kind == "task", messages.c.to_id == task_id,
                messages.c.read_at.is_(None), messages.c.archived_at.is_(None), visible,
            ))).mappings().all()
            return [_row_to_message(row) for row in rows]

    async def archive_messages(self, message_ids: list[str]) -> int:
        """Archive the given messages.  Returns the number newly archived.

        Already-archived rows are left untouched and are not counted, so
        the ``archive_after_inject`` sweep archives each row exactly once.
        """
        if not message_ids:
            return 0
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(messages)
                .where(
                    and_(
                        messages.c.id.in_(message_ids),
                        messages.c.archived_at.is_(None),
                    )
                )
                .values(archived_at=time.time())
            )
            return result.rowcount

    async def has_reply_covering_message(self, msg: Message) -> bool:
        """An exact reply, or a fallback for a newer delivery in this conversation.

        A transcript tail answers a batch, not each older inbox row separately.
        Keep that coverage in persisted reply links (including archived history),
        rather than a per-pass cache or a truncated recent-message scan. A later
        incoming delivery remains eligible even if its answer has identical text.
        """
        reply = messages.alias("reply")
        covered = False
        if msg.delivered_at is not None:
            covered = and_(
                reply.c.via == "transcript_tail",
                messages.c.project_id == msg.project_id,
                messages.c.thread_id == msg.thread_id,
                messages.c.from_kind == msg.from_kind,
                messages.c.from_id == msg.from_id,
                messages.c.to_kind == msg.to_kind,
                messages.c.to_id == msg.to_id,
                messages.c.delivered_at >= msg.delivered_at,
            )
        stmt = select(reply.c.id).select_from(
            messages.join(reply, reply.c.reply_to_id == messages.c.id)
        ).where(or_(reply.c.reply_to_id == msg.id, covered)).limit(1)
        async with self._engine.begin() as conn:
            return (await conn.execute(stmt)).first() is not None

    async def list_messages(
        self,
        *,
        project_id: str | None = None,
        system_only: bool = False,
        thread_id: str | None = None,
        to_kind: str | None = None,
        to_id: str | None = None,
        include_archived: bool = False,
        since: float | None = None,
        limit: int = 100,
        session_addresses: list[str] | None = None,
    ) -> list[Message]:
        """List messages newest-first; system_only selects NULL-project records.

        Omitting project_id still means all projects, including system records.

        ``since`` is an exclusive lower bound on ``created_at`` — the
        polling form ``aq chat --once`` uses it to fetch only what arrived
        after its own outbound message.
        """
        conditions = []
        if system_only:
            conditions.append(messages.c.project_id.is_(None))
        elif project_id:
            conditions.append(messages.c.project_id == project_id)
        if thread_id:
            conditions.append(messages.c.thread_id == thread_id)
        if to_kind:
            conditions.append(messages.c.to_kind == to_kind)
        if to_id:
            conditions.append(messages.c.to_id == to_id)
        if not include_archived:
            conditions.append(messages.c.archived_at.is_(None))
        if since is not None:
            conditions.append(messages.c.created_at > since)

        if session_addresses is not None:
            conditions.append(or_(
                and_(messages.c.to_kind == "session", messages.c.to_id.in_(session_addresses)),
                and_(messages.c.from_kind == "session", messages.c.from_id.in_(session_addresses)),
            ))

        stmt = select(messages)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(messages.c.created_at.desc()).limit(limit)

        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [_row_to_message(r) for r in result.mappings().fetchall()]
