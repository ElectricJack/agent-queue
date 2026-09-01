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
  idempotency guard against double delivery when a nudge and a
  prompt-boundary inject race (spec §4, §13).
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import and_, insert, or_, select, update

from src.database.tables import messages
from src.models import Message


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
    )


class MessageQueriesMixin:
    """Query mixin for ``messages``.  Expects ``self._engine``."""

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
            await conn.execute(
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
                )
            )
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
    ) -> list[Message]:
        """Undelivered, unarchived messages for one recipient.

        Ordered by ``priority`` ascending (lower delivers first) then
        ``created_at`` ascending — the delivery order the engine consumes.
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
                .values(read_at=time.time())
            )
            return result.rowcount > 0

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

        stmt = select(messages)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(messages.c.created_at.desc()).limit(limit)

        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [_row_to_message(r) for r in result.mappings().fetchall()]
