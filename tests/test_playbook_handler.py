"""Tests for src/playbooks/handler — playbook .md vault watcher handler registration.

Also includes full end-to-end integration tests (roadmap 5.1.6) that verify
the complete pipeline: VaultWatcher detects file change → PlaybookHandler
dispatches → PlaybookManager compiles via PlaybookCompiler → result persisted
and active in the registry.
"""

from __future__ import annotations

import json
import logging
import time
from unittest.mock import AsyncMock

import pytest

from src.playbooks.handler import (
    PLAYBOOK_PATTERNS,
    derive_playbook_scope,
    on_playbook_changed,
    register_playbook_handlers,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# derive_playbook_scope
# ---------------------------------------------------------------------------


class TestDerivePlaybookScope:
    """Tests for derive_playbook_scope — extracting scope + identifier from paths."""

    def test_system_scope(self):
        scope, identifier = derive_playbook_scope("system/playbooks/deploy.md")
        assert scope == "system"
        assert identifier is None

    def test_orchestrator_scope(self):
        scope, identifier = derive_playbook_scope("orchestrator/playbooks/routing.md")
        assert scope == "orchestrator"
        assert identifier is None

    def test_project_scope(self):
        scope, identifier = derive_playbook_scope("projects/my-app/playbooks/review.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_project_scope_with_dashes(self):
        scope, identifier = derive_playbook_scope("projects/mech-fighters/playbooks/deploy.md")
        assert scope == "project"
        assert identifier == "mech-fighters"

    def test_agent_type_scope(self):
        scope, identifier = derive_playbook_scope("agent-types/coding/playbooks/quality.md")
        assert scope == "agent_type"
        assert identifier == "coding"

    def test_agent_type_scope_with_complex_name(self):
        scope, identifier = derive_playbook_scope("agent-types/review-specialist/playbooks/gate.md")
        assert scope == "agent_type"
        assert identifier == "review-specialist"

    def test_backslash_normalisation(self):
        """Windows-style separators should be handled."""
        scope, identifier = derive_playbook_scope("projects\\my-app\\playbooks\\deploy.md")
        assert scope == "project"
        assert identifier == "my-app"

    def test_unknown_top_level(self):
        """Unknown top-level directory falls through to the fallback."""
        scope, identifier = derive_playbook_scope("custom/playbooks/foo.md")
        assert scope == "custom"
        assert identifier is None


# ---------------------------------------------------------------------------
# on_playbook_changed (stub handler)
# ---------------------------------------------------------------------------


class TestOnPlaybookChanged:
    """Tests for the stub handler on_playbook_changed."""

    @pytest.mark.asyncio
    async def test_logs_single_change(self, caplog):
        change = VaultChange(
            path="/home/user/.agent-queue/vault/system/playbooks/deploy.md",
            rel_path="system/playbooks/deploy.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.playbooks.handler"):
            await on_playbook_changed([change])

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "Playbook" in record.message
        assert "modified" in record.message
        assert "system" in record.message

    @pytest.mark.asyncio
    async def test_logs_multiple_changes(self, caplog):
        changes = [
            VaultChange(
                path="/vault/system/playbooks/deploy.md",
                rel_path="system/playbooks/deploy.md",
                operation="modified",
            ),
            VaultChange(
                path="/vault/projects/app/playbooks/review.md",
                rel_path="projects/app/playbooks/review.md",
                operation="created",
            ),
            VaultChange(
                path="/vault/agent-types/coding/playbooks/gate.md",
                rel_path="agent-types/coding/playbooks/gate.md",
                operation="deleted",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="src.playbooks.handler"):
            await on_playbook_changed(changes)

        assert len(caplog.records) == 3
        messages = [r.message for r in caplog.records]
        assert any("system" in m and "modified" in m for m in messages)
        assert any("project/app" in m and "created" in m for m in messages)
        assert any("agent_type/coding" in m and "deleted" in m for m in messages)

    @pytest.mark.asyncio
    async def test_empty_changes_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.playbooks.handler"):
            await on_playbook_changed([])

        assert len(caplog.records) == 0

    @pytest.mark.asyncio
    async def test_handler_derives_scope_correctly(self, caplog):
        """Verify scope/identifier derivation inside the handler."""
        change = VaultChange(
            path="/vault/projects/mech-fighters/playbooks/deploy.md",
            rel_path="projects/mech-fighters/playbooks/deploy.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.playbooks.handler"):
            await on_playbook_changed([change])

        assert "project/mech-fighters" in caplog.records[0].message

    @pytest.mark.asyncio
    async def test_handler_singleton_scope_label(self, caplog):
        """Singleton scopes should not include an identifier in the label."""
        change = VaultChange(
            path="/vault/orchestrator/playbooks/routing.md",
            rel_path="orchestrator/playbooks/routing.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.playbooks.handler"):
            await on_playbook_changed([change])

        msg = caplog.records[0].message
        assert "orchestrator" in msg
        assert "None" not in msg

    @pytest.mark.asyncio
    async def test_handler_receives_file_path_and_change_type(self, caplog):
        """Handler should log both the file path and the change type."""
        change = VaultChange(
            path="/vault/projects/app/playbooks/deploy.md",
            rel_path="projects/app/playbooks/deploy.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.playbooks.handler"):
            await on_playbook_changed([change])

        msg = caplog.records[0].message
        assert "created" in msg
        assert "projects/app/playbooks/deploy.md" in msg


# ---------------------------------------------------------------------------
# register_playbook_handlers
# ---------------------------------------------------------------------------


class TestRegisterPlaybookHandlers:
    """Tests for register_playbook_handlers — wiring patterns to VaultWatcher."""

    def test_registers_all_patterns(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_playbook_handlers(watcher)

        assert len(handler_ids) == len(PLAYBOOK_PATTERNS)
        assert watcher.get_handler_count() == len(PLAYBOOK_PATTERNS)

    def test_handler_ids_use_pattern_names(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_ids = register_playbook_handlers(watcher)

        for pattern, hid in zip(PLAYBOOK_PATTERNS, handler_ids):
            assert hid == f"playbook:{pattern}"

    def test_idempotent_registration(self, tmp_path):
        """Registering twice overwrites the same handler IDs."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        ids1 = register_playbook_handlers(watcher)
        ids2 = register_playbook_handlers(watcher)

        assert ids1 == ids2
        assert watcher.get_handler_count() == len(PLAYBOOK_PATTERNS)


# ---------------------------------------------------------------------------
# Integration: patterns match the expected paths
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Verify that the registered patterns match expected playbook file paths."""

    def test_system_playbook_matches(self):
        assert VaultWatcher._matches_pattern("system/playbooks/deploy.md", "system/playbooks/*.md")

    def test_orchestrator_playbook_matches(self):
        assert VaultWatcher._matches_pattern(
            "orchestrator/playbooks/routing.md", "orchestrator/playbooks/*.md"
        )

    def test_project_playbook_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/review.md", "projects/*/playbooks/*.md"
        )

    def test_agent_type_playbook_matches(self):
        assert VaultWatcher._matches_pattern(
            "agent-types/coding/playbooks/quality.md",
            "agent-types/*/playbooks/*.md",
        )

    def test_system_pattern_does_not_match_project(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/deploy.md", "system/playbooks/*.md"
        )

    def test_project_pattern_does_not_match_agent_type(self):
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/playbooks/gate.md", "projects/*/playbooks/*.md"
        )

    def test_agent_type_pattern_does_not_match_project(self):
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/deploy.md",
            "agent-types/*/playbooks/*.md",
        )

    def test_non_playbook_file_does_not_match(self):
        """A non-playbook markdown file should not match any pattern."""
        for pattern in PLAYBOOK_PATTERNS:
            assert not VaultWatcher._matches_pattern("projects/my-app/notes.md", pattern)

    def test_non_md_file_does_not_match(self):
        """A non-.md file in playbooks/ should not match."""
        for pattern in PLAYBOOK_PATTERNS:
            assert not VaultWatcher._matches_pattern("system/playbooks/deploy.yaml", pattern)

    def test_no_cross_scope_matching(self):
        """Each scope's pattern should only match its own scope."""
        assert not VaultWatcher._matches_pattern(
            "orchestrator/playbooks/route.md", "system/playbooks/*.md"
        )
        assert not VaultWatcher._matches_pattern(
            "system/playbooks/deploy.md", "orchestrator/playbooks/*.md"
        )
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/playbooks/gate.md",
            "projects/*/playbooks/*.md",
        )
        assert not VaultWatcher._matches_pattern(
            "projects/my-app/playbooks/review.md",
            "agent-types/*/playbooks/*.md",
        )


# ---------------------------------------------------------------------------
# End-to-end: VaultWatcher detects playbook file change and dispatches
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    """Verify the full pipeline: file change -> VaultWatcher -> handler."""

    @pytest.mark.asyncio
    async def test_detects_and_dispatches_project_playbook(self, tmp_path):
        """Create a project playbook and verify the handler is called."""
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

        watcher.register_handler("projects/*/playbooks/*.md", capture_handler)

        # Take initial snapshot (empty)
        await watcher.check()

        # Create a project playbook file
        playbook_dir = vault / "projects" / "my-app" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        playbook_file.write_text("# Deploy Playbook\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/playbooks/deploy.md"
        assert dispatched[0].operation == "created"

    @pytest.mark.asyncio
    async def test_detects_system_playbook_modification(self, tmp_path):
        """Modify system playbook and verify handler dispatch."""
        vault = tmp_path / "vault"
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "notify.md"
        playbook_file.write_text("# Notify Playbook\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("system/playbooks/*.md", capture_handler)

        # Initial snapshot includes the existing file
        await watcher.check()

        # Modify the file (need different mtime)
        time.sleep(0.05)
        playbook_file.write_text("# Notify Playbook\n- updated: true\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "system/playbooks/notify.md"
        assert dispatched[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_detects_playbook_deletion(self, tmp_path):
        """Delete a playbook and verify deletion is dispatched."""
        vault = tmp_path / "vault"
        playbook_dir = vault / "orchestrator" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "routing.md"
        playbook_file.write_text("# Routing Playbook\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("orchestrator/playbooks/*.md", capture_handler)

        # Initial snapshot includes the existing file
        await watcher.check()

        # Delete the file
        playbook_file.unlink()

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "orchestrator/playbooks/routing.md"
        assert dispatched[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_full_handler_with_all_patterns(self, tmp_path, caplog):
        """Register all playbook patterns via register_playbook_handlers and verify."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_playbook_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Create playbook files in multiple scopes
        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "system" / "playbooks" / "deploy.md").write_text("# Deploy\n")

        (vault / "projects" / "app" / "playbooks").mkdir(parents=True)
        (vault / "projects" / "app" / "playbooks" / "review.md").write_text("# Review\n")

        (vault / "agent-types" / "coder" / "playbooks").mkdir(parents=True)
        (vault / "agent-types" / "coder" / "playbooks" / "gate.md").write_text("# Gate\n")

        # orchestrator/ scope was merged into agent-types/supervisor/.
        (vault / "agent-types" / "supervisor" / "playbooks").mkdir(parents=True)
        (vault / "agent-types" / "supervisor" / "playbooks" / "route.md").write_text("# Route\n")

        # Detect and dispatch
        with caplog.at_level(logging.INFO, logger="src.playbooks.handler"):
            await watcher.check()

        # The stub handler should have logged all 4 (system, project, agent-type
        # coder, agent-type supervisor).
        handler_logs = [
            r for r in caplog.records if "Playbook" in r.message and "created" in r.message
        ]
        assert len(handler_logs) == 4

    @pytest.mark.asyncio
    async def test_non_playbook_file_not_dispatched(self, tmp_path):
        """Other .md files in the same scope should not trigger the handler."""
        vault = tmp_path / "vault"
        (vault / "system" / "playbooks").mkdir(parents=True)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler("system/playbooks/*.md", capture_handler)
        await watcher.check()

        # Create a file outside playbooks/
        (vault / "system" / "notes.md").write_text("# Notes\n")

        await watcher.check()

        assert len(dispatched) == 0


# ---------------------------------------------------------------------------
# Helpers for end-to-end integration tests
# ---------------------------------------------------------------------------

VALID_COMPILED_NODES = {
    "nodes": {
        "start": {
            "entry": True,
            "prompt": "Do something.",
            "goto": "end",
        },
        "end": {"terminal": True},
    }
}


def _make_playbook_md(
    *,
    playbook_id: str = "test-playbook",
    triggers: str = "- git.commit",
    scope: str = "system",
    body: str = "# Test\n\nDo something then finish.",
) -> str:
    """Create a minimal playbook markdown string."""
    return f"""\
---
id: {playbook_id}
triggers:
  {triggers}
scope: {scope}
---

{body}
"""


def _make_mock_provider(responses: list[str] | None = None) -> AsyncMock:
    """Create a mock ChatProvider returning fenced JSON."""
    from src.chat_providers.types import ChatResponse, TextBlock

    provider = AsyncMock()
    provider.model_name = "test-model"

    if responses is None:
        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        responses = [f"```json\n{json_str}\n```"]

    side_effects = []
    for text in responses:
        resp = ChatResponse(content=[TextBlock(text=text)])
        side_effects.append(resp)

    provider.create_message = AsyncMock(side_effect=side_effects)
    return provider


# ---------------------------------------------------------------------------
# End-to-end integration: VaultWatcher → Handler → Manager → Compiler
# (Roadmap 5.1.6)
# ---------------------------------------------------------------------------


class TestEndToEndCompilation:
    """Full-pipeline tests: file change → VaultWatcher → PlaybookHandler →
    PlaybookManager → PlaybookCompiler → compiled result persisted & active.
    """

    @pytest.mark.asyncio
    async def test_create_file_triggers_compilation(self, tmp_path):
        """Creating a playbook .md file compiles it via the full pipeline."""
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        vault.mkdir()

        provider = _make_mock_provider()
        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path / "data"),
        )

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher, playbook_manager=manager)

        # Initial snapshot (empty)
        await watcher.check()

        # Create a playbook file
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        playbook_file.write_text(_make_playbook_md(playbook_id="deploy"))

        # Detect and dispatch → should compile
        await watcher.check()

        # Verify the playbook is now active in the manager
        active = manager.get_playbook("deploy")
        assert active is not None
        assert active.version == 1
        assert active.id == "deploy"
        assert len(active.nodes) > 0

        # Verify LLM was called
        assert provider.create_message.call_count == 1

    @pytest.mark.asyncio
    async def test_modify_file_triggers_recompilation(self, tmp_path):
        """Modifying a playbook .md file triggers recompilation."""
        from src.chat_providers.types import ChatResponse, TextBlock
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        vault.mkdir()

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        provider = AsyncMock()
        provider.model_name = "test-model"
        provider.create_message = AsyncMock(side_effect=[resp, resp])

        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path / "data"),
        )

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher, playbook_manager=manager)

        # Initial snapshot (empty vault)
        await watcher.check()

        # Create the file → detected as "created" on next check
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        md_v1 = _make_playbook_md(playbook_id="deploy", body="# V1\nFirst version.")
        playbook_file.write_text(md_v1)
        await watcher.check()
        assert manager.get_playbook("deploy").version == 1

        # Modify the file (need different mtime + different content)
        time.sleep(0.05)
        md_v2 = _make_playbook_md(playbook_id="deploy", body="# V2\nUpdated version.")
        playbook_file.write_text(md_v2)

        # Detect modification and recompile
        await watcher.check()

        active = manager.get_playbook("deploy")
        assert active is not None
        assert active.version == 2
        assert provider.create_message.call_count == 2

    @pytest.mark.asyncio
    async def test_unchanged_file_skips_recompilation(self, tmp_path):
        """Touching a file without changing content skips recompilation."""
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        vault.mkdir()

        md = _make_playbook_md(playbook_id="deploy")

        provider = _make_mock_provider()
        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path / "data"),
        )

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher, playbook_manager=manager)

        # Initial snapshot (empty vault)
        await watcher.check()

        # Create file → first compilation
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        playbook_file.write_text(md)
        await watcher.check()
        assert provider.create_message.call_count == 1

        # Touch the file without changing content (update mtime)
        time.sleep(0.05)
        playbook_file.write_text(md)

        # Detect "modification" but content unchanged → skip recompilation
        await watcher.check()

        # LLM should NOT have been called again
        assert provider.create_message.call_count == 1
        # Version unchanged
        assert manager.get_playbook("deploy").version == 1

    @pytest.mark.asyncio
    async def test_delete_file_removes_from_registry(self, tmp_path):
        """Deleting a playbook .md file removes it from the active registry."""
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        vault.mkdir()

        provider = _make_mock_provider()
        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path / "data"),
        )

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher, playbook_manager=manager)

        # Initial snapshot (empty)
        await watcher.check()

        # Create file → compile
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        playbook_file.write_text(_make_playbook_md(playbook_id="deploy"))
        await watcher.check()
        assert manager.get_playbook("deploy") is not None

        # Delete → remove from registry
        playbook_file.unlink()
        await watcher.check()

        assert manager.get_playbook("deploy") is None

    @pytest.mark.asyncio
    async def test_compiled_json_persisted_to_disk(self, tmp_path):
        """Compiled playbook JSON is written to the data directory."""
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        data_dir = tmp_path / "data"
        vault.mkdir()

        provider = _make_mock_provider()
        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(data_dir),
        )

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher, playbook_manager=manager)

        # Initial snapshot (empty)
        await watcher.check()

        # Create file → compile
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        (playbook_dir / "deploy.md").write_text(_make_playbook_md(playbook_id="deploy"))
        await watcher.check()

        # Verify JSON was persisted
        json_path = data_dir / "playbooks" / "compiled" / "deploy.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert data["id"] == "deploy"
        assert data["version"] == 1
        assert data["triggers"] == ["git.commit"]
        assert data["scope"] == "system"

    @pytest.mark.asyncio
    async def test_compilation_failure_keeps_previous_version(self, tmp_path):
        """Failed recompilation keeps the previous version active in the full pipeline."""
        from src.chat_providers.types import ChatResponse, TextBlock
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        vault.mkdir()

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        bad_resp = ChatResponse(content=[TextBlock(text="not json at all")])

        provider = AsyncMock()
        provider.model_name = "test-model"
        # First call succeeds, next 3 (retry attempts for second compile) fail
        provider.create_message = AsyncMock(side_effect=[good_resp, bad_resp, bad_resp, bad_resp])

        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path / "data"),
        )

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher, playbook_manager=manager)

        # Initial snapshot (empty)
        await watcher.check()

        # Create file → first compile succeeds
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        md_v1 = _make_playbook_md(playbook_id="deploy", body="# V1\nFirst version.")
        playbook_file.write_text(md_v1)
        await watcher.check()
        assert manager.get_playbook("deploy").version == 1

        # Modify with different content (so hash changes)
        time.sleep(0.05)
        md_v2 = _make_playbook_md(playbook_id="deploy", body="# V2\nBroken update.")
        playbook_file.write_text(md_v2)

        # Recompile fails — previous version should remain
        await watcher.check()
        assert manager.get_playbook("deploy").version == 1

    @pytest.mark.asyncio
    async def test_multiple_scopes_compiled_independently(self, tmp_path):
        """Playbooks in different scopes compile independently via the pipeline."""
        from src.chat_providers.types import ChatResponse, TextBlock
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        vault.mkdir()

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        provider = AsyncMock()
        provider.model_name = "test-model"
        provider.create_message = AsyncMock(side_effect=[resp, resp])

        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path / "data"),
        )

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher, playbook_manager=manager)

        # Initial snapshot (empty)
        await watcher.check()

        # Create playbooks in two different scopes
        (vault / "system" / "playbooks").mkdir(parents=True)
        (vault / "system" / "playbooks" / "sys-deploy.md").write_text(
            _make_playbook_md(playbook_id="sys-deploy")
        )

        (vault / "projects" / "app" / "playbooks").mkdir(parents=True)
        (vault / "projects" / "app" / "playbooks" / "proj-review.md").write_text(
            _make_playbook_md(playbook_id="proj-review", scope="project")
        )

        await watcher.check()

        # Both should be active
        assert manager.get_playbook("sys-deploy") is not None
        assert manager.get_playbook("proj-review") is not None
        assert provider.create_message.call_count == 2

    @pytest.mark.asyncio
    async def test_startup_load_then_watcher_skip(self, tmp_path):
        """After loading from disk at startup, file watcher skips unchanged files."""
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        vault.mkdir()
        data_dir = tmp_path / "data"

        md = _make_playbook_md(playbook_id="deploy")

        # Phase 1: Initial compile via the full pipeline
        provider1 = _make_mock_provider()
        manager1 = PlaybookManager(
            config=None,
            chat_provider=provider1,
            data_dir=str(data_dir),
        )
        watcher1 = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher1, playbook_manager=manager1)

        # Initial snapshot (empty)
        await watcher1.check()

        # Create file → compile
        playbook_dir = vault / "system" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "deploy.md"
        playbook_file.write_text(md)
        await watcher1.check()
        assert provider1.create_message.call_count == 1
        assert manager1.get_playbook("deploy").version == 1

        # Phase 2: Simulate restart — new manager loads from disk
        provider2 = _make_mock_provider()
        manager2 = PlaybookManager(
            config=None,
            chat_provider=provider2,
            data_dir=str(data_dir),
        )
        loaded = await manager2.load_from_disk()
        assert loaded == 1

        watcher2 = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher2, playbook_manager=manager2)

        # The watcher's first check takes a snapshot but doesn't dispatch
        # (no prior snapshot to compare against).  On the second check,
        # the file is already known so no changes are dispatched.
        await watcher2.check()  # initial snapshot
        await watcher2.check()  # no changes detected

        # LLM should not be called — hash matches loaded version
        provider2.create_message.assert_not_called()
        assert manager2.get_playbook("deploy").version == 1

    @pytest.mark.asyncio
    async def test_project_scoped_playbook_records_project_identifier(self, tmp_path):
        """Project-scoped playbooks compiled via the watcher must record the
        project_id from their vault path, so that _matches_scope can reject
        events from other projects.  Regression for the bug where the handler
        dropped `scope_identifier` on the floor and every project-scoped
        playbook fired on events from all projects.
        """
        from src.playbooks.manager import PlaybookManager

        vault = tmp_path / "vault"
        vault.mkdir()

        provider = _make_mock_provider()
        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path / "data"),
        )

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )
        register_playbook_handlers(watcher, playbook_manager=manager)

        await watcher.check()

        playbook_dir = vault / "projects" / "my-app" / "playbooks"
        playbook_dir.mkdir(parents=True)
        playbook_file = playbook_dir / "review.md"
        playbook_file.write_text(
            _make_playbook_md(playbook_id="review", scope="project")
        )

        await watcher.check()

        assert manager.get_playbook("review") is not None
        assert manager.get_scope_identifier("review") == "my-app"
