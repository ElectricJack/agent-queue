"""Tests for the MemoryExtractor.

Covers:
- The ``_looks_like_garbage`` content validator.
- Routing of insights/knowledge/guidance items through the injected
  ``save_callback`` (the plugin's dedup-aware ``_do_memory_save``) rather
  than the direct ``save_document`` path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.plugins.internal.memory.extractor import (
    MemoryExtractor,
    _looks_like_garbage,
)


class TestLooksLikeGarbage:
    """The post-extraction content filter."""

    def test_empty_string_rejected(self):
        assert _looks_like_garbage("") == "empty"
        assert _looks_like_garbage("   \n\t  ") == "empty"

    def test_greeting_opener_rejected(self):
        assert (
            _looks_like_garbage("Hi Jack, here's what I set up today...")
            == "greeting-start"
        )
        assert (
            _looks_like_garbage("Hello Jessica, I've created the comparison.")
            == "greeting-start"
        )
        assert _looks_like_garbage("Hey team, quick update on the work.") == "greeting-start"
        assert (
            _looks_like_garbage("Dear Jessica, please find the document attached.")
            == "greeting-start"
        )

    def test_greeting_case_insensitive(self):
        assert (
            _looks_like_garbage("HI JACK, the build is green.") == "greeting-start"
        )

    def test_raw_json_object_rejected(self):
        payload = (
            '{"sender": "agent@mossandspade.com", '
            '"subject": "Re: About", '
            '"classification_reasons": ["spf_failed", "dkim_failed"]}'
        )
        assert _looks_like_garbage(payload) == "raw-json"

    def test_raw_json_array_rejected(self):
        assert _looks_like_garbage('["one", "two", "three", "four"]') == "raw-json"

    def test_brace_lookalike_not_json_passes(self):
        # Reads like prose, happens to contain braces — not parseable as JSON.
        text = (
            "Use {{Sender First Name}}: {{Concise Subject}} as the task title "
            "format, always under 80 characters."
        )
        assert _looks_like_garbage(text) is None

    def test_html_entity_noise_rejected(self):
        text = "I&#39;ve updated the doc and it&#39;s now &quot;green&quot; in CI."
        assert _looks_like_garbage(text) == "html-entity-noise"

    def test_url_with_no_content_rejected(self):
        text = "https://docs.google.com/spreadsheets/d/1IF3YxiZjTPouVEyg8jQX5wwBU"
        assert _looks_like_garbage(text) == "url-with-no-content"

    def test_url_with_real_context_passes(self):
        text = (
            "The oncall latency dashboard lives at "
            "https://grafana.internal/d/api-latency and the team watches it "
            "during deploys to catch regressions quickly."
        )
        assert _looks_like_garbage(text) is None

    def test_too_short_rejected(self):
        text = "Pytest is used."
        reason = _looks_like_garbage(text)
        assert reason is not None and reason.startswith("too-short")

    def test_too_long_rejected(self):
        text = "word " * 500
        reason = _looks_like_garbage(text)
        assert reason is not None and reason.startswith("too-long")

    def test_normal_insight_passes(self):
        text = (
            "Task titles created from allowlisted emails must follow the "
            "format 'SenderFirstName: ConciseSubject' and stay under 80 "
            "characters. The sender's first name is extracted from event.from."
        )
        assert _looks_like_garbage(text) is None


class TestSaveItemRoutesThroughCallback:
    """When save_callback is provided, _save_item must call it for non-fact items."""

    @pytest.fixture
    def extractor_with_callback(self):
        """Build a MemoryExtractor with mocked deps and a save_callback."""
        bus = MagicMock()
        db = MagicMock()
        memory_service = MagicMock()
        # Direct save_document should NOT be called when a callback is wired.
        memory_service.save_document = AsyncMock()
        memory_service.kv_set = AsyncMock()

        save_callback = AsyncMock(return_value={"chunk_hash": "abc123"})

        ex = MemoryExtractor(
            bus=bus,
            db=db,
            memory_service=memory_service,
            config={},
            chat_provider_config=MagicMock(),
            save_callback=save_callback,
        )
        return ex, save_callback, memory_service

    @pytest.mark.asyncio
    async def test_insight_routed_through_callback(self, extractor_with_callback):
        ex, save_callback, memory_service = extractor_with_callback
        item = {
            "type": "insight",
            "content": (
                "The test framework is pytest and tests live under the "
                "tests/ directory with pytest-asyncio in auto mode."
            ),
            "topic": "testing",
        }
        await ex._save_item("proj-a", item, "task-1")

        save_callback.assert_awaited_once()
        kwargs = save_callback.await_args.kwargs
        assert kwargs["project_id"] == "proj-a"
        assert kwargs["content"] == item["content"]
        assert "auto-extracted" in kwargs["tags"]
        assert "insight" in kwargs["tags"]
        assert kwargs["topic"] == "testing"
        assert kwargs["source_playbook"] == "memory-extractor"
        assert kwargs["scope"] is None
        # Direct service save should be untouched when callback is active.
        memory_service.save_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_fact_still_goes_to_kv(self, extractor_with_callback):
        ex, save_callback, memory_service = extractor_with_callback
        item = {"type": "fact", "key": "test_framework", "value": "pytest"}
        await ex._save_item("proj-a", item, "task-1")

        memory_service.kv_set.assert_awaited_once()
        save_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_garbage_content_not_saved(self, extractor_with_callback):
        ex, save_callback, _ = extractor_with_callback
        item = {
            "type": "insight",
            "content": "Hi Jessica, here's the link: https://docs.example.com/x",
            "topic": "networking",
        }
        await ex._save_item("proj-a", item, "task-1")
        save_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_guidance_also_saved_to_agent_type_scope(
        self, extractor_with_callback
    ):
        ex, save_callback, _ = extractor_with_callback
        item = {
            "type": "guidance",
            "content": (
                "When creating tasks from allowlisted emails, the title must "
                "follow the format 'SenderFirstName: ConciseSubject' and be "
                "under 80 characters."
            ),
            "topic": "task-creation",
        }
        await ex._save_item("proj-a", item, "task-1", agent_type="supervisor")

        # Called twice: once for project scope, once for agent-type scope.
        assert save_callback.await_count == 2
        scopes = [call.kwargs["scope"] for call in save_callback.await_args_list]
        assert None in scopes
        assert "agenttype_supervisor" in scopes


class TestSaveItemFallbackWithoutCallback:
    """When no callback is wired (degraded/legacy mode), save_document is used."""

    @pytest.mark.asyncio
    async def test_insight_falls_back_to_save_document(self):
        bus = MagicMock()
        db = MagicMock()
        memory_service = MagicMock()
        memory_service.save_document = AsyncMock(return_value={"chunk_hash": "h1"})
        memory_service.kv_set = AsyncMock()

        ex = MemoryExtractor(
            bus=bus,
            db=db,
            memory_service=memory_service,
            config={},
            chat_provider_config=MagicMock(),
            save_callback=None,
        )

        item = {
            "type": "insight",
            "content": (
                "Default branch is main and every PR must run the full "
                "pytest suite before merging to prevent regressions."
            ),
            "topic": "ci",
        }
        await ex._save_item("proj-a", item, "task-1")
        memory_service.save_document.assert_awaited_once()
