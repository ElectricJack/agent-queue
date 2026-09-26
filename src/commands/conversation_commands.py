"""Verified, daemon-internal intake for global-supervisor conversations."""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any

from pydantic import ValidationError

from src.commands.principal import (
    TRUSTED_LOCAL,
    PrincipalKind,
    current_principal,
    matches_session_instance,
)
from src.conversations.envelope import ConversationEnvelope
from src.conversations.intake import normalise_text
from src.conversations.limits import (
    AUTHOR_WINDOW_LIMIT,
    CHANNEL_WINDOW_LIMIT,
    MAX_INPUT_CHARS,
    MAX_REPLY_CHARS,
    WINDOW_SECONDS,
)
from src.conversations.outbox import ConversationOutbox, UnboundOutbox
from src.conversations.preconditions import conversation_preconditions
from src.conversations.render import render_brief, render_reply, sanitise_reply
from src.database.queries.conversation_queries import (
    ConversationClosed,
    ConversationConflict,
    ConversationNotFound,
    ConversationRateLimited,
    ConversationStateError,
    CONVERSATION_STATES,
)

_LIVE_SESSION_STATES = frozenset({"starting", "running", "draining"})
_FORBIDDEN_AUTHORITY_ARGS = frozenset(
    {
        "actor",
        "actor_id",
        "human",
        "verified_actor",
        "from_kind",
        "from_id",
        "to_kind",
        "to_id",
        "session",
        "session_id",
        "destination",
        "thread_id",
        "supervisor_owner",
    }
)


def _error(code: str, error: str, **details: Any) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": error, **details}


class ConversationCommandsMixin:
    """Intake checks identity explicitly; SERVICE's stored DENY_ALL is not enforced."""

    def _conversation_read_allowed(self) -> bool:
        principal = current_principal() or TRUSTED_LOCAL
        return principal.kind is PrincipalKind.LOCAL or (
            principal.kind is PrincipalKind.SESSION
            and principal.elevated
            and principal.project_id is None
        )

    async def _cmd_supervisor_inbox_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Installation-wide health without contacting Discord or changing state."""
        if not self._conversation_read_allowed():
            return _error("out_of_scope", "conversation reads require local or global supervisor")
        if args:
            return _error("invalid_request", "status takes no arguments")
        from src.discord.intake_diagnostics import empty_snapshot

        bot = getattr(self.orchestrator, "_discord_bot", None)
        cutover = getattr(bot, "_cutover_report", None)
        outbox = getattr(self.orchestrator, "conversation_outbox", None) or UnboundOutbox()
        preconditions = conversation_preconditions(
            self.config, cutover_status=getattr(cutover, "status", None), outbox_bound=outbox.bound
        )
        diagnostics = {"message_content_intent": None, "permissions": None}
        if bot is not None:
            backfill = getattr(bot, "_conversation_backfill", None)
            if backfill is not None:
                diagnostics = backfill().diagnostics(bot)
            else:
                diagnostics["message_content_intent"] = getattr(
                    getattr(bot, "intents", None), "message_content", None
                )
        counter = getattr(bot, "_intake_diagnostics", None)
        return {
            "success": True,
            "enabled": self.config.discord.conversation.enabled,
            "preconditions": {"ok": preconditions.ok, "unmet": list(preconditions.unmet)},
            "diagnostics": {**diagnostics, "outbox_bound": outbox.bound},
            "limits": {
                "max_input_chars": MAX_INPUT_CHARS,
                "author_window_limit": AUTHOR_WINDOW_LIMIT,
                "channel_window_limit": CHANNEL_WINDOW_LIMIT,
                "window_seconds": WINDOW_SECONDS,
                "max_reply_chars": MAX_REPLY_CHARS,
            },
            "counts": await self.db.conversation_counts(),
            "backfill": {
                "cursors": await self.db.list_backfill_cursors(),
                "gaps": await self.db.list_intake_gaps(),
            },
            "intake": counter.snapshot() if counter is not None else empty_snapshot(),
        }

    async def _cmd_supervisor_inbox_history(self, args: dict[str, Any]) -> dict[str, Any]:
        """Page conversations or the inputs of one conversation, newest first."""
        if not self._conversation_read_allowed():
            return _error("out_of_scope", "conversation reads require local or global supervisor")
        conversation_id = args.get("conversation_id")
        states, limit, before = args.get("states"), args.get("limit", 50), args.get("before")
        if (
            set(args) - {"conversation_id", "states", "limit", "before"}
            or (
                conversation_id is not None
                and (not isinstance(conversation_id, str) or not conversation_id.strip())
            )
            or (
                states is not None
                and (
                    not isinstance(states, list)
                    or any(not isinstance(s, str) or s not in CONVERSATION_STATES for s in states)
                )
            )
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
            or (
                before is not None
                and (
                    isinstance(before, bool)
                    or not isinstance(before, (int, float))
                    or not math.isfinite(before)
                )
            )
        ):
            return _error("invalid_request", "invalid history filter or pagination arguments")

        next_before = None
        if conversation_id is not None:
            conversation = await self.db.get_conversation(conversation_id)
            if conversation is None:
                return _error("conversation_not_found", "conversation does not exist")
            conversations = [conversation] if not states or conversation["state"] in states else []
        else:
            rows = await self.db.list_conversations(states=states, limit=limit + 1, before=before)
            conversations = rows[:limit]
            if len(rows) > limit:
                next_before = conversations[-1]["updated_at"]

        history = []
        for conversation in conversations:
            rows = await self.db.list_conversation_inputs(
                conversation["id"],
                limit=limit + 1,
                before=before if conversation_id is not None else None,
            )
            inputs = [{**item, "text_expired": item["text"] is None} for item in rows[:limit]]
            input_before = inputs[-1]["received_at"] if len(rows) > limit else None
            history.append({**conversation, "inputs": inputs, "next_before": input_before})
            if conversation_id is not None:
                next_before = input_before
        return {"success": True, "conversations": history, "next_before": next_before}

    async def _cmd_supervisor_inbox_reply(self, args: dict[str, Any]) -> dict[str, Any]:
        """Only an explicit answer from the live global supervisor queues a reply."""
        bad = sorted(_FORBIDDEN_AUTHORITY_ARGS.intersection(args))
        if bad:
            return _error("spoofed_identity", "caller identity and destination are server-derived")
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            live = None
            if (
                principal.kind is PrincipalKind.SESSION
                and principal.elevated
                and principal.project_id is None
            ):
                live = await self.db.get_session_by_name("supervisor-global")
            if (
                live is None
                or live.state not in _LIVE_SESSION_STATES
                or live.project_id is not None
                or principal.session_id != live.id
                or not matches_session_instance(principal, live.instance_token)
            ):
                return _error("out_of_scope", "reply requires the live global supervisor launch")

        required = {"conversation_id", "input_id", "text", "idempotency_key"}
        if set(args) != required or any(
            not isinstance(args.get(key), str) or not args[key].strip() for key in required
        ):
            return _error("invalid_request", "conversation_id, input_id, text and key are required")
        if len(args["text"]) > 16000 or len(args["idempotency_key"]) > 128:
            return _error("invalid_request", "reply text or idempotency key exceeds its limit")
        conversation_id, input_id = args["conversation_id"], args["input_id"]
        conversation = await self.db.get_conversation(conversation_id)
        if conversation is None:
            return _error("conversation_not_found", "conversation does not exist")
        item = await self.db.get_conversation_input(input_id)
        if item is None or item["conversation_id"] != conversation_id:
            return _error("input_not_in_conversation", "input does not belong to this conversation")
        if conversation["state"] == "closed":
            return _error("conversation_closed", "conversation is closed")
        if item["state"] == "revoked":
            return _error("input_revoked", "conversation input is revoked")
        outbox = getattr(self.orchestrator, "conversation_outbox", None) or UnboundOutbox()
        if not outbox.bound:
            return _error(
                "preconditions_unmet", "conversation outbox unbound", unmet=["outbox_unbound"]
            )

        digest = hashlib.sha256(f"{conversation_id}:{args['idempotency_key']}".encode()).hexdigest()
        reply_message_id = f"msg-conv-reply-{digest[:32]}"
        try:
            recorded = await self.db.record_conversation_reply(
                conversation_id=conversation_id,
                input_id=input_id,
                reply_message_id=reply_message_id,
                body=args["text"],
                now=getattr(self, "_clock", time.time)(),
            )
        except ConversationNotFound:
            return _error("conversation_not_found", "conversation or input no longer exists")
        except ConversationConflict:
            return _error("input_not_in_conversation", "input or idempotency key belongs elsewhere")
        except ConversationClosed:
            return _error("conversation_closed", "conversation is closed")
        except ConversationStateError:
            return _error("input_revoked", "conversation input is revoked")

        # Repair a crash after persistence but before enqueue, using the first
        # durable body rather than the retry's possibly changed text.
        text = recorded["message"]["body"]
        resolver = getattr(self.orchestrator, "dashboard_links", None)
        base_url = (await resolver.resolve()).url if resolver is not None else ""
        dedup_key = f"conv-reply:{reply_message_id}"
        discord_text = render_reply(
            text,
            dedup_key=dedup_key,
            base_url=base_url,
            conversation_id=conversation_id,
        )
        await outbox.enqueue(
            owner_id=conversation_id,
            kind="reply",
            dedup_key=dedup_key,
            payload={
                "conversation_id": conversation_id,
                "input_id": input_id,
                "reply_message_id": reply_message_id,
                "text": discord_text,
            },
        )
        await self.orchestrator.bus.emit(
            "conversation.reply_queued.v1",
            {
                "conversation_id": conversation_id,
                "input_id": input_id,
                "reply_message_id": reply_message_id,
                "delivery_dedup_key": dedup_key,
                "created": recorded["created"],
            },
        )
        return {
            "success": True,
            "created": recorded["created"],
            "reply_message_id": reply_message_id,
            "delivery_dedup_key": dedup_key,
            "discord_text_chars": len(discord_text),
            "truncated": discord_text
            != f"{sanitise_reply(text, base_url=base_url)} (aq-conv:{dedup_key})",
        }

    async def _conversation_notice(
        self,
        outbox: ConversationOutbox,
        envelope: ConversationEnvelope,
        conversation: dict | None,
        *,
        kind: str,
        dedup_key: str,
        **facts: Any,
    ) -> None:
        await outbox.enqueue(
            owner_id=conversation["id"] if conversation else envelope.external_message_id,
            kind="notice",
            dedup_key=dedup_key,
            payload={
                "kind": kind,
                "channel_id": envelope.channel_id,
                "thread_id": conversation["external_thread_id"] if conversation else None,
                **facts,
            },
        )

    async def _conversation_post_result(
        self,
        *,
        item: dict,
        conversation: dict,
        created: bool,
        source: str,
        outbox: ConversationOutbox,
    ) -> dict[str, Any]:
        # Also on root replay: repair a crash after durable intake but before
        # enqueue. The port's dedup key prevents a second thread-open row.
        if (
            conversation["state"] == "opening"
            and item["external_message_id"] == conversation["external_root_message_id"]
        ):
            await outbox.enqueue(
                owner_id=conversation["id"],
                kind="thread_open",
                dedup_key=f"conv-open:{conversation['transport']}:{conversation['external_root_message_id']}",
                payload={
                    "channel_id": conversation["channel_id"],
                    "root_message_id": conversation["external_root_message_id"],
                    "conversation_id": conversation["id"],
                },
            )
        await self.orchestrator.bus.emit(
            "conversation.input_received.v1",
            {
                "conversation_id": conversation["id"],
                "input_id": item["id"],
                "transport": conversation["transport"],
                "verified_actor": item["verified_actor"],
                "created": created,
                "source": source,
            },
        )
        return {
            "success": True,
            "created": created,
            "conversation_id": conversation["id"],
            "input_id": item["id"],
            "supervisor_message_id": item["supervisor_message_id"],
            "state": item["state"],
        }

    async def _cmd_supervisor_inbox_post(self, args: dict[str, Any]) -> dict[str, Any]:
        bad = sorted(_FORBIDDEN_AUTHORITY_ARGS.intersection(args))
        if bad:
            return _error(
                "spoofed_identity",
                "caller identity is server-derived; forbidden fields: " + ", ".join(bad),
            )
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SERVICE and principal.service_name == "discord-gateway":
            source = args.get("source")
            if not isinstance(source, str) or source not in {"gateway", "backfill"}:
                return _error("invalid_envelope", "gateway source must be gateway or backfill")
        elif (
            principal.kind is PrincipalKind.LOCAL
            and isinstance(args.get("provenance"), str)
            and args["provenance"] in {"replay", "test"}
        ):
            source = args["provenance"]
        else:
            return _error(
                "out_of_scope", "conversation intake requires discord-gateway or local test/replay"
            )
        try:
            envelope = ConversationEnvelope.model_validate(args.get("envelope"))
        except ValidationError:
            # Validation errors contain rejected text; never return or log them.
            return _error("invalid_envelope", "invalid gateway conversation envelope")

        bot = getattr(self.orchestrator, "_discord_bot", None)
        cutover = getattr(bot, "_cutover_report", None)
        outbox = getattr(self.orchestrator, "conversation_outbox", None) or UnboundOutbox()
        preconditions = conversation_preconditions(
            self.config,
            cutover_status=getattr(cutover, "status", None),
            outbox_bound=outbox.bound,
        )
        if not preconditions.ok:
            return _error(
                "preconditions_unmet",
                "conversation preconditions unmet",
                unmet=list(preconditions.unmet),
            )
        allowlist = [str(author) for author in self.config.discord.authorized_users]
        if envelope.author_id not in allowlist:
            return _error("author_not_allowlisted", "author is not on the conversation allowlist")
        if envelope.guild_id != str(self.config.discord.guild_id) or envelope.channel_id != str(
            self.config.discord.channel_id
        ):
            return _error("foreign_destination", "message is not in the configured guild/channel")

        conversation_id = args.get("conversation_id")
        if conversation_id is not None and (
            not isinstance(conversation_id, str) or not conversation_id
        ):
            return _error("invalid_envelope", "conversation_id must be a non-empty string")
        conversation = None
        if envelope.external_thread_id:
            if conversation_id:
                conversation = await self.db.get_conversation(conversation_id)
            else:
                conversation = await self.db.find_conversation_by_thread(
                    transport=envelope.transport,
                    channel_id=envelope.channel_id,
                    external_thread_id=envelope.external_thread_id,
                )
            if conversation is None:
                return _error(
                    "conversation_not_found", "conversation is not bound to a known thread"
                )
            if (
                conversation["transport"] != envelope.transport
                or conversation["guild_id"] != envelope.guild_id
                or conversation["channel_id"] != envelope.channel_id
                or conversation["external_thread_id"] != envelope.external_thread_id
                or conversation["external_root_message_id"] != envelope.external_root_message_id
            ):
                return _error(
                    "foreign_destination", "conversation disagrees with the observed destination"
                )
            if envelope.author_id not in conversation["audience"]:
                return _error(
                    "author_not_allowlisted", "author is not in the conversation audience"
                )
            conversation_id = conversation["id"]
        elif conversation_id:
            return _error("invalid_envelope", "a follow-up must name its observed thread")

        text = normalise_text(
            envelope.text, bot_user_id=getattr(getattr(bot, "user", None), "id", None)
        )
        if not text:
            return _error("empty_text", "message has no text")
        if len(text) > MAX_INPUT_CHARS:
            await self._conversation_notice(
                outbox,
                envelope,
                conversation,
                kind="oversize",
                dedup_key=f"conv-notice:oversize:{envelope.transport}:{envelope.external_message_id}",
                char_count=len(text),
                limit=MAX_INPUT_CHARS,
            )
            return _error("oversize", "message exceeds the conversation input limit")
        replay = await self.db.find_conversation_input_by_external(
            transport=envelope.transport,
            external_message_id=envelope.external_message_id,
        )
        if replay is not None:
            stored = await self.db.get_conversation(replay["conversation_id"])
            if (
                replay["author_id"] != envelope.author_id
                or replay["channel_id"] != envelope.channel_id
                or stored["guild_id"] != envelope.guild_id
                or stored["external_root_message_id"] != envelope.external_root_message_id
                or (conversation_id and conversation_id != stored["id"])
            ):
                return _error("foreign_destination", "replay disagrees with accepted provenance")
            return await self._conversation_post_result(
                item=replay,
                conversation=stored,
                created=False,
                source=source,
                outbox=outbox,
            )

        now = getattr(self, "_clock", time.time)()
        verified_actor = f"human:discord:{envelope.author_id}"

        def brief(conv_id: str, input_id: str) -> str:
            return render_brief(
                conversation_id=conv_id,
                input_id=input_id,
                verified_actor=verified_actor,
                text=text,
                follow_up=conversation_id is not None,
            )

        try:
            accepted = await self.db.accept_conversation_input(
                **envelope.model_dump(exclude={"mentions_bot", "text"}),
                text=text,
                verified_actor=verified_actor,
                audience=allowlist,
                source=source,
                conversation_id=conversation_id,
                brief=brief,
                now=now,
                enforce_limits=True,
            )
        except ConversationRateLimited as exc:
            scope_id = envelope.author_id if exc.scope == "author" else envelope.channel_id
            bucket = int(now // WINDOW_SECONDS) * WINDOW_SECONDS
            await self._conversation_notice(
                outbox,
                envelope,
                conversation,
                kind="rate_limited",
                dedup_key=f"conv-notice:ratelimit:{envelope.transport}:{exc.scope}:{scope_id}:{bucket}",
                scope=exc.scope,
            )
            return _error("rate_limited", "conversation rate limit reached", scope=exc.scope)
        except ConversationClosed:
            await self._conversation_notice(
                outbox,
                envelope,
                conversation,
                kind="conversation_closed",
                dedup_key=f"conv-notice:closed:{conversation_id}",
            )
            return _error("conversation_closed", "conversation is closed; start a new mention")
        except ConversationNotFound:
            return _error("conversation_not_found", "conversation does not exist")
        return await self._conversation_post_result(
            item=accepted["input"],
            conversation=accepted["conversation"],
            created=accepted["created"],
            source=source,
            outbox=outbox,
        )
