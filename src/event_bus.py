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

import asyncio
import inspect
import logging
import uuid
from collections import defaultdict
from typing import Any, Callable, Iterable

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
        # Every event type this bus has actually dispatched.  Bounded by the
        # number of *distinct* type names (a few hundred at most), so it is a
        # set of strings, not a log.  Read by the ``events.registry`` doctor
        # check to compare observed emits against the registered schemas —
        # without it that check has nothing to observe and always says OK.
        self._seen_event_types: set[str] = set()

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

    @property
    def seen_event_types(self) -> set[str]:
        """Event types this bus has dispatched, as a copy (never the live set).

        Observation for ``aq doctor``'s ``events.registry`` check: a type that
        was emitted but has no registered payload schema is a real gap, and
        only a running daemon can report which types are actually in play.
        """
        return set(self._seen_event_types)

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        data.setdefault("event_id", uuid.uuid4().hex[:12])
        self._seen_event_types.add(event_type)

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
        if event_type.startswith("notify."):
            logger.info(
                "EventBus emit %s — %d handlers (direct=%d, wildcard=%d)",
                event_type,
                len(entries),
                len(self._handlers.get(event_type, [])),
                len(self._handlers.get("*", [])),
            )
        for handler, filter in entries:
            if not self._matches_filter(data, filter):
                continue
            if inspect.iscoroutinefunction(handler):
                await handler(data)
            else:
                handler(data)

    async def wait_for(
        self,
        event_types: Iterable[str],
        *,
        filter: dict[str, Any] | None = None,
        timeout: float,
    ) -> dict | None:
        """Await the first event of any of *event_types* matching *filter*, or None on timeout."""
        w = self.waiter(event_types, filter=filter)
        try:
            return await w.wait(timeout)
        finally:
            w.close()

    def waiter(
        self,
        event_types: Iterable[str],
        *,
        filter: dict[str, Any] | None = None,
    ) -> "EventWaiter":
        """Subscribe now, wait later — so no event emitted between the two is missed.

        The long-poll pattern (``task_claim``, swarm-work-model §10) needs to
        subscribe *before* checking whether the condition it cares about is
        already true, otherwise an event landing in that gap is lost and the
        caller blocks for the full timeout even though the work is ready.
        """
        return EventWaiter(self, event_types, filter=filter)

    def subscriber_count(self, event_type: str) -> int:
        return len(self._handlers.get(event_type, []))


class EventWaiter:
    """One-shot subscription created *before* a check so no event is missed."""

    def __init__(self, bus: "EventBus", event_types: Iterable[str], filter=None):
        self._fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._unsubs = [bus.subscribe(t, self._on, filter=filter) for t in event_types]

    async def _on(self, data):
        if not self._fut.done():
            self._fut.set_result(dict(data))

    async def wait(self, timeout: float) -> dict | None:
        try:
            return await asyncio.wait_for(asyncio.shield(self._fut), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []
