"""Session-fenced schedules and atomic, deduplicated message outbox writes.

Lock order is session, held task (via _wait_owner), schedule, message. The
daemon commands are the sole callers of these mutation methods.
"""

from __future__ import annotations

import uuid

from sqlalchemy import case, func, insert, select, update

from src.agent_cron import MAX_ACTIVE, MAX_ATTEMPTS, PENDING_TTL, CronError, due_count, next_fire
from src.agent_waits import WaitError
from src.database.tables import agent_cron as cron, messages, sessions


class AgentCronQueriesMixin:
    async def _cron_owner(self, conn, identity):
        session = (
            (
                await conn.execute(
                    select(sessions)
                    .where(sessions.c.id == identity["session_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if (
            not session
            or not identity["instance_token"]
            or session["instance_token"] != identity["instance_token"]
            or session["state"] not in ("starting", "running")
            or session["desired_state"] != "running"
            or session["project_id"] != identity["project_id"]
        ):
            raise CronError("out_of_scope", "a live matching owner session is required")
        task_id, epoch = None, 0
        if session["lifecycle"] == "named" and session["profile_id"] == "supervisor":
            if not identity["elevated"]:
                raise CronError("out_of_scope", "supervisor ownership requires elevated scope")
        else:
            try:
                owner = await self._wait_owner(conn, **identity)
            except WaitError as exc:
                raise CronError(exc.code, str(exc)) from exc
            task_id, epoch = owner["owner_id"], owner["claim_epoch"]
        return {
            "project_id": session["project_id"],
            "session_id": session["id"],
            "session_instance_token": session["instance_token"],
            "owner_task_id": task_id,
            "claim_epoch": epoch,
        }

    async def _cron_current(self, conn, row):
        try:
            owner = await self._cron_owner(
                conn,
                {
                    "session_id": row["session_id"],
                    "instance_token": row["session_instance_token"],
                    "project_id": row["project_id"],
                    "claim_epoch": row["claim_epoch"],
                    "elevated": row["owner_task_id"] is None,
                },
            )
            return all(owner[key] == row[key] for key in owner)
        except CronError:
            return False

    async def register_agent_cron(self, *, identity, prompt, recurrence, key, now):
        async with self._engine.begin() as conn:
            owner = await self._cron_owner(conn, identity)
            old = (
                (
                    await conn.execute(
                        select(cron).where(
                            cron.c.session_id == owner["session_id"],
                            cron.c.session_instance_token == owner["session_instance_token"],
                            cron.c.idempotency_key == key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if old:
                if (
                    old["prompt"] != prompt
                    or old["recurrence"] != recurrence
                    or old["owner_task_id"] != owner["owner_task_id"]
                    or old["claim_epoch"] != owner["claim_epoch"]
                ):
                    raise CronError("cron.idempotency_conflict", "key names a different schedule")
                return dict(old)
            count = await conn.scalar(
                select(func.count())
                .select_from(cron)
                .where(
                    cron.c.session_id == owner["session_id"],
                    cron.c.session_instance_token == owner["session_instance_token"],
                    cron.c.state == "active",
                )
            )
            if count >= MAX_ACTIVE:
                raise CronError("cron.limit", "10 active schedules per session are permitted")
            row = dict(
                owner,
                id=str(uuid.uuid4()),
                prompt=prompt,
                recurrence=recurrence,
                idempotency_key=key,
                created_at=now,
                next_fire_at=next_fire(recurrence, now),
            )
            return dict(
                (await conn.execute(insert(cron).values(**row).returning(cron))).mappings().one()
            )

    async def get_agent_cron(self, schedule_id):
        async with self._engine.connect() as conn:
            row = await conn.execute(select(cron).where(cron.c.id == schedule_id))
            row = row.mappings().first()
            if not row:
                return None
            result = dict(row)
            message_id = row["pending_message_id"] or (
                f"cron:{row['id']}:{row['tick_count']}" if row["tick_count"] else None
            )
            if message_id:
                message = (
                    (await conn.execute(select(messages).where(messages.c.id == message_id)))
                    .mappings()
                    .first()
                )
                if message:
                    result["delivery_receipt"] = {
                        key: message[key]
                        for key in ("delivered_at", "read_at", "via", "archived_at")
                    }
                    result["delivery_receipt"]["message_id"] = message_id
            return result

    async def list_agent_cron(self, *, session_id, instance_token, limit=100, offset=0):
        async with self._engine.connect() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        select(cron)
                        .where(
                            cron.c.session_id == session_id,
                            cron.c.session_instance_token == instance_token,
                        )
                        .order_by(cron.c.created_at.desc(), cron.c.id)
                        .limit(limit)
                        .offset(offset)
                    )
                ).mappings()
            ]

    async def _cron_archive_pending(self, conn, row, now):
        if row["pending_message_id"]:
            await conn.execute(
                update(messages)
                .where(
                    messages.c.id == row["pending_message_id"],
                    messages.c.delivered_at.is_(None),
                )
                .values(archived_at=now)
            )

    async def _cron_stop(self, conn, row, now, state):
        await self._cron_archive_pending(conn, row, now)
        await conn.execute(
            update(cron)
            .where(cron.c.id == row["id"])
            .values(
                state=state,
                stopped_at=now,
                last_delivery_status=state,
            )
        )

    async def _cancel_sleeping_session_cron(self, conn, session, now):
        """Cancel this instance's schedules while the caller holds its session lock."""
