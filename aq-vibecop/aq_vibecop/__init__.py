"""Vibecop static analysis plugin for Agent Queue.

Wraps the vibecop CLI (a deterministic AI code quality linter) to provide
code scanning tools for AI agents. Agents can self-check their code changes
against 22+ detectors for quality, security, correctness, and testing
antipatterns without consuming LLM tokens.
"""

from aq_vibecop.plugin import VibeCopPlugin

__all__ = ["VibeCopPlugin"]
