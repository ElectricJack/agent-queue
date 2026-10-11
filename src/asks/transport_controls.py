"""Operator mode/repair ports. Live inventory verification belongs to rollout."""

from sqlalchemy import select

from src.asks.delivery import config_fingerprint, destination
from src.asks.models import AskError
from src.database.tables import (
    ask_external_bindings,
    escalation_deliveries,
    escalations,
    human_communication_modes,
    outbound_deliveries,
)
from src.escalations.transport import TransportMissing


async def cutover(handler, args):
    db, config = handler.db, handler.config
    import time

    async with db.immediate() as conn:
        old = await db.get_ask_mode(args["project_id"], destination(config), conn=conn)
        if args["mode"] == "channel" and not (
            old and old["mode"] == "transferring" and old["inventory"].get("transfer_verified")
        ):
            raise AskError(
                "ask.transfer_unverified",
                "Verify the receipt inventory before channel ownership transfer",
            )
        busy = await conn.scalar(
            select(escalation_deliveries.c.id)
            .join(escalations, escalations.c.id == escalation_deliveries.c.escalation_id)
            .where(
                escalations.c.project_id == args["project_id"],
                escalation_deliveries.c.status.in_(["sending", "unknown"]),
            )
            .limit(1)
        )
        if busy and args["mode"] in {"transferring", "channel"}:
            raise AskError("ask.delivery_unknown", "Legacy delivery still has unresolved ownership")
        # A global config change fences every affected durable project, never just this call's row.
        peers = (
            (
                await conn.execute(
                    select(human_communication_modes)
                    .where(
                        human_communication_modes.c.config_fingerprint
                        != config_fingerprint(config),
                    )
                    .order_by(
                        human_communication_modes.c.project_id,
                        human_communication_modes.c.destination,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        for peer in peers:
            if (peer["project_id"], peer["destination"]) == (
                args["project_id"],
                destination(config),
            ):
                continue
            if await conn.scalar(
                select(ask_external_bindings.c.id)
                .where(
                    ask_external_bindings.c.project_id == peer["project_id"],
                    ask_external_bindings.c.inflight_id.is_not(None),
                )
                .limit(1)
            ):
                raise AskError(
                    "ask.delivery_unknown", "Global destination change has unresolved writes"
                )
            await db.advance_ask_mode(
                {
                    **dict(peer),
                    "mode": "delivery_disabled",
                    "config_fingerprint": config_fingerprint(config),
                    "updated_at": time.time(),
                },
                expected_generation=peer["generation"],
                conn=conn,
            )
        mode = await db.advance_ask_mode(
            {
                "project_id": args["project_id"],
                "destination": destination(config),
                "mode": args["mode"],
                "config_fingerprint": config_fingerprint(config),
                "updated_at": time.time(),
            },
            expected_generation=args["expected_generation"],
            conn=conn,
        )
    return {"success": True, "result": mode}


async def repair(handler, args):
    adapter = getattr(handler, "ask_delivery", None)
    if adapter is None:
        raise AskError("ask.transport_unavailable", "Delivery transport is unavailable")
    row = await handler.db.get_outbound_delivery(args["delivery_id"])
    if not row or row["owner_kind"] != "ask":
        raise AskError("ask.not_found", "Delivery not found")
    async with handler.db._engine.connect() as conn:
        binding = await handler.db.get_ask_binding(row["binding_id"], conn=conn)
    if binding["root_generation"] != args["expected_generation"]:
        raise AskError("ask.stale_binding", "Root generation changed; inspect the current receipt")
    if args["disposition"] == "confirmed_sent":
        await adapter.recover(row)
        latest = await handler.db.get_outbound_delivery(row["id"])
        return {"success": latest["state"] == "sent", "result": latest}
    if args["disposition"] == "confirmed_absent":
        if not binding["external_root_id"]:
            raise AskError(
                "ask.delivery_unknown",
                "A missing history marker does not prove absence; acknowledge irreconcilability explicitly",
            )
        try:
            await adapter.transport.read_root(
                channel_id=binding["channel_id"], root_message_id=binding["external_root_id"]
            )
        except TransportMissing:
            pass
        else:
            raise AskError("ask.root_exists", "The root still exists")
    # cancel is the explicit operator acknowledgement that an unknown outcome is irreconcilable.
    async with handler.db.immediate() as conn:
        ask = await adapter.current(row["ask_id"], conn=conn)
        binding = await handler.db.get_ask_binding(row["binding_id"], conn=conn, lock=True)
        if binding["root_generation"] != args["expected_generation"]:
            raise AskError("ask.stale_binding", "Root generation changed")
        if binding["inflight_id"] not in (None, row["id"]):
            raise AskError("ask.delivery_unknown", "A different write owns this root")
        current = (
            (
                await conn.execute(
                    select(outbound_deliveries)
                    .where(outbound_deliveries.c.id == row["id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if current["state"] not in {"unknown", "sending"}:
            raise AskError("ask.repair_state", "Repair only an unresolved delivery")
        case = await adapter.repair_case(ask, binding, args["evidence"], conn=conn, row=row)
        await handler.db.resolve_ask_reconciliation_case(
            case["id"],
            {"disposition": args["disposition"], "evidence": args["evidence"]},
            now=adapter.clock(),
            conn=conn,
        )
        if row["send_kind"] in {"root", "root_edit"}:
            await adapter.replace_deleted(row, reason=args["evidence"], conn=conn)
        else:
            await handler.db.complete_ask_write(
                row, status="cancelled", now=adapter.clock(), error=args["evidence"], conn=conn
            )
    await adapter.enqueue(row["ask_id"])
    return {
        "success": True,
        "result": {
            "replaced_generation": args["expected_generation"]
            if row["send_kind"] in {"root", "root_edit"}
            else None
        },
    }
