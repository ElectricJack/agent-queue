"""Tests for cross-playbook composition via event chaining (roadmap 5.3.11).

Covers the seven mandatory test cases from roadmap 5.3.11 for playbook
composition per the playbooks spec Section 10 (Composability):

  (a) Playbook A completes and emits ``playbook.run.completed`` with
      ``playbook_id="code-review"`` — playbook B subscribed with filter
      ``{"playbook_id": "code-review"}`` triggers
  (b) Playbook B does NOT trigger for ``playbook.run.completed`` from a
      different ``playbook_id``
  (c) 3-playbook chain: A → B → C each triggered by predecessor's
      completion event
  (d) Composition with payload data: playbook A's output is available
      in playbook B's trigger event payload
  (e) Circular composition (A triggers B triggers A) is prevented by
      cooldown or detected and blocked
  (f) Failed playbook emits ``playbook.run.failed`` — downstream
      playbooks subscribed to failure events trigger correctly
  (g) Composition across scopes: system playbook triggers project
      playbook via filtered event

These tests exercise the full interaction between:

- :class:`~src.event_bus.EventBus` — filtered pub/sub delivery
- :class:`~src.playbooks.manager.PlaybookManager` — trigger mapping,
  cooldown, concurrency tracking
- :class:`~src.playbooks.runner.PlaybookRunner` — graph execution and
  event emission on completion/failure

Since no ``PlaybookDispatcher`` component exists yet, the tests wire up
the event-to-playbook-launch flow manually — serving as the reference
implementation for the future dispatcher.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.event_bus import EventBus
from src.playbooks.manager import PlaybookManager
from src.playbooks.models import CompiledPlaybook, PlaybookNode
from src.playbooks.runner import PlaybookRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_playbook(
    *,
    playbook_id: str = "test-playbook",
    version: int = 1,
    source_hash: str = "abc123def456",
    triggers: list[str] | None = None,
    scope: str = "system",
    cooldown_seconds: int | None = None,
) -> CompiledPlaybook:
    """Create a minimal valid CompiledPlaybook for testing."""
    return CompiledPlaybook(
        id=playbook_id,
        version=version,
        source_hash=source_hash,
        triggers=triggers or ["git.commit"],
        scope=scope,
        cooldown_seconds=cooldown_seconds,
        nodes={
            "start": PlaybookNode(
                entry=True,
                prompt="Do something.",
                goto="end",
            ),
            "end": PlaybookNode(terminal=True),
        },
    )


def _simple_graph(playbook_id: str) -> dict:
    """Create a minimal 2-node linear graph dict for PlaybookRunner."""
    return {
        "id": playbook_id,
        "version": 1,
        "nodes": {
            "start": {
                "entry": True,
                "prompt": "Run analysis.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


def _failing_graph(playbook_id: str) -> dict:
    """Create a graph that references a missing node to trigger failure."""
    return {
        "id": playbook_id,
        "version": 1,
        "nodes": {
            "start": {
                "entry": True,
                "prompt": "Begin.",
                "goto": "missing_node",
            },
            "done": {
                "terminal": True,
            },
        },
    }


def _manager_with_playbooks(*playbooks: CompiledPlaybook) -> PlaybookManager:
    """Create a PlaybookManager with pre-loaded playbooks (no disk/store)."""
    manager = PlaybookManager()
    for pb in playbooks:
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
    return manager


def _mock_supervisor() -> AsyncMock:
    """Create a mock Supervisor with a controllable chat() return value."""
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value="Done.")
    supervisor.summarize = AsyncMock(return_value="Summary of prior steps.")
    return supervisor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    """A real EventBus instance (validation disabled for test simplicity)."""
    return EventBus(validate_events=False)


# ---------------------------------------------------------------------------
# (a) Filtered subscription triggers on matching playbook_id
# ---------------------------------------------------------------------------


class TestFilteredTriggerOnCompletion:
    """(a) playbook.run.completed with matching playbook_id filter triggers downstream."""

    async def test_filtered_subscription_triggers_on_matching_playbook_id(
        self, event_bus: EventBus
    ):
        """Playbook B subscribed with filter {"playbook_id": "code-review"} triggers
        when playbook A (id=code-review) completes."""
        # Set up: playbook B triggers on playbook.run.completed filtered by playbook_id
        playbook_b = _make_playbook(
            playbook_id="post-review-summary",
            triggers=["playbook.run.completed"],
        )
        manager = _manager_with_playbooks(playbook_b)

        # Capture events that match the filtered subscription
        triggered_playbooks: list[str] = []

        def on_completed(data: dict) -> None:
            # Simulate what a dispatcher would do: look up matching playbooks
            matching = manager.get_playbooks_by_trigger("playbook.run.completed")
            for pb in matching:
                triggered_playbooks.append(pb.id)

        event_bus.subscribe(
            "playbook.run.completed",
            on_completed,
            filter={"playbook_id": "code-review"},
        )

        # Run playbook A (code-review) — it will emit playbook.run.completed
        supervisor = _mock_supervisor()
        graph_a = _simple_graph("code-review")
        event_data = {"type": "git.commit", "project_id": "myapp"}

        runner = PlaybookRunner(graph_a, event_data, supervisor, event_bus=event_bus)
        result = await runner.run()

        assert result.status == "completed"
        assert "post-review-summary" in triggered_playbooks

    async def test_event_bus_delivers_full_payload_to_filtered_handler(
        self, event_bus: EventBus
    ):
        """The filtered handler receives the complete event payload including
        playbook_id, run_id, final_context etc."""
        received_payloads: list[dict] = []
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: received_payloads.append(d),
            filter={"playbook_id": "code-review"},
        )

        supervisor = _mock_supervisor()
        supervisor.chat = AsyncMock(return_value="All checks passed.")
        graph_a = _simple_graph("code-review")

        runner = PlaybookRunner(graph_a, {"type": "git.commit"}, supervisor, event_bus=event_bus)
        result = await runner.run()

        assert result.status == "completed"
        assert len(received_payloads) == 1
        payload = received_payloads[0]
        assert payload["playbook_id"] == "code-review"
        assert payload["run_id"] == runner.run_id
        assert payload["final_context"] == "All checks passed."
        assert "tokens_used" in payload

    async def test_manager_finds_playbooks_by_completion_trigger(self):
        """PlaybookManager.get_playbooks_by_trigger returns playbooks that
        have 'playbook.run.completed' in their triggers list."""
        pb_downstream = _make_playbook(
            playbook_id="downstream",
            triggers=["playbook.run.completed"],
        )
        pb_unrelated = _make_playbook(
            playbook_id="unrelated",
            triggers=["git.commit"],
        )
        manager = _manager_with_playbooks(pb_downstream, pb_unrelated)

        matching = manager.get_playbooks_by_trigger("playbook.run.completed")
        assert len(matching) == 1
        assert matching[0].id == "downstream"


# ---------------------------------------------------------------------------
# (b) Filtered subscription does NOT trigger for different playbook_id
# ---------------------------------------------------------------------------


class TestFilteredSubscriptionNonMatch:
    """(b) Playbook B does NOT trigger for playbook.run.completed from a
    different playbook_id."""

    async def test_no_trigger_for_different_playbook_id(self, event_bus: EventBus):
        """Handler subscribed with filter {"playbook_id": "code-review"} does NOT
        fire when a different playbook (lint-check) completes."""
        triggered: list[str] = []
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: triggered.append(d["playbook_id"]),
            filter={"playbook_id": "code-review"},
        )

        # Run a different playbook (lint-check)
        supervisor = _mock_supervisor()
        graph = _simple_graph("lint-check")
        runner = PlaybookRunner(graph, {"type": "git.commit"}, supervisor, event_bus=event_bus)
        result = await runner.run()

        assert result.status == "completed"
        assert len(triggered) == 0, (
            "Handler should not fire for playbook_id='lint-check' "
            "when filtered on 'code-review'"
        )

    async def test_unfiltered_subscription_still_receives_all(self, event_bus: EventBus):
        """An unfiltered subscription to playbook.run.completed receives events
        from ANY playbook — filtering is opt-in."""
        all_events: list[str] = []
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: all_events.append(d["playbook_id"]),
        )

        supervisor = _mock_supervisor()

        # Run two different playbooks
        for pb_id in ["alpha", "beta"]:
            graph = _simple_graph(pb_id)
            runner = PlaybookRunner(graph, {"type": "git.commit"}, supervisor, event_bus=event_bus)
            await runner.run()

        assert sorted(all_events) == ["alpha", "beta"]

    async def test_filter_only_matches_exact_value(self, event_bus: EventBus):
        """EventBus filter uses exact equality — partial matches don't trigger."""
        triggered: list[str] = []
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: triggered.append(d["playbook_id"]),
            filter={"playbook_id": "code-review"},
        )

        # Run playbooks with similar but non-matching IDs
        supervisor = _mock_supervisor()
        for pb_id in ["code-review-v2", "my-code-review", "code-revie"]:
            graph = _simple_graph(pb_id)
            runner = PlaybookRunner(graph, {"type": "test"}, supervisor, event_bus=event_bus)
            await runner.run()

        assert len(triggered) == 0


# ---------------------------------------------------------------------------
# (c) 3-playbook chain: A → B → C
# ---------------------------------------------------------------------------


class TestThreePlaybookChain:
    """(c) 3-playbook chain: A → B → C each triggered by predecessor's
    completion event."""

    async def test_three_playbook_chain_fires_sequentially(self, event_bus: EventBus):
        """When playbook A completes, it triggers playbook B, whose completion
        triggers playbook C. All three run via the event bus."""
        execution_order: list[str] = []
        supervisor = _mock_supervisor()

        # Register playbooks B and C in the manager
        pb_b = _make_playbook(
            playbook_id="step-b",
            triggers=["playbook.run.completed"],
        )
        pb_c = _make_playbook(
            playbook_id="step-c",
            triggers=["playbook.run.completed"],
        )
        _manager_with_playbooks(pb_b, pb_c)  # validates trigger indexing

        async def dispatch_completion(data: dict) -> None:
            """Simulate a dispatcher: on playbook.run.completed, launch
            downstream playbooks that match the source playbook_id."""
            source_id = data["playbook_id"]
            execution_order.append(f"completed:{source_id}")

            # Determine which downstream playbook to launch
            if source_id == "step-a":
                # Launch step-b
                graph_b = _simple_graph("step-b")
                runner_b = PlaybookRunner(
                    graph_b, data, supervisor, event_bus=event_bus
                )
                await runner_b.run()
            elif source_id == "step-b":
                # Launch step-c
                graph_c = _simple_graph("step-c")
                runner_c = PlaybookRunner(
                    graph_c, data, supervisor, event_bus=event_bus
                )
                await runner_c.run()

        event_bus.subscribe("playbook.run.completed", dispatch_completion)

        # Start the chain by running playbook A
        graph_a = _simple_graph("step-a")
        runner_a = PlaybookRunner(
            graph_a, {"type": "git.commit"}, supervisor, event_bus=event_bus
        )
        result_a = await runner_a.run()

        assert result_a.status == "completed"
        assert execution_order == [
            "completed:step-a",
            "completed:step-b",
            "completed:step-c",
        ]

    async def test_chain_with_filtered_subscriptions(self, event_bus: EventBus):
        """3-playbook chain using filtered subscriptions: each downstream
        playbook only triggers on its specific predecessor."""
        execution_order: list[str] = []
        supervisor = _mock_supervisor()

        async def launch_b(data: dict) -> None:
            execution_order.append("launched:step-b")
            graph = _simple_graph("step-b")
            runner = PlaybookRunner(graph, data, supervisor, event_bus=event_bus)
            await runner.run()

        async def launch_c(data: dict) -> None:
            execution_order.append("launched:step-c")
            graph = _simple_graph("step-c")
            runner = PlaybookRunner(graph, data, supervisor, event_bus=event_bus)
            await runner.run()

        # B triggers only on A's completion
        event_bus.subscribe(
            "playbook.run.completed",
            launch_b,
            filter={"playbook_id": "step-a"},
        )
        # C triggers only on B's completion
        event_bus.subscribe(
            "playbook.run.completed",
            launch_c,
            filter={"playbook_id": "step-b"},
        )

        # Start the chain
        graph_a = _simple_graph("step-a")
        runner_a = PlaybookRunner(
            graph_a, {"type": "git.commit"}, supervisor, event_bus=event_bus
        )
        await runner_a.run()

        assert execution_order == ["launched:step-b", "launched:step-c"]

    async def test_chain_intermediate_failure_stops_chain(self, event_bus: EventBus):
        """If step B fails, step C (which triggers on B's completion) does NOT run."""
        execution_order: list[str] = []
        supervisor = _mock_supervisor()

        async def launch_b(data: dict) -> None:
            execution_order.append("launched:step-b")
            # Use a failing graph for step B
            graph = _failing_graph("step-b")
            runner = PlaybookRunner(graph, data, supervisor, event_bus=event_bus)
            await runner.run()

        async def launch_c(data: dict) -> None:
            execution_order.append("launched:step-c")

        # B triggers on A's completion
        event_bus.subscribe(
            "playbook.run.completed",
            launch_b,
            filter={"playbook_id": "step-a"},
        )
        # C triggers on B's completion — should NOT fire because B fails
        event_bus.subscribe(
            "playbook.run.completed",
            launch_c,
            filter={"playbook_id": "step-b"},
        )

        # Start the chain
        graph_a = _simple_graph("step-a")
        runner_a = PlaybookRunner(
            graph_a, {"type": "git.commit"}, supervisor, event_bus=event_bus
        )
        await runner_a.run()

        assert execution_order == ["launched:step-b"]
        assert "launched:step-c" not in execution_order


# ---------------------------------------------------------------------------
# (d) Composition with payload data
# ---------------------------------------------------------------------------


class TestCompositionPayloadData:
    """(d) Playbook A's output is available in playbook B's trigger event payload."""

    async def test_completed_event_carries_final_context(self, event_bus: EventBus):
        """The playbook.run.completed event includes final_context which can
        be used by downstream playbooks as their trigger event."""
        received_trigger_events: list[dict] = []
        supervisor = _mock_supervisor()
        supervisor.chat = AsyncMock(return_value="Found 3 issues in src/main.py")

        async def launch_downstream(data: dict) -> None:
            # The downstream playbook receives the completion event as its
            # trigger event — including final_context from playbook A
            received_trigger_events.append(data)

        event_bus.subscribe(
            "playbook.run.completed",
            launch_downstream,
            filter={"playbook_id": "code-review"},
        )

        graph_a = _simple_graph("code-review")
        runner_a = PlaybookRunner(
            graph_a, {"type": "git.commit", "project_id": "myapp"}, supervisor,
            event_bus=event_bus,
        )
        await runner_a.run()

        assert len(received_trigger_events) == 1
        trigger = received_trigger_events[0]

        # Verify all key fields are in the trigger event
        assert trigger["playbook_id"] == "code-review"
        assert trigger["final_context"] == "Found 3 issues in src/main.py"
        assert trigger["project_id"] == "myapp"
        assert "run_id" in trigger
        assert "tokens_used" in trigger

    async def test_downstream_runner_receives_upstream_payload_as_event(
        self, event_bus: EventBus
    ):
        """When playbook B is launched with the completion event as its trigger,
        the upstream playbook's output is accessible via self.event."""
        downstream_events: list[dict] = []
        supervisor = _mock_supervisor()
        supervisor.chat = AsyncMock(return_value="Analysis complete: all clear.")

        async def launch_downstream(data: dict) -> None:
            # Launch playbook B with the completion event as its trigger event
            graph_b = _simple_graph("summary")
            runner_b = PlaybookRunner(
                graph_b, data, supervisor, event_bus=event_bus
            )
            # Verify the runner has access to upstream output
            downstream_events.append(runner_b.event)
            await runner_b.run()

        event_bus.subscribe(
            "playbook.run.completed",
            launch_downstream,
            filter={"playbook_id": "code-review"},
        )

        graph_a = _simple_graph("code-review")
        runner_a = PlaybookRunner(
            graph_a, {"type": "git.commit"}, supervisor, event_bus=event_bus
        )
        await runner_a.run()

        assert len(downstream_events) == 1
        assert downstream_events[0]["playbook_id"] == "code-review"
        assert downstream_events[0]["final_context"] == "Analysis complete: all clear."

    async def test_payload_includes_duration_and_tokens(self, event_bus: EventBus):
        """The completion event payload includes tokens_used and duration_seconds."""
        received: list[dict] = []
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: received.append(d),
        )

        supervisor = _mock_supervisor()
        graph = _simple_graph("metrics-source")
        runner = PlaybookRunner(
            graph, {"type": "test"}, supervisor, event_bus=event_bus
        )
        await runner.run()

        assert len(received) == 1
        assert "tokens_used" in received[0]
        assert isinstance(received[0]["tokens_used"], int)
        assert "duration_seconds" in received[0]
        assert received[0]["duration_seconds"] >= 0


# ---------------------------------------------------------------------------
# (e) Circular composition prevented by cooldown
# ---------------------------------------------------------------------------


class TestCircularCompositionPrevention:
    """(e) Circular composition (A triggers B triggers A) is prevented by
    cooldown or detected and blocked."""

    async def test_cooldown_prevents_circular_retriggering(self, event_bus: EventBus):
        """A → B → A cycle is broken by cooldown: after A completes and triggers
        B, B's completion should NOT re-trigger A because A is on cooldown."""
        execution_order: list[str] = []
        supervisor = _mock_supervisor()

        # Both playbooks trigger on playbook.run.completed with cooldown
        pb_a = _make_playbook(
            playbook_id="cycle-a",
            triggers=["playbook.run.completed"],
            cooldown_seconds=60,
        )
        pb_b = _make_playbook(
            playbook_id="cycle-b",
            triggers=["playbook.run.completed"],
            cooldown_seconds=60,
        )
        manager = _manager_with_playbooks(pb_a, pb_b)

        async def dispatch(data: dict) -> None:
            source_id = data["playbook_id"]
            execution_order.append(f"completed:{source_id}")

            if source_id == "cycle-a":
                # Record cycle-a's execution for cooldown
                manager.record_execution("cycle-a")
                # Launch cycle-b
                graph = _simple_graph("cycle-b")
                runner = PlaybookRunner(
                    graph, data, supervisor, event_bus=event_bus
                )
                await runner.run()
            elif source_id == "cycle-b":
                manager.record_execution("cycle-b")
                # Check if cycle-a is on cooldown before re-launching
                if not manager.is_on_cooldown("cycle-a"):
                    graph = _simple_graph("cycle-a")
                    runner = PlaybookRunner(
                        graph, data, supervisor, event_bus=event_bus
                    )
                    await runner.run()
                else:
                    execution_order.append("blocked:cycle-a(cooldown)")

        event_bus.subscribe("playbook.run.completed", dispatch)

        # Start the chain with cycle-a
        graph_a = _simple_graph("cycle-a")
        runner = PlaybookRunner(
            graph_a, {"type": "git.commit"}, supervisor, event_bus=event_bus
        )
        await runner.run()

        # cycle-a → cycle-b → (cycle-a blocked by cooldown)
        assert execution_order == [
            "completed:cycle-a",
            "completed:cycle-b",
            "blocked:cycle-a(cooldown)",
        ]

    async def test_get_triggerable_playbooks_excludes_on_cooldown(self):
        """PlaybookManager.get_triggerable_playbooks filters out playbooks
        on cooldown, preventing circular re-triggering."""
        pb = _make_playbook(
            playbook_id="cycle-pb",
            triggers=["playbook.run.completed"],
            cooldown_seconds=60,
        )
        manager = _manager_with_playbooks(pb)

        # Before cooldown: playbook is triggerable
        triggerable = manager.get_triggerable_playbooks("playbook.run.completed")
        assert len(triggerable) == 1
        assert triggerable[0].id == "cycle-pb"

        # Record execution (puts it on cooldown)
        manager.record_execution("cycle-pb")

        # After cooldown starts: playbook is NOT triggerable
        triggerable = manager.get_triggerable_playbooks("playbook.run.completed")
        assert len(triggerable) == 0

    async def test_no_cooldown_allows_infinite_loop_risk(self, event_bus: EventBus):
        """Without cooldown, a cycle WOULD run indefinitely. This test uses
        a counter to break after a few iterations, demonstrating the need
        for cooldown in circular compositions."""
        iteration_count = 0
        max_iterations = 5
        supervisor = _mock_supervisor()

        async def dispatch(data: dict) -> None:
            nonlocal iteration_count
            source_id = data["playbook_id"]

            if source_id == "ping" and iteration_count < max_iterations:
                iteration_count += 1
                graph = _simple_graph("pong")
                runner = PlaybookRunner(
                    graph, data, supervisor, event_bus=event_bus
                )
                await runner.run()
            elif source_id == "pong" and iteration_count < max_iterations:
                iteration_count += 1
                graph = _simple_graph("ping")
                runner = PlaybookRunner(
                    graph, data, supervisor, event_bus=event_bus
                )
                await runner.run()

        event_bus.subscribe("playbook.run.completed", dispatch)

        graph_a = _simple_graph("ping")
        runner = PlaybookRunner(
            graph_a, {"type": "start"}, supervisor, event_bus=event_bus
        )
        await runner.run()

        # Without cooldown, the cycle ran until the safety limit
        assert iteration_count == max_iterations

    async def test_concurrency_cap_as_secondary_guard(self):
        """Concurrency cap provides a secondary guard against runaway chains:
        if max_concurrent_runs is reached, new runs are rejected."""
        pb = _make_playbook(
            playbook_id="chain-link",
            triggers=["playbook.run.completed"],
        )
        manager = _manager_with_playbooks(pb)
        manager.max_concurrent_runs = 2

        # Simulate two running playbooks
        task1 = asyncio.ensure_future(asyncio.sleep(100))
        task2 = asyncio.ensure_future(asyncio.sleep(100))
        try:
            manager.register_run("run-1", "chain-link", task1)
            manager.register_run("run-2", "chain-link", task2)

            # Third run should be rejected
            assert not manager.can_start_run()
        finally:
            task1.cancel()
            task2.cancel()
            try:
                await task1
            except asyncio.CancelledError:
                pass
            try:
                await task2
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# (f) Failed playbook triggers downstream failure subscribers
# ---------------------------------------------------------------------------


class TestFailedPlaybookTriggersDownstream:
    """(f) Failed playbook emits playbook.run.failed — downstream playbooks
    subscribed to failure events trigger correctly."""

    async def test_failure_event_triggers_error_handler_playbook(self, event_bus: EventBus):
        """When playbook A fails, a downstream playbook subscribed to
        playbook.run.failed fires."""
        failure_events: list[dict] = []
        supervisor = _mock_supervisor()

        event_bus.subscribe(
            "playbook.run.failed",
            lambda d: failure_events.append(d),
            filter={"playbook_id": "quality-gate"},
        )

        # Run a failing playbook
        graph = _failing_graph("quality-gate")
        runner = PlaybookRunner(
            graph, {"type": "git.commit", "project_id": "myapp"}, supervisor,
            event_bus=event_bus,
        )
        result = await runner.run()

        assert result.status == "failed"
        assert len(failure_events) == 1
        assert failure_events[0]["playbook_id"] == "quality-gate"
        assert "failed_at_node" in failure_events[0]
        assert "error" in failure_events[0]

    async def test_failure_event_does_not_trigger_completion_subscribers(
        self, event_bus: EventBus
    ):
        """Failure event does NOT trigger handlers subscribed to
        playbook.run.completed."""
        completed_events: list[dict] = []
        failed_events: list[dict] = []

        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: completed_events.append(d),
        )
        event_bus.subscribe(
            "playbook.run.failed",
            lambda d: failed_events.append(d),
        )

        supervisor = _mock_supervisor()
        graph = _failing_graph("broken-playbook")
        runner = PlaybookRunner(
            graph, {"type": "test"}, supervisor, event_bus=event_bus
        )
        await runner.run()

        assert len(completed_events) == 0
        assert len(failed_events) == 1

    async def test_failure_subscriber_receives_error_details(self, event_bus: EventBus):
        """The playbook.run.failed event includes error message and
        failed_at_node for diagnostics."""
        captured: list[dict] = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        supervisor = _mock_supervisor()
        supervisor.chat = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        graph = {
            "id": "timeout-playbook",
            "version": 1,
            "nodes": {
                "analyze": {
                    "entry": True,
                    "prompt": "Analyze code.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        runner = PlaybookRunner(
            graph, {"type": "test"}, supervisor, event_bus=event_bus
        )
        result = await runner.run()

        assert result.status == "failed"
        assert len(captured) == 1
        assert captured[0]["failed_at_node"] == "analyze"
        assert "LLM timeout" in captured[0].get("error", "")

    async def test_failure_chain_a_fails_triggers_b(self, event_bus: EventBus):
        """Playbook A fails → triggers playbook B (error handler) which
        runs and completes successfully."""
        execution_log: list[str] = []
        supervisor = _mock_supervisor()

        async def launch_error_handler(data: dict) -> None:
            execution_log.append(f"error_handler_triggered:{data['playbook_id']}")
            graph_b = _simple_graph("error-handler")
            runner_b = PlaybookRunner(
                graph_b, data, supervisor, event_bus=event_bus
            )
            result = await runner_b.run()
            execution_log.append(f"error_handler_completed:{result.status}")

        event_bus.subscribe(
            "playbook.run.failed",
            launch_error_handler,
            filter={"playbook_id": "flaky-task"},
        )

        graph_a = _failing_graph("flaky-task")
        runner = PlaybookRunner(
            graph_a, {"type": "test"}, supervisor, event_bus=event_bus
        )
        await runner.run()

        assert execution_log == [
            "error_handler_triggered:flaky-task",
            "error_handler_completed:completed",
        ]

    async def test_failure_filter_on_different_playbook_id_does_not_fire(
        self, event_bus: EventBus
    ):
        """A failure handler filtered on playbook_id='alpha' does NOT fire
        when playbook_id='beta' fails."""
        triggered: list[str] = []
        event_bus.subscribe(
            "playbook.run.failed",
            lambda d: triggered.append(d["playbook_id"]),
            filter={"playbook_id": "alpha"},
        )

        supervisor = _mock_supervisor()
        graph = _failing_graph("beta")
        runner = PlaybookRunner(
            graph, {"type": "test"}, supervisor, event_bus=event_bus
        )
        await runner.run()

        assert len(triggered) == 0


# ---------------------------------------------------------------------------
# (g) Composition across scopes
# ---------------------------------------------------------------------------


class TestCompositionAcrossScopes:
    """(g) System playbook triggers project playbook via filtered event."""

    async def test_system_playbook_completion_visible_to_project_scope_subscriber(
        self, event_bus: EventBus
    ):
        """A system-scoped playbook emits playbook.run.completed with project_id.
        A project-scoped downstream playbook subscribed with that project_id
        filter triggers."""
        triggered: list[dict] = []

        # Subscribe a "project-scoped" handler filtered on project_id
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: triggered.append(d),
            filter={"project_id": "webapp"},
        )

        supervisor = _mock_supervisor()
        graph = _simple_graph("system-wide-check")
        # The trigger event includes project_id, which gets injected
        # into the completion event by PlaybookRunner
        runner = PlaybookRunner(
            graph,
            {"type": "git.commit", "project_id": "webapp"},
            supervisor,
            event_bus=event_bus,
        )
        await runner.run()

        assert len(triggered) == 1
        assert triggered[0]["project_id"] == "webapp"

    async def test_project_scoped_subscriber_ignores_different_project(
        self, event_bus: EventBus
    ):
        """A handler filtered on project_id='webapp' does NOT fire when
        a playbook runs for project_id='api-service'."""
        triggered: list[dict] = []
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: triggered.append(d),
            filter={"project_id": "webapp"},
        )

        supervisor = _mock_supervisor()
        graph = _simple_graph("system-check")
        runner = PlaybookRunner(
            graph,
            {"type": "git.commit", "project_id": "api-service"},
            supervisor,
            event_bus=event_bus,
        )
        await runner.run()

        assert len(triggered) == 0

    async def test_multi_field_filter_matches_playbook_id_and_project_id(
        self, event_bus: EventBus
    ):
        """A handler with a multi-field filter (playbook_id AND project_id)
        only fires when BOTH fields match."""
        triggered: list[dict] = []
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: triggered.append(d),
            filter={"playbook_id": "code-review", "project_id": "webapp"},
        )

        supervisor = _mock_supervisor()

        # Matching: correct playbook_id AND project_id
        graph1 = _simple_graph("code-review")
        runner1 = PlaybookRunner(
            graph1,
            {"type": "git.commit", "project_id": "webapp"},
            supervisor,
            event_bus=event_bus,
        )
        await runner1.run()

        # Non-matching: correct playbook_id but wrong project_id
        graph2 = _simple_graph("code-review")
        runner2 = PlaybookRunner(
            graph2,
            {"type": "git.commit", "project_id": "api-service"},
            supervisor,
            event_bus=event_bus,
        )
        await runner2.run()

        # Non-matching: wrong playbook_id but correct project_id
        graph3 = _simple_graph("lint-check")
        runner3 = PlaybookRunner(
            graph3,
            {"type": "git.commit", "project_id": "webapp"},
            supervisor,
            event_bus=event_bus,
        )
        await runner3.run()

        assert len(triggered) == 1
        assert triggered[0]["playbook_id"] == "code-review"
        assert triggered[0]["project_id"] == "webapp"

    async def test_system_scope_playbook_without_project_id_not_matched(
        self, event_bus: EventBus
    ):
        """A system playbook running without project_id in its trigger event
        does NOT match a handler filtered on a specific project_id."""
        triggered: list[dict] = []
        event_bus.subscribe(
            "playbook.run.completed",
            lambda d: triggered.append(d),
            filter={"project_id": "webapp"},
        )

        supervisor = _mock_supervisor()
        graph = _simple_graph("system-timer-playbook")
        # No project_id in trigger event
        runner = PlaybookRunner(
            graph,
            {"type": "timer.30m", "tick_time": "2024-01-01T00:00:00Z"},
            supervisor,
            event_bus=event_bus,
        )
        await runner.run()

        assert len(triggered) == 0

    async def test_manager_scope_aware_trigger_lookup(self):
        """PlaybookManager stores scope metadata on each playbook, enabling
        callers to further filter by scope when dispatching."""
        pb_system = _make_playbook(
            playbook_id="system-downstream",
            triggers=["playbook.run.completed"],
            scope="system",
        )
        pb_project = _make_playbook(
            playbook_id="project-downstream",
            triggers=["playbook.run.completed"],
            scope="project",
        )
        manager = _manager_with_playbooks(pb_system, pb_project)

        # Both are returned by trigger lookup
        matching = manager.get_playbooks_by_trigger("playbook.run.completed")
        assert len(matching) == 2
        ids = {pb.id for pb in matching}
        assert ids == {"system-downstream", "project-downstream"}

        # Caller can further filter by scope
        scopes = {pb.id: pb.scope for pb in matching}
        assert scopes["system-downstream"] == "system"
        assert scopes["project-downstream"] == "project"


# ---------------------------------------------------------------------------
# Integration: Full dispatch simulation
# ---------------------------------------------------------------------------


class TestFullDispatchSimulation:
    """End-to-end tests simulating the future PlaybookDispatcher pattern."""

    async def test_dispatcher_pattern_complete_flow(self, event_bus: EventBus):
        """Simulate the full dispatcher flow:
        1. Playbook A runs and completes
        2. Dispatcher receives completion event
        3. Dispatcher queries PlaybookManager for matching downstream playbooks
        4. Dispatcher checks cooldown + concurrency
        5. Dispatcher launches downstream playbook B
        6. Playbook B completes
        """
        log: list[str] = []
        supervisor = _mock_supervisor()

        # Set up the "downstream" playbook
        pb_summary = _make_playbook(
            playbook_id="post-review-summary",
            triggers=["playbook.run.completed"],
            cooldown_seconds=30,
        )
        manager = _manager_with_playbooks(pb_summary)
        manager.max_concurrent_runs = 5

        async def dispatcher(data: dict) -> None:
            """Reference implementation of the dispatcher pattern."""
            source_id = data["playbook_id"]
            event_type = data.get("_event_type", "")

            # 1. Find matching downstream playbooks
            matching = manager.get_triggerable_playbooks(event_type)

            for pb in matching:
                # 2. Check concurrency
                if not manager.can_start_run():
                    log.append(f"rejected:{pb.id}(concurrency)")
                    continue

                log.append(f"dispatching:{pb.id}(triggered_by={source_id})")

                # 3. Launch the downstream playbook
                graph = _simple_graph(pb.id)
                runner = PlaybookRunner(
                    graph, data, supervisor, event_bus=event_bus
                )

                # Register run for concurrency tracking
                task = asyncio.ensure_future(runner.run())
                registered = manager.register_run(runner.run_id, pb.id, task)

                if registered:
                    result = await task
                    manager.unregister_run(runner.run_id)
                    manager.record_execution(pb.id)
                    log.append(f"completed:{pb.id}(status={result.status})")
                else:
                    log.append(f"rejected:{pb.id}(registration_failed)")
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        event_bus.subscribe("playbook.run.completed", dispatcher)

        # Run the upstream playbook
        graph_a = _simple_graph("code-review")
        runner_a = PlaybookRunner(
            graph_a,
            {"type": "git.commit", "project_id": "myapp"},
            supervisor,
            event_bus=event_bus,
        )
        result_a = await runner_a.run()

        assert result_a.status == "completed"
        assert "dispatching:post-review-summary(triggered_by=code-review)" in log
        assert "completed:post-review-summary(status=completed)" in log

    async def test_multiple_downstream_playbooks_trigger(self, event_bus: EventBus):
        """Multiple playbooks subscribed to the same trigger event all fire."""
        supervisor = _mock_supervisor()
        triggered_ids: list[str] = []

        manager = _manager_with_playbooks(
            _make_playbook(playbook_id="notifier", triggers=["playbook.run.completed"]),
            _make_playbook(playbook_id="archiver", triggers=["playbook.run.completed"]),
            _make_playbook(playbook_id="metrics", triggers=["playbook.run.completed"]),
        )

        async def dispatcher(data: dict) -> None:
            event_type = data.get("_event_type", "")
            matching = manager.get_playbooks_by_trigger(event_type)
            for pb in matching:
                triggered_ids.append(pb.id)

        event_bus.subscribe("playbook.run.completed", dispatcher)

        graph = _simple_graph("upstream-task")
        runner = PlaybookRunner(
            graph, {"type": "test"}, supervisor, event_bus=event_bus
        )
        await runner.run()

        assert sorted(triggered_ids) == ["archiver", "metrics", "notifier"]

    async def test_wildcard_subscription_receives_all_playbook_events(
        self, event_bus: EventBus
    ):
        """A wildcard ('*') subscriber receives both completion and failure events."""
        all_events: list[str] = []
        event_bus.subscribe("*", lambda d: all_events.append(d.get("_event_type", "")))

        supervisor = _mock_supervisor()

        # Successful playbook
        graph_ok = _simple_graph("success-pb")
        runner_ok = PlaybookRunner(
            graph_ok, {"type": "test"}, supervisor, event_bus=event_bus
        )
        await runner_ok.run()

        # Failing playbook
        graph_fail = _failing_graph("fail-pb")
        runner_fail = PlaybookRunner(
            graph_fail, {"type": "test"}, supervisor, event_bus=event_bus
        )
        await runner_fail.run()

        assert "playbook.run.completed" in all_events
        assert "playbook.run.failed" in all_events
