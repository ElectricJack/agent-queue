import asyncio
import pytest
from src.event_bus import EventBus


class TestEventBus:
    async def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []
        bus.subscribe("task_completed", lambda data: received.append(data))
        await bus.emit("task_completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert received[0]["task_id"] == "t-1"

    async def test_multiple_subscribers(self):
        bus = EventBus()
        received_a = []
        received_b = []
        bus.subscribe("test", lambda d: received_a.append(d))
        bus.subscribe("test", lambda d: received_b.append(d))
        await bus.emit("test", {"x": 1})
        assert len(received_a) == 1
        assert len(received_b) == 1

    async def test_no_cross_talk(self):
        bus = EventBus()
        received = []
        bus.subscribe("event_a", lambda d: received.append(d))
        await bus.emit("event_b", {"x": 1})
        assert len(received) == 0

    async def test_wildcard_subscriber(self):
        bus = EventBus()
        received = []
        bus.subscribe("*", lambda d: received.append(d))
        await bus.emit("anything", {"x": 1})
        await bus.emit("something_else", {"y": 2})
        assert len(received) == 2

    async def test_async_handler(self):
        bus = EventBus()
        received = []

        async def handler(data):
            await asyncio.sleep(0)
            received.append(data)

        bus.subscribe("test", handler)
        await bus.emit("test", {"x": 1})
        assert len(received) == 1

    # --- filter parameter tests ---

    async def test_filter_matches(self):
        """Handler fires when all filter fields match the payload."""
        bus = EventBus()
        received = []
        bus.subscribe("task.done", lambda d: received.append(d), filter={"project": "acme"})
        await bus.emit("task.done", {"project": "acme", "task_id": "t-1"})
        assert len(received) == 1
        assert received[0]["task_id"] == "t-1"

    async def test_filter_no_match(self):
        """Handler does NOT fire when a filter field doesn't match."""
        bus = EventBus()
        received = []
        bus.subscribe("task.done", lambda d: received.append(d), filter={"project": "acme"})
        await bus.emit("task.done", {"project": "other", "task_id": "t-2"})
        assert len(received) == 0

    async def test_filter_multiple_conditions(self):
        """All filter conditions must match (AND semantics)."""
        bus = EventBus()
        received = []
        bus.subscribe(
            "run.completed",
            lambda d: received.append(d),
            filter={"playbook_id": "cq-gate", "status": "success"},
        )
        # Only one field matches → no delivery
        await bus.emit("run.completed", {"playbook_id": "cq-gate", "status": "failure"})
        assert len(received) == 0
        # Both fields match → delivered
        await bus.emit("run.completed", {"playbook_id": "cq-gate", "status": "success"})
        assert len(received) == 1

    async def test_filter_missing_key_in_payload(self):
        """Filter key absent from payload → no match."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={"key": "val"})
        await bus.emit("evt", {"other": "stuff"})
        assert len(received) == 0

    async def test_filter_none_is_unfiltered(self):
        """Explicitly passing filter=None behaves like no filter (backward compat)."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter=None)
        await bus.emit("evt", {"x": 1})
        assert len(received) == 1

    async def test_filtered_and_unfiltered_coexist(self):
        """Filtered and unfiltered handlers on the same event type work independently."""
        bus = EventBus()
        all_events = []
        filtered_events = []
        bus.subscribe("evt", lambda d: all_events.append(d))
        bus.subscribe("evt", lambda d: filtered_events.append(d), filter={"src": "a"})

        await bus.emit("evt", {"src": "a"})
        await bus.emit("evt", {"src": "b"})

        assert len(all_events) == 2  # receives both
        assert len(filtered_events) == 1  # only the matching one
        assert filtered_events[0]["src"] == "a"

    async def test_filter_with_wildcard_subscriber(self):
        """Filters also work on wildcard ('*') subscriptions."""
        bus = EventBus()
        received = []
        bus.subscribe("*", lambda d: received.append(d), filter={"level": "critical"})
        await bus.emit("alert", {"level": "critical", "msg": "disk full"})
        await bus.emit("alert", {"level": "info", "msg": "ok"})
        assert len(received) == 1
        assert received[0]["msg"] == "disk full"

    async def test_filter_unsubscribe(self):
        """Unsubscribe callable works correctly for filtered subscriptions."""
        bus = EventBus()
        received = []
        unsub = bus.subscribe("evt", lambda d: received.append(d), filter={"x": 1})
        await bus.emit("evt", {"x": 1})
        assert len(received) == 1
        unsub()
        await bus.emit("evt", {"x": 1})
        assert len(received) == 1  # no new event after unsubscribe

    async def test_filter_with_async_handler(self):
        """Filtered subscriptions work with async handlers."""
        bus = EventBus()
        received = []

        async def handler(data):
            await asyncio.sleep(0)
            received.append(data)

        bus.subscribe("evt", handler, filter={"ok": True})
        await bus.emit("evt", {"ok": True})
        await bus.emit("evt", {"ok": False})
        assert len(received) == 1
