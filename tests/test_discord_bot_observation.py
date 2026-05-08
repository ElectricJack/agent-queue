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


def _make_bot_stub(*, db_returns_id: int | None = 99):
    """Build a minimal object with the attributes ``_post_observation_suggestion`` touches.

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

    stub = SimpleNamespace(
        agent=agent,
        orchestrator=orchestrator,
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
