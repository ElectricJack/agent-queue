"""Tests for RuntimeRegistry."""

from __future__ import annotations

import pytest

from src.runtimes import RuntimeRegistry
from src.runtimes.base import Capability, MessageCallback, Runtime
from src.models import AgentOutput, AgentResult, TaskContext


class _FakeRuntime(Runtime):
    name = "fake"
    capabilities = frozenset({Capability.STREAMING_JSON})

    def __init__(self, profile=None, llm_logger=None):
        self.profile = profile
        self.llm_logger = llm_logger

    async def start(self, task: TaskContext) -> None:
        return

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        return AgentOutput(result=AgentResult.COMPLETED)

    async def stop(self) -> None:
        return

    async def is_alive(self) -> bool:
        return False


class TestRuntimeRegistry:
    def test_empty_registry_unknown_returns_none(self):
        reg = RuntimeRegistry(runtimes={})
        assert reg.get("anything") is None

    def test_register_and_get(self):
        reg = RuntimeRegistry(runtimes={"fake": _FakeRuntime})
        assert reg.get("fake") is _FakeRuntime

    def test_names_returns_registered_keys(self):
        reg = RuntimeRegistry(runtimes={"fake": _FakeRuntime, "other": _FakeRuntime})
        assert sorted(reg.names()) == ["fake", "other"]

    def test_create_returns_instance_with_profile_and_logger(self):
        reg = RuntimeRegistry(runtimes={"fake": _FakeRuntime})
        inst = reg.create("fake", profile="P", llm_logger="L")
        assert isinstance(inst, _FakeRuntime)
        assert inst.profile == "P"
        assert inst.llm_logger == "L"

    def test_create_unknown_raises_value_error(self):
        reg = RuntimeRegistry(runtimes={"fake": _FakeRuntime})
        with pytest.raises(ValueError, match="Unknown runtime"):
            reg.create("nope", profile=None)


def test_default_registry_is_empty():
    """No in-tree runtimes: every agent runs as a tmux session."""
    from src.runtimes import default_registry

    assert default_registry().names() == []
