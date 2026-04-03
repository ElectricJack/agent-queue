"""Tests for ReflectionEngine — action-reflect cycle."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_engine(level="full", max_depth=3, per_cycle_cap=10000, hourly_cap=100000):
    from src.config import ReflectionConfig
    from src.reflection import ReflectionEngine

    config = ReflectionConfig(
        level=level,
        max_depth=max_depth,
        per_cycle_token_cap=per_cycle_cap,
        hourly_token_circuit_breaker=hourly_cap,
    )
    return ReflectionEngine(config)


def test_determine_depth_task_completion():
    engine = _make_engine()
    assert engine.determine_depth("task.completed", {}) == "deep"


def test_determine_depth_user_request():
    engine = _make_engine()
    assert engine.determine_depth("user.request", {}) == "standard"


def test_determine_depth_passive_observation():
    engine = _make_engine()
    assert engine.determine_depth("passive.observation", {}) == "light"


def test_determine_depth_hook_failure():
    engine = _make_engine()
    assert engine.determine_depth("hook.failed", {}) == "deep"


def test_determine_depth_hook_success():
    engine = _make_engine()
    assert engine.determine_depth("hook.completed", {}) == "standard"


def test_determine_depth_periodic():
    engine = _make_engine()
    assert engine.determine_depth("periodic.sweep", {}) == "light"


def test_level_off_always_returns_none():
    engine = _make_engine(level="off")
    assert engine.determine_depth("task.completed", {}) is None


def test_level_minimal_downgrades():
    engine = _make_engine(level="minimal")
    assert engine.determine_depth("task.completed", {}) == "light"


def test_should_reflect_off():
    engine = _make_engine(level="off")
    assert engine.should_reflect("task.completed") is False


def test_should_reflect_full():
    engine = _make_engine(level="full")
    assert engine.should_reflect("task.completed") is True


def test_build_reflection_prompt_deep():
    engine = _make_engine()
    prompt = engine.build_reflection_prompt(
        depth="deep",
        trigger="task.completed",
        action_summary="Created task fix-login",
        action_results=[{"tool": "create_task", "result": {"id": "t-123"}}],
    )
    assert "Did I do what was asked" in prompt
    assert "verify" in prompt.lower() or "succeed" in prompt.lower()
    assert "rules" in prompt.lower()
    assert "memory" in prompt.lower()
    assert "follow-up" in prompt.lower()


def test_build_reflection_prompt_light():
    engine = _make_engine()
    prompt = engine.build_reflection_prompt(
        depth="light",
        trigger="passive.observation",
        action_summary="Observed chat about API design",
        action_results=[],
    )
    assert "memory" in prompt.lower()
    assert len(prompt) < 500


def test_max_depth_tracking():
    engine = _make_engine(max_depth=2)
    assert engine.can_reflect_deeper(current_depth=0) is True
    assert engine.can_reflect_deeper(current_depth=1) is True
    assert engine.can_reflect_deeper(current_depth=2) is False


def test_token_tracking():
    engine = _make_engine(per_cycle_cap=100)
    assert engine.can_continue_cycle(tokens_used=50) is True
    assert engine.can_continue_cycle(tokens_used=100) is False
    assert engine.can_continue_cycle(tokens_used=150) is False


def test_circuit_breaker_not_tripped():
    engine = _make_engine(hourly_cap=1000)
    engine.record_tokens(500)
    assert engine.is_circuit_breaker_tripped() is False


def test_circuit_breaker_tripped():
    engine = _make_engine(hourly_cap=1000)
    engine.record_tokens(1001)
    assert engine.is_circuit_breaker_tripped() is True


def test_circuit_breaker_resets_after_hour():
    engine = _make_engine(hourly_cap=1000)
    engine._token_ledger.append((time.time() - 3700, 999))
    assert engine.is_circuit_breaker_tripped() is False


def test_record_tokens_accumulates():
    engine = _make_engine()
    engine.record_tokens(100)
    engine.record_tokens(200)
    assert engine.hourly_tokens_used() == 300
