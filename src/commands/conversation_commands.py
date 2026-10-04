"""Verified, daemon-internal intake for supervisor conversations."""

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
from src.conversations.intake import (
    DM_GUILD,
    KIND_CHANNEL,
    KIND_THREAD,
    dm_thread_id,
    normalise_text,
)
from src.conversations.limits import (
    AUTHOR_WINDOW_LIMIT,
    CHANNEL_WINDOW_LIMIT,
    MAX_INPUT_CHARS,
    MAX_REPLY_CHARS,
    WINDOW_SECONDS,
)
from src.conversations.outbox import ACTION_STATUS_LINE, ConversationOutbox, UnboundOutbox
from src.conversations.preconditions import conversation_preconditions
from src.conversations.render import (
    STATUS_BACK,
    STATUS_OFFLINE,
    render_brief,
    render_reply,
    sanitise_reply,
)
from src.database.queries.conversation_queries import (
    CONVERSATION_STATES,
    QUEUED_SUPERVISOR_RECIPIENT,
    ConversationClosed,
    ConversationConflict,
    ConversationNotFound,
    ConversationRateLimited,
    ConversationStateError,
)
from src.sessions.spec import named_session_name

_LIVE_SESSION_STATES = frozenset({"starting", "running", "draining"})
#: How long after the thread-open ack the §2.4 status line becomes due. The
#: shared dispatcher claims by ``due_at``, so this is what orders the line
#: after the thread it posts in instead of leaving it to a delivery-id hash.
_STATUS_LINE_DELAY = 1.0
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


def _conversation_kind(*, direct: bool, in_thread: bool, require_mention: bool) -> str:
    """Which conversation a new turn opens (chat-extension spec §2.2).

    The classifier already decided this; the command restates it so the
    durable row cannot disagree with the decision that produced it. A direct
    message and a mention-free top-level message are the channel's one
    conversation; a mention or a thread the operator started is its own.
    """
    if direct or not in_thread and not require_mention:
        return KIND_CHANNEL
    return KIND_THREAD


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
        before_id = args.get("before_id")
        if (
            set(args) - {"conversation_id", "states", "limit", "before", "before_id"}
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
            # The id tie-breaker only continues a timestamp cursor.
            or (
                before_id is not None
                and (before is None or not isinstance(before_id, str) or not before_id.strip())
            )
        ):
            return _error("invalid_request", "invalid history filter or pagination arguments")

        next_before = next_before_id = None
        if conversation_id is not None:
            conversation = await self.db.get_conversation(conversation_id)
            if conversation is None:
                return _error("conversation_not_found", "conversation does not exist")
            conversations = [conversation] if not states or conversation["state"] in states else []
        else:
            rows = await self.db.list_conversations(
                states=states, limit=limit + 1, before=before, before_id=before_id
            )
            conversations = rows[:limit]
            if len(rows) > limit:
                next_before = conversations[-1]["updated_at"]
                next_before_id = conversations[-1]["id"]

        history = []
        for conversation in conversations:
            paging = conversation_id is not None
            rows = await self.db.list_conversation_inputs(
                conversation["id"],
                limit=limit + 1,
                before=before if paging else None,
                before_id=before_id if paging else None,
            )
            inputs = [{**item, "text_expired": item["text"] is None} for item in rows[:limit]]
            more = len(rows) > limit
            cursor = {
                "next_before": inputs[-1]["received_at"] if more else None,
                "next_before_id": inputs[-1]["id"] if more else None,
            }
            history.append({**conversation, "inputs": inputs, **cursor})
            if paging:
                next_before, next_before_id = cursor["next_before"], cursor["next_before_id"]
        return {
            "success": True,
            "conversations": history,
            "next_before": next_before,
            "next_before_id": next_before_id,
        }

    async def _cmd_supervisor_inbox_reply(self, args: dict[str, Any]) -> dict[str, Any]:
        """Only the live launch addressed by the input may queue an answer."""
        bad = sorted(_FORBIDDEN_AUTHORITY_ARGS.intersection(args))
        if bad:
            return _error("spoofed_identity", "caller identity and destination are server-derived")
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            live = None
            if principal.kind is PrincipalKind.SESSION and principal.elevated:
                live = await self.db.get_session(principal.session_id)
            expected_name = named_session_name("supervisor", principal.project_id or "global")
            if (
                live is None
                or live.state not in _LIVE_SESSION_STATES
                or live.name != expected_name
                or live.profile_id != "supervisor"
                or live.lifecycle != "named"
                or live.project_id != principal.project_id
                or principal.session_id != live.id
                or not matches_session_instance(principal, live.instance_token)
            ):
                return _error("out_of_scope", "reply requires the live addressed supervisor launch")

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
        await self.db.route_queued_conversation_inputs(self.config.discord.project_id)
        notice = await self.db.get_message(item["supervisor_message_id"])
        if principal.kind is not PrincipalKind.LOCAL and (
            notice is None
            or notice.to_id != f"supervisor-{principal.project_id or 'global'}"
            or notice.project_id != principal.project_id
        ):
            return _error("out_of_scope", "input is addressed to another supervisor")
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
            if item["reply_message_id"] is not None:
                return _error("input_already_answered", "conversation input already has an answer")
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
        # §2.4: the supervisor is back, so the offline line says so and is
        # retired once this answer posts. One second earlier is what orders the
        # edit before the answer the shared dispatcher claims by ``due_at``.
        if await self.db.find_conversation_status_line(conversation_id) is not None:
            await self._queue_status_line(
                outbox,
                conversation,
                state=STATUS_BACK,
                queued=0,
                due_at=getattr(self, "_clock", time.time)() - 1,
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
                "author_id": envelope.author_id,
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
        settings = self.config.discord
        direct = envelope.guild_id == DM_GUILD
        require_mention = bool(settings.conversation.require_mention)
        if direct:
            # §2.1: a direct message is admitted only while the operator has
            # opted DMs in, and only as the dm: thread of its own channel.
            if not settings.conversation.allow_dm or envelope.external_thread_id != dm_thread_id(
                envelope.channel_id
            ):
                return _error("dm", "direct messages are not admitted")
        elif envelope.guild_id != str(settings.guild_id) or envelope.channel_id != str(
            settings.channel_id
        ):
            return _error("foreign_destination", "message is not in the configured guild/channel")

        conversation_id = args.get("conversation_id")
        if conversation_id is not None and (
            not isinstance(conversation_id, str) or not conversation_id
        ):
            return _error("invalid_envelope", "conversation_id must be a non-empty string")
        conversation = None
        if direct:
            # §2.2: a direct message belongs to its private channel's one
            # conversation. Resolving it by the channel rather than by the
            # thread is what lets a new message start a fresh one after the
            # previous conversation was closed by the idle sweep.
            if conversation_id:
                return _error("invalid_envelope", "a direct message names no conversation")
            conversation = await self.db.find_channel_conversation(
                transport=envelope.transport, channel_id=envelope.channel_id
            )
        elif envelope.external_thread_id:
            if conversation_id:
                conversation = await self.db.get_conversation(conversation_id)
            else:
                conversation = await self.db.find_conversation_by_thread(
                    transport=envelope.transport,
                    channel_id=envelope.channel_id,
                    external_thread_id=envelope.external_thread_id,
                )
            # §2.2: an unbound thread is a conversation the operator started,
            # which mention-only routing keeps refusing. An escalation thread
            # never reaches here at all.
            if conversation is None and require_mention:
                return _error(
                    "conversation_not_found", "conversation is not bound to a known thread"
                )
        elif require_mention and not envelope.mentions_bot:
            # The gateway already refused this; the command refuses it again so
            # no caller can skip the flag.
            return _error("no_bot_mention", "top-level message does not mention the bot")
        elif conversation_id:
            # §2.2: a top-level message naming the channel's one conversation
            # continues it rather than opening another. Only a channel
            # conversation can be named this way: a thread conversation is
            # reached through the thread the gateway observed.
            conversation = await self.db.get_conversation(conversation_id)
            if conversation is None or conversation["kind"] != KIND_CHANNEL:
                return _error(
                    "invalid_envelope",
                    "a follow-up must name the channel conversation or its observed thread",
                )
        if conversation is not None:
            if direct:
                disagreement = conversation["guild_id"] != DM_GUILD
            else:
                disagreement = (
                    conversation["transport"] != envelope.transport
                    or conversation["guild_id"] != envelope.guild_id
                    or conversation["channel_id"] != envelope.channel_id
                    or conversation["external_thread_id"] != envelope.external_thread_id
                    or conversation["external_root_message_id"] != envelope.external_root_message_id
                )
            if disagreement:
                return _error(
                    "foreign_destination",
                    "conversation disagrees with the observed destination",
                )
            if envelope.author_id not in conversation["audience"]:
                return _error(
                    "author_not_allowlisted", "author is not in the conversation audience"
                )
            conversation_id = conversation["id"]
        elif not envelope.external_thread_id and (
            envelope.external_root_message_id != envelope.external_message_id
        ):
            # A message that opens a conversation is its own root. A top-level
            # follow-up above named the conversation it joins, and the durable
            # row is what proved that root; here there is no row, so the claim
            # is unchecked and refused.
            return _error(
                "invalid_envelope", "top-level intake requires the message to be its own root"
            )

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
            await self._repair_queued_status_line(outbox, stored)
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
            recipient, supervisor_project_id = await self.db.resolve_conversation_supervisor(
                self.config.discord.project_id
            )
            accepted = await self.db.accept_conversation_input(
                **envelope.model_dump(exclude={"mentions_bot", "text", "tag"}),
                text=text,
                verified_actor=verified_actor,
                audience=allowlist,
                source=source,
                conversation_id=conversation_id,
                brief=brief,
                now=now,
                enforce_limits=True,
                supervisor_recipient=recipient or QUEUED_SUPERVISOR_RECIPIENT,
                supervisor_project_id=supervisor_project_id,
                kind=_conversation_kind(
                    direct=direct,
                    in_thread=bool(envelope.external_thread_id),
                    require_mention=require_mention,
                ),
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
        await self._repair_queued_status_line(
            outbox,
            accepted["conversation"],
            forced=recipient is None,
        )
        posted = await self._conversation_post_result(
            item=accepted["input"],
            conversation=accepted["conversation"],
            created=accepted["created"],
            source=source,
            outbox=outbox,
        )
        if recipient is None:
            # An input with no live supervisor to read it must never look like
            # one that is being handled: say so in the channel, once per
            # conversation, after the acknowledgement. The queue drains by
            # itself when a supervisor starts (§2.4).
            await self._conversation_notice(
                outbox,
                envelope,
                accepted["conversation"],
                kind="supervisor_missing",
                dedup_key=f"conv-notice:supervisor-missing:{accepted['conversation']['id']}",
            )
        return posted

    async def _repair_queued_status_line(
        self, outbox: ConversationOutbox, conversation: dict, *, forced: bool = False
    ) -> None:
        """Reserve the §2.4 status line when a conversation has a queue.

        The line is what tells the operator their message is not lost, so it is
        reserved from the same transaction boundary as the input and repaired on
        replay like the thread-open ack is. With *forced* the queue is known to be
        non-empty even when the count cannot be read yet.
        """
        conversation_id = conversation["id"]
        queued = await self.db.count_queued_conversation_inputs(conversation_id)
        if not queued and not forced:
            return
        # §2.4: one line per conversation, edited in place as the count grows.
        # It is due just after the thread-open ack so it never races the thread
        # it is posted in.
        await self._queue_status_line(
            outbox,
            conversation,
            state=STATUS_OFFLINE,
            queued=queued or 1,
            due_at=getattr(self, "_clock", time.time)() + _STATUS_LINE_DELAY,
        )

    async def _queue_status_line(
        self,
        outbox: ConversationOutbox,
        conversation: dict,
        *,
        state: str,
        queued: int,
        due_at: float | None = None,
    ) -> str | None:
        """Reserve one §2.4 status line for a conversation.

        The queue count is part of the dedup key because the line is edited,
        not re-posted: each new count is one delivery of the same post. The
        offline escalation of an unanswered queue is separate -- it rides the
        ``supervisor_delivery`` incident family and the delay notice.
        """
        conversation_id = conversation["id"]
        return await outbox.enqueue(
            owner_id=conversation_id,
            kind=ACTION_STATUS_LINE,
            dedup_key=f"conv-status:{conversation_id}:{state}:{max(1, int(queued))}",
            payload={
                "state": state,
                "queued": max(1, int(queued)),
                "conversation_id": conversation_id,
                "channel_id": conversation["channel_id"],
                "thread_id": conversation["external_thread_id"],
                "author_id": conversation["created_by"].removeprefix("human:discord:"),
            },
            **({} if due_at is None else {"due_at": due_at}),
        )
