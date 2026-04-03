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


def test_build_system_prompt_returns_string():
    sup = _make_supervisor()
    prompt = sup._build_system_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_process_hook_llm_exists():
    sup = _make_supervisor()
    assert hasattr(sup, "process_hook_llm")
    assert callable(sup.process_hook_llm)


def test_process_hook_llm_sets_project():
    """process_hook_llm sets active project before processing."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    resp = _make_resp(tool_uses=[_make_reply_tool_use("Hook processed successfully")])
    sup._provider.create_message = AsyncMock(return_value=resp)
    sup.handler.execute = AsyncMock(return_value={"status": "ok"})

    result = asyncio.run(
        sup.process_hook_llm(
            hook_context="## Hook Context\nProject: test",
            rendered_prompt="Check tunnel status",
            project_id="my-game",
            hook_name="tunnel-monitor",
        )
    )
    assert "Hook processed" in result
    assert sup._active_project_id == "my-game"


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
    """Supervisor identity template renders with workspace_dir."""
    from src.prompt_builder import PromptBuilder
    import os

    prompts_dir = os.path.join(os.path.dirname(__file__), "..", "src", "prompts")
    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("supervisor-system", {"workspace_dir": "/tmp/test"})
    assert result is not None
    assert "/tmp/test" in result
    assert "supervisor" in result.lower() or "single intelligent entity" in result.lower()


def test_reflect_method_exists():
    """Supervisor has a public reflect() method for event-driven reflection."""
    sup = _make_supervisor()
    assert hasattr(sup, "reflect")
    assert callable(sup.reflect)


def test_process_hook_llm_uses_hook_trigger():
    """process_hook_llm triggers reflection with hook-specific trigger."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # First call: LLM uses a tool
    resp1 = _make_resp(
        tool_uses=[
            _make_tool_use("list_tasks", {"status": "ready"}, "tu-1"),
        ]
    )

    # Second call: reply_to_user
    resp2 = _make_resp(
        tool_uses=[
            _make_reply_tool_use("Hook done"),
        ]
    )

    sup._provider.create_message = AsyncMock(side_effect=[resp1, resp2])
    sup.handler.execute = AsyncMock(return_value={"tasks": []})

    # Track what trigger the reflection sees
    triggers_seen = []

    def track_should(trigger):
        triggers_seen.append(trigger)
        return False  # Skip actual reflection for test speed

    sup.reflection.should_reflect = track_should

    asyncio.run(
        sup.process_hook_llm(
            hook_context="ctx",
            rendered_prompt="prompt",
            project_id="p1",
            hook_name="my-hook",
        )
    )
    assert any("hook" in t for t in triggers_seen)


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
    assert hasattr(sup, "process_hook_llm")
    assert hasattr(sup, "reflect")


def test_reflection_engine_wired_to_config():
    """ReflectionEngine uses config values from SupervisorConfig."""
    from src.config import AppConfig

    app = AppConfig()
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
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    core = registry.get_core_tools()
    names = [t["name"] for t in core]
    assert "reply_to_user" in names


def test_reply_to_user_tool_schema():
    """reply_to_user has the expected schema."""
    from src.tool_registry import ToolRegistry

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


def test_system_prompt_mentions_reply_to_user():
    """System prompt instructs the LLM about reply_to_user."""
    sup = _make_supervisor()
    prompt = sup._build_system_prompt()
    assert "reply_to_user" in prompt


# test_chat_max_rounds_returns_fallback removed — agents now run without step limits
