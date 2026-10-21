"""Gateway-only opaque decisions; controls never contain a mutable option index."""

import time
from dataclasses import asdict

from src.asks.authority import public_allowed
from src.asks.delivery import config_fingerprint, destination
from src.asks.intake import input_values
from src.asks.models import AskError, DecisionActor, DecisionIntent, SourceIdentity
from src.asks.service import version_of
from src.commands.principal import PrincipalKind, current_principal


def gateway_only():
    principal = current_principal()
    if (
        not principal
        or principal.kind is not PrincipalKind.SERVICE
        or principal.service_name != "discord-gateway"
    ):
        raise AskError("ask.gateway_only", "Buttons require authenticated gateway identity")


async def authorize_button_actor(db, config, source, actor, *, conn, application=False):
    principal = current_principal()
    if not application:
        gateway_only()
    elif principal and principal.kind not in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}:
        raise AskError(
            "ask.not_decider", "Button application requires the trusted decision service"
        )
    item = await db.get_ask_input(actor.input_id, conn=conn)
    ask = await db.get_ask(item["ask_id"], conn=conn) if item else None
    if not (
        item
        and ask
        and item["source"] == "interaction"
        and item["transport"] == "discord-interaction"
        and item["state"] != "revoked"
        and item["retention_entry_id"]
        and actor.principal_id == item["verified_actor"]
        and actor.provenance.get("source") == asdict(source)
        and actor.provenance.get("owner") == ask["logical_owner"]
        and (ask["project_id"], ask["source_kind"], ask["source_key"])
        == (source.project_id, source.kind, source.key)
        and public_allowed(ask, config, actor_id=actor.principal_id)
    ):
        raise AskError("ask.not_decider", "Button evidence or audience is no longer valid")


async def decide(handler, args):
    gateway_only()
    db, service, config, now = handler.db, handler._ask_service(), handler.config, time.time()
    settings = config.discord
    if (
        args["author_id"] not in {str(u) for u in settings.authorized_users}
        or args["guild_id"] != str(settings.guild_id)
        or args["channel_id"] != str(settings.channel_id)
        or not args["interaction_id"]
        or not args["custom_id"].startswith("aqask:")
    ):
        raise AskError("ask.not_authorized", "Button actor or destination is invalid")
    async with db.immediate() as conn:
        token = await db.get_ask_token(args["custom_id"][6:], conn=conn)
        ask = await db.get_ask(token["ask_id"], conn=conn) if token else None
    if not ask or not public_allowed(ask, config, actor_id=f"human:discord:{args['author_id']}"):
        raise AskError("ask.not_found", "This control is unavailable")
    source = SourceIdentity(
        ask["project_id"], ask["source_kind"], ask["source_key"], ask["source_identity"]
    )
    async with handler._ask_transaction(source, service) as conn:
        # Source first, ask second, binding and token last. Always reload the source.
        ask = (await service.publish(source, legacy=True, conn=conn)).ask
        mode = await db.get_ask_mode(ask["project_id"], destination(config), conn=conn, lock=True)
        binding = await db.get_ask_binding(token["binding_id"], conn=conn, lock=True)
        token = await db.get_ask_token(token["token"], conn=conn)
        if not (
            binding
            and binding["active"]
            and mode
            and mode["mode"] == "channel"
            and mode["generation"] == token["mode_generation"] == binding["mode_generation"]
            and mode["config_fingerprint"] == config_fingerprint(config)
            and binding["guild_id"] == args["guild_id"]
            and binding["channel_id"] == args["channel_id"]
            and binding["external_root_id"] == token["root_message_id"] == args["message_id"]
            and binding["root_generation"] == token["root_generation"]
            and token["audience"] == ask["audience"]
        ):
            raise AskError(
                "ask.stale_button",
                "This root was replaced or transferred; open the current request",
            )
        # Even a concurrent different choice returns the winner, never a second effect.
        winner = await db.get_ask_decision_by_generation(
            ask["id"], token["decision_generation"], conn=conn
        )
        if winner and token["decision_generation"] == ask["decision_generation"]:
            return {
                "success": True,
                "created": False,
                "input_id": winner["evidence_entry_id"],
                "receipt": {
                    "decision_id": winner["id"],
                    "option_key": winner["option_key"],
                    "application_state": ask["application_state"],
                },
            }
        current = (
            token["decision_generation"],
            token["source_fingerprint"],
            token["feedback_version"],
        )
        expected = version_of(ask)
        if (
            token["revoked_at"] is not None
            or token["expires_at"] <= now
            or current
            != (
                expected.decision_generation,
                expected.source_fingerprint,
                expected.feedback_version,
            )
            or token["display_version"] != ask["display_version"]
        ):
            # Mark the root for fresh controls; keep the failure inside the transaction so it commits.
            await db.update_ask_projection(
                ask["id"], {"display_version": ask["display_version"] + 1}, conn=conn
            )
            return {
                "success": False,
                "error_code": "ask.stale_button",
                "error": "This choice expired or changed. The card will refresh; nothing was applied.",
            }
        choice = next(
            (c for c in ask["context"]["choices"] if c["key"] == token["option_key"]), None
        )
        if (
            ask["source_kind"] in {"review", "standalone_spec"}
            or not choice
            or choice["application"] != "immediate"
        ):
            raise AskError("ask.packet_required", "Read and discuss the complete decision packet")
        values = input_values(
            transport="discord-interaction",
            external_id=args["interaction_id"],
            actor=f"human:discord:{args['author_id']}",
            author_id=args["author_id"],
            channel_id=args["channel_id"],
            guild_id=args["guild_id"],
            text=choice["label"],
            source="interaction",
            project_id=ask["project_id"],
            reference_message_id=args["message_id"],
        )
        values.update(
            ask_id=ask["id"],
            source_version={"expected": asdict(expected)},
            routing_evidence={"token": token["token"], "option_key": choice["key"]},
        )
        item, created, matches = await db.admit_ask_input(values, conn=conn)
        if (
            not matches
            or item["ask_id"] != ask["id"]
            or item["routing_evidence"] != values["routing_evidence"]
        ):
            raise AskError("ask.input_conflict", "Interaction already owns different evidence")
        evidence_id = f"ask-button:{item['id']}"
        await db.insert_ask_correspondence(
            {
                "id": evidence_id,
                "ask_id": ask["id"],
                "input_id": item["id"],
                "direction": "inbound",
                "transport": "discord-interaction",
                "external_message_id": args["interaction_id"],
                "verified_actor": values["verified_actor"],
                "text": choice["label"],
                "entry_kind": "decision_evidence",
                "received_at": now,
                "created_at": now,
            },
            conn=conn,
        )
        await db.update_ask_input(item["id"], {"retention_entry_id": evidence_id}, conn=conn)
        actor = DecisionActor(
            values["verified_actor"],
            {"kind": "verified_button", "source": asdict(source), "owner": ask["logical_owner"]},
            item["id"],
        )
        intent = DecisionIntent(choice["key"], expected)
        preview = await service.prepare_decision(source, actor, intent, conn=conn)
        decision, created = await service.decide(
            source,
            actor,
            intent,
            conn=conn,
            preview_id=preview["id"],
            idempotency_key=f"discord-interaction:{args['interaction_id']}",
            evidence_entry_id=evidence_id,
        )
        receipt = {"decision_id": decision["id"], "option_key": decision["option_key"]}
        await db.update_ask_input(
            item["id"], {"routing_state": "applied", "receipt": receipt}, conn=conn
        )
    result = await handler._apply_ask_decision(decision)
    if created:
        await handler._publish_source_application(decision, result)
    return {
        "success": result["application_state"] == "applied",
        "created": created,
        "input_id": item["id"],
        "receipt": {**receipt, **result},
    }
