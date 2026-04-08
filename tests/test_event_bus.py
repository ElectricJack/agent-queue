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


class TestEventBusBackwardCompatibility:
    """Backward-compatibility guarantees for the filter parameter.

    These tests verify that the addition of ``filter`` to
    ``EventBus.subscribe()`` does not change any pre-existing behaviour.
    Every subscription created *without* a filter (or with ``filter=None``)
    must continue to work exactly as before the feature was introduced.
    """

    # --- (a) subscriber without filter receives ALL events of that type ---

    async def test_no_filter_receives_all_events(self):
        """A subscriber registered without a filter receives every event of its type."""
        bus = EventBus()
        received = []
        bus.subscribe("task.done", lambda d: received.append(d))

        await bus.emit("task.done", {"project": "alpha"})
        await bus.emit("task.done", {"project": "beta"})
        await bus.emit("task.done", {"project": "gamma", "status": "ok"})

        assert len(received) == 3
        assert [d["project"] for d in received] == ["alpha", "beta", "gamma"]

    async def test_no_filter_receives_events_with_varying_payloads(self):
        """Unfiltered subscriber receives events regardless of payload shape."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d))

        await bus.emit("evt", {"a": 1})
        await bus.emit("evt", {"b": 2, "c": 3})
        await bus.emit("evt", {})
        await bus.emit("evt")  # data=None → defaults to {}

        assert len(received) == 4

    async def test_no_filter_wildcard_receives_all_event_types(self):
        """Wildcard subscriber without filter receives every event across all types."""
        bus = EventBus()
        received = []
        bus.subscribe("*", lambda d: received.append(d))

        await bus.emit("type_a", {"x": 1})
        await bus.emit("type_b", {"y": 2})
        await bus.emit("type_c", {"z": 3})

        assert len(received) == 3
        assert received[0]["_event_type"] == "type_a"
        assert received[1]["_event_type"] == "type_b"
        assert received[2]["_event_type"] == "type_c"

    # --- (c) filter=None behaves identically to no filter arg ---

    async def test_filter_none_identical_to_omitted(self):
        """filter=None and omitting the filter arg produce identical behaviour."""
        bus = EventBus()
        without_arg = []
        with_none = []

        bus.subscribe("evt", lambda d: without_arg.append(d))
        bus.subscribe("evt", lambda d: with_none.append(d), filter=None)

        payloads = [
            {"project": "a"},
            {"project": "b", "status": "ok"},
            {},
        ]
        for p in payloads:
            await bus.emit("evt", dict(p))  # copy to avoid shared mutation

        assert len(without_arg) == len(with_none) == 3
        # Both receive the exact same events (same data dicts)
        for a, b in zip(without_arg, with_none):
            assert a is b  # same dict object dispatched to both

    async def test_filter_none_wildcard_identical_to_omitted(self):
        """filter=None on a wildcard subscription is identical to omitting filter."""
        bus = EventBus()
        without_arg = []
        with_none = []

        bus.subscribe("*", lambda d: without_arg.append(d))
        bus.subscribe("*", lambda d: with_none.append(d), filter=None)

        await bus.emit("foo", {"x": 1})
        await bus.emit("bar", {"y": 2})

        assert len(without_arg) == len(with_none) == 2
        for a, b in zip(without_arg, with_none):
            assert a is b

    # --- (d) empty filter {} receives all events ---

    async def test_empty_filter_receives_all_events(self):
        """An empty filter dict {} has no conditions to fail, so all events match."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={})

        await bus.emit("evt", {"project": "a"})
        await bus.emit("evt", {"status": "ok"})
        await bus.emit("evt", {})

        assert len(received) == 3

    async def test_empty_filter_matches_any_payload(self):
        """Empty filter matches events with any combination of payload fields."""
        bus = EventBus()
        received = []
        bus.subscribe("run", lambda d: received.append(d), filter={})

        await bus.emit("run", {"playbook_id": "cq-gate", "status": "success"})
        await bus.emit("run", {"completely": "different", "keys": True})
        await bus.emit("run")  # no explicit payload

        assert len(received) == 3

    async def test_empty_filter_on_wildcard(self):
        """Empty filter on wildcard subscription receives all event types."""
        bus = EventBus()
        received = []
        bus.subscribe("*", lambda d: received.append(d), filter={})

        await bus.emit("type_a", {"x": 1})
        await bus.emit("type_b", {"y": 2})

        assert len(received) == 2

    # --- (e) unfiltered subscriber receives events that also match another's filter ---

    async def test_unfiltered_receives_events_matching_others_filter(self):
        """Unfiltered subscriber sees events even when they match another subscriber's filter."""
        bus = EventBus()
        unfiltered = []
        filtered = []

        bus.subscribe("task.done", lambda d: unfiltered.append(d))
        bus.subscribe("task.done", lambda d: filtered.append(d), filter={"project": "acme"})

        # Event matches the filter → both receive it
        await bus.emit("task.done", {"project": "acme", "task_id": "t-1"})
        # Event doesn't match the filter → only unfiltered receives it
        await bus.emit("task.done", {"project": "other", "task_id": "t-2"})

        assert len(unfiltered) == 2
        assert len(filtered) == 1
        assert unfiltered[0]["task_id"] == "t-1"
        assert unfiltered[1]["task_id"] == "t-2"
        assert filtered[0]["task_id"] == "t-1"

    async def test_unfiltered_receives_events_matching_multiple_filters(self):
        """Unfiltered subscriber receives all events regardless of other filtered subscribers."""
        bus = EventBus()
        unfiltered = []
        filter_a = []
        filter_b = []

        bus.subscribe("evt", lambda d: unfiltered.append(d))
        bus.subscribe("evt", lambda d: filter_a.append(d), filter={"src": "a"})
        bus.subscribe("evt", lambda d: filter_b.append(d), filter={"src": "b"})

        await bus.emit("evt", {"src": "a"})
        await bus.emit("evt", {"src": "b"})
        await bus.emit("evt", {"src": "c"})

        assert len(unfiltered) == 3  # all events
        assert len(filter_a) == 1  # only src=a
        assert len(filter_b) == 1  # only src=b

    async def test_unfiltered_wildcard_with_filtered_specific(self):
        """Unfiltered wildcard subscriber receives events that a filtered specific subscriber also gets."""
        bus = EventBus()
        wildcard_all = []
        specific_filtered = []

        bus.subscribe("*", lambda d: wildcard_all.append(d))
        bus.subscribe("task.done", lambda d: specific_filtered.append(d), filter={"status": "ok"})

        await bus.emit("task.done", {"status": "ok"})
        await bus.emit("task.done", {"status": "fail"})
        await bus.emit("other.event", {"status": "ok"})

        assert len(wildcard_all) == 3  # all events
        assert len(specific_filtered) == 1  # only task.done with status=ok

    async def test_multiple_unfiltered_subscribers_coexist_with_filtered(self):
        """Multiple unfiltered subscribers all receive every event alongside filtered ones."""
        bus = EventBus()
        unfiltered_1 = []
        unfiltered_2 = []
        filtered = []

        bus.subscribe("evt", lambda d: unfiltered_1.append(d))
        bus.subscribe("evt", lambda d: unfiltered_2.append(d), filter=None)
        bus.subscribe("evt", lambda d: filtered.append(d), filter={"x": 1})

        await bus.emit("evt", {"x": 1})
        await bus.emit("evt", {"x": 2})

        assert len(unfiltered_1) == 2
        assert len(unfiltered_2) == 2
        assert len(filtered) == 1
