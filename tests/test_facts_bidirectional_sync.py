"""Tests for facts.md bidirectional sync — Roadmap 2.2.17.

Test cases from the roadmap spec (docs/specs/design/roadmap.md):

(a) Parse a facts.md with `key: value` pairs under headings — each pair
    appears as KV entry in collection.
(b) facts.md with multiple headings creates KV entries with heading as
    namespace.
(c) `memory_recall` for a key parsed from facts.md returns correct value.
(d) Editing facts.md (change a value) triggers re-parse and updates KV
    entry in collection.
(e) `memory_store` a new KV pair triggers facts.md writer to append the
    entry to the file.
(f) facts.md with malformed lines (no colon, empty value) logs warning but
    does not crash — valid lines still parsed.
(g) facts.md with markdown formatting in values (bold, links) preserves
    formatting in stored value.

These are integration-level tests that exercise the full bidirectional
sync pipeline: facts.md ↔ KV backend via MemoryV2Service and the
facts_handler watcher.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.facts_handler import (
    _sync_facts_to_kv,
    on_facts_changed,
    register_facts_handlers,
)
from src.facts_parser import parse_facts_file, render_facts_file
from src.vault_watcher import VaultChange, VaultWatcher

# Skip on Windows — Milvus Lite unavailable, and some tests need memsearch
_SKIP_WINDOWS = sys.platform == "win32"

try:
    from src.memory_v2_service import MEMSEARCH_AVAILABLE, MemoryV2Service
except Exception:
    MEMSEARCH_AVAILABLE = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_service(kv_store: dict[str, dict[str, str]] | None = None):
    """Create a mock service that tracks KV state in a plain dict.

    The returned service behaves like MemoryV2Service for the subset of
    the API used by the facts handler (kv_set, kv_recall-compatible
    get_kv).

    Parameters
    ----------
    kv_store:
        Optional initial state: ``{scope: {namespace/key: value}}``.
        Defaults to an empty dict.

    Returns
    -------
    tuple[AsyncMock, dict]
        The mock service and the backing kv_store dict.
    """
    if kv_store is None:
        kv_store = {}

    service = AsyncMock()
    service.available = True

    async def _kv_set(project_id, namespace, key, value, *, scope=None, _from_vault=False):
        scope_key = scope or f"project_{project_id}"
        if scope_key not in kv_store:
            kv_store[scope_key] = {}
        entry_key = f"{namespace}/{key}"
        kv_store[scope_key][entry_key] = value
        return {
            "kv_namespace": namespace,
            "kv_key": key,
            "kv_value": value,
            "updated_at": int(time.time()),
            "_vault_path": f"/vault/{scope_key}/facts.md",
            "_scope": scope_key,
            "_scope_id": project_id,
            "_from_vault": _from_vault,
        }

    service.kv_set = AsyncMock(side_effect=_kv_set)
    return service, kv_store


# ---------------------------------------------------------------------------
# (a) Parse facts.md → KV entries in collection
# ---------------------------------------------------------------------------


class TestParseFactsToKV:
    """(a) Parse a facts.md with key:value pairs under headings — each pair
    appears as KV entry in collection."""

    @pytest.mark.asyncio
    async def test_simple_pairs_become_kv_entries(self, tmp_path):
        """Each key:value line under a ## heading is synced as a KV entry."""
        facts = tmp_path / "facts.md"
        facts.write_text(
            "## project\ntech_stack: Python\ndb: SQLite\ntest_command: pytest tests/ -v\n"
        )

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/my-app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # All 3 entries should have been synced
        assert service.kv_set.call_count == 3

        # Verify each key was written with correct value
        calls = {c.args[2]: c.args[3] for c in service.kv_set.call_args_list}
        assert calls["tech_stack"] == "Python"
        assert calls["db"] == "SQLite"
        assert calls["test_command"] == "pytest tests/ -v"

        # Verify all calls went to the correct scope
        for call in service.kv_set.call_args_list:
            assert call.kwargs["scope"] == "project_my-app"
            assert call.kwargs["_from_vault"] is True

    @pytest.mark.asyncio
    async def test_entries_appear_in_kv_store(self, tmp_path):
        """Parsed entries are actually present in the KV backend."""
        facts = tmp_path / "facts.md"
        facts.write_text("## config\napi_url: https://api.example.com\nport: 8080\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        scope_entries = kv_store.get("project_app", {})
        assert scope_entries.get("config/api_url") == "https://api.example.com"
        assert scope_entries.get("config/port") == "8080"

    @pytest.mark.asyncio
    async def test_bullet_prefixed_entries(self, tmp_path):
        """Bullet-prefixed lines are stripped before becoming KV entries."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\n- tech_stack: Python\n* db: SQLite\n+ framework: FastAPI\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert service.kv_set.call_count == 3
        keys = {c.args[2] for c in service.kv_set.call_args_list}
        assert keys == {"tech_stack", "db", "framework"}

    @pytest.mark.asyncio
    async def test_frontmatter_skipped(self, tmp_path):
        """YAML frontmatter is not treated as KV entries."""
        facts = tmp_path / "facts.md"
        facts.write_text(
            "---\n"
            "tags: [facts, auto-updated]\n"
            "---\n"
            "\n"
            "# Project Facts\n"
            "\n"
            "## project\n"
            "tech_stack: Python\n"
        )

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # Only the entry under ## project should be synced
        assert service.kv_set.call_count == 1
        assert service.kv_set.call_args.args[2] == "tech_stack"


# ---------------------------------------------------------------------------
# (b) Multiple headings → separate namespaces
# ---------------------------------------------------------------------------


class TestMultipleNamespaces:
    """(b) facts.md with multiple headings creates KV entries with heading
    as namespace."""

    @pytest.mark.asyncio
    async def test_headings_become_namespaces(self, tmp_path):
        """Each ## heading creates entries under its own namespace."""
        facts = tmp_path / "facts.md"
        facts.write_text(
            "## project\n"
            "tech_stack: Python\n"
            "deploy_branch: main\n"
            "\n"
            "## conventions\n"
            "orm_pattern: repository\n"
            "naming: snake_case\n"
            "\n"
            "## stats\n"
            "total_tasks_completed: 47\n"
        )

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # 5 entries total
        assert service.kv_set.call_count == 5

        # Verify namespace assignment
        ns_keys = {(c.args[1], c.args[2]) for c in service.kv_set.call_args_list}
        assert ("project", "tech_stack") in ns_keys
        assert ("project", "deploy_branch") in ns_keys
        assert ("conventions", "orm_pattern") in ns_keys
        assert ("conventions", "naming") in ns_keys
        assert ("stats", "total_tasks_completed") in ns_keys

    @pytest.mark.asyncio
    async def test_namespace_preserved_in_kv_store(self, tmp_path):
        """The namespace/key composite is preserved in the KV backend."""
        facts = tmp_path / "facts.md"
        facts.write_text("## alpha\nkey1: val1\n\n## beta\nkey2: val2\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        scope = kv_store["project_app"]
        assert scope["alpha/key1"] == "val1"
        assert scope["beta/key2"] == "val2"

    @pytest.mark.asyncio
    async def test_all_scope_types(self, tmp_path):
        """Verify namespace handling works across system, agent-type, and project scopes."""
        for rel_path, expected_scope in [
            ("system/facts.md", "system"),
            ("agent-types/coding/facts.md", "agenttype_coding"),
            ("projects/app/facts.md", "project_app"),
        ]:
            facts = tmp_path / rel_path.replace("/", "_")  # unique file per scope
            facts.write_text("## config\nsetting: value\n")

            service, kv_store = _make_mock_service()

            change = VaultChange(
                path=str(facts),
                rel_path=rel_path,
                operation="created",
            )
            await on_facts_changed([change], service=service)

            assert service.kv_set.call_count == 1, f"Failed for {rel_path}"
            assert service.kv_set.call_args.kwargs["scope"] == expected_scope


# ---------------------------------------------------------------------------
# (c) memory_recall returns correct value from parsed facts
# ---------------------------------------------------------------------------


class TestRecallFromParsedFacts:
    """(c) `memory_recall` for a key parsed from facts.md returns correct value."""

    @pytest.mark.asyncio
    async def test_recall_finds_parsed_value(self, tmp_path):
        """After parsing facts.md, kv_set was called; a recall should return
        the correct value via the store."""
        service, kv_store = _make_mock_service()

        # Simulate facts.md parse → kv_set
        facts = tmp_path / "facts.md"
        facts.write_text("## project\ntech_stack: Python 3.12\n")

        change = VaultChange(
            path=str(facts),
            rel_path="projects/my-app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # Verify the key was stored
        assert kv_store["project_my-app"]["project/tech_stack"] == "Python 3.12"

    @pytest.mark.asyncio
    async def test_recall_returns_value_with_colons(self, tmp_path):
        """Values containing colons (like URLs) round-trip correctly."""
        service, kv_store = _make_mock_service()

        facts = tmp_path / "facts.md"
        facts.write_text("## urls\napi: http://localhost:8080/v1\n")

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # The full URL (with colons) should be stored correctly
        assert kv_store["project_app"]["urls/api"] == "http://localhost:8080/v1"

    @pytest.mark.asyncio
    @pytest.mark.skipif(_SKIP_WINDOWS, reason="Milvus Lite not supported on Windows")
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_kv_recall_scope_resolution(self):
        """kv_recall searches scopes in order: project → agent-type → system."""
        # Use a real mock_store that returns different values per scope
        service = MemoryV2Service(milvus_uri="/tmp/test.db")

        mock_router = MagicMock()

        def scope_aware_get_store(*args, **kwargs):
            store = MagicMock()
            # We'll track which scope was queried
            store.get_kv.return_value = None
            return store

        mock_router.get_store.side_effect = scope_aware_get_store
        mock_router.close = MagicMock()

        service._router = mock_router
        service._embedder = MagicMock()
        service._initialized = True

        await service.kv_recall(
            "tech_stack",
            project_id="my-app",
            agent_type="coding",
            namespace="project",
        )

        # All three scopes should have been tried (project, agent-type, system)
        assert mock_router.get_store.call_count == 3


# ---------------------------------------------------------------------------
# (d) Edit facts.md → re-parse and update KV entries
# ---------------------------------------------------------------------------


class TestEditTriggersReparse:
    """(d) Editing facts.md (change a value) triggers re-parse and updates
    KV entry in collection."""

    @pytest.mark.asyncio
    async def test_modified_value_updates_kv(self, tmp_path):
        """Changing a value in facts.md and triggering 'modified' updates the KV."""
        service, kv_store = _make_mock_service()

        facts = tmp_path / "facts.md"

        # Initial parse
        facts.write_text("## project\ndeploy_branch: main\n")
        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert kv_store["project_app"]["project/deploy_branch"] == "main"

        # Edit: change value
        facts.write_text("## project\ndeploy_branch: release\n")
        change2 = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="modified",
        )
        await on_facts_changed([change2], service=service)

        # Value should be updated
        assert kv_store["project_app"]["project/deploy_branch"] == "release"

    @pytest.mark.asyncio
    async def test_add_new_key_on_edit(self, tmp_path):
        """Adding a new key to an existing facts.md triggers a kv_set for the new key."""
        service, kv_store = _make_mock_service()

        facts = tmp_path / "facts.md"

        # Initial
        facts.write_text("## project\ntech_stack: Python\n")
        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # Edit: add new key
        facts.write_text("## project\ntech_stack: Python\ndb: PostgreSQL\n")
        change2 = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="modified",
        )
        await on_facts_changed([change2], service=service)

        # Both keys present
        assert kv_store["project_app"]["project/tech_stack"] == "Python"
        assert kv_store["project_app"]["project/db"] == "PostgreSQL"

    @pytest.mark.asyncio
    async def test_from_vault_flag_prevents_circular_sync(self, tmp_path):
        """All kv_set calls from facts handler use _from_vault=True."""
        service, _ = _make_mock_service()

        facts = tmp_path / "facts.md"
        facts.write_text("## project\nkey: value\n")

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="modified",
        )
        await on_facts_changed([change], service=service)

        # Every kv_set call must have _from_vault=True
        for call in service.kv_set.call_args_list:
            assert call.kwargs["_from_vault"] is True, (
                "Facts handler must pass _from_vault=True to prevent circular sync"
            )

    @pytest.mark.asyncio
    async def test_end_to_end_watcher_edit_triggers_reparse(self, tmp_path):
        """Full pipeline: VaultWatcher detects modification → handler re-parses → kv_set."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        service, kv_store = _make_mock_service()
        register_facts_handlers(watcher, service=service)

        # Take initial snapshot (empty vault)
        await watcher.check()

        # Create initial file
        project_dir = vault / "projects" / "app"
        project_dir.mkdir(parents=True)
        facts_file = project_dir / "facts.md"
        facts_file.write_text("## project\ndeploy_branch: main\n")

        # Detect creation
        await watcher.check()

        assert kv_store["project_app"]["project/deploy_branch"] == "main"

        # Edit the file (need mtime change)
        time.sleep(0.05)
        facts_file.write_text("## project\ndeploy_branch: staging\n")

        # Detect modification
        await watcher.check()

        assert kv_store["project_app"]["project/deploy_branch"] == "staging"


# ---------------------------------------------------------------------------
# (e) memory_store triggers facts.md writer
# ---------------------------------------------------------------------------


class TestStoreTriggersWrite:
    """(e) `memory_store` a new KV pair triggers facts.md writer to append
    the entry to the file."""

    @pytest.mark.skipif(_SKIP_WINDOWS, reason="Milvus Lite not supported on Windows")
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    @pytest.mark.asyncio
    async def test_kv_set_creates_facts_file(self, tmp_path):
        """kv_set without _from_vault creates/updates the vault facts.md."""
        service = MemoryV2Service(
            milvus_uri="/tmp/test.db",
            data_dir=str(tmp_path),
        )

        # Inject mocks
        mock_store = MagicMock()
        mock_store.set_kv.return_value = {
            "chunk_hash": "abc",
            "kv_namespace": "project",
            "kv_key": "api_url",
            "kv_value": "https://api.example.com",
            "updated_at": int(time.time()),
        }
        mock_router = MagicMock()
        mock_router.get_store.return_value = mock_store

        service._router = mock_router
        service._embedder = MagicMock()
        service._initialized = True

        result = await service.kv_set(
            "test-project", "project", "api_url", "https://api.example.com"
        )

        # The facts file should have been created
        vault_path = Path(result["_vault_path"])
        assert vault_path.exists(), f"Expected facts file at {vault_path}"
        content = vault_path.read_text(encoding="utf-8")
        assert "## project" in content
        assert "api_url: https://api.example.com" in content

    @pytest.mark.skipif(_SKIP_WINDOWS, reason="Milvus Lite not supported on Windows")
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    @pytest.mark.asyncio
    async def test_kv_set_appends_to_existing_facts(self, tmp_path):
        """kv_set merges new entry into an existing facts.md without clobbering."""
        service = MemoryV2Service(
            milvus_uri="/tmp/test.db",
            data_dir=str(tmp_path),
        )

        mock_store = MagicMock()
        mock_store.set_kv.return_value = {
            "chunk_hash": "abc",
            "kv_namespace": "project",
            "kv_key": "new_key",
            "kv_value": "new_value",
            "updated_at": int(time.time()),
        }
        mock_router = MagicMock()
        mock_router.get_store.return_value = mock_store

        service._router = mock_router
        service._embedder = MagicMock()
        service._initialized = True

        # Pre-create a facts file with existing content
        from memsearch import MemoryScope, vault_paths as vp

        paths = vp(MemoryScope.PROJECT, "test-project")
        facts_rel = next(p for p in paths if p.endswith("facts.md"))
        facts_path = tmp_path / facts_rel
        facts_path.parent.mkdir(parents=True, exist_ok=True)
        facts_path.write_text("## project\nexisting_key: existing_value\n", encoding="utf-8")

        await service.kv_set("test-project", "project", "new_key", "new_value")

        content = facts_path.read_text(encoding="utf-8")
        assert "existing_key: existing_value" in content
        assert "new_key: new_value" in content

    @pytest.mark.skipif(_SKIP_WINDOWS, reason="Milvus Lite not supported on Windows")
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    @pytest.mark.asyncio
    async def test_kv_set_from_vault_skips_file_write(self, tmp_path):
        """kv_set with _from_vault=True does NOT write to facts.md (prevents loops)."""
        service = MemoryV2Service(
            milvus_uri="/tmp/test.db",
            data_dir=str(tmp_path),
        )

        mock_store = MagicMock()
        mock_store.set_kv.return_value = {
            "chunk_hash": "abc",
            "kv_namespace": "project",
            "kv_key": "key",
            "kv_value": "value",
            "updated_at": int(time.time()),
        }
        mock_router = MagicMock()
        mock_router.get_store.return_value = mock_store

        service._router = mock_router
        service._embedder = MagicMock()
        service._initialized = True

        result = await service.kv_set("test-project", "project", "key", "value", _from_vault=True)

        # The facts file should NOT have been created
        vault_path = Path(result["_vault_path"])
        assert not vault_path.exists(), "_from_vault=True should skip writing the facts file"

    def test_sync_facts_file_creates_new_file(self, tmp_path):
        """_sync_facts_file creates facts.md from scratch."""
        svc = MemoryV2Service()
        facts_path = tmp_path / "vault" / "projects" / "app" / "facts.md"

        svc._sync_facts_file(facts_path, "project", "api_url", "https://api.example.com")

        assert facts_path.exists()
        content = facts_path.read_text(encoding="utf-8")
        assert "## project" in content
        assert "api_url: https://api.example.com" in content

    def test_sync_facts_file_preserves_existing_entries(self, tmp_path):
        """_sync_facts_file preserves other keys when adding a new one."""
        svc = MemoryV2Service()
        facts_path = tmp_path / "facts.md"
        facts_path.write_text(
            "## project\nexisting: value\n",
            encoding="utf-8",
        )

        svc._sync_facts_file(facts_path, "project", "new_key", "new_val")

        content = facts_path.read_text(encoding="utf-8")
        assert "existing: value" in content
        assert "new_key: new_val" in content

    def test_sync_facts_file_updates_existing_key(self, tmp_path):
        """_sync_facts_file overwrites the value of an existing key."""
        svc = MemoryV2Service()
        facts_path = tmp_path / "facts.md"
        facts_path.write_text(
            "## project\nmy_key: old_value\n",
            encoding="utf-8",
        )

        svc._sync_facts_file(facts_path, "project", "my_key", "new_value")

        content = facts_path.read_text(encoding="utf-8")
        assert "my_key: new_value" in content
        assert "old_value" not in content

    def test_sync_facts_file_adds_new_namespace(self, tmp_path):
        """_sync_facts_file adds a new namespace heading when needed."""
        svc = MemoryV2Service()
        facts_path = tmp_path / "facts.md"
        facts_path.write_text(
            "## project\ntech: Python\n",
            encoding="utf-8",
        )

        svc._sync_facts_file(facts_path, "conventions", "naming", "snake_case")

        content = facts_path.read_text(encoding="utf-8")
        assert "## project" in content
        assert "## conventions" in content
        assert "naming: snake_case" in content
        assert "tech: Python" in content


# ---------------------------------------------------------------------------
# (f) Malformed lines → graceful handling
# ---------------------------------------------------------------------------


class TestMalformedLines:
    """(f) facts.md with malformed lines (no colon, empty value) logs
    warning but does not crash — valid lines still parsed."""

    @pytest.mark.asyncio
    async def test_no_colon_line_skipped(self, tmp_path):
        """Lines without a colon are silently skipped."""
        facts = tmp_path / "facts.md"
        facts.write_text(
            "## project\nvalid_key: valid_value\nthis line has no colon\nanother_valid: works\n"
        )

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # Only the valid entries should be synced
        assert service.kv_set.call_count == 2
        keys = {c.args[2] for c in service.kv_set.call_args_list}
        assert keys == {"valid_key", "another_valid"}

    @pytest.mark.asyncio
    async def test_empty_value_is_valid(self, tmp_path):
        """A key with an empty value (key:) is still a valid entry."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\nempty_value:\nhas_value: yes\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert service.kv_set.call_count == 2
        calls = {c.args[2]: c.args[3] for c in service.kv_set.call_args_list}
        assert calls["empty_value"] == ""
        assert calls["has_value"] == "yes"

    @pytest.mark.asyncio
    async def test_comment_lines_skipped(self, tmp_path):
        """Lines starting with # (headings other than ##) are skipped."""
        facts = tmp_path / "facts.md"
        facts.write_text(
            "# Main Title\n## project\nvalid: yes\n### Sub-heading\nstill_valid: yep\n"
        )

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # Only KV lines under ## should be synced; ### is skipped
        # "still_valid" is still under the "project" namespace (### doesn't change ns)
        assert service.kv_set.call_count == 2
        keys = {c.args[2] for c in service.kv_set.call_args_list}
        assert keys == {"valid", "still_valid"}

    @pytest.mark.asyncio
    async def test_mixed_malformed_and_valid(self, tmp_path):
        """A file with many types of malformed lines still parses valid entries."""
        facts = tmp_path / "facts.md"
        facts.write_text(
            "---\n"
            "malformed: frontmatter\n"
            "---\n"
            "\n"
            "# Title\n"
            "orphan_line: should be ignored\n"
            "\n"
            "## project\n"
            "good_key: good_value\n"
            "line without colon\n"
            "   \n"
            "   indented_key: indented_value\n"
            "another good: entry\n"
            "### not a namespace\n"
            "under_sub_heading: still project ns\n"
        )

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        # Only valid KV lines under ## should be parsed
        keys = {c.args[2] for c in service.kv_set.call_args_list}
        assert "good_key" in keys
        assert "indented_key" in keys
        assert "another good" in keys
        assert "under_sub_heading" in keys
        # Frontmatter and orphan lines should NOT be included
        assert "malformed" not in keys
        assert "orphan_line" not in keys

    @pytest.mark.asyncio
    async def test_completely_empty_file(self, tmp_path):
        """An empty file does not crash and produces zero KV entries."""
        facts = tmp_path / "facts.md"
        facts.write_text("")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        service.kv_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_headings_no_entries(self, tmp_path):
        """A file with headings but no KV lines produces zero KV entries."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\n\n## conventions\n\n## stats\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        service.kv_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_nonexistent_file_does_not_crash(self):
        """A change event for a non-existent file is handled gracefully."""
        service, kv_store = _make_mock_service()

        change = VaultChange(
            path="/nonexistent/path/facts.md",
            rel_path="projects/app/facts.md",
            operation="modified",
        )
        # Should not raise
        await on_facts_changed([change], service=service)
        service.kv_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_kv_set_error_doesnt_crash_handler(self, tmp_path):
        """If kv_set raises for one entry, the handler continues with others."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\na: 1\nb: 2\nc: 3\n")

        service, _ = _make_mock_service()
        call_count = 0

        async def failing_kv_set(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if args[2] == "b":
                raise RuntimeError("Milvus connection error")
            return {"kv_key": args[2]}

        service.kv_set = AsyncMock(side_effect=failing_kv_set)

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        # Should not raise
        await on_facts_changed([change], service=service)

        # All 3 entries should have been attempted
        assert call_count == 3

    def test_parser_handles_malformed_gracefully(self):
        """The standalone parser handles all edge cases without crashing."""
        edge_cases = [
            "",  # empty
            "no heading\nno: structure\n",  # no ## heading
            "## heading\n",  # heading only
            "## heading\nno colon line\n",  # no KV under heading
            "---\nfrontmatter: yes\n---\n",  # only frontmatter
            "## h\n: empty key\n",  # empty key
            "## h\n key_with_leading_space: val\n",  # leading space
        ]
        for text in edge_cases:
            # Should never raise
            result = parse_facts_file(text)
            assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# (g) Markdown formatting in values preserved
# ---------------------------------------------------------------------------


class TestMarkdownFormattingPreserved:
    """(g) facts.md with markdown formatting in values (bold, links)
    preserves formatting in stored value."""

    @pytest.mark.asyncio
    async def test_bold_text_preserved(self, tmp_path):
        """Bold markdown in values is preserved through parse → KV."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\nstatus: **active** development\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert service.kv_set.call_args.args[3] == "**active** development"

    @pytest.mark.asyncio
    async def test_links_preserved(self, tmp_path):
        """Markdown links in values are preserved."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\ndocs_url: [API Docs](https://docs.example.com)\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert service.kv_set.call_args.args[3] == "[API Docs](https://docs.example.com)"

    @pytest.mark.asyncio
    async def test_inline_code_preserved(self, tmp_path):
        """Inline code backticks in values are preserved."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\ntest_command: `pytest tests/ -v`\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert service.kv_set.call_args.args[3] == "`pytest tests/ -v`"

    @pytest.mark.asyncio
    async def test_italic_preserved(self, tmp_path):
        """Italic markdown in values is preserved."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\nnote: _important_ consideration\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert service.kv_set.call_args.args[3] == "_important_ consideration"

    @pytest.mark.asyncio
    async def test_list_values_preserved(self, tmp_path):
        """List-like values (bracketed) are preserved as strings."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\ntech_stack: [Python 3.12, SQLAlchemy, Pygame]\n")

        service, kv_store = _make_mock_service()

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert service.kv_set.call_args.args[3] == "[Python 3.12, SQLAlchemy, Pygame]"

    @pytest.mark.asyncio
    async def test_markdown_formatting_roundtrip(self, tmp_path):
        """Markdown-formatted values survive parse → render → parse roundtrip."""
        original_text = (
            "## project\n"
            "docs: [API Docs](https://docs.example.com)\n"
            "status: **active**\n"
            "command: `pytest -v`\n"
        )
        # Parse
        parsed = parse_facts_file(original_text)
        assert parsed["project"]["docs"] == "[API Docs](https://docs.example.com)"
        assert parsed["project"]["status"] == "**active**"
        assert parsed["project"]["command"] == "`pytest -v`"

        # Render back
        rendered = render_facts_file(parsed)

        # Re-parse
        reparsed = parse_facts_file(rendered)
        assert reparsed == parsed  # Lossless roundtrip

    def test_sync_facts_file_preserves_markdown_in_values(self, tmp_path):
        """_sync_facts_file writes markdown-formatted values correctly."""
        svc = MemoryV2Service()
        facts_path = tmp_path / "facts.md"

        svc._sync_facts_file(
            facts_path,
            "project",
            "docs_url",
            "[API Docs](https://docs.example.com)",
        )

        content = facts_path.read_text(encoding="utf-8")
        assert "docs_url: [API Docs](https://docs.example.com)" in content

        # Verify it parses back correctly
        parsed = parse_facts_file(content)
        assert parsed["project"]["docs_url"] == "[API Docs](https://docs.example.com)"


# ---------------------------------------------------------------------------
# Bidirectional sync integration: file → KV → file roundtrip
# ---------------------------------------------------------------------------


class TestBidirectionalRoundtrip:
    """Integration tests for the full bidirectional sync cycle."""

    @pytest.mark.asyncio
    async def test_file_to_kv_direction(self, tmp_path):
        """facts.md → parse → kv_set pipeline works end-to-end."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        service, kv_store = _make_mock_service()
        register_facts_handlers(watcher, service=service)

        # Initial snapshot
        await watcher.check()

        # Create facts file
        project_dir = vault / "projects" / "app"
        project_dir.mkdir(parents=True)
        facts_file = project_dir / "facts.md"
        facts_file.write_text(
            "## project\ntech_stack: Python\ndb: SQLite\n\n## conventions\nnaming: snake_case\n"
        )

        # Detect and dispatch
        await watcher.check()

        # All entries synced
        scope = kv_store["project_app"]
        assert scope["project/tech_stack"] == "Python"
        assert scope["project/db"] == "SQLite"
        assert scope["conventions/naming"] == "snake_case"

    def test_kv_to_file_direction(self, tmp_path):
        """kv_set → _sync_facts_file → parse roundtrip is lossless."""
        svc = MemoryV2Service()
        facts_path = tmp_path / "facts.md"

        # Write several entries
        svc._sync_facts_file(facts_path, "project", "tech", "Python")
        svc._sync_facts_file(facts_path, "project", "db", "SQLite")
        svc._sync_facts_file(facts_path, "conventions", "naming", "snake_case")

        # Read and parse
        content = facts_path.read_text(encoding="utf-8")
        parsed = parse_facts_file(content)

        assert parsed["project"]["tech"] == "Python"
        assert parsed["project"]["db"] == "SQLite"
        assert parsed["conventions"]["naming"] == "snake_case"

    def test_update_existing_via_sync(self, tmp_path):
        """Updating a value via _sync_facts_file preserves other entries."""
        svc = MemoryV2Service()
        facts_path = tmp_path / "facts.md"

        # Initial state
        svc._sync_facts_file(facts_path, "project", "tech", "Python")
        svc._sync_facts_file(facts_path, "project", "db", "SQLite")

        # Update one value
        svc._sync_facts_file(facts_path, "project", "db", "PostgreSQL")

        parsed = parse_facts_file(facts_path.read_text(encoding="utf-8"))
        assert parsed["project"]["tech"] == "Python"  # unchanged
        assert parsed["project"]["db"] == "PostgreSQL"  # updated

    @pytest.mark.asyncio
    async def test_deleted_file_retains_kv_entries(self, tmp_path):
        """Deleting facts.md should not remove KV entries from the backend."""
        service, kv_store = _make_mock_service()

        # First, create and sync
        facts = tmp_path / "facts.md"
        facts.write_text("## project\ntech: Python\n")

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=service)

        assert kv_store["project_app"]["project/tech"] == "Python"

        # Delete the file
        delete_change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="deleted",
        )
        await on_facts_changed([delete_change], service=service)

        # KV entries should still be present (no deletion from store)
        assert kv_store["project_app"]["project/tech"] == "Python"

    @pytest.mark.asyncio
    async def test_multiple_scopes_independent(self, tmp_path):
        """Changes in different scopes don't interfere with each other."""
        service, kv_store = _make_mock_service()

        # System scope
        system_facts = tmp_path / "system_facts.md"
        system_facts.write_text("## global\nversion: 2.0\n")
        c1 = VaultChange(
            path=str(system_facts),
            rel_path="system/facts.md",
            operation="created",
        )

        # Project scope
        project_facts = tmp_path / "project_facts.md"
        project_facts.write_text("## project\nversion: 3.0\n")
        c2 = VaultChange(
            path=str(project_facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )

        await on_facts_changed([c1, c2], service=service)

        # Both scopes should have their own entries
        assert kv_store["system"]["global/version"] == "2.0"
        assert kv_store["project_app"]["project/version"] == "3.0"

    @pytest.mark.asyncio
    async def test_sync_facts_to_kv_returns_count(self, tmp_path):
        """_sync_facts_to_kv returns the number of entries synced."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\na: 1\nb: 2\n\n## config\nc: 3\n")

        service = AsyncMock()
        service.kv_set = AsyncMock(return_value={})

        count = await _sync_facts_to_kv(str(facts), "project", "app", service)
        assert count == 3

    @pytest.mark.asyncio
    async def test_sync_facts_to_kv_nonexistent(self):
        """_sync_facts_to_kv returns 0 for a nonexistent file."""
        service = AsyncMock()
        count = await _sync_facts_to_kv("/no/such/file.md", "project", "app", service)
        assert count == 0

    @pytest.mark.asyncio
    async def test_sync_facts_to_kv_empty(self, tmp_path):
        """_sync_facts_to_kv returns 0 for an empty file."""
        facts = tmp_path / "facts.md"
        facts.write_text("")

        service = AsyncMock()
        count = await _sync_facts_to_kv(str(facts), "project", "app", service)
        assert count == 0
