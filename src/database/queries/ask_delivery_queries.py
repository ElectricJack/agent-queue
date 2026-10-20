"""Connection-aware transport ledger; source locks precede these rows.

An inflight operation survives lease expiry. Only a confirmed completion or
explicit reconciled repair releases it; another HTTP write cannot pass it.
"""

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.asks.models import AskError
from src.database.tables import (
    ask_action_tokens as tokens,
    ask_external_bindings as bindings,
    ask_reconciliation_cases as repairs,
    human_communication_modes as modes,
    outbound_deliveries as outbox,
)


def mapped(result):
    row = result.mappings().first()
    return dict(row) if row else None


class AskDeliveryQueriesMixin:
    async def ask_delivery_status(self, ask_id, *, conn):
        roots = [
            dict(r)
            for r in (
                await conn.execute(
                    select(bindings)
                    .where(bindings.c.ask_id == ask_id)
                    .order_by(bindings.c.created_at)
                    .limit(100)
                )
            ).mappings()
        ]
        issues = [
            dict(r)
            for r in (
                await conn.execute(
                    select(repairs)
                    .where(
                        repairs.c.source_evidence["ask_id"].as_string() == ask_id,
                        repairs.c.status == "open",
                    )
                    .order_by(repairs.c.created_at)
                    .limit(100)
                )
            ).mappings()
        ]
        status = (
            "Delivery unknown"
            if any(r["root_state"] == "unknown" for r in roots)
            or any(r["affected_deliveries"] for r in issues)
            else "Needs attention"
            if issues
            else "Sending"
            if any(r["inflight_id"] for r in roots)
            else "Pending"
            if any(r["root_state"] == "pending" for r in roots)
            else "Confirmed"
            if roots
            else "Not sent"
        )
        return {"bindings": roots, "repairs": issues, "status": status}

    async def resolve_ask_delivery_case(self, binding_id, operation_id, receipt, *, conn, now):
        await conn.execute(
            update(repairs)
            .where(
                repairs.c.conflict_key == f"delivery:{binding_id}:{operation_id}",
                repairs.c.status == "open",
            )
            .values(status="resolved", repair_receipt=receipt, updated_at=now)
        )

    async def get_ask_mode(self, project_id, destination, *, conn, lock=False):
        stmt = select(modes).where(
            modes.c.project_id == project_id, modes.c.destination == destination
        )
        return mapped(await conn.execute(stmt.with_for_update() if lock else stmt))

    async def advance_ask_mode(self, values, *, expected_generation, conn):
        # Serialize a missing row as well as an existing mode.
        from sqlalchemy import func

        await conn.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"ask-mode:{values['project_id']}:{values['destination']}", 0
                    )
                )
            )
        )
        old = await self.get_ask_mode(
            values["project_id"], values["destination"], conn=conn, lock=True
        )
        if (old["generation"] if old else 0) != expected_generation:
            raise AskError("ask.stale_mode", "Communication ownership changed")
        if old and old["migrated"] and values["mode"] == "legacy":
            raise AskError("ask.legacy_retired", "Migrated writers cannot be revived")
        # An unknown HTTP operation is still a writer, even after its lease expires.
        busy = await conn.scalar(
            select(bindings.c.id)
            .where(
                bindings.c.project_id == values["project_id"],
                bindings.c.inflight_id.is_not(None),
            )
            .limit(1)
        )
        if busy and values["mode"] not in {"transferring", "delivery_disabled"}:
            raise AskError("ask.delivery_unknown", "Reconcile outstanding writes before transfer")
        values = {
            **values,
            "generation": expected_generation + 1,
            "migrated": bool((old or {}).get("migrated") or values["mode"] != "legacy"),
        }
        if old:
            return mapped(
                await conn.execute(
                    update(modes)
                    .where(
                        modes.c.project_id == values["project_id"],
                        modes.c.destination == values["destination"],
                    )
                    .values(**values)
                    .returning(modes)
                )
            )
        return mapped(await conn.execute(pg_insert(modes).values(**values).returning(modes)))

    async def get_ask_binding(self, binding_id, *, conn, lock=False):
        stmt = select(bindings).where(bindings.c.id == binding_id)
        return mapped(await conn.execute(stmt.with_for_update() if lock else stmt))

    async def active_ask_binding(self, ask_id, destination, *, conn):
        return mapped(
            await conn.execute(
                select(bindings)
                .where(
                    bindings.c.ask_id == ask_id,
                    bindings.c.destination == destination,
                    bindings.c.active,
                )
                .with_for_update()
            )
        )

    async def insert_ask_binding(self, values, *, conn):
        await conn.execute(pg_insert(bindings).values(**values).on_conflict_do_nothing())
        return await self.active_ask_binding(values["ask_id"], values["destination"], conn=conn)

    async def update_ask_binding(self, binding_id, values, *, conn):
        return mapped(
            await conn.execute(
                update(bindings)
                .where(bindings.c.id == binding_id)
                .values(**values)
                .returning(bindings)
            )
        )

    async def retire_ask_tokens(self, binding_id, *, now, conn):
        await conn.execute(
            update(tokens)
            .where(
                tokens.c.binding_id == binding_id,
                tokens.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    async def get_ask_token(self, token, *, conn):
        return mapped(await conn.execute(select(tokens).where(tokens.c.token == token)))

    async def defer_ask_delivery(self, row, *, now, conn, reason, cancel=False):
        # No external request was started; a pause must not consume an attempt.
        return mapped(
            await conn.execute(
                update(outbox)
                .where(
                    outbox.c.id == row["id"],
                    outbox.c.state == "sending",
                    outbox.c.lease_owner == row["lease_owner"],
                    outbox.c.write_started_at.is_(None),
                )
                .values(
                    state="cancelled" if cancel else "pending",
                    lease_owner=None,
                    lease_expires_at=None,
                    attempt_count=outbox.c.attempt_count - 1,
                    due_at=now + 30,
                    last_error=reason,
                    updated_at=now,
                )
                .returning(outbox)
            )
        )

    async def begin_ask_write(self, row, *, now, attempt_token, conn):
        binding = await self.get_ask_binding(row["binding_id"], conn=conn, lock=True)
        if not binding or binding["inflight_id"] not in (None, row["id"]):
            return None
        current = mapped(
            await conn.execute(
                select(outbox)
                .where(
                    outbox.c.id == row["id"],
                    outbox.c.state == "sending",
                    outbox.c.lease_owner == row["lease_owner"],
                    outbox.c.lease_expires_at > now,
                    outbox.c.write_started_at.is_(None),
                )
                .with_for_update()
            )
        )
        if current is None:
            return None
        await self.update_ask_binding(binding["id"], {"inflight_id": row["id"]}, conn=conn)
        return mapped(
            await conn.execute(
                update(outbox)
                .where(outbox.c.id == row["id"])
                .values(
                    write_started_at=now,
                    attempt_token=attempt_token,
                    updated_at=now,
                )
                .returning(outbox)
            )
        )

    async def complete_ask_write(
        self, row, *, status, now, conn, receipt=None, error=None, retry_at=None
    ):
        """A late confirmed completion may settle its own attempt after lease expiry."""
        binding = await self.get_ask_binding(row["binding_id"], conn=conn, lock=True)
        current = mapped(
            await conn.execute(
                select(outbox)
                .where(
                    outbox.c.id == row["id"],
                    outbox.c.attempt_token == row["attempt_token"],
                    outbox.c.state.in_(["sending", "unknown"]),
                )
                .with_for_update()
            )
        )
        if not current or binding["inflight_id"] != row["id"]:
            return None
        if status == "sent" and not receipt:
            raise ValueError("A confirmed external receipt is required")
        values = dict(
            state=status, lease_owner=None, lease_expires_at=None, updated_at=now, last_error=error
        )
        bvalues = {"updated_at": now}
        if status == "sent":
            values.update(external_receipt_id=receipt, receipt_confirmed_at=now)
            bvalues.update(inflight_id=None)
            if row["send_kind"] in {"root", "root_edit"}:
                bvalues.update(
                    external_root_id=receipt,
                    receipt={"operation_id": row["id"], "message_id": receipt, "confirmed_at": now},
                    root_state="confirmed",
                    applied_display_version=(
                        0 if row["send_kind"] == "root" else row["display_version"]
                    ),
                )
        elif status == "retry":
            values.update(write_started_at=None, attempt_token=None, due_at=retry_at)
            bvalues["inflight_id"] = None
        elif status == "cancelled":
            bvalues["inflight_id"] = None
        elif status == "unknown":
            if row["send_kind"] in {"root", "root_edit"}:
                bvalues["root_state"] = "unknown"
        await self.update_ask_binding(binding["id"], bvalues, conn=conn)
        return mapped(
            await conn.execute(
                update(outbox).where(outbox.c.id == row["id"]).values(**values).returning(outbox)
            )
        )

    async def unresolved_ask_deliveries(self, *, conn, limit=100):
        return [
            dict(r)
            for r in (
                await conn.execute(
                    select(outbox)
                    .where(
                        outbox.c.owner_kind == "ask",
                        or_(outbox.c.state == "unknown", outbox.c.state == "sending"),
                        outbox.c.write_started_at.is_not(None),
                    )
                    .order_by(outbox.c.updated_at, outbox.c.id)
                    .limit(limit)
                )
            ).mappings()
        ]

    async def legacy_human_delivery_blocked(self, project_id, *, channel_id=None):
        async with self._engine.connect() as conn:
            return bool(
                await conn.scalar(
                    select(modes.c.project_id)
                    .where(
                        modes.c.project_id == project_id,
                        modes.c.mode.in_(["transferring", "channel", "delivery_disabled"]),
                    )
                    .limit(1)
                )
            )

    async def legacy_escalation_controls_retired(self, escalation_id):
        from src.database.tables import ask_source_aliases

        async with self._engine.connect() as conn:
            from src.database.tables import human_asks

            return bool(
                await conn.scalar(
                    select(ask_source_aliases.c.ask_id)
                    .join(human_asks, human_asks.c.id == ask_source_aliases.c.ask_id)
                    .join(modes, modes.c.project_id == human_asks.c.project_id)
                    .where(
                        ask_source_aliases.c.legacy_kind == "escalation",
                        ask_source_aliases.c.legacy_id == escalation_id,
                        modes.c.mode.in_(["transferring", "channel", "delivery_disabled"]),
                    )
                    .limit(1)
                )
            )
