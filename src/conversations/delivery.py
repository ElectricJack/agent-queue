"""Conversation actions on the shared, leased outbound-delivery dispatcher."""

from __future__ import annotations

import time

from src.conversations.limits import MAX_REPLY_CHARS
from src.conversations.render import notice_text
from src.delivery.message import FrozenMessage, MessageDelivery
from src.escalations.transport import TransportAmbiguous, TransportError, TransportRetryable


class DurableConversationOutbox:
    """Reserve domain actions, without performing any Discord I/O."""

    bound = True

    def __init__(self, db, *, clock=time.time):
        self.db, self.clock = db, clock

    async def enqueue(self, *, owner_id, kind, dedup_key, payload, priority=20, due_at=None):
        if kind not in {"thread_open", "reply", "notice"}:
            raise ValueError("unknown conversation delivery action")
        conversation = await self.db.get_conversation(owner_id)
        channel_id = payload.get("channel_id") or (conversation or {}).get("channel_id")
        if kind == "thread_open":
            text = "Message received. The supervisor will answer here."
        elif kind == "notice":
            text = payload.get("text") or notice_text(payload["kind"])
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
        conversation = await self.db.get_conversation(row["owner_id"])
        settings = self.config.discord
        allowed = {str(value) for value in settings.authorized_users}
        audience = set((conversation or {}).get("audience", []))
        invalid = (
            not settings.conversation.enabled
            or not allowed
            or row["destination"]["channel_id"] != str(settings.channel_id)
            or (
                conversation
                and (
                    (conversation["state"] == "closed" and payload["action"] != "notice")
                    or conversation["guild_id"] != str(settings.guild_id)
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
        if payload["action"] == "thread_open" and not thread_id:
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
                    kind="notice",
                    dedup_key=f"conv-notice:open-failed:{conversation['id']}",
                    payload={"kind": "open_failed"},
                )
                await self._finish(row, status="unknown", last_error=str(exc))
                return
        if payload["action"] == "reply" and not thread_id:
            await self._finish(
                row,
                status="retry",
                next_attempt_at=self.clock() + 30,
                last_error="waiting for the conversation thread",
            )
            return
        result = await self.delivery.deliver(
            FrozenMessage(
                channel_id=row["destination"]["channel_id"],
                thread_id=thread_id,
                text=payload["text"],
                marker=row["marker"],
                attempt_count=row["attempt_count"],
                reclaimed=bool(row.get("reclaimed")),
                last_error=row.get("last_error"),
            )
        )
        await self._finish(
            row,
            status=result.status,
            external_receipt_id=result.receipt_id,
            next_attempt_at=result.next_attempt_at,
            last_error=result.last_error,
        )

    async def _finish(self, row, **result):
        return await self.db.finish_outbound_delivery(
            row["id"], lease_owner=self.lease_owner, now=self.clock(), **result
        )
