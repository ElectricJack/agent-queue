"""The direct LLM path.  See docs/superpowers/specs/2026-08-30-llm-direct-path-design.md."""

from src.llm.client import LLMClient, LLMResponse, LLMRunResult, LLMToolTurn, ToolCall
from src.llm.spec import LLMCallSpec, ResolvedCall, resolve_call, spec_from_llm_config

__all__ = [
    "LLMCallSpec",
    "LLMClient",
    "LLMResponse",
    "LLMRunResult",
    "LLMToolTurn",
    "ResolvedCall",
    "ToolCall",
    "resolve_call",
    "spec_from_llm_config",
]
