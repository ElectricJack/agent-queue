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
):
    """Build a minimal object with the attributes ``_post_observation_suggestion`` touches.

    The default ``min_confidence=0.0`` keeps the legacy Phase 1 / Phase 2
    tests neutral — every suggestion clears the gate.  Phase 4 tests
    pass an explicit threshold to exercise the gate.

    Returns a tuple of ``(stub, channel, db)`` so tests can assert against
    the mocked dependencies directly.
    """
    db = MagicMock()
    db.create_chat_analyzer_suggestion = AsyncMock(return_value=db_returns_id)

    channel = MagicMock()
    channel.send = AsyncMock(return_value=None)

    handler = MagicMock()
    agent = SimpleNamespace(handler=handler)

    orchestrator = SimpleNamespace(db=db)

    chat_analyzer_cfg = SimpleNamespace(min_confidence=min_confidence)
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
