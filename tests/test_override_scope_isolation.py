"""Tests for override scope isolation — Roadmap 3.2.5.

Verifies that override files are correctly scoped by agent type, project,
and scope level.  Uses real Milvus Lite for integration tests (skipped on
Windows where Milvus Lite is unsupported).

Test cases from the roadmap:

(a) override file ``overrides/coding.md`` does NOT appear in searches for
    agent-type "qa"
(b) override file ``overrides/coding.md`` DOES appear for agent-type
    "coding" in that project
(c) system-level override in ``vault/system/overrides/`` applies to all
    agent types
(d) project override takes precedence over system override for the same
    agent type
(e) agent with no matching override file still works normally (no override
    is fine)
(f) override for project A does not leak into project B searches even for
    the same agent type
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from src.override_handler import (
    OVERRIDE_TAG,
    OverrideIndexer,
)

# Skip entire module on Windows (Milvus Lite not supported).
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Milvus Lite not supported on Windows",
)


# ---------------------------------------------------------------------------
# Sample content
# ---------------------------------------------------------------------------

CODING_OVERRIDE = """\
# Coding Agent Overrides — Test Project

This project uses a custom ECS framework. Do not use inheritance for
game entities — always use composition via the component system.

Prefer integration tests over unit tests for the component system.
"""

QA_OVERRIDE = """\
# QA Agent Overrides — Test Project

Focus on end-to-end testing for this project. Use Playwright for
browser-based tests and pytest for API tests.
"""

SYSTEM_OVERRIDE_CONTENT = """\
# System-Wide Coding Override

All coding agents must use type hints for every function signature.
Follow PEP 8 style guidelines across all projects.
"""

REGULAR_MEMORY = """\
This project uses Python 3.12 and asyncio for all concurrency.
"""


class _FakeEmbedder:
    """Deterministic fake embedder — identical content produces identical vectors."""

    model_name: str = "fake-test-model"
    dimension: int = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        results = []
        for text in texts:
            h = hashlib.sha256(text.encode()).digest()
            vec = [b / 255.0 for b in h[:4]]
            results.append(vec)
        return results


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def scope_env(tmp_path):
    """Provide a fresh CollectionRouter, OverrideIndexer, and temp vault."""
    from memsearch.scoping import CollectionRouter

    db_path = tmp_path / "scope_test.db"
    embedder = _FakeEmbedder()
    router = CollectionRouter(
        milvus_uri=str(db_path),
        dimension=embedder.dimension,
    )
    indexer = OverrideIndexer(router, embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    yield indexer, router, embedder, vault
    router.close()


# ---------------------------------------------------------------------------
# Helper: manually upsert override-tagged chunks into a collection
# ---------------------------------------------------------------------------


async def _upsert_override_chunk(
    router,
    embedder,
    scope,
    scope_id,
    *,
    content: str,
    chunk_hash: str,
    source: str,
    tags: list[str],
    heading: str = "",
):
    """Insert a single override-tagged chunk into the given scope's store."""
    store = router.get_store(scope, scope_id, description=f"{scope.value}/{scope_id or ''}")
    embedding = (await embedder.embed([content]))[0]
    now = int(time.time())
    store.upsert(
        [
            {
                "chunk_hash": chunk_hash,
                "entry_type": "document",
                "embedding": embedding,
                "content": content,
                "original": content,
                "source": source,
                "heading": heading,
                "heading_level": 1,
                "start_line": 1,
                "end_line": 5,
                "tags": json.dumps(tags),
                "updated_at": now,
            }
        ]
    )


# ---------------------------------------------------------------------------
# (a) Type isolation — coding override NOT found for agent-type "qa"
# ---------------------------------------------------------------------------


class TestTypeIsolationNegative:
    """(a) ``overrides/coding.md`` does NOT appear in searches for agent-type "qa"."""

    @pytest.mark.asyncio
    async def test_coding_override_invisible_to_qa_tag_search(self, scope_env):
        """Tag search for 'qa' in the project must not return coding-tagged chunks."""
        indexer, router, _, vault = scope_env
        from memsearch.scoping import MemoryScope

        # Index a coding override into the project collection.
        override_dir = vault / "projects" / "myproject" / "overrides"
        override_dir.mkdir(parents=True)
        coding_file = override_dir / "coding.md"
        coding_file.write_text(CODING_OVERRIDE)
        n = await indexer.index_override("myproject", "coding", str(coding_file))
        assert n >= 1

        # Search by tag "qa" scoped to the project.
        results = router.search_by_tag(
            "qa",
            scopes=[(MemoryScope.PROJECT, "myproject")],
        )

        # No coding-tagged chunks should appear.
        assert len(results) == 0, (
            f"Expected 0 results for tag 'qa', got {len(results)}: "
            f"coding override leaked into qa search"
        )

    @pytest.mark.asyncio
    async def test_coding_override_invisible_via_scalar_query(self, scope_env):
        """Direct scalar query with qa tag filter must not return coding chunks."""
        indexer, router, _, vault = scope_env
        from memsearch.scoping import MemoryScope

        override_dir = vault / "projects" / "myproject" / "overrides"
        override_dir.mkdir(parents=True)
        coding_file = override_dir / "coding.md"
        coding_file.write_text(CODING_OVERRIDE)
        await indexer.index_override("myproject", "coding", str(coding_file))

        # Query the project store directly with a "qa" tag filter.
        store = router.get_store(MemoryScope.PROJECT, "myproject")
        results = store.query(filter_expr='tags like "%\\"qa\\"%"')

        assert len(results) == 0, "Scalar query for 'qa' tag should return nothing"


# ---------------------------------------------------------------------------
# (b) Type isolation — coding override DOES appear for agent-type "coding"
# ---------------------------------------------------------------------------


class TestTypeIsolationPositive:
    """(b) ``overrides/coding.md`` DOES appear for agent-type "coding"."""

    @pytest.mark.asyncio
    async def test_coding_override_found_via_coding_tag_search(self, scope_env):
        """Tag search for 'coding' in the project finds the coding override."""
        indexer, router, _, vault = scope_env
        from memsearch.scoping import MemoryScope

        override_dir = vault / "projects" / "myproject" / "overrides"
        override_dir.mkdir(parents=True)
        coding_file = override_dir / "coding.md"
        coding_file.write_text(CODING_OVERRIDE)
        await indexer.index_override("myproject", "coding", str(coding_file))

        results = router.search_by_tag(
            "coding",
            scopes=[(MemoryScope.PROJECT, "myproject")],
        )

        assert len(results) >= 1, "coding tag search should find the coding override"

        # Every result should carry both #override and #coding tags.
        for r in results:
            tags = json.loads(r.get("tags", "[]"))
            assert OVERRIDE_TAG in tags, f"Chunk should be tagged #override: {tags}"
            assert "coding" in tags, f"Chunk should be tagged #coding: {tags}"

    @pytest.mark.asyncio
    async def test_coding_override_found_via_override_tag_search(self, scope_env):
        """Tag search for 'override' in the project finds the coding override."""
        indexer, router, _, vault = scope_env
        from memsearch.scoping import MemoryScope

        override_dir = vault / "projects" / "myproject" / "overrides"
        override_dir.mkdir(parents=True)
        coding_file = override_dir / "coding.md"
        coding_file.write_text(CODING_OVERRIDE)
        await indexer.index_override("myproject", "coding", str(coding_file))

        results = router.search_by_tag(
            OVERRIDE_TAG,
            scopes=[(MemoryScope.PROJECT, "myproject")],
        )
        assert len(results) >= 1, "override tag search should find the coding override"

    @pytest.mark.asyncio
    async def test_multiple_agent_types_independently_searchable(self, scope_env):
        """Both coding and qa overrides in the same project are independently searchable."""
        indexer, router, _, vault = scope_env
        from memsearch.scoping import MemoryScope

        override_dir = vault / "projects" / "myproject" / "overrides"
        override_dir.mkdir(parents=True)

        # Index coding override
        coding_file = override_dir / "coding.md"
        coding_file.write_text(CODING_OVERRIDE)
        await indexer.index_override("myproject", "coding", str(coding_file))

        # Index qa override
        qa_file = override_dir / "qa.md"
        qa_file.write_text(QA_OVERRIDE)
        await indexer.index_override("myproject", "qa", str(qa_file))

        # Tag search for "coding" — only coding chunks
        coding_results = router.search_by_tag(
            "coding",
            scopes=[(MemoryScope.PROJECT, "myproject")],
        )
        for r in coding_results:
            tags = json.loads(r.get("tags", "[]"))
            assert "coding" in tags
            assert "qa" not in tags, "coding search should not return qa-tagged chunks"

        # Tag search for "qa" — only qa chunks
        qa_results = router.search_by_tag(
            "qa",
            scopes=[(MemoryScope.PROJECT, "myproject")],
        )
        for r in qa_results:
            tags = json.loads(r.get("tags", "[]"))
            assert "qa" in tags
            assert "coding" not in tags, "qa search should not return coding-tagged chunks"

        assert len(coding_results) >= 1
        assert len(qa_results) >= 1


# ---------------------------------------------------------------------------
# (c) System-level override applies to all agent types
# ---------------------------------------------------------------------------


class TestSystemOverride:
    """(c) System-level override in ``vault/system/overrides/`` applies to all."""

    @pytest.mark.asyncio
    async def test_system_override_visible_from_any_agent_type(self, scope_env):
        """A system-scope override is discovered regardless of agent type."""
        _, router, embedder, _ = scope_env
        from memsearch.scoping import MemoryScope

        # Manually upsert a system-level override.
        await _upsert_override_chunk(
            router,
            embedder,
            MemoryScope.SYSTEM,
            None,
            content=SYSTEM_OVERRIDE_CONTENT,
            chunk_hash="sys_override_001",
            source="/vault/system/overrides/coding.md",
            tags=[OVERRIDE_TAG],
            heading="System-Wide Coding Override",
        )

        # The system collection should be searched for every agent type.
        for agent_type in ("coding", "qa", "devops", "review-specialist"):
            results = router.search_by_tag(
                OVERRIDE_TAG,
                scopes=[(MemoryScope.SYSTEM, None)],
            )
            system_hits = [r for r in results if r.get("_scope") == "system"]
            assert len(system_hits) >= 1, (
                f"System override should be visible when searching as '{agent_type}'"
            )

    @pytest.mark.asyncio
    async def test_system_override_included_in_multi_scope_search(self, scope_env):
        """Multi-scope search includes system override alongside project results."""
        _, router, embedder, _ = scope_env
        from memsearch.scoping import MemoryScope

        # Upsert system override.
        await _upsert_override_chunk(
            router,
            embedder,
            MemoryScope.SYSTEM,
            None,
            content=SYSTEM_OVERRIDE_CONTENT,
            chunk_hash="sys_override_multi_001",
            source="/vault/system/overrides/all.md",
            tags=[OVERRIDE_TAG],
            heading="System Override",
        )

        # Multi-scope search: system scope is always included.
        query_embedding = (await embedder.embed(["type hints PEP 8"]))[0]
        results = await router.search(
            query_embedding,
            query_text="type hints PEP 8",
            project_id="anyproject",
            agent_type="coding",
            top_k=20,
        )

        system_results = [r for r in results if r.get("_scope") == "system"]
        assert len(system_results) >= 1, (
            "System override should appear in multi-scope search results"
        )


# ---------------------------------------------------------------------------
# (d) Project override takes precedence over system override
# ---------------------------------------------------------------------------


class TestOverridePrecedence:
    """(d) Project override takes precedence over system override for the same type."""

    @pytest.mark.asyncio
    async def test_scope_weights_ensure_project_beats_system(self, scope_env):
        """SCOPE_WEIGHTS guarantee project > system numerically."""
        from memsearch.scoping import SCOPE_WEIGHTS, MemoryScope

        assert SCOPE_WEIGHTS[MemoryScope.PROJECT] > SCOPE_WEIGHTS[MemoryScope.SYSTEM], (
            f"Project weight ({SCOPE_WEIGHTS[MemoryScope.PROJECT]}) must be "
            f"greater than system weight ({SCOPE_WEIGHTS[MemoryScope.SYSTEM]})"
        )

    @pytest.mark.asyncio
    async def test_project_override_outranks_system_in_search(self, scope_env):
        """Multi-scope search ranks project override above system override."""
        indexer, router, embedder, vault = scope_env
        from memsearch.scoping import SCOPE_WEIGHTS, MemoryScope

        # Use identical content so raw similarity scores are comparable.
        shared_content = (
            "Always use composition over inheritance for game entities. "
            "The ECS framework requires component-based architecture."
        )

        # 1. Index project-level override via the OverrideIndexer.
        override_dir = vault / "projects" / "myproject" / "overrides"
        override_dir.mkdir(parents=True)
        coding_file = override_dir / "coding.md"
        coding_file.write_text(f"# Coding Override\n\n{shared_content}\n")
        await indexer.index_override("myproject", "coding", str(coding_file))

        # 2. Insert the same content into system collection (simulating
        #    a system-level override).
        await _upsert_override_chunk(
            router,
            embedder,
            MemoryScope.SYSTEM,
            None,
            content=shared_content,
            chunk_hash="sys_prec_override_001",
            source="/vault/system/overrides/coding.md",
            tags=[OVERRIDE_TAG, "coding"],
            heading="System Coding Override",
        )

        # 3. Multi-scope search.
        query_embedding = (await embedder.embed([shared_content]))[0]
        results = await router.search(
            query_embedding,
            query_text=shared_content,
            project_id="myproject",
            agent_type="coding",
            top_k=20,
        )

        project_hits = [r for r in results if r.get("_scope") == "project"]
        system_hits = [r for r in results if r.get("_scope") == "system"]

        # Both scopes should contribute results.
        assert len(project_hits) >= 1, "Expected project-scope results"
        assert len(system_hits) >= 1, "Expected system-scope results"

        # Every project hit should carry the project weight, system hits the system weight.
        for r in project_hits:
            assert r["_weight"] == SCOPE_WEIGHTS[MemoryScope.PROJECT]
        for r in system_hits:
            assert r["_weight"] == SCOPE_WEIGHTS[MemoryScope.SYSTEM]

        # The best project result must outrank the best system result.
        best_project = max(r["weighted_score"] for r in project_hits)
        best_system = max(r["weighted_score"] for r in system_hits)
        assert best_project > best_system, (
            f"Project override (weighted_score={best_project:.4f}) should outrank "
            f"system override (weighted_score={best_system:.4f})"
        )

    @pytest.mark.asyncio
    async def test_merge_and_rank_respects_weighted_score(self, scope_env):
        """merge_and_rank sorts by weighted_score, confirming precedence logic."""
        from memsearch.scoping import merge_and_rank

        # Simulate results from two scopes with the same raw score
        # but different weights.
        project_result = {
            "chunk_hash": "proj_001",
            "score": 0.85,
            "weighted_score": 0.85 * 1.0,  # project weight
            "_scope": "project",
        }
        system_result = {
            "chunk_hash": "sys_001",
            "score": 0.85,
            "weighted_score": 0.85 * 0.4,  # system weight
            "_scope": "system",
        }

        ranked = merge_and_rank([system_result, project_result], top_k=10)

        assert len(ranked) == 2
        assert ranked[0]["chunk_hash"] == "proj_001", (
            "Project result should rank first when raw scores are equal"
        )
        assert ranked[1]["chunk_hash"] == "sys_001"


# ---------------------------------------------------------------------------
# (e) Agent with no matching override still works normally
# ---------------------------------------------------------------------------


class TestNoOverrideGraceful:
    """(e) Agent with no matching override file still works normally."""

    @pytest.mark.asyncio
    async def test_search_with_no_overrides_returns_normally(self, scope_env):
        """Multi-scope search succeeds even when no overrides exist."""
        _, router, embedder, _ = scope_env
        from memsearch.scoping import MemoryScope

        # Create a project with regular (non-override) memory only.
        await _upsert_override_chunk(
            router,
            embedder,
            MemoryScope.PROJECT,
            "myproject",
            content=REGULAR_MEMORY,
            chunk_hash="regular_mem_001",
            source="/vault/projects/myproject/memory/arch.md",
            tags=[],
            heading="Architecture",
        )

        # Confirm no override-tagged chunks exist.
        override_results = router.search_by_tag(
            OVERRIDE_TAG,
            scopes=[(MemoryScope.PROJECT, "myproject")],
        )
        assert len(override_results) == 0, "No overrides should exist"

        # Multi-scope search should still complete without errors.
        query_embedding = (await embedder.embed(["Python asyncio"]))[0]
        results = await router.search(
            query_embedding,
            query_text="Python asyncio",
            project_id="myproject",
            agent_type="coding",
            top_k=10,
        )
        assert isinstance(results, list), "Search should return a list"
        # The regular memory should be findable.
        assert len(results) >= 1, "Should find the regular project memory"

    @pytest.mark.asyncio
    async def test_tag_search_empty_when_no_overrides(self, scope_env):
        """Tag search for 'override' returns empty when no overrides are indexed."""
        _, router, embedder, _ = scope_env
        from memsearch.scoping import MemoryScope

        # Create project collection with non-override content.
        await _upsert_override_chunk(
            router,
            embedder,
            MemoryScope.PROJECT,
            "clean-project",
            content="Just a regular memory about database schema.",
            chunk_hash="no_override_mem_001",
            source="/vault/projects/clean-project/memory/db.md",
            tags=["database"],
            heading="Database",
        )

        results = router.search_by_tag(
            OVERRIDE_TAG,
            scopes=[(MemoryScope.PROJECT, "clean-project")],
        )
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_empty_project_search_no_crash(self, scope_env):
        """Searching an empty project (no data at all) doesn't crash."""
        _, router, embedder, _ = scope_env

        query_embedding = (await embedder.embed(["anything"]))[0]
        results = await router.search(
            query_embedding,
            query_text="anything",
            project_id="nonexistent-project",
            agent_type="coding",
            top_k=10,
        )
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# (f) Project isolation — project A override does not leak to project B
# ---------------------------------------------------------------------------


class TestProjectIsolation:
    """(f) Override for project A does not leak into project B."""

    @pytest.mark.asyncio
    async def test_override_invisible_in_other_project_tag_search(self, scope_env):
        """Tag search scoped to project B must not find project A's overrides."""
        indexer, router, embedder, vault = scope_env
        from memsearch.scoping import MemoryScope

        # Index coding override into project A.
        override_dir_a = vault / "projects" / "project-a" / "overrides"
        override_dir_a.mkdir(parents=True)
        coding_file = override_dir_a / "coding.md"
        coding_file.write_text(CODING_OVERRIDE)
        await indexer.index_override("project-a", "coding", str(coding_file))

        # Create project B with regular memory (no overrides).
        await _upsert_override_chunk(
            router,
            embedder,
            MemoryScope.PROJECT,
            "project-b",
            content="Project B uses React and TypeScript.",
            chunk_hash="projb_mem_001",
            source="/vault/projects/project-b/memory/arch.md",
            tags=["architecture"],
            heading="Architecture",
        )

        # Tag search in project B must not find project A's coding override.
        results_b_override = router.search_by_tag(
            OVERRIDE_TAG,
            scopes=[(MemoryScope.PROJECT, "project-b")],
        )
        assert len(results_b_override) == 0, (
            "Project A's override must not leak into project B override search"
        )

        results_b_coding = router.search_by_tag(
            "coding",
            scopes=[(MemoryScope.PROJECT, "project-b")],
        )
        assert len(results_b_coding) == 0, (
            "Project A's coding tag must not leak into project B coding search"
        )

    @pytest.mark.asyncio
    async def test_override_still_exists_in_original_project(self, scope_env):
        """Project A's override is intact while project B sees nothing."""
        indexer, router, embedder, vault = scope_env
        from memsearch.scoping import MemoryScope

        override_dir = vault / "projects" / "project-a" / "overrides"
        override_dir.mkdir(parents=True)
        coding_file = override_dir / "coding.md"
        coding_file.write_text(CODING_OVERRIDE)
        await indexer.index_override("project-a", "coding", str(coding_file))

        # Ensure project B collection exists (empty of overrides).
        router.get_store(MemoryScope.PROJECT, "project-b")

        # Project A DOES have overrides.
        results_a = router.search_by_tag(
            OVERRIDE_TAG,
            scopes=[(MemoryScope.PROJECT, "project-a")],
        )
        assert len(results_a) >= 1, "Project A should retain its override"

        # Project B does NOT.
        results_b = router.search_by_tag(
            OVERRIDE_TAG,
            scopes=[(MemoryScope.PROJECT, "project-b")],
        )
        assert len(results_b) == 0, "Project B should have no overrides"

    @pytest.mark.asyncio
    async def test_multi_scope_search_isolates_project_scopes(self, scope_env):
        """Multi-scope search for project B never returns project A data."""
        indexer, router, embedder, vault = scope_env
        from memsearch.scoping import MemoryScope

        # Override in project A.
        override_dir = vault / "projects" / "project-a" / "overrides"
        override_dir.mkdir(parents=True)
        coding_file = override_dir / "coding.md"
        coding_file.write_text(CODING_OVERRIDE)
        await indexer.index_override("project-a", "coding", str(coding_file))

        # Regular memory in project B.
        await _upsert_override_chunk(
            router,
            embedder,
            MemoryScope.PROJECT,
            "project-b",
            content="Project B focuses on mobile development with Flutter.",
            chunk_hash="projb_flutter_001",
            source="/vault/projects/project-b/memory/mobile.md",
            tags=["mobile"],
            heading="Mobile",
        )

        # Search as project B.
        query_embedding = (await embedder.embed(["ECS composition game entity"]))[0]
        results = await router.search(
            query_embedding,
            query_text="ECS composition game entity",
            project_id="project-b",
            agent_type="coding",
            top_k=20,
        )

        # Any project-scoped result must be from project-b, not project-a.
        for r in results:
            if r.get("_scope") == "project":
                assert r.get("_scope_id") == "project-b", (
                    f"Project-scoped result should be from project-b, "
                    f"got scope_id={r.get('_scope_id')}"
                )

    @pytest.mark.asyncio
    async def test_same_agent_type_different_projects_isolated(self, scope_env):
        """Same agent type override in two projects stays isolated."""
        indexer, router, embedder, vault = scope_env
        from memsearch.scoping import MemoryScope

        # Coding override for project A.
        dir_a = vault / "projects" / "proj-alpha" / "overrides"
        dir_a.mkdir(parents=True)
        (dir_a / "coding.md").write_text(
            "# Coding — Alpha\n\nUse composition with ECS framework.\n"
        )
        await indexer.index_override("proj-alpha", "coding", str(dir_a / "coding.md"))

        # Different coding override for project B.
        dir_b = vault / "projects" / "proj-beta" / "overrides"
        dir_b.mkdir(parents=True)
        (dir_b / "coding.md").write_text("# Coding — Beta\n\nUse microservices with gRPC.\n")
        await indexer.index_override("proj-beta", "coding", str(dir_b / "coding.md"))

        # Each project should see only its own coding override.
        results_alpha = router.search_by_tag(
            "coding",
            scopes=[(MemoryScope.PROJECT, "proj-alpha")],
        )
        results_beta = router.search_by_tag(
            "coding",
            scopes=[(MemoryScope.PROJECT, "proj-beta")],
        )

        assert len(results_alpha) >= 1, "proj-alpha should have coding override"
        assert len(results_beta) >= 1, "proj-beta should have coding override"

        # Verify content isolation — alpha has ECS, beta has gRPC.
        alpha_content = " ".join(r.get("content", "") for r in results_alpha)
        beta_content = " ".join(r.get("content", "") for r in results_beta)

        assert "ECS" in alpha_content or "composition" in alpha_content, (
            "Alpha coding override should mention ECS/composition"
        )
        assert "gRPC" in beta_content or "microservices" in beta_content, (
            "Beta coding override should mention gRPC/microservices"
        )

        # Cross-check: alpha should NOT have gRPC content, beta should NOT have ECS.
        assert "gRPC" not in alpha_content, "Alpha should not see Beta's gRPC content"
        assert "ECS" not in beta_content, "Beta should not see Alpha's ECS content"
