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

    async def mutate_agent_cron(self, schedule_id, *, identity, now, cancel=False):
        async with self._engine.begin() as conn:
            owner = await self._cron_owner(conn, identity)
            row = (
                (await conn.execute(select(cron).where(cron.c.id == schedule_id).with_for_update()))
                .mappings()
                .first()
            )
            if not row:
                raise CronError("not_found", "schedule not found")
            if any(row[key] != owner[key] for key in owner):
                raise CronError("out_of_scope", "schedule belongs to a different owner")
            if cancel and row["state"] == "active":
                await self._cron_stop(conn, row, now, "cancelled")
            elif not cancel and row["state"] == "active" and row["tick_count"]:
                message_id = row["pending_message_id"] or f"cron:{row['id']}:{row['tick_count']}"
                consumed = await conn.scalar(
                    update(messages)
                    .where(
                        messages.c.id == message_id,
                        messages.c.archived_at.is_(None),
                    )
                    .values(
                        delivered_at=func.coalesce(messages.c.delivered_at, now),
                        read_at=func.coalesce(messages.c.read_at, now),
                        via=case(
                            (messages.c.delivered_at.is_(None), "cron_consume"),
                            else_=messages.c.via,
                        ),
                    )
                    .returning(messages.c.id)
                )
                if consumed:
                    await conn.execute(
                        update(cron)
                        .where(cron.c.id == schedule_id)
                        .values(
                            last_delivery_at=func.coalesce(cron.c.last_delivery_at, now),
                            last_delivery_status="consumed",
                            last_error=None,
                        )
                    )
        return await self.get_agent_cron(schedule_id)

    async def _cron_lock_current(self, conn, candidate):
        current = await self._cron_current(conn, candidate)
        row = (
            (await conn.execute(select(cron).where(cron.c.id == candidate["id"]).with_for_update()))
            .mappings()
            .first()
        )
        return dict(row) if row else None, current

    async def reconcile_agent_cron(self, *, now, limit=100):
        async with self._engine.connect() as conn:
            candidates = [
                dict(row)
                for row in (
                    await conn.execute(
                        select(cron)
                        .where(
                            cron.c.state == "active",
                        )
                        .order_by(cron.c.checked_at, cron.c.next_fire_at, cron.c.id)
                        .limit(limit)
                    )
                ).mappings()
            ]
        queued = expired = 0
        failures = []
        for candidate in candidates:
            async with self._engine.begin() as conn:
                row, current = await self._cron_lock_current(conn, candidate)
                if not row or row["state"] != "active":
                    continue
                if not current:
                    await self._cron_stop(conn, row, now, "expired")
                    expired += 1
                    continue
                changes = {"checked_at": now}
                if row["pending_message_id"]:
                    message = (
                        (
                            await conn.execute(
                                select(messages).where(messages.c.id == row["pending_message_id"])
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if message and message["delivered_at"] is not None:
                        changes.update(
                            pending_message_id=None,
                            pending_until=None,
                            last_delivery_at=message["delivered_at"],
                            last_delivery_status="consumed" if message["read_at"] else "delivered",
                            last_error=None,
                        )
                    elif now >= row["pending_until"] or row["delivery_attempts"] >= MAX_ATTEMPTS:
                        await self._cron_archive_pending(conn, row, now)
                        changes.update(
                            pending_message_id=None,
                            pending_until=None,
                            last_delivery_status="failed",
                            last_error=row["last_error"] or "pending delivery expired",
                        )
                        failures.append({"schedule_id": row["id"], "error": changes["last_error"]})
                    else:
                        changes["last_delivery_status"] = "pending"
                if row["next_fire_at"] <= now:
                    count = due_count(row["recurrence"], row["next_fire_at"], now)
                    changes["next_fire_at"] = next_fire(row["recurrence"], now)
                    # Even if a failed/expired prompt is cleared this pass, wait
                    # for the next future tick instead of immediately retrying it.
                    if row["pending_message_id"] and not (
                        message and message["delivered_at"] is not None
                    ):
                        changes["coalesced_count"] = row["coalesced_count"] + count
                    else:
                        tick = row["tick_count"] + 1
                        message_id = f"cron:{row['id']}:{tick}"
                        await conn.execute(
                            insert(messages).values(
                                id=message_id,
                                project_id=row["project_id"],
                                from_kind="system",
                                from_id="system:agent-cron",
                                to_kind="session",
                                # The lens resolves immutable IDs before legacy names.
                                to_id=row["session_id"],
                                subject="Scheduled prompt",
                                body=row["prompt"],
                                body_kind="schedule_prompt",
                                created_at=now,
                                archive_after_inject=True,
                            )
                        )
                        changes.update(
                            tick_count=tick,
                            pending_message_id=message_id,
                            pending_until=now + PENDING_TTL,
                            delivery_attempts=0,
                            next_attempt_at=now,
                            last_delivery_status="pending",
                            coalesced_count=row["coalesced_count"] + max(0, count - 1),
                        )
                        queued += 1
                await conn.execute(update(cron).where(cron.c.id == row["id"]).values(**changes))
        return {
            "scanned": len(candidates),
            "queued": queued,
            "expired": expired,
            "failed": len(failures),
            "failures": failures,
        }

    async def begin_agent_cron_delivery(self, message_id, *, now):
        async with self._engine.connect() as conn:
            candidate = (
                (
                    await conn.execute(
                        select(cron).where(
                            cron.c.pending_message_id == message_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if not candidate:
            return False
        async with self._engine.begin() as conn:
            row, current = await self._cron_lock_current(conn, dict(candidate))
            if not row or row["state"] != "active" or row["pending_message_id"] != message_id:
                return False
            if not current:
                await self._cron_stop(conn, row, now, "expired")
                return False
            message = await conn.execute(select(messages).where(messages.c.id == message_id))
            message = message.mappings().first()
            if (
                not message
                or message["delivered_at"] is not None
                or message["archived_at"] is not None
                or now >= row["pending_until"]
                or row["delivery_attempts"] >= MAX_ATTEMPTS
                or now < row["next_attempt_at"]
            ):
                return False
            attempt = row["delivery_attempts"] + 1
            await conn.execute(
                update(cron)
                .where(cron.c.id == row["id"])
                .values(
                    delivery_attempts=attempt,
                    next_attempt_at=now + 30 * 2 ** (attempt - 1),
                    last_delivery_status="submitting",
                )
            )
            return True

    async def finish_agent_cron_delivery(self, message_id, *, now, delivered, error):
        async with self._engine.begin() as conn:
            await conn.execute(
                update(cron)
                .where(
                    cron.c.pending_message_id == message_id,
                    cron.c.state == "active",
                )
                .values(
                    last_delivery_status="delivered" if delivered else "retry",
                    last_delivery_at=now if delivered else cron.c.last_delivery_at,
                    last_error=None
                    if delivered
                    else (error or "terminal submission deferred")[:1000],
                )
            )
