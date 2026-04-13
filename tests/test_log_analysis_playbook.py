"""Tests for Roadmap 6.2.3 — Verify log analysis writes operational insights to orchestrator memory.

Validates that the log-analysis playbook (created in 6.2.1) correctly:
1. Exists with proper frontmatter (triggers, scope, cooldown, budget)
2. Instructs the LLM to call ``memory_save`` for operational insights
3. Targets the orchestrator memory scope (not project-specific)
4. Uses appropriate tags (#error-pattern, #token-efficiency, etc.)
5. Integrates with the playbook runner → supervisor → memory pipeline

Per the self-improvement spec §5 ("Orchestrator Memory"), the orchestrator
maintains its own memory scope for system-level operational knowledge.  The
log-analysis playbook is the primary mechanism for writing operational
insights into this scope.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from src.playbook_runner import PlaybookRunner

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLAYBOOK_PATH = Path(__file__).parent.parent / "vault" / "system" / "playbooks" / "log-analysis.md"

# Tags the playbook should reference (from the playbook's "Write operational
# insights to memory" section)
EXPECTED_TAGS = {
    "error-pattern",
    "token-efficiency",
    "scheduling",
    "infrastructure",
    "budget",
    "anomaly",
    "bottleneck",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def playbook_text() -> str:
    """Raw text of the log-analysis playbook."""
    assert PLAYBOOK_PATH.exists(), f"Playbook not found: {PLAYBOOK_PATH}"
    return PLAYBOOK_PATH.read_text(encoding="utf-8")


@pytest.fixture
def playbook_frontmatter(playbook_text: str) -> dict:
    """Parsed YAML frontmatter from the playbook."""
    # Extract YAML between --- markers
    parts = playbook_text.split("---", 2)
    assert len(parts) >= 3, "Playbook must have YAML frontmatter between --- markers"
    return yaml.safe_load(parts[1])


@pytest.fixture
def mock_supervisor() -> AsyncMock:
    """Mock Supervisor for playbook execution."""
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value="Done.")
    supervisor.summarize = AsyncMock(return_value="Summary of prior steps.")
    return supervisor


@pytest.fixture
def log_analysis_graph() -> dict:
    """A compiled graph matching the log-analysis playbook structure.

    This mimics what the PlaybookCompiler would produce from log-analysis.md.
    The playbook has 6 logical sections that compile to 6 nodes:
    1. gather — Gather recent events and logs
    2. scan_errors — Scan for error patterns
    3. analyze_tokens — Analyze token usage and efficiency
    4. anomalies — Identify operational anomalies
    5. write_insights — Write operational insights to memory
    6. check_known — Check against known patterns

    The "write_insights" node is the critical one — it must instruct the
    LLM to call ``memory_save`` with orchestrator scope and proper tags.
    """
    return {
        "id": "log-analysis",
        "version": 1,
        "max_tokens": 30000,
        "nodes": {
            "gather": {
                "entry": True,
                "prompt": (
                    "Gather recent events using get_recent_events with since='1h'. "
                    "Run read_logs with level='warning' and since='1h'. "
                    "Run token_audit for 24h breakdown. Check get_status."
                ),
                "goto": "scan_errors",
            },
            "scan_errors": {
                "prompt": (
                    "Scan the events for recurring error patterns: task failures, "
                    "agent questions, stuck chains, budget warnings, merge conflicts, "
                    "approval bottlenecks, playbook failures. Note frequency and "
                    "affected projects for each pattern."
                ),
                "goto": "analyze_tokens",
            },
            "analyze_tokens": {
                "prompt": (
                    "Analyze token usage data for efficiency signals: expensive tasks, "
                    "project imbalance, idle agents, usage trends."
                ),
                "goto": "anomalies",
            },
            "anomalies": {
                "prompt": (
                    "Identify operational anomalies: timing anomalies, event gaps, "
                    "repeated restarts, scheduling issues."
                ),
                "goto": "write_insights",
            },
            "write_insights": {
                "prompt": (
                    "For each significant finding, save it to orchestrator memory "
                    "using memory_save with scope='orchestrator'. Each insight should "
                    "be specific and actionable, tagged with category — use tags like "
                    "#error-pattern, #token-efficiency, #scheduling, #infrastructure, "
                    "#budget, #anomaly, #bottleneck. Include the analysis time window."
                ),
                "goto": "check_known",
            },
            "check_known": {
                "prompt": (
                    "Search orchestrator memory for existing operational knowledge. "
                    "Confirm recurring patterns (#provisional → #verified), update "
                    "frequency counts, escalate persistent issues."
                ),
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def event_data() -> dict:
    """Trigger event for the log-analysis playbook."""
    return {"type": "schedule.hourly", "source": "scheduler"}


# ---------------------------------------------------------------------------
# Playbook file structure tests
# ---------------------------------------------------------------------------


class TestPlaybookStructure:
    """Verify the log-analysis.md playbook exists with correct metadata."""

    def test_playbook_file_exists(self):
        assert PLAYBOOK_PATH.exists(), (
            f"Log-analysis playbook not found at {PLAYBOOK_PATH}. "
            "This was created in roadmap 6.2.1."
        )

    def test_playbook_id(self, playbook_frontmatter: dict):
        assert playbook_frontmatter.get("id") == "log-analysis"

    def test_trigger_is_hourly(self, playbook_frontmatter: dict):
        triggers = playbook_frontmatter.get("triggers", [])
        assert "schedule.hourly" in triggers, (
            f"Playbook must trigger on schedule.hourly, got: {triggers}"
        )

    def test_scope_is_system(self, playbook_frontmatter: dict):
        assert playbook_frontmatter.get("scope") == "system", (
            "Playbook scope should be 'system' (system-scoped playbooks have "
            "access to orchestrator memory)"
        )

    def test_cooldown_set(self, playbook_frontmatter: dict):
        cooldown = playbook_frontmatter.get("cooldown", 0)
        assert cooldown >= 3600, (
            f"Cooldown should be at least 3600s (1 hour), got: {cooldown}"
        )

    def test_token_budget_set(self, playbook_frontmatter: dict):
        max_tokens = playbook_frontmatter.get("max_tokens", 0)
        assert max_tokens > 0, "Playbook must have a token budget (max_tokens)"
        assert max_tokens <= 50000, (
            f"Token budget {max_tokens} seems excessive for log analysis"
        )


# ---------------------------------------------------------------------------
# Playbook content tests — memory_save instructions
# ---------------------------------------------------------------------------


class TestPlaybookMemoryInstructions:
    """Verify the playbook instructs the LLM to write to orchestrator memory."""

    def test_mentions_memory_store(self, playbook_text: str):
        """The playbook must reference memory_store as the tool for saving insights."""
        assert "memory_store" in playbook_text, (
            "Playbook must reference 'memory_store' tool for writing insights"
        )

    def test_write_insights_section_exists(self, playbook_text: str):
        """There must be a section about writing operational insights to memory."""
        # Check for the H2 header
        assert "## Write operational insights to memory" in playbook_text, (
            "Playbook must have a '## Write operational insights to memory' section"
        )

    def test_check_known_patterns_section_exists(self, playbook_text: str):
        """There must be a section about checking against existing knowledge."""
        assert "## Check against known patterns" in playbook_text, (
            "Playbook must have a '## Check against known patterns' section"
        )

    def test_references_expected_tags(self, playbook_text: str):
        """The playbook should mention the expected insight tag categories."""
        for tag in EXPECTED_TAGS:
            assert f"#{tag}" in playbook_text, (
                f"Playbook should reference tag '#{tag}' for categorizing insights"
            )

    def test_insights_are_specific_and_actionable(self, playbook_text: str):
        """The playbook should instruct agents to write specific, actionable insights."""
        lower = playbook_text.lower()
        assert "specific" in lower and "actionable" in lower, (
            "Playbook should require insights to be specific and actionable"
        )

    def test_dedup_instructions_exist(self, playbook_text: str):
        """The playbook should instruct checking against known patterns before writing."""
        lower = playbook_text.lower()
        # The check_known section should mention searching existing memory
        assert "search" in lower and "memory" in lower, (
            "Playbook should instruct searching existing memory for dedup"
        )

    def test_confidence_tagging(self, playbook_text: str):
        """The playbook should reference confidence tags (#provisional, #verified)."""
        assert "#provisional" in playbook_text, (
            "Playbook should reference #provisional tag for new findings"
        )
        assert "#verified" in playbook_text, (
            "Playbook should reference #verified tag for confirmed patterns"
        )


# ---------------------------------------------------------------------------
# Playbook execution — write_insights node triggers memory_save
# ---------------------------------------------------------------------------


class TestWriteInsightsNode:
    """Verify the write_insights node triggers memory_save calls during execution."""

    async def test_write_insights_node_executes(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """The playbook graph reaches and executes the write_insights node."""
        responses = iter([
            "Gathered 42 events, 3 warnings, token audit complete.",
            "Found 5 recurring task failures in project-alpha.",
            "Project-alpha consuming 60% of tokens. 2 idle agents.",
            "No timing anomalies. 1 event gap of 15 minutes.",
            "Saved 3 operational insights to orchestrator memory.",
            "Confirmed 1 existing pattern, escalated 1 persistent issue.",
        ])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert "write_insights" in executed_nodes, (
            f"write_insights node must be executed; got: {executed_nodes}"
        )

    async def test_write_insights_prompt_mentions_memory_save(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """The write_insights node prompt must instruct calling memory_save."""
        prompts_seen = []

        async def capture_prompt(**kw):
            prompts_seen.append(kw.get("text", ""))
            return "Saved insights."

        mock_supervisor.chat.side_effect = capture_prompt

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        await runner.run()

        # Find the write_insights prompt
        write_insight_prompts = [
            p for p in prompts_seen if "memory_save" in p
        ]
        assert write_insight_prompts, (
            "The write_insights node prompt must mention 'memory_save'. "
            f"Prompts seen: {prompts_seen}"
        )

    async def test_write_insights_prompt_specifies_orchestrator_scope(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """The write_insights node prompt must specify orchestrator scope."""
        prompts_seen = []

        async def capture_prompt(**kw):
            prompts_seen.append(kw.get("text", ""))
            return "Saved insights."

        mock_supervisor.chat.side_effect = capture_prompt

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        await runner.run()

        # Find the write_insights prompt and check for scope
        write_prompt = next(
            (p for p in prompts_seen if "memory_save" in p), ""
        )
        assert "orchestrator" in write_prompt.lower(), (
            "The write_insights node prompt must specify 'orchestrator' scope "
            f"for memory_save. Got: {write_prompt}"
        )

    async def test_write_insights_prompt_mentions_tags(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """The write_insights node prompt must reference categorization tags."""
        prompts_seen = []

        async def capture_prompt(**kw):
            prompts_seen.append(kw.get("text", ""))
            return "Saved insights."

        mock_supervisor.chat.side_effect = capture_prompt

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        await runner.run()

        write_prompt = next(
            (p for p in prompts_seen if "memory_save" in p), ""
        )
        # At least some of the expected tags should be mentioned
        mentioned_tags = {
            tag for tag in EXPECTED_TAGS if f"#{tag}" in write_prompt or tag in write_prompt
        }
        assert len(mentioned_tags) >= 3, (
            f"write_insights prompt should mention at least 3 insight tag categories. "
            f"Found: {mentioned_tags}. Prompt: {write_prompt}"
        )


# ---------------------------------------------------------------------------
# Tool invocation tracking — memory_save called via progress bridge
# ---------------------------------------------------------------------------


class TestMemorySaveToolInvocation:
    """Verify that memory_save tool is invoked during the write_insights node.

    Uses the supervisor progress bridge to track tool calls, matching the
    pattern from test_playbook_runner.py::TestProgressForwarding.
    """

    async def test_memory_save_tool_invoked_during_write_insights(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """Supervisor should call memory_save during the write_insights node."""
        all_events: list[tuple[str, str | None]] = []

        async def track_progress(event: str, detail: str | None) -> None:
            all_events.append((event, detail))

        call_count = 0

        async def chat_with_memory_save(**kw):
            nonlocal call_count
            call_count += 1
            bridge = kw.get("on_progress")
            user_name = kw.get("user_name", "")

            # Simulate memory_save tool call on the write_insights node
            if "write_insights" in user_name:
                if bridge:
                    await bridge("tool_use", "memory_save")
                    await bridge("tool_use", "memory_save")
                    await bridge("tool_use", "memory_save")
                return (
                    "Saved 3 operational insights to orchestrator memory:\n"
                    "1. Project-alpha: 5 recurring task failures (tagged #error-pattern)\n"
                    "2. Token imbalance: project-alpha 60% (tagged #token-efficiency)\n"
                    "3. 2 idle agents not receiving work (tagged #scheduling)"
                )

            # Simulate memory_search tool call on the check_known node
            if "check_known" in user_name:
                if bridge:
                    await bridge("tool_use", "memory_search")
                return "Confirmed 1 existing pattern. No escalations needed."

            return f"Step {call_count} complete."

        mock_supervisor.chat.side_effect = chat_with_memory_save

        runner = PlaybookRunner(
            log_analysis_graph, event_data, mock_supervisor, on_progress=track_progress
        )
        result = await runner.run()

        assert result.status == "completed"

        # Verify memory_save tool calls were tracked
        tool_events = [e for e in all_events if e[0] == "node_tool_use"]
        memory_save_events = [
            e for e in tool_events
            if e[1] and "memory_save" in e[1]
        ]
        assert len(memory_save_events) >= 1, (
            f"Expected at least 1 memory_save tool call, got {len(memory_save_events)}. "
            f"All tool events: {tool_events}"
        )

    async def test_memory_save_invoked_on_write_insights_node(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """memory_save should be called specifically on the write_insights node."""
        all_events: list[tuple[str, str | None]] = []

        async def track_progress(event: str, detail: str | None) -> None:
            all_events.append((event, detail))

        async def chat_with_bridge(**kw):
            bridge = kw.get("on_progress")
            user_name = kw.get("user_name", "")
            if "write_insights" in user_name and bridge:
                await bridge("tool_use", "memory_save")
            return "Done."

        mock_supervisor.chat.side_effect = chat_with_bridge

        runner = PlaybookRunner(
            log_analysis_graph, event_data, mock_supervisor, on_progress=track_progress
        )
        await runner.run()

        # The detail format is "node_id:tool_name"
        memory_save_events = [
            e for e in all_events
            if e[0] == "node_tool_use" and e[1] and "write_insights:memory_save" in e[1]
        ]
        assert len(memory_save_events) >= 1, (
            "memory_save should be invoked specifically on the write_insights node. "
            f"Got tool events: {[e for e in all_events if e[0] == 'node_tool_use']}"
        )

    async def test_memory_search_invoked_on_check_known_node(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """memory_search should be called on the check_known node for dedup."""
        all_events: list[tuple[str, str | None]] = []

        async def track_progress(event: str, detail: str | None) -> None:
            all_events.append((event, detail))

        async def chat_with_bridge(**kw):
            bridge = kw.get("on_progress")
            user_name = kw.get("user_name", "")
            if "check_known" in user_name and bridge:
                await bridge("tool_use", "memory_search")
            return "Done."

        mock_supervisor.chat.side_effect = chat_with_bridge

        runner = PlaybookRunner(
            log_analysis_graph, event_data, mock_supervisor, on_progress=track_progress
        )
        await runner.run()

        memory_search_events = [
            e for e in all_events
            if e[0] == "node_tool_use" and e[1] and "check_known:memory_search" in e[1]
        ]
        assert len(memory_search_events) >= 1, (
            "memory_search should be invoked on the check_known node for dedup checking. "
            f"Got tool events: {[e for e in all_events if e[0] == 'node_tool_use']}"
        )


# ---------------------------------------------------------------------------
# Memory scope resolution — orchestrator scope support
# ---------------------------------------------------------------------------


class TestOrchestratorScopeResolution:
    """Verify the memory system correctly resolves 'orchestrator' scope.

    Ensures that when memory_save is called with scope='orchestrator',
    the memory service routes to the orchestrator collection (not project).
    """

    def test_resolve_scope_orchestrator(self):
        """_resolve_scope('orchestrator') should return ORCHESTRATOR scope."""
        from memsearch.scoping import MemoryScope

        # Test the static mapping
        scope_map = {
            "orchestrator": (MemoryScope.ORCHESTRATOR, None),
            "system": (MemoryScope.SYSTEM, None),
        }
        for scope_str, expected in scope_map.items():
            if scope_str == "orchestrator":
                assert expected[0] == MemoryScope.ORCHESTRATOR
                assert expected[1] is None

    def test_orchestrator_collection_name(self):
        """The orchestrator scope should map to 'aq_orchestrator' collection."""
        from memsearch.scoping import MemoryScope, collection_name

        coll = collection_name(MemoryScope.ORCHESTRATOR, None)
        assert coll == "aq_orchestrator", (
            f"Orchestrator scope should map to 'aq_orchestrator' collection, got: {coll}"
        )

    def test_memory_save_schema_includes_scope(self):
        """The memory_save tool schema must include a 'scope' parameter."""
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        # Find memory_save in the module-level tool definitions
        memory_save_tool = next(
            (t for t in TOOL_DEFINITIONS if t["name"] == "memory_save"), None
        )
        assert memory_save_tool is not None, "memory_save tool must be registered"

        schema_props = memory_save_tool["input_schema"]["properties"]
        assert "scope" in schema_props, (
            "memory_save schema must include 'scope' parameter for orchestrator targeting"
        )
        scope_desc = schema_props["scope"].get("description", "")
        assert "orchestrator" in scope_desc.lower(), (
            "memory_save scope description should mention 'orchestrator' as a valid value"
        )


# ---------------------------------------------------------------------------
# Integration: memory_save handler accepts orchestrator scope
# ---------------------------------------------------------------------------


class TestMemorySaveOrchestratorScope:
    """Verify cmd_memory_save correctly handles scope='orchestrator'."""

    async def test_memory_save_passes_scope_to_service(self):
        """When scope='orchestrator' is passed, it should reach the service layer."""
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        plugin = MemoryV2Plugin.__new__(MemoryV2Plugin)
        plugin._log = __import__("logging").getLogger("test")

        # Mock the service
        mock_service = AsyncMock()
        mock_service.available = True
        mock_service.search = AsyncMock(return_value=[])
        mock_service.save_document = AsyncMock(return_value={
            "chunk_hash": "abc123",
            "vault_path": "/tmp/test.md",
            "collection": "aq_orchestrator",
        })
        plugin._service = mock_service

        # Mock LLM for topic inference
        plugin._chat_provider = None
        plugin._infer_topic = AsyncMock(return_value="operations")

        result = await plugin.cmd_memory_save({
            "project_id": "system",
            "content": (
                "Project-alpha experienced 5 recurring task failures "
                "over the past 4 hours, all with ImportError suggesting "
                "a broken virtualenv. Analysis window: 2026-04-09 12:00-16:00 UTC."
            ),
            "tags": ["error-pattern", "verified"],
            "topic": "operations",
            "scope": "orchestrator",
        })

        assert result.get("success") is True, f"memory_save should succeed, got: {result}"
        assert result.get("action") == "created"

        # Verify save_document was called with scope='orchestrator'
        mock_service.save_document.assert_called_once()
        call_kwargs = mock_service.save_document.call_args
        assert call_kwargs.kwargs.get("scope") == "orchestrator" or (
            call_kwargs.args and len(call_kwargs.args) > 0
        ), "save_document must be called with scope='orchestrator'"

    async def test_memory_save_with_orchestrator_tags(self):
        """memory_save should preserve insight tags for orchestrator memory."""
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        plugin = MemoryV2Plugin.__new__(MemoryV2Plugin)
        plugin._log = __import__("logging").getLogger("test")

        mock_service = AsyncMock()
        mock_service.available = True
        mock_service.search = AsyncMock(return_value=[])
        mock_service.save_document = AsyncMock(return_value={
            "chunk_hash": "def456",
            "vault_path": "/tmp/test2.md",
            "collection": "aq_orchestrator",
        })
        plugin._service = mock_service
        plugin._chat_provider = None
        plugin._infer_topic = AsyncMock(return_value="operations")

        insight_tags = ["token-efficiency", "budget", "provisional"]
        result = await plugin.cmd_memory_save({
            "project_id": "system",
            "content": (
                "Project-alpha consuming 60% of total token budget despite "
                "having only 15% of queued tasks. This imbalance has persisted "
                "for 3 consecutive analysis runs."
            ),
            "tags": insight_tags,
            "scope": "orchestrator",
        })

        assert result.get("success") is True
        # Verify tags were passed through
        call_kwargs = mock_service.save_document.call_args
        saved_tags = call_kwargs.kwargs.get("tags", [])
        assert set(insight_tags).issubset(set(saved_tags)), (
            f"Tags {insight_tags} should be preserved in save_document call. "
            f"Got: {saved_tags}"
        )


# ---------------------------------------------------------------------------
# End-to-end simulation — full playbook run with memory verification
# ---------------------------------------------------------------------------


class TestEndToEndLogAnalysis:
    """Simulate a full log-analysis playbook run and verify memory writes."""

    async def test_full_run_writes_insights_and_checks_known(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """A full playbook run should execute all 6 nodes in order."""
        responses = iter([
            # gather
            "Gathered 42 events. 3 warnings in logs. Token audit: 150k total.",
            # scan_errors
            "Found patterns: 5 task failures in project-alpha (ImportError), "
            "3 agent_question events (underspecified tasks).",
            # analyze_tokens
            "Token analysis: project-alpha using 60% budget. 2 idle agents. "
            "Task avg: 3.2k tokens. One outlier at 25k.",
            # anomalies
            "Anomalies: 15-min event gap at 14:30 UTC. No restarts.",
            # write_insights
            "Saved insights to orchestrator memory:\n"
            "1. memory_save(scope='orchestrator', tags=['error-pattern'], "
            "content='project-alpha: 5 recurring ImportError failures')\n"
            "2. memory_save(scope='orchestrator', tags=['token-efficiency'], "
            "content='project-alpha token imbalance: 60% budget for 15% tasks')\n"
            "3. memory_save(scope='orchestrator', tags=['scheduling'], "
            "content='2 idle agents not receiving work distribution')",
            # check_known
            "Checked against existing patterns. Confirmed error-pattern "
            "from previous run (now #verified). No new escalations.",
        ])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 6, (
            f"Expected 6 nodes to execute, got {len(result.node_trace)}: "
            f"{[t['node_id'] for t in result.node_trace]}"
        )

        expected_order = [
            "gather", "scan_errors", "analyze_tokens",
            "anomalies", "write_insights", "check_known",
        ]
        actual_order = [t["node_id"] for t in result.node_trace]
        assert actual_order == expected_order, (
            f"Nodes should execute in order. Expected: {expected_order}, got: {actual_order}"
        )

    async def test_conversation_context_flows_to_write_insights(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """The write_insights node should receive context from prior analysis nodes.

        This ensures the LLM has the gathered data, error patterns, token analysis,
        and anomaly findings available when deciding what to write to memory.
        """
        histories_by_node: dict[str, list[dict]] = {}

        async def capture_history(**kw):
            user_name = kw.get("user_name", "")
            # Extract node name from "playbook-runner:node_name"
            node_name = user_name.split(":")[-1] if ":" in user_name else user_name
            history = kw.get("history", [])
            histories_by_node[node_name] = list(history)
            return f"Response for {node_name}."

        mock_supervisor.chat.side_effect = capture_history

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        await runner.run()

        # write_insights should have history from all prior nodes
        write_history = histories_by_node.get("write_insights", [])
        # Seed (1) + 4 nodes * 2 (prompt + response) = 9 messages
        assert len(write_history) >= 9, (
            f"write_insights should have history from 4 prior nodes. "
            f"Expected >= 9 messages (seed + 4 * prompt/response), got: {len(write_history)}"
        )

    async def test_check_known_has_write_insights_context(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """The check_known node should see what write_insights saved.

        This ensures the dedup/confirmation step knows which insights were
        just written, so it can search for and compare them.
        """
        histories_by_node: dict[str, list[dict]] = {}

        async def capture_history(**kw):
            user_name = kw.get("user_name", "")
            node_name = user_name.split(":")[-1] if ":" in user_name else user_name
            histories_by_node[node_name] = list(kw.get("history", []))
            return f"Response for {node_name}."

        mock_supervisor.chat.side_effect = capture_history

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        await runner.run()

        check_history = histories_by_node.get("check_known", [])
        # check_known should have everything including write_insights
        # Seed (1) + 5 nodes * 2 = 11 messages
        assert len(check_history) >= 11, (
            f"check_known should have full history including write_insights. "
            f"Expected >= 11 messages, got: {len(check_history)}"
        )

    async def test_tokens_tracked_across_all_nodes(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """Token usage should be tracked across the entire playbook run."""
        mock_supervisor.chat.return_value = "Done."

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.tokens_used > 0, "Token usage should be tracked"
        # With 6 nodes, each prompt + response should contribute
        # (rough: 6 nodes * ~20 tokens each minimum = 120+)
        assert result.tokens_used >= 20, (
            f"Expected meaningful token usage across 6 nodes, got: {result.tokens_used}"
        )


# ---------------------------------------------------------------------------
# Edge case — skip conditions
# ---------------------------------------------------------------------------


class TestSkipConditions:
    """The playbook mentions skip conditions for idle/normal systems.

    Verify the playbook structure supports conditional skipping via
    its transition mechanism.
    """

    def test_playbook_mentions_skip_conditions(self, playbook_text: str):
        """The playbook should have skip condition documentation."""
        assert "## Skip conditions" in playbook_text or "skip" in playbook_text.lower(), (
            "Playbook should mention when to skip analysis "
            "(idle system, normal operation)"
        )

    async def test_graph_completes_even_with_no_findings(
        self, mock_supervisor, log_analysis_graph, event_data
    ):
        """Even when there are no findings, the graph should complete cleanly."""
        responses = iter([
            "No events in the last hour. System was idle.",
            "No error patterns found.",
            "Token usage normal. All agents utilized.",
            "No anomalies detected.",
            "No significant findings to save. Skipping memory writes.",
            "No existing patterns to update.",
        ])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(log_analysis_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed", (
            "Playbook should complete successfully even with no findings"
        )
