"""Tests for the Summary + Original Pattern — Roadmap 3.4.7.

Validates that the memory system correctly handles the summary/original
separation per spec §9:

  (a) Saving content >200 tokens generates a summary and stores original separately
  (b) memory_search returns the summary (optimized for search)
  (c) memory_get with full=true returns the original full content
  (d) memory_get without full=true returns the summary
  (e) Saving content <=200 tokens stores it as-is (no summary)
  (f) Summary is meaningfully shorter than original (not just truncated)
  (g) Original content is byte-for-byte identical to what was saved
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip entire module on Windows (Milvus Lite not supported)
if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.memory_v2_service import MemoryV2Service, MEMSEARCH_AVAILABLE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embedder():
    """Create a mock embedding provider."""
    embedder = MagicMock()
    embedder.model_name = "test-model"
    embedder.dimension = 384
    embedder.embed = AsyncMock(return_value=[[0.1] * 384])
    return embedder


@pytest.fixture
def mock_store():
    """Create a mock MilvusStore."""
    store = MagicMock()
    store.count.return_value = 10
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False
    store.upsert.return_value = 1
    store.get.return_value = {
        "chunk_hash": "existing_hash",
        "entry_type": "document",
        "content": "Existing insight about authentication",
        "original": "Full original text",
        "source": "",
        "heading": "Existing insight",
        "topic": "auth",
        "tags": '["insight"]',
        "updated_at": 1000,
        "embedding": [0.1] * 384,
    }
    store.search.return_value = []
    return store


@pytest.fixture
def mock_router(mock_store):
    """Create a mock CollectionRouter."""
    router = MagicMock()
    router.get_store.return_value = mock_store
    router.list_collections.return_value = []
    router.search = AsyncMock(return_value=[])
    router.close = MagicMock()
    return router


@pytest.fixture
def tmp_data_dir():
    """Create a temporary directory for vault files."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def service(mock_embedder, mock_router, tmp_data_dir):
    """Create a MemoryV2Service with mocked dependencies and temp vault."""
    svc = MemoryV2Service(
        milvus_uri="/tmp/test.db",
        embedding_provider="openai",
        data_dir=tmp_data_dir,
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


@pytest.fixture
def plugin():
    from src.plugins.internal.memory_v2 import MemoryV2Plugin

    return MemoryV2Plugin()


@pytest.fixture
def wired_plugin(plugin, service):
    """Plugin with a wired-up service and mock context."""
    plugin._service = service
    plugin._log = MagicMock()
    plugin._ctx = MagicMock()
    plugin._ctx.invoke_llm = AsyncMock(return_value="LLM generated summary")
    return plugin


# ---------------------------------------------------------------------------
# Long content for tests — exceeds _SUMMARY_CHAR_THRESHOLD (800 chars)
# ---------------------------------------------------------------------------

LONG_CONTENT = (
    "The authentication system uses OAuth 2.0 with PKCE flow for public clients. "
    "Access tokens expire after 15 minutes and refresh tokens after 7 days. "
    "When a refresh token is used, the old one is immediately revoked and a new "
    "refresh token is issued alongside the new access token. This rotation "
    "strategy mitigates the risk of stolen refresh tokens. The system also "
    "implements scope downscoping — a refresh token can only request scopes "
    "equal to or narrower than the original grant. For server-to-server "
    "authentication, we use client credentials flow with asymmetric JWT "
    "assertions (RS256). The signing keys are rotated monthly via an automated "
    "key rotation pipeline that updates the JWKS endpoint 24 hours before the "
    "old key expires. Rate limiting is applied per-client: 100 token requests "
    "per minute for interactive flows, 1000/min for service accounts. "
    "Failed authentication attempts are tracked per-IP and trigger progressive "
    "delays after 5 failures within a 10-minute window."
)

SHORT_CONTENT = "OAuth tokens need explicit scope re-request on refresh."

# A realistic summary that is meaningfully shorter
MOCK_SUMMARY = (
    "OAuth 2.0 with PKCE for public clients. 15-min access tokens, 7-day "
    "refresh tokens with rotation. Client credentials + RS256 for service auth. "
    "Rate limiting per-client with progressive delays on failures."
)


# ---------------------------------------------------------------------------
# (a) Saving >200 tokens generates summary and stores original separately
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
class TestCaseA_LongContentGeneratesSummary:
    """(a) Saving content >200 tokens generates a summary for the embedding
    and stores the original separately."""

    @pytest.mark.asyncio
    async def test_long_content_triggers_summary_generation(
        self, wired_plugin, mock_router, mock_store
    ):
        """Long content triggers an LLM summary call."""
        mock_router.search = AsyncMock(return_value=[])

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return MOCK_SUMMARY
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        result = await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": LONG_CONTENT}
        )

        assert result["success"] is True
        assert result["action"] == "created"
        assert result["has_summary"] is True

        # Verify LLM was called with a summarize prompt
        summary_calls = [
            c
            for c in wired_plugin._ctx.invoke_llm.call_args_list
            if "summarize" in c[0][0].lower()
        ]
        assert len(summary_calls) >= 1

    @pytest.mark.asyncio
    async def test_long_content_stores_summary_as_indexed_and_original_separately(
        self, wired_plugin, mock_router, mock_store
    ):
        """Milvus entry stores summary as 'content' and full text as 'original'."""
        mock_router.search = AsyncMock(return_value=[])

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return MOCK_SUMMARY
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": LONG_CONTENT}
        )

        # Inspect the Milvus upsert call
        mock_store.upsert.assert_called_once()
        upserted = mock_store.upsert.call_args[0][0][0]

        # Indexed content should be the summary (shorter)
        assert upserted["content"] == MOCK_SUMMARY
        # Original should be the full content (longer)
        assert upserted["original"] == LONG_CONTENT
        # They should be different
        assert upserted["content"] != upserted["original"]

    @pytest.mark.asyncio
    async def test_long_content_embedding_computed_on_summary(
        self, wired_plugin, mock_router, mock_store, mock_embedder
    ):
        """The embedding is computed on the summary, not the original."""
        mock_router.search = AsyncMock(return_value=[])

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return MOCK_SUMMARY
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": LONG_CONTENT}
        )

        # embed() is called twice: once for dedup search, once for save.
        # The save embedding (last call) should be on the summary text.
        assert mock_embedder.embed.call_count == 2
        last_embed_call = mock_embedder.embed.call_args_list[-1]
        assert last_embed_call[0][0] == [MOCK_SUMMARY]


# ---------------------------------------------------------------------------
# (b) memory_search returns the summary (optimized for search)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
class TestCaseB_SearchReturnsSummary:
    """(b) memory_search returns the summary (shorter, optimized for search)."""

    @pytest.mark.asyncio
    async def test_memory_search_returns_summary_not_original(
        self, wired_plugin, mock_router, service
    ):
        """memory_search results contain the summary, not the original."""
        # Configure service.search to return results with both content and original
        service.search = AsyncMock(
            return_value=[
                {
                    "content": MOCK_SUMMARY,
                    "original": LONG_CONTENT,
                    "source": "/vault/auth.md",
                    "heading": "Auth System",
                    "score": 0.92,
                    "weighted_score": 0.92,
                    "entry_type": "document",
                    "topic": "auth",
                    "tags": '["insight"]',
                    "chunk_hash": "abc123",
                    "_scope": "project",
                    "_scope_id": "test",
                    "_collection": "aq_project_test",
                }
            ]
        )

        result = await wired_plugin.cmd_memory_search(
            {"project_id": "test-project", "query": "auth system"}
        )

        assert result["success"] is True
        assert result["count"] == 1
        # memory_search returns the content field (summary), not original
        assert result["results"][0]["content"] == MOCK_SUMMARY
        # original should NOT appear in memory_search output
        assert "original" not in result["results"][0]

    @pytest.mark.asyncio
    async def test_memory_search_result_is_shorter_than_original(
        self, wired_plugin, mock_router, service
    ):
        """The search result content (summary) is shorter than the original."""
        service.search = AsyncMock(
            return_value=[
                {
                    "content": MOCK_SUMMARY,
                    "original": LONG_CONTENT,
                    "source": "/vault/auth.md",
                    "heading": "Auth",
                    "score": 0.9,
                    "weighted_score": 0.9,
                    "entry_type": "document",
                    "topic": "",
                    "tags": "[]",
                    "chunk_hash": "abc",
                    "_scope": "project",
                    "_scope_id": "test",
                    "_collection": "aq_project_test",
                }
            ]
        )

        result = await wired_plugin.cmd_memory_search(
            {"project_id": "test-project", "query": "auth"}
        )

        returned_content = result["results"][0]["content"]
        assert len(returned_content) < len(LONG_CONTENT)


# ---------------------------------------------------------------------------
# (c) memory_get with full=true returns the original full content
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
class TestCaseC_GetFullReturnsOriginal:
    """(c) memory_get with full=true returns the original full content."""

    @pytest.mark.asyncio
    async def test_full_true_returns_original_content(self, wired_plugin, mock_router):
        """When full=true, the result contains the full original content."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": MOCK_SUMMARY,
                    "original": LONG_CONTENT,
                    "source": "/vault/auth.md",
                    "heading": "Auth",
                    "score": 0.95,
                    "topic": "auth",
                    "tags": '["insight"]',
                    "_collection": "aq_project_test",
                    "_scope": "project",
                    "_scope_id": "test",
                }
            ]
        )

        result = await wired_plugin.cmd_memory_get(
            {"query": "auth system", "project_id": "proj", "full": True}
        )

        assert result["success"] is True
        assert result["source"] == "semantic"
        assert result["count"] == 1
        # Content should be the original, not the summary
        assert result["results"][0]["content"] == LONG_CONTENT
        assert result["results"][0]["full"] is True

    @pytest.mark.asyncio
    async def test_full_true_content_matches_original_exactly(self, wired_plugin, mock_router):
        """The returned content when full=true is the exact original, not a modification."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        # Use content with special chars, newlines, unicode to verify exact match
        original_with_special = (
            "Line 1: OAuth 2.0 uses PKCE—for \"public\" clients.\n"
            "Line 2: Tokens expire in 15′ (minutes).\n"
            "Line 3: Refresh → rotate → revoke old.\n"
            "Line 4: Scopes ≤ original grant.\n"
            "日本語テスト"
        )

        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "Short summary",
                    "original": original_with_special,
                    "source": "/vault/auth.md",
                    "heading": "Auth",
                    "score": 0.9,
                    "topic": "",
                    "tags": "[]",
                    "_collection": "aq_project_test",
                    "_scope": "project",
                    "_scope_id": "test",
                }
            ]
        )

        result = await wired_plugin.cmd_memory_get(
            {"query": "auth", "project_id": "proj", "full": True}
        )

        assert result["results"][0]["content"] == original_with_special


# ---------------------------------------------------------------------------
# (d) memory_get without full=true returns the summary
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
class TestCaseD_GetDefaultReturnsSummary:
    """(d) memory_get without full=true returns the summary."""

    @pytest.mark.asyncio
    async def test_default_returns_summary_not_original(self, wired_plugin, mock_router):
        """Default memory_get (no full param) returns the summary."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": MOCK_SUMMARY,
                    "original": LONG_CONTENT,
                    "source": "/vault/auth.md",
                    "heading": "Auth",
                    "score": 0.95,
                    "topic": "auth",
                    "tags": '["insight"]',
                    "_collection": "aq_project_test",
                    "_scope": "project",
                    "_scope_id": "test",
                }
            ]
        )

        result = await wired_plugin.cmd_memory_get(
            {"query": "auth system", "project_id": "proj"}
        )

        assert result["success"] is True
        assert result["source"] == "semantic"
        # Content should be the summary
        assert result["results"][0]["content"] == MOCK_SUMMARY
        # "full" flag should NOT be present
        assert "full" not in result["results"][0]

    @pytest.mark.asyncio
    async def test_explicit_full_false_returns_summary(self, wired_plugin, mock_router):
        """Explicitly passing full=false returns the summary."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": MOCK_SUMMARY,
                    "original": LONG_CONTENT,
                    "source": "/vault/auth.md",
                    "heading": "Auth",
                    "score": 0.95,
                    "topic": "",
                    "tags": "[]",
                    "_collection": "aq_project_test",
                    "_scope": "project",
                    "_scope_id": "test",
                }
            ]
        )

        result = await wired_plugin.cmd_memory_get(
            {"query": "auth system", "project_id": "proj", "full": False}
        )

        assert result["success"] is True
        assert result["results"][0]["content"] == MOCK_SUMMARY
        assert "full" not in result["results"][0]


# ---------------------------------------------------------------------------
# (e) Saving content <=200 tokens stores it as-is (no summary)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
class TestCaseE_ShortContentNoSummary:
    """(e) Saving content <=200 tokens stores it as-is (no summary generated)."""

    @pytest.mark.asyncio
    async def test_short_content_no_summary_llm_call(self, wired_plugin, mock_router):
        """Short content does not trigger a summary LLM call."""
        mock_router.search = AsyncMock(return_value=[])

        result = await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": SHORT_CONTENT}
        )

        assert result["success"] is True
        assert result["action"] == "created"
        assert result["has_summary"] is False

        # No summary calls should have been made
        summary_calls = [
            c
            for c in wired_plugin._ctx.invoke_llm.call_args_list
            if "summarize" in c[0][0].lower()
        ]
        assert len(summary_calls) == 0

    @pytest.mark.asyncio
    async def test_short_content_stored_as_is_in_milvus(
        self, wired_plugin, mock_router, mock_store
    ):
        """Short content is stored with identical content and original fields."""
        mock_router.search = AsyncMock(return_value=[])

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": SHORT_CONTENT}
        )

        # Inspect Milvus upsert
        mock_store.upsert.assert_called_once()
        upserted = mock_store.upsert.call_args[0][0][0]

        # Both content and original should be the same (no summary transformation)
        assert upserted["content"] == SHORT_CONTENT
        assert upserted["original"] == SHORT_CONTENT

    @pytest.mark.asyncio
    async def test_short_content_embedding_computed_on_original(
        self, wired_plugin, mock_router, mock_embedder
    ):
        """For short content, embedding is computed on the content itself."""
        mock_router.search = AsyncMock(return_value=[])

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": SHORT_CONTENT}
        )

        # embed() is called twice: once for dedup search, once for save.
        # Both should use the same content (no summary transformation).
        assert mock_embedder.embed.call_count == 2
        last_embed_call = mock_embedder.embed.call_args_list[-1]
        assert last_embed_call[0][0] == [SHORT_CONTENT]

    @pytest.mark.asyncio
    async def test_short_content_vault_file_no_original_section(
        self, wired_plugin, mock_router, tmp_data_dir
    ):
        """Short content vault file has no ## Original section."""
        mock_router.search = AsyncMock(return_value=[])

        result = await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": SHORT_CONTENT}
        )

        vault_path = Path(result["vault_path"])
        text = vault_path.read_text(encoding="utf-8")
        assert SHORT_CONTENT in text
        assert "## Original" not in text


# ---------------------------------------------------------------------------
# (f) Summary is meaningfully shorter than original (not just truncated)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
class TestCaseF_SummaryMeaninghfullyShorter:
    """(f) Summary is meaningfully shorter than original (not just truncated)."""

    @pytest.mark.asyncio
    async def test_summary_is_shorter_than_original(
        self, wired_plugin, mock_router, mock_store
    ):
        """The stored summary is meaningfully shorter than the original."""
        mock_router.search = AsyncMock(return_value=[])

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return MOCK_SUMMARY
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": LONG_CONTENT}
        )

        mock_store.upsert.assert_called_once()
        upserted = mock_store.upsert.call_args[0][0][0]

        summary_len = len(upserted["content"])
        original_len = len(upserted["original"])

        # Summary should be meaningfully shorter (at least 30% reduction)
        assert summary_len < original_len * 0.7, (
            f"Summary ({summary_len} chars) should be at least 30% shorter "
            f"than original ({original_len} chars)"
        )

    @pytest.mark.asyncio
    async def test_summary_is_not_a_prefix_of_original(
        self, wired_plugin, mock_router, mock_store
    ):
        """The summary is not simply a truncated prefix of the original."""
        mock_router.search = AsyncMock(return_value=[])

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return MOCK_SUMMARY
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": LONG_CONTENT}
        )

        upserted = mock_store.upsert.call_args[0][0][0]
        summary = upserted["content"]
        original = upserted["original"]

        # Summary should NOT be a simple prefix of the original
        assert not original.startswith(summary), (
            "Summary should be a synthesized condensation, not a prefix truncation"
        )

    @pytest.mark.asyncio
    async def test_summary_differs_from_original(
        self, wired_plugin, mock_router, mock_store
    ):
        """Summary and original are completely different strings."""
        mock_router.search = AsyncMock(return_value=[])

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return MOCK_SUMMARY
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": LONG_CONTENT}
        )

        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["content"] != upserted["original"]


# ---------------------------------------------------------------------------
# (g) Original is byte-for-byte identical to what was saved
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
class TestCaseG_OriginalPreservedExactly:
    """(g) Original content is byte-for-byte identical to what was saved
    (no transformation loss)."""

    @pytest.mark.asyncio
    async def test_original_matches_input_exactly_on_create(
        self, wired_plugin, mock_router, mock_store
    ):
        """On create path, the stored original is byte-for-byte the input content."""
        mock_router.search = AsyncMock(return_value=[])

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return MOCK_SUMMARY
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": LONG_CONTENT}
        )

        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["original"] == LONG_CONTENT
        # Byte-for-byte: encode both and compare
        assert upserted["original"].encode("utf-8") == LONG_CONTENT.encode("utf-8")

    @pytest.mark.asyncio
    async def test_original_preserves_special_characters(
        self, wired_plugin, mock_router, mock_store
    ):
        """Special characters (unicode, newlines, punctuation) survive round-trip."""
        mock_router.search = AsyncMock(return_value=[])

        content_with_special = (
            "Line 1: OAuth 2.0 uses PKCE—for \"public\" clients.\n"
            "Line 2: Tokens expire in 15′ (minutes).\n"
            "Line 3: Refresh → rotate → revoke old.\n"
            "Line 4: Scopes ≤ original grant.\n"
            "日本語テスト\n"
            "Emoji: 🔐🔑\n"
            "Tab:\there\n"
            "Backslash: path\\to\\file\n"
        )

        # Make it long enough to trigger summary generation
        long_special = content_with_special * 5  # Repeat to exceed 800 chars

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return "Brief summary of auth content with special characters."
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": long_special}
        )

        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["original"] == long_special
        assert upserted["original"].encode("utf-8") == long_special.encode("utf-8")

    @pytest.mark.asyncio
    async def test_original_matches_input_on_merge_path(
        self, wired_plugin, mock_router, mock_store
    ):
        """On merge path, the stored original is the full merged content."""
        # Set up a related match to trigger merge
        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "Existing insight about caching strategies",
                    "score": 0.88,
                    "chunk_hash": "existing_hash",
                    "entry_type": "document",
                    "topic": "caching",
                    "tags": '["insight"]',
                    "_scope": "project",
                    "_scope_id": "test",
                }
            ]
        )

        # LLM returns long merged content (triggers summary)
        long_merged = (
            "Comprehensive merged insight about caching strategies including "
            "invalidation patterns, TTL configuration, cache warming techniques, "
            "and distributed cache coordination. " * 5
        )

        async def mock_llm(prompt, **kwargs):
            if "merging two related" in prompt.lower():
                return long_merged
            elif "summarize" in prompt.lower():
                return "Caching strategies summary."
            return "caching"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "New caching insight",
                "tags": ["insight"],
            }
        )

        assert result["action"] == "merged"
        assert result["has_summary"] is True

        # The original stored should be the full merged content
        mock_store.upsert.assert_called()
        # update_document_content fetches existing, then upserts
        last_upsert = mock_store.upsert.call_args[0][0][0]
        assert last_upsert["original"] == long_merged

    @pytest.mark.asyncio
    async def test_short_content_original_matches_input_exactly(
        self, wired_plugin, mock_router, mock_store
    ):
        """For short content (no summary), original equals input content."""
        mock_router.search = AsyncMock(return_value=[])

        await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": SHORT_CONTENT}
        )

        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["original"] == SHORT_CONTENT
        assert upserted["content"] == SHORT_CONTENT

    @pytest.mark.asyncio
    async def test_original_in_vault_file_matches_input(
        self, wired_plugin, mock_router, tmp_data_dir
    ):
        """The ## Original section in the vault file preserves exact content."""
        mock_router.search = AsyncMock(return_value=[])

        async def mock_llm(prompt, **kwargs):
            if "summarize" in prompt.lower():
                return MOCK_SUMMARY
            return "general"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        result = await wired_plugin.cmd_memory_save(
            {"project_id": "test-project", "content": LONG_CONTENT}
        )

        vault_path = Path(result["vault_path"])
        text = vault_path.read_text(encoding="utf-8")

        # Vault file should contain the ## Original section with the full content
        assert "## Original" in text
        assert LONG_CONTENT in text

        # Summary should also appear (before the ## Original section)
        assert MOCK_SUMMARY in text
