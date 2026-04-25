"""Tests for src/facts_handler — facts.md vault watcher handler and KV sync."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.facts_handler import (
    FACTS_PATTERNS,
    FactsChangeInfo,
    _project_id_for_scope,
    _scope_to_kv_scope,
    _sync_facts_to_kv,
    derive_scope,
    on_facts_changed,
    register_facts_handlers,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# derive_scope
# ---------------------------------------------------------------------------


class TestDeriveScope:
    """Tests for derive_scope — extracting scope + identifier from paths."""

    def test_system_scope(self):
        scope, identifier = derive_scope("system/facts.md")
        assert scope == "system"
        assert identifier is None

    def test_orchestrator_scope(self):
        scope, identifier = derive_scope("orchestrator/facts.md")
        assert scope == "orchestrator"
        assert identifier is None

    def test_project_scope(self):
        scope, identifier = derive_scope("projects/my-app/facts.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_project_scope_with_dashes(self):
        scope, identifier = derive_scope("projects/mech-fighters/facts.md")
        assert scope == "project"
        assert identifier == "mech-fighters"

    def test_agent_type_scope(self):
        scope, identifier = derive_scope("agent-types/coding/facts.md")
        assert scope == "agent_type"
        assert identifier == "coding"

    def test_agent_type_scope_with_complex_name(self):
        scope, identifier = derive_scope("agent-types/review-specialist/facts.md")
        assert scope == "agent_type"
        assert identifier == "review-specialist"

    def test_backslash_normalisation(self):
        """Windows-style separators should be handled."""
        scope, identifier = derive_scope("projects\\my-app\\facts.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_unknown_top_level(self):
        """Unknown top-level directory falls through to the fallback."""
        scope, identifier = derive_scope("custom/facts.md")
        assert scope == "custom"
        assert identifier is None


# ---------------------------------------------------------------------------
# FactsChangeInfo
# ---------------------------------------------------------------------------


class TestFactsChangeInfo:
    """Tests for the FactsChangeInfo dataclass."""

    def test_creation(self):
        info = FactsChangeInfo(
            file_path="/vault/projects/app/facts.md",
            change_type="modified",
            scope="project",
            identifier="app",
        )
        assert info.file_path == "/vault/projects/app/facts.md"
        assert info.change_type == "modified"
        assert info.scope == "project"
        assert info.identifier == "app"

    def test_frozen(self):
        info = FactsChangeInfo(
            file_path="/vault/system/facts.md",
            change_type="created",
            scope="system",
            identifier=None,
        )
        with pytest.raises(AttributeError):
            info.scope = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _scope_to_kv_scope
# ---------------------------------------------------------------------------


class TestScopeToKvScope:
    """Tests for _scope_to_kv_scope — scope string conversion."""

    def test_system(self):
        assert _scope_to_kv_scope("system", None) == "system"

    def test_orchestrator(self):
        assert _scope_to_kv_scope("orchestrator", None) == "orchestrator"

    def test_agent_type(self):
        assert _scope_to_kv_scope("agent_type", "coding") == "agenttype_coding"

    def test_project(self):
        assert _scope_to_kv_scope("project", "my-app") == "project_my-app"


# ---------------------------------------------------------------------------
# _project_id_for_scope
# ---------------------------------------------------------------------------


class TestProjectIdForScope:
    """Tests for _project_id_for_scope — deriving project_id."""

    def test_project_scope_uses_identifier(self):
        assert _project_id_for_scope("project", "my-app") == "my-app"

    def test_system_scope_uses_scope_name(self):
        assert _project_id_for_scope("system", None) == "system"

    def test_agent_type_uses_identifier(self):
        assert _project_id_for_scope("agent_type", "coding") == "coding"


# ---------------------------------------------------------------------------
# on_facts_changed — without service (fallback / Phase 1 behaviour)
# ---------------------------------------------------------------------------


class TestOnFactsChangedNoService:
    """Tests for on_facts_changed without a service (log-only fallback)."""

    @pytest.mark.asyncio
    async def test_logs_single_change(self, caplog):
        change = VaultChange(
            path="/home/user/.agent-queue/vault/system/facts.md",
            rel_path="system/facts.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change])

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "facts.md" in record.message
        assert "modified" in record.message
        assert "system" in record.message

    @pytest.mark.asyncio
    async def test_logs_multiple_changes(self, caplog):
        changes = [
            VaultChange(
                path="/vault/system/facts.md",
                rel_path="system/facts.md",
                operation="modified",
            ),
            VaultChange(
                path="/vault/projects/app/facts.md",
                rel_path="projects/app/facts.md",
                operation="created",
            ),
            VaultChange(
                path="/vault/agent-types/coding/facts.md",
                rel_path="agent-types/coding/facts.md",
                operation="deleted",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed(changes)

        assert len(caplog.records) == 3
        # Check scope derivation is correct in log messages
        messages = [r.message for r in caplog.records]
        assert any("system" in m and "modified" in m for m in messages)
        assert any("project/app" in m and "created" in m for m in messages)
        assert any("agent_type/coding" in m and "deleted" in m for m in messages)

    @pytest.mark.asyncio
    async def test_empty_changes_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([])

        assert len(caplog.records) == 0

    @pytest.mark.asyncio
    async def test_handler_derives_scope_correctly(self, caplog):
        """Verify scope/identifier derivation inside the handler."""
        change = VaultChange(
            path="/vault/projects/mech-fighters/facts.md",
            rel_path="projects/mech-fighters/facts.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change])

        assert "project/mech-fighters" in caplog.records[0].message

    @pytest.mark.asyncio
    async def test_handler_singleton_scope_label(self, caplog):
        """Singleton scopes should not include an identifier in the label."""
        change = VaultChange(
            path="/vault/orchestrator/facts.md",
            rel_path="orchestrator/facts.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change])

        msg = caplog.records[0].message
        assert "orchestrator" in msg
        # Should not have "orchestrator/None"
        assert "None" not in msg

    @pytest.mark.asyncio
    async def test_unavailable_service_falls_back_to_logging(self, caplog):
        """A service with available=False should fall back to log-only."""
        service = MagicMock()
        service.available = False

        change = VaultChange(
            path="/vault/system/facts.md",
            rel_path="system/facts.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change], service=service)

        assert len(caplog.records) == 1
        assert "service unavailable" in caplog.records[0].message


# ---------------------------------------------------------------------------
# on_facts_changed — with service (KV sync)
# ---------------------------------------------------------------------------


class TestOnFactsChangedWithService:
    """Tests for on_facts_changed with a MemoryService (KV sync)."""

    @pytest.fixture
    def mock_service(self):
        service = AsyncMock()
        service.available = True
        service.kv_set = AsyncMock(return_value={"key": "value"})
        return service

    @pytest.mark.asyncio
    async def test_created_file_triggers_sync(self, tmp_path, mock_service, caplog):
        """A 'created' change should parse the file and call kv_set."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\ntech_stack: Python\ndb: SQLite\n")

        change = VaultChange(
            path=str(facts),
            rel_path="projects/my-app/facts.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change], service=mock_service)

        # Should have called kv_set twice (two KV entries)
        assert mock_service.kv_set.call_count == 2

        # Verify correct arguments
        calls = mock_service.kv_set.call_args_list
        call_keys = {c.args[2] for c in calls}  # args[2] is 'key'
        assert call_keys == {"tech_stack", "db"}

        # All calls should use _from_vault=True to prevent circular sync
        for call in calls:
            assert call.kwargs.get("_from_vault") is True

        # All calls should have scope=project_my-app
        for call in calls:
            assert call.kwargs.get("scope") == "project_my-app"

    @pytest.mark.asyncio
    async def test_modified_file_triggers_sync(self, tmp_path, mock_service, caplog):
        """A 'modified' change should parse the file and call kv_set."""
        facts = tmp_path / "facts.md"
        facts.write_text("## conventions\nnaming: snake_case\n")

        change = VaultChange(
            path=str(facts),
            rel_path="system/facts.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change], service=mock_service)

        assert mock_service.kv_set.call_count == 1
        call = mock_service.kv_set.call_args
        assert call.args[1] == "conventions"  # namespace
        assert call.args[2] == "naming"  # key
        assert call.args[3] == "snake_case"  # value
        assert call.kwargs["scope"] == "system"
        assert call.kwargs["_from_vault"] is True

    @pytest.mark.asyncio
    async def test_deleted_file_does_not_call_kv_set(self, mock_service, caplog):
        """A 'deleted' change should log but not attempt to parse/sync."""
        change = VaultChange(
            path="/vault/projects/app/facts.md",
            rel_path="projects/app/facts.md",
            operation="deleted",
        )
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await on_facts_changed([change], service=mock_service)

        mock_service.kv_set.assert_not_called()
        assert any("deleted" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_empty_facts_file_no_kv_set(self, tmp_path, mock_service):
        """An empty facts file should not call kv_set."""
        facts = tmp_path / "facts.md"
        facts.write_text("")

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="modified",
        )
        await on_facts_changed([change], service=mock_service)

        mock_service.kv_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_nonexistent_file_no_kv_set(self, mock_service, caplog):
        """A non-existent file path should not crash, just warn."""
        change = VaultChange(
            path="/nonexistent/path/facts.md",
            rel_path="projects/app/facts.md",
            operation="modified",
        )
        with caplog.at_level(logging.WARNING, logger="src.facts_handler"):
            await on_facts_changed([change], service=mock_service)

        mock_service.kv_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_kv_set_error_logged_and_continues(self, tmp_path, mock_service, caplog):
        """If kv_set raises for one entry, others should still be synced."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\na: 1\nb: 2\nc: 3\n")

        call_count = 0

        async def failing_kv_set(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if args[2] == "b":  # fail on key "b"
                raise RuntimeError("Milvus error")
            return {"key": args[2]}

        mock_service.kv_set = AsyncMock(side_effect=failing_kv_set)

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        with caplog.at_level(logging.ERROR, logger="src.facts_handler"):
            await on_facts_changed([change], service=mock_service)

        # All 3 should have been attempted
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_agent_type_scope(self, tmp_path, mock_service):
        """Agent-type scope should use correct scope string."""
        facts = tmp_path / "facts.md"
        facts.write_text("## config\nprompt: coding\n")

        change = VaultChange(
            path=str(facts),
            rel_path="agent-types/coding/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=mock_service)

        call = mock_service.kv_set.call_args
        assert call.kwargs["scope"] == "agenttype_coding"
        assert call.args[0] == "coding"  # project_id

    @pytest.mark.asyncio
    async def test_multiple_namespaces_sync(self, tmp_path, mock_service):
        """Multiple namespaces in a single file should all sync."""
        facts = tmp_path / "facts.md"
        facts.write_text(
            "## project\n"
            "tech_stack: Python\n"
            "\n"
            "## conventions\n"
            "naming: snake_case\n"
        )

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="modified",
        )
        await on_facts_changed([change], service=mock_service)

        assert mock_service.kv_set.call_count == 2
        namespaces = {c.args[1] for c in mock_service.kv_set.call_args_list}
        assert namespaces == {"project", "conventions"}

    @pytest.mark.asyncio
    async def test_bullet_prefixed_entries_sync(self, tmp_path, mock_service):
        """Bullet-prefixed entries should have bullets stripped before sync."""
        facts = tmp_path / "facts.md"
        facts.write_text("## project\n- tech_stack: Python\n- db: SQLite\n")

        change = VaultChange(
            path=str(facts),
            rel_path="projects/app/facts.md",
            operation="created",
        )
        await on_facts_changed([change], service=mock_service)

        assert mock_service.kv_set.call_count == 2
        call_keys = {c.args[2] for c in mock_service.kv_set.call_args_list}
        assert call_keys == {"tech_stack", "db"}


# ---------------------------------------------------------------------------
# _sync_facts_to_kv — direct unit tests
# ---------------------------------------------------------------------------


class TestSyncFactsToKv:
    """Unit tests for the _sync_facts_to_kv helper function."""

    @pytest.mark.asyncio
    async def test_returns_count(self, tmp_path):
        facts = tmp_path / "facts.md"
        facts.write_text("## ns\na: 1\nb: 2\nc: 3\n")

        service = AsyncMock()
        service.kv_set = AsyncMock(return_value={})

        count = await _sync_facts_to_kv(str(facts), "project", "app", service)
        assert count == 3

    @pytest.mark.asyncio
    async def test_nonexistent_returns_zero(self):
        service = AsyncMock()
        count = await _sync_facts_to_kv("/no/such/file.md", "project", "app", service)
        assert count == 0

    @pytest.mark.asyncio
    async def test_empty_file_returns_zero(self, tmp_path):
        facts = tmp_path / "facts.md"
        facts.write_text("")

        service = AsyncMock()
        count = await _sync_facts_to_kv(str(facts), "project", "app", service)
        assert count == 0


# ---------------------------------------------------------------------------
# register_facts_handlers
# ---------------------------------------------------------------------------


class TestRegisterFactsHandlers:
    """Tests for register_facts_handlers — wiring patterns to VaultWatcher."""

    def test_registers_all_patterns(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_facts_handlers(watcher)

        assert len(handler_ids) == len(FACTS_PATTERNS)
        assert watcher.get_handler_count() == len(FACTS_PATTERNS)

    def test_handler_ids_use_pattern_names(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_facts_handlers(watcher)

        for pattern, hid in zip(FACTS_PATTERNS, handler_ids):
            assert hid == f"facts:{pattern}"

    def test_idempotent_registration(self, tmp_path):
        """Registering twice overwrites the same handler IDs."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        ids1 = register_facts_handlers(watcher)
        ids2 = register_facts_handlers(watcher)

        # Same IDs — register_handler with explicit IDs replaces
        assert ids1 == ids2
        # The handler count should still be the same (overwritten, not doubled)
        assert watcher.get_handler_count() == len(FACTS_PATTERNS)

    def test_logs_service_connected(self, tmp_path, caplog):
        """When a service is provided, log message should indicate it."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        service = MagicMock()
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            register_facts_handlers(watcher, service=service)

        assert any("service connected" in r.message for r in caplog.records)

    def test_logs_no_service(self, tmp_path, caplog):
        """When no service is provided, log message should indicate log-only."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            register_facts_handlers(watcher)

        assert any("log-only" in r.message for r in caplog.records)

    def test_reregister_with_service_upgrades_handler(self, tmp_path, caplog):
        """Re-registering with a service should upgrade from log-only to syncing."""
        watcher = VaultWatcher(vault_root=str(tmp_path))

        # Initial registration — no service
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            ids1 = register_facts_handlers(watcher)
        assert any("log-only" in r.message for r in caplog.records)

        caplog.clear()

        # Re-register with service — should indicate service connected
        service = MagicMock()
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            ids2 = register_facts_handlers(watcher, service=service)

        assert ids1 == ids2
        assert watcher.get_handler_count() == len(FACTS_PATTERNS)
        assert any("service connected" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_reregister_end_to_end_sync(self, tmp_path):
        """After re-registration with service, file changes should trigger kv_set."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        # Phase 1: register without service
        register_facts_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Phase 2: re-register WITH service (simulates post-plugin-load wiring)
        service = AsyncMock()
        service.available = True
        service.kv_set = AsyncMock(return_value={})

        register_facts_handlers(watcher, service=service)

        # Create a project facts file
        project_dir = vault / "projects" / "app"
        project_dir.mkdir(parents=True)
        facts_file = project_dir / "facts.md"
        facts_file.write_text("## project\ntech_stack: Python\ndb: SQLite\n")

        # Detect and dispatch
        await watcher.check()

        # kv_set should have been called — the re-registered handler is active
        assert service.kv_set.call_count == 2
        call_keys = {c.args[2] for c in service.kv_set.call_args_list}
        assert call_keys == {"tech_stack", "db"}


# ---------------------------------------------------------------------------
# Integration: patterns match the expected paths
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Verify that the registered patterns actually match fact file paths."""

    def test_system_facts_matches(self):
        assert VaultWatcher._matches_pattern("system/facts.md", "system/facts.md")

    def test_orchestrator_facts_matches(self):
        assert VaultWatcher._matches_pattern(
            "orchestrator/facts.md", "orchestrator/facts.md"
        )

    def test_project_facts_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/facts.md", "projects/*/facts.md"
        )

    def test_agent_type_facts_matches(self):
        assert VaultWatcher._matches_pattern(
            "agent-types/coding/facts.md", "agent-types/*/facts.md"
        )

    def test_system_pattern_does_not_match_project(self):
        """The literal system pattern should not match project facts."""
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/facts.md", "system/facts.md"
        )

    def test_orchestrator_pattern_does_not_match_agent_type(self):
        """The literal orchestrator pattern should not match agent-type facts."""
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/facts.md", "orchestrator/facts.md"
        )

    def test_non_facts_file_does_not_match(self):
        """A non-facts markdown file should not match any pattern."""
        for pattern in FACTS_PATTERNS:
            assert not VaultWatcher._matches_pattern(
                "projects/my-app/notes.md", pattern
            )

    def test_no_cross_scope_matching(self):
        """Each scope's pattern should only match its own scope."""
        # system pattern does not match orchestrator
        assert not VaultWatcher._matches_pattern(
            "orchestrator/facts.md", "system/facts.md"
        )
        # project pattern does not match agent-type
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/facts.md", "projects/*/facts.md"
        )
        # agent-type pattern does not match project
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/facts.md", "agent-types/*/facts.md"
        )


# ---------------------------------------------------------------------------
# End-to-end: VaultWatcher detects fact file change and dispatches
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    """Verify the full pipeline: file change -> VaultWatcher -> handler."""

    @pytest.mark.asyncio
    async def test_detects_and_dispatches_project_facts(self, tmp_path):
        """Create a project facts.md and verify the handler is called."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("projects/*/facts.md", capture_handler)

        # Take initial snapshot (empty)
        await watcher.check()

        # Create a project facts file
        project_dir = vault / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        facts_file = project_dir / "facts.md"
        facts_file.write_text("# Facts\n- key: value\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/facts.md"
        assert dispatched[0].operation == "created"

    @pytest.mark.asyncio
    async def test_detects_system_facts_modification(self, tmp_path):
        """Modify system/facts.md and verify handler dispatch."""
        vault = tmp_path / "vault"
        system_dir = vault / "system"
        system_dir.mkdir(parents=True)
        facts_file = system_dir / "facts.md"
        facts_file.write_text("# System Facts\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("*/facts.md", capture_handler)

        # Initial snapshot includes the existing file
        await watcher.check()

        # Modify the file (need different mtime)
        import time

        time.sleep(0.05)
        facts_file.write_text("# System Facts\n- updated: true\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "system/facts.md"
        assert dispatched[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_full_handler_with_all_patterns(self, tmp_path, caplog):
        """Register all facts patterns via register_facts_handlers and verify dispatch."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_facts_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Create facts files in multiple scopes
        (vault / "system").mkdir()
        (vault / "system" / "facts.md").write_text("# System\n")

        (vault / "projects" / "app").mkdir(parents=True)
        (vault / "projects" / "app" / "facts.md").write_text("# Project\n")

        (vault / "agent-types" / "coder").mkdir(parents=True)
        (vault / "agent-types" / "coder" / "facts.md").write_text("# Agent\n")

        # Detect and dispatch
        with caplog.at_level(logging.INFO, logger="src.facts_handler"):
            await watcher.check()

        # The handler should have logged all 3
        handler_logs = [
            r for r in caplog.records
            if "facts.md" in r.message and "created" in r.message
        ]
        assert len(handler_logs) == 3

    @pytest.mark.asyncio
    async def test_non_facts_file_not_dispatched(self, tmp_path):
        """Other files in the same directory should not trigger the handler."""
        vault = tmp_path / "vault"
        (vault / "system").mkdir(parents=True)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("*/facts.md", capture_handler)
        await watcher.check()

        # Create a non-facts file
        (vault / "system" / "notes.md").write_text("# Notes\n")

        await watcher.check()

        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_end_to_end_with_service(self, tmp_path):
        """Full pipeline: file created -> watcher -> handler -> kv_set."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        service = AsyncMock()
        service.available = True
        service.kv_set = AsyncMock(return_value={})

        register_facts_handlers(watcher, service=service)

        # Initial snapshot
        await watcher.check()

        # Create a project facts file
        project_dir = vault / "projects" / "app"
        project_dir.mkdir(parents=True)
        facts_file = project_dir / "facts.md"
        facts_file.write_text("## project\ntech_stack: Python\ndb: SQLite\n")

        # Detect and dispatch
        await watcher.check()

        # kv_set should have been called for each entry
        assert service.kv_set.call_count == 2
        call_keys = {c.args[2] for c in service.kv_set.call_args_list}
        assert call_keys == {"tech_stack", "db"}
