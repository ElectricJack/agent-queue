"""Transactional collaboration records and ordered message fan-out.

All mutations lock the thread first, then members and live task claims. Thread
closure and its deliveries commit together, including closure discovered by a
send or acceptance which subsequently returns a typed refusal.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src import collaboration as policy
from src.agent_waits import TERMINAL_TASK_STATUSES
from src.collaboration import CollaborationError
from src.database.queries.message_queries import _new_message_id
from src.database.tables import (
    archived_tasks,
    collaboration_members as members,
    collaboration_messages as links,
    collaboration_threads as threads,
    messages,
    sessions,
    tasks,
)


class CollaborationQueriesMixin:
    """Expects an async SQLAlchemy engine in ``self._engine``."""

    @asynccontextmanager
    async def _collaboration_connection(self, conn=None):
        if conn is not None:
            yield conn
        else:
            async with self._engine.begin() as owned:
                yield owned

    async def _locked_collaboration(self, conn, thread_id, project_id) -> dict:
        row = (
            (
                await conn.execute(
                    select(threads)
                    .where(threads.c.id == thread_id, threads.c.project_id == project_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise CollaborationError("not_found", "Collaboration thread not found")
        return dict(row)

    async def _collaboration_members(self, conn, thread_id) -> list[dict]:
        return [
            dict(row)
            for row in (
                await conn.execute(
                    select(members)
                    .where(members.c.thread_id == thread_id)
                    .order_by(members.c.task_id)
                    .with_for_update()
                )
            ).mappings()
        ]

    async def create_collaboration_thread(
        self,
        *,
        project_id,
        created_by_kind,
        created_by_id,
        idempotency_key,
        task_ids,
        goal=None,
        deadline_seconds=policy.DEFAULT_DEADLINE_SECONDS,
        message_budget=policy.MAX_MESSAGES,
        now: float,
    ) -> dict:
        ids = list(task_ids)
        if (
            not policy.MIN_MEMBERS <= len(ids) <= policy.MAX_MEMBERS
            or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids)
        ):
            raise CollaborationError("invalid", "Collaboration requires 2 to 4 distinct members")
        if (
            created_by_kind not in ("operator", "supervisor")
            or not created_by_id
            or not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
        ):
            raise CollaborationError("invalid", "A creator and idempotency key are required")
        if (
            not isinstance(deadline_seconds, (int, float))
            or isinstance(deadline_seconds, bool)
            or not math.isfinite(deadline_seconds)
            or not policy.MIN_DEADLINE_SECONDS <= deadline_seconds <= policy.MAX_DEADLINE_SECONDS
            or not isinstance(message_budget, int)
            or isinstance(message_budget, bool)
            or not 1 <= message_budget <= policy.MAX_MESSAGES
            or not math.isfinite(now)
            or (
                goal is not None
                and (not isinstance(goal, str) or len(goal) > policy.MAX_GOAL_CHARS)
            )
        ):
            raise CollaborationError("invalid", "Invalid goal, deadline or message budget")
        ids.sort()
        request_hash = hashlib.sha256(
            json.dumps(
                [ids, goal, float(deadline_seconds), message_budget],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        key_filter = and_(
            threads.c.project_id == project_id,
            threads.c.created_by_id == created_by_id,
            threads.c.idempotency_key == idempotency_key,
        )
        async with self._engine.begin() as conn:
            existing = (await conn.execute(select(threads).where(key_filter))).mappings().first()
            if existing is None:
                # ON CONFLICT serializes simultaneous creates with the same key.
                existing = (
                    (
                        await conn.execute(
                            pg_insert(threads)
                            .values(
                                id=policy.new_thread_id(),
                                project_id=project_id,
                                created_by_kind=created_by_kind,
                                created_by_id=created_by_id,
                                idempotency_key=idempotency_key,
                                request_hash=request_hash,
                                goal=goal,
                                created_at=now,
                                deadline_at=now + deadline_seconds,
                                message_budget=message_budget,
                            )
                            .on_conflict_do_nothing(
                                constraint="uq_collaboration_threads_idempotency"
                            )
                            .returning(threads)
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    task_rows = (
                        (
                            await conn.execute(
                                select(tasks)
                                .where(tasks.c.project_id == project_id, tasks.c.id.in_(ids))
                                .order_by(tasks.c.id)
                                .with_for_update(read=True)
                            )
                        )
                        .mappings()
                        .all()
                    )
                    if len(task_rows) != len(ids) or any(
                        row["status"] in TERMINAL_TASK_STATUSES for row in task_rows
                    ):
                        raise CollaborationError("invalid", "Invalid collaboration member set")
                    await conn.execute(
                        insert(members),
                        [
                            dict(thread_id=existing["id"], task_id=task_id, invited_at=now)
                            for task_id in ids
                        ],
                    )
                    await conn.execute(
                        pg_insert(messages)
                        .values(
                            [
                                dict(
                                    id=f"collab:{existing['id']}:invite:{task_id}",
                                    project_id=project_id,
                                    from_kind="system",
                                    from_id="collaboration",
                                    to_kind="task",
                                    to_id=task_id,
                                    thread_id=existing["id"],
                                    body_kind="collaboration_invite",
                                    body=policy.render_invite(dict(existing), ids),
                                    created_at=now,
                                )
                                for task_id in ids
                            ]
                        )
                        .on_conflict_do_nothing(index_elements=[messages.c.id])
                    )
                else:
                    existing = (
                        (await conn.execute(select(threads).where(key_filter))).mappings().one()
                    )
            if existing["request_hash"] != request_hash:
                raise CollaborationError(
                    "idempotency_conflict", "Collaboration key has another request"
                )
            return await self.get_collaboration_thread(
                existing["id"], project_id=project_id, conn=conn
            )

    async def _active_collaboration(self, conn, thread, now) -> bool:
        if thread["state"] != "active":
            return False
        if thread["deadline_at"] <= now:
            await self.close_collaboration_thread(
                thread_id=thread["id"],
                project_id=thread["project_id"],
                reason="expired",
                now=now,
                conn=conn,
            )
            return False
        return True

    async def _collaboration_claim(self, conn, project_id, task_id, claim_epoch) -> dict:
        task = (
            (
                await conn.execute(
                    select(tasks)
                    .where(tasks.c.id == task_id, tasks.c.project_id == project_id)
                    .with_for_update(read=True)
                )
            )
            .mappings()
            .first()
        )
        if not task or task["status"] != "IN_PROGRESS" or task["claim_epoch"] != claim_epoch:
            raise CollaborationError(
                "stale_claim", "Collaboration requires the current live task claim"
            )
        return dict(task)

    async def accept_collaboration(
        self,
        *,
        thread_id,
        project_id,
        task_id,
        claim_epoch,
        now: float,
    ) -> dict:
        async with self._engine.begin() as conn:
            thread = await self._locked_collaboration(conn, thread_id, project_id)
            active = await self._active_collaboration(conn, thread, now)
            if active:
                member_rows = await self._collaboration_members(conn, thread_id)
                member = next((row for row in member_rows if row["task_id"] == task_id), None)
                if not member or member["state"] == "removed":
                    raise CollaborationError("not_member", "Task is not a collaboration member")
                await self._collaboration_claim(conn, project_id, task_id, claim_epoch)
                if member["state"] != "accepted" or member["accepted_claim_epoch"] != claim_epoch:
                    await conn.execute(
                        update(members)
                        .where(members.c.thread_id == thread_id, members.c.task_id == task_id)
                        .values(state="accepted", accepted_at=now, accepted_claim_epoch=claim_epoch)
                    )
                result = await self.get_collaboration_thread(
                    thread_id, project_id=project_id, conn=conn
                )
        if not active:
            # Raise outside the owned transaction so expiry remains durable.
            raise CollaborationError("closed", "Collaboration thread is closed")
        return result

    async def get_collaboration_thread(self, thread_id, *, project_id, conn=None) -> dict | None:
        async with self._collaboration_connection(conn) as owned:
            row = (
                (
                    await owned.execute(
                        select(threads).where(
                            threads.c.id == thread_id, threads.c.project_id == project_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            member_rows = (
                await owned.execute(
                    select(
                        members,
                        tasks.c.status.label("task_status"),
                        tasks.c.claim_epoch.label("task_claim_epoch"),
                        tasks.c.assigned_agent_id,
                        archived_tasks.c.id.label("archived_id"),
                    )
                    .select_from(
                        members.outerjoin(
                            tasks,
                            and_(
                                members.c.task_id == tasks.c.id,
                                tasks.c.project_id == project_id,
                            ),
                        ).outerjoin(
                            archived_tasks,
                            and_(
                                members.c.task_id == archived_tasks.c.id,
                                archived_tasks.c.project_id == project_id,
                            ),
                        )
                    )
                    .where(members.c.thread_id == thread_id)
                    .order_by(members.c.task_id)
                )
            ).mappings()
            result = dict(row)
            result["members"] = []
            for item in member_rows:
                member = dict(item)
                member["running"] = member["task_status"] == "IN_PROGRESS" and bool(
                    member["assigned_agent_id"]
                )
                member["task_status"] = member["task_status"] or (
                    "ARCHIVED" if member["archived_id"] else "MISSING"
                )
                del member["assigned_agent_id"], member["archived_id"]
                result["members"].append(member)
            return result

    async def list_collaboration_threads(
        self,
        *,
        project_id,
        task_id=None,
        state=None,
        limit=20,
    ) -> list[dict]:
        stmt = select(threads.c.id).where(threads.c.project_id == project_id)
        if task_id is not None:
            stmt = stmt.where(
                select(members.c.task_id)
                .where(
                    members.c.thread_id == threads.c.id,
                    members.c.task_id == task_id,
                    members.c.state != "removed",
                )
                .exists()
            )
        if state is not None:
            stmt = stmt.where(threads.c.state == state)
        stmt = stmt.order_by(threads.c.created_at.desc(), threads.c.id).limit(
            max(0, min(limit, 100))
        )
        async with self._engine.begin() as conn:
            ids = (await conn.execute(stmt)).scalars().all()
            return [
                await self.get_collaboration_thread(value, project_id=project_id, conn=conn)
                for value in ids
            ]

    async def read_collaboration_messages(
        self,
        thread_id,
        *,
        project_id,
        after_seq=None,
        limit=20,
        max_bytes=32768,
        conn=None,
    ) -> dict | None:
        if after_seq is not None and (not isinstance(after_seq, int) or after_seq < 0):
            raise CollaborationError("invalid", "after_seq must be a nonnegative integer")
        limit = max(0, min(limit, policy.MAX_READ_MESSAGES))
        max_bytes = max(0, min(max_bytes, policy.MAX_READ_BYTES))
        async with self._collaboration_connection(conn) as owned:
            exists = await owned.scalar(
                select(threads.c.id).where(
                    threads.c.id == thread_id, threads.c.project_id == project_id
                )
            )
            if exists is None:
                return None
            stmt = (
                select(
                    links.c.seq,
                    links.c.message_id,
                    links.c.sender_task_id,
                    links.c.body_bytes,
                    links.c.created_at,
                    messages.c.subject,
                    messages.c.body,
                )
                .select_from(
                    links.outerjoin(
                        messages,
                        and_(
                            links.c.message_id == messages.c.id,
                            messages.c.project_id == project_id,
                        ),
                    )
                )
                .where(links.c.thread_id == thread_id)
            )
            if after_seq is None:
                stmt = stmt.order_by(links.c.seq.desc())
            else:
                stmt = stmt.where(links.c.seq > after_seq).order_by(links.c.seq)
            rows = (await owned.execute(stmt.limit(limit + 1))).mappings().all()
            page = []
            used_bytes = 0
            for row in rows:
                size = row["body_bytes"] if row["body"] is not None else 0
                if len(page) >= limit or used_bytes + size > max_bytes:
                    break
                item = dict(row)
                del item["body_bytes"]
                page.append(item)
                used_bytes += size
            if after_seq is None:
                page.reverse()
            return dict(
                messages=page,
                next_cursor=page[-1]["seq"] if page else after_seq,
                has_more=len(rows) > len(page),
            )

    async def _collaboration_send_result(self, conn, link, project_id, *, replayed) -> dict:
        # Copy ids share the canonical id prefix, allowing replay to return the
        # original delivery ids without adding recipient storage to the link table.
        message_id = link["message_id"]
        copies = []
        if message_id:
            copies = (
                await conn.execute(
                    select(messages.c.id, messages.c.created_seq)
                    .where(
                        messages.c.project_id == project_id,
                        or_(
                            messages.c.id == message_id, messages.c.id.startswith(message_id + ":")
                        ),
                    )
                    .order_by(messages.c.created_seq)
                )
            ).all()
        return dict(
            seq=link["seq"],
            message_id=message_id,
            message_ids=[row.id for row in copies],
            created_seq=copies[0].created_seq if copies else None,
            replayed=replayed,
            thread=await self.get_collaboration_thread(
                link["thread_id"],
                project_id=project_id,
                conn=conn,
            ),
        )

    async def append_collaboration_message(
        self,
        *,
        thread_id,
        project_id,
        sender_task_id,
        sender_claim_epoch,
        sender_session_id,
        client_key=None,
        body,
        subject=None,
        reply_to_id=None,
        to_task_id=None,
        now: float,
    ) -> dict:
        client_key = client_key if client_key is not None else uuid.uuid4().hex
        if not isinstance(client_key, str) or not client_key.strip():
            raise CollaborationError("invalid", "client_key must be nonempty")
        async with self._engine.begin() as conn:
            thread = await self._locked_collaboration(conn, thread_id, project_id)
            replay = (
                (
                    await conn.execute(
                        select(links).where(
                            links.c.thread_id == thread_id,
                            links.c.sender_task_id == sender_task_id,
                            links.c.sender_claim_epoch == sender_claim_epoch,
                            links.c.client_key == client_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if replay is not None and replay["message_id"] is None:
                raise CollaborationError("closed", "Collaboration content retention has ended")
            active = replay is not None or await self._active_collaboration(conn, thread, now)
            if active:
                member_rows = await self._collaboration_members(conn, thread_id)
                sender = next(
                    (row for row in member_rows if row["task_id"] == sender_task_id), None
                )
                if not sender or sender["state"] == "removed":
                    raise CollaborationError("not_member", "Task is not a collaboration member")
                task = await self._collaboration_claim(
                    conn, project_id, sender_task_id, sender_claim_epoch
                )
                session = (
                    (
                        await conn.execute(
                            select(sessions).where(
                                sessions.c.id == sender_session_id,
                                sessions.c.project_id == project_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not session
                    or session["task_id"] != sender_task_id
                    or session["state"] not in ("starting", "running")
                    or session["desired_state"] != "running"
                    or not session["agent_id"]
                    or session["agent_id"] != task["assigned_agent_id"]
                    or (
                        session["last_claim_epoch"] is not None
                        and session["last_claim_epoch"] != sender_claim_epoch
                    )
                    or session["lifecycle"] not in ("pool", "task")
                    or (
                        session["lifecycle"] == "pool"
                        and (
                            session["claim_phase"] != "active"
                            or session["last_claim_epoch"] != sender_claim_epoch
                        )
                    )
                ):
                    raise CollaborationError(
                        "stale_claim", "Sender session does not hold the current claim"
                    )
                if (
                    sender["state"] != "accepted"
                    or sender["accepted_claim_epoch"] != sender_claim_epoch
                ):
                    raise CollaborationError(
                        "not_accepted", "Accept collaboration for the current claim first"
                    )
                if replay is not None:
                    return await self._collaboration_send_result(
                        conn, replay, project_id, replayed=True
                    )
                if not isinstance(body, str):
                    raise CollaborationError("invalid", "Message body must be text")
                body_bytes = len(body.encode("utf-8"))
                if body_bytes > policy.MAX_BODY_BYTES:
                    raise CollaborationError(
                        "message_too_large", "Collaboration body exceeds 4096 bytes"
                    )
                window = (
                    (
                        await conn.execute(
                            select(links.c.created_at)
                            .where(
                                links.c.thread_id == thread_id,
                                links.c.sender_task_id == sender_task_id,
                                links.c.created_at > now - policy.RATE_WINDOW_SECONDS,
                            )
                            .order_by(links.c.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(window) >= policy.MAX_SENDS_PER_MINUTE:
                    raise CollaborationError(
                        "rate_limited",
                        "Collaboration sender limit reached",
                        retry_after=max(0, window[0] + policy.RATE_WINDOW_SECONDS - now),
                    )
                recipients = [
                    row["task_id"]
                    for row in member_rows
                    if row["task_id"] != sender_task_id and row["state"] != "removed"
                ]
                if to_task_id is not None:
                    if to_task_id not in recipients:
                        raise CollaborationError(
                            "not_member", "Recipient is not another thread member"
                        )
                    recipients = [to_task_id]
                if not recipients:
                    raise CollaborationError("not_member", "Collaboration has no recipient members")
                seq = thread["last_seq"] + 1
                canonical_id = _new_message_id()
                for index, task_id in enumerate(recipients):
                    message_id = (
                        canonical_id if index == 0 else f"{canonical_id}:{_new_message_id()}"
                    )
                    await conn.execute(
                        insert(messages)
                        .values(
                            id=message_id,
                            project_id=project_id,
                            from_kind="session",
                            from_id=sender_session_id,
                            to_kind="task",
                            to_id=task_id,
                            thread_id=thread_id,
                            body=body,
                            subject=subject,
                            reply_to_id=reply_to_id,
                            body_kind="collaboration",
                            created_at=now,
                        )
                        .returning(messages.c.created_seq)
                    )
                link = dict(
                    thread_id=thread_id,
                    seq=seq,
                    message_id=canonical_id,
                    sender_task_id=sender_task_id,
                    sender_claim_epoch=sender_claim_epoch,
                    sender_session_id=sender_session_id,
                    client_key=client_key,
                    body_bytes=body_bytes,
                    created_at=now,
                )
                await conn.execute(insert(links).values(**link))
                message_count = thread["message_count"] + 1
                await conn.execute(
                    update(threads)
                    .where(threads.c.id == thread_id)
                    .values(
                        last_seq=seq,
                        message_count=message_count,
                        version=threads.c.version + 1,
                    )
                )
                if message_count == thread["message_budget"]:
                    await self.close_collaboration_thread(
                        thread_id=thread_id,
                        project_id=project_id,
                        reason="budget_exhausted",
                        now=now,
                        conn=conn,
                    )
                result = await self._collaboration_send_result(
                    conn, link, project_id, replayed=False
                )
        if not active:
            raise CollaborationError("closed", "Collaboration thread is closed")
        return result

    async def close_collaboration_thread(
        self,
        *,
        thread_id,
        project_id,
        reason,
        note=None,
        now: float,
        conn=None,
    ) -> dict:
        if reason not in policy.CLOSE_REASONS:
            raise CollaborationError("invalid", "Invalid collaboration close reason")
        async with self._collaboration_connection(conn) as owned:
            thread = await self._locked_collaboration(owned, thread_id, project_id)
            if thread["state"] == "active":
                member_rows = await self._collaboration_members(owned, thread_id)
                values = dict(
                    state="expired" if reason == "expired" else "closed",
                    close_reason=reason,
                    closed_at=now,
                    version=thread["version"] + 1,
                    final_result=dict(
                        reason=reason,
                        note=note,
                        message_count=thread["message_count"],
                        last_seq=thread["last_seq"],
                    ),
                )
                await owned.execute(
                    update(threads).where(threads.c.id == thread_id).values(**values)
                )
                thread.update(values)
                deliveries = [
                    dict(
                        id=f"collab:{thread_id}:closed:{member['task_id']}",
                        project_id=project_id,
                        from_kind="system",
                        from_id="collaboration",
                        to_kind="task",
                        to_id=member["task_id"],
                        thread_id=thread_id,
                        body_kind="collaboration_closed",
                        body=policy.render_closed(thread),
                        created_at=now,
                    )
                    for member in member_rows
                    if member["state"] != "removed"
                ]
                if deliveries:
                    await owned.execute(
                        pg_insert(messages)
                        .values(deliveries)
                        .on_conflict_do_nothing(index_elements=[messages.c.id])
                    )
            return await self.get_collaboration_thread(thread_id, project_id=project_id, conn=owned)

    async def remove_collaboration_member(
        self,
        *,
        thread_id,
        project_id,
        task_id,
        now: float,
    ) -> dict:
        async with self._engine.begin() as conn:
            thread = await self._locked_collaboration(conn, thread_id, project_id)
            member_rows = await self._collaboration_members(conn, thread_id)
            member = next((row for row in member_rows if row["task_id"] == task_id), None)
            if member is None:
                raise CollaborationError("not_member", "Task is not a collaboration member")
            if member["state"] != "removed":
                await conn.execute(
                    update(members)
                    .where(members.c.thread_id == thread_id, members.c.task_id == task_id)
                    .values(state="removed", removed_at=now)
                )
                member["state"] = "removed"
                if (
                    thread["state"] == "active"
                    and sum(row["state"] != "removed" for row in member_rows) < policy.MIN_MEMBERS
                ):
                    await self.close_collaboration_thread(
                        thread_id=thread_id,
                        project_id=project_id,
                        reason="members_below_two",
                        now=now,
                        conn=conn,
                    )
                else:
                    await conn.execute(
                        update(threads)
                        .where(threads.c.id == thread_id)
                        .values(version=threads.c.version + 1)
                    )
            return await self.get_collaboration_thread(thread_id, project_id=project_id, conn=conn)
