"""Anthropic chat provider supporting four authentication methods.

Credentials are tried in priority order (first match wins):
1. **Vertex AI** -- ``GOOGLE_CLOUD_PROJECT`` / ``ANTHROPIC_VERTEX_PROJECT_ID``
2. **Bedrock** -- ``AWS_REGION`` / ``AWS_DEFAULT_REGION``
3. **API key** -- ``ANTHROPIC_API_KEY``
4. **Claude Code OAuth** -- reads ``~/.claude/.credentials.json``

This fall-through design means the same deployment works unchanged across
Google Cloud, AWS, direct API, and local development with Claude Code login.

Responses from the Anthropic SDK are converted into the normalized
``ChatResponse`` / ``TextBlock`` / ``ToolUseBlock`` types so the rest of
the codebase stays provider-agnostic.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .base import ChatProvider
from .types import ChatResponse, TextBlock, ToolUseBlock


def _load_claude_oauth_token() -> str | None:
    """Load OAuth access token from Claude Code's credential file."""
    for name in (".credentials.json", "credentials.json"):
        cred_path = Path.home() / ".claude" / name
        if not cred_path.exists():
            continue
        try:
            creds = json.loads(cred_path.read_text())
            oauth = creds.get("claudeAiOauth", {})
            token = oauth.get("accessToken")
            if token:
                expires = oauth.get("expiresAt", 0)
                if expires and expires < time.time() * 1000:
                    print("Warning: Claude OAuth token may be expired — trying anyway")
                return token
        except Exception as e:
            print(f"Warning: could not read Claude credentials from {cred_path}: {e}")
    return None


class AnthropicChatProvider(ChatProvider):
    """Chat provider using the Anthropic SDK (direct API, Vertex AI, Bedrock, or OAuth)."""

    def __init__(self, model: str = ""):
        try:
            import anthropic
        except ModuleNotFoundError:
            self._client = None
            self._model = model
            return

        self._client = None
        self._model = model

        # Try Vertex AI first
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
            "ANTHROPIC_VERTEX_PROJECT_ID"
        )
        if project_id:
            from anthropic import AsyncAnthropicVertex

            region = (
                os.environ.get("GOOGLE_CLOUD_LOCATION")
                or os.environ.get("CLOUD_ML_REGION")
                or "us-east5"
            )
            self._client = AsyncAnthropicVertex(project_id=project_id, region=region)
            if not self._model:
                self._model = "claude-sonnet-4@20250514"
            return

        # Try Bedrock
        if os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"):
            from anthropic import AsyncAnthropicBedrock

            self._client = AsyncAnthropicBedrock()
            if not self._model:
                self._model = "claude-sonnet-4-20250514"
            return

        # Try explicit API key
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
            if not self._model:
                self._model = "claude-sonnet-4-20250514"
            return

        # Try Claude Code OAuth credentials (~/.claude/.credentials.json)
        oauth_token = _load_claude_oauth_token()
        if oauth_token:
            self._client = anthropic.AsyncAnthropic(auth_token=oauth_token)
            if not self._model:
                self._model = "claude-sonnet-4-20250514"
            return

    @property
    def is_configured(self) -> bool:
        return self._client is not None

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
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        resp = await self._client.messages.create(**kwargs)

        content: list[TextBlock | ToolUseBlock] = []
        for block in resp.content:
            if block.type == "text":
                content.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                content.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))

        return ChatResponse(content=content)
