"""Tests for PlatformRegistry."""

from __future__ import annotations

import pytest

from src.platforms import PlatformRegistry
from src.platforms.base import Capability, MessageCallback, Platform
from src.models import AgentOutput, AgentResult, TaskContext


class _FakePlatform(Platform):
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


class TestPlatformRegistry:
    def test_empty_registry_unknown_returns_none(self):
        reg = PlatformRegistry(platforms={})
        assert reg.get("anything") is None

    def test_register_and_get(self):
        reg = PlatformRegistry(platforms={"fake": _FakePlatform})
        assert reg.get("fake") is _FakePlatform

    def test_names_returns_registered_keys(self):
        reg = PlatformRegistry(platforms={"fake": _FakePlatform, "other": _FakePlatform})
        assert sorted(reg.names()) == ["fake", "other"]

    def test_create_returns_instance_with_profile_and_logger(self):
        reg = PlatformRegistry(platforms={"fake": _FakePlatform})
        inst = reg.create("fake", profile="P", llm_logger="L")
        assert isinstance(inst, _FakePlatform)
        assert inst.profile == "P"
        assert inst.llm_logger == "L"

    def test_create_unknown_raises_value_error(self):
        reg = PlatformRegistry(platforms={"fake": _FakePlatform})
        with pytest.raises(ValueError, match="Unknown platform"):
            reg.create("nope", profile=None)
