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
from src.platforms.claude_sdk import ClaudeSDKPlatform
from src.models import AgentProfile


class AdapterFactory:
    """Creates agent adapters by type.

    Instantiates a fresh adapter for each task execution.  The orchestrator
    calls ``create("claude", profile=...)`` once per task assignment.
    Profile-to-config merging is handled by the platform (ClaudeSDKPlatform).
    """

    def __init__(self, llm_logger=None):
        self._llm_logger = llm_logger

    def create(self, agent_type: str, profile: AgentProfile | None = None) -> AgentAdapter:
        if agent_type == "claude":
            return ClaudeSDKPlatform(profile=profile, llm_logger=self._llm_logger)
        raise ValueError(f"Unknown agent type: {agent_type}")
