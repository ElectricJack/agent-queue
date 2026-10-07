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
  completed assistant **prose** turn inside
  ``reply_window_multiplier x reply_timeout`` of ``delivered_at``,
  materialise it as a reply (``via="transcript_tail"``).

Kept intentionally small: the engine reads/writes only through the
public :class:`~src.database.queries.message_queries.MessageQueriesMixin`
surface (spec §11.1 binding resolution #3 — no ``db._engine``, no new
transactions), and treats the event bus as optional (``None`` no-ops).
"""

from __future__ import annotations

import logging
import shlex
import time
from typing import Any

from src.messages.session_lens import Activity, SessionManagerProto
from src.models import Message, TaskStatus
from src.sessions.transcripts import is_model_prose

logger = logging.getLogger(__name__)

__all__ = ["MessageDeliveryEngine", "PARK_AFTER_SECONDS"]


#: Messages pending longer than this (24 h) against a ``to_kind="session"``
#: recipient are re-addressed to the original sender ("parked") — spec §7
#: line 463 "park stale" mitigation for messages to dead/retired sessions.
#: Task recipients ride into ``aq prime`` at the next session start and
#: are never parked; profile recipients are consumed via ``aq inbox`` and
#: also never parked.
PARK_AFTER_SECONDS: float = 86_400.0

COLLABORATION_BODY_KINDS = {"collaboration", "collaboration_invite", "collaboration_closed"}
_TASK_NOTIFICATION_KINDS = {"wait_result", "job_result"} | COLLABORATION_BODY_KINDS

#: ``from_id`` prefix of engine-authored senders (``system:review:<slug>``,
#: ``system:supervisor-delivery-watchdog``, ``system:delivery-engine``).
_SYSTEM_SENDER_PREFIX = "system:"


class MessageDeliveryEngine:
    """Delivery cascade step for the ``messages`` substrate.

    Args:
        db: Adapter exposing :class:`MessageQueriesMixin`.
        sessions: Adapter satisfying :class:`SessionManagerProto`.
        config: ``AppConfig`` or ``MessagesConfig``; the engine reads
            ``max_inject_per_prompt``, ``reply_timeout``,
            ``reply_window_multiplier`` and ``transcript_tail_fallback`` off
            ``.messages`` when present, otherwise off the object itself.
        bus: Optional event bus with ``async emit(event, payload)``.
            ``None`` disables event emission entirely.
    """

    def __init__(
        self,
        db,
        sessions: SessionManagerProto,
        config,
        bus=None,
        cron_service=None,
    ):
        self._db = db
        self._sessions = sessions
        self._config = _messages_config(config)
        self._bus = bus
        self.cron_service = cron_service

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
            # Offline turns stay durable until supervisor start routes them.
            # They must neither cold-start a supervisor nor park.
            pending = [msg for msg in pending if msg.to_id != "conversation-queued"]
            if not pending:
                continue

            if to_kind == "task" and pending[0].body_kind in _TASK_NOTIFICATION_KINDS:
                task = await self._db.get_task(to_id)
                if task and task.status == TaskStatus.PAUSED:
                    # Terminal output does not authorize resuming a manual pause.
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
                if pending[0].body_kind == "schedule_prompt":
                    # Session-bound schedules never create a replacement owner.
                    continue
                if all(msg.body_kind == "conversation_input" for msg in pending):
                    continue
                if to_kind == "task" and pending[0].body_kind in _TASK_NOTIFICATION_KINDS:
                    continue
                started = await self._sessions.ensure_started(
                    kind=kind, target_id=target_id, project_id=resolved_project
                )
                if not started:
                    continue
                activity = "idle"

            if activity == "absent":
                if pending[0].body_kind == "schedule_prompt":
                    continue
                # Task recipients: rides into prime, never parked.
                # Session recipients: subject to the 24h parking sweep.
                if to_kind == "session":
                    parked += await self._maybe_park(pending)
                continue

            # Keep each terminal notification on one line. Codex redraws word
            # wraps as separate terminal rows, so even a non-collapsed batch
            # cannot be verified as the exact original composer text.
            pending = pending[:1]
            scheduled = pending[0].body_kind == "schedule_prompt"
            if scheduled and (
                self.cron_service is None
                or not await self.cron_service.begin_delivery(pending[0].id)
            ):
                continue
            text = _render_nudge(pending)
            try:
                ok = await self._sessions.nudge(
                    kind=kind, target_id=target_id, project_id=resolved_project, text=text
                )
            except Exception as exc:
                if not scheduled:
                    raise
                await self.cron_service.finish_delivery(
                    pending[0].id, delivered=False, error=type(exc).__name__,
                )
                continue
            if not ok:
                if scheduled:
                    await self.cron_service.finish_delivery(pending[0].id, delivered=False)
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
            if scheduled:
                await self.cron_service.finish_delivery(pending[0].id, delivered=True)

        return {
            "success": True,
            "delivered": delivered,
            "skipped_busy": skipped_busy,
            "parked": parked,
        }

    async def check_reply_timeouts(self) -> int:
        """Sweep delivered-but-unreplied messages past ``reply_timeout``.

        For each candidate whose recipient's transcript has an assistant
        prose turn within ``reply_window_multiplier x reply_timeout`` of
        ``delivered_at``, materialise a reply row with
        ``via="transcript_tail"`` (spec §6.2 fallback). Returns the number
        of transcript-tail replies created.

        The window is load-bearing, not a throttle. Without it, every
        delivered-but-unreplied row in a long-lived session's backlog
        qualified forever, so a supervisor resuming after a restart
        fabricated a reply per backlog message from whatever it said next —
        tool calls included (2026-10-05).
        """
        if not self._config.transcript_tail_fallback:
            return 0

        now = time.time()
        cutoff = now - self._config.reply_timeout
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
        seen_threads: set[tuple] = set()
        # Cover the newest delivered request first; creation order can differ
        # when older queued feedback is injected by prime later.
        candidates.sort(key=lambda msg: (msg.delivered_at or 0, msg.created_at, msg.id), reverse=True)
        for msg in candidates:
            # Internal question handoffs use explicit question commands;
            # never fabricate a user reply from the supervisor's transcript.
            if msg.body_kind in (
                {"agent_question", "task_recovery", "wait_result", "job_result"}
                | COLLABORATION_BODY_KINDS
            ):
                continue
            # Review approvals, watchdog pings and delivery-engine notices are
            # engine chatter, not questions asked of a session: nobody is
            # waiting for a reply to them, so the tail would only ever be
            # invented.
            if _is_system_sender(msg):
                continue
            if msg.delivered_at is None or msg.delivered_at > cutoff:
                continue
            if msg.reply_to_id is not None:
                # Already a reply; not the message under sweep.
                continue
            # Once the window has closed no answering turn can still arrive,
            # so the backlog is never answered — and the transcript need not
            # be read to learn that.
            window = self._reply_window(msg.delivered_at)
            if window <= now:
                continue
            if await self._has_reply(msg):
                continue
            kind, target_id, project_id = _target_from_recipient(
                msg.to_kind, msg.to_id, msg.project_id
            )
            if kind is None:
                continue
            # Scope both ends of the conversation: unrelated workers/users
            # sharing a channel must not suppress one another's replies.
            thread_key = (
                project_id, msg.thread_id, kind, target_id, msg.from_kind, msg.from_id,
            )
            if thread_key in seen_threads:
                continue
            tail = await self._sessions.tail_assistant_turn(
                kind=kind,
                target_id=target_id,
                project_id=project_id,
                since=msg.delivered_at,
                until=window,
            )
            if not is_model_prose(tail):
                continue
            seen_threads.add(thread_key)
            reply = await self._db.create_message(
                project_id=msg.project_id,
                from_kind="session",
                from_id=msg.to_id,
                to_kind=msg.from_kind,
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

    def _reply_window(self, delivered_at: float) -> float:
        """Exclusive end of the window in which a reply to *delivered_at* may
        be recovered from the transcript."""
        return delivered_at + (
            self._config.reply_timeout * self._config.reply_window_multiplier
        )

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
            if msg.to_kind != "session" or msg.body_kind == "conversation_input":
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
        return await self._db.has_reply_covering_message(msg)

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


def _is_system_sender(msg: Message) -> bool:
    """True for engine-authored notices, which never await a reply.

    ``from_kind`` is CHECK-constrained to ``{session, user, system}``, but a
    ``user`` row can still be addressed ``system:review:<slug>`` — treat the
    id prefix as authoritative too, so a review approval or watchdog ping can
    never be answered out of a session's transcript.
    """
    return msg.from_kind == "system" or msg.from_id.startswith(_SYSTEM_SENDER_PREFIX)


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
    """Render the text injected into a live session for a pending message.

    Always one short line pointing at the durable body, task comments
    included.  A rendered comment (six header lines plus up to 8 KB) typed in
    whole is never shown verbatim by Claude: over 800 characters it collapses
    to ``[Pasted text #N +M lines]``, and taller than the composer's row window
    it shows only its last rows.  Unconfirmable, it sat unsubmitted and every
    later nudge to the worker deferred behind it (2026-10-01).
    """
    if batch[0].body_kind == "schedule_prompt":
        schedule_id = batch[0].id.split(":")[1]
        return f"Handle `aq cron show {shlex.quote(schedule_id)} --consume --json`."
    if batch[0].body_kind == "wait_result":
        # The durable message identity is also the wait pointer. Worker
        # grants include wait_get; no generic message command is needed.
        wait_id = batch[0].id.removeprefix("wait:").removesuffix(":result")
        return f"Handle `aq wait show {shlex.quote(wait_id)} --consume --json`."
    if batch[0].body_kind == "job_result":
        job_id = batch[0].id.removeprefix("job:").removesuffix(":terminal")
        return f"Handle `aq job result {shlex.quote(job_id)} --json`."
    if batch[0].body_kind in COLLABORATION_BODY_KINDS:
        return f"Handle `aq collaboration show {shlex.quote(batch[0].thread_id)} --json`."
    return f"Handle `aq message status {shlex.quote(batch[0].id)} --json`."
