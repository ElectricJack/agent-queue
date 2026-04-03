"""Tests for ChatObserver — Stage 1 keyword filter and batching."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest


def _make_observer(keywords=None, enabled=True):
    from src.config import ObservationConfig
    from src.chat_observer import ChatObserver

    config = ObservationConfig(
        enabled=enabled,
        batch_window_seconds=60,
        max_buffer_size=20,
        stage1_keywords=keywords or [],
    )
    return ChatObserver(
        config, project_profiles={"my-game": {"particle", "renderer", "unity", "shader"}}
    )


def _make_message(content, project_id="my-game", author="alice", is_bot=False):
    return {
        "channel_id": 12345,
        "project_id": project_id,
        "author": author,
        "content": content,
        "timestamp": 1000.0,
        "is_bot": is_bot,
    }


def test_stage1_passes_project_term():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("the particle system is broken")) is True


def test_stage1_passes_action_word():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("we need to deploy the hotfix today")) is True


def test_stage1_passes_custom_keyword():
    obs = _make_observer(keywords=["kubernetes"])
    assert obs.passes_stage1(_make_message("kubernetes pod is crashlooping")) is True


def test_stage1_rejects_trivial():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("ok")) is False


def test_stage1_rejects_short_generic():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("sounds good to me")) is False


def test_stage1_passes_long_message():
    obs = _make_observer()
    assert (
        obs.passes_stage1(_make_message("I've been thinking about the architecture and " * 10))
        is True
    )


def test_stage1_rejects_bot_messages():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("the particle system looks good", is_bot=True)) is False


def test_stage1_disabled():
    obs = _make_observer(enabled=False)
    assert obs.passes_stage1(_make_message("the particle system is broken")) is False


def test_stage1_unknown_project():
    obs = _make_observer()
    assert (
        obs.passes_stage1(_make_message("something about particles", project_id="unknown-proj"))
        is False
    )


def test_buffer_message():
    obs = _make_observer()
    obs.buffer_message(_make_message("the particle system crashed"))
    assert obs.get_buffer_size(12345) == 1


def test_flush_buffer():
    obs = _make_observer()
    obs.buffer_message(_make_message("msg 1"))
    obs.buffer_message(_make_message("msg 2"))
    batch = obs.flush_buffer(12345)
    assert len(batch) == 2
    assert obs.get_buffer_size(12345) == 0


def test_on_message_buffers_passing_message():
    obs = _make_observer()
    obs.on_message(_make_message("the particle system crashed again"))
    assert obs.get_buffer_size(12345) == 1


def test_on_message_drops_failing_message():
    obs = _make_observer()
    obs.on_message(_make_message("ok"))
    assert obs.get_buffer_size(12345) == 0


def test_on_message_disabled_drops_all():
    obs = _make_observer(enabled=False)
    obs.on_message(_make_message("the particle system is broken"))
    assert obs.get_buffer_size(12345) == 0


def test_batch_ready_by_count():
    from src.config import ObservationConfig
    from src.chat_observer import ChatObserver

    config = ObservationConfig(max_buffer_size=3)
    obs = ChatObserver(config, project_profiles={"my-game": {"particle"}})
    for i in range(3):
        obs.on_message(_make_message(f"particle issue {i}"))
    assert obs.is_batch_ready(12345) is True


def test_batch_not_ready_under_count():
    obs = _make_observer()
    obs.on_message(_make_message("the particle system crashed"))
    assert obs.is_batch_ready(12345) is False


def test_start_creates_timer_task():
    """start() creates the background batch timer."""
    obs = _make_observer()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(obs.start())
        assert obs._running is True
    finally:
        loop.run_until_complete(obs.stop())
        loop.close()


def test_stop_cancels_timer():
    obs = _make_observer()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(obs.start())
        loop.run_until_complete(obs.stop())
        assert obs._running is False
    finally:
        loop.close()


def test_process_batch_calls_callback():
    """When batch is flushed, callback receives messages and project_id."""
    obs = _make_observer()
    callback = AsyncMock(return_value={"action": "ignore"})
    obs._on_batch_ready = callback
    obs.on_message(_make_message("particle system crashed"))
    loop = asyncio.new_event_loop()
    try:
        # Manually flush and invoke process
        loop.run_until_complete(obs._process_batch(12345))
        # Callback should have been called with channel_id and batch
        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == 12345  # channel_id
        assert len(call_args[0][1]) == 1  # batch with 1 message
    finally:
        loop.close()


def test_full_flow_filter_buffer_flush():
    """End-to-end: messages are filtered, buffered, and flushed."""
    obs = _make_observer()
    obs.on_message(_make_message("ok"))
    obs.on_message(_make_message("lol"))
    obs.on_message(_make_message("the particle system is broken"))
    obs.on_message(_make_message("yes"))
    obs.on_message(_make_message("we need to deploy the fix"))
    assert obs.get_buffer_size(12345) == 2
    batch = obs.flush_buffer(12345)
    assert len(batch) == 2
    assert "particle" in batch[0]["content"]
    assert "deploy" in batch[1]["content"]


def test_observer_config_wired_to_supervisor_config():
    """ObservationConfig is accessible from SupervisorConfig."""
    from src.config import AppConfig

    app = AppConfig()
    assert hasattr(app.supervisor, "observation")
    assert app.supervisor.observation.enabled is True
    assert app.supervisor.observation.batch_window_seconds == 60
