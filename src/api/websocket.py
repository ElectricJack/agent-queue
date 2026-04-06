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

logger = logging.getLogger(__name__)

# Max queued events per client before dropping oldest
_MAX_QUEUE_SIZE = 1000


class WebSocketManager:
    """Manages WebSocket client connections and event fan-out."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
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
        if not event_type.startswith("notify."):
            return
        logger.info("WS forwarding notify event: %s to %d clients", event_type, len(self._clients))

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
        """Accept a WebSocket connection and stream events until disconnect."""
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._clients[websocket] = queue
        client_id = id(websocket)
        logger.info("WebSocket client connected: %s (total: %d)", client_id, len(self._clients))

        try:
            while True:
                event = await queue.get()
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
