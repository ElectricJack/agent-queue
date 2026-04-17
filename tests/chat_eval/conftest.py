"""Shared fixtures for the supervisor evaluation framework.

Updated: ChatAgent → Supervisor (post-supervisor refactor).
"""

from __future__ import annotations

import pytest

from src.supervisor import Supervisor
from src.config import AppConfig
from src.models import AgentResult, AgentOutput
from src.adapters.base import AgentAdapter
from src.orchestrator import Orchestrator

from tests.chat_eval.providers import ScriptedProvider
from tests.chat_eval.recording_handler import RecordingCommandHandler


class MockAdapter(AgentAdapter):
    async def start(self, task):
        pass

    async def wait(self, on_message=None):
        return AgentOutput(result=AgentResult.COMPLETED, summary="Done", tokens_used=1000)

    async def stop(self):
        pass

    async def is_alive(self):
        return True


class MockAdapterFactory:
    def __init__(self):
        self.last_profile = None
        self.create_calls = []

    def create(self, agent_type: str, profile=None) -> AgentAdapter:
        self.last_profile = profile
        self.create_calls.append({"agent_type": agent_type, "profile": profile})
        return MockAdapter()


@pytest.fixture
async def eval_config(tmp_path):
    """AppConfig with tmp_path database and workspace dir."""
    return AppConfig(
        database_path=str(tmp_path / "eval_test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def eval_orchestrator(eval_config):
    """Initialized Orchestrator with mock adapter factory."""
    orch = Orchestrator(eval_config, adapter_factory=MockAdapterFactory())
    await orch.initialize()
    yield orch
    await orch.shutdown()


@pytest.fixture
def scripted_provider():
    """Fresh ScriptedProvider instance."""
    return ScriptedProvider()


@pytest.fixture
async def eval_agent(eval_orchestrator, eval_config, scripted_provider):
    """Supervisor with ScriptedProvider + RecordingCommandHandler.

    Returns (agent, recorder, provider) tuple.
    The agent's handler is replaced with a RecordingCommandHandler so tests
    can inspect which tools were called.
    """
    agent = Supervisor(eval_orchestrator, eval_config)
    # Replace the provider directly (bypass initialize which needs real API)
    agent._provider = scripted_provider

    # Replace handler with recording version
    recorder = RecordingCommandHandler(eval_orchestrator, eval_config)
    # Preserve active project if set
    recorder._active_project_id = agent.handler._active_project_id
    agent.handler = recorder

    # Disable reflection for deterministic tests — reflection adds extra
    # LLM calls that would break scripted-provider call-count assertions.
    agent.reflection._config.level = "off"

    return agent, recorder, scripted_provider
