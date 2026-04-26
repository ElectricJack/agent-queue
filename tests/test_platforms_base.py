"""Tests for the Platform ABC, Capability enum, and ABC-conforming MockPlatform."""

from __future__ import annotations

import pytest

from src.platforms.base import Capability, MessageCallback, Platform
from src.models import AgentOutput, AgentResult, TaskContext


class TestCapabilityEnum:
    def test_is_str_enum(self):
        assert isinstance(Capability.MCP, str)
        assert Capability.MCP == "mcp"

    def test_required_members_exist(self):
        for name in (
            "MCP",
            "PLAN_MODE",
            "RESUME",
            "STREAMING_JSON",
            "HOOKS",
            "SKILLS",
            "MEMORY_MD",
            "THINKING",
            "PERMISSION_CALLBACKS",
        ):
            assert hasattr(Capability, name), f"Capability.{name} missing"

    def test_values_are_lowercase_snake(self):
        for member in Capability:
            assert member.value == member.value.lower()
            assert " " not in member.value


class TestPlatformABC:
    def test_cannot_instantiate_bare_abc(self):
        with pytest.raises(TypeError):
            Platform()  # type: ignore[abstract]

    def test_subclass_missing_methods_cannot_instantiate(self):
        class Incomplete(Platform):
            name = "incomplete"
            capabilities = frozenset()

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_with_all_methods_instantiates(self):
        class Complete(Platform):
            name = "complete"
            capabilities = frozenset({Capability.STREAMING_JSON})

            async def start(self, task: TaskContext) -> None:  # noqa: ARG002
                return

            async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:  # noqa: ARG002
                return AgentOutput(result=AgentResult.COMPLETED)

            async def stop(self) -> None:
                return

            async def is_alive(self) -> bool:
                return False

        inst = Complete()
        assert inst.name == "complete"
        assert Capability.STREAMING_JSON in inst.capabilities


class MockPlatform(Platform):
    """Minimal Platform impl for use across the test suite (replaces MockAdapter)."""

    name = "mock"
    capabilities = frozenset()

    def __init__(self, result: AgentResult = AgentResult.COMPLETED, tokens: int = 1000):
        self._result = result
        self._tokens = tokens
        self.started = False
        self.stopped = False

    async def start(self, task: TaskContext) -> None:  # noqa: ARG002
        self.started = True

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:  # noqa: ARG002
        return AgentOutput(
            result=self._result,
            summary="Did the thing",
            tokens_used=self._tokens,
        )

    async def stop(self) -> None:
        self.stopped = True

    async def is_alive(self) -> bool:
        return self.started and not self.stopped


class TestMockPlatform:
    async def test_lifecycle(self):
        platform = MockPlatform()
        ctx = TaskContext(description="test task")
        await platform.start(ctx)
        assert platform.started
        assert await platform.is_alive()
        output = await platform.wait()
        assert output.result == AgentResult.COMPLETED
        assert output.tokens_used == 1000
        await platform.stop()
        assert platform.stopped

    async def test_failed_result(self):
        platform = MockPlatform(result=AgentResult.FAILED)
        ctx = TaskContext(description="test")
        await platform.start(ctx)
        output = await platform.wait()
        assert output.result == AgentResult.FAILED

    async def test_paused_result(self):
        platform = MockPlatform(result=AgentResult.PAUSED_RATE_LIMIT)
        ctx = TaskContext(description="test")
        await platform.start(ctx)
        output = await platform.wait()
        assert output.result == AgentResult.PAUSED_RATE_LIMIT
