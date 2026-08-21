"""WebSocket endpoint for real-time notification events.

Subscribes to ``notify.*`` events on the EventBus and forwards them
as JSON to all connected WebSocket clients.  This is the real-time
transport for the dashboard SPA — the same events that drive Discord
notifications are streamed here for live UI updates.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from src.event_bus import EventBus

# Page size for the persisted-events replay (WG-4).  A disconnected client
# reconnecting with ``after_seq=N`` receives events (N, N+_REPLAY_PAGE],
# then (N+_REPLAY_PAGE, N+2*_REPLAY_PAGE], … until it catches up, at which
# point live streaming takes over.
_REPLAY_PAGE = 500

logger = logging.getLogger(__name__)

# Max queued events per client before dropping oldest
_MAX_QUEUE_SIZE = 1000

# Event-type prefixes forwarded to WebSocket clients.  ``message.*`` joins
# ``notify.*`` so chat surfaces (dashboard chat page, ``aq chat``) can render
# queued → delivered → replied transitions live — supervisor-agent §6.2/§7.
_FORWARDED_PREFIXES: tuple[str, ...] = ("notify.", "message.")


class WebSocketManager:
    """Manages WebSocket client connections and event fan-out."""

    def __init__(self, bus: EventBus, db: Any = None) -> None:
        self._bus = bus
        self._db = db
        self._clients: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
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
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # Drop oldest event to make room
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(data)
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
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        # Register *before* replaying so live events queued during the
        # replay window arrive after the paged history, preserving global
        # order without duplicates.
        self._clients[websocket] = queue
        client_id = id(websocket)
        logger.info("WebSocket client connected: %s (total: %d)", client_id, len(self._clients))

        # Parse after_seq from the query string.
        after_seq: int | None = None
        try:
            raw = websocket.query_params.get("after_seq")
            if raw is not None:
                after_seq = int(raw)
        except Exception:
            after_seq = None

        try:
            # Replay persisted events > after_seq.
            if after_seq is not None and self._db is not None:
                cursor = after_seq
                last_replayed = after_seq
                while True:
                    try:
                        rows = await self._db.get_recent_events(
                            limit=_REPLAY_PAGE, after_id=cursor
                        )
                    except Exception:
                        logger.exception("WS replay: get_recent_events failed")
                        break
                    if not rows:
                        break
                    for row in rows:
                        frame = {
                            "_event_type": row.get("event_type"),
                            "seq": row.get("id"),
                            "project_id": row.get("project_id"),
                            "task_id": row.get("task_id"),
                            "agent_id": row.get("agent_id"),
                            "payload": row.get("payload"),
                            "timestamp": row.get("timestamp"),
                        }
                        await websocket.send_json(frame)
                        last_replayed = row.get("id") or last_replayed
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
            logger.info(
                "WebSocket client disconnected: %s (remaining: %d)",
                client_id,
                len(self._clients),
            )
