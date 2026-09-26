"""Durable delay notices and 30/90-day supervisor conversation retention."""

from __future__ import annotations

import time
from collections.abc import Callable

from src.conversations.limits import (
    SUPERVISOR_DELAY_SECONDS,
    TEXT_RETENTION_DAYS,
    TOMBSTONE_RETENTION_DAYS,
)
from src.conversations.outbox import ConversationOutbox
from src.conversations.render import notice_text

_DAY_SECONDS = 86400


class ConversationMaintenance:
    """Queue notices through the outbox; keep retention independent of transport."""

    def __init__(
        self, *, db, outbox: ConversationOutbox, clock: Callable[[], float] = time.time
    ) -> None:
        self.db = db
        self.outbox = outbox
        self.clock = clock

    async def tick(self, *, now: float | None = None) -> dict[str, int]:
        now = float(self.clock() if now is None else now)
        delay_notices = 0
        if self.outbox.bound:
            # The query is bounded. Stamping successful enqueues advances the
            # next page without timestamp cursors losing equally old inputs.
            while inputs := await self.db.list_inputs_awaiting_supervisor(
                older_than=now - SUPERVISOR_DELAY_SECONDS
            ):
                for item in inputs:
                    conversation_id = item["conversation_id"]
                    await self.outbox.enqueue(
                        owner_id=conversation_id,
                        kind="notice",
                        dedup_key=f"conv-notice:delay:{item['id']}",
                        payload={
                            "kind": "delay",
                            "conversation_id": conversation_id,
                            "text": notice_text("delay"),
                        },
                    )
                    # Enqueue first: a crash before this stamp retries the same
                    # durable dedup key, while failed enqueues remain eligible.
                    delay_notices += int(await self.db.mark_delay_notified(item["id"], now=now))

        text_cutoff = now - TEXT_RETENTION_DAYS * _DAY_SECONDS
        expired_text = await self.db.expire_conversation_text(older_than=text_cutoff, now=now)
        # Briefs and explicit replies also contain conversation text. Archive
        # old messages in every conversation thread, including closed threads,
        # before deleting input tombstones (which carry their message pointers).
        while message_ids := await self.db.list_expired_conversation_message_ids(
            older_than=text_cutoff
        ):
            await self.db.archive_messages(message_ids)

        deleted_tombstones = await self.db.delete_conversation_tombstones(
            older_than=now - TOMBSTONE_RETENTION_DAYS * _DAY_SECONDS
        )
        closed = await self.db.close_idle_conversations(idle_since=text_cutoff, now=now)
        return {
            "delay_notices": delay_notices,
            "expired_text": expired_text,
            "deleted_tombstones": deleted_tombstones,
            "closed": closed,
        }
