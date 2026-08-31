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


class _ConfigAwareRuntime(_FakeRuntime):
    def __init__(self, profile=None, llm_logger=None, config=None):
        super().__init__(profile, llm_logger)
        self.config = config


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

    def test_singleton_is_returned_and_listed_without_construction(self):
        singleton = _FakeRuntime()
        reg = RuntimeRegistry(runtimes={}, singletons={"shared": singleton})

        assert reg.names() == ["shared"]
        assert reg.create("shared", profile="ignored", llm_logger="ignored") is singleton

    def test_create_passes_registry_config_only_to_runtime_that_declares_it(self):
        config = object()
        reg = RuntimeRegistry(runtimes={"aware": _ConfigAwareRuntime}, config=config)

        runtime = reg.create("aware", profile="profile", llm_logger="logger")

        assert runtime.profile == "profile"
        assert runtime.llm_logger == "logger"
        assert runtime.config is config


def test_default_registry_is_empty():
    """No in-tree runtimes: every agent runs as a tmux session."""
    from src.runtimes import default_registry

    config = object()
    registry = default_registry(config=config)
    assert registry.names() == []
    assert registry._config is config
