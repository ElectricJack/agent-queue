"""End-to-end verification: reflection playbook extracts patterns and writes insights (roadmap 6.1.5).

Verifies the complete pipeline:
  1. task.completed event emitted with agent_type from resolved profile
  2. PlaybookManager routes to agent-type-scoped reflection playbook
  3. PlaybookRunner executes reflection graph nodes
  4. Supervisor (via tool calls) reads task records and agent-type memory
  5. Supervisor extracts patterns and writes insights via memory_save
  6. Insights are retrievable from agent-type memory

These tests use mock supervisors that simulate LLM responses with tool-use
behaviour (get_task, memory_search, memory_save) to verify the integration
without real LLM calls.

Depends on: 6.1.3 (trigger system), 6.1.1 (reflection playbook template).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.event_bus import EventBus
from src.event_schemas import validate_payload
from src.playbooks.manager import PlaybookManager
from src.playbooks.models import CompiledPlaybook, PlaybookNode
from src.playbooks.runner import PlaybookRunner, RunResult


# ---------------------------------------------------------------------------
# Reflection playbook graph — mirrors vault/agent-types/coding/playbooks/reflection.md
# ---------------------------------------------------------------------------


def _make_reflection_graph(*, playbook_id: str = "coding-reflection") -> dict:
    """Build a compiled playbook graph that mirrors the coding reflection playbook.

    The real playbook has natural-language nodes compiled into a graph.
    This test graph captures the essential structure:

      review_task → extract_insights → write_insights → consolidate → done

    Uses unconditional ``goto`` transitions so tests don't need to handle
    LLM-based transition classification.  See :func:`_make_branching_reflection_graph`
    for the variant with conditional transitions.
    """
    return {
        "id": playbook_id,
        "version": 1,
        "scope": "agent-type:coding",
        "nodes": {
            "review_task": {
                "entry": True,
                "prompt": (
                    "Review the task record for the triggering event. "
                    "Use get_task with the task_id from the event to read the "
                    "task description, status, result summary, and project. "
                    "Use get_task_result to get the detailed outcome. "
                    "Search agent-type memory for existing insights related to "
                    "this task's domain."
                ),
                "goto": "extract_insights",
            },
            "extract_insights": {
                "prompt": (
                    "Based on the task record, extract reusable insights. "
                    "For completed tasks: identify strategies that worked well, "
                    "build/test commands, project conventions, tool usage patterns. "
                    "For failed tasks: identify root cause category and capture "
                    "the failure pattern with enough detail for future avoidance."
                ),
                "goto": "write_insights",
            },
            "write_insights": {
                "prompt": (
                    "For each insight worth preserving, save it to the coding "
                    "agent-type memory using memory_save. Each insight should be "
                    "specific and actionable, tagged appropriately, and scoped "
                    "correctly. Do not save trivial observations."
                ),
                "goto": "consolidate",
            },
            "consolidate": {
                "prompt": (
                    "Review agent-type memory for consolidation opportunities. "
                    "Search for entries related to the insights you just saved. "
                    "If duplicates exist, let memory_save's auto-merge handle them."
                ),
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


def _make_branching_reflection_graph(*, playbook_id: str = "coding-reflection") -> dict:
    """Reflection graph with conditional transitions on the review_task node.

    Uses the ``otherwise`` transition fallback so that when LLM classification
    doesn't match, execution continues to extract_insights.
    """
    return {
        "id": playbook_id,
        "version": 1,
        "scope": "agent-type:coding",
        "nodes": {
            "review_task": {
                "entry": True,
                "prompt": (
                    "Review the task record for the triggering event. "
                    "Determine if the task is trivial or has meaningful content."
                ),
                "transitions": [
                    {
                        "when": "task is trivial or no meaningful patterns to extract",
                        "goto": "done",
                    },
                    {
                        "when": "task has meaningful content to reflect on",
                        "goto": "extract_insights",
                    },
                    {
                        "otherwise": True,
                        "goto": "extract_insights",
                    },
                ],
            },
            "extract_insights": {
                "prompt": "Extract reusable insights from the task.",
                "goto": "write_insights",
            },
            "write_insights": {
                "prompt": "Write insights to agent-type memory using memory_save.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


def _make_compiled_playbook(
    *,
    playbook_id: str = "coding-reflection",
    scope: str = "agent-type:coding",
    cooldown_seconds: int | None = None,
) -> CompiledPlaybook:
    """Create a CompiledPlaybook for the reflection playbook."""
    return CompiledPlaybook(
        id=playbook_id,
        version=1,
        source_hash="reflection-e2e-test",
        triggers=["task.completed", "task.failed"],
        scope=scope,
        cooldown_seconds=cooldown_seconds,
        nodes={
            "review_task": PlaybookNode(
                entry=True,
                prompt="Review the task record.",
                goto="extract_insights",
            ),
            "extract_insights": PlaybookNode(
                prompt="Extract insights from the task.",
                goto="write_insights",
            ),
            "write_insights": PlaybookNode(
                prompt="Write insights to agent-type memory.",
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


def _task_completed_event(
    *,
    task_id: str = "t-100",
    project_id: str = "my-project",
    title: str = "Implement async retry logic",
    agent_id: str = "agent-1",
    agent_type: str = "coding",
) -> dict:
    """Build a task.completed event payload."""
    return {
        "task_id": task_id,
        "project_id": project_id,
        "title": title,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }


def _task_failed_event(
    *,
    task_id: str = "t-200",
    project_id: str = "my-project",
    title: str = "Fix database migration",
    agent_type: str = "coding",
    error: str = "Max retries exhausted",
) -> dict:
    """Build a task.failed event payload."""
    return {
        "task_id": task_id,
        "project_id": project_id,
        "title": title,
        "status": "BLOCKED",
        "context": "max_retries",
        "error": error,
        "agent_type": agent_type,
    }


# ---------------------------------------------------------------------------
# Mock supervisor that simulates tool-use for reflection
# ---------------------------------------------------------------------------


class ReflectionMockSupervisor:
    """Mock Supervisor that simulates the LLM's behaviour during reflection.

    Tracks all chat() calls and simulates responses that reference tool usage
    for get_task, memory_search, and memory_save — without actually calling
    tools. This lets us verify the runner sends the right prompts and receives
    the right responses at each node.
    """

    def __init__(
        self,
        *,
        task_record: dict | None = None,
        existing_insights: list[str] | None = None,
        extracted_insights: list[str] | None = None,
    ):
        self.chat_calls: list[dict] = []
        self._task_record = task_record or {
            "id": "t-100",
            "title": "Implement async retry logic",
            "status": "DONE",
            "description": "Add retry with exponential backoff to HTTP client",
            "project_id": "my-project",
            "profile_id": "coding",
        }
        self._existing_insights = existing_insights or []
        self._extracted_insights = extracted_insights or [
            "Use tenacity library for retry logic — cleaner than manual loops",
            "Always set max_retries with jitter to avoid thundering herd",
        ]
        self._call_index = 0
        # PlaybookRunner reads supervisor.config.chat_provider.playbook_max_tokens
        # (runner.py:1537) — provide a minimal stand-in.
        from types import SimpleNamespace

        self.config = SimpleNamespace(
            chat_provider=SimpleNamespace(playbook_max_tokens=2048)
        )

    async def chat(self, **kwargs) -> str:
        """Record the call and return a simulated response."""
        self.chat_calls.append(kwargs)
        node_hint = kwargs.get("user_name", "")
        self._call_index += 1

        if "review_task" in node_hint:
            return self._review_response()
        elif "extract_insights" in node_hint:
            return self._extract_response()
        elif "write_insights" in node_hint:
            return self._write_response()
        elif "consolidate" in node_hint:
            return self._consolidate_response()
        else:
            return "Done."

    def _review_response(self) -> str:
        """Simulate reviewing the task record."""
        return (
            f"I've reviewed task {self._task_record['id']}: "
            f"'{self._task_record['title']}'. "
            f"Status: {self._task_record['status']}. "
            f"Description: {self._task_record['description']}. "
            f"I called get_task(task_id='{self._task_record['id']}') and "
            f"searched agent-type memory for related insights. "
            f"Found {len(self._existing_insights)} existing insights. "
            "This task has meaningful content to reflect on."
        )

    def _extract_response(self) -> str:
        """Simulate extracting insights."""
        insights_text = "\n".join(
            f"- Insight {i + 1}: {ins}" for i, ins in enumerate(self._extracted_insights)
        )
        return (
            "Extracted the following insights from this task:\n"
            f"{insights_text}\n\n"
            "These represent reusable patterns for future coding tasks."
        )

    def _write_response(self) -> str:
        """Simulate writing insights to memory."""
        saves = []
        for ins in self._extracted_insights:
            saves.append(
                f"Saved insight: '{ins}' "
                "via memory_save(project_id='my-project', "
                "content=..., tags=['coding', 'async', '#provisional'], "
                "scope='agenttype_coding')"
            )
        return "\n".join(saves) + "\n\nAll insights written to agent-type memory."

    def _consolidate_response(self) -> str:
        """Simulate memory consolidation."""
        return (
            "Searched agent-type memory for related entries. "
            "No duplicates found — all insights are novel. "
            "Memory consolidation complete."
        )


class ToolTrackingSupervisor:
    """Mock Supervisor that tracks simulated tool calls.

    Provides a structured way to verify tool-call patterns during
    reflection by recording which tools would be called at each node.
    The graph used with this supervisor must use ``goto`` transitions
    (not conditional), so no transition-classification calls occur.
    """

    def __init__(self):
        self.chat_calls: list[dict] = []
        self.tool_calls: list[dict] = []
        # PlaybookRunner reads supervisor.config.chat_provider.playbook_max_tokens
        # (runner.py:1537) — provide a minimal stand-in.
        from types import SimpleNamespace

        self.config = SimpleNamespace(
            chat_provider=SimpleNamespace(playbook_max_tokens=2048)
        )
        self._node_tool_mapping: dict[str, list[dict]] = {
            "review_task": [
                {"tool": "get_task", "args": {"task_id": "t-100"}},
                {"tool": "get_task_result", "args": {"task_id": "t-100"}},
                {
                    "tool": "memory_search",
                    "args": {
                        "project_id": "my-project",
                        "query": "async retry patterns",
                        "scope": "agenttype_coding",
                    },
                },
            ],
            "extract_insights": [],  # Pure reasoning, no tool calls
            "write_insights": [
                {
                    "tool": "memory_save",
                    "args": {
                        "project_id": "my-project",
                        "content": "Use tenacity for retry logic",
                        "tags": ["coding", "async", "#provisional"],
                        "scope": "agenttype_coding",
                    },
                },
                {
                    "tool": "memory_save",
                    "args": {
                        "project_id": "my-project",
                        "content": "Set max_retries with jitter",
                        "tags": ["coding", "reliability", "#provisional"],
                        "scope": "agenttype_coding",
                    },
                },
            ],
            "consolidate": [
                {
                    "tool": "memory_search",
                    "args": {
                        "project_id": "my-project",
                        "query": "retry patterns reliability",
                        "scope": "agenttype_coding",
                    },
                },
            ],
        }

    async def chat(self, **kwargs) -> str:
        self.chat_calls.append(kwargs)
        node_hint = kwargs.get("user_name", "")

        # Extract node name from "playbook-runner:node_name"
        node_name = node_hint.split(":", 1)[-1] if ":" in node_hint else ""

        # Record simulated tool calls for this node
        if node_name in self._node_tool_mapping:
            for tc in self._node_tool_mapping[node_name]:
                self.tool_calls.append({**tc, "node": node_name})

        return f"Completed {node_name} step."

    @property
    def tools_by_node(self) -> dict[str, list[str]]:
        """Group tool names by which node invoked them."""
        result: dict[str, list[str]] = {}
        for tc in self.tool_calls:
            node = tc["node"]
            result.setdefault(node, []).append(tc["tool"])
        return result

    @property
    def memory_saves(self) -> list[dict]:
        """All memory_save calls."""
        return [tc for tc in self.tool_calls if tc["tool"] == "memory_save"]


# ---------------------------------------------------------------------------
# Tests: End-to-end trigger → runner execution
# ---------------------------------------------------------------------------


class TestReflectionE2ETriggerToRunner:
    """Verify the full chain: event → PlaybookManager trigger → PlaybookRunner execution."""

    @pytest.fixture
    def event_bus(self) -> EventBus:
        return EventBus(validate_events=False)

    async def test_trigger_dispatches_to_runner(self, event_bus: EventBus) -> None:
        """Event → trigger → on_trigger callback → PlaybookRunner.run() completes."""
        supervisor = ReflectionMockSupervisor()
        runner_results: list[RunResult] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            """Simulate the production on_trigger: create a runner and execute."""
            graph = _make_reflection_graph(playbook_id=playbook.id)
            runner = PlaybookRunner(graph, data, supervisor)
            result = await runner.run()
            runner_results.append(result)

        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_compiled_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        # Emit a task.completed event with agent_type=coding
        await event_bus.emit("task.completed", _task_completed_event())

        # Runner should have completed successfully
        assert len(runner_results) == 1
        result = runner_results[0]
        assert result.status == "completed"
        assert result.tokens_used > 0

    async def test_runner_receives_event_data_in_seed(self, event_bus: EventBus) -> None:
        """PlaybookRunner seeds conversation with the trigger event data."""
        supervisor = ReflectionMockSupervisor()
        runner_messages: list[list[dict]] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            graph = _make_reflection_graph(playbook_id=playbook.id)
            runner = PlaybookRunner(graph, data, supervisor)
            await runner.run()
            runner_messages.append(list(runner.messages))

        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_compiled_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        event = _task_completed_event(task_id="t-seed-test")
        await event_bus.emit("task.completed", event)

        # First message should be the seed containing event data
        assert len(runner_messages) == 1
        seed = runner_messages[0][0]
        assert seed["role"] == "user"
        assert "t-seed-test" in seed["content"]
        assert "my-project" in seed["content"]
        assert "coding" in seed["content"]

    async def test_trigger_does_not_fire_for_wrong_agent_type(self, event_bus: EventBus) -> None:
        """Coding reflection playbook does NOT run for agent_type=review."""
        runner_results: list[RunResult] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            runner_results.append(
                RunResult(
                    run_id="should-not-happen",
                    status="completed",
                    node_trace=[],
                    tokens_used=0,
                )
            )

        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_compiled_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", _task_completed_event(agent_type="review"))

        assert len(runner_results) == 0

    async def test_trigger_fires_on_task_failed(self, event_bus: EventBus) -> None:
        """Reflection playbook also triggers on task.failed events."""
        supervisor = ReflectionMockSupervisor(
            task_record={
                "id": "t-200",
                "title": "Fix database migration",
                "status": "BLOCKED",
                "description": "Migration failed due to missing column",
                "project_id": "my-project",
                "profile_id": "coding",
            },
            extracted_insights=[
                "Always run alembic check before applying migrations",
            ],
        )
        runner_results: list[RunResult] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            graph = _make_reflection_graph(playbook_id=playbook.id)
            runner = PlaybookRunner(graph, data, supervisor)
            result = await runner.run()
            runner_results.append(result)

        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_compiled_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.failed", _task_failed_event())

        assert len(runner_results) == 1
        assert runner_results[0].status == "completed"


# ---------------------------------------------------------------------------
# Tests: Runner walks reflection graph correctly
# ---------------------------------------------------------------------------


class TestReflectionRunnerGraphWalk:
    """Verify PlaybookRunner walks the reflection graph end-to-end."""

    async def test_all_reflection_nodes_executed(self) -> None:
        """Runner visits review_task → extract_insights → write_insights → consolidate → done."""
        supervisor = ReflectionMockSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        result = await runner.run()

        assert result.status == "completed"
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert executed_nodes == [
            "review_task",
            "extract_insights",
            "write_insights",
            "consolidate",
        ]

    async def test_each_node_receives_accumulated_history(self) -> None:
        """Subsequent nodes see prior-node outputs in a structured summary.

        Post-refactor the runner builds fresh per-node context rather than
        accumulating the raw transcript: every non-entry node gets
        ``[seed, prior-step-results, ack]`` regardless of how far along the
        graph is.  The ``prior-step-results`` block grows as more nodes run.
        """
        supervisor = ReflectionMockSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        # Should have 4 chat calls (one per non-terminal node).
        assert len(supervisor.chat_calls) == 4

        # Entry node: history = [seed message]
        first_history = supervisor.chat_calls[0]["history"]
        assert len(first_history) == 1
        assert "Event received" in first_history[0]["content"]

        # Every subsequent node: history = [seed, prior-results, ack]
        for call in supervisor.chat_calls[1:]:
            history = call["history"]
            assert len(history) == 3
            assert "Prior Step Results" in history[1]["content"]
            assert history[2]["role"] == "assistant"

        # Prior-results block grows as nodes complete.
        second_results = supervisor.chat_calls[1]["history"][1]["content"]
        third_results = supervisor.chat_calls[2]["history"][1]["content"]
        fourth_results = supervisor.chat_calls[3]["history"][1]["content"]
        assert len(second_results) < len(third_results) < len(fourth_results)

    async def test_event_data_propagated_through_graph(self) -> None:
        """The trigger event data (task_id, project_id, agent_type) is in the seed."""
        supervisor = ReflectionMockSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event(
            task_id="t-prop-test",
            project_id="myapp",
            agent_type="coding",
        )

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        # The seed message is the first in history for every node call
        for call in supervisor.chat_calls:
            seed = call["history"][0]["content"]
            assert "t-prop-test" in seed
            assert "myapp" in seed
            assert "coding" in seed

    async def test_node_prompts_contain_reflection_instructions(self) -> None:
        """Each node's prompt text reaches the Supervisor via the text kwarg."""
        supervisor = ReflectionMockSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        # Map prompts by node name from user_name="playbook-runner:<node>"
        prompts_by_node: dict[str, str] = {}
        for call in supervisor.chat_calls:
            user_name = call.get("user_name", "")
            if ":" in user_name:
                node_name = user_name.split(":", 1)[1]
                prompts_by_node[node_name] = call["text"]

        # review_task prompt references get_task
        assert "get_task" in prompts_by_node["review_task"]
        # extract_insights prompt references pattern extraction
        extract_prompt = prompts_by_node["extract_insights"].lower()
        assert "extract" in extract_prompt or "insight" in extract_prompt
        # write_insights prompt references memory_save
        assert "memory_save" in prompts_by_node["write_insights"]
        # consolidate prompt references consolidation
        assert "consolidat" in prompts_by_node["consolidate"].lower()


# ---------------------------------------------------------------------------
# Tests: Tool call patterns during reflection
# ---------------------------------------------------------------------------


class TestReflectionToolCallPatterns:
    """Verify the expected tool-call patterns during reflection execution."""

    async def test_review_node_uses_get_task_and_memory_search(self) -> None:
        """The review_task node should invoke get_task and memory_search."""
        supervisor = ToolTrackingSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools_by_node = supervisor.tools_by_node
        assert "get_task" in tools_by_node.get("review_task", [])
        assert "memory_search" in tools_by_node.get("review_task", [])

    async def test_write_node_uses_memory_save(self) -> None:
        """The write_insights node should invoke memory_save."""
        supervisor = ToolTrackingSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools_by_node = supervisor.tools_by_node
        assert "memory_save" in tools_by_node.get("write_insights", [])

    async def test_at_least_one_insight_saved_per_reflection(self) -> None:
        """Each reflection run should produce at least one memory_save call."""
        supervisor = ToolTrackingSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        assert len(supervisor.memory_saves) >= 1

    async def test_consolidate_node_searches_memory(self) -> None:
        """The consolidate node should search memory for related entries."""
        supervisor = ToolTrackingSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools_by_node = supervisor.tools_by_node
        assert "memory_search" in tools_by_node.get("consolidate", [])

    async def test_memory_save_targets_agent_type_scope(self) -> None:
        """memory_save calls should target the agenttype_coding scope."""
        supervisor = ToolTrackingSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        for save in supervisor.memory_saves:
            assert save["args"].get("scope") == "agenttype_coding"


# ---------------------------------------------------------------------------
# Tests: Multi-task insight accumulation
# ---------------------------------------------------------------------------


class TestMultiTaskInsightAccumulation:
    """Verify that multiple task completions produce accumulated insights."""

    async def test_five_tasks_produce_at_least_three_insights(self) -> None:
        """After 5 completed tasks, at least 3 distinct insights should be saved.

        This validates the roadmap checkpoint:
        "After 5 completed tasks: at least 3 insights extracted and saved
        to agent-type memory collection"
        """
        all_saves: list[dict] = []

        tasks = [
            {
                "task_id": f"t-{i}",
                "title": title,
                "insights": insights,
            }
            for i, (title, insights) in enumerate(
                [
                    (
                        "Add retry logic to HTTP client",
                        ["Use tenacity for retry with backoff"],
                    ),
                    (
                        "Fix race condition in async handler",
                        ["Always use asyncio.Lock for shared state"],
                    ),
                    (
                        "Optimize database queries",
                        ["Add select_related for N+1 query prevention"],
                    ),
                    (
                        "Refactor test suite",
                        [],  # Trivial task — no insights expected
                    ),
                    (
                        "Implement WebSocket reconnection",
                        ["Use exponential backoff with jitter for reconnect"],
                    ),
                ]
            )
        ]

        for task in tasks:
            supervisor = ToolTrackingSupervisor()
            # Adjust mock to produce task-specific tool calls
            supervisor._node_tool_mapping["write_insights"] = [
                {
                    "tool": "memory_save",
                    "args": {
                        "project_id": "my-project",
                        "content": insight,
                        "tags": ["coding", "#provisional"],
                        "scope": "agenttype_coding",
                    },
                }
                for insight in task["insights"]
            ]

            graph = _make_reflection_graph()
            event = _task_completed_event(
                task_id=task["task_id"],
                title=task["title"],
            )

            runner = PlaybookRunner(graph, event, supervisor)
            result = await runner.run()
            assert result.status == "completed"

            all_saves.extend(supervisor.memory_saves)

        # At least 3 insights from 5 tasks (one task was trivial with 0 insights)
        assert len(all_saves) >= 3
        # Verify each save has content
        for save in all_saves:
            assert save["args"]["content"]

    async def test_each_task_runs_full_reflection_cycle(self) -> None:
        """Each task.completed should run the full reflection graph."""
        for i in range(3):
            supervisor = ReflectionMockSupervisor(
                task_record={
                    "id": f"t-{i}",
                    "title": f"Task {i}",
                    "status": "DONE",
                    "description": f"Description for task {i}",
                    "project_id": "my-project",
                    "profile_id": "coding",
                },
            )
            graph = _make_reflection_graph()
            event = _task_completed_event(task_id=f"t-{i}", title=f"Task {i}")

            runner = PlaybookRunner(graph, event, supervisor)
            result = await runner.run()

            assert result.status == "completed"
            assert len(result.node_trace) == 4  # 4 non-terminal nodes


# ---------------------------------------------------------------------------
# Tests: Event payload validates correctly for reflection
# ---------------------------------------------------------------------------


class TestReflectionEventPayloads:
    """Verify event payloads used in reflection pass schema validation."""

    def test_completed_event_with_full_reflection_fields_validates(self) -> None:
        """task.completed with all fields needed for reflection passes validation."""
        errors = validate_payload("task.completed", _task_completed_event())
        assert errors == []

    def test_failed_event_with_full_reflection_fields_validates(self) -> None:
        """task.failed with all fields needed for reflection passes validation."""
        errors = validate_payload("task.failed", _task_failed_event())
        assert errors == []

    def test_event_payload_has_task_id_for_get_task(self) -> None:
        """The event payload must contain task_id so playbook can call get_task."""
        event = _task_completed_event(task_id="t-abc")
        assert "task_id" in event
        assert event["task_id"] == "t-abc"

    def test_event_payload_has_project_id_for_memory_scoping(self) -> None:
        """The event payload must contain project_id for memory scope resolution."""
        event = _task_completed_event(project_id="my-app")
        assert "project_id" in event
        assert event["project_id"] == "my-app"

    def test_event_payload_has_agent_type_for_scope_matching(self) -> None:
        """The event payload must contain agent_type for playbook scope matching."""
        event = _task_completed_event(agent_type="coding")
        assert "agent_type" in event
        assert event["agent_type"] == "coding"


# ---------------------------------------------------------------------------
# Tests: Conversation context carries reflection-specific data
# ---------------------------------------------------------------------------


class TestReflectionConversationContext:
    """Verify the runner's conversation history carries all needed context."""

    async def test_seed_message_contains_task_id(self) -> None:
        """The seed message includes the task_id so the LLM can call get_task."""
        supervisor = ReflectionMockSupervisor()
        event = _task_completed_event(task_id="t-ctx-1")
        runner = PlaybookRunner(_make_reflection_graph(), event, supervisor)
        await runner.run()

        seed = runner.messages[0]["content"]
        assert "t-ctx-1" in seed

    async def test_seed_message_contains_project_id(self) -> None:
        """The seed message includes the project_id for memory scoping."""
        supervisor = ReflectionMockSupervisor()
        event = _task_completed_event(project_id="webapp")
        runner = PlaybookRunner(_make_reflection_graph(), event, supervisor)
        await runner.run()

        seed = runner.messages[0]["content"]
        assert "webapp" in seed

    async def test_seed_message_contains_agent_type(self) -> None:
        """The seed message includes agent_type for scope context."""
        supervisor = ReflectionMockSupervisor()
        event = _task_completed_event(agent_type="coding")
        runner = PlaybookRunner(_make_reflection_graph(), event, supervisor)
        await runner.run()

        seed = runner.messages[0]["content"]
        assert "coding" in seed

    async def test_review_response_carries_to_extract_node(self) -> None:
        """The extract_insights node sees the review_task response in its history."""
        supervisor = ReflectionMockSupervisor()
        runner = PlaybookRunner(
            _make_reflection_graph(),
            _task_completed_event(),
            supervisor,
        )
        await runner.run()

        # The runner builds fresh per-node context rather than accumulating
        # the raw transcript: prior outputs land in a structured "Prior Step
        # Results" block at history[1]. See runner_context._build_node_context.
        extract_history = supervisor.chat_calls[1]["history"]
        prior_results = extract_history[1]["content"]
        assert "reviewed task" in prior_results.lower() or "t-100" in prior_results

    async def test_extract_response_carries_to_write_node(self) -> None:
        """The write_insights node sees extracted insights in its history."""
        supervisor = ReflectionMockSupervisor()
        runner = PlaybookRunner(
            _make_reflection_graph(),
            _task_completed_event(),
            supervisor,
        )
        await runner.run()

        # Find the write_insights call by user_name
        write_call = None
        for call in supervisor.chat_calls:
            if "write_insights" in call.get("user_name", ""):
                write_call = call
                break
        assert write_call is not None

        # History should contain the extract_insights response somewhere
        write_history = write_call["history"]
        history_text = " ".join(msg["content"] for msg in write_history)
        assert "insight" in history_text.lower()


# ---------------------------------------------------------------------------
# Tests: Dry-run mode for reflection playbook
# ---------------------------------------------------------------------------


class TestReflectionDryRun:
    """Verify reflection playbook can be dry-run for validation."""

    async def test_dry_run_walks_entire_graph(self) -> None:
        """Dry-run mode walks the reflection graph without LLM calls."""
        graph = _make_reflection_graph()
        event = _task_completed_event()

        result = await PlaybookRunner.dry_run(graph, event)

        assert result.status == "completed"
        executed_nodes = [t["node_id"] for t in result.node_trace]
        # Dry-run follows the 'otherwise' fallback for conditional transitions
        assert "review_task" in executed_nodes
        assert len(executed_nodes) >= 1  # At least the entry node

    async def test_dry_run_produces_zero_real_tokens(self) -> None:
        """Dry-run mode doesn't consume real tokens."""
        graph = _make_reflection_graph()
        event = _task_completed_event()

        result = await PlaybookRunner.dry_run(graph, event)

        # Tokens reported are 0 because no real LLM calls are made
        # (the runner may still track estimated tokens from simulated responses)
        assert result.status == "completed"


# ---------------------------------------------------------------------------
# Tests: Reflection playbook scope matching
# ---------------------------------------------------------------------------


class TestReflectionScopeMatching:
    """Verify scope matching works correctly for agent-type reflection playbooks."""

    @pytest.fixture
    def event_bus(self) -> EventBus:
        return EventBus(validate_events=False)

    async def test_coding_reflection_matches_coding_agent_type(self, event_bus: EventBus) -> None:
        """scope=agent-type:coding matches events with agent_type=coding."""
        triggered: list[str] = []

        async def on_trigger(playbook, data):
            triggered.append(playbook.id)

        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_compiled_playbook(scope="agent-type:coding")
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", _task_completed_event(agent_type="coding"))
        assert "coding-reflection" in triggered

    async def test_coding_reflection_ignores_different_agent_type(
        self, event_bus: EventBus
    ) -> None:
        """scope=agent-type:coding does NOT match events with agent_type=review."""
        triggered: list[str] = []

        async def on_trigger(playbook, data):
            triggered.append(playbook.id)

        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_compiled_playbook(scope="agent-type:coding")
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", _task_completed_event(agent_type="review"))
        assert len(triggered) == 0

    async def test_multiple_agent_type_playbooks_only_matching_runs(
        self, event_bus: EventBus
    ) -> None:
        """When both coding and review reflection exist, only the matching one runs."""
        results: list[tuple[str, str]] = []

        async def on_trigger(playbook, data):
            results.append((playbook.id, data.get("agent_type", "")))

        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        coding_pb = _make_compiled_playbook(
            playbook_id="coding-reflection",
            scope="agent-type:coding",
        )
        review_pb = _make_compiled_playbook(
            playbook_id="review-reflection",
            scope="agent-type:review",
        )

        for pb in [coding_pb, review_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", _task_completed_event(agent_type="coding"))

        assert len(results) == 1
        assert results[0] == ("coding-reflection", "coding")


# ---------------------------------------------------------------------------
# Tests: Reflection with DB persistence
# ---------------------------------------------------------------------------


class TestReflectionDBPersistence:
    """Verify run state is persisted to the database during reflection."""

    async def test_playbook_run_created_in_db(self) -> None:
        """A PlaybookRun record is created at the start of execution."""
        db = AsyncMock()
        db.create_playbook_run = AsyncMock()
        db.update_playbook_run = AsyncMock()
        db.get_daily_playbook_token_usage = AsyncMock(return_value=0)

        supervisor = ReflectionMockSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor, db=db)
        result = await runner.run()

        assert result.status == "completed"
        db.create_playbook_run.assert_called_once()
        run_arg = db.create_playbook_run.call_args[0][0]
        assert run_arg.playbook_id == "coding-reflection"
        assert "t-100" in run_arg.trigger_event

    async def test_run_state_updated_after_each_node(self) -> None:
        """DB is updated with conversation history after each node."""
        db = AsyncMock()
        db.create_playbook_run = AsyncMock()
        db.update_playbook_run = AsyncMock()
        db.get_daily_playbook_token_usage = AsyncMock(return_value=0)

        supervisor = ReflectionMockSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor, db=db)
        result = await runner.run()

        assert result.status == "completed"
        # 4 intermediate updates (one per node) + 1 final completion update = 5
        assert db.update_playbook_run.call_count == 5

    async def test_trigger_event_stored_in_run(self) -> None:
        """The trigger event data is stored in the PlaybookRun for later access."""
        db = AsyncMock()
        db.create_playbook_run = AsyncMock()
        db.update_playbook_run = AsyncMock()
        db.get_daily_playbook_token_usage = AsyncMock(return_value=0)

        supervisor = ReflectionMockSupervisor()
        graph = _make_reflection_graph()
        event = _task_completed_event(task_id="t-persist")

        runner = PlaybookRunner(graph, event, supervisor, db=db)
        await runner.run()

        run_arg = db.create_playbook_run.call_args[0][0]
        stored_event = json.loads(run_arg.trigger_event)
        assert stored_event["task_id"] == "t-persist"
        assert stored_event["agent_type"] == "coding"
        assert stored_event["project_id"] == "my-project"


# ---------------------------------------------------------------------------
# Tests: Reflection playbook vault template structure
# ---------------------------------------------------------------------------


class TestReflectionPlaybookTemplate:
    """Verify the vault reflection playbook template has the required structure."""

    @pytest.fixture
    def playbook_source(self) -> str:
        """Read the reflection playbook markdown from the vault."""
        import os

        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "src",
            "prompts",
            "default_agent_type_playbooks",
            "claude-opus",
            "reflection.md",
        )
        with open(path) as f:
            return f.read()

    def test_playbook_has_yaml_frontmatter(self, playbook_source: str) -> None:
        """The playbook starts with YAML frontmatter."""
        assert playbook_source.startswith("---")

    def test_playbook_has_coding_reflection_id(self, playbook_source: str) -> None:
        """The playbook ID is 'coding-reflection'."""
        assert "id: coding-reflection" in playbook_source

    def test_playbook_triggers_on_task_completed(self, playbook_source: str) -> None:
        """The playbook triggers on task.completed."""
        assert "task.completed" in playbook_source

    def test_playbook_triggers_on_task_failed(self, playbook_source: str) -> None:
        """The playbook triggers on task.failed."""
        assert "task.failed" in playbook_source

    def test_playbook_scope_is_agent_type_coding(self, playbook_source: str) -> None:
        """The playbook scope targets agent-type:coding."""
        assert "scope: agent-type:coding" in playbook_source

    def test_playbook_has_cooldown(self, playbook_source: str) -> None:
        """The playbook has a cooldown period configured."""
        assert "cooldown:" in playbook_source

    def test_playbook_mentions_get_task(self, playbook_source: str) -> None:
        """The playbook template instructs reading the task record."""
        # The playbook may not literally say "get_task" but should reference
        # reading the task record
        lower = playbook_source.lower()
        assert "task record" in lower or "task description" in lower

    def test_playbook_mentions_memory_store(self, playbook_source: str) -> None:
        """The playbook template instructs saving insights via memory_store."""
        assert "memory_store" in playbook_source

    def test_playbook_mentions_insight_extraction(self, playbook_source: str) -> None:
        """The playbook template describes extracting patterns/insights."""
        lower = playbook_source.lower()
        assert "insight" in lower or "pattern" in lower

    def test_playbook_mentions_consolidation(self, playbook_source: str) -> None:
        """The playbook template describes memory consolidation."""
        lower = playbook_source.lower()
        assert "consolidat" in lower

    def test_playbook_has_skip_conditions(self, playbook_source: str) -> None:
        """The playbook template describes when to skip trivial tasks."""
        lower = playbook_source.lower()
        assert "skip" in lower or "trivial" in lower

    def test_playbook_describes_tagging(self, playbook_source: str) -> None:
        """The playbook template describes how to tag insights."""
        assert "#verified" in playbook_source or "#provisional" in playbook_source

    def test_playbook_covers_failed_tasks(self, playbook_source: str) -> None:
        """The playbook template has a section for failed task analysis."""
        lower = playbook_source.lower()
        assert "failed" in lower
        assert "root cause" in lower


# ---------------------------------------------------------------------------
# Tests: Runner user_name identifies playbook context
# ---------------------------------------------------------------------------


class TestReflectionRunnerIdentity:
    """Verify the runner identifies itself to the Supervisor correctly."""

    async def test_supervisor_calls_identify_playbook_runner(self) -> None:
        """Each supervisor.chat() call has user_name='playbook-runner:<node>'."""
        supervisor = ReflectionMockSupervisor()
        runner = PlaybookRunner(
            _make_reflection_graph(),
            _task_completed_event(),
            supervisor,
        )
        await runner.run()

        for call in supervisor.chat_calls:
            assert call["user_name"].startswith("playbook-runner:")

    async def test_node_names_in_user_name(self) -> None:
        """User names match the node IDs from the reflection graph."""
        supervisor = ReflectionMockSupervisor()
        runner = PlaybookRunner(
            _make_reflection_graph(),
            _task_completed_event(),
            supervisor,
        )
        await runner.run()

        # Filter to only playbook-runner calls (skip any transition-classification calls)
        node_calls = [
            call
            for call in supervisor.chat_calls
            if call.get("user_name", "").startswith("playbook-runner:")
        ]
        node_names = [call["user_name"].split(":", 1)[1] for call in node_calls]
        assert node_names == [
            "review_task",
            "extract_insights",
            "write_insights",
            "consolidate",
        ]
