"""Connection-aware shared admission and discussion queries.

Input routing is single-owner: admission commits before correlation, and only
that queued row may be continued after a consumer failure.
"""

import hashlib
import time

from sqlalchemy import Float, and_, cast, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.asks.models import AskError, digest
from src.conversations.limits import AUTHOR_WINDOW_LIMIT, CHANNEL_WINDOW_LIMIT, WINDOW_SECONDS
from src.database.tables import conversation_inputs, escalation_messages, human_asks, messages


class AskInputQueriesMixin:
    async def get_ask_input(self, input_id, *, conn, lock=False):
        stmt = select(conversation_inputs).where(conversation_inputs.c.id == input_id)
        if lock:
            stmt = stmt.with_for_update()
        row = (await conn.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def admit_ask_input(self, values, *, conn, enforce_limits=True):
        for key in (
            f"human-input:{values['transport']}:{values['external_message_id']}",
            f"conversation:author:{values['author_id']}",
            f"conversation:channel:{values['channel_id']}",
        ):
            await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
        existing = (
            (
                await conn.execute(
                    select(conversation_inputs)
                    .where(
                        conversation_inputs.c.transport == values["transport"],
                        conversation_inputs.c.external_message_id == values["external_message_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if existing:
            matches = all(
                existing[key] == values[key]
                for key in (
                    "verified_actor",
                    "text_sha256",
                )
            )
            if not matches:
                await self.update_ask_input(
                    existing["id"],
                    {
                        "routing_state": "quarantined",
                        "routing_evidence": {
                            **existing["routing_evidence"],
                            "conflict": {
                                key: values[key]
                                for key in ("verified_actor", "text_sha256", "source")
                            },
                        },
                    },
                    conn=conn,
                )
                project = existing["project_id"] or values.get("project_id")
                if project:
                    await self.insert_ask_reconciliation_case(
                        {
                            "id": "ask-repair-"
                            + digest([values["transport"], values["external_message_id"]]),
                            "project_id": project,
                            "conflict_key": "input:" + existing["id"],
                            "source_evidence": {
                                "input_id": existing["id"],
                                "attempt": {
                                    key: values[key]
                                    for key in ("verified_actor", "text_sha256", "source")
                                },
                            },
                            "logical_owner": f"supervisor:{project}",
                            "reason": "External input identity has conflicting actor or body",
                            "created_at": values["created_at"],
                            "updated_at": values["created_at"],
                        },
                        conn=conn,
                    )
            return dict(existing), False, matches
        if enforce_limits:
            for column, value, cap in (
                (conversation_inputs.c.author_id, values["author_id"], AUTHOR_WINDOW_LIMIT),
                (conversation_inputs.c.channel_id, values["channel_id"], CHANNEL_WINDOW_LIMIT),
            ):
                count = await conn.scalar(
                    select(func.count())
                    .select_from(conversation_inputs)
                    .where(
                        column == value,
                        conversation_inputs.c.state != "revoked",
                        conversation_inputs.c.created_at >= values["created_at"] - WINDOW_SECONDS,
                    )
                )
                if count >= cap:
                    raise AskError("ask.rate_limited", "Human input rate limit reached")
        row = (
            (
                await conn.execute(
                    pg_insert(conversation_inputs).values(**values).returning(conversation_inputs)
                )
            )
            .mappings()
            .one()
        )
        return dict(row), True, True

    async def update_ask_input(self, input_id, values, *, conn):
        allowed = {
            "conversation_id",
            "ask_id",
            "project_id",
            "routing_state",
            "routing_evidence",
            "source_version",
            "retention_entry_id",
            "receipt",
            "supervisor_message_id",
            "reply_message_id",
            "state",
            "delay_notified_at",
        }
        if set(values) - allowed:
            raise ValueError("Input evidence is immutable")
        row = (
            (
                await conn.execute(
                    update(conversation_inputs)
                    .where(
                        conversation_inputs.c.id == input_id,
                    )
                    .values(**values)
                    .returning(conversation_inputs)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    async def list_ask_history(self, ask_id, *, conn, limit=100):
        rows = [
            dict(row)
            for row in (
                await conn.execute(
                    select(escalation_messages)
                    .where(
                        escalation_messages.c.ask_id == ask_id,
                    )
                    .order_by(
                        escalation_messages.c.created_at.desc(), escalation_messages.c.id.desc()
                    )
                    .limit(limit)
                )
            ).mappings()
        ]
        return list(reversed(rows))

    async def insert_ask_message(self, values, *, conn):
        await conn.execute(
            pg_insert(messages)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["id"],
            )
        )

    async def ask_channel_enabled(self, *, channel_id, guild_id):
        """H06 owns mode transitions; absence of its durable mode is disabled.

        Read the durable table, never a YAML flag or a caller-authored envelope.
        The optional-table guard allows H05 to ship before the outbox package.
        """
        async with self._engine.connect() as conn:
            exists = await conn.scalar(text("SELECT to_regclass('human_communication_modes')"))
            if not exists:
                return False
            return bool(
                await conn.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM human_communication_modes "
                        "WHERE mode = 'channel' AND destination = 'discord:' || :guild || ':' || :channel)"
                    ),
                    {"channel": channel_id, "guild": guild_id},
                )
            )

    async def pending_ask_inputs(self, *, conn, limit=100, project_id=None):
        return [
            dict(row)
            for row in (
                await conn.execute(
                    select(conversation_inputs)
                    .where(
                        conversation_inputs.c.project_id == project_id if project_id else True,
                        or_(
                            conversation_inputs.c.routing_state == "queued",
                            and_(
                                conversation_inputs.c.routing_state.in_(
                                    ("discussion", "interpretation")
                                ),
                                conversation_inputs.c.transport == "discord",
                                conversation_inputs.c.delay_notified_at.is_(None),
                                conversation_inputs.c.created_at <= time.time() - 15,
                            ),
                        ),
                        conversation_inputs.c.state == "accepted",
                    )
                    .order_by(
                        func.coalesce(
                            cast(
                                conversation_inputs.c.routing_evidence[
                                    "last_route_attempt_at"
                                ].astext,
                                Float,
                            ),
                            conversation_inputs.c.created_at,
                        ),
                        conversation_inputs.c.id,
                    )
                    .limit(limit)
                )
            ).mappings()
        ]

    async def ask_candidates(self, project_id, *, conn):
        stmt = select(human_asks).where(
            human_asks.c.decision_state == "pending",
            human_asks.c.next_actor == "human",
            human_asks.c.context_state == "complete",
        )
        if project_id:
            stmt = stmt.where(human_asks.c.project_id == project_id)
        return [
            dict(row)
            for row in (
                await conn.execute(
                    stmt.order_by(human_asks.c.created_at, human_asks.c.id).limit(21)
                )
            ).mappings()
        ]


def text_hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
