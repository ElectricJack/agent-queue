"""Atomic one-shot notification receipts; no worker lifecycle ownership."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import project_schedules as schedules
from src.project_schedules import ScheduleError, visible_schedule


class ProjectScheduleQueriesMixin:
    async def register_project_schedule(self, *, project_id, due_at, body, key, now):
        async with self._engine.begin() as conn:
            identity = (schedules.c.project_id == project_id, schedules.c.idempotency_key == key)
            row = (await conn.execute(select(schedules).where(*identity))).mappings().first()
            if row is None:
                if due_at <= now:
                    raise ScheduleError("schedule.invalid", "due_at must be in the future")
                await conn.execute(insert(schedules).values(
                    id=f"sched-{uuid.uuid4().hex}", project_id=project_id,
                    due_at=due_at, body=body, idempotency_key=key, state="pending", created_at=now,
                ).on_conflict_do_nothing(index_elements=["project_id", "idempotency_key"]))
                row = (await conn.execute(select(schedules).where(*identity))).mappings().one()
            if row["due_at"] != due_at or row["body"] != body:
                raise ScheduleError("schedule.key_conflict", "idempotency key has a different schedule")
            return dict(row)

    async def get_project_schedule(self, *, project_id, schedule_id):
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(schedules).where(
                schedules.c.project_id == project_id, schedules.c.id == schedule_id,
            ))).mappings().first()
            if row is None:
                raise ScheduleError("schedule.not_found", "schedule not found")
            return dict(row)

    async def list_project_schedules(self, *, project_id, state=None, limit=100, offset=0):
        query = select(schedules).where(schedules.c.project_id == project_id)
        if state is not None:
            query = query.where(schedules.c.state == state)
        async with self._engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(query.order_by(
                schedules.c.due_at, schedules.c.id,
            ).limit(limit).offset(offset))).mappings()]

    async def cancel_project_schedule(self, *, project_id, schedule_id, now):
        async with self._engine.begin() as conn:
            row = (await conn.execute(select(schedules).where(
                schedules.c.project_id == project_id, schedules.c.id == schedule_id,
            ).with_for_update())).mappings().first()
            if row is None:
                raise ScheduleError("schedule.not_found", "schedule not found")
            if row["state"] == "pending":
                row = (await conn.execute(update(schedules).where(
                    schedules.c.id == schedule_id,
                ).values(state="cancelled", cancelled_at=now).returning(schedules))).mappings().one()
            return dict(row)

    async def fire_project_schedules(self, *, now):
        async with self._engine.begin() as conn:
            rows = (await conn.execute(select(schedules).where(
                schedules.c.state == "pending", schedules.c.due_at <= now,
            ).order_by(schedules.c.due_at, schedules.c.id).limit(100).with_for_update(
                skip_locked=True,
            ))).mappings().all()
            for row in rows:
                due = visible_schedule(row)["due_at_utc"]
                message = await self.create_message_in_transaction(
                    conn, project_id=row["project_id"], from_kind="system",
                    from_id="project-schedules", to_kind="profile", to_id="supervisor",
                    subject=f"Scheduled notification {row['id']}",
                    body=f"Scheduled for {due} ({row['id']}):\n\n{row['body']}",
                    body_kind="project_schedule", thread_id=row["id"],
                )
                await conn.execute(update(schedules).where(schedules.c.id == row["id"]).values(
                    state="fired", fired_at=now, message_id=message.id,
                ))
            return len(rows)
