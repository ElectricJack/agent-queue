"""Durable operator conversations with the global supervisor.

Implements the persistence half of the Discord mention-routing spec §4.1–§4.2
(``~/.agent-queue/vault/projects/agent-queue/specs/
2026-09-24-discord-mention-routing-to-the-supervisor.md``).  Mixed into the
PostgreSQL adapter like every other ``queries/*.py`` module; expects
``self._engine`` and ``self.immediate()``.

House rules observed here:

- **Durable before visible.**  Accepting an input writes the conversation, the
  input and the supervisor's ``messages`` row in one transaction; nothing in
  this module talks to a transport.
- **Replay-safe.**  ``(transport, external_message_id)`` is unique across
  inputs and outlives the text (the 90-day tombstone), so a gateway retry,
  a history backfill overlap or a restart replay returns the original rows and
  writes nothing.  Replies are keyed by their deterministic message id.
- Timestamps are Float epoch seconds supplied by the caller (``now=``) or set
  here, never by the database.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import and_, case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from src.conversations.limits import AUTHOR_WINDOW_LIMIT, CHANNEL_WINDOW_LIMIT, WINDOW_SECONDS
from src.database.tables import (
    conversation_backfill_cursors,
    conversation_inputs,
    conversation_intake_gaps,
    messages,
    supervisor_conversations,
)

CONVERSATION_STATES = ("opening", "open", "closed", "delivery_blocked")
INPUT_STATES = ("accepted", "answered", "expired", "revoked")
INPUT_SOURCES = frozenset({"gateway", "backfill", "replay", "test"})
GAP_REASONS = frozenset({"cursor_expired", "history_forbidden", "pass_cap"})
#: Mirrors ``ck_conversation_inputs_char_count`` and
#: ``src.conversations.limits.MAX_INPUT_CHARS``.
MAX_INPUT_CHARS = 4000
#: Conversations an idle sweep may close.  ``opening`` is left alone: its
#: thread-open delivery is still owned by the outbox.
IDLE_CLOSABLE_STATES = ("open", "delivery_blocked")

SUPERVISOR_RECIPIENT = "supervisor-global"
_SUPERVISOR_PRIORITY = 50
_UNIQUE_INPUT = "uq_conversation_inputs_external"


class ConversationError(Exception):
    """Base class for refusals raised by the conversation query layer."""


class ConversationNotFound(ConversationError, LookupError):
    """The named conversation or input does not exist."""


class ConversationConflict(ConversationError, ValueError):
    """Identifiers that must agree do not (e.g. an input from another conversation)."""


class ConversationStateError(ConversationError):
    """The row's state does not allow the requested write."""


class ConversationClosed(ConversationStateError):
    """The conversation is closed; a new mention must open a fresh one."""


class ConversationRateLimited(ConversationError):
    """The author or channel has spent its durable sliding-window quota."""

    def __init__(self, scope: str):
        self.scope = scope
        super().__init__(f"conversation {scope} rate limit reached")


def _require_nonempty(values: Mapping[str, Any], names: Sequence[str]) -> None:
    empty = [name for name in names if values.get(name) is None or values.get(name) == ""]
    if empty:
        raise ValueError("required non-empty fields: " + ", ".join(sorted(empty)))


def _now(now: float | None) -> float:
    return float(now if now is not None else time.time())


def _row(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ConversationQueriesMixin:
    """Persistence API for ``supervisor_conversations`` and its satellites."""

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------

    async def accept_conversation_input(
        self,
        *,
        transport: str,
        guild_id: str,
        channel_id: str,
        external_message_id: str,
        external_root_message_id: str,
        external_thread_id: str | None,
        author_id: str,
        verified_actor: str,
        text: str,
        audience: list[str],
        source: str,
        received_at: float,
        conversation_id: str | None,
        brief: str | Callable[[str, str], str],
        supervisor_recipient: str = SUPERVISOR_RECIPIENT,
        now: float | None = None,
        enforce_limits: bool = False,
    ) -> dict[str, Any]:
        """Persist one verified operator message and queue its supervisor notice.

        One transaction writes (a) the conversation -- a new ``opening`` row
        when *conversation_id* is ``None``, otherwise the existing row, locked
        -- (b) the ``conversation_inputs`` row and (c) the deterministic
        ``msg-<input id>`` message to ``session:<supervisor_recipient>`` in
        the conversation's thread.

        A replay of ``(transport, external_message_id)`` returns the original
        rows with ``"created": False`` and writes nothing, whatever text the
        replay carries: an edit is not a new instruction, and expired text
        must not turn back into fresh work.  Concurrent accepts of one
        message serialise on the conversation row (or its root uniqueness),
        so exactly one of them creates.

        Raises :class:`ConversationNotFound` for an unknown *conversation_id*,
        :class:`ConversationClosed` for a closed one, and ``ValueError`` for
        empty, oversize or unknown-source input.

        Returns ``{"created", "conversation", "input", "supervisor_message_id"}``.

        A callable *brief* receives the allocated conversation/input ids and
        renders within this transaction. With *enforce_limits*, author and
        channel quota checks are serialized with the insert; the window uses
        acceptance time, so old backfill messages still spend current quota.
        """
        _require_nonempty(
            {
                "transport": transport,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "external_message_id": external_message_id,
                "external_root_message_id": external_root_message_id,
                "author_id": author_id,
                "verified_actor": verified_actor,
                "text": text,
                "brief": brief,
                "supervisor_recipient": supervisor_recipient,
            },
            (
                "transport",
                "guild_id",
                "channel_id",
                "external_message_id",
                "external_root_message_id",
                "author_id",
                "verified_actor",
                "text",
                "brief",
                "supervisor_recipient",
            ),
        )
        if len(text) > MAX_INPUT_CHARS:
            raise ValueError(f"conversation input exceeds {MAX_INPUT_CHARS} characters")
        if source not in INPUT_SOURCES:
            raise ValueError(f"unknown conversation input source {source!r}")
        stamp = _now(now)
        try:
            return await self._accept_conversation_input_once(
                transport=transport,
                guild_id=guild_id,
                channel_id=channel_id,
                external_message_id=external_message_id,
                external_root_message_id=external_root_message_id,
                external_thread_id=external_thread_id,
                author_id=author_id,
                verified_actor=verified_actor,
                text=text,
                audience=list(audience),
                source=source,
                received_at=float(received_at),
                conversation_id=conversation_id,
                brief=brief,
                supervisor_recipient=supervisor_recipient,
                now=stamp,
                enforce_limits=enforce_limits,
            )
        except IntegrityError as exc:
            # The lock order above makes this unreachable for one message, but
            # a follow-up and a fresh mention racing on the same external id
            # would meet only here.  The loser's transaction rolled back whole.
            if _UNIQUE_INPUT not in str(exc.orig):
                raise
            replay = await self._conversation_input_replay(transport, external_message_id)
            if replay is None:
                raise
            return replay

    async def _accept_conversation_input_once(
        self,
        *,
        transport: str,
        guild_id: str,
        channel_id: str,
        external_message_id: str,
        external_root_message_id: str,
        external_thread_id: str | None,
        author_id: str,
        verified_actor: str,
        text: str,
        audience: list[str],
        source: str,
        received_at: float,
        conversation_id: str | None,
        brief: str | Callable[[str, str], str],
        supervisor_recipient: str,
        now: float,
        enforce_limits: bool,
    ) -> dict[str, Any]:
        async with self.immediate() as conn:
            if enforce_limits:
                # Shared across handlers/restarts and gateway/backfill. Always
                # acquire author then channel to keep the lock order stable.
                for key in (f"conversation:author:{author_id}", f"conversation:channel:{channel_id}"):
                    await conn.execute(
                        select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
                    )
                existing = await _input_by_external(conn, transport, external_message_id)
                if existing is not None:
                    return await _replay(conn, existing)
                for scope, column, value, limit in (
                    ("author", conversation_inputs.c.author_id, author_id, AUTHOR_WINDOW_LIMIT),
                    ("channel", conversation_inputs.c.channel_id, channel_id, CHANNEL_WINDOW_LIMIT),
                ):
                    total = await conn.scalar(
                        select(func.count()).select_from(conversation_inputs).where(
                            conversation_inputs.c.created_at >= now - WINDOW_SECONDS,
                            conversation_inputs.c.state != "revoked",
                            column == value,
                        )
                    )
                    if total >= limit:
                        raise ConversationRateLimited(scope)
            if conversation_id is None:
                existing = await _input_by_external(conn, transport, external_message_id)
                if existing is not None:
                    return await _replay(conn, existing)
                new_id = f"conv-{uuid.uuid4()}"
                inserted = (
                    (
                        await conn.execute(
                            pg_insert(supervisor_conversations)
                            .values(
                                id=new_id,
                                transport=transport,
                                guild_id=guild_id,
                                channel_id=channel_id,
                                external_root_message_id=external_root_message_id,
                                external_thread_id=external_thread_id,
                                thread_id=f"conversation:{new_id}",
                                created_by=verified_actor,
                                audience=audience,
                                state="opening",
                                created_at=now,
                                updated_at=now,
                                closed_at=None,
                            )
                            .on_conflict_do_nothing(
                                index_elements=["transport", "external_root_message_id"]
                            )
                            .returning(supervisor_conversations)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if inserted is not None:
                    conversation = dict(inserted)
                else:
                    # Another accept owns this root.  ON CONFLICT waited for it
                    # to commit, so the row and its input are visible now.
                    conversation = dict(
                        (
                            await conn.execute(
                                select(supervisor_conversations)
                                .where(
                                    supervisor_conversations.c.transport == transport,
                                    supervisor_conversations.c.external_root_message_id
                                    == external_root_message_id,
                                )
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one()
                    )
            else:
                locked = (
                    (
                        await conn.execute(
                            select(supervisor_conversations)
                            .where(supervisor_conversations.c.id == conversation_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if locked is None:
                    raise ConversationNotFound(f"conversation {conversation_id} does not exist")
                conversation = dict(locked)
                if conversation["transport"] != transport:
                    raise ConversationConflict(
                        f"conversation {conversation_id} belongs to transport "
                        f"{conversation['transport']!r}, not {transport!r}"
                    )

            # Checked under the conversation lock: a concurrent accept of the
            # same message has committed by now and is a replay.
            existing = await _input_by_external(conn, transport, external_message_id)
            if existing is not None:
                return await _replay(conn, existing)
            if conversation["state"] == "closed":
                raise ConversationClosed(f"conversation {conversation['id']} is closed")

            input_id = f"cinput-{uuid.uuid4()}"
            supervisor_message_id = f"msg-{input_id}"
            # The renderer needs the server-allocated ids. Render before any
            # message is committed so the notification is never incomplete.
            rendered_brief = brief(conversation["id"], input_id) if callable(brief) else brief
            if not isinstance(rendered_brief, str) or not rendered_brief:
                raise ValueError("brief must render non-empty text")
            await conn.execute(
                pg_insert(messages).values(
                    id=supervisor_message_id,
                    project_id=None,
                    from_kind="user",
                    from_id=f"discord:{author_id}",
                    to_kind="session",
                    to_id=supervisor_recipient,
                    thread_id=conversation["thread_id"],
                    subject=f"Discord conversation {conversation['id']}",
                    body=rendered_brief,
                    priority=_SUPERVISOR_PRIORITY,
                    created_at=now,
                    delivered_at=None,
                    read_at=None,
                    archive_after_inject=0,
                    archived_at=None,
                    reply_to_id=None,
                    via=None,
                    body_kind="conversation_input",
                    pane_open=None,
                )
            )
            item = (
                (
                    await conn.execute(
                        pg_insert(conversation_inputs)
                        .values(
                            id=input_id,
                            conversation_id=conversation["id"],
                            transport=transport,
                            external_message_id=external_message_id,
                            verified_actor=verified_actor,
                            author_id=author_id,
                            channel_id=channel_id,
                            text=text,
                            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                            char_count=len(text),
                            received_at=received_at,
                            source=source,
                            state="accepted",
                            supervisor_message_id=supervisor_message_id,
                            reply_message_id=None,
                            delay_notified_at=None,
                            text_expired_at=None,
                            created_at=now,
                        )
                        .returning(conversation_inputs)
                    )
                )
                .mappings()
                .one()
            )
            if conversation["updated_at"] < now:
                conversation = dict(
                    (
                        await conn.execute(
                            update(supervisor_conversations)
                            .where(supervisor_conversations.c.id == conversation["id"])
                            .values(updated_at=now)
                            .returning(supervisor_conversations)
                        )
                    )
                    .mappings()
                    .one()
                )
            return {
                "created": True,
                "conversation": conversation,
                "input": dict(item),
                "supervisor_message_id": supervisor_message_id,
            }

    async def _conversation_input_replay(
        self, transport: str, external_message_id: str
    ) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            existing = await _input_by_external(conn, transport, external_message_id)
            return await _replay(conn, existing) if existing is not None else None

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def find_conversation_input_by_external(
        self, *, transport: str, external_message_id: str
    ) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            return await _input_by_external(conn, transport, external_message_id)

    async def find_conversation_by_thread(
        self, *, transport: str, channel_id: str, external_thread_id: str
    ) -> dict[str, Any] | None:
        """The conversation bound to an external thread, or ``None``.

        Both the thread and the configured channel must match, so a channel
        that has been reconfigured no longer correlates -- the same rule
        :meth:`find_escalation_by_thread` applies.
        """
        if not channel_id or not external_thread_id:
            return None
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(supervisor_conversations).where(
                            supervisor_conversations.c.transport == transport,
                            supervisor_conversations.c.channel_id == channel_id,
                            supervisor_conversations.c.external_thread_id == external_thread_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row(row)

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(supervisor_conversations).where(
                            supervisor_conversations.c.id == conversation_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row(row)

    async def get_conversation_input(self, input_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(conversation_inputs).where(conversation_inputs.c.id == input_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row(row)

    async def count_conversation_inputs(
        self,
        *,
        since: float,
        author_id: str | None = None,
        channel_id: str | None = None,
    ) -> int:
        """Inputs accepted at or after *since* that still spend rate-limit quota.

        Every accepted input counts, answered or expired, except a revoked
        one.  Replays never create a row, so they never spend quota.
        """
        conditions = [
            conversation_inputs.c.created_at >= since,
            conversation_inputs.c.state != "revoked",
        ]
        if author_id is not None:
            conditions.append(conversation_inputs.c.author_id == author_id)
        if channel_id is not None:
            conditions.append(conversation_inputs.c.channel_id == channel_id)
        async with self._engine.connect() as conn:
            total = await conn.scalar(
                select(func.count()).select_from(conversation_inputs).where(*conditions)
            )
        return int(total or 0)

    # ------------------------------------------------------------------
    # Conversation state
    # ------------------------------------------------------------------

    async def bind_conversation_thread(
        self, conversation_id: str, *, external_thread_id: str, now: float
    ) -> bool:
        """Record the confirmed external thread and move ``opening`` to ``open``.

        A compare-and-set on the state: only the first confirmation binds.
        """
        _require_nonempty({"external_thread_id": external_thread_id}, ("external_thread_id",))
        async with self.immediate() as conn:
            result = await conn.execute(
                update(supervisor_conversations)
                .where(
                    supervisor_conversations.c.id == conversation_id,
                    supervisor_conversations.c.state == "opening",
                )
                .values(external_thread_id=external_thread_id, state="open", updated_at=now)
            )
        return result.rowcount > 0

    async def set_conversation_state(
        self,
        conversation_id: str,
        *,
        state: str,
        now: float,
        expected: tuple[str, ...] = (),
    ) -> bool:
        """Move a conversation to *state*; with *expected*, only from those states.

        Closing stamps ``closed_at``.  Returns whether a row changed.
        """
        if state not in CONVERSATION_STATES:
            raise ValueError(f"unknown conversation state {state!r}")
        conditions = [supervisor_conversations.c.id == conversation_id]
        if expected:
            conditions.append(supervisor_conversations.c.state.in_(expected))
        values: dict[str, Any] = {"state": state, "updated_at": now}
        if state == "closed":
            values["closed_at"] = now
        async with self.immediate() as conn:
            result = await conn.execute(
                update(supervisor_conversations).where(*conditions).values(**values)
            )
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Replies
    # ------------------------------------------------------------------

    async def record_conversation_reply(
        self,
        *,
        conversation_id: str,
        input_id: str,
        reply_message_id: str,
        body: str,
        now: float,
    ) -> dict[str, Any]:
        """Persist the supervisor's answer to one input.

        Writes the ``messages`` row *reply_message_id* from the session that
        received the input to ``user:discord:<author>`` in the conversation
        thread, replying to the input's supervisor message; marks that message
        read; moves the input to ``answered`` and points it at this reply.

        A replay of *reply_message_id* returns ``"created": False`` and writes
        nothing.  Raises :class:`ConversationNotFound`,
        :class:`ConversationConflict` (input from another conversation, or the
        id already names an unrelated message), :class:`ConversationClosed`
        and :class:`ConversationStateError` (revoked input).

        Returns ``{"created", "conversation", "input", "message"}``.
        """
        _require_nonempty(
            {"reply_message_id": reply_message_id, "body": body},
            ("reply_message_id", "body"),
        )
        async with self.immediate() as conn:
            conversation = (
                (
                    await conn.execute(
                        select(supervisor_conversations)
                        .where(supervisor_conversations.c.id == conversation_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if conversation is None:
                raise ConversationNotFound(f"conversation {conversation_id} does not exist")
            item = (
                (
                    await conn.execute(
                        select(conversation_inputs)
                        .where(conversation_inputs.c.id == input_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if item is None:
                raise ConversationNotFound(f"conversation input {input_id} does not exist")
            if item["conversation_id"] != conversation_id:
                raise ConversationConflict(
                    f"input {input_id} does not belong to conversation {conversation_id}"
                )

            existing = (
                (await conn.execute(select(messages).where(messages.c.id == reply_message_id)))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["thread_id"] != conversation["thread_id"]
                    or existing["reply_to_id"] != item["supervisor_message_id"]
                ):
                    raise ConversationConflict(
                        f"message {reply_message_id} already exists outside this input"
                    )
                return {
                    "created": False,
                    "conversation": dict(conversation),
                    "input": dict(item),
                    "message": dict(existing),
                }
            if conversation["state"] == "closed":
                raise ConversationClosed(f"conversation {conversation_id} is closed")
            if item["state"] == "revoked":
                raise ConversationStateError(f"input {input_id} is revoked")

            notice = (
                (
                    await conn.execute(
                        select(messages.c.to_id, messages.c.read_at).where(
                            messages.c.id == item["supervisor_message_id"]
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            sender = notice["to_id"] if notice is not None else SUPERVISOR_RECIPIENT
            reply = (
                (
                    await conn.execute(
                        pg_insert(messages)
                        .values(
                            id=reply_message_id,
                            project_id=None,
                            from_kind="session",
                            from_id=sender,
                            to_kind="user",
                            to_id=f"discord:{item['author_id']}",
                            thread_id=conversation["thread_id"],
                            subject=f"Discord conversation {conversation_id}",
                            body=body,
                            priority=_SUPERVISOR_PRIORITY,
                            created_at=now,
                            delivered_at=None,
                            read_at=None,
                            archive_after_inject=0,
                            archived_at=None,
                            reply_to_id=item["supervisor_message_id"],
                            via=None,
                            body_kind="conversation_reply",
                            pane_open=None,
                        )
                        .returning(messages)
                    )
                )
                .mappings()
                .one()
            )
            if notice is not None and notice["read_at"] is None:
                await conn.execute(
                    update(messages)
                    .where(
                        messages.c.id == item["supervisor_message_id"],
                        messages.c.read_at.is_(None),
                    )
                    .values(read_at=now)
                )
            answered = (
                (
                    await conn.execute(
                        update(conversation_inputs)
                        .where(conversation_inputs.c.id == input_id)
                        .values(
                            state=case(
                                (conversation_inputs.c.state == "accepted", "answered"),
                                else_=conversation_inputs.c.state,
                            ),
                            reply_message_id=reply_message_id,
                        )
                        .returning(conversation_inputs)
                    )
                )
                .mappings()
                .one()
            )
            touched = (
                (
                    await conn.execute(
                        update(supervisor_conversations)
                        .where(supervisor_conversations.c.id == conversation_id)
                        .values(
                            updated_at=func.greatest(supervisor_conversations.c.updated_at, now)
                        )
                        .returning(supervisor_conversations)
                    )
                )
                .mappings()
                .one()
            )
            return {
                "created": True,
                "conversation": dict(touched),
                "input": dict(answered),
                "message": dict(reply),
            }

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def list_conversations(
        self,
        *,
        states: list[str] | None = None,
        limit: int = 50,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        """Conversations, most recently active first; page with ``before=updated_at``."""
        conditions = []
        if states:
            conditions.append(supervisor_conversations.c.state.in_(states))
        if before is not None:
            conditions.append(supervisor_conversations.c.updated_at < before)
        statement = (
            select(supervisor_conversations)
            .where(*conditions)
            .order_by(
                supervisor_conversations.c.updated_at.desc(),
                supervisor_conversations.c.id.desc(),
            )
            .limit(max(1, int(limit)))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def list_conversation_inputs(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        """One conversation's inputs, newest first; page with ``before=received_at``.

        ``text`` is ``None`` once expired.  Each row carries the current reply
        pointer's ``reply_body`` and ``reply_created_at`` (``None`` when
        unanswered).
        """
        reply = messages.alias("reply")
        conditions = [conversation_inputs.c.conversation_id == conversation_id]
        if before is not None:
            conditions.append(conversation_inputs.c.received_at < before)
        statement = (
            select(
                conversation_inputs,
                reply.c.body.label("reply_body"),
                reply.c.created_at.label("reply_created_at"),
            )
            .select_from(
                conversation_inputs.outerjoin(
                    reply, reply.c.id == conversation_inputs.c.reply_message_id
                )
            )
            .where(*conditions)
            .order_by(conversation_inputs.c.received_at.desc(), conversation_inputs.c.id.desc())
            .limit(max(1, int(limit)))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Delay watchdog
    # ------------------------------------------------------------------

    async def list_inputs_awaiting_supervisor(
        self, *, older_than: float, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Accepted inputs the supervisor has not been handed yet, oldest first.

        Received at or before *older_than*, never delay-notified, their
        supervisor message still undelivered, in a conversation that is not
        closed.  Each row adds ``conversation_state``.
        """
        statement = (
            select(
                conversation_inputs, supervisor_conversations.c.state.label("conversation_state")
            )
            .select_from(
                conversation_inputs.join(
                    supervisor_conversations,
                    supervisor_conversations.c.id == conversation_inputs.c.conversation_id,
                ).join(messages, messages.c.id == conversation_inputs.c.supervisor_message_id)
            )
            .where(
                conversation_inputs.c.state == "accepted",
                conversation_inputs.c.delay_notified_at.is_(None),
                conversation_inputs.c.received_at <= older_than,
                messages.c.delivered_at.is_(None),
                supervisor_conversations.c.state != "closed",
            )
            .order_by(conversation_inputs.c.received_at, conversation_inputs.c.id)
            .limit(max(1, int(limit)))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def mark_delay_notified(self, input_id: str, *, now: float) -> bool:
        """Compare-and-set the delay stamp; ``False`` if already notified."""
        async with self.immediate() as conn:
            result = await conn.execute(
                update(conversation_inputs)
                .where(
                    conversation_inputs.c.id == input_id,
                    conversation_inputs.c.delay_notified_at.is_(None),
                )
                .values(delay_notified_at=now)
            )
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    async def expire_conversation_text(self, *, older_than: float, now: float) -> int:
        """Null the text of inputs received before *older_than*.

        The row stays as the dedup tombstone.  An input still ``accepted``
        becomes ``expired``; answered and revoked inputs keep their state.
        Returns the number of inputs expired by this call.
        """
        async with self.immediate() as conn:
            result = await conn.execute(
                update(conversation_inputs)
                .where(
                    conversation_inputs.c.received_at < older_than,
                    conversation_inputs.c.text_expired_at.is_(None),
                )
                .values(
                    text=None,
                    text_expired_at=now,
                    state=case(
                        (conversation_inputs.c.state == "accepted", "expired"),
                        else_=conversation_inputs.c.state,
                    ),
                )
            )
        return int(result.rowcount or 0)

    async def delete_conversation_tombstones(self, *, older_than: float) -> int:
        """Delete inputs received before *older_than*; conversations are kept."""
        async with self.immediate() as conn:
            result = await conn.execute(
                delete(conversation_inputs).where(conversation_inputs.c.received_at < older_than)
            )
        return int(result.rowcount or 0)

    async def close_idle_conversations(self, *, idle_since: float, now: float) -> int:
        """Close open or delivery-blocked conversations untouched since *idle_since*."""
        async with self.immediate() as conn:
            result = await conn.execute(
                update(supervisor_conversations)
                .where(
                    supervisor_conversations.c.state.in_(IDLE_CLOSABLE_STATES),
                    supervisor_conversations.c.updated_at < idle_since,
                )
                .values(state="closed", closed_at=now, updated_at=now)
            )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # Backfill cursors and gaps
    # ------------------------------------------------------------------

    async def list_backfill_cursors(self) -> list[dict[str, Any]]:
        """All persisted channel/thread positions, in stable destination order."""
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(conversation_backfill_cursors).order_by(
                            conversation_backfill_cursors.c.transport,
                            conversation_backfill_cursors.c.channel_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def get_backfill_cursor(
        self, *, transport: str, channel_id: str
    ) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(conversation_backfill_cursors).where(
                            conversation_backfill_cursors.c.transport == transport,
                            conversation_backfill_cursors.c.channel_id == channel_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row(row)

    async def advance_backfill_cursor(
        self,
        *,
        transport: str,
        channel_id: str,
        last_external_message_id: str,
        now: float,
    ) -> None:
        """Upsert the channel's cursor.  Callers advance only past persisted pages."""
        _require_nonempty(
            {
                "transport": transport,
                "channel_id": channel_id,
                "last_external_message_id": last_external_message_id,
            },
            ("transport", "channel_id", "last_external_message_id"),
        )
        statement = pg_insert(conversation_backfill_cursors).values(
            transport=transport,
            channel_id=channel_id,
            last_external_message_id=last_external_message_id,
            advanced_at=now,
        )
        async with self.immediate() as conn:
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=["transport", "channel_id"],
                    set_={
                        "last_external_message_id": statement.excluded.last_external_message_id,
                        "advanced_at": statement.excluded.advanced_at,
                    },
                )
            )

    async def record_intake_gap(
        self,
        *,
        transport: str,
        channel_id: str,
        gap_from: float,
        gap_to: float,
        reason: str,
        now: float,
    ) -> dict[str, Any]:
        if reason not in GAP_REASONS:
            raise ValueError(f"unknown intake gap reason {reason!r}")
        if gap_to < gap_from:
            raise ValueError("intake gap ends before it starts")
        values = {
            "id": f"gap-{uuid.uuid4()}",
            "transport": transport,
            "channel_id": channel_id,
            "gap_from": float(gap_from),
            "gap_to": float(gap_to),
            "reason": reason,
            "recorded_at": float(now),
        }
        async with self.immediate() as conn:
            await conn.execute(pg_insert(conversation_intake_gaps).values(**values))
        return values

    async def list_intake_gaps(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Recorded intake gaps, newest first."""
        statement = (
            select(conversation_intake_gaps)
            .order_by(
                conversation_intake_gaps.c.recorded_at.desc(),
                conversation_intake_gaps.c.id.desc(),
            )
            .limit(max(1, int(limit)))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def conversation_counts(self) -> dict[str, Any]:
        """Conversations per state (every state present) and unanswered inputs.

        ``inputs_pending_supervisor`` counts inputs still ``accepted``: the
        supervisor has not answered them, whether or not it has read them.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(supervisor_conversations.c.state, func.count()).group_by(
                        supervisor_conversations.c.state
                    )
                )
            ).all()
            pending = await conn.scalar(
                select(func.count())
                .select_from(conversation_inputs)
                .where(conversation_inputs.c.state == "accepted")
            )
        by_state = {state: 0 for state in CONVERSATION_STATES}
        by_state.update({state: int(count) for state, count in rows})
        return {"by_state": by_state, "inputs_pending_supervisor": int(pending or 0)}


async def _input_by_external(conn, transport: str, external_message_id: str) -> dict | None:
    row = (
        (
            await conn.execute(
                select(conversation_inputs).where(
                    and_(
                        conversation_inputs.c.transport == transport,
                        conversation_inputs.c.external_message_id == external_message_id,
                    )
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return _row(row)


async def _replay(conn, existing: Mapping[str, Any]) -> dict[str, Any]:
    conversation = (
        (
            await conn.execute(
                select(supervisor_conversations).where(
                    supervisor_conversations.c.id == existing["conversation_id"]
                )
            )
        )
        .mappings()
        .one()
    )
    return {
        "created": False,
        "conversation": dict(conversation),
        "input": dict(existing),
        "supervisor_message_id": existing["supervisor_message_id"],
    }
