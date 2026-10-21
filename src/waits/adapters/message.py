"""Thread participation and addressed incoming messages, using server sequences."""

from sqlalchemy import and_, func, or_, select

from src.collaboration import is_collaboration_thread
from src.database.tables import (
    collaboration_members,
    collaboration_messages,
    collaboration_threads,
    messages,
)
from src.waits.adapters.base import Adapter, fields, foreign, integer, row, string, unresolved
from src.waits.conditions import Condition, Observation, Snapshot, SourceKey


def addressed(cond, *, sender=False):
    kind = messages.c.from_kind if sender else messages.c.to_kind
    identity = messages.c.from_id if sender else messages.c.to_id
    context = cond.context
    return or_(
        and_(kind == "session", identity == context.get("session_id")),
        and_(kind == context["owner_kind"], identity == context["owner_id"]),
    )


class MessageAdapter(Adapter):
    kind = "message"

    def normalise(self, raw):
        fields(raw, required={"thread_id", "after_seq"})
        return Condition(
            self.kind,
            {
                "thread_id": string(raw["thread_id"]),
                "after_seq": integer(raw["after_seq"]),
            },
            raw.get("generation"),
        )

    async def authorise(self, conn, cond, scope):
        cond = await super().authorise(conn, cond, scope)
        if is_collaboration_thread(cond.parameters["thread_id"]):
            thread = await row(
                conn,
                collaboration_threads,
                collaboration_threads.c.id == cond.parameters["thread_id"],
            )
            if not thread:
                unresolved()
            member = await row(
                conn,
                collaboration_members,
                collaboration_members.c.thread_id == thread["id"],
                collaboration_members.c.task_id == scope.owner_id,
            )
            if (
                thread["project_id"] != scope.project_id
                or scope.owner_kind != "task"
                or not member
                or member["state"] != "accepted"
                or member["accepted_claim_epoch"] != scope.claim_epoch
            ):
                foreign()
            if cond.parameters["after_seq"] > thread["last_seq"]:
                unresolved("cursor is ahead of the thread's server sequence")
            return cond
        thread = messages.c.thread_id == cond.parameters["thread_id"]
        if not await conn.scalar(select(messages.c.id).where(thread).limit(1)):
            unresolved()
        if not await conn.scalar(
            select(messages.c.id)
            .where(
                thread,
                messages.c.project_id == scope.project_id,
                or_(addressed(cond), addressed(cond, sender=True)),
            )
            .limit(1)
        ):
            foreign()
        maximum = await conn.scalar(
            select(func.max(messages.c.created_seq)).where(
                thread,
                messages.c.project_id == scope.project_id,
            )
        )
        if cond.parameters["after_seq"] > maximum:
            unresolved("cursor is ahead of the thread's server sequence")
        return cond

    async def snapshot(self, conn, cond):
        if is_collaboration_thread(cond.parameters["thread_id"]):
            return await self.collaboration_snapshot(conn, cond)
        where = [
            messages.c.thread_id == cond.parameters["thread_id"],
            messages.c.project_id == cond.context["project_id"],
        ]
        maximum = await conn.scalar(select(func.max(messages.c.created_seq)).where(*where))
        if maximum is None:
            return None
        message = (
            (
                await conn.execute(
                    select(messages)
                    .where(
                        *where,
                        messages.c.created_seq > cond.parameters["after_seq"],
                        addressed(cond),
                        ~addressed(cond, sender=True),
                    )
                    .order_by(messages.c.created_seq)
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        return Snapshot(
            cond.context["project_id"],
            0,
            Observation(
                str(maximum),
                "satisfied" if message else "unsatisfied",
                f"message:{message['id']}" if message else None,
                {"created_seq": message["created_seq"]} if message else {},
            ),
            "message",
        )

    async def collaboration_snapshot(self, conn, cond):
        thread = await row(
            conn, collaboration_threads, collaboration_threads.c.id == cond.parameters["thread_id"]
        )
        if not thread:
            return None
        member = await row(
            conn,
            collaboration_members,
            collaboration_members.c.thread_id == thread["id"],
            collaboration_members.c.task_id == cond.context["owner_id"],
        )
        if (
            not member
            or member["state"] != "accepted"
            or member["accepted_claim_epoch"] != cond.context["claim_epoch"]
        ):
            return None
        links = collaboration_messages
        # A link may fan out to several messages, or address only one member.
        # Membership alone must not turn another member's reply into our result.
        message = (
            (
                await conn.execute(
                    select(
                        links.c.seq,
                        messages.c.id,
                    )
                    .select_from(
                        links.join(
                            messages,
                            or_(
                                messages.c.id == links.c.message_id,
                                messages.c.id.startswith(links.c.message_id + ":"),
                            ),
                        )
                    )
                    .where(
                        links.c.thread_id == thread["id"],
                        links.c.seq > cond.parameters["after_seq"],
                        messages.c.thread_id == thread["id"],
                        messages.c.project_id == cond.context["project_id"],
                        addressed(cond),
                        ~addressed(cond, sender=True),
                    )
                    .order_by(links.c.seq, messages.c.created_seq)
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        state = "satisfied" if message else "unsatisfied"
        if not message and thread["state"] != "active":
            state = "unavailable"
        return Snapshot(
            thread["project_id"],
            0,
            Observation(
                str(thread["version"]),
                state,
                f"collaboration:{thread['id']}:{message['seq']}" if message else None,
                {"message_id": message["id"], "seq": message["seq"]} if message else {},
            ),
            "message",
        )

    def source_key(self, cond):
        return SourceKey(self.kind, cond.parameters["thread_id"], 0)
