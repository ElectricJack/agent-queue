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

    # --- Filtered subscription test suite (roadmap 0.1.3) ---

    async def test_filter_project_id_match(self):
        """(a) Subscriber with filter {"project_id": "foo"} receives matching event."""
        bus = EventBus()
        received = []
        bus.subscribe(
            "task.started",
            lambda d: received.append(d),
            filter={"project_id": "foo"},
        )
        await bus.emit("task.started", {"project_id": "foo", "task_id": "t-42"})
        assert len(received) == 1
        assert received[0]["project_id"] == "foo"
        assert received[0]["task_id"] == "t-42"

    async def test_filter_project_id_no_match(self):
        """(b) Same subscriber does NOT receive event with different project_id."""
        bus = EventBus()
        received = []
        bus.subscribe(
            "task.started",
            lambda d: received.append(d),
            filter={"project_id": "foo"},
        )
        await bus.emit("task.started", {"project_id": "bar", "task_id": "t-99"})
        assert len(received) == 0

    async def test_filter_multi_field_all_must_match(self):
        """(c) Multi-field filter only fires when ALL fields match (AND semantics)."""
        bus = EventBus()
        received = []
        bus.subscribe(
            "playbook.run.completed",
            lambda d: received.append(d),
            filter={"playbook_id": "code-quality-gate", "status": "success"},
        )
        # Only playbook_id matches
        await bus.emit(
            "playbook.run.completed",
            {"playbook_id": "code-quality-gate", "status": "failure"},
        )
        assert len(received) == 0

        # Only status matches
        await bus.emit(
            "playbook.run.completed",
            {"playbook_id": "deploy-gate", "status": "success"},
        )
        assert len(received) == 0

        # Neither matches
        await bus.emit(
            "playbook.run.completed",
            {"playbook_id": "deploy-gate", "status": "failure"},
        )
        assert len(received) == 0

        # Both match — this one should be delivered
        await bus.emit(
            "playbook.run.completed",
            {"playbook_id": "code-quality-gate", "status": "success", "run_id": "r-1"},
        )
        assert len(received) == 1
        assert received[0]["run_id"] == "r-1"

    async def test_filter_multiple_filtered_subscribers_same_event(self):
        """(d) Multiple filtered subscribers on same event type each receive only their matches."""
        bus = EventBus()
        proj_foo = []
        proj_bar = []
        proj_baz = []

        bus.subscribe("task.done", lambda d: proj_foo.append(d), filter={"project_id": "foo"})
        bus.subscribe("task.done", lambda d: proj_bar.append(d), filter={"project_id": "bar"})
        bus.subscribe("task.done", lambda d: proj_baz.append(d), filter={"project_id": "baz"})

        await bus.emit("task.done", {"project_id": "foo", "task_id": "t-1"})
        await bus.emit("task.done", {"project_id": "bar", "task_id": "t-2"})
        await bus.emit("task.done", {"project_id": "foo", "task_id": "t-3"})

        assert len(proj_foo) == 2
        assert proj_foo[0]["task_id"] == "t-1"
        assert proj_foo[1]["task_id"] == "t-3"

        assert len(proj_bar) == 1
        assert proj_bar[0]["task_id"] == "t-2"

        assert len(proj_baz) == 0

    async def test_filter_mixed_filtered_and_unfiltered(self):
        """(e) Unfiltered subscriber gets ALL events; filtered gets only matches."""
        bus = EventBus()
        all_events = []
        foo_events = []

        bus.subscribe("task.done", lambda d: all_events.append(d))  # no filter
        bus.subscribe(
            "task.done", lambda d: foo_events.append(d), filter={"project_id": "foo"}
        )

        await bus.emit("task.done", {"project_id": "foo", "task_id": "t-1"})
        await bus.emit("task.done", {"project_id": "bar", "task_id": "t-2"})
        await bus.emit("task.done", {"project_id": "foo", "task_id": "t-3"})
        await bus.emit("task.done", {"project_id": "qux", "task_id": "t-4"})

        # Unfiltered receives all 4
        assert len(all_events) == 4
        assert [e["task_id"] for e in all_events] == ["t-1", "t-2", "t-3", "t-4"]

        # Filtered receives only the 2 with project_id=foo
        assert len(foo_events) == 2
        assert [e["task_id"] for e in foo_events] == ["t-1", "t-3"]

    async def test_filter_nested_payload_field(self):
        """(f) Filter on nested payload field works via dict equality."""
        bus = EventBus()
        received = []
        # Filter on a top-level key whose value is a nested dict
        bus.subscribe(
            "build.finished",
            lambda d: received.append(d),
            filter={"metadata": {"env": "production", "region": "us-east"}},
        )

        # Exact nested dict match — should fire
        await bus.emit(
            "build.finished",
            {
                "build_id": "b-1",
                "metadata": {"env": "production", "region": "us-east"},
            },
        )
        assert len(received) == 1

        # Nested dict with different values — should NOT fire
        await bus.emit(
            "build.finished",
            {
                "build_id": "b-2",
                "metadata": {"env": "staging", "region": "us-east"},
            },
        )
        assert len(received) == 1

        # Nested dict with extra keys — should NOT fire (dict equality is strict)
        await bus.emit(
            "build.finished",
            {
                "build_id": "b-3",
                "metadata": {"env": "production", "region": "us-east", "extra": True},
            },
        )
        assert len(received) == 1  # still 1 — extra key means dicts aren't equal

    async def test_filter_none_value_matches_absent_or_null(self):
        """(g) Filter with None value matches events where field is absent or null."""
        bus = EventBus()
        received = []
        bus.subscribe(
            "task.update",
            lambda d: received.append(d),
            filter={"error": None},
        )

        # Field absent from payload — data.get("error") returns None == None → match
        await bus.emit("task.update", {"task_id": "t-1", "status": "ok"})
        assert len(received) == 1

        # Field explicitly set to None — match
        await bus.emit("task.update", {"task_id": "t-2", "error": None})
        assert len(received) == 2

        # Field present with a value — no match
        await bus.emit("task.update", {"task_id": "t-3", "error": "timeout"})
        assert len(received) == 2  # still 2, this one was skipped


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


class TestEventBusFilterEdgeCases:
    """Edge-case tests for EventBus filter matching behaviour.

    Covers: missing payload fields, extra payload fields (subset matching),
    empty payloads, type mismatches, and rapid mixed-event ordering.
    """

    # --- (a) payload missing a field required by the filter → NOT delivered ---

    async def test_missing_single_filter_field_not_delivered(self):
        """Event whose payload is missing the single field required by the filter is skipped."""
        bus = EventBus()
        received = []
        bus.subscribe("deploy", lambda d: received.append(d), filter={"env": "prod"})

        await bus.emit("deploy", {"version": "1.0.0"})  # no 'env' key at all
        assert received == []

    async def test_missing_one_of_multiple_filter_fields_not_delivered(self):
        """When the filter requires two fields and the payload has only one, no delivery."""
        bus = EventBus()
        received = []
        bus.subscribe(
            "deploy",
            lambda d: received.append(d),
            filter={"env": "prod", "region": "us-east-1"},
        )

        # Has 'env' but not 'region'
        await bus.emit("deploy", {"env": "prod", "version": "2.0"})
        assert received == []

        # Has 'region' but not 'env'
        await bus.emit("deploy", {"region": "us-east-1", "version": "2.0"})
        assert received == []

    # --- (b) extra fields beyond filter still matches (subset check) ---

    async def test_extra_fields_still_match(self):
        """Payload with extra fields beyond those in the filter still matches."""
        bus = EventBus()
        received = []
        bus.subscribe("build", lambda d: received.append(d), filter={"status": "success"})

        await bus.emit("build", {
            "status": "success",
            "duration_ms": 12345,
            "commit": "abc123",
            "artifacts": ["dist.tar.gz"],
        })
        assert len(received) == 1
        assert received[0]["commit"] == "abc123"

    async def test_extra_fields_with_multi_key_filter(self):
        """Subset matching works when filter has multiple keys and payload has many more."""
        bus = EventBus()
        received = []
        bus.subscribe(
            "pipeline",
            lambda d: received.append(d),
            filter={"stage": "test", "passed": True},
        )

        await bus.emit("pipeline", {
            "stage": "test",
            "passed": True,
            "duration": 42,
            "runner": "ci-node-3",
            "coverage": 0.87,
        })
        assert len(received) == 1

    # --- (c) empty payload {} does not match any filter with required fields ---

    async def test_empty_payload_no_match_single_field_filter(self):
        """An empty payload {} cannot satisfy a filter that requires any field."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={"key": "value"})

        await bus.emit("evt", {})
        assert received == []

    async def test_empty_payload_no_match_multi_field_filter(self):
        """An empty payload {} cannot satisfy a filter with multiple required fields."""
        bus = EventBus()
        received = []
        bus.subscribe(
            "evt",
            lambda d: received.append(d),
            filter={"a": 1, "b": 2, "c": 3},
        )

        await bus.emit("evt", {})
        assert received == []

    async def test_none_payload_no_match_filter(self):
        """Emitting with data=None (coerced to {}) does not match a non-empty filter."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={"required": True})

        await bus.emit("evt")  # data defaults to {}
        assert received == []

    # --- (d) filter on field with wrong type → no match ---

    async def test_type_mismatch_string_vs_int(self):
        """Filter expects a string but payload has an int → no match."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={"port": "8080"})

        await bus.emit("evt", {"port": 8080})  # int, not str
        assert received == []

    async def test_type_mismatch_int_vs_string(self):
        """Filter expects an int but payload has a string → no match."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={"count": 5})

        await bus.emit("evt", {"count": "5"})  # str, not int
        assert received == []

    async def test_type_mismatch_bool_vs_string(self):
        """Filter expects a bool but payload has a string → no match."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={"enabled": True})

        await bus.emit("evt", {"enabled": "true"})
        assert received == []

    async def test_type_mismatch_none_vs_value(self):
        """Filter expects a concrete value but payload has None → no match."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={"status": "active"})

        await bus.emit("evt", {"status": None})
        assert received == []

    async def test_type_match_confirms_equality_semantics(self):
        """Same type and value → match (sanity check alongside mismatch tests)."""
        bus = EventBus()
        received = []
        bus.subscribe("evt", lambda d: received.append(d), filter={"port": 8080})

        await bus.emit("evt", {"port": 8080})
        assert len(received) == 1

    # --- (e) rapid mixed matching/non-matching events: correct subset, in order ---

    async def test_rapid_mixed_events_correct_subset_in_order(self):
        """Rapidly emitting mixed matching/non-matching events delivers only the matching
        ones, and in the exact emission order."""
        bus = EventBus()
        received = []
        bus.subscribe("tick", lambda d: received.append(d), filter={"match": True})

        # Emit 20 events — even-indexed match, odd-indexed don't
        for i in range(20):
            await bus.emit("tick", {"match": i % 2 == 0, "seq": i})

        # Should receive exactly the 10 matching events
        assert len(received) == 10
        # Verify they arrive in emission order
        expected_seqs = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]
        actual_seqs = [d["seq"] for d in received]
        assert actual_seqs == expected_seqs

    async def test_rapid_mixed_events_multiple_filters_correct_subsets(self):
        """Two filtered subscribers on the same event each receive their correct subset
        in order during rapid emission."""
        bus = EventBus()
        team_a = []
        team_b = []
        all_events = []

        bus.subscribe("result", lambda d: team_a.append(d), filter={"team": "alpha"})
        bus.subscribe("result", lambda d: team_b.append(d), filter={"team": "beta"})
        bus.subscribe("result", lambda d: all_events.append(d))  # unfiltered

        # Rapid interleaved emissions
        sequence = [
            {"team": "alpha", "seq": 0},
            {"team": "beta", "seq": 1},
            {"team": "gamma", "seq": 2},
            {"team": "alpha", "seq": 3},
            {"team": "beta", "seq": 4},
            {"team": "alpha", "seq": 5},
            {"team": "gamma", "seq": 6},
            {"team": "beta", "seq": 7},
        ]
        for payload in sequence:
            await bus.emit("result", dict(payload))

        assert len(all_events) == 8
        assert [d["seq"] for d in team_a] == [0, 3, 5]
        assert [d["seq"] for d in team_b] == [1, 4, 7]

    async def test_rapid_emission_with_async_handlers_preserves_order(self):
        """Async handlers under rapid mixed emission still receive events in order."""
        bus = EventBus()
        received = []

        async def handler(data):
            await asyncio.sleep(0)  # yield to event loop
            received.append(data)

        bus.subscribe("evt", handler, filter={"keep": True})

        for i in range(15):
            await bus.emit("evt", {"keep": i % 3 == 0, "seq": i})

        expected_seqs = [0, 3, 6, 9, 12]
        actual_seqs = [d["seq"] for d in received]
        assert actual_seqs == expected_seqs
