"""Bounded, restart-safe expiry and collaboration content retention."""

from __future__ import annotations

from sqlalchemy import delete, exists, select, update

from src.collaboration import CONTENT_RETENTION_SECONDS, TOMBSTONE_RETENTION_SECONDS
from src.database.tables import (
    collaboration_messages as links,
    collaboration_threads as threads,
    messages,
)

EXPIRY_BATCH_SIZE = 100
CONTENT_BATCH_SIZE = 500
TOMBSTONE_BATCH_SIZE = 500
COLLABORATION_BODY_KINDS = ("collaboration", "collaboration_invite", "collaboration_closed")


class CollaborationLifecycleQueriesMixin:
    async def reconcile_collaborations(self, *, now: float) -> dict:
        """Lock threads before their children, sharing the send/close lock order.

        Each stage commits independently. Concurrent ticks skip locked threads;
        re-running after a crash resumes from persisted state without re-emission.
        """
        expired = 0
        async with self._engine.begin() as conn:
            due = (
                (
                    await conn.execute(
                        select(threads.c.id, threads.c.project_id)
                        .where(threads.c.state == "active", threads.c.deadline_at <= now)
                        .order_by(threads.c.deadline_at, threads.c.id)
                        .limit(EXPIRY_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            for thread in due:
                await self.close_collaboration_thread(
                    thread_id=thread["id"],
                    project_id=thread["project_id"],
                    reason="expired",
                    now=now,
                    conn=conn,
                )
                expired += 1

        has_bodies = exists(
            select(messages.c.id).where(
                messages.c.thread_id == threads.c.id,
                messages.c.body_kind.in_(COLLABORATION_BODY_KINDS),
            )
        )
        messages_deleted = 0
        async with self._engine.begin() as conn:
            old = (
                (
                    await conn.execute(
                        select(threads.c.id)
                        .where(
                            threads.c.state != "active",
                            threads.c.closed_at < now - CONTENT_RETENTION_SECONDS,
                            has_bodies,
                        )
                        .order_by(threads.c.closed_at, threads.c.id)
                        .limit(CONTENT_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for thread_id in old:
                message_ids = (
                    (
                        await conn.execute(
                            select(messages.c.id)
                            .where(
                                messages.c.thread_id == thread_id,
                                messages.c.body_kind.in_(COLLABORATION_BODY_KINDS),
                            )
                            .order_by(messages.c.created_seq)
                            .limit(CONTENT_BATCH_SIZE - messages_deleted)
                        )
                    )
                    .scalars()
                    .all()
                )
                # reply_to_id is a restrictive FK. Keep surviving reply bodies,
                # but detach their pointers before removing the referenced rows.
                await conn.execute(
                    update(messages)
                    .where(messages.c.reply_to_id.in_(message_ids))
                    .values(reply_to_id=None)
                )
                await conn.execute(
                    update(links)
                    .where(links.c.thread_id == thread_id, links.c.message_id.in_(message_ids))
                    .values(message_id=None)
                )
                await conn.execute(delete(messages).where(messages.c.id.in_(message_ids)))
                messages_deleted += len(message_ids)
                if messages_deleted == CONTENT_BATCH_SIZE:
                    break

        async with self._engine.begin() as conn:
            old_ids = (
                (
                    await conn.execute(
                        select(threads.c.id)
                        .where(
                            threads.c.state != "active",
                            threads.c.closed_at < now - TOMBSTONE_RETENTION_SECONDS,
                            ~has_bodies,
                        )
                        .order_by(threads.c.closed_at, threads.c.id)
                        .limit(TOMBSTONE_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            await conn.execute(delete(threads).where(threads.c.id.in_(old_ids)))
        return {
            "expired": expired,
            "messages_deleted": messages_deleted,
            "threads_deleted": len(old_ids),
        }
