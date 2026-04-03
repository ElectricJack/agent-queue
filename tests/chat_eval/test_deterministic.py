"""Deterministic tests for supervisor loop mechanics (no LLM needed).

Uses ScriptedProvider to return pre-built responses. Tests verify:
- Single tool call + text response
- Multi-tool sequences
- Max iteration limit (10)
- Tool error propagation back to LLM
- Active project in system prompt
- History threading across turns

Updated: ChatAgent → Supervisor (post-supervisor refactor).
"""

from __future__ import annotations

import json
import pytest

from src.chat_providers.types import ChatResponse, TextBlock, ToolUseBlock


class TestSingleToolCall:
    """Test basic single tool call followed by text response."""

    async def test_tool_call_then_text(self, eval_agent):
        agent, recorder, provider = eval_agent

        # LLM returns a tool call, then after getting result returns text
        provider.add_tool_then_text("list_projects", {}, "Here are your projects.")

        response = await agent.chat("show me all projects", user_name="test")

        assert response == "Here are your projects."
        assert recorder.was_called("list_projects")
        assert recorder.tool_names_called == ["list_projects"]

    async def test_text_only_response(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_text("Hello! How can I help you?")

        response = await agent.chat("hi", user_name="test")

        assert response == "Hello! How can I help you?"
        assert recorder.tool_names_called == []

    async def test_tool_call_with_args(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_then_text(
            "create_project",
            {"name": "My App"},
            "Created project My App.",
        )

        response = await agent.chat("create a project called My App", user_name="test")

        assert recorder.was_called("create_project")
        calls = recorder.calls_for("create_project")
        assert len(calls) == 1
        assert calls[0].args["name"] == "My App"


class TestMultiToolSequence:
    """Test multiple tool calls in sequence."""

    async def test_two_sequential_tool_calls(self, eval_agent):
        agent, recorder, provider = eval_agent

        # First LLM response: tool call
        provider.add_tool_call("list_projects", {})
        # Second LLM response: another tool call
        provider.add_tool_call("list_agents", {})
        # Third LLM response: reply_to_user
        provider.add_reply("Here's the status overview.")

        response = await agent.chat("give me a system overview", user_name="test")

        assert response == "Here's the status overview."
        assert recorder.tool_names_called == ["list_projects", "list_agents"]

    async def test_parallel_tool_calls_in_single_response(self, eval_agent):
        agent, recorder, provider = eval_agent

        # Single response with multiple tool calls
        provider.add_tool_calls(
            [
                ("list_projects", {}),
                ("list_agents", {}),
            ]
        )
        provider.add_reply("Done.")

        response = await agent.chat("status", user_name="test")

        assert set(recorder.tool_names_called) == {"list_projects", "list_agents"}


# TestMaxIterations removed — agents now run without step limits


class TestToolErrorPropagation:
    """Test that tool errors are sent back to the LLM as tool_result."""

    async def test_error_result_sent_back(self, eval_agent):
        agent, recorder, provider = eval_agent

        # Call a tool with bad args that will error
        provider.add_tool_call("get_task", {"task_id": "nonexistent"})
        provider.add_reply("Sorry, that task wasn't found.")

        response = await agent.chat("show task nonexistent", user_name="test")

        # The tool was called (and errored), then LLM got the error back
        assert recorder.was_called("get_task")
        # Check that the provider saw the error in the second call's messages
        assert provider.call_count == 2
        second_call = provider.calls[1]
        # The last user message should contain tool_result with the error
        last_msg = second_call.messages[-1]
        assert last_msg["role"] == "user"
        assert any("tool_result" in str(item) for item in last_msg["content"])

    async def test_unknown_command_error(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_call("nonexistent_tool", {})
        provider.add_reply("I couldn't do that.")

        response = await agent.chat("do something weird", user_name="test")

        # The error should have been propagated
        assert provider.call_count == 2
        second_call = provider.calls[1]
        last_msg = second_call.messages[-1]
        tool_result_content = last_msg["content"][0]["content"]
        assert "Unknown command" in tool_result_content


class TestActiveProject:
    """Test that active project appears in system prompt."""

    async def test_active_project_in_system_prompt(self, eval_agent):
        agent, recorder, provider = eval_agent

        agent.set_active_project("proj-123")
        provider.add_text("OK")

        await agent.chat("hi", user_name="test")

        # Check the system prompt in the provider call
        assert provider.call_count == 1
        system_prompt = provider.calls[0].system
        assert "proj-123" in system_prompt
        assert "ACTIVE PROJECT" in system_prompt

    async def test_no_active_project(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_text("OK")

        await agent.chat("hi", user_name="test")

        system_prompt = provider.calls[0].system
        assert "ACTIVE PROJECT" not in system_prompt


class TestHistoryThreading:
    """Test that conversation history is threaded correctly across turns."""

    async def test_history_passed_to_provider(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_text("I see the context.")

        history = [
            {"role": "user", "content": "previous message"},
            {"role": "assistant", "content": "previous response"},
        ]

        await agent.chat("new message", user_name="test", history=history)

        messages = provider.calls[0].messages
        # Should have history + current message
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "previous message"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"
        assert "new message" in messages[2]["content"]

    async def test_user_name_prefix(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_text("Hello, Alice!")

        await agent.chat("hello", user_name="Alice")

        messages = provider.calls[0].messages
        assert "[from Alice]: hello" in messages[-1]["content"]

    async def test_tool_results_in_messages(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_call("list_projects", {})
        provider.add_reply("Found projects.")

        await agent.chat("list projects", user_name="test")

        # After tool call, messages should contain tool_result
        assert provider.call_count == 2
        second_messages = provider.calls[1].messages
        # Should have: user msg, assistant (tool_use), user (tool_result)
        assert len(second_messages) == 3
        assert second_messages[1]["role"] == "assistant"
        assert second_messages[2]["role"] == "user"


class TestRecorderFunctionality:
    """Test RecordingCommandHandler tracking features."""

    async def test_recorder_tracks_calls(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_call("list_projects", {})
        provider.add_tool_call("list_agents", {})
        provider.add_reply("Done.")

        await agent.chat("overview", user_name="test")

        assert len(recorder.calls) == 2
        assert recorder.tool_names_called == ["list_projects", "list_agents"]
        assert recorder.was_called("list_projects")
        assert recorder.was_called("list_agents")
        assert not recorder.was_called("create_task")

    async def test_recorder_reset(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_then_text("list_projects", {}, "Done.")

        await agent.chat("list", user_name="test")
        assert len(recorder.calls) == 1

        recorder.reset()
        assert len(recorder.calls) == 0
        assert recorder.tool_names_called == []

    async def test_recorder_calls_for(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_call("list_projects", {})
        provider.add_tool_call("list_projects", {})
        provider.add_reply("Done.")

        await agent.chat("list twice", user_name="test")

        assert len(recorder.calls_for("list_projects")) == 2
        assert len(recorder.calls_for("list_agents")) == 0


class TestShowAllMapping:
    """Test the show_all -> include_completed mapping in _execute_tool."""

    async def test_show_all_maps_to_include_completed(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_call("list_tasks", {"show_all": True})
        provider.add_reply("Here are all tasks.")

        await agent.chat("show all tasks", user_name="test")

        calls = recorder.calls_for("list_tasks")
        assert len(calls) == 1
        # The handler should receive include_completed=True
        assert calls[0].args.get("include_completed") is True
        # show_all should be removed
        assert "show_all" not in calls[0].args


class TestEmptyResponse:
    """Test edge case where provider returns empty content."""

    async def test_empty_text_after_tools_triggers_nudge(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_call("list_projects", {})
        # Empty text response — triggers nudge to call reply_to_user
        provider.add_response(ChatResponse(content=[TextBlock(text="")]))
        # After nudge, LLM calls reply_to_user
        provider.add_reply("Here are the projects I found.")

        response = await agent.chat("do something", user_name="test")

        assert "projects" in response.lower()

    async def test_empty_text_exhausts_nudges(self, eval_agent):
        """When nudges are exhausted, returns final text or Done."""
        agent, recorder, provider = eval_agent

        provider.add_tool_call("list_projects", {})
        # Empty text responses exhaust nudges (max 2)
        provider.add_response(ChatResponse(content=[TextBlock(text="")]))
        provider.add_response(ChatResponse(content=[TextBlock(text="")]))
        # Third empty text — nudges exhausted, returns directly
        provider.add_response(ChatResponse(content=[TextBlock(text="")]))

        response = await agent.chat("do something", user_name="test")

        assert response == "Done."

    async def test_no_provider_raises(self):
        """Test that chat() raises if provider not initialized."""
        from src.supervisor import Supervisor

        # Create supervisor without initializing provider
        agent = Supervisor.__new__(Supervisor)
        agent._provider = None

        with pytest.raises(RuntimeError, match="LLM provider not initialized"):
            await agent.chat("hello", user_name="test")
