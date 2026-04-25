"""Tests for the ``memory_scope_id`` shared-scope mechanism.

A profile can set ``memory_scope_id`` to redirect its agent-type memory
scope to a shared pool.  Two profiles setting the same value
(e.g. ``claude-opus`` and ``claude-sonnet`` both set ``'claude'``) write
to and read from a single Milvus collection and vault directory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.models import AgentProfile
from src.plugins.internal.memory.service import MemoryService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embedder():
    emb = MagicMock()
    emb.model_name = "test-model"
    emb.dimension = 384
    emb.embed = AsyncMock(return_value=[[0.1] * 384])
    return emb


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.count.return_value = 0
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False
    store.upsert.return_value = 1
    store.get.return_value = None
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
def tmp_data_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def service(mock_embedder, mock_router, tmp_data_dir):
    svc = MemoryService(
        milvus_uri="/tmp/test.db",
        embedding_provider="openai",
        data_dir=tmp_data_dir,
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


# ---------------------------------------------------------------------------
# _resolve_scope: alias behaviour
# ---------------------------------------------------------------------------


class TestResolveScopeAlias:
    def test_no_alias_preserves_agent_type(self, service):
        """Without an alias map, agenttype_X resolves to itself."""
        from memsearch import MemoryScope

        mem_scope, scope_id = service._resolve_scope("proj-a", scope="agenttype_coding")
        assert mem_scope == MemoryScope.AGENT_TYPE
        assert scope_id == "coding"

    def test_alias_redirects_agent_type(self, service):
        """An alias redirects the agent-type id at resolve time."""
        from memsearch import MemoryScope

        service.set_scope_alias_map(
            {"claude-opus": "claude", "claude-sonnet": "claude"}
        )

        mem_scope, scope_id = service._resolve_scope(
            "proj-a", scope="agenttype_claude-opus"
        )
        assert mem_scope == MemoryScope.AGENT_TYPE
        assert scope_id == "claude"

        mem_scope, scope_id = service._resolve_scope(
            "proj-a", scope="agenttype_claude-sonnet"
        )
        assert mem_scope == MemoryScope.AGENT_TYPE
        assert scope_id == "claude"

    def test_alias_identity_is_noop(self, service):
        """Alias that equals the key has no effect."""
        from memsearch import MemoryScope

        service.set_scope_alias_map({"claude-code": "claude-code"})

        mem_scope, scope_id = service._resolve_scope(
            "proj-a", scope="agenttype_claude-code"
        )
        assert mem_scope == MemoryScope.AGENT_TYPE
        assert scope_id == "claude-code"

    def test_direct_alias_target_scope(self, service):
        """Calling agenttype_{target} directly still works (no profile for target)."""
        from memsearch import MemoryScope

        service.set_scope_alias_map({"claude-opus": "claude"})

        # There's no profile "claude" — the scope is used as-is.
        mem_scope, scope_id = service._resolve_scope(
            "proj-a", scope="agenttype_claude"
        )
        assert mem_scope == MemoryScope.AGENT_TYPE
        assert scope_id == "claude"

    def test_non_agenttype_scopes_untouched(self, service):
        """System/supervisor/project scopes are unaffected by the alias map."""
        from memsearch import MemoryScope

        service.set_scope_alias_map({"claude-opus": "claude"})

        assert service._resolve_scope("p", scope="system") == (MemoryScope.SYSTEM, None)
        assert service._resolve_scope("p", scope="supervisor") == (
            MemoryScope.SUPERVISOR,
            None,
        )
        assert service._resolve_scope("p", scope="project_foo") == (
            MemoryScope.PROJECT,
            "foo",
        )


# ---------------------------------------------------------------------------
# set_scope_alias_map: hygiene
# ---------------------------------------------------------------------------


class TestSetScopeAliasMap:
    def test_overwrites_previous_map(self, service):
        from memsearch import MemoryScope

        service.set_scope_alias_map({"a": "shared"})
        service.set_scope_alias_map({"b": "other-shared"})

        # New map is authoritative — old entries are gone.
        mem, sid = service._resolve_scope("p", scope="agenttype_a")
        assert sid == "a"  # no longer aliased

        mem, sid = service._resolve_scope("p", scope="agenttype_b")
        assert sid == "other-shared"

    def test_empty_map_clears_all(self, service):
        service.set_scope_alias_map({"a": "shared"})
        service.set_scope_alias_map({})

        mem, sid = service._resolve_scope("p", scope="agenttype_a")
        assert sid == "a"


# ---------------------------------------------------------------------------
# AgentProfile model carries the field
# ---------------------------------------------------------------------------


class TestAgentProfileField:
    def test_default_is_none(self):
        profile = AgentProfile(id="x", name="X")
        assert profile.memory_scope_id is None

    def test_can_set_field(self):
        profile = AgentProfile(id="claude-opus", name="Opus", memory_scope_id="claude")
        assert profile.memory_scope_id == "claude"


# ---------------------------------------------------------------------------
# Profile parser picks up the field from frontmatter
# ---------------------------------------------------------------------------


class TestProfileParserMemoryScopeId:
    def test_frontmatter_memory_scope_id_flows_through(self):
        from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile

        text = """\
---
id: claude-opus
name: Claude Opus
memory_scope_id: claude
---

## Role
Judgment-heavy reasoning.

## Config
```json
{"model": "claude-opus-4-7"}
```
"""
        parsed = parse_profile(text)
        assert parsed.is_valid

        profile_dict = parsed_profile_to_agent_profile(parsed)
        assert profile_dict["memory_scope_id"] == "claude"

    def test_missing_field_is_absent(self):
        from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile

        text = """\
---
id: plain-profile
name: Plain
---

## Role
Nothing special.
"""
        parsed = parse_profile(text)
        assert parsed.is_valid

        profile_dict = parsed_profile_to_agent_profile(parsed)
        assert "memory_scope_id" not in profile_dict
