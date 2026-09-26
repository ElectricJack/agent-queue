"""Verified, daemon-internal intake for global-supervisor conversations."""

from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.conversations.envelope import ConversationEnvelope
from src.conversations.intake import normalise_text
from src.conversations.limits import MAX_INPUT_CHARS, WINDOW_SECONDS
from src.conversations.outbox import ConversationOutbox, UnboundOutbox
from src.conversations.preconditions import conversation_preconditions
from src.conversations.render import render_brief
from src.database.queries.conversation_queries import (
    ConversationClosed,
    ConversationNotFound,
    ConversationRateLimited,
)

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
