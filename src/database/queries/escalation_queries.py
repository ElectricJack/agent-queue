"""Transport-neutral durable escalations, delivery leases, and digest windows.

The query layer owns the concurrency boundaries used by later command and
transport packages.  In particular, accepting a verified reply appends the
immutable conversation fact, advances the escalation revision, and queues the
logical project supervisor message in one database transaction.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    digest_windows,
    escalation_actions,
    escalation_deliveries,
    escalation_messages,
    escalations,
    messages,
)

OPEN_ESCALATION_STATES = frozenset({"needs_human", "reply_received", "resolving"})
TERMINAL_ESCALATION_STATES = frozenset({"resolved", "cancelled", "stale"})
ESCALATION_TRANSITIONS = {
    # ``reply_received`` is intentionally absent as a generic transition
    # target: only accept_escalation_reply may claim that state, because the
    # state asserts that immutable human evidence exists.
    "needs_human": frozenset({"cancelled", "stale"}),
    "reply_received": frozenset({"resolving", "cancelled", "stale"}),
    "resolving": frozenset({"resolved", "cancelled", "stale"}),
    "resolved": frozenset(),
    "cancelled": frozenset(),
    "stale": frozenset(),
}
DELIVERY_FINAL_STATES = frozenset({"sent", "retry", "unknown"})

_ESCALATION_CREATE_REQUIRED = frozenset(
    {
        "id",
        "project_id",
        "source_kind",
        "source_identity",
        "incident_key",
        "supervisor_owner",
        "summary",
        "investigation",
        "decision_requested",
        "severity",
    }
)
_ESCALATION_IDENTITY = (
    "project_id",
    "task_id",
    "source_kind",
    "source_identity",
    "incident_key",
    "supervisor_owner",
)


class EscalationConflict(ValueError):
    """A durable identity was reused for different content."""


class EscalationStateError(ValueError):
    """A requested conversation or delivery transition is invalid."""


def _row_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _require_nonempty(values: Mapping[str, Any], names: Sequence[str]) -> None:
    empty = [name for name in names if values.get(name) is None or values.get(name) == ""]
    if empty:
        raise ValueError("required non-empty fields: " + ", ".join(sorted(empty)))


def _assert_identity(row: Mapping[str, Any], values: Mapping[str, Any], fields: Sequence[str]) -> None:
    changed = [name for name in fields if row.get(name) != values.get(name)]
    if changed:
        raise EscalationConflict("identity reused with different " + ", ".join(changed))


class EscalationQueriesMixin:
    """Persistence API shared by command, supervisor, and transport services."""

    async def create_escalation(self, **values: Any) -> tuple[dict[str, Any], bool]:
        """Create an incident or return its existing row.

        The incident key is unique within a project.  Replaying the same
        source returns ``(row, False)``; reusing that key for another source is
        rejected instead of silently hiding a later incident.
        """
        missing = _ESCALATION_CREATE_REQUIRED - values.keys()
        if missing:
            raise ValueError("escalation missing: " + ", ".join(sorted(missing)))
        _require_nonempty(values, tuple(_ESCALATION_CREATE_REQUIRED))
        now = float(values.pop("now", time.time()))
        row_values = dict(values)
        row_values.setdefault("task_id", None)
        row_values.setdefault("task_title", None)
        row_values.setdefault("task_status", None)
        row_values.setdefault("choices", None)
        row_values.setdefault("state", "needs_human")
        row_values.setdefault("revision", 0)
        row_values.setdefault("terminal_outcome", None)
        row_values.setdefault("terminal_evidence", None)
        row_values.setdefault("terminal_at", None)
        row_values.setdefault("created_at", now)
        row_values.setdefault("updated_at", now)
        if row_values["state"] != "needs_human" or row_values["revision"] != 0:
            raise ValueError("new escalations start at needs_human revision 0")
        bounded = {
            "source_kind": 128,
            "source_identity": 512,
            "incident_key": 512,
            "task_title": 500,
            "task_status": 64,
            "summary": 4000,
            "investigation": 8000,
            "decision_requested": 4000,
        }
        oversized = [
            name
            for name, maximum in bounded.items()
            if row_values.get(name) is not None and len(str(row_values[name])) > maximum
        ]
        if oversized:
            raise ValueError("escalation fields exceed bounded snapshot limits: " + ", ".join(oversized))

        statement = (
            pg_insert(escalations)
            .values(**row_values)
            .on_conflict_do_nothing()
            .returning(escalations)
        )
        async with self.immediate() as conn:
            inserted = (await conn.execute(statement)).mappings().one_or_none()
            if inserted is not None:
                return dict(inserted), True
            matches = (
                (
                    await conn.execute(
                        select(escalations).where(
                            escalations.c.project_id == row_values["project_id"],
                            or_(
                                escalations.c.incident_key == row_values["incident_key"],
                                and_(
                                    escalations.c.source_kind == row_values["source_kind"],
                                    escalations.c.source_identity == row_values["source_identity"],
                                ),
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            if len(matches) != 1:
                raise EscalationConflict("incident and source identities resolve differently")
            existing = matches[0]
            _assert_identity(existing, row_values, _ESCALATION_IDENTITY)
            return dict(existing), False

    async def get_escalation(self, escalation_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(escalations).where(escalations.c.id == escalation_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def list_escalations(
        self,
        *,
        project_id: str | None = None,
        states: Sequence[str] | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        statement = select(escalations)
        if project_id is not None:
            statement = statement.where(escalations.c.project_id == project_id)
        if states is not None:
            statement = statement.where(escalations.c.state.in_(tuple(states)))
        if task_id is not None:
            statement = statement.where(escalations.c.task_id == task_id)
        statement = statement.order_by(escalations.c.updated_at.desc(), escalations.c.id).limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def transition_escalation(
        self,
        escalation_id: str,
        *,
        expected_revision: int,
        new_state: str,
        now: float | None = None,
        summary: str | None = None,
        investigation: str | None = None,
        decision_requested: str | None = None,
        choices: list[Any] | None = None,
        severity: str | None = None,
        terminal_outcome: str | None = None,
        terminal_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Apply one legal state transition with a revision compare-and-set.

        ``None`` means the row was absent or the expected revision lost a
        race.  An invalid transition raises before writing.  This distinction
        lets command surfaces report a normal stale-revision conflict without
        conflating it with a malformed requested transition.
        """
        if new_state not in ESCALATION_TRANSITIONS:
            raise EscalationStateError(f"unknown escalation state: {new_state}")
        current = await self.get_escalation(escalation_id)
        if current is None or current["revision"] != expected_revision:
            return None
        if current["state"] in TERMINAL_ESCALATION_STATES:
            raise EscalationStateError(f"terminal escalation is immutable: {current['state']}")
        if (
            new_state != current["state"]
            and new_state not in ESCALATION_TRANSITIONS[current["state"]]
        ):
            raise EscalationStateError(
                f"invalid escalation transition: {current['state']} -> {new_state}"
            )
        terminal = new_state in TERMINAL_ESCALATION_STATES
        if terminal and not terminal_outcome:
            raise EscalationStateError("terminal escalation state requires an outcome")
        if not terminal and (terminal_outcome is not None or terminal_evidence is not None):
            raise EscalationStateError("open escalation state cannot record terminal evidence")

        changed: dict[str, Any] = {
            "state": new_state,
            "revision": escalations.c.revision + 1,
            "updated_at": float(now if now is not None else time.time()),
            "terminal_at": float(now if now is not None else time.time()) if terminal else None,
            "terminal_outcome": terminal_outcome if terminal else None,
            "terminal_evidence": dict(terminal_evidence) if terminal_evidence is not None else None,
        }
        for name, value in (
            ("summary", summary),
            ("investigation", investigation),
            ("decision_requested", decision_requested),
            ("choices", choices),
            ("severity", severity),
        ):
            if value is not None:
                changed[name] = value

        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        update(escalations)
                        .where(
                            escalations.c.id == escalation_id,
                            escalations.c.revision == expected_revision,
                            escalations.c.state == current["state"],
                        )
                        .values(**changed)
                        .returning(escalations)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def accept_escalation_reply(
        self,
        escalation_id: str,
        *,
        transport: str,
        external_message_id: str,
        verified_actor: str,
        text: str,
        received_sequence: int | None = None,
        received_at: float | None = None,
        supervisor_recipient: str | None = None,
        reply_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a verified reply and queue its supervisor notice atomically.

        A transport replay returns the original reply.  New replies to an open
        incident always advance its revision and set ``reply_received`` -- in
        particular, a reply racing a supervisor's ``resolving`` turn cannot be
        overwritten by that turn's older CAS.  Late replies are retained as
        history but terminal incidents remain terminal and no supervisor work
        is implicitly reopened.
        """
        _require_nonempty(
            {
                "transport": transport,
                "external_message_id": external_message_id,
                "verified_actor": verified_actor,
                "text": text,
            },
            ("transport", "external_message_id", "verified_actor", "text"),
        )
        if len(text) > 16000:
            raise ValueError("reply text exceeds 16000 characters")
        now = float(received_at if received_at is not None else time.time())
        reply_id = reply_id or f"escalation-message-{uuid.uuid4()}"
        supervisor_message_id = f"msg-{reply_id}"

        async with self.immediate() as conn:
            incident = (
                (
                    await conn.execute(
                        select(escalations)
                        .where(escalations.c.id == escalation_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if incident is None:
                raise ValueError("escalation does not exist")

            existing = (
                (
                    await conn.execute(
                        select(escalation_messages).where(
                            escalation_messages.c.transport == transport,
                            escalation_messages.c.external_message_id == external_message_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                _assert_identity(
                    existing,
                    {
                        "escalation_id": escalation_id,
                        "direction": "inbound",
                        "transport": transport,
                        "external_message_id": external_message_id,
                        "verified_actor": verified_actor,
                        "text": text,
                    },
                    (
                        "escalation_id",
                        "direction",
                        "transport",
                        "external_message_id",
                        "verified_actor",
                        "text",
                    ),
                )
                return {
                    "reply": dict(existing),
                    "escalation": dict(incident),
                    "created": False,
                    "supervisor_enqueued": existing["supervisor_message_id"] is not None,
                    "terminal": incident["state"] in TERMINAL_ESCALATION_STATES,
                }

            terminal = incident["state"] in TERMINAL_ESCALATION_STATES
            recipient = supervisor_recipient or incident["supervisor_owner"]
            if not terminal:
                _require_nonempty({"supervisor_recipient": recipient}, ("supervisor_recipient",))
                await conn.execute(
                    pg_insert(messages).values(
                        id=supervisor_message_id,
                        project_id=incident["project_id"],
                        from_kind="user",
                        from_id=verified_actor,
                        to_kind="session",
                        to_id=recipient,
                        thread_id=escalation_id,
                        subject=f"Escalation reply {escalation_id}",
                        body=text,
                        priority=10,
                        created_at=now,
                        delivered_at=None,
                        read_at=None,
                        archive_after_inject=1,
                        archived_at=None,
                        reply_to_id=None,
                        via=None,
                        body_kind="escalation_reply",
                        pane_open=None,
                    )
                )

            reply_values = {
                "id": reply_id,
                "escalation_id": escalation_id,
                "direction": "inbound",
                "transport": transport,
                "verified_actor": verified_actor,
                "text": text,
                "external_message_id": external_message_id,
                "received_sequence": received_sequence,
                "received_at": now,
                "supervisor_message_id": None if terminal else supervisor_message_id,
                "created_at": now,
            }
            await conn.execute(pg_insert(escalation_messages).values(**reply_values))
            updated = dict(incident)
            if not terminal:
                next_revision = int(incident["revision"]) + 1
                changed = (
                    (
                        await conn.execute(
                            update(escalations)
                            .where(
                                escalations.c.id == escalation_id,
                                escalations.c.revision == incident["revision"],
                            )
                            .values(
                                state="reply_received",
                                revision=next_revision,
                                updated_at=now,
                            )
                            .returning(escalations)
                        )
                    )
                    .mappings()
                    .one()
                )
                updated = dict(changed)
            return {
                "reply": reply_values,
                "escalation": updated,
                "created": True,
                "supervisor_enqueued": not terminal,
                "terminal": terminal,
            }

    async def append_escalation_message(
        self,
        escalation_id: str,
        *,
        direction: str,
        transport: str,
        verified_actor: str,
        text: str,
        external_message_id: str | None = None,
        received_sequence: int | None = None,
        received_at: float | None = None,
        message_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Append a non-reply conversation fact, idempotently when externally identified."""
        if direction not in {"inbound", "outbound"}:
            raise ValueError("direction must be inbound or outbound")
        _require_nonempty(
            {"transport": transport, "verified_actor": verified_actor, "text": text},
            ("transport", "verified_actor", "text"),
        )
        now = float(received_at if received_at is not None else time.time())
        values = {
            "id": message_id or f"escalation-message-{uuid.uuid4()}",
            "escalation_id": escalation_id,
            "direction": direction,
            "transport": transport,
            "verified_actor": verified_actor,
            "text": text,
            "external_message_id": external_message_id,
            "received_sequence": received_sequence,
            "received_at": now,
            "supervisor_message_id": None,
            "created_at": now,
        }
        statement = pg_insert(escalation_messages).values(**values)
        if external_message_id is not None:
            statement = statement.on_conflict_do_nothing(
                index_elements=["transport", "external_message_id"]
            )
        async with self.immediate() as conn:
            inserted = (
                (await conn.execute(statement.returning(escalation_messages)))
                .mappings()
                .one_or_none()
            )
            if inserted is not None:
                return dict(inserted), True
            existing = (
                (
                    await conn.execute(
                        select(escalation_messages).where(
                            escalation_messages.c.transport == transport,
                            escalation_messages.c.external_message_id == external_message_id,
                        )
                    )
                )
                .mappings()
                .one()
            )
            _assert_identity(
                existing,
                values,
                ("escalation_id", "direction", "transport", "external_message_id"),
            )
            return dict(existing), False

    async def get_escalation_message(self, reply_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(escalation_messages).where(escalation_messages.c.id == reply_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def list_escalation_messages(self, escalation_id: str) -> list[dict[str, Any]]:
        statement = (
            select(escalation_messages)
            .where(escalation_messages.c.escalation_id == escalation_id)
            .order_by(
                escalation_messages.c.received_at,
                escalation_messages.c.received_sequence.nulls_first(),
                escalation_messages.c.id,
            )
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def begin_escalation_action(
        self,
        escalation_id: str,
        *,
        reply_id: str,
        expected_revision: int,
        idempotency_key: str,
        action_kind: str,
        target_id: str,
        parameters: Mapping[str, Any],
        executor: str,
        now: float | None = None,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        """Reserve one evidence-bound action and take the resolving CAS.

        A matching idempotency replay returns the durable prior reservation,
        even after the escalation revision advances.  A new reservation must
        bind an immutable inbound reply that queued a supervisor notice; that
        condition is the database-level proof that the text crossed a trusted
        human boundary while the incident was open.
        """
        if action_kind not in {"question_answer", "gate_resolve", "task_recover"}:
            raise ValueError("unsupported escalation action")
        _require_nonempty(
            {
                "reply_id": reply_id,
                "idempotency_key": idempotency_key,
                "target_id": target_id,
                "executor": executor,
            },
            ("reply_id", "idempotency_key", "target_id", "executor"),
        )
        when = float(now if now is not None else time.time())
        async with self.immediate() as conn:
            incident = (
                (
                    await conn.execute(
                        select(escalations)
                        .where(escalations.c.id == escalation_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if incident is None:
                raise ValueError("escalation does not exist")

            existing = (
                (
                    await conn.execute(
                        select(escalation_actions).where(
                            escalation_actions.c.escalation_id == escalation_id,
                            escalation_actions.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                _assert_identity(
                    existing,
                    {
                        "reply_id": reply_id,
                        "action_kind": action_kind,
                        "target_id": target_id,
                    },
                    ("reply_id", "action_kind", "target_id"),
                )
                if dict(existing["parameters"]) != dict(parameters):
                    raise EscalationConflict(
                        "action idempotency key reused with different parameters"
                    )
                return {
                    "action": dict(existing),
                    "escalation": dict(incident),
                    "created": False,
                }

            if incident["revision"] != expected_revision:
                raise EscalationStateError("stale escalation revision")
            if incident["state"] != "reply_received":
                raise EscalationStateError(
                    f"escalation is not awaiting reply application: {incident['state']}"
                )
            reply = (
                (
                    await conn.execute(
                        select(escalation_messages).where(
                            escalation_messages.c.id == reply_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                reply is None
                or reply["escalation_id"] != escalation_id
                or reply["direction"] != "inbound"
                or reply["supervisor_message_id"] is None
            ):
                raise EscalationStateError(
                    "reply is not verified human evidence for this escalation"
                )

            next_revision = expected_revision + 1
            changed = (
                (
                    await conn.execute(
                        update(escalations)
                        .where(
                            escalations.c.id == escalation_id,
                            escalations.c.revision == expected_revision,
                            escalations.c.state == "reply_received",
                        )
                        .values(state="resolving", revision=next_revision, updated_at=when)
                        .returning(escalations)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if changed is None:  # pragma: no cover - row lock makes this defensive
                raise EscalationStateError("stale escalation revision")
            values = {
                "id": action_id or f"escalation-action-{uuid.uuid4()}",
                "escalation_id": escalation_id,
                "reply_id": reply_id,
                "idempotency_key": idempotency_key,
                "action_kind": action_kind,
                "target_id": target_id,
                "parameters": dict(parameters),
                "executor": executor,
                "started_revision": next_revision,
                "status": "processing",
                "outcome": None,
                "result": None,
                "error": None,
                "created_at": when,
                "completed_at": None,
            }
            await conn.execute(pg_insert(escalation_actions).values(**values))
            return {"action": values, "escalation": dict(changed), "created": True}

    async def finish_escalation_action(
        self,
        action_id: str,
        *,
        succeeded: bool,
        outcome: str,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record one action outcome and conditionally finish its incident.

        A newer human reply advances the escalation away from the action's
        ``started_revision``.  The action outcome is still recorded, but that
        CAS intentionally leaves the newer conversation in ``reply_received``.
        """
        _require_nonempty({"outcome": outcome}, ("outcome",))
        when = float(now if now is not None else time.time())
        async with self.immediate() as conn:
            action = (
                (
                    await conn.execute(
                        select(escalation_actions)
                        .where(escalation_actions.c.id == action_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if action is None:
                raise ValueError("escalation action does not exist")
            if action["status"] != "processing":
                incident = (
                    (
                        await conn.execute(
                            select(escalations).where(
                                escalations.c.id == action["escalation_id"]
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return {
                    "action": dict(action),
                    "escalation": dict(incident),
                    "completed": False,
                    "resolved": incident["state"] == "resolved",
                }

            completed = (
                (
                    await conn.execute(
                        update(escalation_actions)
                        .where(
                            escalation_actions.c.id == action_id,
                            escalation_actions.c.status == "processing",
                        )
                        .values(
                            status="succeeded" if succeeded else "failed",
                            outcome=outcome,
                            result=dict(result) if result is not None else None,
                            error=error,
                            completed_at=when,
                        )
                        .returning(escalation_actions)
                    )
                )
                .mappings()
                .one()
            )
            incident = (
                (
                    await conn.execute(
                        select(escalations)
                        .where(escalations.c.id == action["escalation_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            resolved = False
            if (
                incident["state"] == "resolving"
                and incident["revision"] == action["started_revision"]
            ):
                values: dict[str, Any] = {
                    "state": "resolved" if succeeded else "reply_received",
                    "revision": int(incident["revision"]) + 1,
                    "updated_at": when,
                }
                if succeeded:
                    resolved = True
                    values.update(
                        terminal_at=when,
                        terminal_outcome=outcome,
                        terminal_evidence={
                            "action_id": action_id,
                            "reply_id": action["reply_id"],
                            "action_kind": action["action_kind"],
                            "target_id": action["target_id"],
                        },
                    )
                incident = (
                    (
                        await conn.execute(
                            update(escalations)
                            .where(
                                escalations.c.id == action["escalation_id"],
                                escalations.c.state == "resolving",
                                escalations.c.revision == action["started_revision"],
                            )
                            .values(**values)
                            .returning(escalations)
                        )
                    )
                    .mappings()
                    .one()
                )
            return {
                "action": dict(completed),
                "escalation": dict(incident),
                "completed": True,
                "resolved": resolved,
            }

    async def list_escalation_actions(self, escalation_id: str) -> list[dict[str, Any]]:
        statement = (
            select(escalation_actions)
            .where(escalation_actions.c.escalation_id == escalation_id)
            .order_by(escalation_actions.c.created_at, escalation_actions.c.id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def enqueue_escalation_delivery(
        self,
        escalation_id: str,
        *,
        dedup_key: str,
        kind: str,
        payload: Mapping[str, Any],
        available_at: float,
        escalation_message_id: str | None = None,
        priority: int = 10,
        generation: int = 0,
        delivery_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        _require_nonempty(
            {"escalation_id": escalation_id, "dedup_key": dedup_key, "kind": kind},
            ("escalation_id", "dedup_key", "kind"),
        )
        now = time.time()
        values = {
            "id": delivery_id or f"escalation-delivery-{uuid.uuid4()}",
            "escalation_id": escalation_id,
            "escalation_message_id": escalation_message_id,
            "dedup_key": dedup_key,
            "kind": kind,
            "payload": dict(payload),
            "priority": priority,
            "generation": generation,
            "status": "pending",
            "attempt_count": 0,
            "next_attempt_at": available_at,
            "created_at": now,
            "updated_at": now,
        }
        statement = (
            pg_insert(escalation_deliveries)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(escalation_deliveries)
        )
        async with self.immediate() as conn:
            inserted = (await conn.execute(statement)).mappings().one_or_none()
            if inserted is not None:
                return dict(inserted), True
            existing = (
                (
                    await conn.execute(
                        select(escalation_deliveries).where(
                            escalation_deliveries.c.dedup_key == dedup_key
                        )
                    )
                )
                .mappings()
                .one()
            )
            _assert_identity(
                existing,
                values,
                ("escalation_id", "escalation_message_id", "dedup_key", "kind", "generation"),
            )
            if dict(existing["payload"]) != dict(payload):
                raise EscalationConflict("delivery identity reused with different payload")
            return dict(existing), False

    async def claim_escalation_deliveries(
        self,
        *,
        lease_owner: str,
        now: float,
        lease_seconds: float = 120.0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        _require_nonempty({"lease_owner": lease_owner}, ("lease_owner",))
        if lease_seconds <= 0 or limit <= 0:
            raise ValueError("lease_seconds and limit must be positive")
        due = or_(
            and_(
                escalation_deliveries.c.status.in_(("pending", "retry")),
                escalation_deliveries.c.next_attempt_at <= now,
            ),
            and_(
                escalation_deliveries.c.status == "sending",
                escalation_deliveries.c.lease_expires_at < now,
            ),
        )
        async with self.immediate() as conn:
            claimable = (
                (
                    await conn.execute(
                        select(escalation_deliveries.c.id, escalation_deliveries.c.status)
                        .where(due)
                        .order_by(
                            escalation_deliveries.c.priority,
                            escalation_deliveries.c.next_attempt_at,
                            escalation_deliveries.c.id,
                        )
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .all()
            )
            ids = [row.id for row in claimable]
            # A row taken from ``sending`` is one whose previous owner never
            # finished it: the process died, or the lease simply expired with an
            # external send in flight.  The dispatcher must know that, because
            # such an attempt cannot prove whether its send landed.
            reclaimed = {row.id: row.status == "sending" for row in claimable}
            if not ids:
                return []
            rows = (
                (
                    await conn.execute(
                        update(escalation_deliveries)
                        .where(escalation_deliveries.c.id.in_(ids))
                        .values(
                            status="sending",
                            attempt_count=escalation_deliveries.c.attempt_count + 1,
                            lease_owner=lease_owner,
                            lease_expires_at=now + lease_seconds,
                            updated_at=now,
                        )
                        .returning(escalation_deliveries)
                    )
                )
                .mappings()
                .all()
            )
        by_id = {row["id"]: {**dict(row), "reclaimed": reclaimed[row["id"]]} for row in rows}
        return [by_id[item_id] for item_id in ids]

    async def note_escalation_delivery_binding(
        self,
        delivery_id: str,
        *,
        lease_owner: str,
        now: float,
        channel_id: str | None = None,
        root_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a confirmed external binding without ending the attempt.

        An escalation root is more than one external write -- the channel post,
        then the thread, then its opener -- and the row has to survive a crash
        between them.  The post is real the moment the platform answers, so it
        is written straight away while the delivery stays ``sending`` under the
        same lease.  A later reclaim then sees the binding and repairs the
        missing half instead of posting a second root.

        Fenced on the lease exactly like :meth:`finish_escalation_delivery`, and
        never clears a binding: only the fields given are written.
        """
        values = {
            name: value
            for name, value in (
                ("channel_id", channel_id),
                ("root_message_id", root_message_id),
                ("thread_id", thread_id),
            )
            if value
        }
        if not values:
            return None
        values["updated_at"] = now
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        update(escalation_deliveries)
                        .where(
                            escalation_deliveries.c.id == delivery_id,
                            escalation_deliveries.c.status == "sending",
                            escalation_deliveries.c.lease_owner == lease_owner,
                        )
                        .values(**values)
                        .returning(escalation_deliveries)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def finish_escalation_delivery(
        self,
        delivery_id: str,
        *,
        lease_owner: str,
        status: str,
        now: float,
        next_attempt_at: float | None = None,
        channel_id: str | None = None,
        root_message_id: str | None = None,
        thread_id: str | None = None,
        external_receipt_id: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in DELIVERY_FINAL_STATES:
            raise EscalationStateError("delivery finish status must be sent, retry, or unknown")
        if status == "sent" and not external_receipt_id:
            raise EscalationStateError("sent delivery requires a confirmed external receipt")
        if status == "retry" and next_attempt_at is None:
            raise EscalationStateError("retry delivery requires next_attempt_at")
        values = {
            "status": status,
            "next_attempt_at": next_attempt_at if next_attempt_at is not None else now,
            "lease_owner": None,
            "lease_expires_at": None,
            "channel_id": channel_id,
            "root_message_id": root_message_id,
            "thread_id": thread_id,
            "external_receipt_id": external_receipt_id,
            "receipt_confirmed_at": now if status == "sent" else None,
            "last_error": last_error,
            "updated_at": now,
        }
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        update(escalation_deliveries)
                        .where(
                            escalation_deliveries.c.id == delivery_id,
                            escalation_deliveries.c.status == "sending",
                            escalation_deliveries.c.lease_owner == lease_owner,
                        )
                        .values(**values)
                        .returning(escalation_deliveries)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def get_escalation_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(escalation_deliveries).where(
                            escalation_deliveries.c.id == delivery_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def list_escalation_deliveries(
        self,
        escalation_id: str,
        *,
        statuses: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(escalation_deliveries).where(
            escalation_deliveries.c.escalation_id == escalation_id
        )
        if statuses is not None:
            statement = statement.where(escalation_deliveries.c.status.in_(tuple(statuses)))
        statement = statement.order_by(
            escalation_deliveries.c.priority,
            escalation_deliveries.c.created_at,
            escalation_deliveries.c.id,
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def reserve_digest_window(
        self,
        *,
        destination: str,
        config_generation: int,
        window_start: float,
        window_end: float,
        activity_cursor: Mapping[str, Any] | None,
        due_at: float,
        is_catchup: bool = False,
        window_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        _require_nonempty({"destination": destination}, ("destination",))
        if config_generation < 0 or window_end <= window_start:
            raise ValueError("invalid digest generation or window bounds")
        now = time.time()
        values = {
            "id": window_id or f"digest-window-{uuid.uuid4()}",
            "destination": destination,
            "config_generation": config_generation,
            "window_start": window_start,
            "window_end": window_end,
            "activity_cursor": dict(activity_cursor) if activity_cursor is not None else None,
            "due_at": due_at,
            "is_catchup": is_catchup,
            "send_status": "pending",
            "attempt_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        key = ("destination", "config_generation", "window_start", "window_end")
        statement = (
            pg_insert(digest_windows)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(key))
            .returning(digest_windows)
        )
        async with self.immediate() as conn:
            inserted = (await conn.execute(statement)).mappings().one_or_none()
            if inserted is not None:
                return dict(inserted), True
            existing = (
                (
                    await conn.execute(
                        select(digest_windows).where(
                            digest_windows.c.destination == destination,
                            digest_windows.c.config_generation == config_generation,
                            digest_windows.c.window_start == window_start,
                            digest_windows.c.window_end == window_end,
                        )
                    )
                )
                .mappings()
                .one()
            )
            _assert_identity(existing, values, (*key, "is_catchup", "due_at"))
            if existing["activity_cursor"] != values["activity_cursor"]:
                raise EscalationConflict("digest window identity reused with different cursor")
            return dict(existing), False

    async def complete_digest_evaluation(
        self,
        window_id: str,
        *,
        activity_cursor: Mapping[str, Any],
        output_hash: str | None,
        payload: Mapping[str, Any] | None,
        suppression_reason: str | None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Persist either sendable output or a durable silent-window result."""
        suppressed = suppression_reason is not None
        if suppressed == (payload is not None):
            raise ValueError("digest evaluation requires either payload or suppression_reason")
        if payload is not None and not output_hash:
            raise ValueError("sendable digest output requires output_hash")
        values = {
            "activity_cursor": dict(activity_cursor),
            "output_hash": output_hash,
            "payload": dict(payload) if payload is not None else None,
            "send_status": "suppressed" if suppressed else "pending",
            "suppression_reason": suppression_reason,
            "updated_at": float(now if now is not None else time.time()),
        }
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        update(digest_windows)
                        .where(
                            digest_windows.c.id == window_id,
                            digest_windows.c.send_status == "pending",
                            digest_windows.c.payload.is_(None),
                            digest_windows.c.suppression_reason.is_(None),
                        )
                        .values(**values)
                        .returning(digest_windows)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def claim_digest_windows(
        self,
        *,
        lease_owner: str,
        now: float,
        lease_seconds: float = 120.0,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        _require_nonempty({"lease_owner": lease_owner}, ("lease_owner",))
        if lease_seconds <= 0 or limit <= 0:
            raise ValueError("lease_seconds and limit must be positive")
        due = or_(
            and_(
                digest_windows.c.send_status.in_(("pending", "retry")),
                digest_windows.c.payload.is_not(None),
                digest_windows.c.due_at <= now,
            ),
            and_(
                digest_windows.c.send_status == "sending",
                digest_windows.c.lease_expires_at < now,
            ),
        )
        async with self.immediate() as conn:
            ids = (
                (
                    await conn.execute(
                        select(digest_windows.c.id)
                        .where(due)
                        .order_by(digest_windows.c.due_at, digest_windows.c.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                return []
            rows = (
                (
                    await conn.execute(
                        update(digest_windows)
                        .where(digest_windows.c.id.in_(ids))
                        .values(
                            send_status="sending",
                            attempt_count=digest_windows.c.attempt_count + 1,
                            lease_owner=lease_owner,
                            lease_expires_at=now + lease_seconds,
                            updated_at=now,
                        )
                        .returning(digest_windows)
                    )
                )
                .mappings()
                .all()
            )
        by_id = {row["id"]: dict(row) for row in rows}
        return [by_id[item_id] for item_id in ids]

    async def finish_digest_delivery(
        self,
        window_id: str,
        *,
        lease_owner: str,
        status: str,
        now: float,
        next_attempt_at: float | None = None,
        external_receipt_id: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in DELIVERY_FINAL_STATES:
            raise EscalationStateError("digest finish status must be sent, retry, or unknown")
        if status == "sent" and not external_receipt_id:
            raise EscalationStateError("sent digest requires a confirmed external receipt")
        if status == "retry" and next_attempt_at is None:
            raise EscalationStateError("retry digest requires next_attempt_at")
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        update(digest_windows)
                        .where(
                            digest_windows.c.id == window_id,
                            digest_windows.c.send_status == "sending",
                            digest_windows.c.lease_owner == lease_owner,
                        )
                        .values(
                            send_status=status,
                            due_at=next_attempt_at if next_attempt_at is not None else now,
                            lease_owner=None,
                            lease_expires_at=None,
                            external_receipt_id=external_receipt_id,
                            receipt_confirmed_at=now if status == "sent" else None,
                            last_error=last_error,
                            updated_at=now,
                        )
                        .returning(digest_windows)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def get_digest_window(self, window_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(digest_windows).where(digest_windows.c.id == window_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _row_dict(row)

    async def list_digest_windows(
        self,
        *,
        destination: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        statement = select(digest_windows)
        if destination is not None:
            statement = statement.where(digest_windows.c.destination == destination)
        if statuses is not None:
            statement = statement.where(digest_windows.c.send_status.in_(tuple(statuses)))
        statement = statement.order_by(
            digest_windows.c.window_end.desc(), digest_windows.c.id
        ).limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]
