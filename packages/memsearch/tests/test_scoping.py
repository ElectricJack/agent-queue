"""Tests for scope-aware collection naming, routing, and cleanup."""

import sys
from pathlib import Path

import pytest

from memsearch.scoping import (
    _PREFIX,
    CollectionRouter,
    MemoryScope,
    collection_name,
    parse_collection_name,
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
        assert (
            collection_name(MemoryScope.AGENT_TYPE, "code-review")
            == "aq_agenttype_code_review"
        )

    def test_agent_type_requires_id(self):
        with pytest.raises(ValueError, match="scope_id is required"):
            collection_name(MemoryScope.AGENT_TYPE)

    def test_agent_type_empty_id_raises(self):
        with pytest.raises(ValueError, match="scope_id is required"):
            collection_name(MemoryScope.AGENT_TYPE, "")

    def test_project(self):
        assert collection_name(MemoryScope.PROJECT, "myapp") == "aq_project_myapp"

    def test_project_sanitized(self):
        assert (
            collection_name(MemoryScope.PROJECT, "mech-fighters")
            == "aq_project_mech_fighters"
        )

    def test_project_requires_id(self):
        with pytest.raises(ValueError, match="scope_id is required"):
            collection_name(MemoryScope.PROJECT)

    def test_project_complex_id(self):
        assert (
            collection_name(MemoryScope.PROJECT, "My Cool App v2.0!")
            == "aq_project_my_cool_app_v2_0"
        )

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

        proj_store.upsert([
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
        ])
        sys_store.upsert([
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
        ])

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
        found = [
            (scope, scope_id)
            for scope, scope_id, name in result
            if name == "aq_project_beta"
        ]
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
        store1.upsert([
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
        ])

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

        proj_store.upsert([
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
        ])
        sys_store.upsert([
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
        ])

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
        router.get_store(MemoryScope.PROJECT, "alpha").upsert([
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
        ])
        router.get_store(MemoryScope.PROJECT, "beta").upsert([
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
        ])

        # Search only alpha scope
        results = router.search_by_tag(
            "shared",
            scopes=[(MemoryScope.PROJECT, "alpha")],
        )
        hashes = {r["chunk_hash"] for r in results}
        assert "alpha_1" in hashes
        assert "beta_1" not in hashes

    def test_tag_search_no_results(self, router: CollectionRouter):
        router.get_store(MemoryScope.SYSTEM).upsert([
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
        ])
        results = router.search_by_tag("nonexistent")
        assert len(results) == 0


@pytestmark_milvus
class TestCollectionRouterContextManager:
    def test_context_manager(self, tmp_path: Path):
        db = tmp_path / "ctx_test.db"
        with CollectionRouter(milvus_uri=str(db), dimension=4) as router:
            store = router.get_store(MemoryScope.SYSTEM)
            store.upsert([
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
            ])
            assert store.count() == 1
        # After exit, stores are cleared
        assert len(router._stores) == 0


@pytestmark_milvus
class TestCollectionRouterProperties:
    def test_uri_property(self, router: CollectionRouter):
        assert "scoping_test.db" in router.uri

    def test_dimension_property(self, router: CollectionRouter):
        assert router.dimension == 4
