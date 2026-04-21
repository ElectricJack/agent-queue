"""EventsMixin — event emission methods for PlaybookRunner.

Extracted from :mod:`src.playbooks.runner` to reduce file size.
These methods emit lifecycle events on the EventBus for downstream
subscribers (Discord notifications, dashboards, audit logs, etc.).

The mixin expects the following attributes on ``self``:
- ``event_bus`` — optional :class:`EventBus`
- ``event`` — trigger event dict (for ``project_id``)
- ``run_id`` — current run ID
- ``_playbook_id`` — playbook identifier
- ``tokens_used`` — cumulative token count
- ``messages`` — conversation history list
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.event_bus import EventBus

logger = logging.getLogger(__name__)


class EventsMixin:
    """Mixin providing event emission methods for the PlaybookRunner."""

    # Attributes expected from PlaybookRunner (for type checking purposes)
    event_bus: EventBus | None
    event: dict
    graph: dict
    run_id: str
    _playbook_id: str
    tokens_used: int
    messages: list[dict]
    node_trace: list

    async def _emit_bus_event(self, event_type: str, payload: dict) -> None:
        """Emit an event on the EventBus if one is configured.

        Silently ignores errors to avoid breaking the runner if a subscriber
        misbehaves.  The caller is responsible for building the payload —
        this helper only adds ``project_id`` from the trigger event when
        present (for scope-based filtering by downstream playbooks).
        """
        if self.event_bus is None:
            return
        # Inject project_id from the trigger event if available
        project_id = self.event.get("project_id")
        if project_id and "project_id" not in payload:
            payload["project_id"] = project_id
        try:
            await self.event_bus.emit(event_type, payload)
        except Exception:
            logger.warning(
                "Failed to emit %s for playbook run %s",
                event_type,
                self.run_id,
                exc_info=True,
            )

    async def _emit_started_event(self) -> None:
        """Emit ``playbook.run.started`` and ``notify.playbook_run_started``.

        Fires once when the runner begins executing the entry node.
        The ``notify.*`` variant drives Discord/Telegram notifications; the
        raw ``playbook.run.started`` is for EventBus composition hooks.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
        }
        await self._emit_bus_event("playbook.run.started", payload)

        # Typed notification for Discord/Telegram transports
        from src.notifications.events import PlaybookRunStartedEvent

        scope = self.graph.get("scope", "system") if hasattr(self, "graph") else "system"
        notify_event = PlaybookRunStartedEvent(
            playbook_id=self._playbook_id,
            run_id=self.run_id,
            trigger_event_type=self.event.get("_event_type", self.event.get("type", "")),
            scope=scope,
            project_id=self.event.get("project_id"),
        )
        await self._emit_bus_event(
            "notify.playbook_run_started", notify_event.model_dump(mode="json")
        )

    async def _emit_completed_event(
        self,
        *,
        final_context: str | None = None,
        started_at: float | None = None,
    ) -> None:
        """Emit ``playbook.run.completed`` and ``notify.playbook_run_completed``.

        See ``docs/specs/design/playbooks.md`` Section 7 — Event System.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
        }
        if final_context is not None:
            payload["final_context"] = final_context
        if started_at is not None:
            payload["duration_seconds"] = round(time.time() - started_at, 2)
        payload["tokens_used"] = self.tokens_used
        await self._emit_bus_event("playbook.run.completed", payload)

        # Typed notification for Discord/Telegram transports
        from src.notifications.events import PlaybookRunCompletedEvent

        notify_event = PlaybookRunCompletedEvent(
            playbook_id=self._playbook_id,
            run_id=self.run_id,
            final_context=final_context,
            tokens_used=self.tokens_used,
            duration_seconds=payload.get("duration_seconds", 0.0),
            node_count=len(self.node_trace) if hasattr(self, "node_trace") else 0,
            project_id=self.event.get("project_id"),
        )
        await self._emit_bus_event(
            "notify.playbook_run_completed", notify_event.model_dump(mode="json")
        )

    async def _emit_failed_event(
        self,
        *,
        failed_at_node: str | None = None,
        error: str | None = None,
        started_at: float | None = None,
    ) -> None:
        """Emit ``playbook.run.failed`` on the EventBus.

        See ``docs/specs/design/playbooks.md`` Section 7 — Event System.
        """
        # Determine the node where failure occurred — fall back to the last
        # node in the trace, or "<unknown>" if the failure happened before
        # any node was reached (e.g. missing entry node, budget pre-check).
        if failed_at_node is None and self.node_trace:
            failed_at_node = self.node_trace[-1].node_id
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "failed_at_node": failed_at_node or "<unknown>",
        }
        if error is not None:
            payload["error"] = error
        if started_at is not None:
            payload["duration_seconds"] = round(time.time() - started_at, 2)
        payload["tokens_used"] = self.tokens_used
        await self._emit_bus_event("playbook.run.failed", payload)

        # Typed notification for Discord/Telegram transports
        from src.notifications.events import PlaybookRunFailedEvent

        notify_event = PlaybookRunFailedEvent(
            playbook_id=self._playbook_id,
            run_id=self.run_id,
            failed_at_node=failed_at_node or "<unknown>",
            error=error or "",
            tokens_used=self.tokens_used,
            duration_seconds=payload.get("duration_seconds", 0.0),
            project_id=self.event.get("project_id"),
        )
        await self._emit_bus_event(
            "notify.playbook_run_failed", notify_event.model_dump(mode="json")
        )

    async def _emit_paused_event(
        self,
        *,
        node_id: str,
        started_at: float | None = None,
        paused_at: float | None = None,
    ) -> None:
        """Emit ``playbook.run.paused`` on the EventBus.

        Fired when execution pauses at a ``wait_for_human`` node (spec §9).
        Notification subscribers (Discord, dashboard) use this to surface
        the review request.  The payload includes the node ID, conversation
        context summary, and timing information so the notification can be
        informative without requiring a DB lookup.

        Also emits a ``notify.playbook_run_paused`` event (roadmap 5.4.2)
        so that notification transports (Discord, Telegram) can deliver a
        human-readable context summary to the reviewer.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "node_id": node_id,
        }
        if started_at is not None:
            payload["running_seconds"] = round((paused_at or time.time()) - started_at, 2)
        if paused_at is not None:
            payload["paused_at"] = paused_at
        payload["tokens_used"] = self.tokens_used
        # Include the last assistant response as context for the reviewer
        last_response = ""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    last_response = content[:2000]  # cap for event payload
                    break
        if last_response:
            payload["last_response"] = last_response
        await self._emit_bus_event("playbook.run.paused", payload)

        # Emit typed notification event for Discord/Telegram (roadmap 5.4.2)
        await self._emit_paused_notification(payload, last_response)

    async def _emit_paused_notification(
        self,
        raw_payload: dict[str, Any],
        last_response: str,
    ) -> None:
        """Emit ``notify.playbook_run_paused`` for notification transports.

        Converts the raw ``playbook.run.paused`` payload into a typed
        ``PlaybookRunPausedEvent`` and emits it on the EventBus so that
        Discord, Telegram, and other transports can render a rich
        human-review notification with context summary (roadmap 5.4.2).
        """
        from src.notifications.events import PlaybookRunPausedEvent

        event = PlaybookRunPausedEvent(
            playbook_id=raw_payload.get("playbook_id", ""),
            run_id=raw_payload.get("run_id", ""),
            node_id=raw_payload.get("node_id", ""),
            last_response=last_response,
            running_seconds=raw_payload.get("running_seconds", 0.0),
            tokens_used=raw_payload.get("tokens_used", 0),
            paused_at=raw_payload.get("paused_at", 0.0),
            project_id=raw_payload.get("project_id"),
        )
        await self._emit_bus_event("notify.playbook_run_paused", event.model_dump(mode="json"))

    async def _emit_resumed_event(
        self,
        *,
        node_id: str,
        human_input: str,
    ) -> None:
        """Emit ``playbook.run.resumed`` on the EventBus.

        Fired when a paused run is successfully resumed after human review
        (spec §9).  Downstream subscribers can use this for audit logging,
        dashboards, or chaining further automation.

        .. note::

           Prior to roadmap 5.4.3 this method emitted ``human.review.completed``.
           That event is now the **trigger** (fired by Dashboard/Discord to
           initiate the resume); ``playbook.run.resumed`` is the **notification**
           confirming the resume occurred.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "node_id": node_id,
            "decision": human_input[:2000],  # cap for event payload
        }
        await self._emit_bus_event("playbook.run.resumed", payload)

        # Also emit a typed notification event for Discord/Telegram transports
        await self._emit_resumed_notification(payload, human_input)

    async def _emit_resumed_notification(
        self,
        raw_payload: dict[str, Any],
        human_input: str,
    ) -> None:
        """Emit ``notify.playbook_run_resumed`` for notification transports.

        Converts the raw ``playbook.run.resumed`` payload into a typed
        ``PlaybookRunResumedEvent`` and emits it on the EventBus so that
        Discord, Telegram, and other transports can render a rich
        notification confirming the run was resumed (roadmap 5.4.3).
        """
        from src.notifications.events import PlaybookRunResumedEvent

        event = PlaybookRunResumedEvent(
            playbook_id=raw_payload.get("playbook_id", ""),
            run_id=raw_payload.get("run_id", ""),
            node_id=raw_payload.get("node_id", ""),
            decision=human_input[:2000],
            project_id=raw_payload.get("project_id"),
        )
        await self._emit_bus_event("notify.playbook_run_resumed", event.model_dump(mode="json"))

    async def _emit_timed_out_event(
        self,
        *,
        node_id: str,
        paused_at: float,
        timeout_seconds: int,
        transitioned_to: str | None = None,
    ) -> None:
        """Emit ``playbook.run.timed_out`` on the EventBus.

        Fired when a paused run exceeds its configured pause timeout (spec §9,
        roadmap 5.4.4).  If the run transitions to a timeout node, the node ID
        is included so downstream subscribers know the run continues rather
        than simply failing.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "node_id": node_id,
            "paused_at": paused_at,
            "timeout_seconds": timeout_seconds,
            "waited_seconds": round(time.time() - paused_at, 2),
        }
        if transitioned_to is not None:
            payload["transitioned_to"] = transitioned_to
        payload["tokens_used"] = self.tokens_used
        await self._emit_bus_event("playbook.run.timed_out", payload)

        # Emit typed notification event so Discord/Telegram route the timeout
        # to the same channel that received the original pause notification
        # (roadmap 5.4.7 test case (f)).
        await self._emit_timed_out_notification(payload)

    async def _emit_timed_out_notification(
        self,
        raw_payload: dict[str, Any],
    ) -> None:
        """Emit ``notify.playbook_run_timed_out`` for notification transports.

        Mirrors :meth:`_emit_paused_notification` — converts the raw
        ``playbook.run.timed_out`` payload into a typed
        ``PlaybookRunTimedOutEvent`` and emits it on the EventBus.  Because
        the runner's ``_emit_bus_event`` automatically injects ``project_id``
        from the trigger event, the notification routes to the same channel
        that received the original pause notification (roadmap 5.4.7 case f).
        """
        from src.notifications.events import PlaybookRunTimedOutEvent

        event = PlaybookRunTimedOutEvent(
            playbook_id=raw_payload.get("playbook_id", ""),
            run_id=raw_payload.get("run_id", ""),
            node_id=raw_payload.get("node_id", ""),
            timeout_seconds=raw_payload.get("timeout_seconds", 0),
            waited_seconds=raw_payload.get("waited_seconds", 0.0),
            tokens_used=raw_payload.get("tokens_used", 0),
            transitioned_to=raw_payload.get("transitioned_to"),
            project_id=raw_payload.get("project_id"),
        )
        await self._emit_bus_event("notify.playbook_run_timed_out", event.model_dump(mode="json"))
