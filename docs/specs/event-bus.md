# Event Bus Spec

## 1. Overview

`EventBus` (`src/event_bus.py`) is a minimal in-process async pub/sub mechanism. It decouples components by allowing any part of the system to emit named events without knowing which handlers will receive them. All orchestration remains deterministic — the bus carries no LLM or scheduling logic.

> **Future evolution:** See [[design/playbooks]] for EventBus payload filtering and event schema registry.

## Source Files
- `src/event_bus.py`

## 2. Subscribing

```python
bus.subscribe(event_type: str, handler: Callable) -> None
```

Registers `handler` to be called whenever an event of `event_type` is emitted. Multiple handlers may be registered for the same event type; they are stored in insertion order and all will be called on each emit. There is no unsubscribe mechanism — subscriptions are permanent for the lifetime of the bus instance.

## 3. Emitting

```python
await bus.emit(event_type: str, data: dict | None = None) -> None
```

Fans out to every handler registered under `event_type`, followed by every handler registered under `"*"` (wildcard). Before dispatch, the key `_event_type` is injected into `data` so handlers always know which event triggered them. If `data` is omitted, `None`, or an empty dict `{}` it defaults to a new `{}` (via `data or {}`). **Note:** `_event_type` is written directly into the caller's dict object — the original dict is mutated as a side effect.

Dispatch order:
1. Specific handlers (registered under `event_type`), in insertion order.
2. Wildcard handlers (registered under `"*"`), in insertion order.

## 4. Wildcard Support

Subscribing with the literal string `"*"` as the event type causes the handler to receive every event emitted on the bus, regardless of its type. Wildcard handlers run after all specific handlers for a given emit call.

```python
bus.subscribe("*", my_catch_all_handler)
```

## 5. Handler Types

Both synchronous and asynchronous handlers are supported. The bus inspects each handler with `inspect.iscoroutinefunction`:

- Async handlers (`async def`) are awaited directly.
- Sync handlers (plain `def`) are called normally (blocking the event loop for the duration of the call).

There is no thread-pool offloading for sync handlers, so sync handlers should be fast to avoid stalling the asyncio loop.

## 6. Error Isolation

The current implementation provides no error isolation. If a handler raises an exception, it propagates out of `emit` immediately and the remaining handlers in the fan-out list are not called. Callers of `emit` are responsible for catching exceptions if resilience is required.

## 7. Implementation Notes

- Handlers are dispatched **sequentially**, not concurrently. Each handler is awaited (or called) to completion before the next one starts. There is no `asyncio.gather` or `asyncio.create_task` usage — `asyncio` is imported in the module but not currently used.
- The handler snapshot (`list(self._handlers.get(...))`) is taken at the start of `emit`, so handlers added during dispatch are not included in the current fan-out.
