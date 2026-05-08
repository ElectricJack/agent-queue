"""Tests for ``Supervisor.observe()`` Stage-2 LLM passive-observation.

These tests focus on the *state-aware prompt* introduced in Phase 3 of
the chat-analyzer suggestion-quality overhaul plan: ``observe()`` must
call ``CommandHandler.execute("list_tasks", …)`` to fetch the
project's active tasks and recently created tasks, then surface them in
the LLM prompt under ``### Active Tasks`` and
``### Recently Created (last 5 min)`` sections so the model can avoid
proposing duplicative work.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock


def _make_supervisor():
    """Construct a Supervisor with the minimal mocks needed for observe()."""
    from src.runtimes.supervisor import Supervisor

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
    return Supervisor(orch, config)


def _mock_provider_returning(text: str):
    """Build a mocked LLM provider whose create_message returns ``text``."""
    provider = MagicMock()
    resp = MagicMock()
    resp.tool_uses = []
    resp.text_parts = [text]
    provider.create_message = AsyncMock(return_value=resp)
    return provider


def _captured_prompt(provider) -> str:
    """Extract the user prompt sent to ``provider.create_message``."""
    assert provider.create_message.await_count >= 1, (
        "expected provider.create_message to have been awaited at least once"
    )
    call = provider.create_message.await_args_list[0]
    messages = call.kwargs.get("messages") or (call.args[0] if call.args else None)
    assert messages, "create_message must be called with messages"
    # observe() sends a single user message containing the assembled prompt.
    return messages[0]["content"]


def _messages():
    return [
        {
            "author": "alice",
            "content": "the particle system needs work",
            "timestamp": 1000.0,
        }
    ]


# ---------------------------------------------------------------------------
# Phase 3 — state-aware prompt: active tasks
# ---------------------------------------------------------------------------


def test_observe_includes_active_tasks_in_prompt():
    """The ``### Active Tasks`` section must surface in-flight task state.

    When ``list_tasks`` returns an in-progress task for the project, its
    title and status must appear in the prompt sent to the LLM provider
    so the model can reason about complementary (not duplicative) work.
    """
    sup = _make_supervisor()
    sup._provider = _mock_provider_returning('{"action": "ignore"}')

    active_task = {
        "id": "abc-123",
        "project_id": "my-game",
        "title": "Fix the particle renderer crash",
        "status": "IN_PROGRESS",
        "created_at": time.time() - 7200,  # two hours ago — not "recent"
        "updated_at": time.time() - 60,
    }
    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock(return_value={"tasks": [active_task]})

    asyncio.run(sup.observe(messages=_messages(), project_id="my-game"))

    prompt = _captured_prompt(sup._provider)
    assert "### Active Tasks" in prompt, (
        "prompt must include an `### Active Tasks` heading"
    )
    assert "Fix the particle renderer crash" in prompt, (
        "active task title must appear in the prompt"
    )
    assert "IN_PROGRESS" in prompt, "active task status must appear in the prompt"


# ---------------------------------------------------------------------------
# Phase 3 — state-aware prompt: recent task creations
# ---------------------------------------------------------------------------


def test_observe_includes_recent_task_creations():
    """The ``### Recently Created (last 5 min)`` section must surface fresh tasks.

    Tasks whose ``created_at`` falls within the last 5 minutes must
    appear in a dedicated section so the model knows we just created
    matching work and should not re-propose it.
    """
    sup = _make_supervisor()
    sup._provider = _mock_provider_returning('{"action": "ignore"}')

    now = time.time()
    recent_task = {
        "id": "def-456",
        "project_id": "my-game",
        "title": "Start step 9 workflow with atom-claude",
        "status": "DEFINED",
        "created_at": now - 30,  # 30 seconds ago — well within 5 min
        "updated_at": now - 30,
    }
    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock(return_value={"tasks": [recent_task]})

    asyncio.run(sup.observe(messages=_messages(), project_id="my-game"))

    prompt = _captured_prompt(sup._provider)
    assert "### Recently Created (last 5 min)" in prompt, (
        "prompt must include a `### Recently Created (last 5 min)` heading"
    )
    assert "Start step 9 workflow with atom-claude" in prompt, (
        "recent task title must appear in the prompt"
    )


# ---------------------------------------------------------------------------
# Phase 3 — graceful degradation when handler.execute fails
# ---------------------------------------------------------------------------


def test_observe_tolerates_handler_errors():
    """If ``handler.execute`` raises, observe() must still call the LLM.

    The fallback prompt should communicate that no active-task data is
    available rather than failing the whole observation.
    """
    sup = _make_supervisor()
    sup._provider = _mock_provider_returning('{"action": "ignore"}')

    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock(side_effect=RuntimeError("DB down"))

    result = asyncio.run(sup.observe(messages=_messages(), project_id="my-game"))

    assert isinstance(result, dict)
    assert result.get("action") == "ignore"
    prompt = _captured_prompt(sup._provider)
    assert "no active task data available" in prompt.lower(), (
        "fallback message must explain that no task data is available"
    )


# ---------------------------------------------------------------------------
# Phase 3 — instructions block
# ---------------------------------------------------------------------------


def test_observe_instructs_model_to_ignore_overlap_with_existing_tasks():
    """The LLM instructions must explicitly forbid duplicative suggestions.

    If the model's proposed ``task`` semantically overlaps with anything
    listed under ``### Active Tasks`` or ``### Recently Created``, it
    must respond ``{"action": "ignore"}``.
    """
    sup = _make_supervisor()
    sup._provider = _mock_provider_returning('{"action": "ignore"}')

    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock(return_value={"tasks": []})

    asyncio.run(sup.observe(messages=_messages(), project_id="my-game"))

    prompt = _captured_prompt(sup._provider)
    # The instructions must reference both prompt sections AND tell the
    # model to respond with the ignore action when overlap is detected.
    lowered = prompt.lower()
    assert "active tasks" in lowered, (
        "instructions must reference the Active Tasks section"
    )
    assert "recently created" in lowered, (
        "instructions must reference the Recently Created section"
    )
    assert "overlap" in lowered or "duplic" in lowered, (
        "instructions must mention semantic overlap / duplication"
    )
    assert '"action": "ignore"' in prompt, (
        "instructions must explicitly say to respond with ignore on overlap"
    )
