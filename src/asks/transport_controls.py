"""Operator mode/repair ports. Live inventory verification belongs to rollout."""

from datetime import UTC, datetime

from sqlalchemy import select

from src.asks.delivery import config_fingerprint, destination
from src.asks.models import AskError
from src.database.tables import (
    ask_external_bindings,
    human_communication_modes,
    outbound_deliveries,
)
from src.escalations.transport import TransportMissing


async def cutover_status(handler, args):
    project = args["project_id"]
    configured = destination(handler.config)
    async with handler.db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        human_communication_modes.c.destination,
                        human_communication_modes.c.mode,
                        human_communication_modes.c.generation,
                        human_communication_modes.c.migrated,
                        human_communication_modes.c.compatibility_deadline,
                    ).where(
                        human_communication_modes.c.project_id == project
                    )
                )
            )
            .mappings()
            .all()
        )
    modes = {row["destination"]: row for row in rows}
    modes.setdefault(
        configured,
        {
            "mode": "legacy",
            "generation": 0,
            "migrated": False,
            "compatibility_deadline": None,
        },
    )
    destinations = []
    for dest, row in sorted(modes.items()):
        deadline = row["compatibility_deadline"]
        destinations.append(
            {
                "destination": dest,
                "mode": row["mode"],
                "generation": row["generation"],
                "migrated": row["migrated"],
                # The persisted first-channel deadline is never extended by rollback.
                "first_switch_utc": datetime.fromtimestamp(deadline - 30 * 86400, UTC).isoformat()
                if deadline is not None
                else None,
                "compatibility_deadline": deadline,
                "compatibility_deadline_utc": datetime.fromtimestamp(deadline, UTC).isoformat()
                if deadline is not None
                else None,
            }
        )
    return {
        "success": True,
        "result": {
            "project_id": project,
            "configured_destination": configured,
            "destinations": destinations,
        },
    }


async def cutover(handler, args):
    import time

    from sqlalchemy import func, update

    from src.asks.models import digest
    from src.asks.reconcile import diagnostics
    from src.commands.principal import TRUSTED_LOCAL, current_principal
    from src.database.tables import ask_action_tokens

    db, config = handler.db, handler.config
    project, dest, target = args["project_id"], destination(handler.config), args["mode"]
    async with db.immediate() as conn:
        # The admission fence commits even with unknown writes. Those writes remain
        # evidence, and block channel ownership until independently reconciled.
        await conn.execute(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended(f"ask-mode:{project}:{dest}", 0))
            )
        )
        old = await db.get_ask_mode(project, dest, conn=conn, lock=True)
        if old and old["migrated"] and target == "legacy":
            raise AskError("ask.legacy_retired", "Migrated writers cannot be revived")
        if target == "channel" and (not old or old["mode"] != "transferring"):
            raise AskError("ask.transfer_unverified", "Fence both writers before channel transfer")
        if (old["generation"] if old else 0) != args["expected_generation"]:
            raise AskError("ask.stale_mode", "Communication ownership changed")
        allowed = {
            "legacy": {"shadow", "delivery_disabled"},
            "shadow": {"dashboard", "delivery_disabled"},
            "dashboard": {"transferring", "delivery_disabled"},
            "transferring": {"transferring", "channel", "delivery_disabled"},
            "channel": {"transferring", "delivery_disabled"},
            "delivery_disabled": {"transferring", "delivery_disabled"},
        }
        if target not in allowed[(old or {}).get("mode", "legacy")]:
            raise AskError(
                "ask.invalid_mode_transition", "Follow shadow, dashboard, transfer, channel"
            )
        inventory = await diagnostics(conn, project_id=project, destination_id=dest)
        if target == "channel":
            blockers = {k: v for k, v in inventory.items() if k != "transfers"}
            if blockers:
                raise AskError(
                    "ask.transfer_unverified", "Reconcile transfer blockers first", fields=blockers
                )
            if not (
                args.get("inventory_sha256") == digest(inventory)
                and (args.get("evidence") or "").strip()
                and args.get("legacy_controls_disabled")
                and args.get("aliases_redirected")
            ):
                raise AskError(
                    "ask.transfer_unverified",
                    "Record the current inventory hash and controls/redirect evidence",
                )
        # Global destination/allowlist changes revoke every old ownership row.
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
                )
            )
            .mappings()
            .all()
        )
        for peer in peers:
            if (peer["project_id"], peer["destination"]) == (project, dest):
                continue
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
            await conn.execute(
                update(ask_action_tokens)
                .where(
                    ask_action_tokens.c.binding_id.in_(
                        select(ask_external_bindings.c.id).where(
                            ask_external_bindings.c.project_id == peer["project_id"],
                            ask_external_bindings.c.destination == peer["destination"],
                        )
                    ),
                    ask_action_tokens.c.revoked_at.is_(None),
                )
                .values(revoked_at=time.time())
            )
        recorded = dict((old or {}).get("inventory") or {})
        recorded.update(
            last_inventory=inventory,
            inventory_sha256=digest(inventory),
            recorded_at=time.time(),
            principal=(current_principal() or TRUSTED_LOCAL).describe(),
        )
        if target == "channel":
            recorded["transfer_receipt"] = {
                "evidence": args["evidence"],
                "legacy_controls_disabled": True,
                "aliases_redirected": True,
                "generation": args["expected_generation"],
                "inventory_sha256": digest(inventory),
            }
        mode = await db.advance_ask_mode(
            {
                "project_id": project,
                "destination": dest,
                "mode": target,
                "config_fingerprint": config_fingerprint(config),
                "updated_at": time.time(),
                "inventory": recorded,
                # Rollback/re-enable never extends the first destination's 30-day window.
                "compatibility_deadline": (old or {}).get("compatibility_deadline")
                or (time.time() + 30 * 86400 if target == "channel" else None),
            },
            expected_generation=args["expected_generation"],
            conn=conn,
        )
        if target in {"transferring", "delivery_disabled"}:
            await conn.execute(
                update(ask_action_tokens)
                .where(
                    ask_action_tokens.c.binding_id.in_(
                        select(ask_external_bindings.c.id).where(
                            ask_external_bindings.c.project_id == project,
                            ask_external_bindings.c.destination == dest,
                        )
                    ),
                    ask_action_tokens.c.revoked_at.is_(None),
                )
                .values(revoked_at=time.time())
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
