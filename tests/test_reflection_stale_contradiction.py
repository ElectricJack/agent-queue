"""Tests: internal handler behaviour for stale memory flagging and contradiction surfacing.

Roadmap 6.5.5 — depends on 6.5.3 (stale detection), 6.5.4 (contradiction detection),
and 6.1.1 (reflection playbook).

Verifies:
  1. An extended reflection graph walks through contradiction surfacing and stale
     flagging nodes in the correct order
  2. Tool-call patterns match expectations:
       - surface_contradictions node calls memory_health, memory_get, memory_search
       - flag_stale node calls memory_stale, memory_delete / memory_update
  3. Skip behaviour: when no contradictions or stale memories exist, nodes complete
     quickly without taking action
  4. Node prompts (in the test fixture graph) reference the correct tools
  5. DB persistence and dry-run work with the extended graph
  6. memory_health and memory_stale return structures matching expectations

**Status:** xfail at the module level. The underlying memory tools
(memory_health, memory_stale, memory_delete, memory_update) are already
implemented, but the extended reflection playbook (the 6-node graph this
file exercises) has not been authored yet — the live playbook at
vault/agent-types/coding/playbooks/reflection.md is the 4-node v1.
Remove the xfail marker once the playbook is written and compiled.
See docs/specs/design/memory-consolidation.md.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.playbooks.runner import PlaybookRunner

pytestmark = pytest.mark.xfail(
    reason=(
        "Extended reflection playbook (stale/contradiction nodes) not yet "
        "compiled — tools exist but no playbook file drives them. "
        "See docs/specs/design/memory-consolidation.md."
    ),
    strict=False,
)


# ---------------------------------------------------------------------------
# Reflection graph WITH stale + contradiction nodes (6.5.5 extension)
# ---------------------------------------------------------------------------


def _make_extended_reflection_graph(*, playbook_id: str = "coding-reflection") -> dict:
    """Build a compiled playbook graph that mirrors the updated coding reflection playbook.

    Extends the baseline graph with two new nodes after consolidation:

      review_task → extract_insights → write_insights → consolidate
        → surface_contradictions → flag_stale → done
    """
    return {
        "id": playbook_id,
        "version": 2,
        "scope": "agent-type:coding",
        "nodes": {
            "review_task": {
                "entry": True,
                "prompt": (
                    "Review the task record for the triggering event. "
                    "Use get_task with the task_id from the event to read the "
                    "task description, status, result summary, and project. "
                    "Search agent-type memory for existing insights related to "
                    "this task's domain."
                ),
                "goto": "extract_insights",
            },
            "extract_insights": {
                "prompt": (
                    "Based on the task record, extract reusable insights. "
                    "For completed tasks: identify strategies that worked well. "
                    "For failed tasks: identify root cause category."
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
                    "Merge duplicates, update outdated insights, promote "
                    "cross-project patterns."
                ),
                "goto": "surface_contradictions",
            },
            "surface_contradictions": {
                "prompt": (
                    "Call memory_health for the project to check for memories "
                    "tagged #contested. For each contested memory, read the full "
                    "content via memory_get, search for the opposing entry via "
                    "memory_search, and evaluate whether this task's outcome "
                    "resolves the contradiction. If confirmed, memory_update the "
                    "winner (remove #contested, add #verified) and memory_delete "
                    "the refuted entry. If unresolved, leave both tagged #contested."
                ),
                "goto": "flag_stale",
            },
            "flag_stale": {
                "prompt": (
                    "Call memory_stale for the project to find documents not "
                    "retrieved recently. For each stale entry, decide: delete "
                    "(clearly outdated/wrong), refresh (valid but stale content), "
                    "or keep (valid but rarely needed). Use memory_delete or "
                    "memory_update accordingly. Limit review to top 10 candidates."
                ),
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


def _task_completed_event(
    *,
    task_id: str = "t-100",
    project_id: str = "my-project",
    title: str = "Implement async retry logic",
    agent_id: str = "agent-1",
    agent_type: str = "coding",
) -> dict:
    return {
        "task_id": task_id,
        "project_id": project_id,
        "title": title,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }


# ---------------------------------------------------------------------------
# Mock supervisors
# ---------------------------------------------------------------------------


class ExtendedReflectionMockSupervisor:
    """Mock Supervisor that simulates all nodes including contradiction + stale."""

    def __init__(
        self,
        *,
        contradiction_count: int = 0,
        stale_count: int = 0,
    ):
        self.chat_calls: list[dict] = []
        self._contradiction_count = contradiction_count
        self._stale_count = stale_count

    async def chat(self, **kwargs) -> str:
        self.chat_calls.append(kwargs)
        node_hint = kwargs.get("user_name", "")

        if "review_task" in node_hint:
            return "Reviewed task t-100: 'Implement async retry logic'. Status: DONE."
        elif "extract_insights" in node_hint:
            return "Extracted insights: Use tenacity for retry logic."
        elif "write_insights" in node_hint:
            return "Saved insight via memory_save."
        elif "consolidate" in node_hint:
            return "No duplicates found. Consolidation complete."
        elif "surface_contradictions" in node_hint:
            return self._contradictions_response()
        elif "flag_stale" in node_hint:
            return self._stale_response()
        else:
            return "Done."

    def _contradictions_response(self) -> str:
        if self._contradiction_count == 0:
            return (
                "Called memory_health for project. contradiction_count=0. "
                "No contested memories found. Skipping."
            )
        return (
            f"Called memory_health for project. contradiction_count={self._contradiction_count}. "
            "Found contested memories. Read full content via memory_get, searched "
            "for opposing entries via memory_search. Evaluated contradictions in "
            "light of current task outcome. Resolved 1 contradiction: updated "
            "winner via memory_update (removed #contested, added #verified), "
            "deleted refuted entry via memory_delete."
        )

    def _stale_response(self) -> str:
        if self._stale_count == 0:
            return (
                "Called memory_stale for project. No stale memories found. Skipping."
            )
        return (
            f"Called memory_stale for project. Found {self._stale_count} stale entries. "
            "Reviewed top candidates: deleted 1 outdated entry (referenced removed API), "
            "refreshed 1 entry with updated content, kept 2 rarely-needed but valid entries."
        )


class ExtendedToolTrackingSupervisor:
    """Mock Supervisor that tracks simulated tool calls for all nodes."""

    def __init__(
        self,
        *,
        has_contradictions: bool = False,
        has_stale: bool = False,
    ):
        self.chat_calls: list[dict] = []
        self.tool_calls: list[dict] = []
        self._has_contradictions = has_contradictions
        self._has_stale = has_stale

        self._node_tool_mapping: dict[str, list[dict]] = {
            "review_task": [
                {"tool": "get_task", "args": {"task_id": "t-100"}},
                {
                    "tool": "memory_search",
                    "args": {
                        "project_id": "my-project",
                        "query": "async retry patterns",
                        "scope": "agenttype_coding",
                    },
                },
            ],
            "extract_insights": [],
            "write_insights": [
                {
                    "tool": "memory_save",
                    "args": {
                        "project_id": "my-project",
                        "content": "Use tenacity for retry logic",
                        "tags": ["coding", "#provisional"],
                        "scope": "agenttype_coding",
                    },
                },
            ],
            "consolidate": [
                {
                    "tool": "memory_search",
                    "args": {
                        "project_id": "my-project",
                        "query": "retry patterns",
                        "scope": "agenttype_coding",
                    },
                },
            ],
            "surface_contradictions": self._contradiction_tools(),
            "flag_stale": self._stale_tools(),
        }

    def _contradiction_tools(self) -> list[dict]:
        tools = [
            {
                "tool": "memory_health",
                "args": {"project_id": "my-project"},
            },
        ]
        if self._has_contradictions:
            tools.extend(
                [
                    {
                        "tool": "memory_get",
                        "args": {
                            "project_id": "my-project",
                            "chunk_hash": "abc123",
                        },
                    },
                    {
                        "tool": "memory_search",
                        "args": {
                            "project_id": "my-project",
                            "query": "conflicting topic",
                        },
                    },
                    {
                        "tool": "memory_update",
                        "args": {
                            "project_id": "my-project",
                            "chunk_hash": "abc123",
                            "tags": ["coding", "#verified"],
                        },
                    },
                    {
                        "tool": "memory_delete",
                        "args": {
                            "project_id": "my-project",
                            "chunk_hash": "def456",
                        },
                    },
                ]
            )
        return tools

    def _stale_tools(self) -> list[dict]:
        tools = [
            {
                "tool": "memory_stale",
                "args": {"project_id": "my-project"},
            },
        ]
        if self._has_stale:
            tools.extend(
                [
                    {
                        "tool": "memory_delete",
                        "args": {
                            "project_id": "my-project",
                            "chunk_hash": "stale1",
                        },
                    },
                    {
                        "tool": "memory_update",
                        "args": {
                            "project_id": "my-project",
                            "chunk_hash": "stale2",
                            "content": "Refreshed insight content",
                        },
                    },
                ]
            )
        return tools

    async def chat(self, **kwargs) -> str:
        self.chat_calls.append(kwargs)
        node_hint = kwargs.get("user_name", "")
        node_name = node_hint.split(":", 1)[-1] if ":" in node_hint else ""

        if node_name in self._node_tool_mapping:
            for tc in self._node_tool_mapping[node_name]:
                self.tool_calls.append({**tc, "node": node_name})

        return f"Completed {node_name} step."

    @property
    def tools_by_node(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for tc in self.tool_calls:
            node = tc["node"]
            result.setdefault(node, []).append(tc["tool"])
        return result

    @property
    def memory_health_calls(self) -> list[dict]:
        return [tc for tc in self.tool_calls if tc["tool"] == "memory_health"]

    @property
    def memory_stale_calls(self) -> list[dict]:
        return [tc for tc in self.tool_calls if tc["tool"] == "memory_stale"]


# ---------------------------------------------------------------------------
# Tests: Playbook template — minimal smoke check
# ---------------------------------------------------------------------------


class TestReflectionPlaybookStaleContradictionTemplate:
    """Verify the playbook template has basic resolution language."""

    @pytest.fixture
    def playbook_source(self) -> str:
        import os

        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "vault",
            "agent-types",
            "coding",
            "playbooks",
            "reflection.md",
        )
        with open(path) as f:
            return f.read()

    def test_playbook_describes_contradiction_resolution(self, playbook_source: str) -> None:
        """The playbook describes how to resolve contradictions."""
        lower = playbook_source.lower()
        assert "resolve" in lower or "resolution" in lower
        # Should mention evaluating in light of current task
        assert "evaluate" in lower or "confirm" in lower


# ---------------------------------------------------------------------------
# Tests: Extended graph walks through all nodes
# ---------------------------------------------------------------------------


class TestExtendedReflectionGraphWalk:
    """Verify PlaybookRunner walks the extended graph with stale + contradiction nodes."""

    async def test_all_extended_nodes_executed(self) -> None:
        """Runner visits all 6 non-terminal nodes in the correct order."""
        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
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
            "surface_contradictions",
            "flag_stale",
        ]

    async def test_six_chat_calls_for_extended_graph(self) -> None:
        """Runner makes 6 chat calls (one per non-terminal node)."""
        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        assert len(supervisor.chat_calls) == 6

    async def test_node_names_in_user_name_extended(self) -> None:
        """User names match the node IDs in the extended reflection graph."""
        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

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
            "surface_contradictions",
            "flag_stale",
        ]


# ---------------------------------------------------------------------------
# Tests: Tool-call patterns for new nodes
# ---------------------------------------------------------------------------


class TestStaleContradictionToolCallPatterns:
    """Verify the expected tool-call patterns in the new nodes."""

    async def test_surface_contradictions_calls_memory_health(self) -> None:
        """The surface_contradictions node must call memory_health."""
        supervisor = ExtendedToolTrackingSupervisor(has_contradictions=False)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools = supervisor.tools_by_node.get("surface_contradictions", [])
        assert "memory_health" in tools

    async def test_surface_contradictions_reads_contested_entries(self) -> None:
        """When contradictions exist, the node reads contested entries."""
        supervisor = ExtendedToolTrackingSupervisor(has_contradictions=True)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools = supervisor.tools_by_node.get("surface_contradictions", [])
        assert "memory_health" in tools
        assert "memory_get" in tools
        assert "memory_search" in tools

    async def test_surface_contradictions_resolves_via_update_and_delete(self) -> None:
        """When a contradiction can be resolved, the node updates winner and deletes loser."""
        supervisor = ExtendedToolTrackingSupervisor(has_contradictions=True)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools = supervisor.tools_by_node.get("surface_contradictions", [])
        assert "memory_update" in tools
        assert "memory_delete" in tools

    async def test_flag_stale_calls_memory_stale(self) -> None:
        """The flag_stale node must call memory_stale."""
        supervisor = ExtendedToolTrackingSupervisor(has_stale=False)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools = supervisor.tools_by_node.get("flag_stale", [])
        assert "memory_stale" in tools

    async def test_flag_stale_deletes_outdated_entries(self) -> None:
        """When stale entries are found, the node can delete outdated ones."""
        supervisor = ExtendedToolTrackingSupervisor(has_stale=True)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools = supervisor.tools_by_node.get("flag_stale", [])
        assert "memory_stale" in tools
        assert "memory_delete" in tools

    async def test_flag_stale_refreshes_valid_entries(self) -> None:
        """When stale entries are found, the node can refresh valid but stale ones."""
        supervisor = ExtendedToolTrackingSupervisor(has_stale=True)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        tools = supervisor.tools_by_node.get("flag_stale", [])
        assert "memory_update" in tools

    async def test_no_action_nodes_when_nothing_found(self) -> None:
        """When no contradictions or stale memories exist, only detection tools are called."""
        supervisor = ExtendedToolTrackingSupervisor(
            has_contradictions=False, has_stale=False
        )
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        # Contradiction node: only memory_health, no memory_get/delete/update
        contra_tools = supervisor.tools_by_node.get("surface_contradictions", [])
        assert "memory_health" in contra_tools
        assert "memory_get" not in contra_tools
        assert "memory_delete" not in contra_tools

        # Stale node: only memory_stale, no memory_delete/update
        stale_tools = supervisor.tools_by_node.get("flag_stale", [])
        assert "memory_stale" in stale_tools
        assert "memory_delete" not in stale_tools
        assert "memory_update" not in stale_tools

    async def test_at_least_one_memory_health_call(self) -> None:
        """Each extended reflection run produces at least one memory_health call."""
        supervisor = ExtendedToolTrackingSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        assert len(supervisor.memory_health_calls) >= 1

    async def test_at_least_one_memory_stale_call(self) -> None:
        """Each extended reflection run produces at least one memory_stale call."""
        supervisor = ExtendedToolTrackingSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        assert len(supervisor.memory_stale_calls) >= 1


# ---------------------------------------------------------------------------
# Tests: Skip behaviour for new nodes
# ---------------------------------------------------------------------------


class TestStaleContradictionSkipBehaviour:
    """Verify nodes handle empty results gracefully."""

    async def test_no_contradictions_completes_quickly(self) -> None:
        """When contradiction_count=0, the node completes without error."""
        supervisor = ExtendedReflectionMockSupervisor(contradiction_count=0)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # Verify the contradiction node was still executed
        executed = [t["node_id"] for t in result.node_trace]
        assert "surface_contradictions" in executed

    async def test_no_stale_memories_completes_quickly(self) -> None:
        """When no stale memories found, the node completes without error."""
        supervisor = ExtendedReflectionMockSupervisor(stale_count=0)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        result = await runner.run()

        assert result.status == "completed"
        executed = [t["node_id"] for t in result.node_trace]
        assert "flag_stale" in executed

    async def test_contradictions_found_completes_successfully(self) -> None:
        """When contradictions exist, the node resolves them and completes."""
        supervisor = ExtendedReflectionMockSupervisor(contradiction_count=2)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        result = await runner.run()

        assert result.status == "completed"

    async def test_stale_found_completes_successfully(self) -> None:
        """When stale memories exist, the node processes them and completes."""
        supervisor = ExtendedReflectionMockSupervisor(stale_count=5)
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        result = await runner.run()

        assert result.status == "completed"

    async def test_both_contradictions_and_stale_found(self) -> None:
        """When both contradictions and stale exist, both nodes process them."""
        supervisor = ExtendedReflectionMockSupervisor(
            contradiction_count=3, stale_count=7
        )
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        result = await runner.run()

        assert result.status == "completed"
        executed = [t["node_id"] for t in result.node_trace]
        assert "surface_contradictions" in executed
        assert "flag_stale" in executed


# ---------------------------------------------------------------------------
# Tests: Prompts reference correct tools
# ---------------------------------------------------------------------------


class TestNodePromptsReferenceTools:
    """Verify node prompts contain references to the expected tools."""

    async def test_surface_contradictions_prompt_mentions_memory_health(self) -> None:
        """The surface_contradictions node prompt instructs calling memory_health."""
        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        prompts_by_node = self._extract_prompts(supervisor)
        assert "memory_health" in prompts_by_node.get("surface_contradictions", "")

    async def test_surface_contradictions_prompt_mentions_contested(self) -> None:
        """The surface_contradictions node prompt references #contested tag."""
        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        prompts_by_node = self._extract_prompts(supervisor)
        assert "#contested" in prompts_by_node.get("surface_contradictions", "")

    async def test_flag_stale_prompt_mentions_memory_stale(self) -> None:
        """The flag_stale node prompt instructs calling memory_stale."""
        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        prompts_by_node = self._extract_prompts(supervisor)
        assert "memory_stale" in prompts_by_node.get("flag_stale", "")

    async def test_flag_stale_prompt_mentions_triage_actions(self) -> None:
        """The flag_stale prompt mentions delete, refresh, and keep as actions."""
        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        await runner.run()

        prompts_by_node = self._extract_prompts(supervisor)
        stale_prompt = prompts_by_node.get("flag_stale", "").lower()
        assert "delete" in stale_prompt
        assert "refresh" in stale_prompt
        assert "keep" in stale_prompt

    def _extract_prompts(self, supervisor) -> dict[str, str]:
        result: dict[str, str] = {}
        for call in supervisor.chat_calls:
            user_name = call.get("user_name", "")
            if ":" in user_name:
                node_name = user_name.split(":", 1)[1]
                result[node_name] = call.get("text", "")
        return result


# ---------------------------------------------------------------------------
# Tests: DB persistence with extended graph
# ---------------------------------------------------------------------------


class TestExtendedReflectionDBPersistence:
    """Verify run state includes the new nodes when persisted."""

    async def test_six_intermediate_updates_for_extended_graph(self) -> None:
        """DB is updated after each of the 6 non-terminal nodes + 1 final = 7."""
        db = AsyncMock()
        db.create_playbook_run = AsyncMock()
        db.update_playbook_run = AsyncMock()
        db.get_daily_playbook_token_usage = AsyncMock(return_value=0)

        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor, db=db)
        result = await runner.run()

        assert result.status == "completed"
        # 6 intermediate updates (one per node) + 1 final completion = 7
        assert db.update_playbook_run.call_count == 7

    async def test_run_record_contains_new_node_trace(self) -> None:
        """The completed run result traces through all 6 nodes."""
        supervisor = ExtendedReflectionMockSupervisor()
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        runner = PlaybookRunner(graph, event, supervisor)
        result = await runner.run()

        node_ids = [t["node_id"] for t in result.node_trace]
        assert "surface_contradictions" in node_ids
        assert "flag_stale" in node_ids
        assert len(node_ids) == 6


# ---------------------------------------------------------------------------
# Tests: Dry-run for extended graph
# ---------------------------------------------------------------------------


class TestExtendedReflectionDryRun:
    """Verify dry-run mode works with the extended graph."""

    async def test_dry_run_walks_extended_graph(self) -> None:
        """Dry-run mode walks the extended graph without LLM calls."""
        graph = _make_extended_reflection_graph()
        event = _task_completed_event()

        result = await PlaybookRunner.dry_run(graph, event)

        assert result.status == "completed"
        executed = [t["node_id"] for t in result.node_trace]
        assert "review_task" in executed
        assert len(executed) >= 1


# ---------------------------------------------------------------------------
# Tests: API response structure compatibility (service-level)
# ---------------------------------------------------------------------------


class TestMemoryHealthResponseCompatibility:
    """Verify memory_health returns the fields the playbook expects.

    These tests use the real MemoryV2Service.health() method (with a mock
    store) to confirm the response structure matches playbook expectations.
    """

    async def test_health_response_has_contradiction_count(self) -> None:
        """memory_health response includes contradiction_count field."""
        response = await self._get_health_response()
        assert "contradiction_count" in response

    async def test_health_response_has_contradictions_list(self) -> None:
        """memory_health response includes contradictions list."""
        response = await self._get_health_response()
        assert "contradictions" in response
        assert isinstance(response["contradictions"], list)

    async def test_health_contested_entry_has_chunk_hash(self) -> None:
        """Each contested entry in contradictions list has chunk_hash."""
        response = await self._get_health_response(with_contested=True)
        assert len(response["contradictions"]) > 0
        entry = response["contradictions"][0]
        assert "chunk_hash" in entry

    async def test_health_contested_entry_has_topic(self) -> None:
        """Each contested entry in contradictions list has topic."""
        response = await self._get_health_response(with_contested=True)
        entry = response["contradictions"][0]
        assert "topic" in entry

    async def test_health_contested_entry_has_heading(self) -> None:
        """Each contested entry in contradictions list has heading."""
        response = await self._get_health_response(with_contested=True)
        entry = response["contradictions"][0]
        assert "heading" in entry

    async def test_health_contested_entry_has_tags(self) -> None:
        """Each contested entry in contradictions list has tags."""
        response = await self._get_health_response(with_contested=True)
        entry = response["contradictions"][0]
        assert "tags" in entry
        assert isinstance(entry["tags"], list)
        assert "contested" in entry["tags"]

    async def _get_health_response(
        self, *, with_contested: bool = False
    ) -> dict:
        """Build a realistic health() response using the real service method.

        We mock the store to avoid needing a real Milvus/vector DB.
        """
        from unittest.mock import MagicMock, PropertyMock, patch
        import time

        try:
            from memsearch.scoping import MemoryScope
        except ImportError:
            pytest.skip("memsearch not installed")

        now = time.time()
        entries = [
            {
                "chunk_hash": f"hash_{i}",
                "entry_type": "document",
                "heading": f"Insight {i}",
                "topic": f"topic_{i}",
                "tags": '["insight"]',
                "retrieval_count": i,
                "last_retrieved": now - (i * 86400),
                "updated_at": now - (i * 86400 * 2),
                "content": f"Content for insight {i}",
            }
            for i in range(5)
        ]
        if with_contested:
            entries.append(
                {
                    "chunk_hash": "contested_hash_1",
                    "entry_type": "document",
                    "heading": "Contested insight",
                    "topic": "async patterns",
                    "tags": '["insight", "contested"]',
                    "retrieval_count": 2,
                    "last_retrieved": now - 86400,
                    "updated_at": now - (3 * 86400),
                    "content": "Use asyncio.gather for parallel tasks",
                }
            )

        mock_store = MagicMock()
        mock_store.query.return_value = entries

        from src.plugins.internal.memory_v2.service import MemoryV2Service

        service = MemoryV2Service.__new__(MemoryV2Service)

        with (
            patch.object(
                type(service), "available", new_callable=PropertyMock, return_value=True
            ),
            patch.object(service, "_get_store", return_value=mock_store),
            patch.object(
                service,
                "_resolve_scope",
                return_value=(MemoryScope.PROJECT, "test-project"),
            ),
        ):
            return await service.health("test-project")


class TestMemoryStaleResponseCompatibility:
    """Verify memory_stale returns the fields the playbook expects.

    These tests use the real MemoryV2Service.find_stale() method (with a
    mock store) to confirm the response structure matches playbook expectations.
    """

    async def test_stale_response_has_total_stale(self) -> None:
        """memory_stale response includes total_stale field."""
        response = await self._get_stale_response()
        assert "total_stale" in response

    async def test_stale_response_has_stale_documents_list(self) -> None:
        """memory_stale response includes stale_documents list."""
        response = await self._get_stale_response()
        assert "stale_documents" in response
        assert isinstance(response["stale_documents"], list)

    async def test_stale_response_has_never_retrieved_count(self) -> None:
        """memory_stale response includes never_retrieved_count field."""
        response = await self._get_stale_response()
        assert "never_retrieved_count" in response

    async def test_stale_document_has_chunk_hash(self) -> None:
        """Each stale document has chunk_hash for deletion/update."""
        response = await self._get_stale_response()
        assert len(response["stale_documents"]) > 0
        doc = response["stale_documents"][0]
        assert "chunk_hash" in doc

    async def test_stale_document_has_title(self) -> None:
        """Each stale document has title for human-readable display."""
        response = await self._get_stale_response()
        doc = response["stale_documents"][0]
        assert "title" in doc

    async def test_stale_document_has_topic(self) -> None:
        """Each stale document has topic field."""
        response = await self._get_stale_response()
        doc = response["stale_documents"][0]
        assert "topic" in doc

    async def test_stale_document_has_tags(self) -> None:
        """Each stale document has tags list."""
        response = await self._get_stale_response()
        doc = response["stale_documents"][0]
        assert "tags" in doc
        assert isinstance(doc["tags"], list)

    async def test_stale_document_has_content_preview(self) -> None:
        """Each stale document has content_preview for quick review."""
        response = await self._get_stale_response()
        doc = response["stale_documents"][0]
        assert "content_preview" in doc

    async def test_stale_document_has_reason(self) -> None:
        """Each stale document has reason (never_retrieved or stale)."""
        response = await self._get_stale_response()
        docs = response["stale_documents"]
        reasons = {d["reason"] for d in docs}
        # Should have at least one of the two reason types
        assert reasons <= {"never_retrieved", "stale"}
        assert len(reasons) > 0

    async def test_stale_document_has_days_since_retrieval(self) -> None:
        """Each stale document has days_since_retrieval field."""
        response = await self._get_stale_response()
        doc = response["stale_documents"][0]
        assert "days_since_retrieval" in doc

    async def test_stale_respects_limit_parameter(self) -> None:
        """memory_stale respects the limit parameter (playbook uses 10)."""
        response = await self._get_stale_response(limit=2)
        assert len(response["stale_documents"]) <= 2
        assert response["limit"] == 2

    async def test_never_retrieved_sorted_first(self) -> None:
        """Default staleness sort puts never-retrieved entries first."""
        response = await self._get_stale_response()
        docs = response["stale_documents"]
        # Never-retrieved should come before stale
        never_seen = [i for i, d in enumerate(docs) if d["reason"] == "never_retrieved"]
        stale_seen = [i for i, d in enumerate(docs) if d["reason"] == "stale"]
        if never_seen and stale_seen:
            assert max(never_seen) < min(stale_seen)

    async def _get_stale_response(self, *, limit: int = 50) -> dict:
        """Build a realistic find_stale() response using the real service method."""
        from unittest.mock import MagicMock, PropertyMock, patch
        import time

        try:
            from memsearch.scoping import MemoryScope
        except ImportError:
            pytest.skip("memsearch not installed")

        now = time.time()
        entries = [
            # Never retrieved
            {
                "chunk_hash": "never_1",
                "entry_type": "document",
                "heading": "Old insight never used",
                "topic": "testing",
                "tags": '["testing", "provisional"]',
                "retrieval_count": 0,
                "last_retrieved": 0,
                "updated_at": now - (60 * 86400),
                "content": "Always run pytest with -v flag for verbose output",
                "source": "task-001",
            },
            # Stale (retrieved 45 days ago)
            {
                "chunk_hash": "stale_1",
                "entry_type": "document",
                "heading": "Async pattern insight",
                "topic": "async",
                "tags": '["async", "verified"]',
                "retrieval_count": 3,
                "last_retrieved": now - (45 * 86400),
                "updated_at": now - (90 * 86400),
                "content": "Use asyncio.gather for parallel I/O operations",
                "source": "task-042",
            },
            # Fresh (retrieved yesterday) — should NOT appear
            {
                "chunk_hash": "fresh_1",
                "entry_type": "document",
                "heading": "Fresh insight",
                "topic": "recent",
                "tags": '["recent"]',
                "retrieval_count": 10,
                "last_retrieved": now - 86400,
                "updated_at": now - (5 * 86400),
                "content": "This was recently retrieved and is not stale",
                "source": "task-099",
            },
        ]

        mock_store = MagicMock()
        mock_store.query.return_value = entries

        from src.plugins.internal.memory_v2.service import MemoryV2Service

        service = MemoryV2Service.__new__(MemoryV2Service)

        with (
            patch.object(
                type(service), "available", new_callable=PropertyMock, return_value=True
            ),
            patch.object(service, "_get_store", return_value=mock_store),
            patch.object(
                service,
                "_resolve_scope",
                return_value=(MemoryScope.PROJECT, "test-project"),
            ),
        ):
            return await service.find_stale("test-project", limit=limit)
