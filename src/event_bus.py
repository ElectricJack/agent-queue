"""Lightweight async pub/sub event bus for decoupling system components.

The EventBus is the primary mechanism for loose coupling between the
orchestrator, hook engine, and notification subsystem. Components subscribe
to named event types (e.g., "task_completed", "agent_failed") and receive
async callbacks when those events are emitted.

A special wildcard subscription ("*") receives every event regardless of type.
The hook engine uses this to evaluate all events against its trigger
conditions without needing individual subscriptions per event type.

See specs/event-bus.md for the full specification.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from typing import Any, Callable


class EventBus:
    """Async event dispatcher with named channels and wildcard support.

    Handlers are invoked sequentially in subscription order. Both sync and
    async handlers are supported — sync handlers are called directly while
    async handlers are awaited.
    """

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable) -> None:
        self._handlers[event_type].append(handler)

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        data["_event_type"] = event_type
        handlers = list(self._handlers.get(event_type, []))
        handlers.extend(self._handlers.get("*", []))
        for handler in handlers:
            if inspect.iscoroutinefunction(handler):
                await handler(data)
            else:
                handler(data)
