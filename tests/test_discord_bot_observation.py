"""Tests for AgentQueueBot._post_observation_suggestion.

Covers Phase 1 of the chat-analyzer suggestion-quality overhaul: the bot
must compute a deterministic suggestion_hash and pass it to
``db.create_chat_analyzer_suggestion``. Without this, the DB insert
raises (column is nullable=False and the method signature requires it),
silently swallowing every suggestion record and leaving the dedup table
empty in production.

These tests pin the contract by calling the bound method directly on a
minimal stand-in object so we don't need to spin up a real
``commands.Bot``.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_bot_stub(
    *,
    db_returns_id: int | None = 99,
    min_confidence: float = 0.0,
    in_flight_min_confidence: float = 0.85,
    dismiss_cooldown_seconds: int = 0,
):
    """Build a minimal object with the attributes ``_post_observation_suggestion`` touches.

    The default ``min_confidence=0.0`` keeps the legacy Phase 1 / Phase 2
    tests neutral — every suggestion clears the gate.  Phase 4 tests
    pass an explicit threshold to exercise the gate.

    Phase 5 — ``in_flight_min_confidence`` (default ``0.85``) is the
    higher bar suggestions must clear when the project has an active
    IN_PROGRESS task.  The default stub seeds
    ``handler.execute("list_tasks", …)`` to return an empty task list so
    legacy tests never trip the in-flight gate; tests that exercise the
    gate override ``stub.agent.handler.execute`` to seed an active task.

    Phase 6 — ``dismiss_cooldown_seconds`` (default ``0`` here, vs the
    production default of ``600``) is the post-dismissal silence window
    in seconds.  We default to ``0`` for the stub so legacy tests do not
    accidentally trip the cooldown gate; Phase 6 tests pass an explicit
    value (e.g. ``600``).  ``db.get_last_dismiss_time`` defaults to
    ``None`` (no prior dismissal) so the gate stays quiet unless a test
    explicitly seeds a recent dismissal.

    Returns a tuple of ``(stub, channel, db)`` so tests can assert against
    the mocked dependencies directly.
    """
    db = MagicMock()
    db.create_chat_analyzer_suggestion = AsyncMock(return_value=db_returns_id)
    # Phase 8 footprint write — referenced by gate-suppression branches.
    # Default to a no-op success so tests that don't seed the dedup gate
    # (or other gates) don't have to wire it up themselves.
    db.create_suppressed_chat_analyzer_suggestion = AsyncMock(return_value=0)
    # Phase 2 dedup query — default to "novel" so legacy tests post.
    db.get_suggestion_hash_exists = AsyncMock(return_value=False)
    # Phase 6 dismiss-cooldown query — default to "no prior dismissal" so
    # legacy tests are not silenced by the new gate.
    db.get_last_dismiss_time = AsyncMock(return_value=None)

    channel = MagicMock()
    channel.send = AsyncMock(return_value=None)

    handler = MagicMock()
    # Phase 5 — the in-flight gate calls handler.execute("list_tasks", ...).
    # Default to "no active tasks" so the gate stays quiet for every test
    # that does not explicitly seed an active task.
    handler.execute = AsyncMock(return_value={"tasks": []})
    agent = SimpleNamespace(handler=handler)

    orchestrator = SimpleNamespace(db=db)

    chat_analyzer_cfg = SimpleNamespace(
        min_confidence=min_confidence,
        in_flight_min_confidence=in_flight_min_confidence,
        dismiss_cooldown_seconds=dismiss_cooldown_seconds,
    )
    config = SimpleNamespace(chat_analyzer=chat_analyzer_cfg)

    stub = SimpleNamespace(
        agent=agent,
        orchestrator=orchestrator,
        config=config,
        get_channel=lambda _cid: channel,
    )
    return stub, channel, db


@pytest.mark.asyncio
async def test_post_observation_suggestion_persists_hash():
    """The bot must pass a non-empty suggestion_hash derived from
    (suggestion_type, content, project_id) to the DB insert.
    """
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub()

    project_id = "my-game"
    suggestion = {
        "suggestion_type": "task",
        "content": "Add a particle renderer benchmark",
        "task_title": "Benchmark particle renderer",
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id=project_id,
        suggestion=suggestion,
    )

    # Confirm the DB write happened and was given a non-empty hash.
    assert db.create_chat_analyzer_suggestion.await_count == 1
    kwargs = db.create_chat_analyzer_suggestion.await_args.kwargs
    assert kwargs["project_id"] == project_id
    assert kwargs["channel_id"] == 12345
    assert kwargs["suggestion_type"] == "task"
    assert kwargs["suggestion_text"] == "Add a particle renderer benchmark"

    suggestion_hash = kwargs["suggestion_hash"]
    assert isinstance(suggestion_hash, str)
    assert suggestion_hash, "suggestion_hash must not be empty"
    # SHA-256 hex digest is 64 lowercase hex chars
    assert len(suggestion_hash) == 64
    int(suggestion_hash, 16)  # raises if not hex

    # And the embed actually got sent.
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_post_observation_suggestion_hash_is_deterministic():
    """Same (project_id, suggestion_type, content) must produce the same hash."""
    from src.discord.bot import AgentQueueBot

    project_id = "my-game"
    suggestion = {
        "suggestion_type": "task",
        "content": "Add a particle renderer benchmark",
        "task_title": "Benchmark particle renderer",
    }

    stub_a, _, db_a = _make_bot_stub()
    stub_b, _, db_b = _make_bot_stub()

    await AgentQueueBot._post_observation_suggestion(
        stub_a, channel_id=1, project_id=project_id, suggestion=suggestion
    )
    await AgentQueueBot._post_observation_suggestion(
        stub_b, channel_id=2, project_id=project_id, suggestion=suggestion
    )

    hash_a = db_a.create_chat_analyzer_suggestion.await_args.kwargs["suggestion_hash"]
    hash_b = db_b.create_chat_analyzer_suggestion.await_args.kwargs["suggestion_hash"]
    assert hash_a == hash_b


@pytest.mark.asyncio
async def test_post_observation_suggestion_hash_normalizes_text():
    """Whitespace and case differences in content must not produce different hashes."""
    from src.discord.bot import AgentQueueBot

    project_id = "my-game"
    base = {
        "suggestion_type": "task",
        "content": "Add a particle renderer benchmark",
        "task_title": "Benchmark",
    }
    noisy = {
        "suggestion_type": "task",
        "content": "  ADD  a   Particle\tRenderer\nBenchmark  ",
        "task_title": "Benchmark",
    }

    stub_a, _, db_a = _make_bot_stub()
    stub_b, _, db_b = _make_bot_stub()

    await AgentQueueBot._post_observation_suggestion(
        stub_a, channel_id=1, project_id=project_id, suggestion=base
    )
    await AgentQueueBot._post_observation_suggestion(
        stub_b, channel_id=1, project_id=project_id, suggestion=noisy
    )

    hash_a = db_a.create_chat_analyzer_suggestion.await_args.kwargs["suggestion_hash"]
    hash_b = db_b.create_chat_analyzer_suggestion.await_args.kwargs["suggestion_hash"]
    assert hash_a == hash_b


def test_compute_suggestion_hash_is_sha256_hex():
    """The helper returns a 64-char lowercase hex sha256 digest."""
    from src.discord.bot import AgentQueueBot

    digest = AgentQueueBot._compute_suggestion_hash(
        project_id="proj",
        suggestion_type="task",
        text="Hello world",
    )
    assert len(digest) == 64
    int(digest, 16)
    # Sanity: matches an explicit sha256 over the documented normalization
    expected = hashlib.sha256(b"proj\x00task\x00hello world").hexdigest()
    assert digest == expected


# ---------------------------------------------------------------------------
# Phase 4 — confidence gate in _post_observation_suggestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_suggestion_is_suppressed(caplog):
    """Suggestions whose confidence falls below the threshold must NOT post.

    With ``chat_analyzer.min_confidence = 0.6`` and a suggestion carrying
    ``confidence = 0.3``, the bot must:
      * skip ``channel.send`` entirely (no Discord post)
      * NOT insert a regular ``status="pending"`` row (the LLM-side
        ``confidence < threshold`` is the gate; we don't queue rejected
        suggestions). Phase 8 instead writes a separate
        ``status="suppressed"`` footprint via
        ``create_suppressed_chat_analyzer_suggestion`` — that path is
        covered in ``test_chat_analyzer_metrics.py`` against a real DB
        rather than the MagicMock stub used here.
      * emit a structured log line tagged ``gate="confidence"`` so Phase 8
        metrics can count suppressions per gate
    """
    import logging

    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(min_confidence=0.6)

    suggestion = {
        "suggestion_type": "task",
        "content": "Add a benchmark for the renderer",
        "task_title": "Benchmark renderer",
        "confidence": 0.3,
        "intent_confidence": 0.6,
        "novelty": 1.0,
        "actionability": 0.5,
    }

    with caplog.at_level(logging.INFO, logger="src.discord.bot"):
        await AgentQueueBot._post_observation_suggestion(
            stub,
            channel_id=12345,
            project_id="my-game",
            suggestion=suggestion,
        )

    # No Discord post
    assert channel.send.await_count == 0, (
        "low-confidence suggestion must not be posted to Discord"
    )
    # No DB row
    assert db.create_chat_analyzer_suggestion.await_count == 0, (
        "low-confidence suggestion must not be persisted"
    )
    # Structured log with gate=confidence
    matched = [
        rec
        for rec in caplog.records
        if getattr(rec, "gate", None) == "confidence"
    ]
    assert matched, (
        "expected an INFO log record carrying extra={'gate': 'confidence'}"
    )


@pytest.mark.asyncio
async def test_high_confidence_suggestion_passes_gate_and_posts():
    """When confidence >= threshold the suggestion must post normally."""
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(min_confidence=0.6)

    suggestion = {
        "suggestion_type": "task",
        "content": "Refactor the particle pool allocator",
        "task_title": "Refactor allocator",
        "confidence": 0.85,
        "intent_confidence": 0.95,
        "novelty": 1.0,
        "actionability": 0.9,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="my-game",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1, (
        "high-confidence suggestion must reach Discord"
    )
    assert db.create_chat_analyzer_suggestion.await_count == 1, (
        "high-confidence suggestion must be persisted"
    )


@pytest.mark.asyncio
async def test_missing_confidence_defaults_to_pass_through_when_threshold_zero():
    """Backward-compat: a suggestion lacking ``confidence`` must still post
    when the threshold is the default-permissive ``0.0`` used by legacy tests.
    """
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(min_confidence=0.0)

    suggestion = {
        "suggestion_type": "task",
        "content": "anything",
        "task_title": "anything",
        # no confidence key — legacy LLM response shape
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=1,
        project_id="p",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1
    assert db.create_chat_analyzer_suggestion.await_count == 1


# ---------------------------------------------------------------------------
# Phase 2 — hash-dedup gate in _post_observation_suggestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_hash_suggestion_is_suppressed(caplog):
    """Suggestions whose hash already exists for the project must NOT post.

    Phase 2 wires ``db.get_suggestion_hash_exists(project_id, suggestion_hash)``
    into ``_post_observation_suggestion``. When the hash is already present,
    the bot must:
      * skip ``channel.send`` entirely (no Discord post)
      * NOT insert a new ``status="pending"`` row via
        ``create_chat_analyzer_suggestion`` (the row already exists — that
        is why we are deduping)
      * emit a structured INFO log line tagged ``gate="dedup"`` so the
        Phase-8 metrics command can count dedup-suppressions per gate
    """
    import logging

    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(min_confidence=0.0)
    # Pre-seed the dedup gate to claim the hash already exists.
    db.get_suggestion_hash_exists = AsyncMock(return_value=True)
    # Phase-8 footprint write is best-effort and wrapped in try/except in
    # production code; provide a plausible AsyncMock so the await succeeds
    # if the implementation chooses to record a footprint here.
    db.create_suppressed_chat_analyzer_suggestion = AsyncMock(return_value=42)

    suggestion = {
        "suggestion_type": "task",
        "content": "Add a particle renderer benchmark",
        "task_title": "Benchmark particle renderer",
        # confidence is well above the (zero) threshold so we know any
        # suppression we observe came from the dedup gate, not the
        # confidence gate.
        "confidence": 0.95,
        "intent_confidence": 1.0,
        "novelty": 1.0,
        "actionability": 0.95,
    }

    with caplog.at_level(logging.INFO, logger="src.discord.bot"):
        await AgentQueueBot._post_observation_suggestion(
            stub,
            channel_id=12345,
            project_id="p1",
            suggestion=suggestion,
        )

    # No Discord post
    assert channel.send.await_count == 0, (
        "duplicate-hash suggestion must not be posted to Discord"
    )
    # No new pending DB row — the existing one is the one we deduped against
    assert db.create_chat_analyzer_suggestion.await_count == 0, (
        "duplicate-hash suggestion must not create a new pending row"
    )
    # Structured log with gate=dedup
    matched = [
        rec for rec in caplog.records if getattr(rec, "gate", None) == "dedup"
    ]
    assert matched, (
        "expected an INFO log record carrying extra={'gate': 'dedup'}"
    )
    # Sanity: the dedup query was actually consulted
    assert db.get_suggestion_hash_exists.await_count == 1, (
        "dedup gate must call get_suggestion_hash_exists"
    )


@pytest.mark.asyncio
async def test_distinct_hash_is_not_suppressed():
    """When the hash is NOT already in the table the suggestion must post.

    This proves the dedup gate is selective rather than a blanket mute.
    """
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(min_confidence=0.0)
    db.get_suggestion_hash_exists = AsyncMock(return_value=False)

    suggestion = {
        "suggestion_type": "task",
        "content": "Refactor the particle pool allocator",
        "task_title": "Refactor allocator",
        "confidence": 0.95,
        "intent_confidence": 1.0,
        "novelty": 1.0,
        "actionability": 0.95,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="p1",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1, (
        "novel-hash suggestion must reach Discord"
    )
    assert db.create_chat_analyzer_suggestion.await_count == 1, (
        "novel-hash suggestion must be persisted as a pending row"
    )
    assert db.get_suggestion_hash_exists.await_count == 1, (
        "dedup gate must query before posting, even when it ultimately allows"
    )


@pytest.mark.asyncio
async def test_post_observation_suggestion_passes_real_confidence_to_embed(monkeypatch):
    """The embed must receive the suggestion's real confidence, not a constant.

    Pre-Phase-4, the call site hardcoded ``confidence=0.8`` regardless of
    the LLM response.  Phase 4 wires the real value through.  We verify
    by intercepting ``format_suggestion_embed`` and inspecting the
    ``confidence`` argument it receives.
    """
    from src.discord import bot as bot_module
    from src.discord import views as views_module
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(min_confidence=0.0)

    captured: dict = {}

    def _fake_embed(suggestion_type, text, project_id, confidence):
        captured["confidence"] = confidence
        captured["suggestion_type"] = suggestion_type
        captured["text"] = text
        captured["project_id"] = project_id
        # Return a dummy embed-like object so channel.send accepts it.
        return SimpleNamespace(_kind="embed")

    # ``_post_observation_suggestion`` does ``from src.discord.views import …``
    # locally, so patch the symbol on the views module — both paths point at
    # the same callable.
    monkeypatch.setattr(views_module, "format_suggestion_embed", _fake_embed)
    # Also patch the module-level alias the bot already imported, in case
    # an earlier import bound the symbol directly.
    if hasattr(bot_module, "format_suggestion_embed"):
        monkeypatch.setattr(bot_module, "format_suggestion_embed", _fake_embed)

    suggestion = {
        "suggestion_type": "task",
        "content": "Polish the menu animations",
        "task_title": "Polish menu animations",
        "confidence": 0.42,
        "intent_confidence": 0.7,
        "novelty": 1.0,
        "actionability": 0.6,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=42,
        project_id="my-game",
        suggestion=suggestion,
    )

    assert captured.get("confidence") == pytest.approx(0.42), (
        "format_suggestion_embed must receive the dynamic confidence, not 0.8"
    )


# ---------------------------------------------------------------------------
# Phase 5 — in-flight active task escalates the confidence threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggestion_in_channel_with_active_task_uses_higher_threshold(caplog):
    """When the project has an active IN_PROGRESS task, raise the bar.

    A confidence value that would normally pass the base ``min_confidence``
    gate (here ``0.7`` vs threshold ``0.6``) must still be suppressed when
    an in-flight task exists for the project, because the user is watching
    execution rather than shopping for new work. The bot must:

      * skip ``channel.send`` entirely (no Discord post)
      * NOT insert a regular ``status="pending"`` row — like the other
        gates we are rejecting, not queueing
      * emit a structured INFO log line tagged
        ``gate="in_flight_active_task"`` so Phase-8 metrics can count
        in-flight suppressions per gate
      * record a Phase-8 footprint via
        ``create_suppressed_chat_analyzer_suggestion`` with
        ``suppressed_by="in_flight_active_task"`` so the metrics command
        sees the rejection without scanning logs
    """
    import logging

    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
    )
    # Seed an active IN_PROGRESS task for the project so the in-flight
    # lookup returns at least one row.
    stub.agent.handler.execute = AsyncMock(
        return_value={
            "tasks": [
                {
                    "id": "calm-pinnacle",
                    "title": "Run step 9 workflow",
                    "status": "IN_PROGRESS",
                }
            ]
        }
    )

    suggestion = {
        "suggestion_type": "task",
        "content": "Add a benchmark for the renderer",
        "task_title": "Benchmark renderer",
        # 0.7 is above the 0.6 base gate but below the 0.85 in-flight bar.
        "confidence": 0.7,
        "intent_confidence": 0.85,
        "novelty": 0.95,
        "actionability": 0.87,
    }

    with caplog.at_level(logging.INFO, logger="src.discord.bot"):
        await AgentQueueBot._post_observation_suggestion(
            stub,
            channel_id=12345,
            project_id="my-game",
            suggestion=suggestion,
        )

    # No Discord post
    assert channel.send.await_count == 0, (
        "in-flight suppression must not post to Discord"
    )
    # No new pending DB row
    assert db.create_chat_analyzer_suggestion.await_count == 0, (
        "in-flight suppression must not create a pending suggestion row"
    )
    # The lookup must actually have happened — the gate cannot decide
    # without consulting the orchestrator.
    assert stub.agent.handler.execute.await_count >= 1, (
        "in-flight gate must call handler.execute(list_tasks, ...)"
    )
    list_tasks_calls = [
        call
        for call in stub.agent.handler.execute.await_args_list
        if call.args and call.args[0] == "list_tasks"
    ]
    assert list_tasks_calls, (
        "in-flight gate must invoke the list_tasks command"
    )
    # And the lookup must scope to the project so a different project's
    # active work doesn't accidentally silence this channel.
    args_dict = list_tasks_calls[-1].args[1]
    assert args_dict.get("project_id") == "my-game"
    # Status filter is not strictly required (the supervisor's analogous
    # call also accepts the broader non-terminal sweep), but we expect the
    # bot to ask specifically for IN_PROGRESS so the gate fires only when
    # work is actually executing — not merely DEFINED/READY in the queue.
    assert args_dict.get("status") == "IN_PROGRESS"

    # Structured log
    matched = [
        rec
        for rec in caplog.records
        if getattr(rec, "gate", None) == "in_flight_active_task"
    ]
    assert matched, (
        "expected an INFO log record carrying extra={'gate': 'in_flight_active_task'}"
    )

    # Phase-8 footprint with the right suppressed_by tag
    assert db.create_suppressed_chat_analyzer_suggestion.await_count == 1
    footprint_kwargs = (
        db.create_suppressed_chat_analyzer_suggestion.await_args.kwargs
    )
    assert footprint_kwargs.get("suppressed_by") == "in_flight_active_task"
    assert footprint_kwargs.get("project_id") == "my-game"


@pytest.mark.asyncio
async def test_high_confidence_suggestion_still_posts_with_active_task():
    """Confidence at or above ``in_flight_min_confidence`` clears the bar.

    Same setup as the suppression test (active IN_PROGRESS task seeded,
    base ``min_confidence=0.6``, in-flight ``0.85``) but confidence is
    ``0.9`` — the suggestion is high-signal enough to interrupt even when
    the user is watching another task execute.
    """
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
    )
    # Active task seeded — the gate sees in-flight work and switches to
    # the elevated threshold.
    stub.agent.handler.execute = AsyncMock(
        return_value={
            "tasks": [
                {
                    "id": "calm-pinnacle",
                    "title": "Run step 9 workflow",
                    "status": "IN_PROGRESS",
                }
            ]
        }
    )
    # Phase-2 dedup must allow the post — explicit so the test reads
    # without depending on the MagicMock default.
    db.get_suggestion_hash_exists = AsyncMock(return_value=False)

    suggestion = {
        "suggestion_type": "task",
        "content": "Two stuck tasks older than 30 minutes — investigate",
        "task_title": "Investigate stuck tasks",
        # 0.9 clears the elevated 0.85 bar.
        "confidence": 0.9,
        "intent_confidence": 0.95,
        "novelty": 1.0,
        "actionability": 0.95,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="my-game",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1, (
        "high-signal suggestion must reach Discord even with active task"
    )
    assert db.create_chat_analyzer_suggestion.await_count == 1, (
        "high-signal suggestion must be persisted as a pending row"
    )
    # No suppression footprint should have been written for the in-flight gate.
    in_flight_footprints = [
        call
        for call in db.create_suppressed_chat_analyzer_suggestion.await_args_list
        if call.kwargs.get("suppressed_by") == "in_flight_active_task"
    ]
    assert not in_flight_footprints, (
        "high-signal suggestion must not record an in-flight suppression footprint"
    )


@pytest.mark.asyncio
async def test_in_flight_threshold_does_not_apply_without_active_tasks():
    """Regression: a mid-confidence suggestion still posts when no task is in flight.

    Same confidence as the suppression test (``0.7``) and same thresholds,
    but ``handler.execute`` returns no tasks — the elevated bar does not
    apply, the basic gate (``0.6``) is the only one in play, and the
    suggestion posts normally.
    """
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
    )
    # No active tasks — the in-flight gate must not fire.
    stub.agent.handler.execute = AsyncMock(return_value={"tasks": []})
    db.get_suggestion_hash_exists = AsyncMock(return_value=False)

    suggestion = {
        "suggestion_type": "task",
        "content": "Refactor the particle pool allocator",
        "task_title": "Refactor allocator",
        "confidence": 0.7,
        "intent_confidence": 0.85,
        "novelty": 0.95,
        "actionability": 0.87,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="my-game",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1
    assert db.create_chat_analyzer_suggestion.await_count == 1


@pytest.mark.asyncio
async def test_in_flight_lookup_failure_falls_back_to_base_gate(caplog):
    """A handler error during the active-task lookup must not crash the
    suggestion pipeline; it must fall through and let the base gate decide.

    This mirrors the defensive try/except patterns elsewhere in
    ``_post_observation_suggestion`` (Phase 2 dedup, Phase 4/5/8
    footprint writes). Observability never blocks delivery.
    """
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
    )
    # Simulate an orchestrator error on the lookup.
    stub.agent.handler.execute = AsyncMock(side_effect=RuntimeError("DB down"))
    db.get_suggestion_hash_exists = AsyncMock(return_value=False)

    suggestion = {
        "suggestion_type": "task",
        "content": "Polish the menu animations",
        "task_title": "Polish menu animations",
        # Above the basic gate; the in-flight lookup would normally
        # decide whether to escalate — but the lookup failed.
        "confidence": 0.7,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="my-game",
        suggestion=suggestion,
    )

    # Lookup failure → treat as "no active tasks" → base gate already
    # cleared → suggestion still posts.
    assert channel.send.await_count == 1
    assert db.create_chat_analyzer_suggestion.await_count == 1


# ---------------------------------------------------------------------------
# Phase 6 — dismiss cooldown gate in _post_observation_suggestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_dismissal_silences_channel(caplog, monkeypatch):
    """A recent user dismissal in a channel must mute new suggestions.

    Phase 6 wires ``db.get_last_dismiss_time(project_id, channel_id)``
    into ``_post_observation_suggestion``. When the most recent
    ``resolved_at`` for a dismissed suggestion in this channel is
    within ``chat_analyzer.dismiss_cooldown_seconds`` of "now", the bot
    must:

      * skip ``channel.send`` entirely (no Discord post)
      * NOT insert a regular ``status="pending"`` row
      * emit a structured INFO log line tagged
        ``gate="dismiss_cooldown"`` so Phase-8 metrics can count
        cooldown suppressions per gate
      * record a Phase-8 footprint via
        ``create_suppressed_chat_analyzer_suggestion`` with
        ``suppressed_by="dismiss_cooldown"``
    """
    import logging
    import time as time_module

    from src.discord import bot as bot_module
    from src.discord.bot import AgentQueueBot

    # Pin "now" so the cooldown math is deterministic.
    fake_now = 1_700_000_000.0
    monkeypatch.setattr(bot_module.time, "time", lambda: fake_now)

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
        dismiss_cooldown_seconds=600,
    )
    # User dismissed a suggestion 60 s ago — well inside the 600 s window.
    db.get_last_dismiss_time = AsyncMock(return_value=fake_now - 60)
    # Hash dedup must allow the post so any suppression we observe came
    # from the cooldown gate.
    db.get_suggestion_hash_exists = AsyncMock(return_value=False)

    suggestion = {
        "suggestion_type": "task",
        "content": "Refactor the particle pool allocator",
        "task_title": "Refactor allocator",
        # Above both base + in-flight thresholds — without the cooldown
        # gate this would post.
        "confidence": 0.95,
        "intent_confidence": 1.0,
        "novelty": 1.0,
        "actionability": 0.95,
    }

    with caplog.at_level(logging.INFO, logger="src.discord.bot"):
        await AgentQueueBot._post_observation_suggestion(
            stub,
            channel_id=12345,
            project_id="my-game",
            suggestion=suggestion,
        )

    # Don't crash on the unused import — it documents that we considered
    # using stdlib ``time`` directly but chose monkeypatching ``bot.time``
    # so the production code path under test is the one we exercise.
    _ = time_module

    # No Discord post
    assert channel.send.await_count == 0, (
        "recent-dismissal cooldown must not post to Discord"
    )
    # No new pending DB row
    assert db.create_chat_analyzer_suggestion.await_count == 0, (
        "cooldown suppression must not create a pending suggestion row"
    )

    # The lookup must scope to (project_id, channel_id) so a dismissal
    # in another channel does not silence this one.
    assert db.get_last_dismiss_time.await_count == 1
    lookup_call = db.get_last_dismiss_time.await_args
    assert lookup_call.kwargs.get("project_id") == "my-game"
    assert lookup_call.kwargs.get("channel_id") == 12345

    # Structured log
    matched = [
        rec
        for rec in caplog.records
        if getattr(rec, "gate", None) == "dismiss_cooldown"
    ]
    assert matched, (
        "expected an INFO log record carrying extra={'gate': 'dismiss_cooldown'}"
    )

    # Phase-8 footprint with the right suppressed_by tag
    assert db.create_suppressed_chat_analyzer_suggestion.await_count == 1
    footprint_kwargs = (
        db.create_suppressed_chat_analyzer_suggestion.await_args.kwargs
    )
    assert footprint_kwargs.get("suppressed_by") == "dismiss_cooldown"
    assert footprint_kwargs.get("project_id") == "my-game"
    assert footprint_kwargs.get("channel_id") == 12345


@pytest.mark.asyncio
async def test_dismissal_outside_cooldown_does_not_silence(monkeypatch):
    """A dismissal older than the cooldown window must NOT silence.

    Inverse of the suppression test: ``resolved_at = now - 700`` with
    ``dismiss_cooldown_seconds=600`` falls outside the window, so the
    suggestion posts normally.
    """
    from src.discord import bot as bot_module
    from src.discord.bot import AgentQueueBot

    fake_now = 1_700_000_000.0
    monkeypatch.setattr(bot_module.time, "time", lambda: fake_now)

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
        dismiss_cooldown_seconds=600,
    )
    # 700 s in the past — outside the 600 s cooldown window.
    db.get_last_dismiss_time = AsyncMock(return_value=fake_now - 700)
    db.get_suggestion_hash_exists = AsyncMock(return_value=False)

    suggestion = {
        "suggestion_type": "task",
        "content": "Polish the menu animations",
        "task_title": "Polish menu animations",
        "confidence": 0.95,
        "intent_confidence": 1.0,
        "novelty": 1.0,
        "actionability": 0.95,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="my-game",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1, (
        "dismissal outside the cooldown window must not silence the channel"
    )
    assert db.create_chat_analyzer_suggestion.await_count == 1, (
        "post-cooldown suggestion must be persisted as a pending row"
    )
    # No suppression footprint should have been written for the cooldown gate.
    cooldown_footprints = [
        call
        for call in db.create_suppressed_chat_analyzer_suggestion.await_args_list
        if call.kwargs.get("suppressed_by") == "dismiss_cooldown"
    ]
    assert not cooldown_footprints, (
        "post-cooldown suggestion must not record a cooldown suppression footprint"
    )


@pytest.mark.asyncio
async def test_no_prior_dismissal_does_not_silence():
    """When no prior dismissal exists the cooldown gate must not fire.

    Regression: ``get_last_dismiss_time`` returns ``None`` for a clean
    channel; the gate must treat that as "no cooldown active" and let
    the suggestion through.
    """
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
        dismiss_cooldown_seconds=600,
    )
    # No prior dismissal at all.
    db.get_last_dismiss_time = AsyncMock(return_value=None)

    suggestion = {
        "suggestion_type": "task",
        "content": "Add a benchmark for the renderer",
        "task_title": "Benchmark renderer",
        "confidence": 0.95,
        "intent_confidence": 1.0,
        "novelty": 1.0,
        "actionability": 0.95,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="my-game",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1
    assert db.create_chat_analyzer_suggestion.await_count == 1


@pytest.mark.asyncio
async def test_dismiss_cooldown_lookup_failure_falls_back_to_post():
    """A DB error during the cooldown lookup must not crash the pipeline.

    Mirrors the defensive try/except patterns in Phase 2 (dedup) and
    Phase 5 (in-flight). Observability never blocks delivery — when we
    cannot read the cooldown state we degrade to "no cooldown active"
    and let the suggestion post.
    """
    from src.discord.bot import AgentQueueBot

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
        dismiss_cooldown_seconds=600,
    )
    db.get_last_dismiss_time = AsyncMock(side_effect=RuntimeError("DB down"))

    suggestion = {
        "suggestion_type": "task",
        "content": "Polish the menu animations",
        "task_title": "Polish menu animations",
        "confidence": 0.95,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="my-game",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1
    assert db.create_chat_analyzer_suggestion.await_count == 1


@pytest.mark.asyncio
async def test_dismiss_cooldown_disabled_when_seconds_zero(monkeypatch):
    """``dismiss_cooldown_seconds=0`` disables the gate entirely.

    Even with a fresh dismissal one second ago, the gate must not
    suppress when the configured window is zero.  Operators use this
    to opt out of the cooldown without having to disable other gates.
    """
    from src.discord import bot as bot_module
    from src.discord.bot import AgentQueueBot

    fake_now = 1_700_000_000.0
    monkeypatch.setattr(bot_module.time, "time", lambda: fake_now)

    stub, channel, db = _make_bot_stub(
        min_confidence=0.6,
        in_flight_min_confidence=0.85,
        dismiss_cooldown_seconds=0,
    )
    # Even a 1-second-old dismissal must not block when window=0.
    db.get_last_dismiss_time = AsyncMock(return_value=fake_now - 1)

    suggestion = {
        "suggestion_type": "task",
        "content": "anything",
        "task_title": "anything",
        "confidence": 0.95,
    }

    await AgentQueueBot._post_observation_suggestion(
        stub,
        channel_id=12345,
        project_id="my-game",
        suggestion=suggestion,
    )

    assert channel.send.await_count == 1
    assert db.create_chat_analyzer_suggestion.await_count == 1
