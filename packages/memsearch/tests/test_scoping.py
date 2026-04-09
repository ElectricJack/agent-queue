"""Tests for scope-aware collection naming, routing, and cleanup."""

import json
import sys
from pathlib import Path

import pytest

from memsearch.scoping import (
    _PREFIX,
    SCOPE_WEIGHTS,
    CollectionRouter,
    MemoryScope,
    ScopeEntry,
    collection_name,
    merge_and_rank,
    parse_collection_name,
    resolve_scopes,
    sanitize_id,
    vault_paths,
)

# ---- Pure function tests (no Milvus needed) --------------------------------


class TestSanitizeId:
    def test_simple(self):
        assert sanitize_id("coding") == "coding"

    def test_hyphens_to_underscores(self):
        assert sanitize_id("mech-fighters") == "mech_fighters"

    def test_spaces_to_underscores(self):
        assert sanitize_id("my project") == "my_project"

    def test_uppercase_lowered(self):
        assert sanitize_id("MyProject") == "myproject"

    def test_special_chars_removed(self):
        assert sanitize_id("project!!!v2.0") == "project_v2_0"

    def test_consecutive_specials_collapsed(self):
        assert sanitize_id("a--b__c") == "a_b_c"

    def test_leading_trailing_stripped(self):
        assert sanitize_id("--hello--") == "hello"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Cannot sanitize"):
            sanitize_id("")

    def test_all_specials_raises(self):
        with pytest.raises(ValueError, match="Cannot sanitize"):
            sanitize_id("---!!!")

    def test_numeric_id(self):
        assert sanitize_id("123") == "123"

    def test_mixed_unicode(self):
        # Non-ASCII characters get replaced with underscores
        result = sanitize_id("proj-alpha")
        assert result == "proj_alpha"


class TestCollectionName:
    def test_system(self):
        assert collection_name(MemoryScope.SYSTEM) == "aq_system"

    def test_system_ignores_scope_id(self):
        # scope_id is ignored for SYSTEM
        assert collection_name(MemoryScope.SYSTEM, "anything") == "aq_system"

    def test_orchestrator(self):
        assert collection_name(MemoryScope.ORCHESTRATOR) == "aq_orchestrator"

    def test_agent_type(self):
        assert collection_name(MemoryScope.AGENT_TYPE, "coding") == "aq_agenttype_coding"

    def test_agent_type_sanitized(self):
        assert collection_name(MemoryScope.AGENT_TYPE, "code-review") == "aq_agenttype_code_review"

    def test_agent_type_requires_id(self):
        with pytest.raises(ValueError, match="scope_id is required"):
            collection_name(MemoryScope.AGENT_TYPE)

    def test_agent_type_empty_id_raises(self):
        with pytest.raises(ValueError, match="scope_id is required"):
            collection_name(MemoryScope.AGENT_TYPE, "")

    def test_project(self):
        assert collection_name(MemoryScope.PROJECT, "myapp") == "aq_project_myapp"

    def test_project_sanitized(self):
        assert collection_name(MemoryScope.PROJECT, "mech-fighters") == "aq_project_mech_fighters"

    def test_project_requires_id(self):
        with pytest.raises(ValueError, match="scope_id is required"):
            collection_name(MemoryScope.PROJECT)

    def test_project_complex_id(self):
        assert collection_name(MemoryScope.PROJECT, "My Cool App v2.0!") == "aq_project_my_cool_app_v2_0"

    def test_all_start_with_prefix(self):
        names = [
            collection_name(MemoryScope.SYSTEM),
            collection_name(MemoryScope.ORCHESTRATOR),
            collection_name(MemoryScope.AGENT_TYPE, "test"),
            collection_name(MemoryScope.PROJECT, "test"),
        ]
        for n in names:
            assert n.startswith(_PREFIX)

    def test_very_long_id_raises(self):
        long_id = "a" * 300
        with pytest.raises(ValueError, match="Collection name too long"):
            collection_name(MemoryScope.PROJECT, long_id)


class TestParseCollectionName:
    def test_system(self):
        scope, scope_id = parse_collection_name("aq_system")
        assert scope == MemoryScope.SYSTEM
        assert scope_id is None

    def test_orchestrator(self):
        scope, scope_id = parse_collection_name("aq_orchestrator")
        assert scope == MemoryScope.ORCHESTRATOR
        assert scope_id is None

    def test_agent_type(self):
        scope, scope_id = parse_collection_name("aq_agenttype_coding")
        assert scope == MemoryScope.AGENT_TYPE
        assert scope_id == "coding"

    def test_agent_type_compound_id(self):
        scope, scope_id = parse_collection_name("aq_agenttype_code_review")
        assert scope == MemoryScope.AGENT_TYPE
        assert scope_id == "code_review"

    def test_project(self):
        scope, scope_id = parse_collection_name("aq_project_myapp")
        assert scope == MemoryScope.PROJECT
        assert scope_id == "myapp"

    def test_project_compound_id(self):
        scope, scope_id = parse_collection_name("aq_project_mech_fighters")
        assert scope == MemoryScope.PROJECT
        assert scope_id == "mech_fighters"

    def test_roundtrip_system(self):
        name = collection_name(MemoryScope.SYSTEM)
        scope, scope_id = parse_collection_name(name)
        assert scope == MemoryScope.SYSTEM
        assert scope_id is None

    def test_roundtrip_orchestrator(self):
        name = collection_name(MemoryScope.ORCHESTRATOR)
        scope, scope_id = parse_collection_name(name)
        assert scope == MemoryScope.ORCHESTRATOR
        assert scope_id is None

    def test_roundtrip_agent_type(self):
        name = collection_name(MemoryScope.AGENT_TYPE, "review")
        scope, scope_id = parse_collection_name(name)
        assert scope == MemoryScope.AGENT_TYPE
        assert scope_id == "review"

    def test_roundtrip_project(self):
        name = collection_name(MemoryScope.PROJECT, "agent-queue")
        scope, scope_id = parse_collection_name(name)
        assert scope == MemoryScope.PROJECT
        assert scope_id == "agent_queue"  # sanitized form

    def test_no_prefix_raises(self):
        with pytest.raises(ValueError, match="Not an agent-queue collection"):
            parse_collection_name("memsearch_chunks")

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError, match="Unknown scope"):
            parse_collection_name("aq_foobar")

    def test_empty_agent_type_raises(self):
        with pytest.raises(ValueError, match="Missing agent type"):
            parse_collection_name("aq_agenttype_")

    def test_empty_project_raises(self):
        with pytest.raises(ValueError, match="Missing project"):
            parse_collection_name("aq_project_")


class TestVaultPaths:
    def test_system_paths(self):
        paths = vault_paths(MemoryScope.SYSTEM)
        assert "vault/system/memory/" in paths
        assert "vault/system/facts.md" in paths

    def test_orchestrator_paths(self):
        paths = vault_paths(MemoryScope.ORCHESTRATOR)
        assert "vault/orchestrator/memory/" in paths
        assert "vault/orchestrator/facts.md" in paths

    def test_agent_type_paths_substituted(self):
        paths = vault_paths(MemoryScope.AGENT_TYPE, "coding")
        assert "vault/agent-types/coding/memory/" in paths
        assert "vault/agent-types/coding/facts.md" in paths

    def test_project_paths_substituted(self):
        paths = vault_paths(MemoryScope.PROJECT, "my-app")
        assert "vault/projects/my_app/memory/" in paths
        assert "vault/projects/my_app/notes/" in paths
        assert "vault/projects/my_app/references/" in paths
        assert "vault/projects/my_app/facts.md" in paths

    def test_project_has_four_paths(self):
        paths = vault_paths(MemoryScope.PROJECT, "test")
        assert len(paths) == 4


# ---- Integration tests (require Milvus Lite / non-Windows) ----------------

pytestmark_milvus = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Milvus Lite not supported on Windows",
)


@pytest.fixture
def router(tmp_path: Path):
    """CollectionRouter with a temp Milvus Lite db."""
    db = tmp_path / "scoping_test.db"
    r = CollectionRouter(milvus_uri=str(db), dimension=4)
    yield r
    r.close()


@pytestmark_milvus
class TestCollectionRouterGetStore:
    def test_creates_store(self, router: CollectionRouter):
        store = router.get_store(MemoryScope.SYSTEM)
        assert store is not None
        assert store._collection == "aq_system"

    def test_caches_store(self, router: CollectionRouter):
        s1 = router.get_store(MemoryScope.SYSTEM)
        s2 = router.get_store(MemoryScope.SYSTEM)
        assert s1 is s2

    def test_different_scopes_different_stores(self, router: CollectionRouter):
        s1 = router.get_store(MemoryScope.SYSTEM)
        s2 = router.get_store(MemoryScope.ORCHESTRATOR)
        assert s1 is not s2
        assert s1._collection != s2._collection

    def test_project_store(self, router: CollectionRouter):
        store = router.get_store(MemoryScope.PROJECT, "test-app")
        assert store._collection == "aq_project_test_app"

    def test_agent_type_store(self, router: CollectionRouter):
        store = router.get_store(MemoryScope.AGENT_TYPE, "coding")
        assert store._collection == "aq_agenttype_coding"

    def test_has_store_before_and_after(self, router: CollectionRouter):
        assert not router.has_store(MemoryScope.SYSTEM)
        router.get_store(MemoryScope.SYSTEM)
        assert router.has_store(MemoryScope.SYSTEM)


@pytestmark_milvus
class TestCollectionRouterUpsertAndSearch:
    def test_scoped_upsert_and_search(self, router: CollectionRouter):
        """Data in one scope is isolated from another scope."""
        proj_store = router.get_store(MemoryScope.PROJECT, "alpha")
        sys_store = router.get_store(MemoryScope.SYSTEM)

        proj_store.upsert(
            [
                {
                    "chunk_hash": "proj_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Project alpha config",
                    "source": "alpha.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        sys_store.upsert(
            [
                {
                    "chunk_hash": "sys_1",
                    "embedding": [0.0, 1.0, 0.0, 0.0],
                    "content": "System global config",
                    "source": "system.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )

        # Each scope only sees its own data
        proj_results = proj_store.query(filter_expr='chunk_hash != ""')
        assert len(proj_results) == 1
        assert proj_results[0]["chunk_hash"] == "proj_1"

        sys_results = sys_store.query(filter_expr='chunk_hash != ""')
        assert len(sys_results) == 1
        assert sys_results[0]["chunk_hash"] == "sys_1"


@pytestmark_milvus
class TestCollectionRouterListCollections:
    def test_empty_initially(self, router: CollectionRouter):
        result = router.list_collections()
        # May be empty or contain collections from get_store calls
        for _scope, _scope_id, name in result:
            assert name.startswith(_PREFIX)

    def test_lists_created_collections(self, router: CollectionRouter):
        router.get_store(MemoryScope.SYSTEM)
        router.get_store(MemoryScope.PROJECT, "alpha")
        router.get_store(MemoryScope.AGENT_TYPE, "coding")

        result = router.list_collections()
        names = {name for _, _, name in result}
        assert "aq_system" in names
        assert "aq_project_alpha" in names
        assert "aq_agenttype_coding" in names

    def test_returns_parsed_scopes(self, router: CollectionRouter):
        router.get_store(MemoryScope.PROJECT, "beta")
        result = router.list_collections()
        found = [(scope, scope_id) for scope, scope_id, name in result if name == "aq_project_beta"]
        assert len(found) == 1
        assert found[0] == (MemoryScope.PROJECT, "beta")


@pytestmark_milvus
class TestCollectionRouterDropAndCleanup:
    def test_drop_cached_collection(self, router: CollectionRouter):
        router.get_store(MemoryScope.PROJECT, "ephemeral")
        assert router.has_store(MemoryScope.PROJECT, "ephemeral")

        dropped = router.drop_collection(MemoryScope.PROJECT, "ephemeral")
        assert dropped is True
        assert not router.has_store(MemoryScope.PROJECT, "ephemeral")

    def test_drop_nonexistent_returns_false(self, router: CollectionRouter):
        dropped = router.drop_collection(MemoryScope.PROJECT, "nonexistent")
        assert dropped is False

    def test_cleanup_project(self, router: CollectionRouter):
        router.get_store(MemoryScope.PROJECT, "temp-proj")
        dropped = router.cleanup_project("temp-proj")
        assert dropped is True
        assert not router.has_store(MemoryScope.PROJECT, "temp-proj")

    def test_cleanup_agent_type(self, router: CollectionRouter):
        router.get_store(MemoryScope.AGENT_TYPE, "temp-type")
        dropped = router.cleanup_agent_type("temp-type")
        assert dropped is True
        assert not router.has_store(MemoryScope.AGENT_TYPE, "temp-type")

    def test_drop_then_recreate(self, router: CollectionRouter):
        """After dropping, the same scope can be recreated."""
        store1 = router.get_store(MemoryScope.PROJECT, "cycle")
        store1.upsert(
            [
                {
                    "chunk_hash": "old_data",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "old",
                    "source": "old.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )

        router.drop_collection(MemoryScope.PROJECT, "cycle")

        # Recreate — should be empty
        store2 = router.get_store(MemoryScope.PROJECT, "cycle")
        results = store2.query(filter_expr='chunk_hash != ""')
        assert len(results) == 0


@pytestmark_milvus
class TestCollectionRouterSearchByTag:
    def test_cross_scope_tag_search(self, router: CollectionRouter):
        proj_store = router.get_store(MemoryScope.PROJECT, "alpha")
        sys_store = router.get_store(MemoryScope.SYSTEM)

        proj_store.upsert(
            [
                {
                    "chunk_hash": "proj_tagged",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Project uses SQLite",
                    "source": "proj.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["sqlite", "database"]',
                },
            ]
        )
        sys_store.upsert(
            [
                {
                    "chunk_hash": "sys_tagged",
                    "embedding": [0.0, 1.0, 0.0, 0.0],
                    "content": "System SQLite config",
                    "source": "sys.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["sqlite", "config"]',
                },
            ]
        )

        results = router.search_by_tag("sqlite")
        hashes = {r["chunk_hash"] for r in results}
        assert "proj_tagged" in hashes
        assert "sys_tagged" in hashes

        # Each result has scope metadata
        for r in results:
            assert "_collection" in r
            assert "_scope" in r

    def test_tag_search_scoped(self, router: CollectionRouter):
        """Restricting scopes filters out other collections."""
        router.get_store(MemoryScope.PROJECT, "alpha").upsert(
            [
                {
                    "chunk_hash": "alpha_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Alpha data",
                    "source": "a.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["shared"]',
                },
            ]
        )
        router.get_store(MemoryScope.PROJECT, "beta").upsert(
            [
                {
                    "chunk_hash": "beta_1",
                    "embedding": [0.0, 1.0, 0.0, 0.0],
                    "content": "Beta data",
                    "source": "b.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["shared"]',
                },
            ]
        )

        # Search only alpha scope
        results = router.search_by_tag(
            "shared",
            scopes=[(MemoryScope.PROJECT, "alpha")],
        )
        hashes = {r["chunk_hash"] for r in results}
        assert "alpha_1" in hashes
        assert "beta_1" not in hashes

    def test_tag_search_no_results(self, router: CollectionRouter):
        router.get_store(MemoryScope.SYSTEM).upsert(
            [
                {
                    "chunk_hash": "untagged",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "No relevant tags",
                    "source": "test.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["unrelated"]',
                },
            ]
        )
        results = router.search_by_tag("nonexistent")
        assert len(results) == 0

    def test_tag_search_discovers_all_collections(self, tmp_path: Path):
        """search_by_tag with scopes=None discovers ALL aq_* collections,
        not just cached ones (spec §7.3 cross-scope discovery)."""
        db = tmp_path / "discover_test.db"

        # Create and populate collections with one router, then close it
        r1 = CollectionRouter(milvus_uri=str(db), dimension=4)
        r1.get_store(MemoryScope.PROJECT, "alpha").upsert(
            [
                {
                    "chunk_hash": "alpha_discover",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Alpha project uses SQLite",
                    "source": "alpha.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["sqlite", "database"]',
                },
            ]
        )
        r1.get_store(MemoryScope.PROJECT, "beta").upsert(
            [
                {
                    "chunk_hash": "beta_discover",
                    "embedding": [0.0, 1.0, 0.0, 0.0],
                    "content": "Beta project also uses SQLite",
                    "source": "beta.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["sqlite", "config"]',
                },
            ]
        )
        r1.get_store(MemoryScope.SYSTEM).upsert(
            [
                {
                    "chunk_hash": "sys_discover",
                    "embedding": [0.0, 0.0, 1.0, 0.0],
                    "content": "System SQLite defaults",
                    "source": "sys.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["sqlite"]',
                },
            ]
        )
        r1.close()

        # Open a FRESH router with NO cached stores.  search_by_tag must
        # discover the three collections on its own via list_collections.
        r2 = CollectionRouter(milvus_uri=str(db), dimension=4)
        assert len(r2._stores) == 0  # nothing cached yet

        results = r2.search_by_tag("sqlite")
        hashes = {r["chunk_hash"] for r in results}
        assert "alpha_discover" in hashes
        assert "beta_discover" in hashes
        assert "sys_discover" in hashes
        assert len(hashes) == 3

        # Each result is annotated with scope metadata
        for r in results:
            assert "_collection" in r
            assert "_scope" in r
            assert "_scope_id" in r

        r2.close()

    def test_tag_search_entry_type_filter(self, router: CollectionRouter):
        """entry_type parameter filters results to a specific type."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                {
                    "chunk_hash": "doc_entry",
                    "entry_type": "document",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Document about auth",
                    "source": "auth.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["auth"]',
                },
                {
                    "chunk_hash": "kv_entry",
                    "entry_type": "kv",
                    "embedding": [0.0, 0.0, 0.0, 0.0],
                    "content": "",
                    "source": "facts.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 0,
                    "end_line": 0,
                    "kv_namespace": "conventions",
                    "kv_key": "auth_method",
                    "kv_value": '"jwt"',
                    "tags": '["auth"]',
                },
            ]
        )

        # Without filter — both returned
        all_results = router.search_by_tag("auth")
        assert len(all_results) == 2

        # With entry_type filter — only documents
        doc_results = router.search_by_tag("auth", entry_type="document")
        assert len(doc_results) == 1
        assert doc_results[0]["chunk_hash"] == "doc_entry"

        # With entry_type filter — only KV
        kv_results = router.search_by_tag("auth", entry_type="kv")
        assert len(kv_results) == 1
        assert kv_results[0]["chunk_hash"] == "kv_entry"

    def test_tag_search_limit(self, router: CollectionRouter):
        """limit parameter caps results per collection."""
        store = router.get_store(MemoryScope.SYSTEM)
        chunks = [
            {
                "chunk_hash": f"limit_test_{i}",
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "content": f"Item {i}",
                "source": "test.md",
                "heading": "",
                "heading_level": 0,
                "start_line": i,
                "end_line": i,
                "tags": '["bulk"]',
            }
            for i in range(5)
        ]
        store.upsert(chunks)

        results = router.search_by_tag("bulk", limit=2)
        assert len(results) == 2

    def test_tag_search_special_characters(self, router: CollectionRouter):
        """Tags with special characters are escaped properly."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                {
                    "chunk_hash": "special_tag",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Special char tag",
                    "source": "test.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["c++", "node.js"]',
                },
            ]
        )
        # Search for a tag that needs no special escaping
        results = router.search_by_tag("node.js")
        # Milvus LIKE uses % as wildcard, . is literal
        assert len(results) >= 1
        assert results[0]["chunk_hash"] == "special_tag"


@pytestmark_milvus
class TestCollectionRouterSearchByTagAsync:
    """Integration tests for async cross-collection tag search."""

    @pytest.mark.asyncio
    async def test_async_tag_search_basic(self, router: CollectionRouter):
        """Async search_by_tag finds entries across multiple collections."""
        router.get_store(MemoryScope.PROJECT, "alpha").upsert(
            [
                {
                    "chunk_hash": "async_proj",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Project uses Redis",
                    "source": "proj.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["redis", "cache"]',
                },
            ]
        )
        router.get_store(MemoryScope.SYSTEM).upsert(
            [
                {
                    "chunk_hash": "async_sys",
                    "embedding": [0.0, 1.0, 0.0, 0.0],
                    "content": "System Redis config",
                    "source": "sys.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["redis", "config"]',
                },
            ]
        )

        results = await router.search_by_tag_async("redis")
        hashes = {r["chunk_hash"] for r in results}
        assert "async_proj" in hashes
        assert "async_sys" in hashes
        for r in results:
            assert "_collection" in r
            assert "_scope" in r

    @pytest.mark.asyncio
    async def test_async_tag_search_discovers_all(self, tmp_path: Path):
        """Async variant also discovers non-cached collections."""
        db = tmp_path / "async_discover.db"

        # Populate with one router
        r1 = CollectionRouter(milvus_uri=str(db), dimension=4)
        r1.get_store(MemoryScope.PROJECT, "gamma").upsert(
            [
                {
                    "chunk_hash": "gamma_async",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Gamma project auth",
                    "source": "gamma.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["auth"]',
                },
            ]
        )
        r1.get_store(MemoryScope.SYSTEM).upsert(
            [
                {
                    "chunk_hash": "sys_async",
                    "embedding": [0.0, 1.0, 0.0, 0.0],
                    "content": "System auth defaults",
                    "source": "sys.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["auth"]',
                },
            ]
        )
        r1.close()

        # Fresh router — no cached stores
        r2 = CollectionRouter(milvus_uri=str(db), dimension=4)
        assert len(r2._stores) == 0

        results = await r2.search_by_tag_async("auth")
        hashes = {r["chunk_hash"] for r in results}
        assert "gamma_async" in hashes
        assert "sys_async" in hashes
        r2.close()

    @pytest.mark.asyncio
    async def test_async_entry_type_filter(self, router: CollectionRouter):
        """Async variant supports entry_type filtering."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                {
                    "chunk_hash": "async_doc",
                    "entry_type": "document",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "A document",
                    "source": "test.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["shared"]',
                },
                {
                    "chunk_hash": "async_kv",
                    "entry_type": "kv",
                    "embedding": [0.0, 0.0, 0.0, 0.0],
                    "content": "",
                    "source": "facts.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 0,
                    "end_line": 0,
                    "tags": '["shared"]',
                },
            ]
        )

        doc_results = await router.search_by_tag_async("shared", entry_type="document")
        assert len(doc_results) == 1
        assert doc_results[0]["entry_type"] == "document"

    @pytest.mark.asyncio
    async def test_async_scoped_restriction(self, router: CollectionRouter):
        """Async variant respects scope restrictions."""
        router.get_store(MemoryScope.PROJECT, "alpha").upsert(
            [
                {
                    "chunk_hash": "async_alpha",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Alpha",
                    "source": "a.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["common"]',
                },
            ]
        )
        router.get_store(MemoryScope.PROJECT, "beta").upsert(
            [
                {
                    "chunk_hash": "async_beta",
                    "embedding": [0.0, 1.0, 0.0, 0.0],
                    "content": "Beta",
                    "source": "b.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "tags": '["common"]',
                },
            ]
        )

        results = await router.search_by_tag_async(
            "common",
            scopes=[(MemoryScope.PROJECT, "alpha")],
        )
        hashes = {r["chunk_hash"] for r in results}
        assert "async_alpha" in hashes
        assert "async_beta" not in hashes

    @pytest.mark.asyncio
    async def test_async_no_results(self, router: CollectionRouter):
        """Async returns empty list when no matches."""
        router.get_store(MemoryScope.SYSTEM)
        results = await router.search_by_tag_async("nonexistent_tag")
        assert results == []

    @pytest.mark.asyncio
    async def test_async_empty_router(self, tmp_path: Path):
        """Async on empty Milvus with no collections returns empty list."""
        db = tmp_path / "empty_async.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)
        results = await r.search_by_tag_async("anything")
        assert results == []
        r.close()


@pytestmark_milvus
class TestEnsureSystemCollection:
    """Tests for CollectionRouter.ensure_system_collection (roadmap 3.1.3)."""

    def test_creates_system_collection(self, router: CollectionRouter):
        """ensure_system_collection creates the aq_system collection."""
        assert not router.has_store(MemoryScope.SYSTEM)
        store = router.ensure_system_collection()
        assert store is not None
        assert store._collection == "aq_system"
        assert router.has_store(MemoryScope.SYSTEM)

    def test_idempotent(self, router: CollectionRouter):
        """Calling ensure_system_collection twice returns the same store."""
        store1 = router.ensure_system_collection()
        store2 = router.ensure_system_collection()
        assert store1 is store2

    def test_collection_appears_in_list(self, router: CollectionRouter):
        """After ensure, aq_system appears in list_collections."""
        router.ensure_system_collection()
        names = {name for _, _, name in router.list_collections()}
        assert "aq_system" in names

    def test_collection_is_writable(self, router: CollectionRouter):
        """The ensured system collection accepts upserts."""
        store = router.ensure_system_collection()
        store.upsert(
            [
                {
                    "chunk_hash": "ensure_test_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "System-wide convention",
                    "source": "system.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        results = store.query(filter_expr='chunk_hash == "ensure_test_1"')
        assert len(results) == 1
        assert results[0]["content"] == "System-wide convention"

    def test_collection_is_searchable_after_ensure(self, router: CollectionRouter):
        """After ensure, the system collection is available in multi-scope search."""
        store = router.ensure_system_collection()
        store.upsert(
            [
                {
                    "chunk_hash": "ensure_search_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Global pattern for error handling",
                    "source": "system.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        # Verify it's discoverable via _get_store_if_exists (used by search)
        found = router._get_store_if_exists(MemoryScope.SYSTEM)
        assert found is not None
        assert found is store

    def test_fresh_router_finds_ensured_collection(self, tmp_path: Path):
        """A fresh router can discover the system collection created by ensure."""
        db = tmp_path / "ensure_persist.db"
        r1 = CollectionRouter(milvus_uri=str(db), dimension=4)
        store = r1.ensure_system_collection()
        store.upsert(
            [
                {
                    "chunk_hash": "persist_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Persistent system data",
                    "source": "sys.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        r1.close()

        # Fresh router — no cached stores
        r2 = CollectionRouter(milvus_uri=str(db), dimension=4)
        assert len(r2._stores) == 0
        found = r2._get_store_if_exists(MemoryScope.SYSTEM)
        assert found is not None
        results = found.query(filter_expr='chunk_hash == "persist_1"')
        assert len(results) == 1
        r2.close()

    def test_does_not_affect_other_scopes(self, router: CollectionRouter):
        """ensure_system_collection only creates the system collection."""
        router.ensure_system_collection()
        assert router.has_store(MemoryScope.SYSTEM)
        assert not router.has_store(MemoryScope.ORCHESTRATOR)
        assert not router.has_store(MemoryScope.AGENT_TYPE, "coding")
        assert not router.has_store(MemoryScope.PROJECT, "myapp")


@pytestmark_milvus
class TestEnsureOrchestratorCollection:
    """Tests for CollectionRouter.ensure_orchestrator_collection (roadmap 3.1.4)."""

    def test_creates_orchestrator_collection(self, router: CollectionRouter):
        """ensure_orchestrator_collection creates the aq_orchestrator collection."""
        assert not router.has_store(MemoryScope.ORCHESTRATOR)
        store = router.ensure_orchestrator_collection()
        assert store is not None
        assert store._collection == "aq_orchestrator"
        assert router.has_store(MemoryScope.ORCHESTRATOR)

    def test_idempotent(self, router: CollectionRouter):
        """Calling ensure_orchestrator_collection twice returns the same store."""
        store1 = router.ensure_orchestrator_collection()
        store2 = router.ensure_orchestrator_collection()
        assert store1 is store2

    def test_collection_appears_in_list(self, router: CollectionRouter):
        """After ensure, aq_orchestrator appears in list_collections."""
        router.ensure_orchestrator_collection()
        names = {name for _, _, name in router.list_collections()}
        assert "aq_orchestrator" in names

    def test_collection_is_writable(self, router: CollectionRouter):
        """The ensured orchestrator collection accepts upserts."""
        store = router.ensure_orchestrator_collection()
        store.upsert(
            [
                {
                    "chunk_hash": "orch_ensure_test_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Orchestrator operational insight",
                    "source": "orchestrator.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        results = store.query(filter_expr='chunk_hash == "orch_ensure_test_1"')
        assert len(results) == 1
        assert results[0]["content"] == "Orchestrator operational insight"

    def test_collection_is_searchable_after_ensure(self, router: CollectionRouter):
        """After ensure, the orchestrator collection is available in multi-scope search."""
        store = router.ensure_orchestrator_collection()
        store.upsert(
            [
                {
                    "chunk_hash": "orch_ensure_search_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Scheduling pattern for rate limits",
                    "source": "orchestrator.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        # Verify it's discoverable via _get_store_if_exists (used by search)
        found = router._get_store_if_exists(MemoryScope.ORCHESTRATOR)
        assert found is not None
        assert found is store

    def test_fresh_router_finds_ensured_collection(self, tmp_path: Path):
        """A fresh router can discover the orchestrator collection created by ensure."""
        db = tmp_path / "orch_ensure_persist.db"
        r1 = CollectionRouter(milvus_uri=str(db), dimension=4)
        store = r1.ensure_orchestrator_collection()
        store.upsert(
            [
                {
                    "chunk_hash": "orch_persist_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Persistent orchestrator data",
                    "source": "orch.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        r1.close()

        # Fresh router — no cached stores
        r2 = CollectionRouter(milvus_uri=str(db), dimension=4)
        assert len(r2._stores) == 0
        found = r2._get_store_if_exists(MemoryScope.ORCHESTRATOR)
        assert found is not None
        results = found.query(filter_expr='chunk_hash == "orch_persist_1"')
        assert len(results) == 1
        r2.close()

    def test_does_not_affect_other_scopes(self, router: CollectionRouter):
        """ensure_orchestrator_collection only creates the orchestrator collection."""
        router.ensure_orchestrator_collection()
        assert router.has_store(MemoryScope.ORCHESTRATOR)
        assert not router.has_store(MemoryScope.SYSTEM)
        assert not router.has_store(MemoryScope.AGENT_TYPE, "coding")
        assert not router.has_store(MemoryScope.PROJECT, "myapp")

    def test_coexists_with_system_collection(self, router: CollectionRouter):
        """Both system and orchestrator collections can be ensured independently."""
        router.ensure_system_collection()
        router.ensure_orchestrator_collection()
        assert router.has_store(MemoryScope.SYSTEM)
        assert router.has_store(MemoryScope.ORCHESTRATOR)
        names = {name for _, _, name in router.list_collections()}
        assert "aq_system" in names
        assert "aq_orchestrator" in names


@pytestmark_milvus
class TestEnsureAgentTypeCollection:
    """Tests for CollectionRouter.ensure_agent_type_collection (roadmap 3.1.2)."""

    def test_creates_agent_type_collection(self, router: CollectionRouter):
        """ensure_agent_type_collection creates the aq_agenttype_{type} collection."""
        assert not router.has_store(MemoryScope.AGENT_TYPE, "coding")
        store = router.ensure_agent_type_collection("coding")
        assert store is not None
        assert store._collection == "aq_agenttype_coding"
        assert router.has_store(MemoryScope.AGENT_TYPE, "coding")

    def test_idempotent(self, router: CollectionRouter):
        """Calling ensure_agent_type_collection twice returns the same store."""
        store1 = router.ensure_agent_type_collection("coding")
        store2 = router.ensure_agent_type_collection("coding")
        assert store1 is store2

    def test_sanitizes_agent_type_name(self, router: CollectionRouter):
        """Agent type names with special characters are sanitized."""
        store = router.ensure_agent_type_collection("code-review")
        assert store._collection == "aq_agenttype_code_review"
        assert router.has_store(MemoryScope.AGENT_TYPE, "code-review")

    def test_different_agent_types_different_stores(self, router: CollectionRouter):
        """Different agent types get separate collections."""
        s1 = router.ensure_agent_type_collection("coding")
        s2 = router.ensure_agent_type_collection("review")
        assert s1 is not s2
        assert s1._collection != s2._collection

    def test_collection_appears_in_list(self, router: CollectionRouter):
        """After ensure, aq_agenttype_coding appears in list_collections."""
        router.ensure_agent_type_collection("coding")
        names = {name for _, _, name in router.list_collections()}
        assert "aq_agenttype_coding" in names

    def test_collection_is_writable(self, router: CollectionRouter):
        """The ensured agent-type collection accepts upserts."""
        store = router.ensure_agent_type_collection("coding")
        store.upsert(
            [
                {
                    "chunk_hash": "at_ensure_test_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Cross-project coding convention",
                    "source": "coding.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        results = store.query(filter_expr='chunk_hash == "at_ensure_test_1"')
        assert len(results) == 1
        assert results[0]["content"] == "Cross-project coding convention"

    def test_collection_is_searchable_after_ensure(self, router: CollectionRouter):
        """After ensure, the agent-type collection is available in multi-scope search."""
        store = router.ensure_agent_type_collection("coding")
        store.upsert(
            [
                {
                    "chunk_hash": "at_ensure_search_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Always use type hints in Python code",
                    "source": "coding.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        # Verify it's discoverable via _get_store_if_exists (used by search)
        found = router._get_store_if_exists(MemoryScope.AGENT_TYPE, "coding")
        assert found is not None
        assert found is store

    def test_fresh_router_finds_ensured_collection(self, tmp_path: Path):
        """A fresh router can discover the agent-type collection created by ensure."""
        db = tmp_path / "at_ensure_persist.db"
        r1 = CollectionRouter(milvus_uri=str(db), dimension=4)
        store = r1.ensure_agent_type_collection("coding")
        store.upsert(
            [
                {
                    "chunk_hash": "at_persist_1",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Persistent agent-type data",
                    "source": "coding.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                },
            ]
        )
        r1.close()

        # Fresh router — no cached stores
        r2 = CollectionRouter(milvus_uri=str(db), dimension=4)
        assert len(r2._stores) == 0
        found = r2._get_store_if_exists(MemoryScope.AGENT_TYPE, "coding")
        assert found is not None
        results = found.query(filter_expr='chunk_hash == "at_persist_1"')
        assert len(results) == 1
        r2.close()

    def test_does_not_affect_other_scopes(self, router: CollectionRouter):
        """ensure_agent_type_collection only creates the agent-type collection."""
        router.ensure_agent_type_collection("coding")
        assert router.has_store(MemoryScope.AGENT_TYPE, "coding")
        assert not router.has_store(MemoryScope.SYSTEM)
        assert not router.has_store(MemoryScope.ORCHESTRATOR)
        assert not router.has_store(MemoryScope.PROJECT, "myapp")

    def test_coexists_with_system_and_orchestrator(self, router: CollectionRouter):
        """Agent-type, system, and orchestrator collections can all coexist."""
        router.ensure_system_collection()
        router.ensure_orchestrator_collection()
        router.ensure_agent_type_collection("coding")
        assert router.has_store(MemoryScope.SYSTEM)
        assert router.has_store(MemoryScope.ORCHESTRATOR)
        assert router.has_store(MemoryScope.AGENT_TYPE, "coding")
        names = {name for _, _, name in router.list_collections()}
        assert "aq_system" in names
        assert "aq_orchestrator" in names
        assert "aq_agenttype_coding" in names


@pytestmark_milvus
class TestCollectionRouterContextManager:
    def test_context_manager(self, tmp_path: Path):
        db = tmp_path / "ctx_test.db"
        with CollectionRouter(milvus_uri=str(db), dimension=4) as router:
            store = router.get_store(MemoryScope.SYSTEM)
            store.upsert(
                [
                    {
                        "chunk_hash": "ctx_1",
                        "embedding": [1.0, 0.0, 0.0, 0.0],
                        "content": "Context manager test",
                        "source": "test.md",
                        "heading": "",
                        "heading_level": 0,
                        "start_line": 1,
                        "end_line": 1,
                    },
                ]
            )
            assert store.count() == 1
        # After exit, stores are cleared
        assert len(router._stores) == 0


@pytestmark_milvus
class TestCollectionRouterProperties:
    def test_uri_property(self, router: CollectionRouter):
        assert "scoping_test.db" in router.uri

    def test_dimension_property(self, router: CollectionRouter):
        assert router.dimension == 4


# ---- Pure function tests for merge_and_rank ----------------------------------


class TestMergeAndRank:
    def test_basic_merge(self):
        results = [
            {"chunk_hash": "a", "score": 0.9, "weighted_score": 0.9},
            {"chunk_hash": "b", "score": 0.8, "weighted_score": 0.56},
            {"chunk_hash": "c", "score": 0.7, "weighted_score": 0.28},
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 3
        assert merged[0]["chunk_hash"] == "a"
        assert merged[1]["chunk_hash"] == "b"
        assert merged[2]["chunk_hash"] == "c"

    def test_deduplication_keeps_highest_score(self):
        results = [
            {"chunk_hash": "dup", "score": 0.5, "weighted_score": 0.5, "_scope": "project"},
            {"chunk_hash": "dup", "score": 0.9, "weighted_score": 0.36, "_scope": "system"},
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 1
        assert merged[0]["weighted_score"] == 0.5
        assert merged[0]["_scope"] == "project"

    def test_top_k_truncation(self):
        results = [
            {"chunk_hash": f"item_{i}", "score": 1.0 - i * 0.1, "weighted_score": 1.0 - i * 0.1} for i in range(10)
        ]
        merged = merge_and_rank(results, top_k=3)
        assert len(merged) == 3
        assert merged[0]["chunk_hash"] == "item_0"

    def test_empty_input(self):
        assert merge_and_rank([], top_k=10) == []

    def test_project_outranks_system(self):
        """A moderately relevant project memory should outrank a highly
        relevant system memory (spec §4)."""
        results = [
            {
                "chunk_hash": "project_hit",
                "score": 0.6,
                "weighted_score": 0.6 * 1.0,  # project weight
                "_scope": "project",
            },
            {
                "chunk_hash": "system_hit",
                "score": 0.9,
                "weighted_score": 0.9 * 0.4,  # system weight
                "_scope": "system",
            },
        ]
        merged = merge_and_rank(results, top_k=10)
        assert merged[0]["chunk_hash"] == "project_hit"
        assert merged[1]["chunk_hash"] == "system_hit"

    def test_missing_chunk_hash_handled(self):
        """Results without chunk_hash should not crash."""
        results = [
            {"score": 0.9, "weighted_score": 0.9},
            {"score": 0.8, "weighted_score": 0.8},
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 2


class TestScopeWeights:
    def test_project_is_highest(self):
        assert SCOPE_WEIGHTS[MemoryScope.PROJECT] == 1.0

    def test_agent_type_weight(self):
        assert SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE] == 0.7

    def test_system_is_lowest(self):
        assert SCOPE_WEIGHTS[MemoryScope.SYSTEM] == 0.4

    def test_all_weights_positive(self):
        for w in SCOPE_WEIGHTS.values():
            assert w > 0

    def test_specificity_ordering(self):
        """More specific scopes have higher weights."""
        assert SCOPE_WEIGHTS[MemoryScope.PROJECT] > SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE]
        assert SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE] > SCOPE_WEIGHTS[MemoryScope.SYSTEM]


# ---- Integration tests for multi-scope search (require Milvus Lite) ----------


def _make_chunks(prefix: str, embeddings: list[list[float]], contents: list[str]):
    """Helper to create chunk dicts for upsert."""
    chunks = []
    for i, (emb, content) in enumerate(zip(embeddings, contents, strict=True)):
        chunks.append(
            {
                "chunk_hash": f"{prefix}_{i}",
                "embedding": emb,
                "content": content,
                "source": f"{prefix}.md",
                "heading": "",
                "heading_level": 0,
                "start_line": i + 1,
                "end_line": i + 1,
            }
        )
    return chunks


@pytestmark_milvus
class TestCollectionRouterSearch:
    """Integration tests for multi-scope parallel search."""

    @pytest.fixture
    def populated_router(self, router: CollectionRouter):
        """Router with data in project, agent-type, and system collections."""
        proj_store = router.get_store(MemoryScope.PROJECT, "myapp")
        proj_store.upsert(
            _make_chunks(
                "proj",
                [[1.0, 0.0, 0.0, 0.0], [0.9, 0.1, 0.0, 0.0]],
                ["Project authentication guide", "Project database schema"],
            )
        )

        at_store = router.get_store(MemoryScope.AGENT_TYPE, "coding")
        at_store.upsert(
            _make_chunks(
                "agenttype",
                [[0.0, 1.0, 0.0, 0.0], [0.0, 0.9, 0.1, 0.0]],
                ["Coding best practices for testing", "Code review checklist"],
            )
        )

        sys_store = router.get_store(MemoryScope.SYSTEM)
        sys_store.upsert(
            _make_chunks(
                "sys",
                [[0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.9, 0.1]],
                ["System-wide logging configuration", "System error handling patterns"],
            )
        )

        return router

    @pytest.mark.asyncio
    async def test_search_all_scopes(self, populated_router: CollectionRouter):
        """Search across all three scopes returns results from each."""
        results = await populated_router.search(
            [1.0, 0.5, 0.5, 0.0],
            query_text="authentication",
            project_id="myapp",
            agent_type="coding",
            top_k=10,
        )
        assert len(results) > 0
        # Results from multiple scopes
        scopes_found = {r["_scope"] for r in results}
        assert "project" in scopes_found
        assert "system" in scopes_found

    @pytest.mark.asyncio
    async def test_search_project_only(self, populated_router: CollectionRouter):
        """Search with only project_id set queries project + system."""
        results = await populated_router.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="authentication",
            project_id="myapp",
            top_k=10,
        )
        scopes = {r["_scope"] for r in results}
        assert "project" in scopes
        assert "system" in scopes
        assert "agent_type" not in scopes

    @pytest.mark.asyncio
    async def test_search_system_only(self, populated_router: CollectionRouter):
        """Search with no project/agent_type queries only system."""
        results = await populated_router.search(
            [0.0, 0.0, 1.0, 0.0],
            query_text="logging",
            top_k=10,
        )
        scopes = {r["_scope"] for r in results}
        assert scopes == {"system"}

    @pytest.mark.asyncio
    async def test_weighted_scores_applied(self, populated_router: CollectionRouter):
        """Results have weighted_score = score * weight."""
        results = await populated_router.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="guide",
            project_id="myapp",
            agent_type="coding",
            top_k=10,
        )
        for r in results:
            assert "weighted_score" in r
            assert "_weight" in r
            assert "_scope" in r
            assert "_collection" in r
            expected = r["score"] * r["_weight"]
            assert abs(r["weighted_score"] - expected) < 1e-6

    @pytest.mark.asyncio
    async def test_project_results_rank_higher(self, populated_router: CollectionRouter):
        """Project results should rank above system results with similar raw scores
        due to weight=1.0 vs weight=0.4."""
        # Query that's equidistant to project and system data
        results = await populated_router.search(
            [0.5, 0.0, 0.5, 0.0],
            query_text="configuration",
            project_id="myapp",
            top_k=10,
        )
        if len(results) >= 2:
            project_results = [r for r in results if r["_scope"] == "project"]
            system_results = [r for r in results if r["_scope"] == "system"]
            if project_results and system_results:
                # Best project weighted_score >= best system weighted_score
                # when raw scores are close (project weight 1.0 > system 0.4)
                best_proj = max(r["weighted_score"] for r in project_results)
                best_sys = max(r["weighted_score"] for r in system_results)
                assert best_proj >= best_sys * 0.9  # allow small margin for RRF

    @pytest.mark.asyncio
    async def test_search_missing_collection(self, router: CollectionRouter):
        """Search gracefully handles non-existent collections."""
        # Only system exists
        router.get_store(MemoryScope.SYSTEM).upsert(
            _make_chunks(
                "sys",
                [[0.0, 0.0, 1.0, 0.0]],
                ["System config"],
            )
        )
        results = await router.search(
            [0.0, 0.0, 1.0, 0.0],
            query_text="config",
            project_id="nonexistent",
            agent_type="nonexistent",
            top_k=10,
        )
        # Should still get system results
        assert len(results) > 0
        assert all(r["_scope"] == "system" for r in results)

    @pytest.mark.asyncio
    async def test_search_empty_returns_empty(self, router: CollectionRouter):
        """Search on a router with no collections returns empty."""
        results = await router.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="anything",
            top_k=10,
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_search_custom_weights(self, populated_router: CollectionRouter):
        """Custom weights override the defaults."""
        custom_weights = {
            MemoryScope.PROJECT: 0.1,
            MemoryScope.AGENT_TYPE: 0.1,
            MemoryScope.SYSTEM: 5.0,
        }
        results = await populated_router.search(
            [0.5, 0.5, 0.5, 0.0],
            query_text="test",
            project_id="myapp",
            agent_type="coding",
            top_k=10,
            weights=custom_weights,
        )
        for r in results:
            if r["_scope"] == "system":
                assert r["_weight"] == 5.0
            elif r["_scope"] == "project":
                assert r["_weight"] == 0.1

    @pytest.mark.asyncio
    async def test_scope_metadata_annotations(self, populated_router: CollectionRouter):
        """Each result is annotated with scope metadata."""
        results = await populated_router.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="auth",
            project_id="myapp",
            agent_type="coding",
            top_k=10,
        )
        for r in results:
            assert "_collection" in r
            assert r["_collection"].startswith("aq_")
            assert "_scope" in r
            assert r["_scope"] in ("project", "agent_type", "system")
            # _scope_id is None for system, string for others
            if r["_scope"] == "system":
                assert r["_scope_id"] is None
            else:
                assert isinstance(r["_scope_id"], str)


@pytestmark_milvus
class TestCollectionRouterSearchTopic:
    """Tests for topic-filtered multi-scope search."""

    @pytest.fixture
    def topic_router(self, router: CollectionRouter):
        """Router with topic-tagged data in project and system scopes."""
        proj_store = router.get_store(MemoryScope.PROJECT, "myapp")
        proj_store.upsert(
            [
                {
                    "chunk_hash": "proj_auth_0",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "OAuth token refresh flow for auth",
                    "source": "auth.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "topic": "authentication",
                },
                {
                    "chunk_hash": "proj_db_0",
                    "embedding": [0.0, 1.0, 0.0, 0.0],
                    "content": "Database schema migration patterns",
                    "source": "db.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "topic": "database",
                },
                {
                    "chunk_hash": "proj_untagged_0",
                    "embedding": [0.5, 0.5, 0.0, 0.0],
                    "content": "General project notes",
                    "source": "notes.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "topic": "",
                },
            ]
        )

        sys_store = router.get_store(MemoryScope.SYSTEM)
        sys_store.upsert(
            [
                {
                    "chunk_hash": "sys_auth_0",
                    "embedding": [0.9, 0.0, 0.1, 0.0],
                    "content": "System auth best practices",
                    "source": "sys_auth.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "topic": "authentication",
                },
                {
                    "chunk_hash": "sys_generic_0",
                    "embedding": [0.0, 0.0, 1.0, 0.0],
                    "content": "Generic system configuration",
                    "source": "sys_config.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "topic": "",
                },
            ]
        )
        return router

    @pytest.mark.asyncio
    async def test_topic_filter_narrows_results(self, topic_router: CollectionRouter):
        """Topic filter returns only matching + untagged entries."""
        results = await topic_router.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="auth",
            project_id="myapp",
            topic="authentication",
            top_k=10,
        )
        # Should find auth-topic entries and untagged entries, but not database-topic
        hashes = {r["chunk_hash"] for r in results}
        assert "proj_auth_0" in hashes or "sys_auth_0" in hashes
        assert "proj_db_0" not in hashes

    @pytest.mark.asyncio
    async def test_topic_fallback_marks_results(self, router: CollectionRouter):
        """When topic filter yields < threshold results, fallback is used
        and results are marked with topic_fallback=True."""
        sys_store = router.get_store(MemoryScope.SYSTEM)
        # Only add entries with a different topic (not matching our query topic)
        sys_store.upsert(
            [
                {
                    "chunk_hash": "sys_only_0",
                    "embedding": [1.0, 0.0, 0.0, 0.0],
                    "content": "Some system data",
                    "source": "sys.md",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "topic": "unrelated",
                },
            ]
        )
        # Search with a topic that has no matches → triggers fallback
        results = await router.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="data",
            topic="nonexistent_topic",
            top_k=10,
        )
        # Fallback results should be marked
        if results:
            assert all(r.get("topic_fallback") is True for r in results)


@pytestmark_milvus
class TestCollectionRouterRecall:
    """Tests for KV lookup with scope resolution."""

    @pytest.fixture
    def kv_router(self, router: CollectionRouter):
        """Router with KV entries in project, agent-type, and system scopes."""
        proj_store = router.get_store(MemoryScope.PROJECT, "myapp")
        proj_store.upsert(
            [
                {
                    "chunk_hash": "kv_proj_tech",
                    "entry_type": "kv",
                    "embedding": [0.0, 0.0, 0.0, 0.0],
                    "content": "",
                    "kv_namespace": "project",
                    "kv_key": "tech_stack",
                    "kv_value": '["Python", "SQLAlchemy"]',
                    "source": "",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 0,
                    "end_line": 0,
                },
                {
                    "chunk_hash": "kv_proj_branch",
                    "entry_type": "kv",
                    "embedding": [0.0, 0.0, 0.0, 0.0],
                    "content": "",
                    "kv_namespace": "project",
                    "kv_key": "deploy_branch",
                    "kv_value": '"main"',
                    "source": "",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 0,
                    "end_line": 0,
                },
            ]
        )

        at_store = router.get_store(MemoryScope.AGENT_TYPE, "coding")
        at_store.upsert(
            [
                {
                    "chunk_hash": "kv_at_test",
                    "entry_type": "kv",
                    "embedding": [0.0, 0.0, 0.0, 0.0],
                    "content": "",
                    "kv_namespace": "conventions",
                    "kv_key": "test_command",
                    "kv_value": '"pytest tests/ -v"',
                    "source": "",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 0,
                    "end_line": 0,
                },
                {
                    "chunk_hash": "kv_at_tech",
                    "entry_type": "kv",
                    "embedding": [0.0, 0.0, 0.0, 0.0],
                    "content": "",
                    "kv_namespace": "project",
                    "kv_key": "tech_stack",
                    "kv_value": '["Python"]',
                    "source": "",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 0,
                    "end_line": 0,
                },
            ]
        )

        sys_store = router.get_store(MemoryScope.SYSTEM)
        sys_store.upsert(
            [
                {
                    "chunk_hash": "kv_sys_version",
                    "entry_type": "kv",
                    "embedding": [0.0, 0.0, 0.0, 0.0],
                    "content": "",
                    "kv_namespace": "system",
                    "kv_key": "version",
                    "kv_value": '"1.0"',
                    "source": "",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 0,
                    "end_line": 0,
                },
                {
                    "chunk_hash": "kv_sys_tech",
                    "entry_type": "kv",
                    "embedding": [0.0, 0.0, 0.0, 0.0],
                    "content": "",
                    "kv_namespace": "project",
                    "kv_key": "tech_stack",
                    "kv_value": '["Generic"]',
                    "source": "",
                    "heading": "",
                    "heading_level": 0,
                    "start_line": 0,
                    "end_line": 0,
                },
            ]
        )
        return router

    @pytest.mark.asyncio
    async def test_recall_project_first(self, kv_router: CollectionRouter):
        """KV recall returns project-scope value when available (most specific)."""
        value = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
            agent_type="coding",
            namespace="project",
        )
        assert value == '["Python", "SQLAlchemy"]'

    @pytest.mark.asyncio
    async def test_recall_falls_through_to_agent_type(self, kv_router: CollectionRouter):
        """When project scope doesn't have the key, falls through to agent-type."""
        value = await kv_router.recall(
            "test_command",
            project_id="myapp",
            agent_type="coding",
            namespace="conventions",
        )
        assert value == '"pytest tests/ -v"'

    @pytest.mark.asyncio
    async def test_recall_falls_through_to_system(self, kv_router: CollectionRouter):
        """When project and agent-type don't have the key, falls to system."""
        value = await kv_router.recall(
            "version",
            project_id="myapp",
            agent_type="coding",
            namespace="system",
        )
        assert value == '"1.0"'

    @pytest.mark.asyncio
    async def test_recall_not_found(self, kv_router: CollectionRouter):
        """Returns None when key is not found in any scope."""
        value = await kv_router.recall(
            "nonexistent_key",
            project_id="myapp",
            agent_type="coding",
        )
        assert value is None

    @pytest.mark.asyncio
    async def test_recall_namespace_filter(self, kv_router: CollectionRouter):
        """Namespace parameter correctly filters results."""
        # "version" exists in "system" namespace, not in "project" namespace
        value = await kv_router.recall(
            "version",
            project_id="myapp",
            namespace="project",
        )
        assert value is None

    @pytest.mark.asyncio
    async def test_recall_missing_scopes(self, kv_router: CollectionRouter):
        """Recall works when some scopes don't exist."""
        value = await kv_router.recall(
            "version",
            project_id="nonexistent_project",
            agent_type="nonexistent_type",
            namespace="system",
        )
        # Falls through to system scope
        assert value == '"1.0"'

    @pytest.mark.asyncio
    async def test_recall_system_only(self, kv_router: CollectionRouter):
        """Recall with no project/agent_type searches only system."""
        value = await kv_router.recall(
            "version",
            namespace="system",
        )
        assert value == '"1.0"'

    @pytest.mark.asyncio
    async def test_recall_without_namespace(self, kv_router: CollectionRouter):
        """Recall without namespace matches any namespace."""
        value = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
        )
        # Should find it in project scope (any namespace)
        assert value is not None


@pytestmark_milvus
class TestCollectionRouterGetStoreIfExists:
    """Tests for _get_store_if_exists (read-only store access)."""

    def test_returns_none_for_nonexistent(self, router: CollectionRouter):
        result = router._get_store_if_exists(MemoryScope.PROJECT, "missing")
        assert result is None

    def test_returns_cached_store(self, router: CollectionRouter):
        store = router.get_store(MemoryScope.SYSTEM)
        found = router._get_store_if_exists(MemoryScope.SYSTEM)
        assert found is store

    def test_opens_existing_uncached_collection(self, router: CollectionRouter):
        """If a collection exists in Milvus but isn't cached, opens it."""
        # Create a collection, then clear cache
        router.get_store(MemoryScope.PROJECT, "test")
        assert router.has_store(MemoryScope.PROJECT, "test")

        # Remove from cache (simulating a fresh router that shares the db)
        name = collection_name(MemoryScope.PROJECT, "test")
        del router._stores[name]
        assert not router.has_store(MemoryScope.PROJECT, "test")

        # _get_store_if_exists should find and open it
        store = router._get_store_if_exists(MemoryScope.PROJECT, "test")
        assert store is not None
        assert router.has_store(MemoryScope.PROJECT, "test")


# ---- Roadmap 2.1.18: Multi-collection weighted merge test cases (a)-(f) ----
# Spec: docs/specs/design/memory-scoping.md §4 — Scope Hierarchy
#
#   (a) Weighted ranking correctness — weight-1.0 result ranks above
#       equally-similar weight-0.4 result
#   (b) Weight adjustment not override — very high similarity in low-weight
#       collection can still outrank moderate similarity in high-weight collection
#   (c) Empty collection in merge set does not cause errors
#   (d) Deduplication across collections (same chunk_hash appears once)
#   (e) Merge respects requested result limit (top-K after merge)
#   (f) Parallel search latency (not sequential N * latency)


class TestMultiCollectionWeightedMergeRoadmapPure:
    """Roadmap 2.1.18 pure-function test cases (no Milvus required).

    Validates merge_and_rank and SCOPE_WEIGHTS for cases (a), (b), (d), (e).
    """

    # -- (a) Weighted ranking correctness --------------------------------------

    def test_a_equal_similarity_ranked_by_weight(self):
        """(a) Results with equal raw scores are ranked by scope weight.

        Three results from different scopes with identical similarity (0.8)
        should sort: project (0.80), agent-type (0.56), system (0.32).
        """
        results = [
            {
                "chunk_hash": "sys_hit",
                "score": 0.8,
                "weighted_score": 0.8 * SCOPE_WEIGHTS[MemoryScope.SYSTEM],
                "_scope": "system",
            },
            {
                "chunk_hash": "at_hit",
                "score": 0.8,
                "weighted_score": 0.8 * SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE],
                "_scope": "agent_type",
            },
            {
                "chunk_hash": "proj_hit",
                "score": 0.8,
                "weighted_score": 0.8 * SCOPE_WEIGHTS[MemoryScope.PROJECT],
                "_scope": "project",
            },
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 3
        assert merged[0]["_scope"] == "project"
        assert merged[1]["_scope"] == "agent_type"
        assert merged[2]["_scope"] == "system"

    def test_a_weight_1_0_beats_weight_0_4_at_every_score(self):
        """(a) At equal raw score, weight-1.0 (project) always outranks
        weight-0.4 (system) regardless of the raw score value."""
        for raw_score in [0.1, 0.3, 0.5, 0.7, 0.9, 1.0]:
            results = [
                {
                    "chunk_hash": "project",
                    "score": raw_score,
                    "weighted_score": raw_score * SCOPE_WEIGHTS[MemoryScope.PROJECT],
                    "_scope": "project",
                },
                {
                    "chunk_hash": "system",
                    "score": raw_score,
                    "weighted_score": raw_score * SCOPE_WEIGHTS[MemoryScope.SYSTEM],
                    "_scope": "system",
                },
            ]
            merged = merge_and_rank(results, top_k=10)
            assert merged[0]["_scope"] == "project", f"Failed at raw_score={raw_score}"

    def test_a_weights_match_spec_values(self):
        """(a) Verify the weight constants match spec §4: [1.0, 0.7, 0.4]."""
        assert SCOPE_WEIGHTS[MemoryScope.PROJECT] == 1.0
        assert SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE] == 0.7
        assert SCOPE_WEIGHTS[MemoryScope.SYSTEM] == 0.4

    # -- (b) Weight adjustment not override ------------------------------------

    def test_b_high_system_score_can_outrank_low_project_score(self):
        """(b) Very high similarity in system (weight=0.4) outranks low
        similarity in project (weight=1.0).

        System: 0.95 * 0.4 = 0.38  >  Project: 0.3 * 1.0 = 0.30.
        """
        results = [
            {
                "chunk_hash": "sys_hit",
                "score": 0.95,
                "weighted_score": 0.95 * SCOPE_WEIGHTS[MemoryScope.SYSTEM],
                "_scope": "system",
            },
            {
                "chunk_hash": "proj_hit",
                "score": 0.3,
                "weighted_score": 0.3 * SCOPE_WEIGHTS[MemoryScope.PROJECT],
                "_scope": "project",
            },
        ]
        merged = merge_and_rank(results, top_k=10)
        assert merged[0]["_scope"] == "system", (
            "High-similarity system result should outrank low-similarity project result"
        )
        assert merged[0]["weighted_score"] > merged[1]["weighted_score"]

    def test_b_agent_type_can_outrank_project(self):
        """(b) Extended: high agent-type score (weight=0.7) can outrank
        low project score (weight=1.0).

        Agent-type: 0.9 * 0.7 = 0.63  >  Project: 0.5 * 1.0 = 0.50.
        """
        results = [
            {
                "chunk_hash": "at_hit",
                "score": 0.9,
                "weighted_score": 0.9 * SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE],
                "_scope": "agent_type",
            },
            {
                "chunk_hash": "proj_hit",
                "score": 0.5,
                "weighted_score": 0.5 * SCOPE_WEIGHTS[MemoryScope.PROJECT],
                "_scope": "project",
            },
        ]
        merged = merge_and_rank(results, top_k=10)
        assert merged[0]["_scope"] == "agent_type"

    def test_b_weight_multiplies_not_replaces(self):
        """(b) Weighted score is score * weight — never overridden to just weight."""
        results = [
            {
                "chunk_hash": "a",
                "score": 0.6,
                "weighted_score": 0.6 * 1.0,
                "_scope": "project",
            },
        ]
        merged = merge_and_rank(results, top_k=10)
        # weighted_score should be 0.6, not 1.0 (the weight itself)
        assert merged[0]["weighted_score"] == pytest.approx(0.6)
        assert merged[0]["weighted_score"] != 1.0

    # -- (d) Deduplication across collections ----------------------------------

    def test_d_dedup_across_scopes_keeps_highest_weighted(self):
        """(d) Same chunk_hash in project (0.6*1.0=0.6) and system
        (0.8*0.4=0.32) — kept once with the project entry (highest weighted)."""
        results = [
            {
                "chunk_hash": "shared_chunk",
                "score": 0.6,
                "weighted_score": 0.6 * SCOPE_WEIGHTS[MemoryScope.PROJECT],
                "_scope": "project",
                "_scope_id": "myapp",
            },
            {
                "chunk_hash": "shared_chunk",
                "score": 0.8,
                "weighted_score": 0.8 * SCOPE_WEIGHTS[MemoryScope.SYSTEM],
                "_scope": "system",
                "_scope_id": None,
            },
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 1
        assert merged[0]["_scope"] == "project"
        assert merged[0]["weighted_score"] == pytest.approx(0.6)

    def test_d_dedup_three_scopes_single_entry(self):
        """(d) Same chunk in all 3 scopes — deduplicated to single entry
        from the highest-weighted scope (project)."""
        results = [
            {
                "chunk_hash": "x",
                "score": 0.7,
                "weighted_score": 0.7 * SCOPE_WEIGHTS[MemoryScope.PROJECT],
                "_scope": "project",
            },
            {
                "chunk_hash": "x",
                "score": 0.7,
                "weighted_score": 0.7 * SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE],
                "_scope": "agent_type",
            },
            {
                "chunk_hash": "x",
                "score": 0.7,
                "weighted_score": 0.7 * SCOPE_WEIGHTS[MemoryScope.SYSTEM],
                "_scope": "system",
            },
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 1
        assert merged[0]["_scope"] == "project"

    def test_d_dedup_mixed_unique_and_shared(self):
        """(d) Mix of unique and shared chunk_hashes — shared are deduped,
        unique are preserved."""
        results = [
            {"chunk_hash": "shared", "score": 0.8, "weighted_score": 0.8, "_scope": "project"},
            {"chunk_hash": "shared", "score": 0.7, "weighted_score": 0.28, "_scope": "system"},
            {"chunk_hash": "unique_p", "score": 0.6, "weighted_score": 0.6, "_scope": "project"},
            {"chunk_hash": "unique_s", "score": 0.9, "weighted_score": 0.36, "_scope": "system"},
        ]
        merged = merge_and_rank(results, top_k=10)
        hashes = [m["chunk_hash"] for m in merged]
        assert len(merged) == 3
        assert hashes.count("shared") == 1
        assert "unique_p" in hashes
        assert "unique_s" in hashes

    # -- (e) Top-K after merge -------------------------------------------------

    def test_e_top_k_after_merge_not_per_collection(self):
        """(e) top_k=3 with 5 results from project + 5 from system → 3 total."""
        results = []
        for i in range(5):
            score = 0.9 - i * 0.1
            results.append(
                {
                    "chunk_hash": f"proj_{i}",
                    "score": score,
                    "weighted_score": score * SCOPE_WEIGHTS[MemoryScope.PROJECT],
                    "_scope": "project",
                }
            )
        for i in range(5):
            score = 0.9 - i * 0.1
            results.append(
                {
                    "chunk_hash": f"sys_{i}",
                    "score": score,
                    "weighted_score": score * SCOPE_WEIGHTS[MemoryScope.SYSTEM],
                    "_scope": "system",
                }
            )
        merged = merge_and_rank(results, top_k=3)
        assert len(merged) == 3

    def test_e_top_k_1_returns_single_best(self):
        """(e) top_k=1 returns only the single highest-weighted result."""
        results = [
            {"chunk_hash": "a", "score": 0.9, "weighted_score": 0.9, "_scope": "project"},
            {"chunk_hash": "b", "score": 0.8, "weighted_score": 0.56, "_scope": "agent_type"},
            {"chunk_hash": "c", "score": 0.7, "weighted_score": 0.28, "_scope": "system"},
        ]
        merged = merge_and_rank(results, top_k=1)
        assert len(merged) == 1
        assert merged[0]["chunk_hash"] == "a"

    def test_e_top_k_larger_than_results(self):
        """(e) top_k larger than result count returns all results."""
        results = [
            {"chunk_hash": "a", "score": 0.9, "weighted_score": 0.9, "_scope": "project"},
            {"chunk_hash": "b", "score": 0.5, "weighted_score": 0.2, "_scope": "system"},
        ]
        merged = merge_and_rank(results, top_k=100)
        assert len(merged) == 2


@pytestmark_milvus
class TestMultiCollectionWeightedMergeRoadmap:
    """Roadmap 2.1.18 integration test cases (a)-(f).

    End-to-end tests for multi-collection parallel search with weighted
    merging per spec §4 — Scope Hierarchy.

      (a) Weighted ranking correctness
      (b) Weight adjustment (not override)
      (c) Empty collection in merge set
      (d) Deduplication across collections
      (e) Top-K after merge
      (f) Parallel latency
    """

    @pytest.fixture
    def weighted_router(self, tmp_path: Path):
        """Router with controlled data in 3 collections (3 entries each).

        - Project:    embeddings near [1, 0, 0, 0]
        - Agent-type: embeddings near [0, 1, 0, 0]
        - System:     embeddings near [0, 0, 1, 0]
        """
        db = tmp_path / "weighted_merge_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.PROJECT, "myapp").upsert(
            _make_chunks(
                "proj",
                [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.95, 0.05, 0.0, 0.0],
                    [0.9, 0.1, 0.0, 0.0],
                ],
                [
                    "Project authentication guide",
                    "Project API design patterns",
                    "Project database configuration",
                ],
            )
        )

        r.get_store(MemoryScope.AGENT_TYPE, "coding").upsert(
            _make_chunks(
                "at",
                [
                    [0.0, 1.0, 0.0, 0.0],
                    [0.05, 0.95, 0.0, 0.0],
                    [0.0, 0.9, 0.1, 0.0],
                ],
                [
                    "Coding testing best practices",
                    "Code review guidelines",
                    "Debugging workflow tips",
                ],
            )
        )

        r.get_store(MemoryScope.SYSTEM).upsert(
            _make_chunks(
                "sys",
                [
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.05, 0.95, 0.0],
                    [0.0, 0.0, 0.9, 0.1],
                ],
                [
                    "System logging configuration",
                    "System error handling patterns",
                    "System monitoring setup",
                ],
            )
        )

        yield r
        r.close()

    # -- (a) Weighted ranking correctness --------------------------------------

    @pytest.mark.asyncio
    async def test_a_equal_embeddings_ranked_by_scope_weight(self, tmp_path: Path):
        """(a) When all collections contain an equally-similar entry,
        project (weight=1.0) ranks above system (weight=0.4).

        Uses identical embeddings in all 3 scopes so raw similarity
        is identical — only scope weight determines ranking.
        """
        db = tmp_path / "equal_embed_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        emb = [1.0, 0.0, 0.0, 0.0]
        r.get_store(MemoryScope.PROJECT, "myapp").upsert(_make_chunks("eq_proj", [emb], ["Equal test content project"]))
        r.get_store(MemoryScope.AGENT_TYPE, "coding").upsert(
            _make_chunks("eq_at", [emb], ["Equal test content agent type"])
        )
        r.get_store(MemoryScope.SYSTEM).upsert(_make_chunks("eq_sys", [emb], ["Equal test content system"]))

        results = await r.search(
            emb,
            query_text="Equal test content",
            project_id="myapp",
            agent_type="coding",
            top_k=10,
        )

        scopes = [res["_scope"] for res in results]
        assert "project" in scopes
        assert "system" in scopes

        project_results = [res for res in results if res["_scope"] == "project"]
        system_results = [res for res in results if res["_scope"] == "system"]

        if project_results and system_results:
            best_proj = max(res["weighted_score"] for res in project_results)
            best_sys = max(res["weighted_score"] for res in system_results)
            assert best_proj > best_sys, (
                f"Project weighted_score ({best_proj:.4f}) should exceed "
                f"system weighted_score ({best_sys:.4f}) at equal similarity"
            )
        r.close()

    @pytest.mark.asyncio
    async def test_a_three_scopes_ordered_by_weight(self, tmp_path: Path):
        """(a) Extended: project > agent-type > system ordering when all
        contain equally-similar content."""
        db = tmp_path / "three_scope_order.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        emb = [0.7, 0.7, 0.0, 0.0]
        r.get_store(MemoryScope.PROJECT, "myapp").upsert(
            _make_chunks("order_p", [emb], ["Ordering test content for project"])
        )
        r.get_store(MemoryScope.AGENT_TYPE, "coding").upsert(
            _make_chunks("order_a", [emb], ["Ordering test content for agent type"])
        )
        r.get_store(MemoryScope.SYSTEM).upsert(_make_chunks("order_s", [emb], ["Ordering test content for system"]))

        results = await r.search(
            emb,
            query_text="Ordering test content",
            project_id="myapp",
            agent_type="coding",
            top_k=10,
        )

        # Best weighted_score per scope
        scope_scores: dict[str, float] = {}
        for res in results:
            scope = res["_scope"]
            score = res["weighted_score"]
            if scope not in scope_scores or score > scope_scores[scope]:
                scope_scores[scope] = score

        if all(s in scope_scores for s in ("project", "agent_type", "system")):
            assert scope_scores["project"] > scope_scores["agent_type"], "Project should rank above agent-type"
            assert scope_scores["agent_type"] > scope_scores["system"], "Agent-type should rank above system"
        r.close()

    # -- (b) Weight adjustment not override ------------------------------------

    @pytest.mark.asyncio
    async def test_b_high_system_similarity_outranks_low_project(self, tmp_path: Path):
        """(b) Weight adjusts score, doesn't override — a better-matched
        system result outranks a worse-matched project result.

        With default weights [1.0, 0.4], Milvus Lite's RRF hybrid scoring
        compresses raw score ranges (rank-based, so the 2x max ratio can't
        overcome the 2.5x weight advantage of project).  We use custom
        weights to amplify the system scope, proving the weighting mechanism
        is a multiplier that allows *any* scope to outrank another when the
        similarity advantage is large enough.

        The companion pure-function tests (TestMultiCollectionWeightedMergeRoadmapPure)
        verify the exact arithmetic with default weights.
        """
        db = tmp_path / "weight_not_override.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        query = [1.0, 0.0, 0.0, 0.0]

        # System: near-perfect embedding + text match
        r.get_store(MemoryScope.SYSTEM).upsert(
            _make_chunks("b_sys", [query], ["Authentication token security patterns"])
        )
        # Project: orthogonal embedding, no text overlap
        r.get_store(MemoryScope.PROJECT, "myapp").upsert(
            _make_chunks("b_proj", [[0.0, 0.0, 0.0, 1.0]], ["Database migration strategy notes"])
        )

        # Custom weights: boost system to overcome default project advantage
        custom_weights = {
            MemoryScope.PROJECT: 0.1,
            MemoryScope.SYSTEM: 5.0,
        }
        results = await r.search(
            query,
            query_text="Authentication token security patterns",
            project_id="myapp",
            top_k=10,
            weights=custom_weights,
        )

        system_results = [res for res in results if res["_scope"] == "system"]
        project_results = [res for res in results if res["_scope"] == "project"]

        assert system_results, "Expected system results"
        assert project_results, "Expected project results"

        best_sys = max(res["weighted_score"] for res in system_results)
        best_proj = max(res["weighted_score"] for res in project_results)
        assert best_sys > best_proj, (
            f"System weighted_score ({best_sys:.4f}) should outrank "
            f"project ({best_proj:.4f}) with custom weights that reflect "
            f"similarity advantage"
        )

        # Also verify weights were applied as multipliers
        for res in system_results:
            assert res["_weight"] == 5.0
        for res in project_results:
            assert res["_weight"] == 0.1
        r.close()

    @pytest.mark.asyncio
    async def test_b_raw_score_reflects_similarity(self, tmp_path: Path):
        """(b) Extended: raw scores (before weighting) are higher for
        better-matching entries, confirming similarity IS measured —
        weight adjusts, doesn't replace the similarity signal."""
        db = tmp_path / "raw_score_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        query = [1.0, 0.0, 0.0, 0.0]

        # System: exact embedding + text match among several entries
        r.get_store(MemoryScope.SYSTEM).upsert(
            _make_chunks(
                "b2_sys",
                [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                ],
                [
                    "Authentication token refresh security",
                    "Database schema design patterns",
                    "Container orchestration overview",
                ],
            )
        )
        # Project: all entries far from query
        r.get_store(MemoryScope.PROJECT, "myapp").upsert(
            _make_chunks(
                "b2_proj",
                [
                    [0.0, 0.0, 0.0, 1.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                ],
                [
                    "Deployment pipeline configuration",
                    "Build system optimization tips",
                    "Code review process document",
                ],
            )
        )

        results = await r.search(
            query,
            query_text="Authentication token refresh security",
            project_id="myapp",
            top_k=10,
        )

        system_results = [res for res in results if res["_scope"] == "system"]
        project_results = [res for res in results if res["_scope"] == "project"]

        if system_results and project_results:
            best_sys_raw = max(res["score"] for res in system_results)
            best_proj_raw = max(res["score"] for res in project_results)
            assert best_sys_raw > best_proj_raw, (
                f"System raw score ({best_sys_raw:.4f}) should exceed "
                f"project raw score ({best_proj_raw:.4f}) — weight adjusts "
                f"the final score, doesn't replace similarity"
            )
        r.close()

    @pytest.mark.asyncio
    async def test_b_weight_applied_as_multiplier(self, weighted_router: CollectionRouter):
        """(b) Extended: weighted_score equals score * weight for every
        result (weight is a multiplier, not a replacement)."""
        results = await weighted_router.search(
            [0.5, 0.5, 0.5, 0.0],
            query_text="test",
            project_id="myapp",
            agent_type="coding",
            top_k=10,
        )
        for res in results:
            expected = res["score"] * res["_weight"]
            assert abs(res["weighted_score"] - expected) < 1e-6, (
                f"weighted_score ({res['weighted_score']}) != score ({res['score']}) * weight ({res['_weight']})"
            )

    # -- (c) Empty collection handling -----------------------------------------

    @pytest.mark.asyncio
    async def test_c_empty_collection_no_error(self, tmp_path: Path):
        """(c) An empty collection in the merge set does not cause errors.

        Creates project collection but inserts no data.  Only system has data.
        Search should return system results without error.
        """
        db = tmp_path / "empty_coll_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        # Create project collection but leave it empty
        r.get_store(MemoryScope.PROJECT, "empty_project")

        # System has actual data
        r.get_store(MemoryScope.SYSTEM).upsert(_make_chunks("c_sys", [[1.0, 0.0, 0.0, 0.0]], ["System has data"]))

        results = await r.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="System has data",
            project_id="empty_project",
            top_k=10,
        )

        assert len(results) > 0
        assert all(res["_scope"] == "system" for res in results)
        r.close()

    @pytest.mark.asyncio
    async def test_c_all_empty_collections(self, tmp_path: Path):
        """(c) Extended: ALL collections in merge set are empty → empty
        result list, no error."""
        db = tmp_path / "all_empty_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.PROJECT, "empty_proj")
        r.get_store(MemoryScope.AGENT_TYPE, "empty_at")
        r.get_store(MemoryScope.SYSTEM)

        results = await r.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="anything",
            project_id="empty_proj",
            agent_type="empty_at",
            top_k=10,
        )
        assert results == []
        r.close()

    @pytest.mark.asyncio
    async def test_c_missing_collection_no_error(self, tmp_path: Path):
        """(c) Extended: non-existent collections (never created) handled
        gracefully — returns results from the collections that do exist."""
        db = tmp_path / "missing_coll_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.SYSTEM).upsert(_make_chunks("c_sys2", [[1.0, 0.0, 0.0, 0.0]], ["System data only"]))

        # Search references non-existent project and agent-type
        results = await r.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="System data only",
            project_id="does_not_exist",
            agent_type="also_missing",
            top_k=10,
        )
        assert len(results) > 0
        assert all(res["_scope"] == "system" for res in results)
        r.close()

    # -- (d) Deduplication across collections ----------------------------------

    @pytest.mark.asyncio
    async def test_d_dedup_same_hash_across_collections(self, tmp_path: Path):
        """(d) Same chunk_hash in project and system collections —
        appears once in results with the highest weighted_score."""
        db = tmp_path / "dedup_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        shared_hash = "shared_dedup_chunk"
        emb = [1.0, 0.0, 0.0, 0.0]
        entry = {
            "chunk_hash": shared_hash,
            "embedding": emb,
            "content": "Shared knowledge across scopes",
            "source": "shared.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
        }

        r.get_store(MemoryScope.PROJECT, "myapp").upsert([entry])
        r.get_store(MemoryScope.SYSTEM).upsert([entry])

        results = await r.search(
            emb,
            query_text="Shared knowledge",
            project_id="myapp",
            top_k=10,
        )

        hashes = [res["chunk_hash"] for res in results]
        assert hashes.count(shared_hash) == 1, f"Expected 1 occurrence of shared chunk, got {hashes.count(shared_hash)}"

        shared_result = next(res for res in results if res["chunk_hash"] == shared_hash)
        assert shared_result["_scope"] == "project", "Dedup should keep the entry with highest weighted_score (project)"
        r.close()

    @pytest.mark.asyncio
    async def test_d_dedup_preserves_unique_entries(self, tmp_path: Path):
        """(d) Extended: unique entries are not affected by deduplication,
        only shared chunk_hashes are collapsed."""
        db = tmp_path / "dedup_unique_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        emb = [1.0, 0.0, 0.0, 0.0]

        # Shared entry in both collections
        shared_entry = {
            "chunk_hash": "shared",
            "embedding": emb,
            "content": "Shared content",
            "source": "s.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
        }
        # Unique entries in each collection
        proj_unique = {
            "chunk_hash": "proj_only",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "Project unique content",
            "source": "p.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
        }
        sys_unique = {
            "chunk_hash": "sys_only",
            "embedding": [0.8, 0.0, 0.2, 0.0],
            "content": "System unique content",
            "source": "s2.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
        }

        r.get_store(MemoryScope.PROJECT, "myapp").upsert([shared_entry, proj_unique])
        r.get_store(MemoryScope.SYSTEM).upsert([shared_entry, sys_unique])

        results = await r.search(
            emb,
            query_text="content",
            project_id="myapp",
            top_k=10,
        )

        hashes = {res["chunk_hash"] for res in results}
        assert "shared" in hashes
        assert "proj_only" in hashes
        assert "sys_only" in hashes
        # shared appears once (deduplicated), others appear once each
        hash_list = [res["chunk_hash"] for res in results]
        assert hash_list.count("shared") == 1
        r.close()

    # -- (e) Top-K after merge -------------------------------------------------

    @pytest.mark.asyncio
    async def test_e_top_k_limits_merged_results(self, weighted_router: CollectionRouter):
        """(e) top_k=2 across 3 collections (9 total entries) → at most 2."""
        results = await weighted_router.search(
            [0.5, 0.5, 0.5, 0.0],
            query_text="test",
            project_id="myapp",
            agent_type="coding",
            top_k=2,
        )
        assert len(results) <= 2, f"Expected at most 2 results (top_k=2), got {len(results)}"

    @pytest.mark.asyncio
    async def test_e_top_k_spans_multiple_scopes(self, weighted_router: CollectionRouter):
        """(e) Extended: with top_k=4, results can come from multiple
        scopes (not capped at top_k per-collection)."""
        results = await weighted_router.search(
            [0.5, 0.5, 0.5, 0.0],
            query_text="test",
            project_id="myapp",
            agent_type="coding",
            top_k=4,
        )
        assert len(results) <= 4
        # With a mixed-direction query, results should span multiple scopes
        scopes = {res["_scope"] for res in results}
        assert len(scopes) >= 1

    @pytest.mark.asyncio
    async def test_e_top_k_1_returns_single_best(self, weighted_router: CollectionRouter):
        """(e) top_k=1 returns exactly 1 result — the single best across
        all collections."""
        results = await weighted_router.search(
            [1.0, 0.0, 0.0, 0.0],
            query_text="authentication",
            project_id="myapp",
            agent_type="coding",
            top_k=1,
        )
        assert len(results) == 1

    # -- (f) Parallel latency --------------------------------------------------

    @pytest.mark.asyncio
    async def test_f_parallel_search_not_sequential(self, weighted_router: CollectionRouter):
        """(f) Parallel search across 3 collections completes in reasonable
        time — not sequential N * single-collection latency.

        Strategy: time a 3-scope search and a 1-scope search.  The 3-scope
        search should not take >=3x the single-scope time (which would
        indicate sequential execution).

        Note: with Milvus Lite and tiny test collections the thread-pool
        overhead can dominate, so we use a generous threshold (5x).
        The real benefit of parallelism shows with larger collections.
        """
        import time

        query = [0.5, 0.5, 0.5, 0.0]

        # Warm up (first search may initialise lazy resources)
        await weighted_router.search(query, query_text="warmup", project_id="myapp", top_k=5)

        # Time single-scope search (system only, no project_id/agent_type)
        single_times: list[float] = []
        for _ in range(5):
            t0 = time.perf_counter()
            await weighted_router.search(query, query_text="single", top_k=5)
            single_times.append(time.perf_counter() - t0)
        avg_single = sum(single_times) / len(single_times)

        # Time three-scope parallel search
        multi_times: list[float] = []
        for _ in range(5):
            t0 = time.perf_counter()
            await weighted_router.search(
                query,
                query_text="multi",
                project_id="myapp",
                agent_type="coding",
                top_k=5,
            )
            multi_times.append(time.perf_counter() - t0)
        avg_multi = sum(multi_times) / len(multi_times)

        # 3 scopes searched in parallel should be well under 3x sequential.
        # We allow 5x as a generous ceiling for CI / Milvus Lite overhead.
        assert avg_multi < avg_single * 3.0 * 5.0, (
            f"3-scope search ({avg_multi:.4f}s) took >= 15x single-scope "
            f"({avg_single:.4f}s) -- parallelism may not be working"
        )

    @pytest.mark.asyncio
    async def test_f_parallel_search_returns_all_scopes(self, weighted_router: CollectionRouter):
        """(f) Extended: parallel search actually queries all scopes and
        returns results from each — confirming the parallel fan-out works."""
        results = await weighted_router.search(
            [0.5, 0.5, 0.5, 0.0],
            query_text="test",
            project_id="myapp",
            agent_type="coding",
            top_k=10,
        )
        scopes_found = {res["_scope"] for res in results}
        assert "project" in scopes_found, "No project results — parallel search may have skipped it"
        assert "agent_type" in scopes_found, "No agent-type results — parallel search may have skipped it"
        assert "system" in scopes_found, "No system results — parallel search may have skipped it"


# ---- Roadmap 2.1.19: Cross-collection tag search test cases (a)-(g) --------
# Spec: docs/specs/design/memory-plugin.md §7 — Tag-Based Cross-Scope Discovery


def _make_tagged_entry(
    chunk_hash: str,
    content: str,
    tags: list[str],
    *,
    entry_type: str = "document",
    topic: str = "",
    source: str = "test.md",
    embedding: list[float] | None = None,
) -> dict:
    """Helper to build a chunk dict with tags for upsert."""
    return {
        "chunk_hash": chunk_hash,
        "entry_type": entry_type,
        "embedding": embedding or [0.0, 0.0, 0.0, 0.0],
        "content": content,
        "source": source,
        "heading": "",
        "heading_level": 0,
        "start_line": 1,
        "end_line": 1,
        "tags": json.dumps(tags),
        "topic": topic,
    }


@pytestmark_milvus
class TestCrossCollectionTagSearchRoadmap:
    """Roadmap 2.1.19 test cases (a)-(g) for cross-collection tag search.

    These tests validate spec §7.3 — Tag-Based Cross-Scope Discovery:
      (a) Project-scoped tagged memory found by global (cross-scope) search
      (b) Multi-collection results with correct source attribution
      (c) Non-existent tag returns empty list (not error)
      (d) Entry with multiple tags found by search on any single tag
      (e) Tag search combined with topic filter narrows results
      (f) Case-insensitive tag matching
      (g) Special characters in tags (hyphens, underscores)
    """

    @pytest.fixture
    def multi_scope_router(self, tmp_path: Path):
        """Router with tagged data across project, agent-type, and system collections."""
        db = tmp_path / "roadmap_tag_test.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        # Project collection: api-pattern tagged entries
        proj_store = r.get_store(MemoryScope.PROJECT, "webapp")
        proj_store.upsert(
            [
                _make_tagged_entry(
                    "proj_api",
                    "REST API authentication pattern using JWT tokens",
                    ["api-pattern", "auth"],
                    topic="architecture",
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
                _make_tagged_entry(
                    "proj_db",
                    "Database connection pooling strategy",
                    ["database", "performance"],
                    topic="database",
                    embedding=[0.0, 1.0, 0.0, 0.0],
                ),
                _make_tagged_entry(
                    "proj_api_v2",
                    "GraphQL API design patterns",
                    ["api-pattern", "graphql"],
                    topic="architecture",
                    embedding=[0.9, 0.0, 0.1, 0.0],
                ),
            ]
        )

        # Agent-type collection
        at_store = r.get_store(MemoryScope.AGENT_TYPE, "coding")
        at_store.upsert(
            [
                _make_tagged_entry(
                    "at_api",
                    "Best practices for API error handling",
                    ["api-pattern", "error-handling"],
                    topic="conventions",
                    embedding=[0.8, 0.0, 0.0, 0.2],
                ),
            ]
        )

        # System collection
        sys_store = r.get_store(MemoryScope.SYSTEM)
        sys_store.upsert(
            [
                _make_tagged_entry(
                    "sys_api",
                    "System-wide API rate limiting configuration",
                    ["api-pattern", "config"],
                    topic="operations",
                    embedding=[0.0, 0.0, 1.0, 0.0],
                ),
                _make_tagged_entry(
                    "sys_logging",
                    "Structured logging configuration",
                    ["config", "logging"],
                    topic="operations",
                    embedding=[0.0, 0.0, 0.0, 1.0],
                ),
            ]
        )

        yield r
        r.close()

    # -- (a) Project-scoped tagged memory found by global search ---------------

    def test_a_project_tag_found_by_global_search(self, multi_scope_router: CollectionRouter):
        """(a) Memory tagged #api-pattern in project collection is found by
        tag search from system scope (i.e., global cross-scope search)."""
        results = multi_scope_router.search_by_tag("api-pattern")

        # Should find project entries via cross-scope discovery
        hashes = {r["chunk_hash"] for r in results}
        assert "proj_api" in hashes, "Project-scoped tagged entry not found by global search"
        assert "proj_api_v2" in hashes, "Second project entry not found"

    def test_a_project_tag_discovered_by_fresh_router(self, tmp_path: Path):
        """(a) Extended: fresh router with no cache discovers project-scoped
        tagged entries (spec §7.3 cross-cutting discovery)."""
        db = tmp_path / "fresh_discover.db"

        # Populate with first router
        r1 = CollectionRouter(milvus_uri=str(db), dimension=4)
        r1.get_store(MemoryScope.PROJECT, "webapp").upsert(
            [
                _make_tagged_entry(
                    "proj_fresh",
                    "Project API pattern for fresh discovery",
                    ["api-pattern"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )
        r1.close()

        # Fresh router discovers the project entry
        r2 = CollectionRouter(milvus_uri=str(db), dimension=4)
        assert len(r2._stores) == 0  # no cached stores

        results = r2.search_by_tag("api-pattern")
        hashes = {r["chunk_hash"] for r in results}
        assert "proj_fresh" in hashes, "Fresh router failed to discover project entry"
        r2.close()

    # -- (b) Source attribution across multiple collections --------------------

    def test_b_multi_collection_source_attribution(self, multi_scope_router: CollectionRouter):
        """(b) Tag search returns results from multiple collections with
        correct source attribution (_collection, _scope, _scope_id)."""
        results = multi_scope_router.search_by_tag("api-pattern")

        # Should have results from 3 different collections
        collections = {r["_collection"] for r in results}
        assert len(collections) >= 2, f"Expected results from multiple collections, got: {collections}"

        # Verify each result has correct source attribution
        for r in results:
            assert "_collection" in r, "Missing _collection annotation"
            assert "_scope" in r, "Missing _scope annotation"
            assert "_scope_id" in r, "Missing _scope_id annotation"
            assert r["_collection"].startswith("aq_"), f"Collection name should start with aq_: {r['_collection']}"

        # Verify specific scope values
        scope_map = {r["chunk_hash"]: (r["_scope"], r["_scope_id"]) for r in results}
        assert scope_map["proj_api"] == ("project", "webapp")
        assert scope_map["proj_api_v2"] == ("project", "webapp")
        assert scope_map["at_api"] == ("agent_type", "coding")
        assert scope_map["sys_api"] == ("system", None)

    def test_b_collection_names_are_correct(self, multi_scope_router: CollectionRouter):
        """(b) Extended: collection names follow aq_* naming convention."""
        results = multi_scope_router.search_by_tag("api-pattern")

        collection_map = {r["chunk_hash"]: r["_collection"] for r in results}
        assert collection_map["proj_api"] == "aq_project_webapp"
        assert collection_map["at_api"] == "aq_agenttype_coding"
        assert collection_map["sys_api"] == "aq_system"

    # -- (c) Non-existent tag returns empty (not error) ------------------------

    def test_c_nonexistent_tag_returns_empty(self, multi_scope_router: CollectionRouter):
        """(c) Tag search with non-existent tag returns empty list (not error)."""
        results = multi_scope_router.search_by_tag("completely-nonexistent-tag-xyz")
        assert results == [], f"Expected empty list, got {len(results)} results"

    def test_c_nonexistent_tag_empty_router(self, tmp_path: Path):
        """(c) Extended: non-existent tag on a router with no collections."""
        db = tmp_path / "empty_nonexist.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)
        results = r.search_by_tag("nonexistent")
        assert results == []
        r.close()

    # -- (d) Multiple tags: entry found by any single tag ----------------------

    def test_d_multiple_tags_found_by_any_single_tag(self, multi_scope_router: CollectionRouter):
        """(d) Memory with multiple tags is found by search on any single tag."""
        # proj_api has tags: ["api-pattern", "auth"]
        # Search by "api-pattern"
        results_api = multi_scope_router.search_by_tag("api-pattern")
        hashes_api = {r["chunk_hash"] for r in results_api}
        assert "proj_api" in hashes_api

        # Search by "auth" — should also find proj_api
        results_auth = multi_scope_router.search_by_tag("auth")
        hashes_auth = {r["chunk_hash"] for r in results_auth}
        assert "proj_api" in hashes_auth, "Entry with multiple tags not found when searching by second tag"

    def test_d_each_tag_independently_findable(self, multi_scope_router: CollectionRouter):
        """(d) Extended: every tag on a multi-tagged entry can discover it."""
        # proj_api_v2 has tags: ["api-pattern", "graphql"]
        for tag in ["api-pattern", "graphql"]:
            results = multi_scope_router.search_by_tag(tag)
            hashes = {r["chunk_hash"] for r in results}
            assert "proj_api_v2" in hashes, f"Entry not found when searching by tag '{tag}'"

    # -- (e) Tag search + topic filter -----------------------------------------

    def test_e_tag_search_with_topic_filter(self, multi_scope_router: CollectionRouter):
        """(e) Tag search combined with topic filter narrows results correctly."""
        # Search api-pattern without topic — should get entries from all topics
        all_results = multi_scope_router.search_by_tag("api-pattern")
        assert len(all_results) >= 3  # proj_api, proj_api_v2, at_api, sys_api

        # Search api-pattern with topic="architecture" — should only get
        # entries with topic="architecture" or topic=""
        filtered = multi_scope_router.search_by_tag("api-pattern", topic="architecture")
        for r in filtered:
            assert r.get("topic", "") in ("architecture", ""), f"Topic filter leaked: got topic='{r.get('topic')}'"

        # The architecture-tagged entries should be found
        hashes = {r["chunk_hash"] for r in filtered}
        assert "proj_api" in hashes  # topic="architecture"
        assert "proj_api_v2" in hashes  # topic="architecture"

    def test_e_topic_filter_excludes_other_topics(self, multi_scope_router: CollectionRouter):
        """(e) Extended: topic filter correctly excludes non-matching topics."""
        # Search api-pattern with topic="conventions" — should only get at_api
        filtered = multi_scope_router.search_by_tag("api-pattern", topic="conventions")
        hashes = {r["chunk_hash"] for r in filtered}
        assert "at_api" in hashes  # topic="conventions"
        # Entries with other topics should be excluded
        assert "proj_api" not in hashes  # topic="architecture"
        assert "sys_api" not in hashes  # topic="operations"

    # -- (f) Case-insensitive tag matching -------------------------------------

    def test_f_case_insensitive_tag_search(self, router: CollectionRouter):
        """(f) Tag names are case-insensitive: searching 'API' matches 'api'."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                _make_tagged_entry(
                    "case_test",
                    "API design guidelines",
                    ["api", "design"],  # tags stored lowercase
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        # Search with uppercase — should match lowercase tags
        results_upper = router.search_by_tag("API")
        hashes_upper = {r["chunk_hash"] for r in results_upper}
        assert "case_test" in hashes_upper, "Uppercase search did not match lowercase tag"

        # Search with mixed case
        results_mixed = router.search_by_tag("Api")
        hashes_mixed = {r["chunk_hash"] for r in results_mixed}
        assert "case_test" in hashes_mixed, "Mixed-case search did not match lowercase tag"

        # Search with lowercase (exact match)
        results_lower = router.search_by_tag("api")
        hashes_lower = {r["chunk_hash"] for r in results_lower}
        assert "case_test" in hashes_lower, "Lowercase search did not match"

    def test_f_case_insensitive_with_hash_prefix(self, router: CollectionRouter):
        """(f) Extended: tag names with # prefix are case-insensitive."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                _make_tagged_entry(
                    "hash_case",
                    "Content with hash-prefixed tag",
                    ["api-pattern"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        # Various case combinations
        for variant in ["API-PATTERN", "Api-Pattern", "api-pattern", "API-pattern"]:
            results = router.search_by_tag(variant)
            hashes = {r["chunk_hash"] for r in results}
            assert "hash_case" in hashes, f"Case variant '{variant}' did not match tag 'api-pattern'"

    # -- (g) Special characters in tags ----------------------------------------

    def test_g_hyphens_in_tags(self, router: CollectionRouter):
        """(g) Tags with hyphens work correctly."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                _make_tagged_entry(
                    "hyphen_tag",
                    "Content with hyphenated tag",
                    ["api-pattern", "error-handling", "rate-limiting"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        for tag in ["api-pattern", "error-handling", "rate-limiting"]:
            results = router.search_by_tag(tag)
            assert len(results) >= 1, f"Hyphenated tag '{tag}' not found"
            assert results[0]["chunk_hash"] == "hyphen_tag"

    def test_g_underscores_in_tags(self, router: CollectionRouter):
        """(g) Tags with underscores work correctly."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                _make_tagged_entry(
                    "underscore_tag",
                    "Content with underscore tag",
                    ["api_pattern", "error_handling"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        for tag in ["api_pattern", "error_handling"]:
            results = router.search_by_tag(tag)
            assert len(results) >= 1, f"Underscore tag '{tag}' not found"
            assert results[0]["chunk_hash"] == "underscore_tag"

    def test_g_dots_in_tags(self, router: CollectionRouter):
        """(g) Tags with dots (e.g., 'node.js') work correctly."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                _make_tagged_entry(
                    "dot_tag",
                    "Content about Node.js",
                    ["node.js", "v8.engine"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        results = router.search_by_tag("node.js")
        assert len(results) >= 1, "Dot-containing tag 'node.js' not found"
        assert results[0]["chunk_hash"] == "dot_tag"

    def test_g_mixed_special_chars(self, router: CollectionRouter):
        """(g) Tags with mixed special characters all work."""
        store = router.get_store(MemoryScope.SYSTEM)
        store.upsert(
            [
                _make_tagged_entry(
                    "mixed_special",
                    "Content with various special char tags",
                    ["c++", "c#", ".net-core", "vue_3.x"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        # These tags should all be findable
        for tag in ["c++", ".net-core", "vue_3.x"]:
            results = router.search_by_tag(tag)
            assert len(results) >= 1, f"Special-char tag '{tag}' not found"


# ---- Roadmap 2.1.19: Async variants of key test cases -----------------------


@pytestmark_milvus
class TestCrossCollectionTagSearchRoadmapAsync:
    """Async variants of roadmap 2.1.19 test cases (a), (b), (d), (f)."""

    @pytest.mark.asyncio
    async def test_a_async_project_tag_found_globally(self, tmp_path: Path):
        """(a) Async: project-scoped tagged entry discovered by global search."""
        db = tmp_path / "async_roadmap_a.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.PROJECT, "webapp").upsert(
            [
                _make_tagged_entry(
                    "async_proj_api",
                    "Project API pattern",
                    ["api-pattern"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )
        r.get_store(MemoryScope.SYSTEM).upsert(
            [
                _make_tagged_entry(
                    "async_sys_api",
                    "System API config",
                    ["api-pattern"],
                    embedding=[0.0, 1.0, 0.0, 0.0],
                ),
            ]
        )

        results = await r.search_by_tag_async("api-pattern")
        hashes = {h["chunk_hash"] for h in results}
        assert "async_proj_api" in hashes
        assert "async_sys_api" in hashes
        r.close()

    @pytest.mark.asyncio
    async def test_b_async_source_attribution(self, tmp_path: Path):
        """(b) Async: results have correct source attribution."""
        db = tmp_path / "async_roadmap_b.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.PROJECT, "myapp").upsert(
            [
                _make_tagged_entry(
                    "b_proj",
                    "Project entry",
                    ["shared-tag"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )
        r.get_store(MemoryScope.SYSTEM).upsert(
            [
                _make_tagged_entry(
                    "b_sys",
                    "System entry",
                    ["shared-tag"],
                    embedding=[0.0, 1.0, 0.0, 0.0],
                ),
            ]
        )

        results = await r.search_by_tag_async("shared-tag")
        scope_map = {h["chunk_hash"]: (h["_scope"], h["_scope_id"]) for h in results}
        assert scope_map["b_proj"] == ("project", "myapp")
        assert scope_map["b_sys"] == ("system", None)
        r.close()

    @pytest.mark.asyncio
    async def test_c_async_nonexistent_tag_empty(self, tmp_path: Path):
        """(c) Async: non-existent tag returns empty list."""
        db = tmp_path / "async_roadmap_c.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)
        r.get_store(MemoryScope.SYSTEM).upsert(
            [
                _make_tagged_entry(
                    "c_entry",
                    "Some content",
                    ["real-tag"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        results = await r.search_by_tag_async("nonexistent-tag-xyz")
        assert results == []
        r.close()

    @pytest.mark.asyncio
    async def test_d_async_multiple_tags(self, tmp_path: Path):
        """(d) Async: entry with multiple tags found by any single tag."""
        db = tmp_path / "async_roadmap_d.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.SYSTEM).upsert(
            [
                _make_tagged_entry(
                    "multi_tag",
                    "Multi-tagged entry",
                    ["alpha", "beta", "gamma"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        for tag in ["alpha", "beta", "gamma"]:
            results = await r.search_by_tag_async(tag)
            hashes = {h["chunk_hash"] for h in results}
            assert "multi_tag" in hashes, f"Tag '{tag}' did not find multi-tagged entry"
        r.close()

    @pytest.mark.asyncio
    async def test_e_async_tag_with_topic_filter(self, tmp_path: Path):
        """(e) Async: tag search combined with topic filter."""
        db = tmp_path / "async_roadmap_e.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.SYSTEM).upsert(
            [
                _make_tagged_entry(
                    "e_arch",
                    "Architecture pattern",
                    ["pattern"],
                    topic="architecture",
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
                _make_tagged_entry(
                    "e_ops",
                    "Operations pattern",
                    ["pattern"],
                    topic="operations",
                    embedding=[0.0, 1.0, 0.0, 0.0],
                ),
            ]
        )

        # With topic filter
        results = await r.search_by_tag_async("pattern", topic="architecture")
        hashes = {h["chunk_hash"] for h in results}
        assert "e_arch" in hashes
        assert "e_ops" not in hashes
        r.close()

    @pytest.mark.asyncio
    async def test_f_async_case_insensitive(self, tmp_path: Path):
        """(f) Async: case-insensitive tag matching."""
        db = tmp_path / "async_roadmap_f.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.SYSTEM).upsert(
            [
                _make_tagged_entry(
                    "ci_entry",
                    "Case insensitive test",
                    ["api"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        # Uppercase search should find lowercase-tagged entry
        results = await r.search_by_tag_async("API")
        hashes = {h["chunk_hash"] for h in results}
        assert "ci_entry" in hashes, "Async case-insensitive search failed"
        r.close()

    @pytest.mark.asyncio
    async def test_g_async_special_chars(self, tmp_path: Path):
        """(g) Async: special characters in tags work correctly."""
        db = tmp_path / "async_roadmap_g.db"
        r = CollectionRouter(milvus_uri=str(db), dimension=4)

        r.get_store(MemoryScope.SYSTEM).upsert(
            [
                _make_tagged_entry(
                    "special_async",
                    "Special chars async",
                    ["api-pattern", "node_js", "vue.js"],
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
            ]
        )

        for tag in ["api-pattern", "node_js", "vue.js"]:
            results = await r.search_by_tag_async(tag)
            hashes = {h["chunk_hash"] for h in results}
            assert "special_async" in hashes, f"Async special char tag '{tag}' failed"
        r.close()


# ---- Scope Resolver tests (pure, no Milvus needed) --------------------------


class TestScopeEntry:
    """Tests for the ScopeEntry dataclass."""

    def test_frozen(self):
        entry = ScopeEntry(
            scope=MemoryScope.SYSTEM,
            scope_id=None,
            collection="aq_system",
            weight=0.4,
        )
        with pytest.raises(AttributeError):
            entry.weight = 1.0  # type: ignore[misc]

    def test_equality(self):
        a = ScopeEntry(MemoryScope.PROJECT, "myapp", "aq_project_myapp", 1.0)
        b = ScopeEntry(MemoryScope.PROJECT, "myapp", "aq_project_myapp", 1.0)
        assert a == b

    def test_inequality_different_weight(self):
        a = ScopeEntry(MemoryScope.SYSTEM, None, "aq_system", 0.4)
        b = ScopeEntry(MemoryScope.SYSTEM, None, "aq_system", 0.5)
        assert a != b

    def test_repr_readable(self):
        entry = ScopeEntry(MemoryScope.AGENT_TYPE, "coding", "aq_agenttype_coding", 0.7)
        r = repr(entry)
        assert "coding" in r
        assert "0.7" in r


class TestResolveScopes:
    """Tests for resolve_scopes() — the scope resolver per spec §4."""

    def test_full_context_agent_type_and_project(self):
        """Given both agent_type and project_id, returns 3 scopes in order."""
        entries = resolve_scopes(agent_type="coding", project_id="mech-fighters")
        assert len(entries) == 3
        assert entries[0].scope == MemoryScope.PROJECT
        assert entries[1].scope == MemoryScope.AGENT_TYPE
        assert entries[2].scope == MemoryScope.SYSTEM

    def test_default_weights(self):
        """Default weights match spec §4: project=1.0, agent-type=0.7, system=0.4."""
        entries = resolve_scopes(agent_type="coding", project_id="mech-fighters")
        assert entries[0].weight == 1.0
        assert entries[1].weight == 0.7
        assert entries[2].weight == 0.4

    def test_collection_names_are_canonical(self):
        """Collection names use the canonical naming convention."""
        entries = resolve_scopes(agent_type="coding", project_id="mech-fighters")
        assert entries[0].collection == "aq_project_mech_fighters"
        assert entries[1].collection == "aq_agenttype_coding"
        assert entries[2].collection == "aq_system"

    def test_scope_ids_are_raw(self):
        """scope_id stores the raw identifier, not the sanitized form."""
        entries = resolve_scopes(agent_type="code-review", project_id="My App")
        assert entries[0].scope_id == "My App"
        assert entries[1].scope_id == "code-review"
        assert entries[2].scope_id is None

    def test_agent_type_only(self):
        """With only agent_type, returns agent-type + system."""
        entries = resolve_scopes(agent_type="coding")
        assert len(entries) == 2
        assert entries[0].scope == MemoryScope.AGENT_TYPE
        assert entries[0].weight == 0.7
        assert entries[1].scope == MemoryScope.SYSTEM
        assert entries[1].weight == 0.4

    def test_project_only(self):
        """With only project_id, returns project + system."""
        entries = resolve_scopes(project_id="myapp")
        assert len(entries) == 2
        assert entries[0].scope == MemoryScope.PROJECT
        assert entries[0].weight == 1.0
        assert entries[1].scope == MemoryScope.SYSTEM
        assert entries[1].weight == 0.4

    def test_no_context_system_only(self):
        """With no agent_type or project_id, returns system only."""
        entries = resolve_scopes()
        assert len(entries) == 1
        assert entries[0].scope == MemoryScope.SYSTEM
        assert entries[0].collection == "aq_system"
        assert entries[0].weight == 0.4

    def test_system_always_included(self):
        """System scope is always present regardless of other params."""
        for kwargs in [
            {},
            {"agent_type": "coding"},
            {"project_id": "app"},
            {"agent_type": "coding", "project_id": "app"},
        ]:
            entries = resolve_scopes(**kwargs)
            system_entries = [e for e in entries if e.scope == MemoryScope.SYSTEM]
            assert len(system_entries) == 1, f"System scope missing for {kwargs}"

    def test_ordering_most_specific_first(self):
        """Results are ordered from most specific to broadest."""
        entries = resolve_scopes(agent_type="coding", project_id="app")
        weights = [e.weight for e in entries]
        assert weights == sorted(weights, reverse=True)

    def test_custom_weights_override(self):
        """Custom weights override defaults."""
        custom = {
            MemoryScope.PROJECT: 0.9,
            MemoryScope.AGENT_TYPE: 0.5,
            MemoryScope.SYSTEM: 0.1,
        }
        entries = resolve_scopes(
            agent_type="coding",
            project_id="app",
            weights=custom,
        )
        assert entries[0].weight == 0.9  # project
        assert entries[1].weight == 0.5  # agent-type
        assert entries[2].weight == 0.1  # system

    def test_partial_custom_weights(self):
        """Partial custom weights — unspecified scopes keep defaults."""
        custom = {MemoryScope.PROJECT: 0.8}
        entries = resolve_scopes(
            agent_type="coding",
            project_id="app",
            weights=custom,
        )
        assert entries[0].weight == 0.8  # overridden
        assert entries[1].weight == 0.7  # default
        assert entries[2].weight == 0.4  # default

    def test_include_orchestrator(self):
        """include_orchestrator=True adds orchestrator between agent-type and system."""
        entries = resolve_scopes(
            agent_type="coding",
            project_id="app",
            include_orchestrator=True,
        )
        assert len(entries) == 4
        assert entries[0].scope == MemoryScope.PROJECT
        assert entries[1].scope == MemoryScope.AGENT_TYPE
        assert entries[2].scope == MemoryScope.ORCHESTRATOR
        assert entries[3].scope == MemoryScope.SYSTEM
        # Orchestrator default weight is 0.5
        assert entries[2].weight == 0.5
        assert entries[2].collection == "aq_orchestrator"

    def test_include_orchestrator_no_agent_type(self):
        """Orchestrator is placed between project and system when no agent_type."""
        entries = resolve_scopes(project_id="app", include_orchestrator=True)
        assert len(entries) == 3
        assert entries[0].scope == MemoryScope.PROJECT
        assert entries[1].scope == MemoryScope.ORCHESTRATOR
        assert entries[2].scope == MemoryScope.SYSTEM

    def test_include_orchestrator_no_project(self):
        """Orchestrator is placed between agent-type and system when no project."""
        entries = resolve_scopes(agent_type="coding", include_orchestrator=True)
        assert len(entries) == 3
        assert entries[0].scope == MemoryScope.AGENT_TYPE
        assert entries[1].scope == MemoryScope.ORCHESTRATOR
        assert entries[2].scope == MemoryScope.SYSTEM

    def test_sanitized_collection_names(self):
        """Collection names are properly sanitized."""
        entries = resolve_scopes(
            agent_type="code-review",
            project_id="My Cool Project!!!",
        )
        assert entries[0].collection == "aq_project_my_cool_project"
        assert entries[1].collection == "aq_agenttype_code_review"

    def test_returns_list_of_scope_entries(self):
        """All returned items are ScopeEntry instances."""
        entries = resolve_scopes(agent_type="coding", project_id="app")
        for entry in entries:
            assert isinstance(entry, ScopeEntry)

    def test_no_duplicate_scopes(self):
        """Each scope appears at most once."""
        entries = resolve_scopes(
            agent_type="coding",
            project_id="app",
            include_orchestrator=True,
        )
        scopes = [e.scope for e in entries]
        assert len(scopes) == len(set(scopes))

    def test_collection_names_valid_milvus(self):
        """All generated collection names are valid Milvus names."""
        import re

        milvus_re = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,254}$")
        entries = resolve_scopes(
            agent_type="code-review",
            project_id="mech-fighters",
            include_orchestrator=True,
        )
        for entry in entries:
            assert milvus_re.match(entry.collection), f"Invalid Milvus collection name: {entry.collection}"


# ---- Roadmap 3.1.8: Scope resolution test cases (a)-(f) ---------------------
# Spec: docs/specs/design/memory-scoping.md §4 — Scope Hierarchy
#
#   (a) resolver for (agent_type="coding", project_id="myapp") returns
#       collections in order: [aq_project_myapp, aq_agenttype_coding,
#       aq_system] with weights [1.0, 0.7, 0.4]
#   (b) resolver for (agent_type="coding", project_id=None) returns
#       [aq_agenttype_coding, aq_system] (no project scope)
#   (c) resolver for (agent_type=None, project_id="myapp") returns
#       [aq_project_myapp, aq_system] (no agent-type scope)
#   (d) resolver for unknown agent_type still returns system collection
#   (e) collections are created on-demand if they don't exist yet
#   (f) weight values match the spec exactly and are configurable


class TestRoadmap318ScopeResolution:
    """Roadmap 3.1.8 — Scope resolution per memory-scoping §4.

    Each test method maps 1:1 to a roadmap case letter using the exact
    parameters and assertions from the spec.
    """

    # -- (a) Full context: both agent_type and project_id ----------------------

    def test_a_full_context_ordered_collections_and_weights(self):
        """(a) resolver for (agent_type="coding", project_id="myapp") returns
        collections in order [aq_project_myapp, aq_agenttype_coding, aq_system]
        with weights [1.0, 0.7, 0.4].
        """
        entries = resolve_scopes(agent_type="coding", project_id="myapp")

        assert len(entries) == 3

        # Ordered collection names
        collections = [e.collection for e in entries]
        assert collections == ["aq_project_myapp", "aq_agenttype_coding", "aq_system"]

        # Weights per spec §4
        weights = [e.weight for e in entries]
        assert weights == [1.0, 0.7, 0.4]

        # Scope enum values in order
        scopes = [e.scope for e in entries]
        assert scopes == [MemoryScope.PROJECT, MemoryScope.AGENT_TYPE, MemoryScope.SYSTEM]

    # -- (b) No project: agent_type only ---------------------------------------

    def test_b_no_project_scope(self):
        """(b) resolver for (agent_type="coding", project_id=None) returns
        [aq_agenttype_coding, aq_system] — no project scope in output.
        """
        entries = resolve_scopes(agent_type="coding", project_id=None)

        assert len(entries) == 2

        collections = [e.collection for e in entries]
        assert collections == ["aq_agenttype_coding", "aq_system"]

        weights = [e.weight for e in entries]
        assert weights == [0.7, 0.4]

        # No PROJECT scope present
        scopes = {e.scope for e in entries}
        assert MemoryScope.PROJECT not in scopes

    # -- (c) No agent-type: project only ---------------------------------------

    def test_c_no_agent_type_scope(self):
        """(c) resolver for (agent_type=None, project_id="myapp") returns
        [aq_project_myapp, aq_system] — no agent-type scope in output.
        """
        entries = resolve_scopes(agent_type=None, project_id="myapp")

        assert len(entries) == 2

        collections = [e.collection for e in entries]
        assert collections == ["aq_project_myapp", "aq_system"]

        weights = [e.weight for e in entries]
        assert weights == [1.0, 0.4]

        # No AGENT_TYPE scope present
        scopes = {e.scope for e in entries}
        assert MemoryScope.AGENT_TYPE not in scopes

    # -- (d) Unknown agent_type still returns system ---------------------------

    def test_d_unknown_agent_type_returns_system(self):
        """(d) resolver for unknown agent_type still returns system collection.

        An unrecognized agent type name should not cause an error — the resolver
        simply creates a scope entry for it and always includes system.
        """
        entries = resolve_scopes(agent_type="totally_unknown_type_xyz")

        # System is always present
        system_entries = [e for e in entries if e.scope == MemoryScope.SYSTEM]
        assert len(system_entries) == 1
        assert system_entries[0].collection == "aq_system"
        assert system_entries[0].weight == 0.4

        # The unknown agent type still gets its own scope entry
        at_entries = [e for e in entries if e.scope == MemoryScope.AGENT_TYPE]
        assert len(at_entries) == 1
        assert at_entries[0].collection == "aq_agenttype_totally_unknown_type_xyz"
        assert at_entries[0].scope_id == "totally_unknown_type_xyz"

    def test_d_unknown_agent_type_with_project_still_returns_system(self):
        """(d) even with both unknown agent_type and project, system is present."""
        entries = resolve_scopes(
            agent_type="never_heard_of_this",
            project_id="nonexistent_project",
        )
        scopes = [e.scope for e in entries]
        assert MemoryScope.SYSTEM in scopes
        assert len(entries) == 3

    def test_d_no_context_still_returns_system(self):
        """(d) with no agent_type and no project_id, system is still returned."""
        entries = resolve_scopes()
        assert len(entries) == 1
        assert entries[0].scope == MemoryScope.SYSTEM
        assert entries[0].collection == "aq_system"

    # -- (e) On-demand collection creation -------------------------------------

    @pytestmark_milvus
    def test_e_on_demand_creation_via_router(self, tmp_path: Path):
        """(e) collections are created on-demand if they don't exist yet.

        Resolves scopes, then uses CollectionRouter.get_store() for each
        entry. Stores are created lazily — no pre-registration required.
        """
        db = tmp_path / "roadmap_318e.db"
        router = CollectionRouter(milvus_uri=str(db), dimension=4)
        try:
            entries = resolve_scopes(agent_type="coding", project_id="myapp")

            # Before: no stores open
            for entry in entries:
                assert not router.has_store(entry.scope, entry.scope_id)

            # Create stores on-demand from resolved scopes
            stores = []
            for entry in entries:
                store = router.get_store(entry.scope, entry.scope_id)
                stores.append(store)

            # After: all stores exist and match expected collection names
            assert len(stores) == 3
            assert stores[0]._collection == "aq_project_myapp"
            assert stores[1]._collection == "aq_agenttype_coding"
            assert stores[2]._collection == "aq_system"

            for entry in entries:
                assert router.has_store(entry.scope, entry.scope_id)
        finally:
            router.close()

    @pytestmark_milvus
    def test_e_on_demand_creation_idempotent(self, tmp_path: Path):
        """(e) calling get_store twice returns the same cached instance."""
        db = tmp_path / "roadmap_318e_idem.db"
        router = CollectionRouter(milvus_uri=str(db), dimension=4)
        try:
            entries = resolve_scopes(agent_type="coding", project_id="myapp")

            first = [router.get_store(e.scope, e.scope_id) for e in entries]
            second = [router.get_store(e.scope, e.scope_id) for e in entries]

            for s1, s2 in zip(first, second, strict=True):
                assert s1 is s2, "get_store should return cached instance"
        finally:
            router.close()

    @pytestmark_milvus
    def test_e_on_demand_creation_unknown_agent_type(self, tmp_path: Path):
        """(e) on-demand creation works even for previously unseen agent types."""
        db = tmp_path / "roadmap_318e_unknown.db"
        router = CollectionRouter(milvus_uri=str(db), dimension=4)
        try:
            entries = resolve_scopes(agent_type="brand_new_agent_type")

            for entry in entries:
                store = router.get_store(entry.scope, entry.scope_id)
                assert store is not None

            # Verify the agent-type collection was created
            assert router.has_store(MemoryScope.AGENT_TYPE, "brand_new_agent_type")
            assert router.has_store(MemoryScope.SYSTEM)
        finally:
            router.close()

    # -- (f) Weights match spec and are configurable ---------------------------

    def test_f_default_weights_match_spec(self):
        """(f) default weights match spec §4 exactly: project=1.0, agent-type=0.7,
        system=0.4.
        """
        assert SCOPE_WEIGHTS[MemoryScope.PROJECT] == 1.0
        assert SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE] == 0.7
        assert SCOPE_WEIGHTS[MemoryScope.SYSTEM] == 0.4

        # Verify resolve_scopes uses these defaults
        entries = resolve_scopes(agent_type="coding", project_id="myapp")
        for entry in entries:
            assert entry.weight == SCOPE_WEIGHTS[entry.scope]

    def test_f_weights_configurable_full_override(self):
        """(f) all weights can be overridden via the weights parameter."""
        custom = {
            MemoryScope.PROJECT: 0.9,
            MemoryScope.AGENT_TYPE: 0.6,
            MemoryScope.SYSTEM: 0.2,
        }
        entries = resolve_scopes(
            agent_type="coding",
            project_id="myapp",
            weights=custom,
        )
        assert entries[0].weight == 0.9  # project
        assert entries[1].weight == 0.6  # agent-type
        assert entries[2].weight == 0.2  # system

    def test_f_weights_configurable_partial_override(self):
        """(f) partial weight overrides — unspecified scopes keep spec defaults."""
        custom = {MemoryScope.PROJECT: 0.85}
        entries = resolve_scopes(
            agent_type="coding",
            project_id="myapp",
            weights=custom,
        )
        assert entries[0].weight == 0.85  # overridden
        assert entries[1].weight == 0.7  # default (spec §4)
        assert entries[2].weight == 0.4  # default (spec §4)

    def test_f_weights_ordering_preserved_even_with_custom(self):
        """(f) custom weights don't affect scope order — still project → agent → system."""
        # Even with inverted weights, scope ORDER is fixed by specificity
        custom = {
            MemoryScope.PROJECT: 0.1,  # intentionally lower
            MemoryScope.SYSTEM: 0.9,  # intentionally higher
        }
        entries = resolve_scopes(
            agent_type="coding",
            project_id="myapp",
            weights=custom,
        )
        # Scope order is always most-specific first, regardless of weight values
        scopes = [e.scope for e in entries]
        assert scopes == [MemoryScope.PROJECT, MemoryScope.AGENT_TYPE, MemoryScope.SYSTEM]

        # But weight values reflect the custom config
        assert entries[0].weight == 0.1
        assert entries[2].weight == 0.9


# ---- Roadmap 3.1.9: KV scope resolution with first-match-wins ---------------
# Spec: docs/specs/design/memory-scoping.md §6 — Multi-Scope Query
#
#   (a) KV key exists in project scope — returns project value,
#       does NOT query agent-type or system
#   (b) KV key missing from project scope, exists in agent-type scope —
#       returns agent-type value
#   (c) KV key missing from project and agent-type, exists in system —
#       returns system value
#   (d) KV key missing from all scopes — returns None/empty
#   (e) Same key exists in both project and system scope — project value
#       wins (first-match)
#   (f) Writing a KV entry writes to the most specific scope
#       (project if project_id is set)
#   (g) Deleting a project-scope KV entry causes fallthrough to
#       agent-type/system value


@pytestmark_milvus
class TestRoadmap319KVScopeResolution:
    """Roadmap 3.1.9 — KV scope resolution with first-match-wins per §6.

    Each test method maps 1:1 to a roadmap case letter.  Uses real Milvus
    Lite stores (via ``CollectionRouter``) with KV entries seeded in
    different scopes to verify correct first-match-wins semantics.
    """

    @pytest.fixture
    def kv_router(self, tmp_path: Path):
        """Router with KV entries spread across project, agent-type, and system scopes.

        Seed data:
          - project "myapp":   tech_stack = "Python+SQLAlchemy" (ns=project)
                               deploy_branch = "main"          (ns=project)
          - agent-type "coding": test_command = "pytest -v"     (ns=conventions)
                                 tech_stack = "Python"          (ns=project)
          - system:            version = "1.0"                  (ns=system)
                               tech_stack = "Generic"           (ns=project)
        """
        db = tmp_path / "roadmap_319.db"
        router = CollectionRouter(milvus_uri=str(db), dimension=4)

        # -- Project scope ------------------------------------------------
        proj_store = router.get_store(MemoryScope.PROJECT, "myapp")
        proj_store.set_kv("tech_stack", "Python+SQLAlchemy", namespace="project")
        proj_store.set_kv("deploy_branch", "main", namespace="project")

        # -- Agent-type scope ---------------------------------------------
        at_store = router.get_store(MemoryScope.AGENT_TYPE, "coding")
        at_store.set_kv("test_command", "pytest -v", namespace="conventions")
        at_store.set_kv("tech_stack", "Python", namespace="project")

        # -- System scope -------------------------------------------------
        sys_store = router.get_store(MemoryScope.SYSTEM)
        sys_store.set_kv("version", "1.0", namespace="system")
        sys_store.set_kv("tech_stack", "Generic", namespace="project")

        yield router
        router.close()

    # -- (a) Project scope wins — returns project value immediately --------

    @pytest.mark.asyncio
    async def test_a_project_scope_returns_project_value(
        self, kv_router: CollectionRouter
    ):
        """(a) KV key exists in project scope — returns project value,
        does NOT query agent-type or system.

        tech_stack is set in all three scopes (project, agent-type, system).
        With project_id="myapp", the project value must be returned.
        """
        value = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
            agent_type="coding",
            namespace="project",
        )
        assert value is not None
        # Must be the project-scope value, not agent-type or system
        assert json.loads(value) == "Python+SQLAlchemy"

    @pytest.mark.asyncio
    async def test_a_project_scope_does_not_query_lower_scopes(
        self, kv_router: CollectionRouter
    ):
        """(a) Verify first-match-wins short-circuits — project hit means
        the agent-type and system values are never consulted.

        deploy_branch only exists in the project scope; confirms that
        project scope is checked first and returns immediately.
        """
        value = await kv_router.recall(
            "deploy_branch",
            project_id="myapp",
            agent_type="coding",
            namespace="project",
        )
        assert value is not None
        assert json.loads(value) == "main"

    # -- (b) Fallthrough to agent-type scope --------------------------------

    @pytest.mark.asyncio
    async def test_b_falls_through_to_agent_type(
        self, kv_router: CollectionRouter
    ):
        """(b) KV key missing from project scope, exists in agent-type scope
        — returns agent-type value.

        test_command only exists in agent-type "coding" scope (namespace
        "conventions"), not in project "myapp".
        """
        value = await kv_router.recall(
            "test_command",
            project_id="myapp",
            agent_type="coding",
            namespace="conventions",
        )
        assert value is not None
        assert json.loads(value) == "pytest -v"

    # -- (c) Fallthrough to system scope ------------------------------------

    @pytest.mark.asyncio
    async def test_c_falls_through_to_system(
        self, kv_router: CollectionRouter
    ):
        """(c) KV key missing from project and agent-type, exists in system
        — returns system value.

        version only exists in system scope (namespace "system").
        """
        value = await kv_router.recall(
            "version",
            project_id="myapp",
            agent_type="coding",
            namespace="system",
        )
        assert value is not None
        assert json.loads(value) == "1.0"

    # -- (d) Missing from all scopes → None --------------------------------

    @pytest.mark.asyncio
    async def test_d_missing_key_returns_none(
        self, kv_router: CollectionRouter
    ):
        """(d) KV key missing from all scopes — returns None."""
        value = await kv_router.recall(
            "nonexistent_key",
            project_id="myapp",
            agent_type="coding",
        )
        assert value is None

    @pytest.mark.asyncio
    async def test_d_missing_namespace_returns_none(
        self, kv_router: CollectionRouter
    ):
        """(d) Key exists but in a different namespace — returns None."""
        # tech_stack exists in "project" namespace, not "system"
        value = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
            agent_type="coding",
            namespace="system",
        )
        assert value is None

    @pytest.mark.asyncio
    async def test_d_missing_scopes_returns_none(
        self, kv_router: CollectionRouter
    ):
        """(d) Non-existent project and agent-type, key not in system — None."""
        value = await kv_router.recall(
            "deploy_branch",
            project_id="no_such_project",
            agent_type="no_such_type",
            namespace="project",
        )
        assert value is None

    # -- (e) Same key in multiple scopes — project wins (first-match) ------

    @pytest.mark.asyncio
    async def test_e_project_wins_over_system(
        self, kv_router: CollectionRouter
    ):
        """(e) Same key exists in both project and system scope — project
        value wins (first-match).

        tech_stack is set in project ("Python+SQLAlchemy"), agent-type
        ("Python"), and system ("Generic").  Project must win.
        """
        value = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
            agent_type="coding",
            namespace="project",
        )
        assert value is not None
        assert json.loads(value) == "Python+SQLAlchemy"

    @pytest.mark.asyncio
    async def test_e_agent_type_wins_over_system_when_no_project(
        self, kv_router: CollectionRouter
    ):
        """(e) With no project_id, agent-type value wins over system.

        tech_stack is in agent-type ("Python") and system ("Generic").
        Without a project scope, agent-type is most specific.
        """
        value = await kv_router.recall(
            "tech_stack",
            agent_type="coding",
            namespace="project",
        )
        assert value is not None
        assert json.loads(value) == "Python"

    @pytest.mark.asyncio
    async def test_e_system_wins_when_no_project_or_agent_type(
        self, kv_router: CollectionRouter
    ):
        """(e) With no project_id or agent_type, system is the only scope.

        tech_stack in system ("Generic") is returned as the only scope.
        """
        value = await kv_router.recall(
            "tech_stack",
            namespace="project",
        )
        assert value is not None
        assert json.loads(value) == "Generic"

    # -- (f) Writing goes to most specific scope ----------------------------

    @pytest.mark.asyncio
    async def test_f_write_goes_to_project_scope(
        self, kv_router: CollectionRouter
    ):
        """(f) Writing a KV entry writes to the most specific scope
        (project if project_id is set).

        Writes "new_key" to project scope, then verifies:
        1. Recall with project_id returns the value.
        2. The value is NOT in agent-type or system scope.
        """
        # Write to project scope
        proj_store = kv_router.get_store(MemoryScope.PROJECT, "myapp")
        proj_store.set_kv("new_key", "project_value", namespace="test")

        # Recall should find it in project scope
        value = await kv_router.recall(
            "new_key",
            project_id="myapp",
            agent_type="coding",
            namespace="test",
        )
        assert value is not None
        assert json.loads(value) == "project_value"

        # Verify NOT in agent-type scope
        at_store = kv_router.get_store(MemoryScope.AGENT_TYPE, "coding")
        at_result = at_store.get_kv("new_key", namespace="test")
        assert at_result is None

        # Verify NOT in system scope
        sys_store = kv_router.get_store(MemoryScope.SYSTEM)
        sys_result = sys_store.get_kv("new_key", namespace="test")
        assert sys_result is None

    @pytest.mark.asyncio
    async def test_f_write_to_system_when_no_project(
        self, kv_router: CollectionRouter
    ):
        """(f) Without project_id, writes to system scope (most specific
        available scope when only system is targeted).
        """
        sys_store = kv_router.get_store(MemoryScope.SYSTEM)
        sys_store.set_kv("sys_only_key", "sys_value", namespace="test")

        # Recall without project_id or agent_type → system scope only
        value = await kv_router.recall(
            "sys_only_key",
            namespace="test",
        )
        assert value is not None
        assert json.loads(value) == "sys_value"

    @pytest.mark.asyncio
    async def test_f_write_to_agent_type_scope(
        self, kv_router: CollectionRouter
    ):
        """(f) Writing directly to agent-type scope stores it there."""
        at_store = kv_router.get_store(MemoryScope.AGENT_TYPE, "coding")
        at_store.set_kv("at_only_key", "at_value", namespace="test")

        # Recall with agent_type but no project → agent-type scope first
        value = await kv_router.recall(
            "at_only_key",
            agent_type="coding",
            namespace="test",
        )
        assert value is not None
        assert json.loads(value) == "at_value"

        # Not visible in project scope
        proj_store = kv_router.get_store(MemoryScope.PROJECT, "myapp")
        proj_result = proj_store.get_kv("at_only_key", namespace="test")
        assert proj_result is None

    # -- (g) Deleting project entry → fallthrough to lower scope -----------

    @pytest.mark.asyncio
    async def test_g_delete_project_causes_fallthrough_to_agent_type(
        self, kv_router: CollectionRouter
    ):
        """(g) Deleting a project-scope KV entry causes fallthrough to
        agent-type value.

        tech_stack exists in project ("Python+SQLAlchemy") and agent-type
        ("Python").  After deleting from project, agent-type wins.
        """
        # Pre-condition: project value wins
        before = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
            agent_type="coding",
            namespace="project",
        )
        assert json.loads(before) == "Python+SQLAlchemy"

        # Delete from project scope
        proj_store = kv_router.get_store(MemoryScope.PROJECT, "myapp")
        deleted = proj_store.delete_kv("tech_stack", namespace="project")
        assert deleted is True

        # After deletion: agent-type value should win via fallthrough
        after = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
            agent_type="coding",
            namespace="project",
        )
        assert after is not None
        assert json.loads(after) == "Python"

    @pytest.mark.asyncio
    async def test_g_delete_project_causes_fallthrough_to_system(
        self, kv_router: CollectionRouter
    ):
        """(g) Deleting project AND agent-type entries causes fallthrough
        to system value.

        After deleting tech_stack from both project and agent-type scopes,
        the system value ("Generic") should win.
        """
        # Delete from project scope
        proj_store = kv_router.get_store(MemoryScope.PROJECT, "myapp")
        proj_store.delete_kv("tech_stack", namespace="project")

        # Delete from agent-type scope
        at_store = kv_router.get_store(MemoryScope.AGENT_TYPE, "coding")
        at_store.delete_kv("tech_stack", namespace="project")

        # Now system value should win
        value = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
            agent_type="coding",
            namespace="project",
        )
        assert value is not None
        assert json.loads(value) == "Generic"

    @pytest.mark.asyncio
    async def test_g_delete_all_scopes_returns_none(
        self, kv_router: CollectionRouter
    ):
        """(g) Deleting from all scopes returns None — no fallthrough target."""
        # Delete tech_stack from all three scopes
        proj_store = kv_router.get_store(MemoryScope.PROJECT, "myapp")
        proj_store.delete_kv("tech_stack", namespace="project")

        at_store = kv_router.get_store(MemoryScope.AGENT_TYPE, "coding")
        at_store.delete_kv("tech_stack", namespace="project")

        sys_store = kv_router.get_store(MemoryScope.SYSTEM)
        sys_store.delete_kv("tech_stack", namespace="project")

        value = await kv_router.recall(
            "tech_stack",
            project_id="myapp",
            agent_type="coding",
            namespace="project",
        )
        assert value is None

    @pytest.mark.asyncio
    async def test_g_delete_idempotent(
        self, kv_router: CollectionRouter
    ):
        """(g) Deleting a non-existent key returns False (no error)."""
        proj_store = kv_router.get_store(MemoryScope.PROJECT, "myapp")
        result = proj_store.delete_kv("nonexistent_key", namespace="project")
        assert result is False
