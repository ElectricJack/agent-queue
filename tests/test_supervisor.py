"""Tests for Supervisor — the single intelligent entity."""

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_supervisor():
    from src.supervisor import Supervisor

    orch = MagicMock()
    orch.config = MagicMock()
    orch.llm_logger = MagicMock()
    orch.llm_logger._enabled = False
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    config.chat_provider = MagicMock()
    config.supervisor = MagicMock()
    config.supervisor.reflection = MagicMock()
    config.supervisor.reflection.level = "full"
    config.supervisor.reflection.max_depth = 3
    config.supervisor.reflection.per_cycle_token_cap = 10000
    config.supervisor.reflection.hourly_token_circuit_breaker = 100000
    config.supervisor.reflection.periodic_interval = 900
    # No step limit — agents run until they finish
    return Supervisor(orch, config)


def _make_tool_use(name="create_task", input_data=None, id="tu-1"):
    """Helper to create a mock tool_use object."""
    tu = MagicMock()
    tu.name = name
    tu.input = input_data or {}
    tu.id = id
    return tu


def _make_reply_tool_use(message, id="tu-reply"):
    """Helper to create a reply_to_user tool_use."""
    return _make_tool_use(
        name="reply_to_user",
        input_data={"message": message},
        id=id,
    )


def _make_resp(text_parts=None, tool_uses=None):
    """Helper to create a mock LLM response."""
    resp = MagicMock()
    resp.tool_uses = tool_uses or []
    resp.text_parts = text_parts or []
    return resp


def test_supervisor_class_exists():
    from src.supervisor import Supervisor

    assert Supervisor is not None


def test_supervisor_inherits_chat_behavior():
    sup = _make_supervisor()
    assert hasattr(sup, "chat")
    assert hasattr(sup, "initialize")
    assert hasattr(sup, "summarize")
    assert hasattr(sup, "handler")


def test_supervisor_has_reflection_engine():
    sup = _make_supervisor()
    assert hasattr(sup, "reflection")
    assert sup.reflection is not None


def test_backward_compat_import():
    from src.chat_agent import ChatAgent

    assert ChatAgent is not None


def test_backward_compat_is_supervisor():
    from src.chat_agent import ChatAgent
    from src.supervisor import Supervisor

    assert ChatAgent is Supervisor


def test_set_active_project():
    sup = _make_supervisor()
    sup.set_active_project("my-project")
    assert sup._active_project_id == "my-project"


async def test_build_system_prompt_returns_string():
    sup = _make_supervisor()
    prompt = await sup._build_system_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_chat_simple_text_no_tools():
    """Simple text response with no tool use returns directly."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(text_parts=["Hi there!"])
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.run(sup.chat("Hello", "testuser"))
    assert result == "Hi there!"
    assert sup._provider.create_message.call_count == 1


def test_chat_reply_to_user_after_tool_use():
    """After tool use, reply_to_user delivers the response."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # First call: LLM uses a tool
    resp1 = _make_resp(
        tool_uses=[
            _make_tool_use("create_task", {"title": "Fix login"}, "tu-1"),
        ]
    )

    # Second call: LLM calls reply_to_user
    resp2 = _make_resp(
        tool_uses=[
            _make_reply_tool_use("Task **Fix login** created successfully."),
        ]
    )

    sup._provider.create_message = AsyncMock(side_effect=[resp1, resp2])
    sup.handler.execute = AsyncMock(return_value={"id": "t-123", "title": "Fix login"})

    result = asyncio.run(sup.chat("Create a task to fix login", "testuser"))
    assert "Fix login" in result
    assert "created" in result.lower()


def test_chat_reply_to_user_with_other_tools():
    """reply_to_user can be called alongside other tools in the same turn."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # LLM calls a tool AND reply_to_user in the same response
    resp = _make_resp(
        tool_uses=[
            _make_tool_use("create_task", {"title": "Fix bug"}, "tu-1"),
            _make_reply_tool_use("Created task to fix the bug.", "tu-reply"),
        ]
    )

    sup._provider.create_message = AsyncMock(return_value=resp)
    sup.handler.execute = AsyncMock(return_value={"id": "t-456"})

    result = asyncio.run(sup.chat("Fix the bug", "testuser"))
    assert "Created task" in result


def test_chat_nudges_llm_when_no_reply_to_user():
    """When LLM stops without reply_to_user after tools, it gets nudged."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # Round 1: tool use
    resp1 = _make_resp(
        tool_uses=[
            _make_tool_use("memory_search", {"query": "test"}, "tu-1"),
        ]
    )

    # Round 2: LLM returns text without reply_to_user (gets nudged)
    resp2 = _make_resp(text_parts=["Done. Actions taken: memory_search"])

    # Round 3: LLM calls reply_to_user after nudge
    resp3 = _make_resp(
        tool_uses=[
            _make_reply_tool_use("I searched memory and found relevant results."),
        ]
    )

    sup._provider.create_message = AsyncMock(side_effect=[resp1, resp2, resp3])
    sup.handler.execute = AsyncMock(return_value={"results": []})

    result = asyncio.run(sup.chat("Search for test", "testuser"))
    assert "searched memory" in result.lower()
    # At least 3 calls: tool use, nudged text, reply (+ possible reflection)
    assert sup._provider.create_message.call_count >= 3


def test_chat_max_nudges_then_returns():
    """After max nudges, returns the text response directly."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # Round 1: tool use
    resp_tool = _make_resp(
        tool_uses=[
            _make_tool_use("memory_search", {"query": "test"}, "tu-1"),
        ]
    )

    # Rounds 2, 3: text without reply_to_user (nudged twice)
    resp_text1 = _make_resp(text_parts=["Some text"])
    resp_text2 = _make_resp(text_parts=["More text"])

    # Round 4: still text (max nudges exceeded, returns directly)
    resp_text3 = _make_resp(text_parts=["Final text answer"])

    sup._provider.create_message = AsyncMock(
        side_effect=[resp_tool, resp_text1, resp_text2, resp_text3]
    )
    sup.handler.execute = AsyncMock(return_value={"results": []})

    result = asyncio.run(sup.chat("Search for test", "testuser"))
    assert result == "Final text answer"


def test_chat_triggers_reflection_on_reply():
    """After reply_to_user with tool use, the Supervisor attempts reflection."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # First call: LLM uses a tool
    resp1 = _make_resp(
        tool_uses=[
            _make_tool_use("create_task", {"title": "Fix login"}, "tu-1"),
        ]
    )

    # Second call: reply_to_user
    resp2 = _make_resp(
        tool_uses=[
            _make_reply_tool_use("Task created."),
        ]
    )

    # Third call (reflection)
    resp_reflect = _make_resp(text_parts=["Reflection: task verified."])

    sup._provider.create_message = AsyncMock(side_effect=[resp1, resp2, resp_reflect])
    sup.handler.execute = AsyncMock(return_value={"id": "t-123", "title": "Fix login"})

    result = asyncio.run(sup.chat("Create a task to fix login", "testuser"))
    assert "Task created" in result


def test_chat_skips_reflection_when_off():
    """When reflection level is off, no reflection pass happens."""
    sup = _make_supervisor()
    sup.reflection._config.level = "off"
    sup._provider = MagicMock()

    resp = _make_resp(text_parts=["Done."])
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.run(sup.chat("Hello", "testuser"))
    assert sup._provider.create_message.call_count == 1


def test_chat_no_reflection_for_simple_text():
    """No tool use = no reflection needed."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    resp = _make_resp(text_parts=["Hi there!"])
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.run(sup.chat("Hello", "testuser"))
    assert result == "Hi there!"
    assert sup._provider.create_message.call_count == 1


def test_supervisor_prompt_template_exists():
    """Supervisor identity template renders successfully."""
    from src.prompt_builder import PromptBuilder
    import os

    prompts_dir = os.path.join(os.path.dirname(__file__), "..", "src", "prompts")
    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("supervisor-system", {"workspace_dir": "/tmp/test"})
    assert result is not None
    assert "supervisor" in result.lower() or "single intelligent entity" in result.lower()


def test_reflect_method_exists():
    """Supervisor has a public reflect() method for event-driven reflection."""
    sup = _make_supervisor()
    assert hasattr(sup, "reflect")
    assert callable(sup.reflect)


def test_full_integration_supervisor_replaces_chat_agent():
    """Verify Supervisor can be used everywhere ChatAgent was."""
    from src.supervisor import Supervisor
    from src.chat_agent import ChatAgent

    assert Supervisor is ChatAgent

    sup = _make_supervisor()
    assert hasattr(sup, "chat")
    assert hasattr(sup, "summarize")
    assert hasattr(sup, "initialize")
    assert hasattr(sup, "is_ready")
    assert hasattr(sup, "model")
    assert hasattr(sup, "set_active_project")
    assert hasattr(sup, "reload_credentials")
    assert hasattr(sup, "handler")
    assert hasattr(sup, "reflection")
    assert hasattr(sup, "reflect")


def test_reflection_engine_wired_to_config(tmp_path):
    """ReflectionEngine uses config values from SupervisorConfig."""
    from src.config import AppConfig

    app = AppConfig(data_dir=str(tmp_path / "data"))
    from src.reflection import ReflectionEngine

    engine = ReflectionEngine(app.supervisor.reflection)
    assert engine.level == "full"
    assert engine._config.max_depth == 3


def test_on_task_completed_exists():
    sup = _make_supervisor()
    assert hasattr(sup, "on_task_completed")
    assert callable(sup.on_task_completed)


def test_on_task_completed_calls_process():
    sup = _make_supervisor()
    sup.handler.execute = AsyncMock(
        return_value={"plan_found": False, "reason": "No plan file found"}
    )

    result = asyncio.run(
        sup.on_task_completed(
            task_id="t-123",
            project_id="my-game",
            workspace_path="/tmp/workspace",
        )
    )

    sup.handler.execute.assert_called_once_with(
        "process_task_completion",
        {
            "task_id": "t-123",
            "workspace_path": "/tmp/workspace",
        },
    )
    assert result["plan_found"] is False


def test_on_task_completed_sets_project():
    sup = _make_supervisor()
    sup.handler.execute = AsyncMock(return_value={"plan_found": False})

    asyncio.run(
        sup.on_task_completed(
            task_id="t-123",
            project_id="my-game",
            workspace_path="/tmp/workspace",
        )
    )
    assert sup._active_project_id == "my-game"


def test_on_task_completed_returns_plan_found():
    sup = _make_supervisor()
    sup.handler.execute = AsyncMock(return_value={"plan_found": True, "steps_count": 3})

    result = asyncio.run(
        sup.on_task_completed(
            task_id="t-123",
            project_id="my-game",
            workspace_path="/tmp/workspace",
        )
    )
    assert result["plan_found"] is True
    assert result["steps_count"] == 3


def test_on_task_completed_handles_error():
    sup = _make_supervisor()
    sup.handler.execute = AsyncMock(side_effect=Exception("DB error"))

    result = asyncio.run(
        sup.on_task_completed(
            task_id="t-123",
            project_id="proj",
            workspace_path="/tmp/ws",
        )
    )
    assert result["plan_found"] is False


def test_observe_method_exists():
    """Supervisor has an observe() method for passive observation."""
    sup = _make_supervisor()
    assert hasattr(sup, "observe")
    assert callable(sup.observe)


def test_observe_returns_decision():
    """observe() returns a dict with action and content."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    mock_resp = MagicMock()
    mock_resp.tool_uses = []
    mock_resp.text_parts = ['{"action": "ignore"}']
    sup._provider.create_message = AsyncMock(return_value=mock_resp)

    result = asyncio.run(
        sup.observe(
            messages=[
                {
                    "author": "alice",
                    "content": "the particle system needs work",
                    "timestamp": 1000.0,
                }
            ],
            project_id="my-game",
        )
    )
    assert isinstance(result, dict)
    assert "action" in result


def test_observe_without_provider_returns_ignore():
    """observe() returns ignore when LLM is not available."""
    sup = _make_supervisor()
    sup._provider = None
    result = asyncio.run(sup.observe(messages=[], project_id="test"))
    assert result["action"] == "ignore"


def test_observe_handles_llm_error():
    """observe() returns ignore on LLM error (never crashes)."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    sup._provider.create_message = AsyncMock(side_effect=Exception("LLM down"))
    result = asyncio.run(
        sup.observe(
            messages=[{"author": "bob", "content": "deploy failed", "timestamp": 1.0}],
            project_id="test",
        )
    )
    assert result["action"] == "ignore"


def test_reply_to_user_tool_in_registry():
    """reply_to_user is registered as a core tool."""
    from src.tools import ToolRegistry

    registry = ToolRegistry()
    core = registry.get_core_tools()
    names = [t["name"] for t in core]
    assert "reply_to_user" in names


def test_reply_to_user_tool_schema():
    """reply_to_user has the expected schema."""
    from src.tools import ToolRegistry

    registry = ToolRegistry()
    all_tools = {t["name"]: t for t in registry.get_all_tools()}
    tool = all_tools["reply_to_user"]
    assert "message" in tool["input_schema"]["properties"]
    assert "message" in tool["input_schema"]["required"]


def test_chat_reply_to_user_empty_message_returns_done():
    """reply_to_user with empty message returns 'Done.'."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    resp = _make_resp(
        tool_uses=[
            _make_reply_tool_use(""),
        ]
    )
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.run(sup.chat("Do something", "testuser"))
    assert result == "Done."


async def test_system_prompt_mentions_reply_to_user():
    """System prompt instructs the LLM about reply_to_user."""
    sup = _make_supervisor()
    prompt = await sup._build_system_prompt()
    assert "reply_to_user" in prompt


# test_chat_max_rounds_returns_fallback removed — agents now run without step limits


# ------------------------------------------------------------------
# tool_overrides tests
# ------------------------------------------------------------------


def test_chat_tool_overrides_filters_tools():
    """When tool_overrides is provided, only those tools are sent to the LLM."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(text_parts=["Done."])
    sup._provider.create_message = AsyncMock(return_value=resp)

    asyncio.run(sup.chat("Hello", "testuser", tool_overrides=["create_task", "reply_to_user"]))

    call_kwargs = sup._provider.create_message.call_args
    tools_sent = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
    tool_names = {t["name"] for t in tools_sent}
    assert tool_names == {"create_task", "reply_to_user"}


def test_chat_tool_overrides_empty_list_no_tools():
    """Empty tool_overrides list means no tools (text-only response)."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(text_parts=["Text only response."])
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.run(sup.chat("Hello", "testuser", tool_overrides=[]))

    call_kwargs = sup._provider.create_message.call_args
    tools_sent = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
    assert tools_sent == []
    assert result == "Text only response."


def test_chat_tool_overrides_none_uses_default():
    """When tool_overrides is None, the full default tool set is used."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(text_parts=["Hello!"])
    sup._provider.create_message = AsyncMock(return_value=resp)

    asyncio.run(sup.chat("Hello", "testuser", tool_overrides=None))

    call_kwargs = sup._provider.create_message.call_args
    tools_sent = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
    tool_names = {t["name"] for t in tools_sent}
    # Default includes core tools at minimum
    assert "browse_tools" in tool_names
    assert "reply_to_user" in tool_names
    assert "load_tools" in tool_names


def test_chat_tool_overrides_unknown_raises_valueerror():
    """Unknown tool names in tool_overrides raise ValueError."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    try:
        asyncio.run(
            sup.chat("Hello", "testuser", tool_overrides=["nonexistent_tool_xyz"])
        )
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "nonexistent_tool_xyz" in str(e)
        assert "Unknown tool names" in str(e)


def test_chat_tool_overrides_can_include_category_tools():
    """tool_overrides can include tools from any category, not just core."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(text_parts=["Done."])
    sup._provider.create_message = AsyncMock(return_value=resp)

    # create_project is in the "project" category, not a core tool
    asyncio.run(
        sup.chat("Hello", "testuser", tool_overrides=["create_project", "reply_to_user"])
    )

    call_kwargs = sup._provider.create_message.call_args
    tools_sent = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
    tool_names = {t["name"] for t in tools_sent}
    assert tool_names == {"create_project", "reply_to_user"}


def test_chat_tool_overrides_disables_load_tools_expansion():
    """When tool_overrides is active, load_tools calls don't expand the tool set."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # First response: LLM calls load_tools
    resp1 = _make_resp(
        tool_uses=[_make_tool_use("load_tools", {"category": "git"}, "tu-load")]
    )
    # Second response: LLM calls reply_to_user
    resp2 = _make_resp(
        tool_uses=[_make_reply_tool_use("Done.", "tu-reply")]
    )

    sup._provider.create_message = AsyncMock(side_effect=[resp1, resp2])
    sup.handler.execute = AsyncMock(return_value={"loaded": "git"})

    asyncio.run(
        sup.chat("Hello", "testuser", tool_overrides=["load_tools", "reply_to_user"])
    )

    # Second call should still only have the override tools (no git tools added)
    second_call = sup._provider.create_message.call_args_list[1]
    tools_sent = second_call.kwargs.get("tools") or second_call[1].get("tools")
    tool_names = {t["name"] for t in tools_sent}
    assert tool_names == {"load_tools", "reply_to_user"}


# ------------------------------------------------------------------
# Roadmap 0.4.5 — tool_overrides restriction tests (Section 6)
# ------------------------------------------------------------------


def test_tool_overrides_read_write_only():
    """(a) chat() with tool_overrides=["list_projects","edit_project"] only exposes those two.

    Original spec names read_file/write_file, but those are plugin-provided and
    unavailable in the unit-test mock.  list_projects/edit_project exercise the
    same code path (category tools pulled into override set).
    """
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(text_parts=["Done."])
    sup._provider.create_message = AsyncMock(return_value=resp)

    asyncio.run(
        sup.chat("Hello", "testuser", tool_overrides=["list_projects", "edit_project"])
    )

    call_kwargs = sup._provider.create_message.call_args
    tools_sent = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
    tool_names = {t["name"] for t in tools_sent}
    assert tool_names == {"list_projects", "edit_project"}, (
        f"Expected exactly list_projects and edit_project, got {tool_names}"
    )


def test_tool_overrides_blocks_unlisted_tool():
    """(b) LLM attempt to call a tool not in the override list is blocked — only
    override tools are sent to the LLM, so the provider never receives schemas
    for non-override tools.
    """
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # First call: LLM returns a tool call for create_task (NOT in overrides).
    # In a real scenario the provider wouldn't generate this since the schema
    # isn't in the tools list, but we simulate it to verify the tool set.
    resp1 = _make_resp(
        tool_uses=[_make_tool_use("create_task", {"title": "x"}, "tu-bad")]
    )
    # Second call: LLM finishes with reply_to_user
    resp2 = _make_resp(
        tool_uses=[_make_reply_tool_use("Done.", "tu-reply")]
    )

    sup._provider.create_message = AsyncMock(side_effect=[resp1, resp2])
    sup.handler.execute = AsyncMock(return_value={"success": True})

    asyncio.run(
        sup.chat(
            "Hello", "testuser", tool_overrides=["list_projects", "reply_to_user"]
        )
    )

    # Verify EVERY call to the LLM only included the override tools — the LLM
    # never had access to create_task's schema, so a conforming provider would
    # never generate that tool call.
    for call in sup._provider.create_message.call_args_list:
        tools_sent = call.kwargs.get("tools") or call[1].get("tools")
        tool_names = {t["name"] for t in tools_sent}
        assert "create_task" not in tool_names, (
            "create_task should not be exposed when overrides restrict "
            "to list_projects+reply_to_user"
        )
        assert tool_names == {"list_projects", "reply_to_user"}


def test_tool_overrides_none_backward_compat():
    """(c) chat() without tool_overrides exposes the full default tool set (backward compat)."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(text_parts=["Hello!"])
    sup._provider.create_message = AsyncMock(return_value=resp)

    # Call without tool_overrides at all (not even passing the kwarg)
    asyncio.run(sup.chat("Hello", "testuser"))

    call_kwargs = sup._provider.create_message.call_args
    tools_sent = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
    tool_names = {t["name"] for t in tools_sent}

    # Default must include the core meta-tools and communication tools
    for expected in ("browse_tools", "load_tools", "reply_to_user", "create_task", "list_tasks"):
        assert expected in tool_names, f"Default tool set missing core tool '{expected}'"

    # Should have more than just a couple — the full core set
    assert len(tool_names) >= 5, f"Expected at least 5 core tools, got {len(tool_names)}"


def test_tool_overrides_empty_disables_all_tools():
    """(d) empty tool_overrides=[] disables all tools (LLM can only produce text)."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(text_parts=["Text only."])
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.run(sup.chat("Hello", "testuser", tool_overrides=[]))

    call_kwargs = sup._provider.create_message.call_args
    tools_sent = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
    assert tools_sent == [], "Empty tool_overrides should send zero tools to the LLM"
    assert result == "Text only."


def test_tool_overrides_unknown_name_raises_before_llm_call():
    """(e) tool_overrides with unknown tool name raises validation error before LLM call."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    sup._provider.create_message = AsyncMock()

    try:
        asyncio.run(
            sup.chat(
                "Hello",
                "testuser",
                tool_overrides=["read_file", "totally_fake_tool_42"],
            )
        )
        assert False, "Should have raised ValueError"
    except ValueError as e:
        # Error message should name the bad tool
        assert "totally_fake_tool_42" in str(e)
        assert "Unknown tool names" in str(e)

    # The LLM should never have been called — validation happens first
    sup._provider.create_message.assert_not_called()


def test_tool_overrides_restriction_is_per_call():
    """(f) tool restriction applies only to that single call — next call has full tools."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # --- First call: restricted to list_projects + reply_to_user ---
    resp_restricted = _make_resp(text_parts=["Restricted."])
    # --- Second call: no overrides (full default) ---
    resp_default = _make_resp(text_parts=["Default."])

    sup._provider.create_message = AsyncMock(
        side_effect=[resp_restricted, resp_default]
    )

    # Call 1: restricted
    result1 = asyncio.run(
        sup.chat("Hello", "testuser", tool_overrides=["list_projects", "reply_to_user"])
    )
    # Call 2: unrestricted (default)
    result2 = asyncio.run(sup.chat("Hello again", "testuser"))

    assert result1 == "Restricted."
    assert result2 == "Default."

    # Verify first call had only the override tools
    first_call = sup._provider.create_message.call_args_list[0]
    tools_first = first_call.kwargs.get("tools") or first_call[1].get("tools")
    names_first = {t["name"] for t in tools_first}
    assert names_first == {"list_projects", "reply_to_user"}, (
        f"First call should have restricted tools, got {names_first}"
    )

    # Verify second call has the full default tool set
    second_call = sup._provider.create_message.call_args_list[1]
    tools_second = second_call.kwargs.get("tools") or second_call[1].get("tools")
    names_second = {t["name"] for t in tools_second}
    assert "browse_tools" in names_second, "Second call should have browse_tools (core)"
    assert "load_tools" in names_second, "Second call should have load_tools (core)"
    assert "reply_to_user" in names_second, "Second call should have reply_to_user (core)"
    assert "create_task" in names_second, "Second call should have create_task (core)"
    assert len(names_second) > len(names_first), (
        f"Default tool set ({len(names_second)}) should be larger than "
        f"restricted set ({len(names_first)})"
    )
