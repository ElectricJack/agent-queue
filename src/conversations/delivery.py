"""Conversation actions on the shared, leased outbound-delivery dispatcher."""

from __future__ import annotations

import time

from src.conversations.intake import DM_GUILD, KIND_CHANNEL, is_dm_thread
from src.conversations.limits import MAX_REPLY_CHARS
from src.conversations.outbox import (
    ACTION_NOTICE,
    ACTION_REPLY,
    ACTION_STATUS_LINE,
    ACTION_THREAD_OPEN,
    CONVERSATION_ACTIONS,
)
from src.conversations.render import notice_text, status_line_text
from src.delivery.message import FrozenMessage, MessageDelivery
from src.escalations.transport import (
    TransportAmbiguous,
    TransportError,
    TransportMissing,
    TransportRetryable,
)


class DurableConversationOutbox:
    """Reserve domain actions, without performing any Discord I/O."""

    bound = True

    def __init__(self, db, *, clock=time.time):
        self.db, self.clock = db, clock

    async def enqueue(self, *, owner_id, kind, dedup_key, payload, priority=20, due_at=None):
        if kind not in CONVERSATION_ACTIONS:
            raise ValueError("unknown conversation delivery action")
        conversation = await self.db.get_conversation(owner_id)
        channel_id = payload.get("channel_id") or (conversation or {}).get("channel_id")
        if kind == ACTION_THREAD_OPEN:
            text = "Message received. The supervisor will answer here."
        elif kind == ACTION_NOTICE:
            text = payload.get("text") or notice_text(payload["kind"])
        elif kind == ACTION_STATUS_LINE:
            text = status_line_text(payload["state"], queued=payload.get("queued", 0))
        else:
            text = payload["text"]
        # The shared adapter appends its own short reconciliation marker.
        # Reply rendering already reserves 100 characters below Discord's cap.
        if len(text) > MAX_REPLY_CHARS:
            raise ValueError("conversation delivery exceeds its reply budget")
        now = self.clock()
        row, _created = await self.db.reserve_outbound_delivery(
            owner_kind="conversation",
            owner_id=owner_id,
            dedup_key=dedup_key,
            destination={"transport": "discord", "channel_id": channel_id},
            payload={**payload, "action": kind, "text": text},
            due_at=now if due_at is None else due_at,
            now=now,
        )
        return row["id"]


class ConversationDeliveryAdapter:
    """Bind threads and send through the same receipt/reconciliation primitive as reports."""

    def __init__(self, db, transport, *, config, lease_owner, clock=time.time):
        self.db, self.transport, self.config = db, transport, config
        self.lease_owner, self.clock = lease_owner, clock
        self.delivery = MessageDelivery(transport, clock=clock)

    async def deliver(self, row):
        payload = row["payload"]
        action = payload["action"]
        conversation = await self.db.get_conversation(row["owner_id"])
        settings = self.config.discord
        allowed = {str(value) for value in settings.authorized_users}
        audience = set((conversation or {}).get("audience", []))
        # A direct message is admitted as its own channel conversation (§2.1):
        # its destination is the DM channel, not the configured one, and it is
        # only ever deliverable while the operator has opted DMs in.
        direct = bool(conversation) and conversation["guild_id"] == DM_GUILD
        invalid = (
            not settings.conversation.enabled
            or not allowed
            or (
                row["destination"]["channel_id"] != str(settings.channel_id)
                and not (direct and settings.conversation.allow_dm)
            )
            or (
                conversation
                and (
                    (conversation["state"] == "closed" and action != ACTION_NOTICE)
                    or (
                        conversation["guild_id"] != str(settings.guild_id)
                        and not (direct and conversation["guild_id"] == DM_GUILD)
                    )
                    or not audience.issubset(allowed)
                )
            )
            or (not conversation and payload.get("author_id") not in allowed)
        )
        if invalid:
            await self._finish(
                row, status="cancelled", last_error="conversation visibility changed"
            )
            return
        thread_id = payload.get("thread_id") or (conversation or {}).get("external_thread_id")
        # §2.1/§2.2: a direct message is admitted as its own channel
        # conversation. There is no thread to bind, none to open and none to
        # wait for.
        direct = is_dm_thread(thread_id)
        # A channel conversation has no thread, and never opens one: its ack,
        # its answer, its status line and its notices are ordinary posts in the
        # channel itself, each replying to the operator's message for context.
        # Only an operator-started thread (kind='thread') or an escalation root
        # is bound to a thread.
        in_channel = bool(conversation) and conversation.get("kind") == KIND_CHANNEL
        reference = conversation["external_root_message_id"] if in_channel else None
        if direct or in_channel:
            thread_id = None
        elif action == ACTION_THREAD_OPEN and not thread_id:
            try:
                thread = await self.transport.ensure_thread(
                    channel_id=conversation["channel_id"],
                    root_message_id=conversation["external_root_message_id"],
                    name="Supervisor conversation",
                )
                thread_id = thread.thread_id
                await self.db.bind_conversation_thread(
                    conversation["id"], external_thread_id=thread_id, now=self.clock()
                )
            except (TransportRetryable, TransportAmbiguous) as exc:
                # Discord roots own at most one thread. Re-finding that binding
                # is safe even when thread creation's receipt was lost.
                await self._finish(
                    row,
                    status="retry",
                    next_attempt_at=self.clock() + 30,
                    last_error=f"thread binding: {exc}",
                )
                return
            except TransportError as exc:
                await self.db.set_conversation_state(
                    conversation["id"],
                    state="delivery_blocked",
                    now=self.clock(),
                    expected=("opening",),
                )
                await DurableConversationOutbox(self.db, clock=self.clock).enqueue(
                    owner_id=conversation["id"],
                    kind=ACTION_NOTICE,
                    dedup_key=f"conv-notice:open-failed:{conversation['id']}",
                    payload={"kind": "open_failed"},
                )
                await self._finish(row, status="unknown", last_error=str(exc))
                return
        if (
            action in {ACTION_REPLY, ACTION_STATUS_LINE}
            and not thread_id
            and not direct
            and not in_channel
        ):
            await self._finish(
                row,
                status="retry",
                next_attempt_at=self.clock() + 30,
                last_error="waiting for the conversation thread",
            )
            return
        if action == ACTION_STATUS_LINE:
            await self._deliver_status_line(row, thread_id, reference=reference)
            return
        if action == ACTION_REPLY:
            # §2.4: the first answer retires the offline line rather than
            # leaving a stale "answering now" under the reply.
            await self._retire_status_line(row, thread_id)
        result = await self.delivery.deliver(
            FrozenMessage(
                channel_id=row["destination"]["channel_id"],
                thread_id=thread_id,
                text=payload["text"],
                marker=row["marker"],
                attempt_count=row["attempt_count"],
                reclaimed=bool(row.get("reclaimed")),
                last_error=row.get("last_error"),
                reference_message_id=reference,
            )
        )
        if (
            result.status == "sent"
            and conversation
            and conversation["state"] == "opening"
            and action not in {ACTION_REPLY, ACTION_NOTICE}
        ):
            # A conversation the operator's own thread or a channel already
            # bound has no thread to open; its acknowledgement post does. A
            # notice never does: flipping the state here would consume the
            # ``opening``->``open`` CAS that binds the thread a thread
            # conversation has not opened yet.
            await self.db.set_conversation_state(
                conversation["id"], state="open", now=self.clock(), expected=("opening",)
            )
        await self._finish(
            row,
            status=result.status,
            external_receipt_id=result.receipt_id,
            next_attempt_at=result.next_attempt_at,
            last_error=result.last_error,
        )

    async def _deliver_status_line(self, row, thread_id, *, reference=None):
        """Post the §2.4 status line once per conversation, editing it after.

        The line carries the queue count, so each count is its own delivery;
        editing what the previous delivery posted is what keeps it to one line
        in the channel.  A deleted or missing post falls back to a fresh one.
        """
        previous = await self.db.find_conversation_status_line(
            row["owner_id"], exclude_delivery_id=row["id"]
        )
        content = f"{row['payload']['text']}\n{row['marker']}"
        if previous is not None:
            try:
                await self.transport.edit_message(
                    channel_id=row["destination"]["channel_id"],
                    thread_id=thread_id,
                    message_id=previous["external_receipt_id"],
                    content=content,
                )
                await self._finish(
                    row, status="sent", external_receipt_id=previous["external_receipt_id"]
                )
                return
            except TransportMissing:
                # Jack deleted it; a new line is the honest state.
                pass
        result = await self.delivery.deliver(
            FrozenMessage(
                channel_id=row["destination"]["channel_id"],
                thread_id=thread_id,
                text=row["payload"]["text"],
                marker=row["marker"],
                attempt_count=row["attempt_count"],
                reclaimed=bool(row.get("reclaimed")),
                last_error=row.get("last_error"),
                reference_message_id=reference,
            )
        )
        await self._finish(
            row,
            status=result.status,
            external_receipt_id=result.receipt_id,
            next_attempt_at=result.next_attempt_at,
            last_error=result.last_error,
        )

    async def _retire_status_line(self, row, thread_id):
        """Remove a delivered §2.4 status line once a real answer posts.

        Best effort by design: the answer is the deliverable, and a line the
        daemon cannot delete must not hold it back.  A reservation that has not
        been posted yet is cancelled instead, so the operator is never told the
        supervisor is away after it answered.  A line left behind is edited
        again if the supervisor goes offline once more.
        """
        await self.db.cancel_pending_conversation_status_line(row["owner_id"], now=self.clock())
        previous = await self.db.find_conversation_status_line(
            row["owner_id"], exclude_delivery_id=row["id"]
        )
        if previous is None:
            return
        try:
            await self.transport.delete_message(
                channel_id=row["destination"]["channel_id"],
                thread_id=thread_id,
                message_id=previous["external_receipt_id"],
            )
        except TransportError:
            return

    async def _finish(self, row, **result):
        return await self.db.finish_outbound_delivery(
            row["id"], lease_owner=self.lease_owner, now=self.clock(), **result
        )
