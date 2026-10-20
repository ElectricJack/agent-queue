"""Human subjects; supervisor discussion is not a human decision.

Lifting, digest rendering and dispatch belong to the human-wait service.
"""

import time
from dataclasses import replace

from sqlalchemy import or_, select

from src.database.tables import doc_reviews, messages, task_comments, tasks, wait_source_events
from src.waits.adapters.base import fields, foreign, integer, invalid, row, string, unresolved
from src.waits.adapters.message import MessageAdapter, addressed
from src.waits.conditions import Condition, DeadlinePolicy, Observation, Snapshot, SourceKey


class HumanAdapter(MessageAdapter):
    kind = "human"

    def __init__(self, clock=time.time):
        self.clock = clock

    def normalise(self, raw):
        subject = raw.get("subject")
        if subject == "review":
            fields(raw, required={"subject", "review_id"}, optional={"revision"})
            params = {"subject": subject, "review_id": string(raw["review_id"])}
            if "revision" in raw:
                params["revision"] = integer(raw["revision"], 1)
        elif subject == "task_comment":
            fields(raw, required={"subject", "task_id"})
            params = {"subject": subject, "task_id": string(raw["task_id"])}
        elif subject == "message_thread":
            fields(raw, required={"subject", "thread_id", "after_seq"})
            params = {
                "subject": subject,
                "thread_id": string(raw["thread_id"]),
                "after_seq": integer(raw["after_seq"]),
            }
        else:
            invalid("human subject must be review, task_comment or message_thread")
        return Condition(self.kind, params, raw.get("generation"))

    async def authorise(self, conn, cond, scope):
        if cond.parameters["subject"] == "message_thread":
            cond = await super().authorise(conn, cond, scope)
            human_recipient = await conn.scalar(
                select(messages.c.id)
                .where(
                    messages.c.project_id == scope.project_id,
                    messages.c.thread_id == cond.parameters["thread_id"],
                    messages.c.to_kind == "user",
                    addressed(cond, sender=True),
                )
                .limit(1)
            )
            if not human_recipient:
                unresolved("thread has no request from this principal to a human")
            return cond
        # Skip MessageAdapter's participation query for non-thread subjects.
        from src.waits.adapters.base import Adapter

        cond = await Adapter.authorise(self, conn, cond, scope)
        if cond.parameters["subject"] == "review":
            review = await row(conn, doc_reviews, doc_reviews.c.id == cond.parameters["review_id"])
            if review:
                if review["project_id"] != scope.project_id:
                    foreign()
                if review["state"] != "in_review" and not human_decision(review):
                    unresolved("review is not awaiting a human decision")
                if "revision" not in cond.parameters:
                    cond = replace(cond, parameters={**cond.parameters,
                                                     "revision": review["current_revision"]})
        if cond.parameters["subject"] == "task_comment":
            # task_comments has no sequence column. Capture the server clock;
            # the human-event writer/dispatcher will supply its durable seq.
            task = await row(conn, tasks, tasks.c.id == cond.parameters["task_id"])
            if task and task["project_id"] != scope.project_id:
                foreign()
            if task and task["status"] in {"COMPLETED", "FAILED"}:
                unresolved("task is no longer open for a human response")
            cond = cond.bind(generation=cond.generation, after_at=self.clock(),
                             task_created_at=task["created_at"] if task else None)
        return cond

    async def snapshot(self, conn, cond):
        p = cond.parameters
        if p["subject"] == "review":
            review = await row(conn, doc_reviews, doc_reviews.c.id == p["review_id"])
            if not review:
                return None
            if "revision" in p and p["revision"] != review["current_revision"]:
                return Snapshot(review["project_id"], review["current_revision"],
                                Observation("revision_changed", "unavailable"), "human:review")
            decided = human_decision(review)
            state = "satisfied" if decided else "unsatisfied"
            if review["state"] == "withdrawn":
                state = "unavailable"
            return Snapshot(
                review["project_id"],
                review["current_revision"],
                Observation(
                    f"{review['updated_at']}:{review['state']}",
                    state,
                    f"review:{review['id']}@{review['current_revision']}",
                    {"state": review["state"], "decided_by": review["decided_by"]},
                ),
                "human:review",
            )
        if p["subject"] == "task_comment":
            task = await row(conn, tasks, tasks.c.id == p["task_id"])
            if not task:
                return None
            if task["created_at"] != cond.context.get("task_created_at"):
                return Snapshot(task["project_id"], task["claim_epoch"],
                                Observation("task_replaced", "unavailable"), "human:task_comment")
            comment = (
                (
                    await conn.execute(
                        select(task_comments)
                        .where(
                            task_comments.c.task_id == task["id"],
                            task_comments.c.project_id == task["project_id"],
                            task_comments.c.author_kind == "user",
                            or_(
                                task_comments.c.created_at > cond.context["after_at"],
                                select(wait_source_events.c.seq).where(
                                    wait_source_events.c.source_key == f"human:task_comment:{task['id']}",
                                    wait_source_events.c.seq > cond.context.get(
                                        "source_watermark_seq", 2**63 - 1),
                                    wait_source_events.c.source_revision == task_comments.c.id,
                                ).exists(),
                            ),
                        )
                        .order_by(task_comments.c.created_at, task_comments.c.id)
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            state = "satisfied" if comment else "unsatisfied"
            if task["status"] in {"COMPLETED", "FAILED"} and not comment:
                state = "unavailable"
            # Parking deliberately advances the owner's claim epoch. Comments
            # still answer this task incarnation, including after that fence.
            return Snapshot(
                task["project_id"],
                cond.generation if cond.generation is not None else task["claim_epoch"],
                Observation(
                    str(comment["id"] if comment else task["updated_at"]),
                    state,
                    f"task_comment:{comment['id']}" if comment else None,
                ),
                "human:task_comment",
            )
        message = (
            (
                await conn.execute(
                    select(messages)
                    .where(
                        messages.c.project_id == cond.context["project_id"],
                        messages.c.thread_id == p["thread_id"],
                        messages.c.created_seq > p["after_seq"],
                        messages.c.from_kind == "user",
                        addressed(cond),
                    )
                    .order_by(messages.c.created_seq)
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        exists = await conn.scalar(
            select(messages.c.id)
            .where(
                messages.c.project_id == cond.context["project_id"],
                messages.c.thread_id == p["thread_id"],
            )
            .limit(1)
        )
        if not exists:
            return None
        return Snapshot(
            cond.context["project_id"],
            0,
            Observation(
                str(message["created_seq"] if message else p["after_seq"]),
                "satisfied" if message else "unsatisfied",
                f"message:{message['id']}" if message else None,
            ),
            "human:thread",
        )

    def source_key(self, cond):
        p = cond.parameters
        if p["subject"] == "review":
            return SourceKey("review", p["review_id"], cond.generation)
        if p["subject"] == "message_thread":
            return SourceKey("message", p["thread_id"], 0)
        return SourceKey("human", f"task_comment:{p['task_id']}", cond.generation)

    def deadline_policy(self, cond, policy):
        return DeadlinePolicy(None)


def human_decision(review):
    # _review_decider_label persists human:local-operator for authenticated
    # dashboard/local decisions; user:* is the message/event principal label.
    return review["decided_at"] is not None and (review["decided_by"] or "").split(":")[0] in {
        "human",
        "user",
    }
