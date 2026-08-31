"""MessageDeliveryEngine — cascade step + transcript-tail fallback.

Owns the delivery policy in supervisor-agent §5/§7:

- Enumerate recipients with pending messages.
- For each recipient, dispatch by ``to_kind`` and the session activity signal:
  ``user`` → mark delivered ``via="platform"`` (adapters render
  ``message.sent``); ``session|task`` → consult the
  :class:`~src.messages.session_lens.SessionManagerProto` for a coarse
  ``idle|busy|sleeping|absent`` signal and pick nudge / skip / wake / park.
- Sweep delivered-but-unreplied messages past
  ``messages.reply_timeout`` and, if the recipient's transcript shows a
  completed assistant turn since delivery, materialise it as a reply
  (``via="transcript_tail"``).

Kept intentionally small: the engine reads/writes only through the
public :class:`~src.database.queries.message_queries.MessageQueriesMixin`
surface (spec §11.1 binding resolution #3 — no ``db._engine``, no new
transactions), and treats the event bus as optional (``None`` no-ops).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.messages.session_lens import Activity, SessionManagerProto
from src.models import Message

logger = logging.getLogger(__name__)

__all__ = ["MessageDeliveryEngine", "PARK_AFTER_SECONDS"]


#: Messages pending longer than this (24 h) against a ``to_kind="session"``
#: recipient are re-addressed to the original sender ("parked") — spec §7
#: line 463 "park stale" mitigation for messages to dead/retired sessions.
#: Task recipients ride into ``aq prime`` at the next session start and
#: are never parked; profile recipients are consumed via ``aq inbox`` and
#: also never parked.
PARK_AFTER_SECONDS: float = 86_400.0


class MessageDeliveryEngine:
    """Delivery cascade step for the ``messages`` substrate.

    Args:
        db: Adapter exposing :class:`MessageQueriesMixin`.
        sessions: Adapter satisfying :class:`SessionManagerProto`.
        config: ``AppConfig`` or ``MessagesConfig``; the engine reads
            ``max_inject_per_prompt``, ``reply_timeout``, and
            ``transcript_tail_fallback`` off ``.messages`` when present,
            otherwise off the object itself.
        bus: Optional event bus with ``async emit(event, payload)``.
            ``None`` disables event emission entirely.
    """

    def __init__(
        self,
        db,
        sessions: SessionManagerProto,
        config,
        bus=None,
    ):
        self._db = db
        self._sessions = sessions
        self._config = _messages_config(config)
        self._bus = bus

    # -- public --------------------------------------------------------

    async def run_delivery_pass(self) -> dict[str, Any]:
        """One cascade step: deliver / skip / park pending messages.

        Returns ``{"success": True, "delivered": n, "skipped_busy": n,
        "parked": n}``. ``delivered`` counts messages the engine itself
        claimed via :meth:`~MessageQueriesMixin.mark_delivered` (CAS
        losses do not count and produce no event).
        """
        delivered = 0
        skipped_busy = 0
        parked = 0

        recipients = await self._db.get_pending_recipients()
        for to_kind, to_id, project_id in recipients:
            pending = await self._db.get_pending_messages(
                to_kind, to_id, limit=self._config.max_inject_per_prompt
            )
            if not pending:
                continue

            if to_kind == "user":
                delivered += await self._deliver_to_user(pending)
                continue

            if to_kind == "profile":
                # Profile recipients are project-agnostic and consumed by
                # ``aq inbox`` / prime — the delivery engine never touches
                # a session for them and never parks them (spec §11 line
                # 403; parking horizon per spec §7 line 463 applies only
                # to ``to_kind="session"``).
                continue

            kind, target_id, resolved_project = _target_from_recipient(
                to_kind, to_id, project_id
            )
            if kind is None:
                # Legal to_kind we don't route (defensive; MESSAGE_TO_KINDS
                # today is {session, task, profile, user} and all four are
                # handled above). Leave pending.
                continue

            activity: Activity = await self._sessions.activity(
                kind=kind, target_id=target_id, project_id=resolved_project
            )

            if activity == "busy":
                skipped_busy += 1
                continue

            if activity == "sleeping":
                started = await self._sessions.ensure_started(
                    kind=kind, target_id=target_id, project_id=resolved_project
                )
                if not started:
                    continue
                activity = "idle"

            if activity == "absent":
                # Task recipients: rides into prime, never parked.
                # Session recipients: subject to the 24h parking sweep.
                if to_kind == "session":
                    parked += await self._maybe_park(pending)
                continue

            # idle → render one nudge for the batch
            text = _render_nudge(pending)
            ok = await self._sessions.nudge(
                kind=kind, target_id=target_id, project_id=resolved_project, text=text
            )
            if not ok:
                # Leave rows pending; the engine retries next cycle.
                continue
            for msg in pending:
                if await self._db.mark_delivered(msg.id, via="nudge"):
                    delivered += 1
                    await self._emit(
                        "message.delivered",
                        {
                            "message_id": msg.id,
                            "project_id": msg.project_id,
                            "method": "nudge",
                        },
                    )

        return {
            "success": True,
            "delivered": delivered,
            "skipped_busy": skipped_busy,
            "parked": parked,
        }

    async def check_reply_timeouts(self) -> int:
        """Sweep delivered-but-unreplied messages past ``reply_timeout``.

        For each candidate whose recipient's transcript has an assistant
        turn newer than ``delivered_at``, materialise a reply row with
        ``via="transcript_tail"`` (spec §6.2 fallback). Returns the
        number of transcript-tail replies created.
        """
        if not self._config.transcript_tail_fallback:
            return 0

        cutoff = time.time() - self._config.reply_timeout
        # Cast a wide net: session/task recipients whose messages could be
        # awaiting a reply. list_messages already excludes archived rows.
        candidates: list[Message] = []
        for to_kind in ("session", "task"):
            candidates.extend(
                await self._db.list_messages(
                    to_kind=to_kind, include_archived=False, limit=200
                )
            )

        resolved = 0
        seen_threads: set[str] = set()
        for msg in candidates:
            # Internal question handoffs use explicit question commands;
            # never fabricate a user reply from the supervisor's transcript.
            if msg.body_kind == "agent_question":
                continue
            if msg.delivered_at is None or msg.delivered_at > cutoff:
                continue
            if msg.reply_to_id is not None:
                # Already a reply; not the message under sweep.
                continue
            if await self._has_reply(msg):
                continue
            kind, target_id, project_id = _target_from_recipient(
                msg.to_kind, msg.to_id, msg.project_id
            )
            if kind is None:
                continue
            # Dedupe per recipient thread so we don't post the same tail
            # twice when several delivered messages share it.
            thread_key = (
                f"{project_id}:{msg.thread_id}"
                if msg.thread_id
                else f"{kind}:{target_id}:{project_id}"
            )
            if thread_key in seen_threads:
                continue
            tail = await self._sessions.tail_assistant_turn(
                kind=kind,
                target_id=target_id,
                project_id=project_id,
                since=msg.delivered_at,
            )
            if not tail:
                continue
            seen_threads.add(thread_key)
            reply = await self._db.create_message(
                project_id=msg.project_id,
                from_kind="session",
                from_id=msg.to_id,
                to_kind=msg.from_kind if msg.from_kind != "system" else "user",
                to_id=msg.from_id,
                body=tail,
                thread_id=msg.thread_id,
                reply_to_id=msg.id,
            )
            claimed = await self._db.mark_delivered(reply.id, via="transcript_tail")
            if not claimed:
                # CAS lost — another actor already delivered this reply row.
                # Skip both events; don't count as resolved.
                continue
            await self._emit(
                "message.replied",
                {
                    "message_id": msg.id,
                    "reply_id": reply.id,
                    "project_id": msg.project_id,
                    "body": tail,
                    "via": "transcript_tail",
                    **({"thread_id": msg.thread_id} if msg.thread_id else {}),
                },
            )
            # Platform renderers (e.g. Discord's _on_message_sent) subscribe
            # to message.sent, not message.replied — so tail-recovered
            # replies addressed to a user must fan out the same envelope
            # _deliver_to_user emits, otherwise Discord never posts them.
            if reply.to_kind == "user":
                await self._emit("message.sent", _message_sent_payload(reply))
            resolved += 1
        return resolved

    # -- internals -----------------------------------------------------

    async def _deliver_to_user(self, pending: list[Message]) -> int:
        delivered = 0
        for msg in pending:
            if await self._db.mark_delivered(msg.id, via="platform"):
                delivered += 1
                await self._emit("message.sent", _message_sent_payload(msg))
        return delivered

    async def _maybe_park(self, pending: list[Message]) -> int:
        """Re-address stale ``to_kind="session"`` messages to the original
        sender (spec §7 line 463). Task and profile recipients are never
        parked; the caller must not invoke this for them."""
        now = time.time()
        parked = 0
        for msg in pending:
            if msg.to_kind != "session":
                continue
            if (now - msg.created_at) < PARK_AFTER_SECONDS:
                continue
            # Only park to a real sender; system-authored rows have no user
            # to notify, so archive silently.
            if msg.from_kind == "user":
                body = (
                    f"[parked] your message to {msg.to_kind}:{msg.to_id} "
                    f"({msg.id}) was not delivered within "
                    f"{int(PARK_AFTER_SECONDS // 3600)}h. Original body:\n\n"
                    f"{msg.body}"
                )
                await self._db.create_message(
                    project_id=msg.project_id,
                    from_kind="system",
                    from_id="delivery-engine",
                    to_kind="user",
                    to_id=msg.from_id,
                    body=body,
                    subject=(f"Undelivered: {msg.subject}" if msg.subject else "Undelivered message"),
                    thread_id=msg.thread_id,
                    reply_to_id=msg.id,
                )
            await self._db.archive_messages([msg.id])
            parked += 1
        return parked

    async def _has_reply(self, msg: Message) -> bool:
        """True if a reply row already links back to ``msg``.

        Cheap-enough scan of the recent project message stream — the
        sweep runs once per cascade cycle and only inspects the small
        set of unresolved candidates.
        """
        rows = await self._db.list_messages(
            project_id=msg.project_id, system_only=msg.project_id is None,
            include_archived=True, limit=200
        )
        return any(r.reply_to_id == msg.id for r in rows)

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.emit(event, payload)
        except Exception:
            logger.debug("emit(%s) failed", event, exc_info=True)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _messages_config(config):
    """Accept either an :class:`AppConfig` or a :class:`MessagesConfig`."""
    if hasattr(config, "messages"):
        return config.messages
    return config


def _target_from_recipient(
    to_kind: str, to_id: str, project_id: str | None
) -> tuple[str | None, str | None, str | None]:
    """Map (``messages.to_kind``, ``to_id``) → ``SessionManagerProto`` kind.

    Legal ``to_kind`` set is :data:`~src.models.MESSAGE_TO_KINDS`
    (``{session, task, profile, user}``). The delivery engine handles
    ``user`` and ``profile`` before this call; only session-bearing kinds
    reach here.

    - ``task`` → ``("task", <task_id>, <project_id>)``
    - ``session`` → ``("session", <session_name>, <project_id>)``
      (``to_id`` is a session **name** such as ``supervisor-<pid>`` or
      ``s-<task-name>`` — the lens resolves it via ``get_session_by_name``)
    - anything else → ``(None, None, None)``.
    """
    if to_kind == "task":
        return "task", to_id, project_id
    if to_kind == "session":
        return "session", to_id, project_id
    return None, None, None


def _message_sent_payload(msg: Message) -> dict[str, Any]:
    """Envelope emitted on ``message.sent`` for user-addressed rows.

    Shared by the platform-delivery path (:meth:`_deliver_to_user`) and
    the transcript-tail fallback so platform renderers see the identical
    payload shape regardless of which path materialised the row.
    """
    payload: dict[str, Any] = {
        "message_id": msg.id,
        "project_id": msg.project_id,
        "from_kind": msg.from_kind,
        "from_id": msg.from_id,
        "to_kind": msg.to_kind,
        "to_id": msg.to_id,
    }
    if msg.thread_id:
        payload["thread_id"] = msg.thread_id
    if msg.subject:
        payload["subject"] = msg.subject
    pane_open = getattr(msg, "pane_open", None)
    if pane_open:
        if isinstance(pane_open, str):
            import json as _json

            try:
                payload["pane_open"] = _json.loads(pane_open)
            except Exception:
                pass
        else:
            payload["pane_open"] = pane_open
    return payload


def _render_nudge(batch: list[Message]) -> str:
    """Render one nudge text block for a batch of pending messages.

    Follows the spec §6.1 envelope: ``[message <id> from <from>] <body>``
    plus the standing "reply with ``aq reply <id>``" instruction. When
    several messages arrive together, each renders as a paragraph in
    priority-then-arrival order (already the ``get_pending_messages``
    ordering) and the reply instruction lists them all.
    """
    parts: list[str] = []
    for msg in batch:
        header = f"[{msg.id} from {msg.from_kind}:{msg.from_id}]"
        if msg.subject:
            header = f"{header} {msg.subject}"
        parts.append(f"{header}\n{msg.body}")
    ids = ", ".join(msg.id for msg in batch)
    instruction = (
        f'Reply with `aq reply <msg-id> "…"` (ids: {ids}).' if len(batch) > 1
        else f'Reply with `aq reply {batch[0].id} "…"`.'
    )
    return "\n\n".join(parts) + "\n\n" + instruction
