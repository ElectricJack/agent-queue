"""Google Gemini chat provider using the ``google-genai`` SDK.

Supports both the Gemini API (API key auth) and Vertex AI.  Format
conversion between the internal Anthropic-style types and Gemini's
native types is handled by the shared ``gemini_adapter`` module.
"""

from __future__ import annotations

import os

from .adapters import gemini_adapter
from .base import ChatProvider
from .types import ChatResponse


class GeminiChatProvider(ChatProvider):
    """Chat provider using Google's Gemini models via google-genai SDK."""

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str = "",
    ):
        from google import genai

        resolved_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        self._client = genai.Client(api_key=resolved_key)
        self._model = str(model) if model else "gemini-2.5-flash"

    @property
    def model_name(self) -> str:
        return self._model

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=8192),
        )
        if tools:
            config.tools = gemini_adapter.convert_tools(tools)

        gemini_contents = gemini_adapter.convert_messages(messages)

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=gemini_contents,
            config=config,
        )

        return gemini_adapter.parse_response(response)
