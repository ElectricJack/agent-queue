"""Authenticate once, persist once, then correlate without transferring ownership.

Only gateway code constructs the external envelope. Interpretation is appended
to verified evidence and never changes the executing principal into a human.
"""

import re
import time
from dataclasses import asdict

from sqlalchemy import or_, select

from src.asks import discussion
from src.asks.authority import addressed_supervisor, public_allowed, visible
from src.asks.models import (
    AskError,
    DecisionActor,
    DecisionIntent,
    SourceIdentity,
    SourceVersion,
    digest,
)
from src.asks.service import new_id, version_of
from src.commands.principal import PrincipalKind, current_principal
from src.database.queries.ask_input_queries import text_hash
from src.database.tables import (
    ask_external_bindings,
    ask_source_aliases,
    escalation_deliveries,
    escalation_messages,
    escalations,
    outbound_deliveries,
)

ASK_REFERENCE = re.compile(r"\bask-[A-Za-z0-9_-]+\b")
CHOICES = {
    "yes": "approve",
    "approve": "approve",
    "approved": "approve",
    "no": "decline",
    "decline": "decline",
    "reject": "decline",
}


def clear_intent(text):
    """A deliberately narrow grammar; model confidence never supplies authority."""
    value = ASK_REFERENCE.sub("", text).strip(" .!:\n\t").casefold()
    if value in CHOICES:
        return CHOICES[value], None
    condition = re.fullmatch(
        r"(?:yes|approve|approved)[, ]+(?:if|provided that|but)\s+(.+)", value, flags=re.DOTALL
    )
    if condition and not re.search(r"\b(?:decline|reject|approve)\b", condition[1]):
        return "fold_in", text
    return None, None


def input_values(
    *,
    transport,
    external_id,
    actor,
    author_id,
    channel_id,
    text,
    source,
    project_id=None,
    guild_id=None,
    reference_message_id=None,
    external_thread_id=None,
    received_at=None,
):
    now = time.time()
    return {
        "id": new_id("cinput"),
        "transport": transport,
        "external_message_id": external_id,
        "verified_actor": actor,
        "author_id": author_id,
        "channel_id": channel_id,
        "text": text,
        "text_sha256": text_hash(text),
        "char_count": len(text),
        "source": source,
        "project_id": project_id,
        "guild_id": guild_id,
        "reference_message_id": reference_message_id,
        "external_thread_id": external_thread_id,
        "received_at": received_at if received_at is not None else now,
        "created_at": now,
        "routing_state": "queued",
    }


async def admit_gateway(handler, envelope, *, source):
    principal = current_principal()
    if not (
        principal
        and principal.kind is PrincipalKind.SERVICE
        and principal.service_name == "discord-gateway"
    ):
        raise AskError(
            "ask.gateway_only", "Human intake is restricted to the authenticated gateway"
        )
    settings = handler.config.discord
    if (
        envelope.guild_id != str(settings.guild_id)
        or envelope.channel_id != str(settings.channel_id)
        or envelope.author_id not in {str(u) for u in settings.authorized_users}
    ):
        raise AskError("ask.not_authorized", "Human input destination or allowlist is invalid")
    if not envelope.text.strip() or len(envelope.text) > 4000:
        raise AskError("ask.invalid_input", "Live human input must contain 1–4000 characters")
    if not await handler.db.ask_channel_enabled(
        channel_id=envelope.channel_id, guild_id=envelope.guild_id
    ):
        raise AskError("ask.channel_disabled", "Shared intake ownership has not transferred")
    values = input_values(
        transport="discord",
        external_id=envelope.external_message_id,
        actor=f"human:discord:{envelope.author_id}",
        author_id=envelope.author_id,
        channel_id=envelope.channel_id,
        guild_id=envelope.guild_id,
        text=envelope.text,
        source=source,
        project_id=settings.project_id or None,
        reference_message_id=envelope.reference_message_id,
        external_thread_id=envelope.external_thread_id,
        received_at=envelope.received_at,
    )
    async with handler.db.immediate() as conn:
        item, created, matches = await handler.db.admit_ask_input(values, conn=conn)
    if not matches:
        return {
            "success": False,
            "input_id": item["id"],
            "error_code": "ask.input_conflict",
            "error": "Input identity has conflicting evidence; repair is required",
        }
    # Replay returns the original receipt, including queued ownership. The
    # reconcile command resumes a failed consumer without admitting it again.
    if created:
        try:
            item = await route(handler, item)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Input %s persisted; correlation remains queued", item["id"]
            )
            return {
                "success": True,
                "created": True,
                "input_id": item["id"],
                "receipt": {"input_id": item["id"], "state": "queued"},
            }
    return {
        "success": True,
        "created": created,
        "input_id": item["id"],
        "receipt": item["receipt"] or {"input_id": item["id"], "state": "queued"},
    }


async def bound_ask(db, item, *, conn):
    reference = item["reference_message_id"]
    roots = (
        (
            await conn.execute(
                select(ask_external_bindings.c.ask_id)
                .where(
                    ask_external_bindings.c.channel_id == item["channel_id"],
                    or_(
                        ask_external_bindings.c.external_root_id == reference
                        if reference
                        else False,
                        ask_external_bindings.c.legacy_thread_id == item["external_thread_id"]
                        if item["external_thread_id"]
                        else False,
                    ),
                )
                .distinct()
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if len(roots) == 1:
        return roots[0]
    if len(roots) > 1:
        raise AskError("ask.input_ambiguous", "Historical root has conflicting request bindings")
    if reference:
        entry = (
            await conn.execute(
                select(escalation_messages.c.ask_id).where(
                    escalation_messages.c.transport == item["transport"],
                    escalation_messages.c.external_message_id == reference,
                )
            )
        ).scalar_one_or_none()
        if entry:
            return entry
        deliveries = (
            (
                await conn.execute(
                    select(outbound_deliveries.c.payload).where(
                        outbound_deliveries.c.external_receipt_id == reference,
                        outbound_deliveries.c.destination["channel_id"].astext
                        == item["channel_id"],
                    )
                )
            )
            .scalars()
            .all()
        )
        for payload in deliveries:
            if payload.get("ask_id"):
                return payload["ask_id"]
    legacy = await conn.scalar(
        select(ask_source_aliases.c.ask_id)
        .join(
            escalation_deliveries,
            escalation_deliveries.c.escalation_id == ask_source_aliases.c.legacy_id,
        )
        .where(
            ask_source_aliases.c.legacy_kind == "escalation",
            escalation_deliveries.c.channel_id == item["channel_id"],
            or_(
                escalation_deliveries.c.root_message_id == reference if reference else False,
                escalation_deliveries.c.thread_id == item["external_thread_id"]
                if item["external_thread_id"]
                else False,
            ),
        )
        .limit(1)
    )
    return legacy


async def clarify(handler, item, text, *, conn):
    item = await discussion.attach_channel(handler, item, conn=conn)
    await discussion.enqueue(handler, item, None, text, key=f"ask-clarify:{item['id']}", conn=conn)
    return await handler.db.update_ask_input(
        item["id"],
        {
            "routing_state": "clarification",
            "receipt": {"input_id": item["id"], "state": "clarification", "text": text},
        },
        conn=conn,
    )


async def route(handler, item):
    db = handler.db
    # Resolve identity without locking the input; source is always locked first.
    async with db.immediate() as conn:
        bound = await bound_ask(db, item, conn=conn)
        explicit = set(ASK_REFERENCE.findall(item["text"] or ""))
        selected = bound or (next(iter(explicit)) if len(explicit) == 1 else None)
        candidate_page = await db.ask_candidates(item["project_id"], conn=conn)
        candidates = [
            a
            for a in candidate_page
            if visible(a, actor_id=item["verified_actor"])
            and public_allowed(a, handler.config, actor_id=item["verified_actor"])
        ]
        option, _ = clear_intent(item["text"] or "")
        if (
            not selected
            and not explicit
            and option
            and len(candidates) == 1
            and len(candidate_page) < 21
        ):
            selected = candidates[0]["id"]
        ask = await db.get_ask(selected, conn=conn) if selected else None
        if ask and (
            not visible(ask, actor_id=item["verified_actor"])
            or not public_allowed(ask, handler.config, actor_id=item["verified_actor"])
        ):
            ask = None
        if ask:
            # The gateway has authenticated the human; do not impersonate local.
            source = SourceIdentity(
                ask["project_id"], ask["source_kind"], ask["source_key"], ask["source_identity"]
            )
            service = handler._ask_service()
            snap = await service.adapters.get(source.kind).snapshot(source, conn=conn)
            ask = (await service.publish_snapshot(snap, conn=conn, legacy=True)).ask
        current = await db.get_ask_input(item["id"], conn=conn, lock=True)
        if current["routing_state"] != "queued":
            return current
        item = current
        if len(explicit) > 1 or (bound and explicit and explicit != {bound}):
            return await clarify(
                handler, item, "Please name one request and one intended action.", conn=conn
            )
        if selected and not ask:
            return await clarify(
                handler, item, "Open this request in the dashboard to continue.", conn=conn
            )
        if ask:
            if item["transport"] == "discord":
                from src.asks.delivery import destination

                mode = await db.get_ask_mode(
                    ask["project_id"], destination(handler.config), conn=conn
                )
                if not mode or mode["mode"] != "channel":
                    return await clarify(
                        handler,
                        item,
                        "External decisions are paused for this project; use its dashboard.",
                        conn=conn,
                    )
                if item["external_thread_id"]:
                    binding = await db.active_ask_binding(ask["id"], mode["destination"], conn=conn)
                    if (
                        not binding
                        or binding["legacy_thread_id"] != item["external_thread_id"]
                        or not mode["compatibility_deadline"]
                        or time.time() >= mode["compatibility_deadline"]
                    ):
                        return await clarify(
                            handler,
                            item,
                            "This legacy thread is read-only. Reply to the main-channel card or use the dashboard.",
                            conn=conn,
                        )
            if ask["decision_state"] != "pending":
                item = await discussion.attach_channel(handler, item, conn=conn)
                receipt = {
                    "input_id": item["id"],
                    "ask_id": ask["id"],
                    "state": "closed",
                    "decision_state": ask["decision_state"],
                    "application_state": ask["application_state"],
                }
                await discussion.enqueue(
                    handler,
                    item,
                    ask,
                    f"This request is {ask['decision_state']}; application is {ask['application_state']}.",
                    key=f"ask-closed:{item['id']}",
                    conn=conn,
                )
                return await db.update_ask_input(
                    item["id"],
                    {
                        "ask_id": ask["id"],
                        "routing_state": "receipt",
                        "receipt": receipt,
                    },
                    conn=conn,
                )
            option, _ = clear_intent(item["text"])
            if not option and re.search(r"\b(yes|no|approve|decline|reject)\b", item["text"], re.I):
                return await clarify(
                    handler,
                    item,
                    "Please confirm one action or review a prepared decision summary.",
                    conn=conn,
                )
            if item["transport"] == "discord" and item["external_thread_id"]:
                item = await discussion.attach_channel(handler, item, conn=conn)
                receipt = dict(binding["receipt"] or {})
                if not receipt.get("move_notice_delivery_id"):
                    adapter = getattr(handler, "ask_delivery", None)
                    text = "Discussion has moved here. Reply to the main-channel card; old links remain readable."
                    if adapter:
                        notice = await adapter.reserve(
                            ask,
                            binding,
                            "move_notice",
                            {
                                "text": text,
                                "input_id": item["id"],
                                "entry_id": f"move:{binding['id']}",
                            },
                            key=f"ask-move:{binding['id']}",
                            conn=conn,
                        )
                        notice_id = notice["id"]
                    else:
                        notice_id = await discussion.enqueue(
                            handler, item, ask, text, key=f"ask-move:{binding['id']}", conn=conn
                        )
                    if notice_id:
                        receipt["move_notice_delivery_id"] = notice_id
                        await db.update_ask_binding(binding["id"], {"receipt": receipt}, conn=conn)
            return await discussion.queue_discussion(
                handler, ask, item, conn=conn, interpretation=bool(option)
            )
        if option and (len(candidates) > 1 or len(candidate_page) >= 21):
            return await clarify(
                handler,
                item,
                "Which request do you mean? Reply to its card or name its ask ID.",
                conn=conn,
            )
        # Unbound prose goes to the owning supervisor with eligible references,
        # but must not become a decision on a guessed target.
        item = await discussion.attach_channel(handler, item, conn=conn)
        project = item["project_id"]
        recipient, supervisor_project = await db.resolve_conversation_supervisor(project)
        packet = {
            "input_id": item["id"],
            "conversation_id": item["conversation_id"],
            "text": item["text"],
            "eligible_asks": [
                {"ask_id": a["id"], "expected": asdict(version_of(a))} for a in candidates[:20]
            ],
            "label": "UNTRUSTED HUMAN CONTEXT. Ask for an explicit target before deciding.",
        }
        import json

        message_id = f"msg-{item['id']}"
        await db.insert_ask_message(
            {
                "id": message_id,
                "project_id": supervisor_project,
                "from_kind": "user",
                "from_id": item["verified_actor"],
                "to_kind": "session",
                "to_id": recipient or "conversation-queued",
                "thread_id": f"conversation:{item['conversation_id']}",
                "body": "Answer this ordinary conversation with supervisor_inbox_reply.\n"
                + json.dumps(packet),
                "body_kind": "conversation_input",
                "created_at": time.time(),
            },
            conn=conn,
        )
        return await db.update_ask_input(
            item["id"],
            {
                "routing_state": "conversation",
                "supervisor_message_id": message_id,
                "routing_evidence": {k: v for k, v in packet.items() if k != "text"},
                "receipt": {
                    "input_id": item["id"],
                    "state": "queued",
                    "conversation_id": item["conversation_id"],
                },
            },
            conn=conn,
        )


async def apply_input(handler, args):
    db, service = handler.db, handler._ask_service()
    replay = None
    async with db.immediate() as conn:
        _, source = await handler._ask_source_for_caller(args["ask_id"], conn=conn)
        prior = await db.get_ask_input(args["input_id"], conn=conn)
        if prior and prior["ask_id"] == args["ask_id"] and prior["routing_state"] == "applied":
            await addressed_supervisor(prior, conn=conn)
            replay = await db.get_ask_decision(prior["receipt"]["decision_id"], conn=conn)
    if replay:
        result = await handler._apply_ask_decision(replay)
        return {
            "success": result["application_state"] == "applied",
            "created": False,
            "input_id": prior["id"],
            "receipt": prior["receipt"],
            **result,
        }
    async with handler._ask_transaction(source, service) as conn:
        ask = await discussion.locked_ask(handler, args["ask_id"], conn=conn)
        item = await db.get_ask_input(args["input_id"], conn=conn, lock=True)
        if not item or item["ask_id"] != ask["id"] or not item["source_version"]:
            raise AskError("ask.unbound_input", "Clarify the target before applying this input")
        await addressed_supervisor(item, conn=conn)
        if item["routing_state"] == "applied":
            return {
                "success": ask["application_state"] == "applied",
                "created": False,
                "input_id": item["id"],
                "receipt": item["receipt"],
                "decision_id": item["receipt"]["decision_id"],
                "application_state": ask["application_state"],
            }
        if item["routing_state"] != "interpretation":
            raise AskError("ask.discussion_only", "Discussion cannot authorize a decision")
        expected = SourceVersion(**args["expected"])
        source = SourceIdentity(
            ask["project_id"], ask["source_kind"], ask["source_key"], ask["source_identity"]
        )
        actor = DecisionActor(
            item["verified_actor"],
            {
                "kind": "verified_input",
                "source": asdict(source),
                "owner": ask["logical_owner"],
                "interpreted_by": current_principal().session_id,
            },
            item["id"],
        )
        from src.asks.authority import authorize_input_actor

        await authorize_input_actor(db, handler.config, source, actor, conn=conn)
        await db.insert_ask_correspondence(
            {
                "id": "ask-interpretation-" + digest([item["id"], args["idempotency_key"]]),
                "ask_id": ask["id"],
                "input_id": item["id"],
                "source_version": asdict(expected),
                "entry_kind": "interpretation",
                "direction": "inbound",
                "transport": "internal",
                "verified_actor": f"session:{current_principal().session_id}",
                "text": args["interpretation"],
                "received_at": time.time(),
                "created_at": time.time(),
            },
            conn=conn,
        )
        if ask["decision_state"] != "pending" or item["source_version"]["expected"] != asdict(
            expected
        ):
            return {
                "success": False,
                "error_code": "ask.stale_source",
                "error": "Read the current receipt",
                "ask_id": ask["id"],
            }
        if asdict(version_of(ask)) != asdict(expected):
            return {
                "success": False,
                "error_code": "ask.stale_source",
                "error": "Input references an older source; read the current receipt",
            }
        option, condition = clear_intent(item["text"] or "")
        if option != args["option_key"] or not option:
            await clarify(
                handler,
                item,
                "Please confirm one action or review a prepared decision summary.",
                conn=conn,
            )
            return {
                "success": False,
                "error_code": "ask.clarification_required",
                "error": "Stored words do not authorize this action",
            }
        if option == "fold_in" and ask["source_kind"] != "review":
            raise AskError(
                "ask.clarification_required", "Conditional decisions require a prepared summary"
            )
        evidence_id = f"ask-evidence-{item['id']}"
        await db.insert_ask_correspondence(
            {
                "id": evidence_id,
                "ask_id": ask["id"],
                "input_id": item["id"],
                "source_version": item["source_version"],
                "entry_kind": "decision_evidence",
                "direction": "inbound",
                "transport": item["transport"],
                "verified_actor": item["verified_actor"],
                "text": item["text"],
                "received_at": item["received_at"],
                "created_at": time.time(),
            },
            conn=conn,
        )
        if condition:
            await db.insert_review_comment(
                {
                    "id": f"cmt-{item['id']}",
                    "review_id": ask["source_key"],
                    "revision": item["source_version"]["revision"],
                    "heading_path": [],
                    "body": item["text"],
                    "author": item["verified_actor"],
                    "source_discussion_id": evidence_id,
                    "created_at": time.time(),
                },
                conn=conn,
            )
            snap = await service.adapters.get(source.kind).snapshot(source, conn=conn)
            ask = (await service.publish_snapshot(snap, conn=conn)).ask
            expected = version_of(ask)
        intent = DecisionIntent(option, expected)
        preview = await service.prepare_decision(source, actor, intent, conn=conn)
        decision, created = await service.decide(
            source,
            actor,
            intent,
            conn=conn,
            preview_id=preview["id"],
            idempotency_key=f"input:{item['id']}",
            evidence_entry_id=evidence_id,
        )
        receipt = {
            "input_id": item["id"],
            "decision_id": decision["id"],
            "state": "accepted",
            "saved_comment_id": f"cmt-{item['id']}" if condition else None,
        }
        await db.update_ask_input(
            item["id"],
            {"routing_state": "applied", "receipt": receipt, "retention_entry_id": evidence_id},
            conn=conn,
        )
    result = await handler._apply_ask_decision(decision)
    async with db.immediate() as conn:
        item = await db.get_ask_input(item["id"], conn=conn)
        ask = await db.get_ask(item["ask_id"], conn=conn)
        summary = (
            f"Saved formal feedback {receipt['saved_comment_id']}; fold-in "
            if receipt["saved_comment_id"]
            else f"Saved {decision['option_key']}; "
        )
        await discussion.enqueue(
            handler,
            item,
            ask,
            summary + f"application is {result['application_state']}.",
            key=f"ask-outcome:{item['id']}",
            conn=conn,
        )
    await handler._publish_source_application(decision, result)
    return {
        "success": result["application_state"] == "applied",
        "created": created,
        "input_id": item["id"],
        "receipt": receipt,
        **result,
    }


async def backfill_legacy(handler, *, conn, limit=100, project_id=None):
    from src.asks.reset_policy import before_reset
    """Bounded resumable import; conflicts remain owned repair cases."""
    rows = (
        (
            await conn.execute(
                select(
                    escalation_messages,
                    escalations.c.project_id,
                )
                .join(escalations, escalations.c.id == escalation_messages.c.escalation_id)
                .where(
                    escalation_messages.c.direction == "inbound",
                    escalation_messages.c.input_id.is_(None),
                    escalation_messages.c.external_message_id.is_not(None),
                    escalations.c.project_id == project_id if project_id else True,
                    ~before_reset(escalation_messages, escalations.c.project_id),
                    escalations.c.terminal_outcome.is_distinct_from("legacy_discarded"),
                )
                .order_by(escalation_messages.c.id)
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    from sqlalchemy import update

    for entry in rows:
        actor = entry["verified_actor"]
        if actor.startswith("discord:"):
            actor = "human:" + actor
        delivery = (
            (
                await conn.execute(
                    select(escalation_deliveries)
                    .where(
                        escalation_deliveries.c.escalation_id == entry["escalation_id"],
                        escalation_deliveries.c.channel_id.is_not(None),
                    )
                    .order_by(escalation_deliveries.c.created_at)
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        values = input_values(
            transport=entry["transport"],
            external_id=entry["external_message_id"],
            actor=actor,
            author_id=actor.removeprefix("human:discord:"),
            channel_id=delivery["channel_id"] if delivery else "legacy:unknown",
            text=entry["text"],
            source="legacy",
            project_id=entry["project_id"],
            external_thread_id=delivery["thread_id"] if delivery else None,
            received_at=entry["received_at"],
        )
        values.update(
            ask_id=entry["ask_id"],
            routing_state="receipt",
            retention_entry_id=entry["id"],
            receipt={"state": "legacy", "entry_id": entry["id"]},
        )
        item, _, _ = await handler.db.admit_ask_input(values, conn=conn, enforce_limits=False)
        await conn.execute(
            update(escalation_messages)
            .where(
                escalation_messages.c.id == entry["id"],
            )
            .values(input_id=item["id"])
        )
    return len(rows)
