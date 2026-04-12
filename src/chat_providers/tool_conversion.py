"""Backward-compatible re-export from the adapters package.

The conversion logic now lives in ``adapters.openai_adapter``.
This module re-exports ``anthropic_tools_to_openai`` so existing
imports continue to work.
"""

from .adapters.openai_adapter import convert_tools as anthropic_tools_to_openai

__all__ = ["anthropic_tools_to_openai"]
