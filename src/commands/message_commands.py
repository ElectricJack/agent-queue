"""Message commands mixin — the inter-agent message queue surface.

Implements docs/specs/implementation/supervisor-agent.md §6.1 (Phase 1).  The
mixin is registered in ``CommandHandler``'s bases, so every command here is
automatically reachable from Discord, the embedded MCP server, the HTTP API
(``POST /api/message/{command}``) and the ``aq`` CLI.

What lives here is the *queue*: create a row, read the pending rows for a
recipient, mark them delivered, write a reply row.  What does **not** live
here is the delivery engine (waking a sleeping session, nudging an idle one,
transcript-tail fallback) — that is Phase 3 and needs the session runtime's
activity signal.  Until it lands, ``messages.enabled`` alone gives the
degraded but useful "queue and inbox" mode described in §13.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.  No ``_cmd_*`` in this codebase injects a
``success`` key; ``CommandHandler.execute()`` returns the dict verbatim.
"""

from __future__ import annotations

import logging

from src.models import MESSAGE_FROM_KINDS, MESSAGE_TO_KINDS, Message

logger = logging.getLogger(__name__)

#: Exact error string used when the rollout flag is off.  Operators and docs
#: match on this — do not reword.
MESSAGES_DISABLED_ERROR = "messages are disabled (messages.enabled=false)"


def message_to_dict(msg: Message) -> dict:
    """Render a :class:`~src.models.Message` for the command envelope.

    ``from`` / ``to`` are the flattened ``kind:id`` forms and ``read`` is a
    boolean — those are what ``BRIEF_PROJECTIONS["message"]``
    (``src/cli/envelope.py``) projects, so ``aq message list --brief --json``
    resolves against a real key set rather than a hoped-for one.
    """
    return {
        "id": msg.id,
        "project_id": msg.project_id,
        "from_kind": msg.from_kind,
        "from_id": msg.from_id,
        "from": f"{msg.from_kind}:{msg.from_id}",
        "to_kind": msg.to_kind,
        "to_id": msg.to_id,
        "to": f"{msg.to_kind}:{msg.to_id}",
        "thread_id": msg.thread_id,
        "subject": msg.subject,
        "body": msg.body,
        "priority": msg.priority,
        "created_at": msg.created_at,
        "delivered_at": msg.delivered_at,
        "read_at": msg.read_at,
        "read": msg.read_at is not None,
        "delivered": msg.delivered_at is not None,
        "archive_after_inject": msg.archive_after_inject,
        "archived_at": msg.archived_at,
        "reply_to_id": msg.reply_to_id,
        "via": msg.via,
        "body_kind": msg.body_kind,
        "pane_open": (
            __import__("json").loads(msg.pane_open) if isinstance(msg.pane_open, str) else None
        ),
    }


class MessageCommandsMixin:
    """Message command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # Shared guards
    # ------------------------------------------------------------------

    def _messages_disabled_error(self) -> dict | None:
        """Return the refusal dict when ``messages.enabled`` is false."""
        messages_cfg = getattr(self.config, "messages", None)
        if messages_cfg is None or not messages_cfg.enabled:
            return {"error": MESSAGES_DISABLED_ERROR}
        return None

    def _system_message_scope_error(self) -> dict | None:
        """Only local callers and explicit global administrators own system chat."""
        scope = self._current_scope
        if scope and scope.get("kind") != "local":
            if not scope.get("elevated") or scope.get("project_id") is not None:
                return {"error": "out of scope: system messages require global admin"}
        return None

    async def _emit_message_event(self, event_type: str, payload: dict) -> None:
        """Emit a ``message.*`` event without letting it fail the command."""
        try:
            await self.orchestrator.bus.emit(event_type, payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("failed to emit %s: %s", event_type, exc)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_message_send(self, args: dict) -> dict:
        """Queue a message to a session, task, profile, or user."""
        disabled = self._messages_disabled_error()
        if disabled:
            return disabled

        to_kind = (args.get("to_kind") or "").strip()
        to_id = (args.get("to_id") or "").strip()
        if to_kind not in MESSAGE_TO_KINDS:
            return {
                "error": f"Invalid to_kind '{to_kind}'. "
                f"Allowed: {', '.join(sorted(MESSAGE_TO_KINDS))}"
            }
        if not to_id:
            return {"error": "to_id is required"}

        from_kind = (args.get("from_kind") or "user").strip()
        if from_kind not in MESSAGE_FROM_KINDS:
            return {
                "error": f"Invalid from_kind '{from_kind}'. "
                f"Allowed: {', '.join(sorted(MESSAGE_FROM_KINDS))}"
            }
        from_id = (args.get("from_id") or "").strip()
        if not from_id:
            return {"error": "from_id is required"}

        scope = self._current_scope or {}
        global_sender = (
            scope.get("kind") == "session"
            and scope.get("elevated")
            and scope.get("project_id") is None
            and not args.get("project_id")
        )
        system_only = global_sender or bool(args.get("system_only")) or (
            to_kind == "session" and to_id == "supervisor-global"
        ) or (
            not args.get("project_id")
            and from_kind == "session"
            and from_id == "supervisor-global"
        )
        if system_only:
            scope_error = self._system_message_scope_error()
            if scope_error:
                return scope_error
            project_id = None
        else:
            project_id = args.get("project_id") or self._active_project_id
            if not project_id:
                return {"error": "project_id is required (no active project set)"}
            project = await self.db.get_project(project_id)
            if not project:
                return {"error": f"Project '{project_id}' not found"}

        body = args.get("body")
        if not body or not str(body).strip():
            return {"error": "body is required"}

        priority = args.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int):
            return {"error": "priority must be an integer"}

        reply_to_id = args.get("reply_to_id")
        if reply_to_id:
            parent = await self.db.get_message(reply_to_id)
            if parent is None:
                return {"error": f"Message '{reply_to_id}' not found (reply_to_id)"}

        pane_open = args.get("pane_open")
        body_kind: str | None = None
        pane_open_json: str | None = None
        if pane_open is not None:
            if not isinstance(pane_open, dict):
                return {"error": "pane_open must be an object"}
            view_id = pane_open.get("view")
            if not view_id or not isinstance(view_id, str):
                return {"error": "pane_open.view is required"}
            from src.panes.registry import SERVER_PANE_REGISTRY

            entry = SERVER_PANE_REGISTRY.get(view_id)
            if entry is None:
                return {"error": f"unknown pane view '{view_id}'"}
            if not entry.agent_pushable:
                return {"error": f"pane view '{view_id}' is not agent-pushable"}
            pane_args = pane_open.get("args", {})
            if not isinstance(pane_args, dict):
                return {"error": "pane_open.args must be an object"}
            import json as _json

            pane_open_json = _json.dumps({"view": view_id, "args": pane_args})
            body_kind = "pane_open"

        msg = await self.db.create_message(
            project_id=project_id,
            from_kind=from_kind,
            from_id=from_id,
            to_kind=to_kind,
            to_id=to_id,
            body=str(body),
            subject=args.get("subject"),
            thread_id=args.get("thread_id"),
            priority=priority,
            archive_after_inject=bool(args.get("archive_after_inject", False)),
            reply_to_id=reply_to_id,
            body_kind=body_kind,
            pane_open=pane_open_json,
        )

        _sent_payload = {
            "message_id": msg.id,
            "project_id": msg.project_id,
            "from_kind": msg.from_kind,
            "from_id": msg.from_id,
            "to_kind": msg.to_kind,
            "to_id": msg.to_id,
            "thread_id": msg.thread_id,
            "subject": msg.subject,
        }
        if pane_open_json:
            import json as _json

            _sent_payload["pane_open"] = _json.loads(pane_open_json)
        await self._emit_message_event("message.sent", _sent_payload)

        # "queued" is the only state Phase 1 can honestly report: nothing
        # delivers a message until the Phase 3 engine exists.
        return {
            "message_id": msg.id,
            "state": "queued",
            "message": message_to_dict(msg),
        }

    async def _cmd_message_reply(self, args: dict) -> dict:
        """Reply to a message: marks it read and writes the linked reply row."""
        disabled = self._messages_disabled_error()
        if disabled:
            return disabled

        message_id = (args.get("message_id") or "").strip()
        if not message_id:
            return {"error": "message_id is required"}
        original = await self.db.get_message(message_id)
        if original is None:
            return {"error": f"Message '{message_id}' not found"}

        if original.project_id is None:
            scope_error = self._system_message_scope_error()
            if scope_error:
                return scope_error

        body = args.get("body")
        if not body or not str(body).strip():
            return {"error": "body is required"}

        # A reply is addressed to the original's *sender*, so that sender has
        # to be an addressable recipient.  ``system`` is in MESSAGE_FROM_KINDS
        # but not MESSAGE_TO_KINDS (``ck_messages_to_kind`` enforces that), and
        # ``message_send`` accepts from_kind="system" — so without this guard a
        # reply to a system-sent message reaches create_message and surfaces a
        # raw IntegrityError with the whole chat body echoed in the bound
        # parameters.  Fail cleanly before the insert instead.
        if original.from_kind not in MESSAGE_TO_KINDS:
            return {
                "error": (
                    f"cannot reply to a '{original.from_kind}' message — "
                    f"'{original.from_kind}' is not an addressable recipient "
                    f"(addressable: {', '.join(sorted(MESSAGE_TO_KINDS))})"
                )
            }

        # The reply comes *from* whoever the original was addressed to and
        # goes *to* whoever sent it — mirroring, per design §6.2.  ``task``
        # and ``profile`` recipients have no from_kind of their own, so a
        # reply on their behalf is attributed to the session that read it;
        # callers override with from_kind/from_id when they know better.
        from_kind = (args.get("from_kind") or "").strip()
        if not from_kind:
            from_kind = original.to_kind if original.to_kind in MESSAGE_FROM_KINDS else "session"
        if from_kind not in MESSAGE_FROM_KINDS:
            return {
                "error": f"Invalid from_kind '{from_kind}'. "
                f"Allowed: {', '.join(sorted(MESSAGE_FROM_KINDS))}"
            }
        from_id = (args.get("from_id") or "").strip() or original.to_id

        via = args.get("via")

        await self.db.mark_read(message_id)
        reply = await self.db.create_message(
            project_id=original.project_id,
            from_kind=from_kind,
            from_id=from_id,
            to_kind=original.from_kind,
            to_id=original.from_id,
            body=str(body),
            subject=args.get("subject"),
            thread_id=original.thread_id,
            priority=args.get("priority", original.priority),
            reply_to_id=original.id,
        )

        payload = {
            "message_id": original.id,
            "reply_id": reply.id,
            "project_id": reply.project_id,
            "body": reply.body,
        }
        if via:
            payload["via"] = via
        if reply.thread_id:
            payload["thread_id"] = reply.thread_id
        await self._emit_message_event("message.replied", payload)

        return {
            "message_id": original.id,
            "reply_id": reply.id,
            "reply": message_to_dict(reply),
        }

    async def _cmd_message_inbox(self, args: dict) -> dict:
        """List a recipient's pending messages, optionally marking them delivered."""
        disabled = self._messages_disabled_error()
        if disabled:
            return disabled

        to_kind = (args.get("to_kind") or "").strip()
        to_id = (args.get("to_id") or "").strip()
        if to_kind not in MESSAGE_TO_KINDS:
            return {
                "error": f"Invalid to_kind '{to_kind}'. "
                f"Allowed: {', '.join(sorted(MESSAGE_TO_KINDS))}"
            }
        if not to_id:
            return {"error": "to_id is required"}

        if to_kind == "session" and to_id == "supervisor-global":
            scope_error = self._system_message_scope_error()
            if scope_error:
                return scope_error

        inject = bool(args.get("inject", False))
        limit = args.get("limit")
        if limit is None:
            limit = self.config.messages.max_inject_per_prompt if inject else 50
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            return {"error": "limit must be a non-negative integer"}

        pending = await self.db.get_pending_messages(to_kind, to_id, limit=limit)
        if any(message.project_id is None for message in pending):
            scope_error = self._system_message_scope_error()
            if scope_error:
                return scope_error
        if not inject:
            return {
                "to_kind": to_kind,
                "to_id": to_id,
                "count": len(pending),
                "messages": [message_to_dict(m) for m in pending],
            }

        # Inject path: compare-and-set each row so a racing nudge (Phase 3)
        # and this prompt-boundary inject can never both claim a message.
        claimed: list[Message] = []
        for msg in pending:
            if not await self.db.mark_delivered(msg.id):
                continue
            claimed.append(msg)
            await self._emit_message_event(
                "message.delivered",
                {
                    "message_id": msg.id,
                    "project_id": msg.project_id,
                    "method": "inject",
                },
            )

        archived_ids = [m.id for m in claimed if m.archive_after_inject]
        archived = await self.db.archive_messages(archived_ids) if archived_ids else 0

        # Re-read so the caller sees the real stamps rather than our guesses.
        rendered = []
        for msg in claimed:
            fresh = await self.db.get_message(msg.id)
            rendered.append(message_to_dict(fresh or msg))

        return {
            "to_kind": to_kind,
            "to_id": to_id,
            "count": len(rendered),
            "injected": len(rendered),
            "archived": archived,
            "messages": rendered,
        }

    async def _cmd_message_list(self, args: dict) -> dict:
        """List messages with project/thread/recipient filters."""
        disabled = self._messages_disabled_error()
        if disabled:
            return disabled

        to_kind = args.get("to_kind")
        if to_kind and to_kind not in MESSAGE_TO_KINDS:
            return {
                "error": f"Invalid to_kind '{to_kind}'. "
                f"Allowed: {', '.join(sorted(MESSAGE_TO_KINDS))}"
            }

        limit = args.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            return {"error": "limit must be a non-negative integer"}

        since = args.get("since")
        if since is not None:
            try:
                since = float(since)
            except (TypeError, ValueError):
                return {"error": "since must be an epoch timestamp (number)"}

        system_only = bool(args.get("system_only", False))
        if system_only:
            scope_error = self._system_message_scope_error()
            if scope_error:
                return scope_error

        rows = await self.db.list_messages(
            project_id=None if system_only else args.get("project_id") or self._active_project_id,
            system_only=system_only,
            thread_id=args.get("thread_id"),
            to_kind=to_kind,
            to_id=args.get("to_id"),
            include_archived=bool(args.get("include_archived", False)),
            since=since,
            limit=limit,
        )
        return {
            "count": len(rows),
            "messages": [message_to_dict(m) for m in rows],
        }
