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


# ---------------------------------------------------------------------------
# Phase 7 — _parse_observe_response unit tests
# ---------------------------------------------------------------------------


def test_parse_malformed_json_returns_ignore():
    """Malformed JSON in the LLM response must degrade to ``ignore``.

    The Stage-2 parser is the last line of defence between an unreliable
    LLM and the Discord-facing suggester.  When the model emits text that
    is not valid JSON, the parser must not raise — it must return
    ``{"action": "ignore"}`` so the bot stays silent.
    """
    sup = _make_supervisor()

    # A deliberately broken response — unterminated string + trailing junk.
    result = sup._parse_observe_response('{"action": "suggest", "content": "oops')
    assert result == {"action": "ignore"}

    # Plain non-JSON text should also degrade to ignore.
    assert sup._parse_observe_response("not json at all") == {"action": "ignore"}

    # Empty string is degenerate but must not raise.
    assert sup._parse_observe_response("") == {"action": "ignore"}


def test_parse_unknown_action_returns_ignore():
    """Valid JSON with an unrecognised ``action`` must degrade to ignore.

    The contract enumerates exactly three actions — ``ignore``, ``memory``,
    ``suggest``.  Anything else (typo, hallucinated verb, wrong language)
    must be treated as if the LLM said nothing.
    """
    sup = _make_supervisor()

    assert sup._parse_observe_response(
        '{"action": "delete_everything", "content": "go"}'
    ) == {"action": "ignore"}
    assert sup._parse_observe_response('{"action": "suggestion"}') == {
        "action": "ignore"
    }
    # Action key entirely missing — also degrades.
    assert sup._parse_observe_response('{"content": "stuff"}') == {"action": "ignore"}
    # JSON array (wrong shape) — must not raise.
    assert sup._parse_observe_response('["ignore"]') == {"action": "ignore"}


def test_parse_strips_code_fences_before_decoding():
    """The parser strips ``` fences before attempting to decode JSON.

    LLMs frequently wrap JSON output in fenced code blocks even when told
    not to.  The parser already handles this; the test pins the behavior
    so a future refactor cannot regress it.
    """
    sup = _make_supervisor()

    fenced = '```json\n{"action": "memory", "content": "particle bug noted"}\n```'
    result = sup._parse_observe_response(fenced)
    assert result["action"] == "memory"
    assert result["content"] == "particle bug noted"


# ---------------------------------------------------------------------------
# Phase 7 — happy paths for each action, with confidence-component fields
# ---------------------------------------------------------------------------


def test_parse_happy_path_ignore_preserves_confidence_fields():
    """``ignore`` action with intent/novelty/actionability fields passes through.

    The Phase 4 design adds three component scores
    (``intent_confidence``, ``novelty``, ``actionability``) to every
    response.  The parser is contractually backward-compatible: it must
    preserve those keys verbatim so downstream gates can read them.
    """
    sup = _make_supervisor()
    raw = (
        '{"action": "ignore", "intent_confidence": 0.2, '
        '"novelty": 0.1, "actionability": 0.0}'
    )
    result = sup._parse_observe_response(raw)
    assert result["action"] == "ignore"
    assert result["intent_confidence"] == 0.2
    assert result["novelty"] == 0.1
    assert result["actionability"] == 0.0


def test_parse_happy_path_memory_preserves_confidence_fields():
    """``memory`` action keeps its content + the three component scores."""
    sup = _make_supervisor()
    raw = (
        '{"action": "memory", "content": "user prefers dark mode", '
        '"intent_confidence": 0.7, "novelty": 0.6, "actionability": 0.3}'
    )
    result = sup._parse_observe_response(raw)
    assert result["action"] == "memory"
    assert result["content"] == "user prefers dark mode"
    assert result["intent_confidence"] == 0.7
    assert result["novelty"] == 0.6
    assert result["actionability"] == 0.3


def test_parse_happy_path_suggest_preserves_confidence_fields():
    """``suggest`` action keeps its full payload including the score components.

    The downstream Discord embed renderer needs ``content``,
    ``suggestion_type``, and ``task_title``; the gate stack needs the
    three score components.  All six fields must survive the parser.
    """
    sup = _make_supervisor()
    raw = (
        '{"action": "suggest", "content": "Add a task to refactor X", '
        '"suggestion_type": "task", "task_title": "Refactor X module", '
        '"intent_confidence": 0.9, "novelty": 0.8, "actionability": 0.85}'
    )
    result = sup._parse_observe_response(raw)
    assert result["action"] == "suggest"
    assert result["content"] == "Add a task to refactor X"
    assert result["suggestion_type"] == "task"
    assert result["task_title"] == "Refactor X module"
    assert result["intent_confidence"] == 0.9
    assert result["novelty"] == 0.8
    assert result["actionability"] == 0.85


# ---------------------------------------------------------------------------
# Phase 7 — observe() error/short-circuit paths
# ---------------------------------------------------------------------------


def test_observe_swallows_provider_exception():
    """A provider exception must be swallowed and degrade to ignore.

    ``observe()`` is invoked from the chat-message hot path; if the LLM
    provider raises (network blip, rate limit, malformed config) the bot
    must stay quiet rather than propagate the exception up to Discord.
    """
    sup = _make_supervisor()
    provider = MagicMock()
    provider.create_message = AsyncMock(side_effect=RuntimeError("kaboom"))
    sup._provider = provider

    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock(return_value={"tasks": []})

    result = asyncio.run(sup.observe(messages=_messages(), project_id="my-game"))
    assert result == {"action": "ignore"}
    # Confirm the provider WAS called — we want the exception path, not a
    # short-circuit before the provider was reached.
    assert provider.create_message.await_count == 1


def test_observe_empty_messages_short_circuits_without_provider_call():
    """An empty ``messages`` list must short-circuit before calling the provider.

    There is nothing for the LLM to classify; spending a token round-trip
    on an empty conversation would be pure waste.  The handler should
    likewise not be queried — the early exit happens before any work.
    """
    sup = _make_supervisor()
    provider = MagicMock()
    provider.create_message = AsyncMock()
    sup._provider = provider

    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock()

    result = asyncio.run(sup.observe(messages=[], project_id="my-game"))
    assert result == {"action": "ignore"}
    provider.create_message.assert_not_awaited()
    sup.handler.execute.assert_not_awaited()


def test_observe_no_provider_short_circuits():
    """When ``_provider`` is None the call must degrade to ignore.

    ``initialize()`` is the only path that sets ``_provider``; if it
    failed (no API key, unsupported provider) the supervisor stays
    operational for non-LLM duties but ``observe()`` becomes a no-op.
    """
    sup = _make_supervisor()
    sup._provider = None

    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock()

    result = asyncio.run(sup.observe(messages=_messages(), project_id="my-game"))
    assert result == {"action": "ignore"}
    sup.handler.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase 7 — assembled prompt structure (Conversation + state-aware sections)
# ---------------------------------------------------------------------------


def test_observe_prompt_includes_all_three_state_sections():
    """A single ``observe()`` call must emit the full state-aware prompt.

    The Phase 3 design requires three top-level sections:

    * ``### Conversation`` — the raw chat lines under analysis
    * ``### Active Tasks`` — every non-terminal task in the project
    * ``### Recently Created (last 5 min)`` — tasks created inside the
      window

    All three must appear in one assembled prompt so the LLM has the
    full state picture in a single call (no round-trips).
    """
    sup = _make_supervisor()
    sup._provider = _mock_provider_returning('{"action": "ignore"}')

    now = time.time()
    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock(
        return_value={
            "tasks": [
                {
                    "id": "t1",
                    "title": "Old in-flight task",
                    "status": "IN_PROGRESS",
                    "created_at": now - 7200,
                },
                {
                    "id": "t2",
                    "title": "Brand new task",
                    "status": "DEFINED",
                    "created_at": now - 30,
                },
            ]
        }
    )

    asyncio.run(sup.observe(messages=_messages(), project_id="my-game"))

    prompt = _captured_prompt(sup._provider)
    # All three section headings present.
    assert "### Conversation" in prompt
    assert "### Active Tasks" in prompt
    assert "### Recently Created (last 5 min)" in prompt
    # The conversation line must surface under ### Conversation.
    assert "the particle system needs work" in prompt
    assert "[alice]" in prompt
    # Both tasks listed under Active Tasks.
    assert "Old in-flight task" in prompt
    assert "Brand new task" in prompt
    # Only the recent task surfaces under Recently Created.  We assert this
    # by slicing the prompt at the heading and checking the right entry
    # appears in that slice.
    recent_slice = prompt.split("### Recently Created (last 5 min)", 1)[1]
    assert "Brand new task" in recent_slice
    assert "Old in-flight task" not in recent_slice.split("### ", 1)[0], (
        "the older task must not appear in the Recently Created section"
    )


def test_observe_prompt_conversation_section_renders_each_message():
    """The ``### Conversation`` section must render one line per message.

    Each line carries the author handle and content so the model can
    distinguish speakers in multi-party batches.  This pins the existing
    ``[author]: content`` wire-format introduced before Phase 3.
    """
    sup = _make_supervisor()
    sup._provider = _mock_provider_returning('{"action": "ignore"}')
    sup.handler = MagicMock()
    sup.handler.execute = AsyncMock(return_value={"tasks": []})

    multi = [
        {"author": "alice", "content": "the particle system needs work", "timestamp": 1.0},
        {"author": "bob", "content": "agreed, the renderer is leaky", "timestamp": 2.0},
    ]
    asyncio.run(sup.observe(messages=multi, project_id="my-game"))

    prompt = _captured_prompt(sup._provider)
    convo_section = prompt.split("### Conversation", 1)[1].split("###", 1)[0]
    assert "[alice]: the particle system needs work" in convo_section
    assert "[bob]: agreed, the renderer is leaky" in convo_section
