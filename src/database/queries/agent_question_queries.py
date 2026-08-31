"""Durable question identity, answer CAS, and transport receipts."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from sqlalchemy import insert, or_, select, update, text
from sqlalchemy.exc import IntegrityError

from src.database.tables import (
    agent_questions as questions,
    message_discord_receipts,
    messages,
    sessions,
    tasks,
    agents,
)

PENDING_QUESTION_STATES = ("supervisor", "human", "answered")


class AgentQuestionQueriesMixin:
    async def create_agent_question(self, **values) -> bool:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(insert(questions).values(**values))
            return True
        except IntegrityError:
            # A replay/concurrent observer is benign only for this identity.
            if await self.get_agent_question(values["id"]):
                return False
            raise

    async def get_agent_question(self, question_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(select(questions).where(questions.c.id == question_id)))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def list_agent_questions(
        self, project_id=None, session_id=None, pending_only=True
    ) -> list[dict]:
        stmt = select(questions)
        if project_id is not None:
            stmt = stmt.where(questions.c.project_id == project_id)
        if session_id is not None:
            stmt = stmt.where(questions.c.session_id == session_id)
        if pending_only:
            stmt = stmt.where(questions.c.state.in_(PENDING_QUESTION_STATES))
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(stmt.order_by(questions.c.created_at, questions.c.id))
            ).mappings()
            return [dict(row) for row in rows]

    async def transition_agent_question(self, question_id, expected_states, **values) -> bool:
        values.setdefault("updated_at", time.time())
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(questions)
                .where(questions.c.id == question_id, questions.c.state.in_(expected_states))
                .values(**values)
            )
            if result.rowcount == 1 and values.get("state") != "supervisor":
                await conn.execute(
                    update(messages)
                    .where(messages.c.id == "msg-" + question_id)
                    .values(archived_at=values["updated_at"])
                )
            return result.rowcount == 1

    async def mark_agent_question_notified(self, question_id, channel_id, message_id) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(questions)
                .where(questions.c.id == question_id, questions.c.discord_message_id.is_(None))
                .values(discord_channel_id=str(channel_id), discord_message_id=str(message_id))
            )

    async def claim_agent_question_notification(self, question_id, now) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(questions)
                .where(
                    questions.c.id == question_id,
                    questions.c.state == "human",
                    questions.c.discord_message_id.is_(None),
                    questions.c.notification_next_at <= now,
                )
                .values(
                    notification_next_at=now + 60,
                    notification_attempts=questions.c.notification_attempts + 1,
                )
            )
            return result.rowcount == 1

    async def queue_agent_question_supervisor(self, question_id, body, recipient, now) -> None:
        # Receipt and queue insertion share a transaction: a daemon crash
        # cannot leave a routed question with no message or enqueue it twice.
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(questions)
                .where(
                    questions.c.id == question_id,
                    questions.c.state == "supervisor",
                    questions.c.supervisor_routed_at.is_(None),
                )
                .values(supervisor_routed_at=now)
            )
            if result.rowcount != 1:
                return
            await conn.execute(
                insert(messages).values(
                    id="msg-" + question_id,
                    project_id=None,
                    from_kind="system",
                    from_id="agent-questions",
                    to_kind="session",
                    to_id=recipient,
                    body=body,
                    subject="Worker question " + question_id,
                    created_at=now,
                    archive_after_inject=1,
                    priority=50,
                    body_kind="agent_question",
                )
            )

    async def claim_agent_question_delivery(self, question_id, token, now) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(questions)
                .where(
                    questions.c.id == question_id,
                    questions.c.state == "answered",
                    or_(
                        questions.c.delivery_lease_until.is_(None),
                        questions.c.delivery_lease_until < now,
                    ),
                )
                .values(delivery_token=token, delivery_lease_until=now + 120)
            )
            return result.rowcount == 1

    async def finish_agent_question_delivery(self, question_id, token, *, delivered, now) -> bool:
        fields = dict(delivery_token=None, delivery_lease_until=None, updated_at=now)
        if delivered:
            fields.update(state="delivered", delivered_at=now)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(questions)
                .where(
                    questions.c.id == question_id,
                    questions.c.state == "answered",
                    questions.c.delivery_token == token,
                )
                .values(**fields)
            )
            return result.rowcount == 1

    @asynccontextmanager
    async def agent_question_delivery_guard(self, question_id, token):
        """Fence the claim for the bounded terminal submission transaction.

        SQLite's write reservation and PostgreSQL's row locks block claim,
        session and answer transitions until submission/receipt commits.
        Callers MUST bound external I/O while holding this context and must
        use this connection, never a nested database connection.
        """
        async with self._engine.begin() as conn:
            if conn.dialect.name == "sqlite":
                await conn.execute(text("BEGIN IMMEDIATE"))
            stmt = (
                select(questions.c.id)
                .select_from(
                    questions.join(tasks, tasks.c.id == questions.c.task_id)
                    .join(sessions, sessions.c.id == questions.c.session_id)
                    .join(agents, agents.c.id == questions.c.agent_id)
                )
                .where(
                    questions.c.id == question_id,
                    questions.c.state == "answered",
                    questions.c.delivery_token == token,
                    tasks.c.claim_epoch == questions.c.claim_epoch,
                    tasks.c.assigned_agent_id == questions.c.agent_id,
                    tasks.c.project_id == questions.c.project_id,
                    agents.c.current_task_id == tasks.c.id,
                    agents.c.deleted_at.is_(None),
                    sessions.c.task_id == tasks.c.id,
                    sessions.c.project_id == questions.c.project_id,
                    sessions.c.name == questions.c.session_name,
                    sessions.c.instance_token == questions.c.instance_token,
                    sessions.c.state.in_(("starting", "running", "draining")),
                    sessions.c.desired_state == "running",
                    sessions.c.profile_id != "supervisor",
                    or_(sessions.c.agent_id.is_(None), sessions.c.agent_id == questions.c.agent_id),
                    or_(
                        sessions.c.last_claim_epoch.is_(None),
                        sessions.c.last_claim_epoch == tasks.c.claim_epoch,
                    ),
                    or_(
                        sessions.c.lifecycle == "task",
                        (sessions.c.lifecycle == "pool") & (sessions.c.claim_phase == "active"),
                    ),
                )
                .with_for_update()
            )
            # Task status is persisted by enum name, like the other queries.
            from src.models import TaskStatus

            stmt = stmt.where(tasks.c.status == TaskStatus.IN_PROGRESS.value)
            found = (await conn.execute(stmt)).first()
            yield conn if found else None

    async def record_agent_question_delivery(self, conn, question_id, token, session_id, now):
        await conn.execute(
            update(questions)
            .where(
                questions.c.id == question_id,
                questions.c.delivery_token == token,
                questions.c.state == "answered",
            )
            .values(
                state="delivered",
                delivered_at=now,
                updated_at=now,
                delivery_token=None,
                delivery_lease_until=None,
            )
        )
        await conn.execute(
            update(sessions)
            .where(
                sessions.c.id == session_id,
                or_(sessions.c.last_activity.is_(None), sessions.c.last_activity < now),
            )
            .values(last_activity=now)
        )

    async def get_message_discord_receipt(self, message_id) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(message_discord_receipts).where(
                            message_discord_receipts.c.message_id == message_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def ensure_message_discord_notification(self, message_id) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(insert(message_discord_receipts).values(message_id=message_id))
        except IntegrityError:
            if not await self.get_message_discord_receipt(message_id):
                raise

    async def list_pending_message_discord_notifications(self, limit=100) -> list[str]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(message_discord_receipts.c.message_id)
                .join(messages, messages.c.id == message_discord_receipts.c.message_id)
                .where(
                    message_discord_receipts.c.discord_message_id.is_(None),
                    messages.c.archived_at.is_(None),
                )
                .order_by(messages.c.created_at)
                .limit(limit)
            )
            return list(rows.scalars())

    async def mark_message_discord_notified(
        self, message_id, channel_id, discord_message_id
    ) -> None:
        await self.ensure_message_discord_notification(message_id)
        async with self._engine.begin() as conn:
            await conn.execute(
                update(message_discord_receipts)
                .where(
                    message_discord_receipts.c.message_id == message_id,
                    message_discord_receipts.c.discord_message_id.is_(None),
                )
                .values(
                    discord_channel_id=str(channel_id), discord_message_id=str(discord_message_id)
                )
            )
