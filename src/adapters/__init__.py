"""Adapter layer: abstracts AI coding agents behind a common interface.

The orchestrator never talks to a specific agent implementation directly.
Instead, it requests an adapter from AdapterFactory by type string (e.g.
"claude") and interacts through the AgentAdapter ABC defined in base.py.

AdapterFactory implements the factory pattern so adding a new agent backend
is a one-line change here plus a new module.  Currently only "claude"
(Claude Code via the Agent SDK) is implemented, but the design anticipates
future adapters for tools like Codex, Cursor, Aider, etc.
"""

from __future__ import annotations

from src.adapters.base import AgentAdapter
from src.adapters.claude import ClaudeAdapter, ClaudeAdapterConfig
from src.models import AgentProfile


class AdapterFactory:
    """Creates agent adapters by type.

    Holds per-type configuration (e.g. ClaudeAdapterConfig) and instantiates
    a fresh adapter for each task execution.  The orchestrator calls
    ``create("claude", profile=...)`` once per task assignment.

    When a profile is provided, the factory merges profile overrides into the
    base config (model, permission_mode, allowed_tools).  Fields left empty
    in the profile fall through to the base config defaults.
    """

    def __init__(self, claude_config: ClaudeAdapterConfig | None = None, llm_logger=None):
        self._claude_config = claude_config or ClaudeAdapterConfig()
        self._llm_logger = llm_logger

    def create(self, agent_type: str, profile: AgentProfile | None = None) -> AgentAdapter:
        if agent_type == "claude":
            config = self._config_for_profile(profile)
            return ClaudeAdapter(config, llm_logger=self._llm_logger)
        raise ValueError(f"Unknown agent type: {agent_type}")

    def _config_for_profile(
        self,
        profile: AgentProfile | None,
    ) -> ClaudeAdapterConfig:
        """Merge profile overrides into the base ClaudeAdapterConfig."""
        if profile is None:
            return self._claude_config
        return ClaudeAdapterConfig(
            model=profile.model or self._claude_config.model,
            permission_mode=(profile.permission_mode or self._claude_config.permission_mode),
            allowed_tools=(profile.allowed_tools or self._claude_config.allowed_tools),
            max_turns=self._claude_config.max_turns,
        )
