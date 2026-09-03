"""Google Gemini chat provider using the ``google-genai`` SDK.

Supports both the Gemini API (API key auth) and Vertex AI.  Format
conversion between the internal Anthropic-style types and Gemini's
native types is handled by the shared ``gemini_adapter`` module.
"""

from __future__ import annotations

import os

from src.llm.providers.adapters import gemini_adapter
from src.llm.providers.base import LLMProvider
from src.llm.types import ChatResponse


class GoogleProvider(LLMProvider):
    """Chat provider using Google's Gemini models via google-genai SDK."""

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str = "",
        thinking_budget: int = 8192,
    ):
        from google import genai

        # Vertex AI mode: SDK auto-detects ADC + GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION
        if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1", "yes"):
            self._client = genai.Client(vertexai=True)
        else:
            resolved_key = (
                api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
            )
            self._client = genai.Client(api_key=resolved_key)
        self._model = str(model) if model else "gemini-2.5-flash"
        self._thinking_budget = thinking_budget

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def reports_usage(self) -> bool:
        # Gemini exposes usage metadata for supported models; a missing value
        # on an individual response remains unreported and is checked by the
        # executor after the call.
        return True

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        from google.genai import types

        thinking = self._thinking_budget
        config = types.GenerateContentConfig(
            system_instruction=system,
            # Gemini counts thinking tokens against max_output_tokens, so add
            # the thinking budget on top of the caller's requested response
            # budget to avoid starving the visible response.
            max_output_tokens=max_tokens + thinking,
            thinking_config=types.ThinkingConfig(thinking_budget=thinking),
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
