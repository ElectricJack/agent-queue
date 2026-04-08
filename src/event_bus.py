"""Lightweight async pub/sub event bus for decoupling system components.

The EventBus is the primary mechanism for loose coupling between the
orchestrator, hook engine, and notification subsystem. Components subscribe
to named event types (e.g., "task_completed", "agent_failed") and receive
async callbacks when those events are emitted.

A special wildcard subscription ("*") receives every event regardless of type.
The hook engine uses this to evaluate all events against its trigger
conditions without needing individual subscriptions per event type.

Payload validation (Phase 0.2.3):
    When ``validate_events`` is enabled (the default), every ``emit()`` call
    runs the payload through ``validate_event()`` from :mod:`event_schemas`.
    In **dev** mode (``env="dev"``), validation failures raise
    :class:`EventValidationError`.  In **prod** mode they are logged as
    warnings but the event is still delivered.  Set ``validate_events=False``
    to skip validation entirely (e.g., for hot-path benchmarks).

See specs/event-bus.md for the full specification.
"""

from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from typing import Any, Callable

from src.event_schemas import validate_event

logger = logging.getLogger(__name__)


class EventValidationError(Exception):
    """Raised in dev mode when an event payload fails schema validation."""


class EventBus:
    """Async event dispatcher with named channels and wildcard support.

    Handlers are invoked sequentially in subscription order. Both sync and
    async handlers are supported — sync handlers are called directly while
    async handlers are awaited.

    Subscriptions may include an optional payload filter (dict[str, Any]).
    When a filter is provided, the handler only fires if every key/value pair
    in the filter matches the corresponding field in the event data.

    Args:
        env: Environment name (``"dev"``, ``"production"``, etc.).
            In ``"dev"`` mode, validation errors raise
            :class:`EventValidationError`.  In all other modes they
            are logged as warnings.  Defaults to ``"production"``.
        validate_events: Master switch for event payload validation.
            Set to ``False`` to disable validation entirely.
            Defaults to ``True``.
    """

    # Each entry is (handler, filter_dict | None)
    _Subscription = tuple[Callable, dict[str, Any] | None]

    def __init__(
        self,
        *,
        env: str = "production",
        validate_events: bool = True,
    ):
        self._handlers: dict[str, list[EventBus._Subscription]] = defaultdict(list)
        self._env = env
        self._validate_events = validate_events

    def subscribe(
        self,
        event_type: str,
        handler: Callable,
        filter: dict[str, Any] | None = None,
    ) -> Callable[[], None]:
        """Subscribe a handler and return an unsubscribe callable.

        Args:
            event_type: The event name to listen for, or ``"*"`` for all events.
            handler: Sync or async callable invoked with the event data dict.
            filter: Optional dict of key/value pairs that must all match
                fields in the event payload for the handler to be invoked.
                ``None`` (the default) means the handler receives every event
                of the given type, preserving backward compatibility.
        """
        entry: EventBus._Subscription = (handler, filter)
        self._handlers[event_type].append(entry)

        def unsubscribe() -> None:
            try:
                self._handlers[event_type].remove(entry)
            except ValueError:
                pass  # already removed

        return unsubscribe

    @staticmethod
    def _matches_filter(data: dict[str, Any], filter: dict[str, Any] | None) -> bool:
        """Return True if *data* satisfies all conditions in *filter*."""
        if filter is None:
            return True
        return all(data.get(k) == v for k, v in filter.items())

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}

        # --- Payload validation (Phase 0.2.3) ---
        if self._validate_events:
            errors = validate_event(event_type, data)
            if errors:
                msg = "; ".join(errors)
                if self._env == "dev":
                    raise EventValidationError(msg)
                else:
                    logger.warning("Event validation warnings: %s", msg)

        data["_event_type"] = event_type
        entries = list(self._handlers.get(event_type, []))
        entries.extend(self._handlers.get("*", []))
        for handler, filter in entries:
            if not self._matches_filter(data, filter):
                continue
            if inspect.iscoroutinefunction(handler):
                await handler(data)
            else:
                handler(data)
