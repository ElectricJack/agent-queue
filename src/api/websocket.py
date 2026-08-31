"""WebSocket endpoint for real-time notification events.

Subscribes to ``notify.*`` events on the EventBus and forwards them
as JSON to all connected WebSocket clients.  This is the real-time
transport for the dashboard SPA — the same events that drive Discord
notifications are streamed here for live UI updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from src.event_bus import EventBus

# Daemon-epoch token.  Re-generated each time this module is imported (i.e.
# each daemon start).  Clients that persist the last-seen seq alongside the
# epoch can detect a daemon restart and discard their stale seq, preventing
# the permanent-starvation bug described in the Wave-4 code review.
_EPOCH: str = str(uuid4())

# Page size for the persisted-events replay (WG-4).  A disconnected client
# reconnecting with ``after_seq=N`` receives events (N, N+_REPLAY_PAGE],
# then (N+_REPLAY_PAGE, N+2*_REPLAY_PAGE], … until it catches up, at which
# point live streaming takes over.
_REPLAY_PAGE = 500

logger = logging.getLogger(__name__)

# Max queued events per client before dropping oldest
_MAX_QUEUE_SIZE = 1000

# Event-type prefixes forwarded to WebSocket clients.  Extended (D3/D1/D2) so
# the dashboard's gates inbox, sessions pages, and task views react live to
# bus events — see docs/superpowers/plans/2026-08-21-wave4-dashboard-d1-d4.md.
_FORWARDED_PREFIXES: tuple[str, ...] = (
    "notify.",
    "agent.",
    "message.",
    "gate.",
    "session.",
    "task.",
)


_QUESTION_EVENTS = frozenset({"agent.question", "agent.question.updated"})
_QUESTION_INVALIDATION_FIELDS = ("id", "agent_id", "session_id", "task_id", "project_id", "state")


def _question_invalidation(data, scope):
    """Project only authorized, non-content fields for live and replay WS.

    The internal EventBus keeps the full question for Discord/service routing;
    the browser only needs identifiers to invalidate its scoped query cache.
    """
    nested = data.get("payload")
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except (ValueError, TypeError):
            return None
    nested = nested if isinstance(nested, dict) else {}
    fields = {key: data.get(key) if data.get(key) is not None else nested.get(key)
              for key in _QUESTION_INVALIDATION_FIELDS}
    if not all(isinstance(fields[key], str) and fields[key] for key in ("id", "session_id", "project_id")):
        return None
    kind = getattr(scope, "kind", None)
    if kind == "local":
        allowed = True
    elif kind == "session":
        project = scope.project_id
        allowed = project is None or project == fields["project_id"]
        if not scope.elevated:
            allowed = (allowed and scope.session_id == fields["session_id"]
                       and (scope.task_id is None or scope.task_id == fields["task_id"]))
    else:
        allowed = False
    if not allowed:
        return None
    result = {"_event_type": data["_event_type"], **fields}
    if "seq" in data:
        result["seq"] = data["seq"]
    return result


class WebSocketManager:
    """Manages WebSocket client connections and event fan-out."""

    def __init__(self, bus: EventBus, db: Any = None) -> None:
        self._bus = bus
        self._db = db
        self._clients: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        # aq-surface Phase S2: per-connection RequestScope recorded during
        # handshake.  TODO(S3): use self._client_scope[ws] to filter
        # task-scoped events for session-bound clients.
        from src.api.auth import RequestScope

        self._client_scope: dict[WebSocket, RequestScope] = {}
        self._unsub: Any = None

    def start(self) -> None:
        """Subscribe to all bus events and filter for notify.*."""
        logger.info("WebSocketManager subscribing to bus %s (id=%d)", self._bus, id(self._bus))
        logger.info("Bus handlers before subscribe: %s", dict(self._bus._handlers))
        self._unsub = self._bus.subscribe("*", self._on_event)
        logger.info("Bus handlers after subscribe: %s", dict(self._bus._handlers))

    def shutdown(self) -> None:
        """Unsubscribe from the bus."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _on_event(self, data: dict[str, Any]) -> None:
        """Fan out notify.* events to all connected clients."""
        event_type = data.get("_event_type", "")
        logger.debug("WS _on_event received: %s (clients=%d)", event_type, len(self._clients))
        if not event_type.startswith(_FORWARDED_PREFIXES):
            return
        logger.info("WS forwarding event: %s to %d clients", event_type, len(self._clients))

        for ws, queue in list(self._clients.items()):
            event = data
            if event_type in _QUESTION_EVENTS:
                event = _question_invalidation(data, self._client_scope.get(ws))
                if event is None:
                    continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest event to make room
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    async def handle(self, websocket: WebSocket) -> None:
        """Accept a WebSocket connection and stream events until disconnect.

        Supports ``?after_seq=N`` (WG-4): reconnecting clients receive the
        persisted events with ``id > N`` (paged, ascending) *before* live
        streaming resumes.  The replay carries the DB row id as ``seq`` on
        each frame; live frames set ``seq: null`` because they are not
        necessarily backed by a DB row.
        """
        # aq-surface Phase S2: handshake-level auth.  Same policy as REST —
        # absent header → LOCAL_SCOPE (subject to require_session_token);
        # invalid → close(4401); valid → session RequestScope.
        from src.api import dependencies as _deps
        from src.api.auth import LOCAL_SCOPE

        header = websocket.headers.get("Authorization", "")
        scope = LOCAL_SCOPE
        store = _deps._token_store
        require = _deps._require_session_token
        if header.startswith("Bearer "):
            token = header[len("Bearer ") :].strip()
            if store is not None:
                resolved = await store.validate(token)
                if resolved is None:
                    await websocket.close(code=4401, reason="invalid or expired token")
                    return
                scope = resolved
        else:
            if require:
                await websocket.close(code=4401, reason="session token required")
                return

        await websocket.accept()
        self._client_scope[websocket] = scope
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        # Register *before* replaying so live events queued during the
        # replay window arrive after the paged history, preserving global
        # order without duplicates.
        self._clients[websocket] = queue
        client_id = id(websocket)
        logger.info("WebSocket client connected: %s (total: %d)", client_id, len(self._clients))

        # Send epoch frame immediately so the client can detect daemon restarts
        # and discard a stale after_seq (which would otherwise cause permanent
        # dedup starvation after a fresh-DB restart).  This frame is delivered
        # before replay begins; the client resets its dedup cursor when the
        # epoch differs from the one it stored.
        await websocket.send_json({"type": "hello", "epoch": _EPOCH})

        # Parse after_seq from the query string.
        after_seq: int | None = None
        try:
            raw = websocket.query_params.get("after_seq")
            if raw is not None:
                after_seq = int(raw)
        except Exception:
            after_seq = None

        # Hoisted so the live loop's dedup check can reference it even
        # when no replay ran.  ``after_seq is None`` disables dedup below.
        last_replayed: int = after_seq if after_seq is not None else 0

        try:
            # Replay persisted events > after_seq.
            if after_seq is not None and self._db is not None:
                cursor = after_seq
                while True:
                    try:
                        rows = await self._db.get_recent_events(limit=_REPLAY_PAGE, after_id=cursor)
                    except Exception:
                        logger.exception("WS replay: get_recent_events failed")
                        break
                    if not rows:
                        break
                    for row in rows:
                        # Advance the watermark for EVERY row so live-mode
                        # dedup covers filtered rows too — otherwise a
                        # burst of non-forwarded rows during the race
                        # window would leave ``last_replayed`` stale.
                        row_id = row.get("id")
                        if row_id is not None:
                            last_replayed = row_id
                        event_type = row.get("event_type") or ""
                        if not event_type.startswith(_FORWARDED_PREFIXES):
                            # Same filter as live mode — otherwise a
                            # reconnect would flood the client with
                            # internal ``task.*`` / ``gate.*`` traffic
                            # that the live path never delivers.
                            continue
                        frame = {
                            "_event_type": event_type,
                            "seq": row_id,
                            "project_id": row.get("project_id"),
                            "task_id": row.get("task_id"),
                            "agent_id": row.get("agent_id"),
                            "payload": row.get("payload"),
                            "timestamp": row.get("timestamp"),
                        }
                        if event_type in _QUESTION_EVENTS:
                            frame = _question_invalidation(frame, scope)
                            if frame is None:
                                continue
                        await websocket.send_json(frame)
                    if len(rows) < _REPLAY_PAGE:
                        break
                    cursor = rows[-1].get("id") or cursor

                # Drop any live frames that were queued while replay ran
                # and would duplicate what we already delivered.
                remaining: list[dict[str, Any]] = []
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    seq = item.get("seq")
                    if isinstance(seq, int) and seq <= last_replayed:
                        continue
                    remaining.append(item)
                for item in remaining:
                    try:
                        queue.put_nowait(item)
                    except asyncio.QueueFull:
                        pass

            while True:
                event = await queue.get()
                # Live frames carry seq=None unless the emitter threaded
                # the DB id into the payload (log_event returns the id).
                seq = event.get("seq") if "seq" in event else None
                # Dedup: a live frame whose seq is <= the last replayed
                # row id would duplicate what the client already saw
                # during replay.  Only applies when the emitter honestly
                # threaded a persisted row id; ``seq=None`` bypasses.
                if after_seq is not None and isinstance(seq, int) and seq <= last_replayed:
                    continue
                if "seq" not in event:
                    event = {**event, "seq": None}
                logger.info(
                    "WS sending event to client %s: %s", client_id, event.get("_event_type")
                )
                await websocket.send_json(event)
                logger.info("WS sent successfully to client %s", client_id)
        except WebSocketDisconnect:
            logger.info("WS client %s disconnected normally", client_id)
        except Exception as e:
            logger.error("WebSocket client %s error: %s", client_id, e, exc_info=True)
        finally:
            self._clients.pop(websocket, None)
            self._client_scope.pop(websocket, None)
            logger.info(
                "WebSocket client disconnected: %s (remaining: %d)",
                client_id,
                len(self._clients),
            )
