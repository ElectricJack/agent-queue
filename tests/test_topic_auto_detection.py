"""Tests for topic auto-detection in memory_save (roadmap 2.2.6, spec §3).

Tests cover:
- Controlled vocabulary: list exists, expected topics present
- Keyword-based fallback: correct topic for various content
- LLM-based inference: prompt format, normalization, fallback on failure
- Integration with cmd_memory_save: topic inferred when not provided,
  passed through when explicit, flag in response
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip entire module on Windows (Milvus Lite not supported)
if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.memory_v2_service import MEMSEARCH_AVAILABLE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin():
    from src.plugins.internal.memory_v2 import MemoryV2Plugin

    return MemoryV2Plugin()


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    embedder.model_name = "test-model"
    embedder.dimension = 384
    embedder.embed = AsyncMock(return_value=[[0.1] * 384])
    return embedder


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.count.return_value = 10
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
    router = MagicMock()
    router.get_store.return_value = mock_store
    router.list_collections.return_value = []
    router.search = AsyncMock(return_value=[])
    router.close = MagicMock()
    return router


@pytest.fixture
def service(mock_embedder, mock_router):
    import tempfile

    from src.memory_v2_service import MemoryV2Service

    with tempfile.TemporaryDirectory() as d:
        svc = MemoryV2Service(
            milvus_uri="/tmp/test.db",
            embedding_provider="openai",
            data_dir=d,
        )
        svc._embedder = mock_embedder
        svc._router = mock_router
        svc._initialized = True
        yield svc


@pytest.fixture
def wired_plugin(plugin, service):
    """Plugin with a wired-up service and mock context."""
    plugin._service = service
    plugin._log = MagicMock()
    plugin._ctx = MagicMock()
    plugin._ctx.invoke_llm = AsyncMock(return_value="testing")
    return plugin


# ---------------------------------------------------------------------------
# Controlled Vocabulary
# ---------------------------------------------------------------------------


class TestControlledVocabulary:
    """Verify the controlled topic vocabulary."""

    def test_vocabulary_exists(self, plugin):
        assert hasattr(plugin, "CONTROLLED_TOPICS")
        assert isinstance(plugin.CONTROLLED_TOPICS, list)
        assert len(plugin.CONTROLLED_TOPICS) > 10

    def test_common_topics_present(self, plugin):
        expected = [
            "authentication",
            "database",
            "testing",
            "deployment",
            "security",
            "performance",
            "architecture",
            "documentation",
            "git",
            "ci-cd",
        ]
        for topic in expected:
            assert topic in plugin.CONTROLLED_TOPICS, f"Missing topic: {topic}"

    def test_topics_are_lowercase_hyphenated(self, plugin):
        for topic in plugin.CONTROLLED_TOPICS:
            assert topic == topic.lower(), f"Topic not lowercase: {topic}"
            assert " " not in topic, f"Topic contains space: {topic}"
            assert "_" not in topic, f"Topic contains underscore: {topic}"

    def test_topics_are_sorted(self, plugin):
        assert plugin.CONTROLLED_TOPICS == sorted(plugin.CONTROLLED_TOPICS), (
            "Topics should be sorted alphabetically"
        )

    def test_no_duplicate_topics(self, plugin):
        assert len(plugin.CONTROLLED_TOPICS) == len(set(plugin.CONTROLLED_TOPICS)), (
            "Duplicate topics found"
        )


# ---------------------------------------------------------------------------
# Keyword-based fallback
# ---------------------------------------------------------------------------


class TestKeywordFallback:
    """Test keyword-based topic inference."""

    def test_auth_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("OAuth token refresh broke") == "authentication"
        assert plugin._infer_topic_via_keywords("JWT validation failed") == "authentication"
        assert plugin._infer_topic_via_keywords("SSO login flow") == "authentication"

    def test_database_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("SQL query optimization") == "database"
        assert plugin._infer_topic_via_keywords("Alembic migration needs review") == "database"
        assert plugin._infer_topic_via_keywords("PostgreSQL connection pool") == "database"

    def test_testing_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("pytest fixtures need cleanup") == "testing"
        assert plugin._infer_topic_via_keywords("The test coverage is low") == "testing"

    def test_deployment_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("Docker container restart") == "deployment"
        assert plugin._infer_topic_via_keywords("Kubernetes pod scaling") == "deployment"

    def test_git_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("merge conflict resolution") == "git"
        assert plugin._infer_topic_via_keywords("rebase the branch") == "git"

    def test_performance_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("cache invalidation strategy") == "performance"
        assert plugin._infer_topic_via_keywords("latency is too high") == "performance"

    def test_security_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("encryption key rotation") == "security"
        assert plugin._infer_topic_via_keywords("CVE patch required") == "security"

    def test_no_match_returns_none(self, plugin):
        assert plugin._infer_topic_via_keywords("xyzzy foobar baz") is None
        assert plugin._infer_topic_via_keywords("") is None

    def test_case_insensitive(self, plugin):
        assert plugin._infer_topic_via_keywords("OAUTH TOKEN") == "authentication"
        assert plugin._infer_topic_via_keywords("Docker Container") == "deployment"

    def test_highest_score_wins(self, plugin):
        # Content with multiple database keywords should still pick database
        content = (
            "The SQL database schema migration via Alembic needs a new query for the sqlite table"
        )
        assert plugin._infer_topic_via_keywords(content) == "database"

    def test_ci_cd_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("GitHub Actions pipeline") == "ci-cd"

    def test_logging_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("structlog logger setup") == "logging"

    def test_plugins_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("plugin system extension hooks") == "plugins"

    def test_scheduling_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("cron job scheduling") == "scheduling"
        assert plugin._infer_topic_via_keywords("rate limit handling") == "scheduling"

    def test_memory_keywords(self, plugin):
        assert plugin._infer_topic_via_keywords("vault file structure") == "memory"
        assert plugin._infer_topic_via_keywords("vector search optimization") == "memory"


# ---------------------------------------------------------------------------
# LLM-based inference
# ---------------------------------------------------------------------------


class TestLLMInference:
    """Test LLM-based topic inference."""

    @pytest.mark.asyncio
    async def test_llm_returns_controlled_topic(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="authentication")
        result = await wired_plugin._infer_topic_via_llm("OAuth token refresh", "")
        assert result == "authentication"

    @pytest.mark.asyncio
    async def test_llm_normalizes_response(self, wired_plugin):
        # Spaces → hyphens
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="error handling")
        result = await wired_plugin._infer_topic_via_llm("exception patterns", "")
        assert result == "error-handling"

    @pytest.mark.asyncio
    async def test_llm_strips_quotes(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value='"testing"')
        result = await wired_plugin._infer_topic_via_llm("pytest fixture", "")
        assert result == "testing"

    @pytest.mark.asyncio
    async def test_llm_strips_whitespace(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="  database\n")
        result = await wired_plugin._infer_topic_via_llm("SQL query", "")
        assert result == "database"

    @pytest.mark.asyncio
    async def test_llm_normalizes_underscores(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="error_handling")
        result = await wired_plugin._infer_topic_via_llm("exception", "")
        assert result == "error-handling"

    @pytest.mark.asyncio
    async def test_llm_removes_special_chars(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="ci/cd!")
        result = await wired_plugin._infer_topic_via_llm("pipeline stuff", "")
        assert result == "cicd"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
        result = await wired_plugin._infer_topic_via_llm("some content", "")
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_empty_response_returns_none(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="   ")
        result = await wired_plugin._infer_topic_via_llm("some content", "")
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_prompt_includes_vocabulary(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="testing")
        await wired_plugin._infer_topic_via_llm("test content", "")
        call_args = wired_plugin._ctx.invoke_llm.call_args
        prompt = call_args[0][0]
        assert "CONTROLLED TOPICS" in prompt
        assert "authentication" in prompt
        assert "testing" in prompt

    @pytest.mark.asyncio
    async def test_llm_prompt_includes_context(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="testing")
        await wired_plugin._infer_topic_via_llm(
            "test content", "Tags: insight, auth\nSource task: task-123"
        )
        call_args = wired_plugin._ctx.invoke_llm.call_args
        prompt = call_args[0][0]
        assert "CONTEXT" in prompt
        assert "Tags: insight, auth" in prompt

    @pytest.mark.asyncio
    async def test_llm_prompt_omits_context_when_empty(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="testing")
        await wired_plugin._infer_topic_via_llm("test content", "")
        call_args = wired_plugin._ctx.invoke_llm.call_args
        prompt = call_args[0][0]
        assert "CONTEXT:" not in prompt

    @pytest.mark.asyncio
    async def test_llm_uses_haiku_model(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="testing")
        await wired_plugin._infer_topic_via_llm("test content", "")
        call_args = wired_plugin._ctx.invoke_llm.call_args
        assert call_args[1]["model"] == "claude-haiku-4-20250514"


# ---------------------------------------------------------------------------
# Full _infer_topic flow
# ---------------------------------------------------------------------------


class TestInferTopic:
    """Test the full _infer_topic orchestration."""

    @pytest.mark.asyncio
    async def test_llm_success_used_over_keyword(self, wired_plugin):
        """LLM result takes precedence over keyword fallback."""
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="architecture")
        # Content has "test" keyword which would match "testing" via keywords
        result = await wired_plugin._infer_topic("test the architecture pattern")
        assert result == "architecture"

    @pytest.mark.asyncio
    async def test_fallback_to_keywords_on_llm_failure(self, wired_plugin):
        """When LLM fails, keyword matching takes over."""
        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=RuntimeError("unavailable"))
        result = await wired_plugin._infer_topic("OAuth token refresh requires explicit scope")
        assert result == "authentication"

    @pytest.mark.asyncio
    async def test_none_when_no_match(self, wired_plugin):
        """Returns None when both LLM and keywords fail to match."""
        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=RuntimeError("unavailable"))
        result = await wired_plugin._infer_topic("xyzzy foobar baz")
        assert result is None

    @pytest.mark.asyncio
    async def test_passes_tags_as_context(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="authentication")
        await wired_plugin._infer_topic(
            "some content",
            tags=["insight", "auth-related"],
        )
        call_args = wired_plugin._ctx.invoke_llm.call_args
        prompt = call_args[0][0]
        assert "auth-related" in prompt

    @pytest.mark.asyncio
    async def test_passes_source_task_as_context(self, wired_plugin):
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="testing")
        await wired_plugin._infer_topic(
            "some content",
            source_task="task-fix-auth",
        )
        call_args = wired_plugin._ctx.invoke_llm.call_args
        prompt = call_args[0][0]
        assert "task-fix-auth" in prompt


# ---------------------------------------------------------------------------
# Integration with cmd_memory_save
# ---------------------------------------------------------------------------


class TestMemorySaveTopicIntegration:
    """Test topic auto-detection integrated into cmd_memory_save."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_infers_topic_when_not_provided(self, wired_plugin, mock_router):
        """When topic is omitted, auto-detection kicks in."""
        mock_router.search = AsyncMock(return_value=[])
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="authentication")

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "OAuth tokens must be refreshed with explicit scope.",
            }
        )
        assert result["success"] is True
        assert result["action"] == "created"
        assert result["topic"] == "authentication"
        assert result["topic_auto_detected"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_explicit_topic_not_overridden(self, wired_plugin, mock_router):
        """When topic is explicitly provided, no auto-detection occurs."""
        mock_router.search = AsyncMock(return_value=[])

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "OAuth tokens must be refreshed with explicit scope.",
                "topic": "security",
            }
        )
        assert result["success"] is True
        assert result["topic"] == "security"
        assert "topic_auto_detected" not in result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_keyword_fallback_in_save(self, wired_plugin, mock_router):
        """When LLM is unavailable, keyword fallback still assigns a topic."""
        mock_router.search = AsyncMock(return_value=[])
        # First call for topic inference fails, subsequent calls succeed
        # (summary/merge might also use LLM)
        call_count = 0

        async def selective_llm_failure(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if "CONTROLLED TOPICS" in prompt:
                raise RuntimeError("LLM unavailable")
            return "LLM generated summary"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=selective_llm_failure)

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "The pytest fixtures need better isolation for database tests.",
            }
        )
        assert result["success"] is True
        assert result["topic"] == "testing"
        assert result["topic_auto_detected"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_no_topic_when_inference_fails(self, wired_plugin, mock_router):
        """When both LLM and keywords fail, topic remains empty."""
        mock_router.search = AsyncMock(return_value=[])
        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Xyzzy foobar baz qux.",
            }
        )
        assert result["success"] is True
        assert result["topic"] == ""  # Empty, no match
        assert "topic_auto_detected" not in result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_auto_topic_used_in_dedup_search(self, wired_plugin, mock_router):
        """Auto-detected topic should be used for the dedup search."""
        wired_plugin._ctx.invoke_llm = AsyncMock(return_value="authentication")
        mock_router.search = AsyncMock(return_value=[])

        await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "OAuth tokens need scope re-request on refresh.",
            }
        )

        # Verify the inferred topic was used: check the saved document has the topic
        store = mock_router.get_store.return_value
        upserted = store.upsert.call_args[0][0][0]
        assert upserted["topic"] == "authentication"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_auto_topic_in_merge_path(self, wired_plugin, mock_router):
        """Auto-detected topic should be propagated through the merge path."""

        # LLM returns different things for different prompts
        async def mock_llm(prompt, **kwargs):
            if "CONTROLLED TOPICS" in prompt:
                return "authentication"
            if "merging two related" in prompt.lower():
                return "Merged: OAuth tokens need both scope and refresh handling."
            return "LLM response"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "OAuth needs scope on refresh",
                    "score": 0.88,
                    "chunk_hash": "existing_hash",
                    "entry_type": "document",
                    "topic": "authentication",
                    "tags": '["insight"]',
                    "_scope": "project",
                    "_scope_id": "test",
                }
            ]
        )

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "OAuth also requires re-consent for elevated scopes.",
            }
        )
        assert result["success"] is True
        assert result["action"] == "merged"
        assert result.get("topic_auto_detected") is True
