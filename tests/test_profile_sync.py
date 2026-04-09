"""Tests for profile_sync — parsed markdown profile to agent_profiles table upsert.

Covers:
- sync_profile_to_db: create new profile, update existing, validation failures
- sync_profile_text_to_db: convenience wrapper (markdown text -> DB)
- derive_profile_id: vault path -> profile ID extraction
- ProfileSyncResult: dataclass and to_dict serialization
- on_profile_changed: VaultWatcher handler with DB integration
- register_profile_handlers: handler registration with DB injection
- Error handling: invalid JSON, missing ID, missing name fallback, DB errors
- Tool name soft-validation (warnings, not errors)
- File deletion handling (DB row retained)
- Pattern matching for agent-types/*/profile.md and orchestrator/profile.md
- End-to-end dispatch through VaultWatcher with DB sync
- Notification events: sync failures emit notify.profile_sync_failed
- Roadmap 4.1.10 (a)-(g): Profile parser and DB sync integration tests
- Roadmap 4.1.11 (a)-(g): Profile error handling tests
- Roadmap 4.1.12 (a)-(f): File watcher profile sync integration tests
- Roadmap 4.3.5 (a)-(g): Starter knowledge pack provisioning integration tests
"""

from __future__ import annotations

import os
import time

import pytest

from src.database import Database
from src.models import AgentProfile
from src.known_tools import KNOWN_TOOL_NAMES
from src.profile_parser import ParsedProfile, parse_profile, parsed_profile_to_agent_profile
from src.profile_sync import (
    PROFILE_PATTERNS,
    ProfileSyncResult,
    _find_profile_files,
    derive_profile_id,
    on_profile_changed,
    register_profile_handlers,
    scan_and_sync_existing_profiles,
    sync_profile_text_to_db,
    sync_profile_to_db,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_PROFILE = """\
---
id: test-agent
name: Test Agent
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""

FULL_PROFILE = """\
---
id: coding
name: Coding Agent
tags: [profile, agent-type]
---

# Coding Agent

## Role
You are a software engineering agent. You write, modify, and debug code
within a project workspace.

## Config
```json
{
  "model": "claude-sonnet-4-6",
  "permission_mode": "auto",
  "max_tokens_per_task": 100000
}
```

## Tools
```json
{
  "allowed": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
  "denied": []
}
```

## MCP Servers
```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
  }
}
```

## Rules
- Always run existing tests before committing
- Never commit secrets, .env files, or credentials
- Prefer small, focused commits over large ones

## Reflection
After completing a task, consider:
- Did I encounter any surprising behavior worth remembering?
"""

INVALID_JSON_PROFILE = """\
---
id: broken
name: Broken Agent
---

## Config
```json
{not valid json}
```
"""

NO_ID_PROFILE = """\
---
name: Unnamed Agent
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""

UNKNOWN_TOOLS_PROFILE = """\
---
id: custom-tools
name: Custom Tools Agent
---

## Tools
```json
{
  "allowed": ["Read", "Write", "my_custom_tool", "another_unknown_tool"]
}
```
"""


def _create_file(path: str, content: str = "test") -> None:
    """Create a file with the given content, ensuring parent dirs exist."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _touch(path: str) -> None:
    """Update a file's mtime to trigger modification detection."""
    now = time.time()
    os.utime(path, (now + 1, now + 1))


class ChangeCollector:
    """Collects VaultChange lists from handler calls for assertions."""

    def __init__(self):
        self.calls: list[list[VaultChange]] = []

    async def __call__(self, changes: list[VaultChange]) -> None:
        self.calls.append(changes)

    @property
    def all_changes(self) -> list[VaultChange]:
        return [c for batch in self.calls for c in batch]

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# derive_profile_id
# ---------------------------------------------------------------------------


class TestDeriveProfileId:
    """Test profile ID derivation from vault-relative paths."""

    def test_agent_type_coding(self):
        assert derive_profile_id("agent-types/coding/profile.md") == "coding"

    def test_agent_type_code_review(self):
        assert derive_profile_id("agent-types/code-review/profile.md") == "code-review"

    def test_agent_type_qa(self):
        assert derive_profile_id("agent-types/qa/profile.md") == "qa"

    def test_orchestrator(self):
        assert derive_profile_id("orchestrator/profile.md") == "orchestrator"

    def test_unrecognized_path(self):
        assert derive_profile_id("other/profile.md") is None

    def test_agent_types_root(self):
        """agent-types/profile.md has no type directory — should not match."""
        assert derive_profile_id("agent-types/profile.md") is None

    def test_wrong_filename(self):
        assert derive_profile_id("agent-types/coding/config.md") is None

    def test_windows_separators(self):
        assert derive_profile_id("agent-types\\coding\\profile.md") == "coding"

    def test_empty_path(self):
        assert derive_profile_id("") is None

    def test_deeply_nested(self):
        """Nested paths still extract the first type directory."""
        assert derive_profile_id("agent-types/coding/subdir/profile.md") == "coding"


# ---------------------------------------------------------------------------
# ProfileSyncResult
# ---------------------------------------------------------------------------


class TestProfileSyncResult:
    """Test the ProfileSyncResult dataclass and serialization."""

    def test_success_result_to_dict(self):
        result = ProfileSyncResult(
            success=True,
            action="created",
            profile_id="coding",
            name="Coding Agent",
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["action"] == "created"
        assert d["profile_id"] == "coding"
        assert d["name"] == "Coding Agent"
        assert "warnings" not in d
        assert "errors" not in d

    def test_failure_result_to_dict(self):
        result = ProfileSyncResult(
            success=False,
            action="none",
            errors=["Invalid JSON"],
        )
        d = result.to_dict()
        assert d["success"] is False
        assert d["action"] == "none"
        assert d["errors"] == ["Invalid JSON"]
        assert "profile_id" not in d

    def test_result_with_warnings(self):
        result = ProfileSyncResult(
            success=True,
            action="updated",
            profile_id="test",
            name="Test",
            warnings=["Unrecognized tools: foo"],
        )
        d = result.to_dict()
        assert d["warnings"] == ["Unrecognized tools: foo"]

    def test_frozen_result(self):
        result = ProfileSyncResult(success=True, action="created")
        assert result.success is True


# ---------------------------------------------------------------------------
# sync_profile_to_db — core upsert
# ---------------------------------------------------------------------------


class TestSyncProfileToDb:
    """Test the core profile-to-DB upsert function."""

    @pytest.mark.asyncio
    async def test_create_new_profile(self, db):
        """Syncing a profile that doesn't exist creates it."""
        parsed = parse_profile(MINIMAL_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is True
        assert result.action == "created"
        assert result.profile_id == "test-agent"
        assert result.name == "Test Agent"

        # Verify in DB
        profile = await db.get_profile("test-agent")
        assert profile is not None
        assert profile.name == "Test Agent"
        assert profile.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_update_existing_profile(self, db):
        """Syncing a profile that already exists updates it."""
        # Create initial profile
        await db.create_profile(
            AgentProfile(
                id="test-agent",
                name="Old Name",
                model="old-model",
            )
        )

        # Sync new version
        parsed = parse_profile(MINIMAL_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is True
        assert result.action == "updated"

        # Verify updated fields
        profile = await db.get_profile("test-agent")
        assert profile.name == "Test Agent"
        assert profile.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_full_profile_all_fields(self, db):
        """Full profile with all sections syncs all fields correctly."""
        parsed = parse_profile(FULL_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is True
        assert result.action == "created"
        assert result.profile_id == "coding"

        profile = await db.get_profile("coding")
        assert profile.name == "Coding Agent"
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert profile.allowed_tools == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        assert "github" in profile.mcp_servers
        assert profile.mcp_servers["github"]["command"] == "npx"
        assert "## Role" in profile.system_prompt_suffix
        assert "## Rules" in profile.system_prompt_suffix
        assert "## Reflection" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_invalid_json_fails(self, db):
        """Profile with invalid JSON in structured sections fails sync."""
        parsed = parse_profile(INVALID_JSON_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is False
        assert result.action == "none"
        assert result.errors
        assert any("Invalid JSON" in e for e in result.errors)

        # DB should not have the profile
        assert await db.get_profile("broken") is None

    @pytest.mark.asyncio
    async def test_invalid_json_preserves_existing(self, db):
        """Failed sync due to invalid JSON preserves the existing DB row."""
        # Create initial good profile
        await db.create_profile(
            AgentProfile(
                id="broken",
                name="Original",
                model="original-model",
            )
        )

        # Attempt to sync broken profile
        parsed = parse_profile(INVALID_JSON_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is False

        # Original profile should be unchanged
        profile = await db.get_profile("broken")
        assert profile.name == "Original"
        assert profile.model == "original-model"

    @pytest.mark.asyncio
    async def test_missing_id_uses_fallback(self, db):
        """Profile without frontmatter ID uses fallback_id."""
        parsed = parse_profile(NO_ID_PROFILE)
        result = await sync_profile_to_db(parsed, db, fallback_id="derived-id")

        assert result.success is True
        assert result.profile_id == "derived-id"

        profile = await db.get_profile("derived-id")
        assert profile is not None
        assert profile.name == "Unnamed Agent"

    @pytest.mark.asyncio
    async def test_missing_id_no_fallback_fails(self, db):
        """Profile without ID and no fallback fails sync."""
        parsed = parse_profile(NO_ID_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is False
        assert result.action == "none"
        assert result.errors
        assert any("ID is required" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_missing_name_uses_id(self, db):
        """Profile with ID but no name uses ID as name fallback."""
        text = """\
---
id: nameless
---

## Config
```json
{"model": "test"}
```
"""
        parsed = parse_profile(text)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is True
        assert result.name == "nameless"

        profile = await db.get_profile("nameless")
        assert profile.name == "nameless"

    @pytest.mark.asyncio
    async def test_unknown_tools_produce_warnings(self, db):
        """Unrecognized tool names produce warnings but don't fail sync."""
        parsed = parse_profile(UNKNOWN_TOOLS_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is True
        assert result.warnings
        assert any("my_custom_tool" in w for w in result.warnings)
        assert any("another_unknown_tool" in w for w in result.warnings)

        # Profile should still be created with the tools
        profile = await db.get_profile("custom-tools")
        assert "my_custom_tool" in profile.allowed_tools

    @pytest.mark.asyncio
    async def test_empty_profile_no_id(self, db):
        """Empty profile (no sections, no frontmatter) fails."""
        parsed = parse_profile("")
        result = await sync_profile_to_db(parsed, db)

        assert result.success is False
        assert any("ID is required" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_source_path_passed_through(self, db):
        """source_path is for logging only, doesn't affect sync."""
        parsed = parse_profile(MINIMAL_PROFILE)
        result = await sync_profile_to_db(
            parsed, db, source_path="/vault/agent-types/test/profile.md"
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_frontmatter_id_takes_precedence_over_fallback(self, db):
        """When both frontmatter ID and fallback_id exist, frontmatter wins."""
        parsed = parse_profile(MINIMAL_PROFILE)  # has id: test-agent
        result = await sync_profile_to_db(parsed, db, fallback_id="fallback-id")

        assert result.success is True
        assert result.profile_id == "test-agent"
        assert await db.get_profile("fallback-id") is None

    @pytest.mark.asyncio
    async def test_idempotent_sync(self, db):
        """Syncing the same profile twice produces 'created' then 'updated'."""
        parsed = parse_profile(MINIMAL_PROFILE)

        r1 = await sync_profile_to_db(parsed, db)
        assert r1.action == "created"

        r2 = await sync_profile_to_db(parsed, db)
        assert r2.action == "updated"

        # Only one row in DB
        profiles = await db.list_profiles()
        assert len([p for p in profiles if p.id == "test-agent"]) == 1

    @pytest.mark.asyncio
    async def test_profile_with_no_tools_section(self, db):
        """Profile without a Tools section syncs with empty allowed_tools."""
        text = """\
---
id: no-tools
name: No Tools Agent
---

## Config
```json
{"model": "test"}
```
"""
        parsed = parse_profile(text)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is True
        profile = await db.get_profile("no-tools")
        assert profile.allowed_tools == []

    @pytest.mark.asyncio
    async def test_profile_with_no_config_section(self, db):
        """Profile without a Config section syncs with empty model/permission_mode."""
        text = """\
---
id: no-config
name: No Config Agent
---

## Role
You do things.
"""
        parsed = parse_profile(text)
        result = await sync_profile_to_db(parsed, db)

        assert result.success is True
        profile = await db.get_profile("no-config")
        assert profile.model == ""
        assert profile.permission_mode == ""
        assert "## Role" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_mcp_servers_preserved(self, db):
        """MCP server configuration is preserved through sync."""
        parsed = parse_profile(FULL_PROFILE)
        await sync_profile_to_db(parsed, db)

        profile = await db.get_profile("coding")
        github = profile.mcp_servers["github"]
        assert github["command"] == "npx"
        assert github["args"] == ["-y", "@modelcontextprotocol/server-github"]
        assert github["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"


# ---------------------------------------------------------------------------
# sync_profile_text_to_db — convenience wrapper
# ---------------------------------------------------------------------------


class TestSyncProfileTextToDb:
    """Test the text-to-DB convenience wrapper."""

    @pytest.mark.asyncio
    async def test_text_sync_creates_profile(self, db):
        result = await sync_profile_text_to_db(MINIMAL_PROFILE, db)

        assert result.success is True
        assert result.action == "created"
        assert result.profile_id == "test-agent"

    @pytest.mark.asyncio
    async def test_text_sync_with_fallback_id(self, db):
        result = await sync_profile_text_to_db(NO_ID_PROFILE, db, fallback_id="from-path")
        assert result.success is True
        assert result.profile_id == "from-path"

    @pytest.mark.asyncio
    async def test_text_sync_invalid_json(self, db):
        result = await sync_profile_text_to_db(INVALID_JSON_PROFILE, db)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_text_sync_empty_string(self, db):
        result = await sync_profile_text_to_db("", db)
        assert result.success is False


# ---------------------------------------------------------------------------
# on_profile_changed — VaultWatcher handler
# ---------------------------------------------------------------------------


class TestOnProfileChanged:
    """Test the VaultWatcher handler for profile.md changes."""

    @pytest.mark.asyncio
    async def test_created_profile_synced_to_db(self, db, tmp_path):
        """Creating a profile.md triggers sync to DB."""
        profile_path = tmp_path / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db)

        profile = await db.get_profile("coding")
        assert profile is not None
        assert profile.name == "Coding Agent"
        assert profile.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_modified_profile_updates_db(self, db, tmp_path):
        """Modifying a profile.md updates the existing DB row."""
        # Create initial profile in DB
        await db.create_profile(AgentProfile(id="coding", name="Old Name"))

        # Write updated file
        profile_path = tmp_path / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="modified",
        )
        await on_profile_changed([change], db=db)

        profile = await db.get_profile("coding")
        assert profile.name == "Coding Agent"  # Updated

    @pytest.mark.asyncio
    async def test_deleted_profile_retains_db_row(self, db):
        """Deleting a profile.md does NOT remove the DB row (per spec)."""
        await db.create_profile(AgentProfile(id="coding", name="Coding Agent"))

        change = VaultChange(
            path="/vault/agent-types/coding/profile.md",
            rel_path="agent-types/coding/profile.md",
            operation="deleted",
        )
        await on_profile_changed([change], db=db)

        # DB row should still exist
        profile = await db.get_profile("coding")
        assert profile is not None
        assert profile.name == "Coding Agent"

    @pytest.mark.asyncio
    async def test_no_db_logs_only(self):
        """Without DB, handler just logs (no error)."""
        change = VaultChange(
            path="/vault/agent-types/coding/profile.md",
            rel_path="agent-types/coding/profile.md",
            operation="modified",
        )
        # Should complete without error (log-only mode)
        await on_profile_changed([change], db=None)

    @pytest.mark.asyncio
    async def test_no_db_default(self):
        """Handler with default db=None doesn't crash."""
        change = VaultChange(
            path="/vault/agent-types/coding/profile.md",
            rel_path="agent-types/coding/profile.md",
            operation="created",
        )
        await on_profile_changed([change])

    @pytest.mark.asyncio
    async def test_unreadable_file(self, db):
        """Handler gracefully handles unreadable files."""
        change = VaultChange(
            path="/nonexistent/path/profile.md",
            rel_path="agent-types/ghost/profile.md",
            operation="created",
        )
        # Should not raise
        await on_profile_changed([change], db=db)

    @pytest.mark.asyncio
    async def test_invalid_json_file(self, db, tmp_path):
        """Handler gracefully handles profile with invalid JSON."""
        profile_path = tmp_path / "agent-types" / "broken" / "profile.md"
        _create_file(str(profile_path), INVALID_JSON_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/broken/profile.md",
            operation="created",
        )
        # Should not raise, and DB should not have the profile
        await on_profile_changed([change], db=db)
        assert await db.get_profile("broken") is None

    @pytest.mark.asyncio
    async def test_fallback_id_from_path(self, db, tmp_path):
        """Profile without frontmatter ID uses path-derived ID."""
        profile_path = tmp_path / "agent-types" / "derived" / "profile.md"
        _create_file(str(profile_path), NO_ID_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/derived/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db)

        # Should have used path-derived ID "derived"
        profile = await db.get_profile("derived")
        assert profile is not None
        assert profile.name == "Unnamed Agent"

    @pytest.mark.asyncio
    async def test_multiple_changes_in_batch(self, db, tmp_path):
        """Multiple profile changes in one batch are all processed."""
        p1 = tmp_path / "agent-types" / "a" / "profile.md"
        p2 = tmp_path / "agent-types" / "b" / "profile.md"
        _create_file(str(p1), MINIMAL_PROFILE.replace("test-agent", "a").replace("Test Agent", "A"))
        _create_file(str(p2), MINIMAL_PROFILE.replace("test-agent", "b").replace("Test Agent", "B"))

        changes = [
            VaultChange(path=str(p1), rel_path="agent-types/a/profile.md", operation="created"),
            VaultChange(path=str(p2), rel_path="agent-types/b/profile.md", operation="created"),
        ]
        await on_profile_changed(changes, db=db)

        assert await db.get_profile("a") is not None
        assert await db.get_profile("b") is not None

    @pytest.mark.asyncio
    async def test_empty_changes_list(self, db):
        """Empty changes list is handled gracefully."""
        await on_profile_changed([], db=db)

    @pytest.mark.asyncio
    async def test_orchestrator_profile(self, db, tmp_path):
        """orchestrator/profile.md is synced with path-derived ID."""
        text = """\
---
name: Orchestrator Profile
---

## Role
You coordinate agents.
"""
        profile_path = tmp_path / "orchestrator" / "profile.md"
        _create_file(str(profile_path), text)

        change = VaultChange(
            path=str(profile_path),
            rel_path="orchestrator/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db)

        profile = await db.get_profile("orchestrator")
        assert profile is not None
        assert profile.name == "Orchestrator Profile"


# ---------------------------------------------------------------------------
# register_profile_handlers
# ---------------------------------------------------------------------------


class TestRegisterProfileHandlers:
    """Test handler registration with VaultWatcher."""

    def test_registers_correct_number_of_handlers(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path), poll_interval=0, debounce_seconds=0)
        ids = register_profile_handlers(watcher)
        assert len(ids) == len(PROFILE_PATTERNS)
        assert watcher.get_handler_count() == len(PROFILE_PATTERNS)

    def test_returns_unique_handler_ids(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path), poll_interval=0, debounce_seconds=0)
        ids = register_profile_handlers(watcher)
        assert len(set(ids)) == len(ids)

    def test_handler_ids_are_deterministic(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path), poll_interval=0, debounce_seconds=0)
        ids = register_profile_handlers(watcher)
        for hid in ids:
            assert hid.startswith("profile:")

    def test_handlers_can_be_unregistered(self, tmp_path):
        watcher = VaultWatcher(str(tmp_path), poll_interval=0, debounce_seconds=0)
        ids = register_profile_handlers(watcher)
        for hid in ids:
            assert watcher.unregister_handler(hid) is True
        assert watcher.get_handler_count() == 0

    def test_re_registration_replaces_handlers(self, tmp_path):
        """Re-registering with a new DB replaces handlers (deterministic IDs)."""
        watcher = VaultWatcher(str(tmp_path), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=None)
        assert watcher.get_handler_count() == len(PROFILE_PATTERNS)

        # Re-register — same handler IDs should be reused
        register_profile_handlers(watcher, db="mock_db")
        assert watcher.get_handler_count() == len(PROFILE_PATTERNS)

    def test_patterns_include_agent_types(self):
        assert "agent-types/*/profile.md" in PROFILE_PATTERNS

    def test_patterns_include_orchestrator(self):
        assert "orchestrator/profile.md" in PROFILE_PATTERNS


# ---------------------------------------------------------------------------
# End-to-end dispatch through VaultWatcher with DB sync
# ---------------------------------------------------------------------------


class TestProfileSyncEndToEnd:
    """Integration tests: file change -> VaultWatcher -> DB sync."""

    @pytest.mark.asyncio
    async def test_agent_type_profile_creation_synced(self, db, tmp_path):
        """Creating agent-types/coding/profile.md syncs to DB."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)

        # Take initial snapshot
        await watcher.check()

        # Create a profile file
        _create_file(str(vault / "agent-types" / "coding" / "profile.md"), FULL_PROFILE)

        # Detect and dispatch
        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].operation == "created"

        # Profile should now be in DB
        profile = await db.get_profile("coding")
        assert profile is not None
        assert profile.name == "Coding Agent"
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert "github" in profile.mcp_servers

    @pytest.mark.asyncio
    async def test_profile_modification_synced(self, db, tmp_path):
        """Modifying a profile.md triggers DB update."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "test" / "profile.md"
        _create_file(str(profile_path), MINIMAL_PROFILE)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)

        # Initial snapshot + first sync
        await watcher.check()

        # Simulate file creation detected
        # (The initial check() captures the snapshot but doesn't dispatch,
        # so we need to re-create or touch the file)
        new_content = MINIMAL_PROFILE.replace("Test Agent", "Updated Agent")
        with open(str(profile_path), "w") as f:
            f.write(new_content)
        _touch(str(profile_path))

        changes = await watcher.check()
        assert len(changes) >= 1

        profile = await db.get_profile("test-agent")
        assert profile is not None
        assert profile.name == "Updated Agent"

    @pytest.mark.asyncio
    async def test_profile_deletion_retains_db(self, db, tmp_path):
        """Deleting a profile.md does not remove the DB row."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "test" / "profile.md"
        _create_file(str(profile_path), MINIMAL_PROFILE)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)

        await watcher.check()

        # Manually create the DB row (simulating previous sync)
        await db.create_profile(AgentProfile(id="test-agent", name="Test Agent"))

        # Delete the file
        os.remove(str(profile_path))
        await watcher.check()

        # DB row should still exist
        profile = await db.get_profile("test-agent")
        assert profile is not None

    @pytest.mark.asyncio
    async def test_invalid_profile_not_synced(self, db, tmp_path):
        """Profile with invalid JSON is not synced to DB."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)

        await watcher.check()

        _create_file(
            str(vault / "agent-types" / "broken" / "profile.md"),
            INVALID_JSON_PROFILE,
        )

        await watcher.check()

        # Profile should NOT be in DB
        assert await db.get_profile("broken") is None

    @pytest.mark.asyncio
    async def test_multiple_profiles_synced(self, db, tmp_path):
        """Multiple profile files created at once are all synced."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)

        await watcher.check()

        for agent_type in ["coding", "review", "qa"]:
            text = f"""\
---
id: {agent_type}
name: {agent_type.title()} Agent
---

## Config
```json
{{"model": "claude-sonnet-4-6"}}
```
"""
            _create_file(
                str(vault / "agent-types" / agent_type / "profile.md"),
                text,
            )

        await watcher.check()

        for agent_type in ["coding", "review", "qa"]:
            profile = await db.get_profile(agent_type)
            assert profile is not None, f"Profile {agent_type} not found in DB"

    @pytest.mark.asyncio
    async def test_orchestrator_profile_synced(self, db, tmp_path):
        """orchestrator/profile.md is synced to DB."""
        vault = tmp_path / "vault"
        vault.mkdir()

        text = """\
---
id: orchestrator
name: Orchestrator
---

## Role
You coordinate agents.
"""
        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)

        await watcher.check()

        _create_file(str(vault / "orchestrator" / "profile.md"), text)

        await watcher.check()

        profile = await db.get_profile("orchestrator")
        assert profile is not None
        assert profile.name == "Orchestrator"

    @pytest.mark.asyncio
    async def test_no_db_mode(self, tmp_path):
        """Without DB, handler just logs (no crash)."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher)  # No db=

        await watcher.check()

        _create_file(
            str(vault / "agent-types" / "test" / "profile.md"),
            MINIMAL_PROFILE,
        )

        # Should not raise
        await watcher.check()


# ---------------------------------------------------------------------------
# Database upsert_profile
# ---------------------------------------------------------------------------


class TestUpsertProfile:
    """Test the database upsert_profile method directly."""

    @pytest.mark.asyncio
    async def test_upsert_creates_new(self, db):
        profile = AgentProfile(id="new", name="New Profile", model="test-model")
        action = await db.upsert_profile(profile)
        assert action == "created"

        fetched = await db.get_profile("new")
        assert fetched.name == "New Profile"
        assert fetched.model == "test-model"

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, db):
        await db.create_profile(AgentProfile(id="existing", name="Old", model="old"))
        profile = AgentProfile(id="existing", name="Updated", model="new-model")
        action = await db.upsert_profile(profile)
        assert action == "updated"

        fetched = await db.get_profile("existing")
        assert fetched.name == "Updated"
        assert fetched.model == "new-model"

    @pytest.mark.asyncio
    async def test_upsert_preserves_created_at(self, db):
        """Upsert-as-update should preserve the original created_at."""
        profile = AgentProfile(id="ts-test", name="Timestamp Test")
        await db.create_profile(profile)

        # Get the created_at from the raw DB
        async with db._engine.begin() as conn:
            from sqlalchemy import select
            from src.database.tables import agent_profiles

            result = await conn.execute(
                select(agent_profiles.c.created_at).where(agent_profiles.c.id == "ts-test")
            )
            original_created_at = result.scalar()

        # Update via upsert
        updated = AgentProfile(id="ts-test", name="Updated Name")
        await db.upsert_profile(updated)

        # created_at should be unchanged
        async with db._engine.begin() as conn:
            result = await conn.execute(
                select(agent_profiles.c.created_at).where(agent_profiles.c.id == "ts-test")
            )
            new_created_at = result.scalar()

        assert original_created_at == new_created_at

    @pytest.mark.asyncio
    async def test_upsert_updates_all_fields(self, db):
        """Upsert updates all mutable fields."""
        await db.create_profile(
            AgentProfile(
                id="full",
                name="Original",
                description="old desc",
                model="old-model",
                permission_mode="plan",
                allowed_tools=["Read"],
                mcp_servers={"old": {"command": "old"}},
                system_prompt_suffix="old prompt",
            )
        )

        updated = AgentProfile(
            id="full",
            name="New Name",
            description="new desc",
            model="new-model",
            permission_mode="auto",
            allowed_tools=["Read", "Write", "Edit"],
            mcp_servers={"new": {"command": "new"}},
            system_prompt_suffix="new prompt",
        )
        action = await db.upsert_profile(updated)
        assert action == "updated"

        fetched = await db.get_profile("full")
        assert fetched.name == "New Name"
        assert fetched.description == "new desc"
        assert fetched.model == "new-model"
        assert fetched.permission_mode == "auto"
        assert fetched.allowed_tools == ["Read", "Write", "Edit"]
        assert fetched.mcp_servers == {"new": {"command": "new"}}
        assert fetched.system_prompt_suffix == "new prompt"


# ---------------------------------------------------------------------------
# Notification on sync failure (Roadmap 4.1.8)
# ---------------------------------------------------------------------------


class TestProfileSyncNotifications:
    """Test that sync failures emit notify.profile_sync_failed events."""

    @pytest.fixture
    def event_bus(self):
        from src.event_bus import EventBus

        return EventBus(env="dev", validate_events=True)

    @pytest.fixture
    def event_collector(self, event_bus):
        """Subscribe to all events and collect them."""
        events: list[dict] = []

        def _handler(data: dict) -> None:
            events.append(data)

        event_bus.subscribe("notify.profile_sync_failed", _handler)
        return events

    @pytest.mark.asyncio
    async def test_invalid_json_emits_notification(self, db, tmp_path, event_bus, event_collector):
        """Bad JSON in profile.md emits notify.profile_sync_failed."""
        profile_path = tmp_path / "agent-types" / "broken" / "profile.md"
        _create_file(str(profile_path), INVALID_JSON_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/broken/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        # DB should not have the profile
        assert await db.get_profile("broken") is None

        # Notification should have been emitted
        assert len(event_collector) == 1
        event = event_collector[0]
        assert event["event_type"] == "notify.profile_sync_failed"
        assert event["severity"] == "error"
        assert event["category"] == "system"
        assert any("Invalid JSON" in e for e in event["errors"])
        assert event["source_path"] == "agent-types/broken/profile.md"

    @pytest.mark.asyncio
    async def test_invalid_json_preserves_existing_and_notifies(
        self, db, tmp_path, event_bus, event_collector
    ):
        """Bad JSON preserves existing DB row AND sends notification."""
        # Seed good profile
        await db.create_profile(AgentProfile(id="broken", name="Original", model="original-model"))

        profile_path = tmp_path / "agent-types" / "broken" / "profile.md"
        _create_file(str(profile_path), INVALID_JSON_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/broken/profile.md",
            operation="modified",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        # Previous config retained
        profile = await db.get_profile("broken")
        assert profile.name == "Original"
        assert profile.model == "original-model"

        # Notification sent
        assert len(event_collector) == 1
        assert event_collector[0]["event_type"] == "notify.profile_sync_failed"

    @pytest.mark.asyncio
    async def test_successful_sync_no_notification(self, db, tmp_path, event_bus, event_collector):
        """Successful sync does NOT emit a failure notification."""
        profile_path = tmp_path / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert await db.get_profile("coding") is not None
        assert len(event_collector) == 0

    @pytest.mark.asyncio
    async def test_no_event_bus_no_crash(self, db, tmp_path):
        """Sync failure without event bus doesn't crash (backward compat)."""
        profile_path = tmp_path / "agent-types" / "broken" / "profile.md"
        _create_file(str(profile_path), INVALID_JSON_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/broken/profile.md",
            operation="created",
        )
        # No event_bus passed — should not crash
        await on_profile_changed([change], db=db)
        assert await db.get_profile("broken") is None

    @pytest.mark.asyncio
    async def test_notification_includes_source_path(
        self, db, tmp_path, event_bus, event_collector
    ):
        """Notification event includes the source file path."""
        profile_path = tmp_path / "agent-types" / "bad" / "profile.md"
        _create_file(str(profile_path), INVALID_JSON_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/bad/profile.md",
            operation="modified",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        assert event_collector[0]["source_path"] == "agent-types/bad/profile.md"

    @pytest.mark.asyncio
    async def test_multiple_failures_emit_multiple_notifications(
        self, db, tmp_path, event_bus, event_collector
    ):
        """Each failed sync in a batch emits its own notification."""
        p1 = tmp_path / "agent-types" / "bad1" / "profile.md"
        p2 = tmp_path / "agent-types" / "bad2" / "profile.md"
        _create_file(str(p1), INVALID_JSON_PROFILE)
        _create_file(str(p2), INVALID_JSON_PROFILE)

        changes = [
            VaultChange(path=str(p1), rel_path="agent-types/bad1/profile.md", operation="created"),
            VaultChange(path=str(p2), rel_path="agent-types/bad2/profile.md", operation="created"),
        ]
        await on_profile_changed(changes, db=db, event_bus=event_bus)

        assert len(event_collector) == 2
        paths = {e["source_path"] for e in event_collector}
        assert "agent-types/bad1/profile.md" in paths
        assert "agent-types/bad2/profile.md" in paths

    @pytest.mark.asyncio
    async def test_register_handlers_passes_event_bus(
        self, db, tmp_path, event_bus, event_collector
    ):
        """register_profile_handlers wires event_bus through to handler."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db, event_bus=event_bus)

        await watcher.check()

        _create_file(
            str(vault / "agent-types" / "broken" / "profile.md"),
            INVALID_JSON_PROFILE,
        )

        await watcher.check()

        # Notification should have been emitted through the watcher pipeline
        assert len(event_collector) == 1
        assert event_collector[0]["event_type"] == "notify.profile_sync_failed"

    @pytest.mark.asyncio
    async def test_notification_event_schema_valid(self, db, tmp_path, event_bus, event_collector):
        """Emitted event passes schema validation."""
        profile_path = tmp_path / "agent-types" / "broken" / "profile.md"
        _create_file(str(profile_path), INVALID_JSON_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/broken/profile.md",
            operation="created",
        )
        # EventBus in dev mode raises on validation errors — this would fail
        # if the event payload doesn't match the schema
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        event = event_collector[0]
        # Verify required fields are present
        assert "event_type" in event
        assert "severity" in event
        assert "category" in event


# ---------------------------------------------------------------------------
# Roadmap 4.1.10 — Profile parser and DB sync integration
# ---------------------------------------------------------------------------
#
# Tests (a)-(g) per docs/specs/design/roadmap.md §4.1.10:
#   (a) Valid profile.md with all sections parses every field correctly
#   (b) Parsed Config JSON values sync to agent_profiles DB table
#   (c) Parsed Tools list syncs and tool names validated against registry
#   (d) English sections (Role, Rules, Reflection) stored as raw markdown
#   (e) Partial profile parses present sections, leaves others as defaults
#   (f) Round-trip: write → parse → sync to DB → read DB → verify
#   (g) Sync is an upsert — existing profile updated, not duplicated
# ---------------------------------------------------------------------------

# A comprehensive profile with all sections for roadmap tests.
ROADMAP_FULL_PROFILE = """\
---
id: full-agent
name: Full Integration Agent
tags: [test, integration]
---

# Full Integration Agent

## Role
You are a **senior software engineer** specializing in Python backend services.
You follow project conventions, write thorough tests, and produce clean,
well-documented code.

### Primary Responsibilities
- Implement new features
- Fix bugs and regressions

## Config
```json
{
  "model": "claude-sonnet-4-6",
  "permission_mode": "auto",
  "max_tokens_per_task": 200000
}
```

## Tools
```json
{
  "allowed": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
  "denied": ["WebSearch"]
}
```

## MCP Servers
```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
  },
  "playwright": {
    "command": "npx",
    "args": ["@anthropic/mcp-playwright"]
  }
}
```

## Rules
- Always run existing tests before committing
- Never commit secrets, .env files, or credentials
- Prefer small, focused commits over large ones
- If tests fail after your changes, fix them before moving on

## Reflection
After completing a task, consider:
- Did I encounter any surprising behavior worth remembering?
- Did I resolve an error that might recur? If so, save the pattern.
"""


class TestRoadmap4110:
    """Roadmap 4.1.10 — Profile parser and DB sync integration tests.

    Validates the full pipeline per profiles spec §2 (Hybrid Format) and
    §3 (Sync Model): markdown file → parse → validate → upsert DB row.
    """

    # -------------------------------------------------------------------
    # (a) Valid profile.md with all sections parses every field correctly
    # -------------------------------------------------------------------

    def test_a_all_sections_parse_frontmatter(self):
        """(a) Frontmatter fields (id, name, tags) are parsed correctly."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid, f"Parse errors: {result.errors}"
        assert result.frontmatter.id == "full-agent"
        assert result.frontmatter.name == "Full Integration Agent"
        assert result.frontmatter.tags == ["test", "integration"]

    def test_a_all_sections_parse_config(self):
        """(a) Config section JSON is parsed with all three known keys."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid
        assert result.config["model"] == "claude-sonnet-4-6"
        assert result.config["permission_mode"] == "auto"
        assert result.config["max_tokens_per_task"] == 200000

    def test_a_all_sections_parse_tools(self):
        """(a) Tools section JSON is parsed with allowed and denied lists."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid
        assert result.tools["allowed"] == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        assert result.tools["denied"] == ["WebSearch"]

    def test_a_all_sections_parse_mcp_servers(self):
        """(a) MCP Servers section JSON is parsed with all server definitions."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid
        assert len(result.mcp_servers) == 2
        assert "github" in result.mcp_servers
        assert "playwright" in result.mcp_servers

        github = result.mcp_servers["github"]
        assert github["command"] == "npx"
        assert github["args"] == ["-y", "@modelcontextprotocol/server-github"]
        assert github["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"

        playwright = result.mcp_servers["playwright"]
        assert playwright["command"] == "npx"
        assert playwright["args"] == ["@anthropic/mcp-playwright"]

    def test_a_all_sections_parse_role(self):
        """(a) Role section is parsed as text content."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid
        assert "senior software engineer" in result.role
        assert "Python backend services" in result.role
        assert "### Primary Responsibilities" in result.role

    def test_a_all_sections_parse_rules(self):
        """(a) Rules section is parsed as text content."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid
        assert "- Always run existing tests before committing" in result.rules
        assert "- Never commit secrets" in result.rules
        assert "- Prefer small, focused commits" in result.rules

    def test_a_all_sections_parse_reflection(self):
        """(a) Reflection section is parsed as text content."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid
        assert "After completing a task, consider:" in result.reflection
        assert "surprising behavior" in result.reflection
        assert "save the pattern" in result.reflection

    def test_a_all_six_sections_present(self):
        """(a) All six sections are stored in the sections dict."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid
        expected = {"config", "tools", "mcp servers", "role", "rules", "reflection"}
        assert expected == set(result.sections.keys())

    def test_a_no_errors_or_warnings(self):
        """(a) A valid full profile produces no errors and no warnings."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.is_valid
        assert result.errors == []
        assert result.warnings == []

    # -------------------------------------------------------------------
    # (b) Parsed Config JSON values sync to agent_profiles DB table
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_b_config_model_syncs_to_db(self, db):
        """(b) Config 'model' value syncs to the agent_profiles.model column."""
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert result.success

        profile = await db.get_profile("full-agent")
        assert profile.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_b_config_permission_mode_syncs_to_db(self, db):
        """(b) Config 'permission_mode' syncs to agent_profiles.permission_mode."""
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert result.success

        profile = await db.get_profile("full-agent")
        assert profile.permission_mode == "auto"

    @pytest.mark.asyncio
    async def test_b_config_max_tokens_parsed_not_lost(self, db):
        """(b) Config 'max_tokens_per_task' is parsed and validated correctly.

        Note: max_tokens_per_task is validated at parse time and available
        in ParsedProfile.config but is not mapped to a dedicated AgentProfile
        field.  It may be consumed at task-execution time from the parsed config.
        """
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        assert parsed.config["max_tokens_per_task"] == 200000
        assert parsed.is_valid  # Validation passed

    @pytest.mark.asyncio
    async def test_b_all_valid_permission_modes_sync(self, db):
        """(b) Each valid permission_mode value syncs without error."""
        from src.profile_parser import VALID_PERMISSION_MODES

        for mode in sorted(VALID_PERMISSION_MODES):
            text = f"""\
---
id: mode-test-{mode}
name: Mode Test {mode}
---

## Config
```json
{{"permission_mode": "{mode}"}}
```
"""
            parsed = parse_profile(text)
            assert parsed.is_valid, f"Mode {mode!r} should be valid: {parsed.errors}"
            result = await sync_profile_to_db(parsed, db)
            assert result.success, f"Sync failed for mode {mode!r}: {result.errors}"

            profile = await db.get_profile(f"mode-test-{mode}")
            assert profile.permission_mode == mode

    @pytest.mark.asyncio
    async def test_b_invalid_permission_mode_rejected(self, db):
        """(b) Invalid permission_mode produces a parse error — not synced."""
        text = """\
---
id: bad-mode
name: Bad Mode
---

## Config
```json
{"permission_mode": "invalid_mode"}
```
"""
        parsed = parse_profile(text)
        assert not parsed.is_valid
        assert any("permission_mode" in e for e in parsed.errors)

        result = await sync_profile_to_db(parsed, db)
        assert not result.success
        assert await db.get_profile("bad-mode") is None

    # -------------------------------------------------------------------
    # (c) Parsed Tools list syncs and tool names validated
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_c_allowed_tools_sync_to_db(self, db):
        """(c) Parsed allowed tools list syncs to agent_profiles.allowed_tools."""
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert result.success

        profile = await db.get_profile("full-agent")
        expected = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        assert profile.allowed_tools == expected

    @pytest.mark.asyncio
    async def test_c_known_tools_no_warnings(self, db):
        """(c) Known tool names (from KNOWN_TOOL_NAMES) produce no warnings."""
        # All tools in ROADMAP_FULL_PROFILE are known Claude Code built-ins
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert result.success
        # No tool-related warnings — all are recognized
        assert result.warnings is None or not any("tool" in w.lower() for w in result.warnings)

    @pytest.mark.asyncio
    async def test_c_unknown_tools_produce_warnings_not_errors(self, db):
        """(c) Unrecognized tool names produce warnings (not errors) — sync succeeds."""
        text = """\
---
id: custom-tools
name: Custom Tools
---

## Tools
```json
{
  "allowed": ["Read", "my_custom_tool", "another_unknown"]
}
```
"""
        parsed = parse_profile(text)
        assert parsed.is_valid  # Unknown tools are warnings, not errors

        result = await sync_profile_to_db(parsed, db)
        assert result.success
        assert result.warnings
        assert any("my_custom_tool" in w for w in result.warnings)
        assert any("another_unknown" in w for w in result.warnings)

        # Tools are still stored even though some are unknown
        profile = await db.get_profile("custom-tools")
        assert "my_custom_tool" in profile.allowed_tools
        assert "another_unknown" in profile.allowed_tools

    def test_c_parser_tool_validation_with_known_tools(self):
        """(c) Parser validates tool names against known_tools when provided."""
        text = """\
---
id: validated
name: Validated
---

## Tools
```json
{
  "allowed": ["Read", "nonexistent_tool"]
}
```
"""
        known = {"Read", "Write", "Bash"}
        result = parse_profile(text, known_tools=known)
        assert result.is_valid  # Unknown tools are warnings, not errors
        assert any("nonexistent_tool" in w for w in result.warnings)

    def test_c_tools_structural_validation(self):
        """(c) Tools with invalid structure produce parse errors."""
        text = """\
## Tools
```json
{
  "allowed": [123, true, null]
}
```
"""
        result = parse_profile(text)
        assert not result.is_valid
        assert len(result.errors) >= 1
        assert any("must be a string" in e for e in result.errors)

    # -------------------------------------------------------------------
    # (d) English sections stored as raw markdown strings
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_d_role_stored_as_raw_markdown_in_db(self, db):
        """(d) Role markdown survives parse → sync → DB read as raw text."""
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        await sync_profile_to_db(parsed, db)

        profile = await db.get_profile("full-agent")
        # The system_prompt_suffix contains all prompt sections with labels
        assert "## Role" in profile.system_prompt_suffix
        assert "**senior software engineer**" in profile.system_prompt_suffix
        assert "### Primary Responsibilities" in profile.system_prompt_suffix
        assert "- Implement new features" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_d_rules_stored_as_raw_markdown_in_db(self, db):
        """(d) Rules markdown survives parse → sync → DB read as raw text."""
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        await sync_profile_to_db(parsed, db)

        profile = await db.get_profile("full-agent")
        assert "## Rules" in profile.system_prompt_suffix
        assert "- Always run existing tests before committing" in profile.system_prompt_suffix
        assert "- Never commit secrets" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_d_reflection_stored_as_raw_markdown_in_db(self, db):
        """(d) Reflection markdown survives parse → sync → DB read as raw text."""
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        await sync_profile_to_db(parsed, db)

        profile = await db.get_profile("full-agent")
        assert "## Reflection" in profile.system_prompt_suffix
        assert "After completing a task, consider:" in profile.system_prompt_suffix
        assert "surprising behavior" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_d_markdown_formatting_preserved_through_sync(self, db):
        """(d) Rich markdown (bold, sub-headings, lists) is preserved through DB sync."""
        text = """\
---
id: rich-md
name: Rich Markdown
---

## Role
You are a **bold** and _italic_ agent.

### Sub-heading
- List item one
- List item two

> A blockquote

## Rules
1. Numbered rule
2. Another rule with `inline code`

```python
# Code example in rules
def test(): pass
```
"""
        parsed = parse_profile(text)
        result = await sync_profile_to_db(parsed, db)
        assert result.success

        profile = await db.get_profile("rich-md")
        suffix = profile.system_prompt_suffix
        # Bold and italic preserved
        assert "**bold**" in suffix
        assert "_italic_" in suffix
        # Sub-headings preserved
        assert "### Sub-heading" in suffix
        # Lists preserved
        assert "- List item one" in suffix
        # Blockquote preserved
        assert "> A blockquote" in suffix
        # Numbered list preserved
        assert "1. Numbered rule" in suffix
        # Inline code preserved
        assert "`inline code`" in suffix
        # Code block in rules preserved
        assert "```python" in suffix
        assert "def test(): pass" in suffix

    def test_d_english_sections_are_not_json_parsed(self):
        """(d) English sections do not attempt JSON extraction."""
        result = parse_profile(ROADMAP_FULL_PROFILE)
        assert result.sections["role"].json_data is None
        assert result.sections["rules"].json_data is None
        assert result.sections["reflection"].json_data is None

    # -------------------------------------------------------------------
    # (e) Partial profile — present sections parsed, others defaulted
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_e_role_and_config_only(self, db):
        """(e) Profile with only Role + Config — Tools, MCP Servers, Rules, Reflection default."""
        text = """\
---
id: partial-rc
name: Role Config Only
---

## Role
You are a focused agent.

## Config
```json
{"model": "claude-sonnet-4-6", "permission_mode": "plan"}
```
"""
        parsed = parse_profile(text)
        assert parsed.is_valid
        # Present sections have content
        assert "focused agent" in parsed.role
        assert parsed.config["model"] == "claude-sonnet-4-6"
        assert parsed.config["permission_mode"] == "plan"
        # Missing sections use defaults
        assert parsed.tools == {}
        assert parsed.mcp_servers == {}
        assert parsed.rules == ""
        assert parsed.reflection == ""

        # Sync to DB — defaults should be empty/blank
        result = await sync_profile_to_db(parsed, db)
        assert result.success

        profile = await db.get_profile("partial-rc")
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "plan"
        assert profile.allowed_tools == []
        assert profile.mcp_servers == {}
        # system_prompt_suffix should contain only Role (no Rules/Reflection)
        assert "## Role" in profile.system_prompt_suffix
        assert "## Rules" not in profile.system_prompt_suffix
        assert "## Reflection" not in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_e_tools_and_mcp_only(self, db):
        """(e) Profile with only Tools + MCP Servers — Config, Role, Rules, Reflection default."""
        text = """\
---
id: partial-tm
name: Tools MCP Only
---

## Tools
```json
{"allowed": ["Read", "Bash"]}
```

## MCP Servers
```json
{"linter": {"command": "eslint-server"}}
```
"""
        parsed = parse_profile(text)
        assert parsed.is_valid
        assert parsed.tools["allowed"] == ["Read", "Bash"]
        assert "linter" in parsed.mcp_servers
        # Defaults
        assert parsed.config == {}
        assert parsed.role == ""
        assert parsed.rules == ""
        assert parsed.reflection == ""

        result = await sync_profile_to_db(parsed, db)
        assert result.success

        profile = await db.get_profile("partial-tm")
        assert profile.model == ""
        assert profile.permission_mode == ""
        assert profile.allowed_tools == ["Read", "Bash"]
        assert profile.mcp_servers["linter"]["command"] == "eslint-server"
        assert profile.system_prompt_suffix == ""

    @pytest.mark.asyncio
    async def test_e_rules_only(self, db):
        """(e) Profile with only Rules — everything else defaults."""
        text = """\
---
id: rules-only
name: Rules Only
---

## Rules
- Follow PEP 8
- Write docstrings
"""
        parsed = parse_profile(text)
        assert parsed.is_valid
        assert "- Follow PEP 8" in parsed.rules
        assert parsed.config == {}
        assert parsed.tools == {}
        assert parsed.mcp_servers == {}
        assert parsed.role == ""
        assert parsed.reflection == ""

        result = await sync_profile_to_db(parsed, db)
        assert result.success

        profile = await db.get_profile("rules-only")
        assert profile.model == ""
        assert profile.allowed_tools == []
        assert profile.mcp_servers == {}
        assert "## Rules" in profile.system_prompt_suffix
        assert "- Follow PEP 8" in profile.system_prompt_suffix
        # Only Rules in suffix — no Role or Reflection labels
        assert "## Role" not in profile.system_prompt_suffix
        assert "## Reflection" not in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_e_frontmatter_only(self, db):
        """(e) Profile with only frontmatter — all sections default."""
        text = """\
---
id: bare
name: Bare Profile
---
"""
        parsed = parse_profile(text)
        assert parsed.is_valid
        assert parsed.config == {}
        assert parsed.tools == {}
        assert parsed.mcp_servers == {}
        assert parsed.role == ""
        assert parsed.rules == ""
        assert parsed.reflection == ""

        result = await sync_profile_to_db(parsed, db)
        assert result.success

        profile = await db.get_profile("bare")
        assert profile.name == "Bare Profile"
        assert profile.model == ""
        assert profile.permission_mode == ""
        assert profile.allowed_tools == []
        assert profile.mcp_servers == {}
        assert profile.system_prompt_suffix == ""

    # -------------------------------------------------------------------
    # (f) Round-trip: write → parse → sync to DB → read DB → verify
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_f_round_trip_full_profile(self, db):
        """(f) Full round-trip: markdown → parse → sync → DB read → field verification."""
        # Step 1: Parse the markdown
        parsed = parse_profile(ROADMAP_FULL_PROFILE)
        assert parsed.is_valid

        # Step 2: Convert to AgentProfile dict (intermediate verification)
        profile_dict = parsed_profile_to_agent_profile(parsed)
        assert profile_dict["id"] == "full-agent"
        assert profile_dict["name"] == "Full Integration Agent"
        assert profile_dict["model"] == "claude-sonnet-4-6"
        assert profile_dict["permission_mode"] == "auto"
        assert profile_dict["allowed_tools"] == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        assert len(profile_dict["mcp_servers"]) == 2
        assert "role" in profile_dict
        assert "rules" in profile_dict
        assert "reflection" in profile_dict
        assert "system_prompt_suffix" in profile_dict

        # Step 3: Sync to database
        result = await sync_profile_to_db(parsed, db)
        assert result.success
        assert result.action == "created"
        assert result.profile_id == "full-agent"

        # Step 4: Read back from database
        profile = await db.get_profile("full-agent")
        assert profile is not None

        # Step 5: Verify ALL fields match the original parsed content
        assert profile.id == "full-agent"
        assert profile.name == "Full Integration Agent"
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert profile.allowed_tools == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

        # MCP servers round-trip
        assert len(profile.mcp_servers) == 2
        github = profile.mcp_servers["github"]
        assert github["command"] == "npx"
        assert github["args"] == ["-y", "@modelcontextprotocol/server-github"]
        assert github["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
        playwright = profile.mcp_servers["playwright"]
        assert playwright["command"] == "npx"
        assert playwright["args"] == ["@anthropic/mcp-playwright"]

        # Prompt sections round-trip (in system_prompt_suffix)
        assert "senior software engineer" in profile.system_prompt_suffix
        assert "### Primary Responsibilities" in profile.system_prompt_suffix
        assert "- Always run existing tests before committing" in profile.system_prompt_suffix
        assert "surprising behavior" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_f_round_trip_via_text_wrapper(self, db):
        """(f) Round-trip using sync_profile_text_to_db convenience wrapper."""
        # Single call: markdown text → parse → sync
        result = await sync_profile_text_to_db(ROADMAP_FULL_PROFILE, db)
        assert result.success
        assert result.profile_id == "full-agent"

        # Read back and verify key fields
        profile = await db.get_profile("full-agent")
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert profile.allowed_tools == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        assert "github" in profile.mcp_servers
        assert "playwright" in profile.mcp_servers
        assert "senior software engineer" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_f_round_trip_partial_profile(self, db):
        """(f) Round-trip with a partial profile preserves present fields and defaults."""
        text = """\
---
id: partial-rt
name: Partial Round-Trip
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```

## Role
You handle code reviews.
"""
        result = await sync_profile_text_to_db(text, db)
        assert result.success

        profile = await db.get_profile("partial-rt")
        assert profile.id == "partial-rt"
        assert profile.name == "Partial Round-Trip"
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == ""  # Not specified → default
        assert profile.allowed_tools == []  # No Tools section → empty
        assert profile.mcp_servers == {}  # No MCP Servers → empty
        assert "code reviews" in profile.system_prompt_suffix
        # Only Role in suffix
        assert "## Role" in profile.system_prompt_suffix
        assert "## Rules" not in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_f_round_trip_via_file_watcher(self, db, tmp_path):
        """(f) Round-trip through the VaultWatcher file handler pipeline."""
        profile_path = tmp_path / "agent-types" / "roundtrip" / "profile.md"
        _create_file(str(profile_path), ROADMAP_FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/roundtrip/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db)

        # Verify the full profile round-tripped through the watcher pipeline
        profile = await db.get_profile("full-agent")
        assert profile is not None
        assert profile.name == "Full Integration Agent"
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert profile.allowed_tools == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        assert len(profile.mcp_servers) == 2
        assert "senior software engineer" in profile.system_prompt_suffix

    # -------------------------------------------------------------------
    # (g) Sync is an upsert — existing profile updated, not duplicated
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_g_upsert_creates_then_updates(self, db):
        """(g) First sync creates; second sync updates the same row."""
        parsed = parse_profile(ROADMAP_FULL_PROFILE)

        r1 = await sync_profile_to_db(parsed, db)
        assert r1.success
        assert r1.action == "created"

        r2 = await sync_profile_to_db(parsed, db)
        assert r2.success
        assert r2.action == "updated"

    @pytest.mark.asyncio
    async def test_g_upsert_does_not_duplicate(self, db):
        """(g) Multiple syncs of the same profile produce exactly one DB row."""
        parsed = parse_profile(ROADMAP_FULL_PROFILE)

        for _ in range(5):
            result = await sync_profile_to_db(parsed, db)
            assert result.success

        profiles = await db.list_profiles()
        matching = [p for p in profiles if p.id == "full-agent"]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_g_upsert_updates_all_fields(self, db):
        """(g) Upsert updates every mutable field on the existing row."""
        # Create initial version
        v1_text = """\
---
id: evolving
name: Version One
---

## Config
```json
{"model": "old-model", "permission_mode": "plan"}
```

## Tools
```json
{"allowed": ["Read"]}
```

## Role
Original role.
"""
        r1 = await sync_profile_text_to_db(v1_text, db)
        assert r1.success
        assert r1.action == "created"

        p1 = await db.get_profile("evolving")
        assert p1.name == "Version One"
        assert p1.model == "old-model"
        assert p1.permission_mode == "plan"
        assert p1.allowed_tools == ["Read"]

        # Update with different values
        v2_text = """\
---
id: evolving
name: Version Two
---

## Config
```json
{"model": "new-model", "permission_mode": "auto"}
```

## Tools
```json
{"allowed": ["Read", "Write", "Bash"]}
```

## MCP Servers
```json
{"linter": {"command": "eslint"}}
```

## Role
Updated role.

## Rules
- New rule
"""
        r2 = await sync_profile_text_to_db(v2_text, db)
        assert r2.success
        assert r2.action == "updated"

        p2 = await db.get_profile("evolving")
        assert p2.name == "Version Two"
        assert p2.model == "new-model"
        assert p2.permission_mode == "auto"
        assert p2.allowed_tools == ["Read", "Write", "Bash"]
        assert p2.mcp_servers == {"linter": {"command": "eslint"}}
        assert "Updated role" in p2.system_prompt_suffix
        assert "- New rule" in p2.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_g_upsert_preserves_row_on_invalid_update(self, db):
        """(g) Failed sync (invalid JSON) preserves the existing DB row."""
        # Seed a valid profile
        await sync_profile_text_to_db(
            """\
---
id: stable
name: Stable Profile
---

## Config
```json
{"model": "good-model"}
```
""",
            db,
        )

        original = await db.get_profile("stable")
        assert original.model == "good-model"

        # Attempt to sync invalid version
        bad_text = """\
---
id: stable
name: Broken Stable
---

## Config
```json
{not valid json}
```
"""
        result = await sync_profile_text_to_db(bad_text, db)
        assert not result.success

        # Original row preserved unchanged
        preserved = await db.get_profile("stable")
        assert preserved.name == "Stable Profile"
        assert preserved.model == "good-model"

    @pytest.mark.asyncio
    async def test_g_upsert_only_one_row_after_create_update_cycle(self, db):
        """(g) Create → update → update produces exactly one row with latest values."""
        texts = [
            '---\nid: cycle\nname: V1\n---\n\n## Config\n```json\n{"model": "m1"}\n```\n',
            '---\nid: cycle\nname: V2\n---\n\n## Config\n```json\n{"model": "m2"}\n```\n',
            '---\nid: cycle\nname: V3\n---\n\n## Config\n```json\n{"model": "m3"}\n```\n',
        ]

        actions = []
        for text in texts:
            result = await sync_profile_text_to_db(text, db)
            assert result.success
            actions.append(result.action)

        assert actions == ["created", "updated", "updated"]

        # Only one row, with V3 values
        profiles = await db.list_profiles()
        matching = [p for p in profiles if p.id == "cycle"]
        assert len(matching) == 1
        assert matching[0].name == "V3"
        assert matching[0].model == "m3"


# ---------------------------------------------------------------------------
# Roadmap 4.1.11 — Profile error handling tests
# ---------------------------------------------------------------------------
#
# Tests (a)-(g) per docs/specs/design/roadmap.md §4.1.11:
#   (a) Malformed JSON in Config block triggers sync failure + notification
#   (b) DB row retains previous valid values after failed sync (no partial update)
#   (c) Invalid tool name in Tools produces warning but doesn't block sync
#   (d) Malformed JSON in MCP Servers triggers failure notification
#   (e) Completely empty profile.md does not crash parser (returns empty/default)
#   (f) Profile.md with valid frontmatter but garbled body sections fails gracefully
#   (g) Error notification includes file path and specific parse error
# ---------------------------------------------------------------------------

# --- Test fixtures for 4.1.11 ---

MALFORMED_CONFIG_PROFILE = """\
---
id: bad-config
name: Bad Config Agent
---

## Config
```json
{model: "claude-sonnet-4-6", trailing_comma: true,}
```
"""

MALFORMED_MCP_PROFILE = """\
---
id: bad-mcp
name: Bad MCP Agent
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```

## MCP Servers
```json
{not valid mcp json here!!!}
```
"""

GARBLED_BODY_PROFILE = """\
---
id: garbled
name: Garbled Body Agent
---

## Config
```json
@#$%^&*() this is not json at all
```

## Tools
```json
<xml>not json either</xml>
```

## MCP Servers
```json
[[[broken nesting
```
"""

VALID_FRONTMATTER_GARBLED_BODY_MIXED = """\
---
id: mixed-garble
name: Mixed Garbled Agent
tags: [test]
---

# Some Title

## Role
This role section is perfectly fine English text.

## Config
```json
{completely broken json :::}
```

## Rules
- These rules are valid markdown
- Nothing wrong here

## Tools
```json
{"allowed": ["Read", "Write"]}
```
"""


class TestRoadmap4111:
    """Roadmap 4.1.11 — Profile error handling tests.

    Validates graceful handling of malformed input per profiles spec §3
    (Sync Model): bad JSON triggers failure + notification, DB retains
    previous config, warnings don't block sync, empty/garbled input
    doesn't crash.
    """

    @pytest.fixture
    def event_bus(self):
        from src.event_bus import EventBus

        return EventBus(env="dev", validate_events=True)

    @pytest.fixture
    def event_collector(self, event_bus):
        """Subscribe to profile sync failure events and collect them."""
        events: list[dict] = []

        def _handler(data: dict) -> None:
            events.append(data)

        event_bus.subscribe("notify.profile_sync_failed", _handler)
        return events

    # -------------------------------------------------------------------
    # (a) Malformed JSON in Config block triggers sync failure + notification
    # -------------------------------------------------------------------

    def test_a_malformed_config_json_produces_parse_error(self):
        """(a) Malformed JSON in ## Config produces a parse error."""
        parsed = parse_profile(MALFORMED_CONFIG_PROFILE)
        assert not parsed.is_valid
        assert any("Invalid JSON" in e and "Config" in e for e in parsed.errors)

    @pytest.mark.asyncio
    async def test_a_malformed_config_json_fails_sync(self, db):
        """(a) Malformed JSON in ## Config causes sync_profile_to_db to fail."""
        parsed = parse_profile(MALFORMED_CONFIG_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert not result.success
        assert result.action == "none"
        assert result.errors
        assert any("Invalid JSON" in e for e in result.errors)

        # Profile should NOT exist in DB
        assert await db.get_profile("bad-config") is None

    @pytest.mark.asyncio
    async def test_a_malformed_config_json_emits_notification(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(a) Malformed JSON in ## Config triggers notify.profile_sync_failed."""
        profile_path = tmp_path / "agent-types" / "bad-config" / "profile.md"
        _create_file(str(profile_path), MALFORMED_CONFIG_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/bad-config/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        event = event_collector[0]
        assert event["event_type"] == "notify.profile_sync_failed"
        assert event["severity"] == "error"
        assert any("Invalid JSON" in e for e in event["errors"])

    @pytest.mark.asyncio
    async def test_a_malformed_config_does_not_create_db_row(self, db):
        """(a) No DB row is created when Config JSON is malformed."""
        result = await sync_profile_text_to_db(MALFORMED_CONFIG_PROFILE, db)
        assert not result.success

        # Verify no profile leaked into DB
        profiles = await db.list_profiles()
        assert not any(p.id == "bad-config" for p in profiles)

    # -------------------------------------------------------------------
    # (b) DB row retains previous valid values after failed sync
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_b_previous_config_retained_on_bad_config_json(self, db):
        """(b) Existing DB profile is untouched when new Config JSON is malformed."""
        # Seed a good profile
        await db.create_profile(
            AgentProfile(
                id="bad-config",
                name="Original Good Agent",
                model="original-model",
                permission_mode="plan",
                allowed_tools=["Read", "Write"],
                mcp_servers={"github": {"command": "npx"}},
                system_prompt_suffix="Original prompt",
            )
        )

        # Attempt sync with malformed Config JSON
        parsed = parse_profile(MALFORMED_CONFIG_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert not result.success

        # ALL previous fields must be retained — no partial update
        profile = await db.get_profile("bad-config")
        assert profile.name == "Original Good Agent"
        assert profile.model == "original-model"
        assert profile.permission_mode == "plan"
        assert profile.allowed_tools == ["Read", "Write"]
        assert profile.mcp_servers == {"github": {"command": "npx"}}
        assert profile.system_prompt_suffix == "Original prompt"

    @pytest.mark.asyncio
    async def test_b_previous_config_retained_on_bad_mcp_json(self, db):
        """(b) Existing DB profile is untouched when MCP Servers JSON is malformed."""
        await db.create_profile(
            AgentProfile(
                id="bad-mcp",
                name="Original MCP Agent",
                model="good-model",
            )
        )

        parsed = parse_profile(MALFORMED_MCP_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert not result.success

        profile = await db.get_profile("bad-mcp")
        assert profile.name == "Original MCP Agent"
        assert profile.model == "good-model"

    @pytest.mark.asyncio
    async def test_b_retained_and_notified_via_watcher(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(b) Via watcher pipeline: DB retained AND notification sent on failure."""
        # Seed good profile
        await db.create_profile(
            AgentProfile(id="bad-config", name="Stable Agent", model="stable-model")
        )

        profile_path = tmp_path / "agent-types" / "bad-config" / "profile.md"
        _create_file(str(profile_path), MALFORMED_CONFIG_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/bad-config/profile.md",
            operation="modified",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        # DB preserved
        profile = await db.get_profile("bad-config")
        assert profile.name == "Stable Agent"
        assert profile.model == "stable-model"

        # Notification sent
        assert len(event_collector) == 1
        assert event_collector[0]["event_type"] == "notify.profile_sync_failed"

    @pytest.mark.asyncio
    async def test_b_no_partial_update_on_garbled_body(self, db):
        """(b) Garbled body with multiple bad sections doesn't partially update DB."""
        await db.create_profile(
            AgentProfile(
                id="garbled",
                name="Clean Agent",
                model="clean-model",
                allowed_tools=["Bash"],
            )
        )

        parsed = parse_profile(GARBLED_BODY_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert not result.success

        # All original fields intact
        profile = await db.get_profile("garbled")
        assert profile.name == "Clean Agent"
        assert profile.model == "clean-model"
        assert profile.allowed_tools == ["Bash"]

    # -------------------------------------------------------------------
    # (c) Invalid tool name produces warning, does not block sync
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_c_invalid_tool_name_warning_sync_succeeds(self, db):
        """(c) Invalid tool names in Tools produce warnings but sync still succeeds."""
        text = """\
---
id: warn-tools
name: Warning Tools Agent
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```

## Tools
```json
{
  "allowed": ["Read", "Write", "totally_fake_tool", "another_bogus_tool"]
}
```

## MCP Servers
```json
{"myserver": {"command": "my-server-cmd"}}
```

## Role
You are a helpful agent.

## Rules
- Follow best practices
"""
        parsed = parse_profile(text)
        assert parsed.is_valid, f"Should be valid despite unknown tools: {parsed.errors}"

        result = await sync_profile_to_db(parsed, db)
        assert result.success
        assert result.warnings
        assert any("totally_fake_tool" in w for w in result.warnings)
        assert any("another_bogus_tool" in w for w in result.warnings)

        # Other sections synced correctly despite tool warnings
        profile = await db.get_profile("warn-tools")
        assert profile is not None
        assert profile.model == "claude-sonnet-4-6"
        assert profile.mcp_servers["myserver"]["command"] == "my-server-cmd"
        assert "helpful agent" in profile.system_prompt_suffix
        assert "- Follow best practices" in profile.system_prompt_suffix
        # Tools still stored (unknown names are allowed, just warned)
        assert "totally_fake_tool" in profile.allowed_tools
        assert "another_bogus_tool" in profile.allowed_tools
        assert "Read" in profile.allowed_tools
        assert "Write" in profile.allowed_tools

    @pytest.mark.asyncio
    async def test_c_tool_warning_does_not_emit_failure_notification(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(c) Tool name warnings do NOT trigger notify.profile_sync_failed."""
        text = """\
---
id: warn-tools-notify
name: Warning Tools Notify
---

## Tools
```json
{"allowed": ["Read", "nonexistent_tool_xyz"]}
```
"""
        profile_path = tmp_path / "agent-types" / "warn-tools-notify" / "profile.md"
        _create_file(str(profile_path), text)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/warn-tools-notify/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        # Sync succeeded — no failure notification
        assert len(event_collector) == 0

        # But profile was created with warnings
        profile = await db.get_profile("warn-tools-notify")
        assert profile is not None
        assert "nonexistent_tool_xyz" in profile.allowed_tools

    def test_c_parser_level_tool_warnings_with_known_set(self):
        """(c) Parser-level tool validation with known_tools set produces warnings."""
        text = """\
---
id: parse-warn
name: Parse Warn
---

## Tools
```json
{"allowed": ["Read", "Write", "ghost_tool"], "denied": ["phantom_tool"]}
```
"""
        known = {"Read", "Write", "Edit", "Bash"}
        result = parse_profile(text, known_tools=known)
        assert result.is_valid  # Warnings, not errors
        assert any("ghost_tool" in w for w in result.warnings)
        assert any("phantom_tool" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_c_all_unknown_tools_still_sync(self, db):
        """(c) Even when ALL tool names are unknown, sync succeeds with warnings."""
        text = """\
---
id: all-unknown-tools
name: All Unknown
---

## Tools
```json
{"allowed": ["fake1", "fake2", "fake3"]}
```
"""
        result = await sync_profile_text_to_db(text, db)
        assert result.success
        assert result.warnings
        # All three fake tools produce warnings
        tool_warnings = [w for w in result.warnings if "Unrecognized" in w or "tool" in w.lower()]
        assert len(tool_warnings) >= 1

        profile = await db.get_profile("all-unknown-tools")
        assert profile.allowed_tools == ["fake1", "fake2", "fake3"]

    # -------------------------------------------------------------------
    # (d) Malformed JSON in MCP Servers triggers failure notification
    # -------------------------------------------------------------------

    def test_d_malformed_mcp_json_produces_parse_error(self):
        """(d) Malformed JSON in ## MCP Servers produces a parse error."""
        parsed = parse_profile(MALFORMED_MCP_PROFILE)
        assert not parsed.is_valid
        assert any("Invalid JSON" in e and "MCP" in e for e in parsed.errors)

    @pytest.mark.asyncio
    async def test_d_malformed_mcp_json_fails_sync(self, db):
        """(d) Malformed JSON in ## MCP Servers causes sync failure."""
        parsed = parse_profile(MALFORMED_MCP_PROFILE)
        result = await sync_profile_to_db(parsed, db)

        assert not result.success
        assert result.action == "none"
        assert await db.get_profile("bad-mcp") is None

    @pytest.mark.asyncio
    async def test_d_malformed_mcp_json_emits_notification(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(d) Malformed JSON in ## MCP Servers triggers notify.profile_sync_failed."""
        profile_path = tmp_path / "agent-types" / "bad-mcp" / "profile.md"
        _create_file(str(profile_path), MALFORMED_MCP_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/bad-mcp/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        event = event_collector[0]
        assert event["event_type"] == "notify.profile_sync_failed"
        assert event["severity"] == "error"
        assert any("Invalid JSON" in e for e in event["errors"])

    @pytest.mark.asyncio
    async def test_d_valid_config_but_bad_mcp_still_fails(self, db):
        """(d) Profile with valid Config but malformed MCP Servers fails entirely."""
        parsed = parse_profile(MALFORMED_MCP_PROFILE)
        # Config parses fine but MCP Servers block is broken
        assert not parsed.is_valid

        result = await sync_profile_to_db(parsed, db)
        assert not result.success

        # Neither config nor anything else is synced — atomic failure
        assert await db.get_profile("bad-mcp") is None

    # -------------------------------------------------------------------
    # (e) Completely empty profile.md does not crash parser
    # -------------------------------------------------------------------

    def test_e_empty_string_does_not_crash(self):
        """(e) Empty string input returns empty/default ParsedProfile."""
        result = parse_profile("")
        assert result.frontmatter.id == ""
        assert result.frontmatter.name == ""
        assert result.config == {}
        assert result.tools == {}
        assert result.mcp_servers == {}
        assert result.role == ""
        assert result.rules == ""
        assert result.reflection == ""
        assert result.errors == []
        assert result.warnings == []
        # is_valid is True because there are no parse errors (just empty)
        assert result.is_valid

    def test_e_whitespace_only_does_not_crash(self):
        """(e) Whitespace-only input returns empty/default ParsedProfile."""
        for ws in ["   ", "\n\n\n", "\t\t", "  \n  \t  \n  "]:
            result = parse_profile(ws)
            assert result.frontmatter.id == ""
            assert result.config == {}
            assert result.is_valid

    def test_e_newlines_only_does_not_crash(self):
        """(e) Newline-only input returns empty/default ParsedProfile."""
        result = parse_profile("\n\n\n\n")
        assert result.is_valid
        assert result.frontmatter.id == ""

    @pytest.mark.asyncio
    async def test_e_empty_profile_sync_fails_gracefully(self, db):
        """(e) Empty profile syncs fail (no ID) but don't crash."""
        result = await sync_profile_text_to_db("", db)
        assert not result.success
        assert any("ID is required" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_e_empty_profile_with_fallback_id(self, db):
        """(e) Empty profile with fallback_id syncs successfully (empty defaults)."""
        result = await sync_profile_text_to_db("", db, fallback_id="empty-agent")
        # Empty text produces an empty ParsedProfile with no errors
        # so sync succeeds with the fallback ID
        assert result.success
        assert result.profile_id == "empty-agent"

        profile = await db.get_profile("empty-agent")
        assert profile is not None
        assert profile.model == ""
        assert profile.allowed_tools == []
        assert profile.mcp_servers == {}

    def test_e_frontmatter_delimiters_only(self):
        """(e) Just frontmatter delimiters with nothing inside doesn't crash."""
        result = parse_profile("---\n---\n")
        assert result.is_valid
        assert result.frontmatter.id == ""

    # -------------------------------------------------------------------
    # (f) Profile.md with valid frontmatter but garbled body sections
    # -------------------------------------------------------------------

    def test_f_garbled_all_sections_fails_gracefully(self):
        """(f) Profile with valid frontmatter but all garbled JSON sections fails gracefully."""
        parsed = parse_profile(GARBLED_BODY_PROFILE)
        assert not parsed.is_valid
        # Multiple errors — one per garbled JSON section
        assert len(parsed.errors) >= 2
        # Each garbled section produces an "Invalid JSON" error
        config_errors = [e for e in parsed.errors if "Config" in e]
        assert len(config_errors) >= 1

    def test_f_garbled_body_preserves_frontmatter(self):
        """(f) Frontmatter is still parsed correctly even when body is garbled."""
        parsed = parse_profile(GARBLED_BODY_PROFILE)
        assert parsed.frontmatter.id == "garbled"
        assert parsed.frontmatter.name == "Garbled Body Agent"

    def test_f_mixed_valid_and_garbled_sections(self):
        """(f) Profile with mix of valid and garbled sections: valid sections parse,
        garbled ones produce errors."""
        parsed = parse_profile(VALID_FRONTMATTER_GARBLED_BODY_MIXED)
        # Config is garbled → parse error
        assert not parsed.is_valid
        assert any("Config" in e for e in parsed.errors)

        # But valid sections are still parsed:
        # Role (English section) should be captured
        assert "perfectly fine English text" in parsed.role
        # Rules (English section) should be captured
        assert "These rules are valid markdown" in parsed.rules
        # Tools (valid JSON) should be parsed
        assert parsed.tools.get("allowed") == ["Read", "Write"]

    @pytest.mark.asyncio
    async def test_f_garbled_body_does_not_sync(self, db):
        """(f) Profile with garbled body sections does not sync to DB."""
        parsed = parse_profile(GARBLED_BODY_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert not result.success
        assert await db.get_profile("garbled") is None

    @pytest.mark.asyncio
    async def test_f_garbled_body_emits_notification(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(f) Profile with garbled body triggers notification via watcher."""
        profile_path = tmp_path / "agent-types" / "garbled" / "profile.md"
        _create_file(str(profile_path), GARBLED_BODY_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/garbled/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        assert event_collector[0]["event_type"] == "notify.profile_sync_failed"
        assert len(event_collector[0]["errors"]) >= 1

    def test_f_no_exception_raised_on_garbled_input(self):
        """(f) Parser never raises an exception, even on severely garbled input."""
        garbled_inputs = [
            "---\nid: x\n---\n## Config\n```json\n\x00\x01\x02\n```\n",
            "---\nid: x\n---\n## MCP Servers\n```json\n{{{{{{\n```\n",
            "---\nid: x\n---\n" + "## Config\n" * 100,
            "---\nid: x\n---\n## Tools\n```json\n" + "}" * 1000 + "\n```\n",
        ]
        for text in garbled_inputs:
            # Should never raise — just populate errors
            result = parse_profile(text)
            assert isinstance(result, ParsedProfile)
            assert result.frontmatter.id == "x"

    # -------------------------------------------------------------------
    # (g) Error notification includes file path and specific parse error
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_g_notification_includes_source_path(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(g) Error notification includes the source file path."""
        profile_path = tmp_path / "agent-types" / "bad-config" / "profile.md"
        _create_file(str(profile_path), MALFORMED_CONFIG_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/bad-config/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        event = event_collector[0]
        assert event["source_path"] == "agent-types/bad-config/profile.md"

    @pytest.mark.asyncio
    async def test_g_notification_includes_specific_parse_error(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(g) Error notification contains the specific JSON parse error message."""
        profile_path = tmp_path / "agent-types" / "bad-config" / "profile.md"
        _create_file(str(profile_path), MALFORMED_CONFIG_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/bad-config/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        event = event_collector[0]
        errors = event["errors"]
        assert len(errors) >= 1
        # Error should mention the section and include JSON parse details
        error_text = errors[0]
        assert "Invalid JSON" in error_text
        assert "Config" in error_text
        # Should include line/column info from json.JSONDecodeError
        assert "line" in error_text.lower()

    @pytest.mark.asyncio
    async def test_g_notification_includes_mcp_parse_error_details(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(g) MCP Servers parse error notification includes section-specific details."""
        profile_path = tmp_path / "agent-types" / "bad-mcp" / "profile.md"
        _create_file(str(profile_path), MALFORMED_MCP_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/bad-mcp/profile.md",
            operation="modified",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        event = event_collector[0]
        errors = event["errors"]
        assert len(errors) >= 1
        # Should reference MCP Servers section specifically
        assert any("MCP" in e for e in errors)

    @pytest.mark.asyncio
    async def test_g_notification_from_garbled_body_has_multiple_errors(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(g) Garbled body with multiple bad sections includes all errors in notification."""
        profile_path = tmp_path / "agent-types" / "garbled" / "profile.md"
        _create_file(str(profile_path), GARBLED_BODY_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/garbled/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        event = event_collector[0]
        errors = event["errors"]
        # Multiple garbled sections should produce multiple error entries
        assert len(errors) >= 2
        # Should have errors for Config, Tools, and/or MCP Servers
        error_text = " ".join(errors)
        assert "Config" in error_text or "Tools" in error_text or "MCP" in error_text

    @pytest.mark.asyncio
    async def test_g_notification_event_fields_are_complete(
        self, db, tmp_path, event_bus, event_collector
    ):
        """(g) Notification event has all expected fields for debugging."""
        profile_path = tmp_path / "agent-types" / "bad-config" / "profile.md"
        _create_file(str(profile_path), MALFORMED_CONFIG_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/bad-config/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, event_bus=event_bus)

        assert len(event_collector) == 1
        event = event_collector[0]
        # All required notification fields present
        assert event["event_type"] == "notify.profile_sync_failed"
        assert event["severity"] == "error"
        assert event["category"] == "system"
        assert isinstance(event["source_path"], str)
        assert len(event["source_path"]) > 0
        assert isinstance(event["errors"], list)
        assert len(event["errors"]) > 0
        assert isinstance(event["warnings"], list)
        # profile_id may be empty if ID couldn't be resolved, but should exist
        assert "profile_id" in event


# ---------------------------------------------------------------------------
# Startup scan — scan_and_sync_existing_profiles
# ---------------------------------------------------------------------------
#
# Roadmap 4.1.7 — Ensures existing vault profile files are synced to the
# database at startup, before the VaultWatcher begins change detection.
# ---------------------------------------------------------------------------


class TestFindProfileFiles:
    """Test _find_profile_files vault directory scanning."""

    def test_finds_agent_type_profiles(self, tmp_path):
        """Discovers profile.md files under agent-types/<type>/."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "agent-types" / "coding" / "profile.md"), MINIMAL_PROFILE)
        _create_file(str(vault / "agent-types" / "review" / "profile.md"), MINIMAL_PROFILE)

        found = _find_profile_files(str(vault))
        rel_paths = {r for _, r in found}
        assert "agent-types/coding/profile.md" in rel_paths
        assert "agent-types/review/profile.md" in rel_paths

    def test_finds_orchestrator_profile(self, tmp_path):
        """Discovers orchestrator/profile.md."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "orchestrator" / "profile.md"), MINIMAL_PROFILE)

        found = _find_profile_files(str(vault))
        rel_paths = {r for _, r in found}
        assert "orchestrator/profile.md" in rel_paths

    def test_ignores_non_matching_files(self, tmp_path):
        """Ignores files that don't match any profile pattern."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "projects" / "app" / "profile.md"), "not a profile pattern")
        _create_file(str(vault / "system" / "facts.md"), "facts")
        _create_file(str(vault / "random.md"), "random")

        found = _find_profile_files(str(vault))
        assert len(found) == 0

    def test_returns_empty_for_nonexistent_vault(self, tmp_path):
        """Returns empty list if vault directory doesn't exist."""
        found = _find_profile_files(str(tmp_path / "nonexistent"))
        assert found == []

    def test_returns_empty_for_empty_vault(self, tmp_path):
        """Returns empty list if vault has no profile files."""
        vault = tmp_path / "vault"
        vault.mkdir()
        found = _find_profile_files(str(vault))
        assert found == []

    def test_skips_hidden_directories(self, tmp_path):
        """Skips files inside hidden directories (.obsidian, .git, etc.)."""
        vault = tmp_path / "vault"
        _create_file(
            str(vault / ".obsidian" / "agent-types" / "coding" / "profile.md"),
            MINIMAL_PROFILE,
        )
        _create_file(
            str(vault / "agent-types" / ".hidden" / "profile.md"),
            MINIMAL_PROFILE,
        )

        found = _find_profile_files(str(vault))
        assert len(found) == 0

    def test_skips_hidden_files(self, tmp_path):
        """Skips hidden files (starting with '.')."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "agent-types" / "coding" / ".profile.md"), MINIMAL_PROFILE)

        found = _find_profile_files(str(vault))
        assert len(found) == 0

    def test_returns_absolute_and_relative_paths(self, tmp_path):
        """Each result is a (absolute_path, relative_path) tuple."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "agent-types" / "coding" / "profile.md"), MINIMAL_PROFILE)

        found = _find_profile_files(str(vault))
        assert len(found) == 1
        abs_path, rel_path = found[0]
        assert os.path.isabs(abs_path)
        assert rel_path == "agent-types/coding/profile.md"
        assert abs_path.endswith(os.path.join("agent-types", "coding", "profile.md"))

    def test_no_duplicate_entries(self, tmp_path):
        """Each file appears exactly once even if it matches multiple patterns."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "agent-types" / "coding" / "profile.md"), MINIMAL_PROFILE)
        _create_file(str(vault / "orchestrator" / "profile.md"), MINIMAL_PROFILE)

        found = _find_profile_files(str(vault))
        abs_paths = [a for a, _ in found]
        assert len(abs_paths) == len(set(abs_paths))


class TestScanAndSyncExistingProfiles:
    """Test startup scan: sync existing vault profile files to the database."""

    @pytest.mark.asyncio
    async def test_syncs_single_profile(self, db, tmp_path):
        """A single existing profile file is synced to DB on startup scan."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "agent-types" / "coding" / "profile.md"), FULL_PROFILE)

        results = await scan_and_sync_existing_profiles(str(vault), db)

        assert len(results) == 1
        assert results[0].success
        assert results[0].profile_id == "coding"

        profile = await db.get_profile("coding")
        assert profile is not None
        assert profile.name == "Coding Agent"
        assert profile.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_syncs_multiple_profiles(self, db, tmp_path):
        """Multiple existing profile files are all synced."""
        vault = tmp_path / "vault"
        for agent_type in ["coding", "review", "qa"]:
            text = f"""\
---
id: {agent_type}
name: {agent_type.title()} Agent
---

## Config
```json
{{"model": "claude-sonnet-4-6"}}
```
"""
            _create_file(str(vault / "agent-types" / agent_type / "profile.md"), text)

        results = await scan_and_sync_existing_profiles(str(vault), db)

        assert len(results) == 3
        assert all(r.success for r in results)

        for agent_type in ["coding", "review", "qa"]:
            profile = await db.get_profile(agent_type)
            assert profile is not None, f"Profile {agent_type} not found"

    @pytest.mark.asyncio
    async def test_syncs_orchestrator_profile(self, db, tmp_path):
        """orchestrator/profile.md is synced on startup scan."""
        vault = tmp_path / "vault"
        text = """\
---
id: orchestrator
name: Orchestrator
---

## Role
You coordinate agents.
"""
        _create_file(str(vault / "orchestrator" / "profile.md"), text)

        results = await scan_and_sync_existing_profiles(str(vault), db)

        assert len(results) == 1
        assert results[0].success
        assert results[0].profile_id == "orchestrator"

        profile = await db.get_profile("orchestrator")
        assert profile is not None
        assert profile.name == "Orchestrator"

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_profiles(self, db, tmp_path):
        """Empty result when no profile files exist."""
        vault = tmp_path / "vault"
        vault.mkdir()

        results = await scan_and_sync_existing_profiles(str(vault), db)
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_nonexistent_vault(self, db, tmp_path):
        """Empty result when vault directory doesn't exist."""
        results = await scan_and_sync_existing_profiles(str(tmp_path / "nonexistent"), db)
        assert results == []

    @pytest.mark.asyncio
    async def test_invalid_profile_reported_as_failure(self, db, tmp_path):
        """Invalid profile produces a failure result but doesn't crash."""
        vault = tmp_path / "vault"
        _create_file(
            str(vault / "agent-types" / "broken" / "profile.md"),
            INVALID_JSON_PROFILE,
        )

        results = await scan_and_sync_existing_profiles(str(vault), db)

        assert len(results) == 1
        assert not results[0].success
        assert results[0].errors

        # Invalid profile NOT in DB
        assert await db.get_profile("broken") is None

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid(self, db, tmp_path):
        """Valid profiles sync successfully; invalid ones fail gracefully."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "agent-types" / "good" / "profile.md"), FULL_PROFILE)
        _create_file(
            str(vault / "agent-types" / "bad" / "profile.md"),
            INVALID_JSON_PROFILE,
        )

        results = await scan_and_sync_existing_profiles(str(vault), db)

        assert len(results) == 2
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 1
        assert len(failures) == 1

        # Good profile is in DB
        assert await db.get_profile("coding") is not None
        # Bad profile is NOT in DB
        assert await db.get_profile("broken") is None

    @pytest.mark.asyncio
    async def test_upsert_semantics(self, db, tmp_path):
        """Startup scan upserts — existing DB row is updated, not duplicated."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "agent-types" / "coding" / "profile.md"), FULL_PROFILE)

        # First scan: creates
        r1 = await scan_and_sync_existing_profiles(str(vault), db)
        assert len(r1) == 1
        assert r1[0].action == "created"

        # Second scan (simulate restart): updates
        r2 = await scan_and_sync_existing_profiles(str(vault), db)
        assert len(r2) == 1
        assert r2[0].action == "updated"

        # Still only one row
        profiles = await db.list_profiles()
        matching = [p for p in profiles if p.id == "coding"]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_fallback_id_from_path(self, db, tmp_path):
        """Profile without frontmatter ID gets ID derived from vault path."""
        vault = tmp_path / "vault"
        text = """\
---
name: No ID Agent
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""
        _create_file(str(vault / "agent-types" / "derived" / "profile.md"), text)

        results = await scan_and_sync_existing_profiles(str(vault), db)

        assert len(results) == 1
        assert results[0].success
        assert results[0].profile_id == "derived"

        profile = await db.get_profile("derived")
        assert profile is not None

    @pytest.mark.asyncio
    async def test_emits_notification_on_failure(self, db, tmp_path):
        """Failed sync during startup scan emits notification if event_bus provided."""
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=True)
        events: list[dict] = []
        bus.subscribe("notify.profile_sync_failed", lambda data: events.append(data))

        vault = tmp_path / "vault"
        _create_file(
            str(vault / "agent-types" / "bad" / "profile.md"),
            INVALID_JSON_PROFILE,
        )

        await scan_and_sync_existing_profiles(str(vault), db, event_bus=bus)

        assert len(events) == 1
        assert events[0]["event_type"] == "notify.profile_sync_failed"

    @pytest.mark.asyncio
    async def test_full_field_round_trip(self, db, tmp_path):
        """All parsed fields survive the startup scan pipeline."""
        vault = tmp_path / "vault"
        _create_file(str(vault / "agent-types" / "coding" / "profile.md"), FULL_PROFILE)

        results = await scan_and_sync_existing_profiles(str(vault), db)
        assert len(results) == 1
        assert results[0].success

        profile = await db.get_profile("coding")
        assert profile.name == "Coding Agent"
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert "Read" in profile.allowed_tools
        assert "github" in profile.mcp_servers
        assert "software engineering agent" in profile.system_prompt_suffix
        assert "Always run existing tests" in profile.system_prompt_suffix


# ---------------------------------------------------------------------------
# Roadmap 4.1.12 — File watcher profile sync integration tests
# ---------------------------------------------------------------------------
#
# Tests (a)-(f) per docs/specs/design/roadmap.md §4.1.12:
#   (a) Edit profile.md Role section — DB updates with new Role text
#       within one watcher cycle
#   (b) Edit profile.md Config JSON (change model) — DB reflects new
#       model value
#   (c) Rapid edits to profile.md (3 edits in 500ms) trigger only one
#       sync due to debounce
#   (d) Creating a new profile.md in vault triggers initial sync and DB
#       row creation
#   (e) Deleting profile.md does NOT delete DB row (preserves last known
#       config with warning)
#   (f) Concurrent edits to two different agents' profile.md files sync
#       independently and correctly
# ---------------------------------------------------------------------------


class TestRoadmap4112:
    """Roadmap 4.1.12 — File watcher profile sync integration tests.

    End-to-end tests that exercise the full pipeline: write/edit file on disk →
    VaultWatcher detects change → handler dispatched → profile parsed → DB
    upserted.  Uses real VaultWatcher, real parser, and real database.
    """

    # -------------------------------------------------------------------
    # (a) Edit profile.md Role section — DB updates with new Role text
    #     within one watcher cycle
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_a_edit_role_section_updates_db(self, db, tmp_path):
        """(a) Editing the Role section in profile.md updates DB within one watcher cycle."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "coding" / "profile.md"

        initial_text = """\
---
id: coding
name: Coding Agent
---

## Role
You are a backend engineer.

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""
        _create_file(str(profile_path), initial_text)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)

        # Initial snapshot + creation sync
        await watcher.check()  # snapshot
        _touch(str(profile_path))
        await watcher.check()  # detect + dispatch creation

        profile = await db.get_profile("coding")
        assert profile is not None
        assert "backend engineer" in profile.system_prompt_suffix

        # Now edit only the Role section
        updated_text = """\
---
id: coding
name: Coding Agent
---

## Role
You are a **full-stack engineer** specializing in React and Python.
You build end-to-end features with thorough test coverage.

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""
        with open(str(profile_path), "w") as f:
            f.write(updated_text)
        _touch(str(profile_path))

        # Single watcher cycle should detect and sync
        changes = await watcher.check()
        assert len(changes) >= 1

        # Verify DB has the new Role text
        profile = await db.get_profile("coding")
        assert profile is not None
        assert "full-stack engineer" in profile.system_prompt_suffix
        assert "React and Python" in profile.system_prompt_suffix
        assert "backend engineer" not in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_a_role_edit_preserves_other_fields(self, db, tmp_path):
        """(a) Editing Role preserves model, tools, and other DB fields."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "test" / "profile.md"

        initial_text = """\
---
id: test
name: Test Agent
---

## Role
Original role text.

## Config
```json
{"model": "claude-sonnet-4-6", "permission_mode": "auto"}
```

## Tools
```json
{"allowed": ["Read", "Write", "Edit"]}
```
"""
        _create_file(str(profile_path), initial_text)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()
        _touch(str(profile_path))
        await watcher.check()

        # Now edit only the Role
        updated_text = initial_text.replace("Original role text.", "New role text here.")
        with open(str(profile_path), "w") as f:
            f.write(updated_text)
        _touch(str(profile_path))
        await watcher.check()

        profile = await db.get_profile("test")
        assert "New role text" in profile.system_prompt_suffix
        # Other fields preserved
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert profile.allowed_tools == ["Read", "Write", "Edit"]

    # -------------------------------------------------------------------
    # (b) Edit profile.md Config JSON (change model) — DB reflects new
    #     model value
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_b_edit_config_model_updates_db(self, db, tmp_path):
        """(b) Changing the model in Config JSON updates the DB model field."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "coding" / "profile.md"

        initial_text = """\
---
id: coding
name: Coding Agent
---

## Role
You are a software engineer.

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""
        _create_file(str(profile_path), initial_text)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()
        _touch(str(profile_path))
        await watcher.check()

        # Verify initial model
        profile = await db.get_profile("coding")
        assert profile is not None
        assert profile.model == "claude-sonnet-4-6"

        # Change model to opus
        updated_text = initial_text.replace("claude-sonnet-4-6", "claude-opus-4")
        with open(str(profile_path), "w") as f:
            f.write(updated_text)
        _touch(str(profile_path))

        changes = await watcher.check()
        assert len(changes) >= 1

        profile = await db.get_profile("coding")
        assert profile.model == "claude-opus-4"

    @pytest.mark.asyncio
    async def test_b_edit_config_permission_mode_updates_db(self, db, tmp_path):
        """(b) Changing permission_mode in Config JSON updates the DB."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "test" / "profile.md"

        initial_text = """\
---
id: test
name: Test Agent
---

## Config
```json
{"model": "claude-sonnet-4-6", "permission_mode": "plan"}
```
"""
        _create_file(str(profile_path), initial_text)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()
        _touch(str(profile_path))
        await watcher.check()

        assert (await db.get_profile("test")).permission_mode == "plan"

        # Change permission_mode
        updated_text = initial_text.replace('"plan"', '"auto"')
        with open(str(profile_path), "w") as f:
            f.write(updated_text)
        _touch(str(profile_path))
        await watcher.check()

        assert (await db.get_profile("test")).permission_mode == "auto"

    @pytest.mark.asyncio
    async def test_b_edit_config_preserves_role_text(self, db, tmp_path):
        """(b) Changing Config preserves the existing Role section in DB."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "test" / "profile.md"

        initial_text = """\
---
id: test
name: Test Agent
---

## Role
Specialist in database optimization.

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""
        _create_file(str(profile_path), initial_text)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()
        _touch(str(profile_path))
        await watcher.check()

        # Change model
        updated_text = initial_text.replace("claude-sonnet-4-6", "claude-opus-4")
        with open(str(profile_path), "w") as f:
            f.write(updated_text)
        _touch(str(profile_path))
        await watcher.check()

        profile = await db.get_profile("test")
        assert profile.model == "claude-opus-4"
        assert "database optimization" in profile.system_prompt_suffix

    # -------------------------------------------------------------------
    # (c) Rapid edits to profile.md (3 edits in 500ms) trigger only one
    #     sync due to debounce
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_c_rapid_edits_trigger_single_sync(self, db, tmp_path):
        """(c) Three rapid edits within the debounce window trigger only one sync.

        Uses a long debounce_seconds to hold changes, then force-flushes.
        Only the final edit's content should be reflected in the DB.
        """
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "rapid" / "profile.md"

        _create_file(
            str(profile_path),
            """\
---
id: rapid
name: Rapid Agent
---

## Config
```json
{"model": "model-v1"}
```
""",
        )

        # Long debounce — changes won't dispatch until force-flushed
        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=999)
        register_profile_handlers(watcher, db=db)

        # Initial snapshot
        watcher._snapshot()
        watcher._initialized = True

        # Edit 1: create detected
        _touch(str(profile_path))
        await watcher.check()

        # Edit 2: change model to v2
        with open(str(profile_path), "w") as f:
            f.write("""\
---
id: rapid
name: Rapid Agent
---

## Config
```json
{"model": "model-v2"}
```
""")
        _touch(str(profile_path))
        await watcher.check()

        # Edit 3: change model to v3 (the final value)
        with open(str(profile_path), "w") as f:
            f.write("""\
---
id: rapid
name: Rapid Agent
---

## Config
```json
{"model": "model-v3"}
```
""")
        _touch(str(profile_path))
        await watcher.check()

        # Nothing dispatched yet — still within debounce window
        assert await db.get_profile("rapid") is None
        assert watcher.get_pending_change_count() > 0

        # Force flush — should dispatch exactly once
        await watcher._flush_pending(force=True)

        # DB should reflect the final state (model-v3)
        profile = await db.get_profile("rapid")
        assert profile is not None
        assert profile.model == "model-v3"

    @pytest.mark.asyncio
    async def test_c_rapid_edits_deduplicated_to_single_change(self, db, tmp_path):
        """(c) Three rapid edits are deduplicated to a single VaultChange."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "dedup" / "profile.md"

        _create_file(str(profile_path), MINIMAL_PROFILE)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=999)
        collector = ChangeCollector()
        watcher.register_handler("agent-types/*/profile.md", collector)

        watcher._snapshot()
        watcher._initialized = True

        # Three rapid edits
        for i in range(3):
            with open(str(profile_path), "w") as f:
                f.write(MINIMAL_PROFILE.replace("Test Agent", f"Agent v{i + 1}"))
            _touch(str(profile_path))
            await watcher.check()

        # Not dispatched yet
        assert collector.call_count == 0

        # Force flush
        await watcher._flush_pending(force=True)

        # Handler called exactly once with one deduplicated change
        assert collector.call_count == 1
        assert len(collector.all_changes) == 1
        # File existed at snapshot time, so all three edits are "modified" →
        # deduplicated to a single "modified"
        assert collector.all_changes[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_c_debounce_zero_dispatches_immediately(self, db, tmp_path):
        """(c) With debounce_seconds=0, edits are dispatched on the same check cycle."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "instant" / "profile.md"

        _create_file(
            str(profile_path),
            """\
---
id: instant
name: Instant Agent
---

## Config
```json
{"model": "fast-model"}
```
""",
        )

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()  # snapshot

        # Edit
        with open(str(profile_path), "w") as f:
            f.write("""\
---
id: instant
name: Instant Agent
---

## Config
```json
{"model": "updated-model"}
```
""")
        _touch(str(profile_path))
        await watcher.check()

        # Should be dispatched immediately
        profile = await db.get_profile("instant")
        assert profile is not None
        assert profile.model == "updated-model"

    # -------------------------------------------------------------------
    # (d) Creating a new profile.md in vault triggers initial sync and
    #     DB row creation
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_d_new_profile_triggers_db_creation(self, db, tmp_path):
        """(d) Creating a new profile.md triggers DB row creation."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()  # initial snapshot (empty vault)

        # Verify no profile exists
        assert await db.get_profile("new-agent") is None

        # Create new profile file
        _create_file(
            str(vault / "agent-types" / "new-agent" / "profile.md"),
            """\
---
id: new-agent
name: New Agent
---

## Role
A brand new agent for testing.

## Config
```json
{"model": "claude-sonnet-4-6", "permission_mode": "auto"}
```

## Tools
```json
{"allowed": ["Read", "Write"]}
```
""",
        )

        changes = await watcher.check()
        assert len(changes) == 1
        assert changes[0].operation == "created"

        # DB row should now exist with all fields
        profile = await db.get_profile("new-agent")
        assert profile is not None
        assert profile.name == "New Agent"
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert profile.allowed_tools == ["Read", "Write"]
        assert "brand new agent" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_d_new_profile_uses_path_derived_id(self, db, tmp_path):
        """(d) New profile without frontmatter id uses path-derived ID."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()

        # Profile without explicit id in frontmatter
        _create_file(
            str(vault / "agent-types" / "inferred" / "profile.md"),
            """\
---
name: Inferred Agent
---

## Role
ID will be derived from path.
""",
        )

        await watcher.check()

        # ID derived from path: agent-types/inferred/profile.md → "inferred"
        profile = await db.get_profile("inferred")
        assert profile is not None
        assert profile.name == "Inferred Agent"

    @pytest.mark.asyncio
    async def test_d_new_orchestrator_profile_creation(self, db, tmp_path):
        """(d) Creating orchestrator/profile.md triggers DB row creation."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()

        _create_file(
            str(vault / "orchestrator" / "profile.md"),
            """\
---
id: orchestrator
name: Orchestrator
---

## Role
You coordinate and supervise other agents.
""",
        )

        changes = await watcher.check()
        assert any(c.operation == "created" for c in changes)

        profile = await db.get_profile("orchestrator")
        assert profile is not None
        assert profile.name == "Orchestrator"
        assert "coordinate" in profile.system_prompt_suffix

    # -------------------------------------------------------------------
    # (e) Deleting profile.md does NOT delete DB row (preserves last
    #     known config with warning)
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_e_delete_preserves_db_row(self, db, tmp_path):
        """(e) Deleting profile.md does NOT delete the DB row."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "keeper" / "profile.md"

        _create_file(
            str(profile_path),
            """\
---
id: keeper
name: Keeper Agent
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```

## Role
An agent whose config should persist after file deletion.
""",
        )

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()  # snapshot
        _touch(str(profile_path))
        await watcher.check()  # detect creation, sync to DB

        # Verify profile exists in DB
        profile = await db.get_profile("keeper")
        assert profile is not None
        assert profile.model == "claude-sonnet-4-6"
        assert "persist after file deletion" in profile.system_prompt_suffix

        # Delete the file
        os.remove(str(profile_path))
        changes = await watcher.check()
        assert any(c.operation == "deleted" for c in changes)

        # DB row must STILL exist with last known values (per spec)
        profile = await db.get_profile("keeper")
        assert profile is not None
        assert profile.name == "Keeper Agent"
        assert profile.model == "claude-sonnet-4-6"
        assert "persist after file deletion" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_e_delete_preserves_all_fields(self, db, tmp_path):
        """(e) All DB fields (tools, mcp_servers, etc.) survive file deletion."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "full-del" / "profile.md"

        full_text = """\
---
id: full-del
name: Full Delete Test
---

## Config
```json
{"model": "claude-sonnet-4-6", "permission_mode": "auto"}
```

## Tools
```json
{"allowed": ["Read", "Write", "Edit"]}
```

## MCP Servers
```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"]
  }
}
```

## Role
You handle complex tasks.

## Rules
- Always test first.
"""
        _create_file(str(profile_path), full_text)

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()
        _touch(str(profile_path))
        await watcher.check()

        # Verify full profile in DB
        profile = await db.get_profile("full-del")
        assert profile is not None
        assert profile.model == "claude-sonnet-4-6"
        assert profile.allowed_tools == ["Read", "Write", "Edit"]
        assert "github" in profile.mcp_servers

        # Delete the file
        os.remove(str(profile_path))
        await watcher.check()

        # All fields preserved
        profile = await db.get_profile("full-del")
        assert profile is not None
        assert profile.name == "Full Delete Test"
        assert profile.model == "claude-sonnet-4-6"
        assert profile.permission_mode == "auto"
        assert profile.allowed_tools == ["Read", "Write", "Edit"]
        assert "github" in profile.mcp_servers
        assert "complex tasks" in profile.system_prompt_suffix
        assert "Always test first" in profile.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_e_delete_then_recreate(self, db, tmp_path):
        """(e) After deletion, re-creating profile.md updates DB with new content."""
        vault = tmp_path / "vault"
        profile_path = vault / "agent-types" / "phoenix" / "profile.md"

        _create_file(
            str(profile_path),
            """\
---
id: phoenix
name: Phoenix Agent
---

## Config
```json
{"model": "old-model"}
```
""",
        )

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()
        _touch(str(profile_path))
        await watcher.check()

        assert (await db.get_profile("phoenix")).model == "old-model"

        # Delete
        os.remove(str(profile_path))
        await watcher.check()

        # Still in DB
        assert (await db.get_profile("phoenix")).model == "old-model"

        # Re-create with new content
        _create_file(
            str(profile_path),
            """\
---
id: phoenix
name: Phoenix Agent Reborn
---

## Config
```json
{"model": "new-model"}
```
""",
        )
        await watcher.check()

        profile = await db.get_profile("phoenix")
        assert profile.name == "Phoenix Agent Reborn"
        assert profile.model == "new-model"

    # -------------------------------------------------------------------
    # (f) Concurrent edits to two different agents' profile.md files sync
    #     independently and correctly
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_f_concurrent_edits_two_agents(self, db, tmp_path):
        """(f) Concurrent edits to two agents' profiles sync independently."""
        vault = tmp_path / "vault"
        path_a = vault / "agent-types" / "agent-a" / "profile.md"
        path_b = vault / "agent-types" / "agent-b" / "profile.md"

        _create_file(
            str(path_a),
            """\
---
id: agent-a
name: Agent A
---

## Config
```json
{"model": "model-a-v1"}
```

## Role
Agent A does task A.
""",
        )
        _create_file(
            str(path_b),
            """\
---
id: agent-b
name: Agent B
---

## Config
```json
{"model": "model-b-v1"}
```

## Role
Agent B does task B.
""",
        )

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()  # snapshot with both files

        # Touch both to trigger creation events
        _touch(str(path_a))
        _touch(str(path_b))
        changes = await watcher.check()
        assert len(changes) == 2

        # Both should be in DB
        profile_a = await db.get_profile("agent-a")
        profile_b = await db.get_profile("agent-b")
        assert profile_a is not None
        assert profile_b is not None
        assert profile_a.model == "model-a-v1"
        assert profile_b.model == "model-b-v1"

        # Now edit both concurrently (different changes)
        with open(str(path_a), "w") as f:
            f.write("""\
---
id: agent-a
name: Agent A Updated
---

## Config
```json
{"model": "model-a-v2"}
```

## Role
Agent A now does task A-prime.
""")
        _touch(str(path_a))

        with open(str(path_b), "w") as f:
            f.write("""\
---
id: agent-b
name: Agent B Updated
---

## Config
```json
{"model": "model-b-v2"}
```

## Role
Agent B now does task B-prime.
""")
        _touch(str(path_b))

        changes = await watcher.check()
        assert len(changes) == 2

        # Both should reflect their independent updates
        profile_a = await db.get_profile("agent-a")
        profile_b = await db.get_profile("agent-b")
        assert profile_a.name == "Agent A Updated"
        assert profile_a.model == "model-a-v2"
        assert "A-prime" in profile_a.system_prompt_suffix
        assert profile_b.name == "Agent B Updated"
        assert profile_b.model == "model-b-v2"
        assert "B-prime" in profile_b.system_prompt_suffix

    @pytest.mark.asyncio
    async def test_f_concurrent_edits_no_cross_contamination(self, db, tmp_path):
        """(f) Editing one agent's profile does not affect the other."""
        vault = tmp_path / "vault"
        path_a = vault / "agent-types" / "alpha" / "profile.md"
        path_b = vault / "agent-types" / "beta" / "profile.md"

        _create_file(
            str(path_a),
            """\
---
id: alpha
name: Alpha
---

## Config
```json
{"model": "alpha-model"}
```
""",
        )
        _create_file(
            str(path_b),
            """\
---
id: beta
name: Beta
---

## Config
```json
{"model": "beta-model"}
```
""",
        )

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()
        _touch(str(path_a))
        _touch(str(path_b))
        await watcher.check()

        # Edit only alpha
        with open(str(path_a), "w") as f:
            f.write("""\
---
id: alpha
name: Alpha Changed
---

## Config
```json
{"model": "alpha-model-v2"}
```
""")
        _touch(str(path_a))
        await watcher.check()

        # Alpha changed
        profile_a = await db.get_profile("alpha")
        assert profile_a.name == "Alpha Changed"
        assert profile_a.model == "alpha-model-v2"

        # Beta unchanged
        profile_b = await db.get_profile("beta")
        assert profile_b.name == "Beta"
        assert profile_b.model == "beta-model"

    @pytest.mark.asyncio
    async def test_f_concurrent_with_orchestrator_and_agent(self, db, tmp_path):
        """(f) Agent-type and orchestrator profiles sync independently."""
        vault = tmp_path / "vault"
        agent_path = vault / "agent-types" / "coding" / "profile.md"
        orch_path = vault / "orchestrator" / "profile.md"

        _create_file(
            str(agent_path),
            """\
---
id: coding
name: Coding Agent
---

## Config
```json
{"model": "agent-model"}
```
""",
        )
        _create_file(
            str(orch_path),
            """\
---
id: orchestrator
name: Orchestrator
---

## Config
```json
{"model": "orchestrator-model"}
```
""",
        )

        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=0)
        register_profile_handlers(watcher, db=db)
        await watcher.check()
        _touch(str(agent_path))
        _touch(str(orch_path))
        await watcher.check()

        # Both synced independently
        coding = await db.get_profile("coding")
        orch = await db.get_profile("orchestrator")
        assert coding is not None
        assert orch is not None
        assert coding.model == "agent-model"
        assert orch.model == "orchestrator-model"

        # Edit both simultaneously
        with open(str(agent_path), "w") as f:
            f.write("""\
---
id: coding
name: Coding Agent v2
---

## Config
```json
{"model": "agent-model-v2"}
```
""")
        _touch(str(agent_path))

        with open(str(orch_path), "w") as f:
            f.write("""\
---
id: orchestrator
name: Orchestrator v2
---

## Config
```json
{"model": "orchestrator-model-v2"}
```
""")
        _touch(str(orch_path))

        await watcher.check()

        coding = await db.get_profile("coding")
        orch = await db.get_profile("orchestrator")
        assert coding.name == "Coding Agent v2"
        assert coding.model == "agent-model-v2"
        assert orch.name == "Orchestrator v2"
        assert orch.model == "orchestrator-model-v2"

    @pytest.mark.asyncio
    async def test_f_concurrent_debounced_edits(self, db, tmp_path):
        """(f) Concurrent edits under debounce still sync both correctly."""
        vault = tmp_path / "vault"
        path_a = vault / "agent-types" / "da" / "profile.md"
        path_b = vault / "agent-types" / "db" / "profile.md"

        _create_file(
            str(path_a),
            """\
---
id: da
name: DA
---

## Config
```json
{"model": "da-v1"}
```
""",
        )
        _create_file(
            str(path_b),
            """\
---
id: db
name: DB
---

## Config
```json
{"model": "db-v1"}
```
""",
        )

        # Use large debounce — changes accumulate
        watcher = VaultWatcher(str(vault), poll_interval=0, debounce_seconds=999)
        register_profile_handlers(watcher, db=db)
        watcher._snapshot()
        watcher._initialized = True

        # Edit both files
        _touch(str(path_a))
        await watcher.check()
        _touch(str(path_b))
        await watcher.check()

        # Nothing dispatched yet
        assert await db.get_profile("da") is None
        assert await db.get_profile("db") is None

        # Force flush — both should sync
        await watcher._flush_pending(force=True)

        profile_a = await db.get_profile("da")
        profile_b = await db.get_profile("db")
        assert profile_a is not None
        assert profile_b is not None
        assert profile_a.model == "da-v1"
        assert profile_b.model == "db-v1"


# ---------------------------------------------------------------------------
# Starter knowledge pack provisioning — Roadmap §4.3.5 (a)-(g)
# ---------------------------------------------------------------------------


class TestStarterKnowledgeProvisioning:
    """Integration tests for starter knowledge pack provisioning via watcher.

    Roadmap 4.3.5 — verifies that creating profiles through the vault watcher
    path correctly triggers starter knowledge pack copying.
    """

    @pytest.mark.asyncio
    async def test_watcher_copies_starter_knowledge_on_create(self, db, tmp_path):
        """(a) Creating first profile.md for 'coding' copies knowledge pack to memory dir.

        When on_profile_changed receives a 'created' event for a new agent-type
        profile and data_dir is provided, starter knowledge files should be copied
        into the agent-type's memory directory.
        """
        from src.vault import ensure_default_templates, ensure_vault_profile_dirs

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)
        ensure_vault_profile_dirs(data_dir, "coding")

        # Write profile.md into the vault location
        profile_path = tmp_path / "vault" / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, data_dir=data_dir)

        # Verify profile synced to DB
        profile = await db.get_profile("coding")
        assert profile is not None

        # Verify starter knowledge files were copied
        memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
        assert (memory_dir / "common-pitfalls.md").is_file()
        assert (memory_dir / "git-conventions.md").is_file()

    @pytest.mark.asyncio
    async def test_watcher_copied_files_have_starter_tag(self, db, tmp_path):
        """(b) Copied files are tagged #starter in frontmatter.

        Starter knowledge files copied via the watcher path must retain
        their #starter tag so agents and users can identify them.
        """
        import yaml

        from src.vault import ensure_default_templates, ensure_vault_profile_dirs

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)
        ensure_vault_profile_dirs(data_dir, "coding")

        profile_path = tmp_path / "vault" / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, data_dir=data_dir)

        memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
        for filename in ("common-pitfalls.md", "git-conventions.md"):
            content = (memory_dir / filename).read_text()
            assert content.startswith("---"), f"{filename} missing YAML frontmatter"
            lines = content.strip().splitlines()
            end_idx = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
            fm = yaml.safe_load("\n".join(lines[1:end_idx]))
            assert "starter" in fm.get("tags", []), (
                f"{filename} missing 'starter' tag in frontmatter"
            )

    @pytest.mark.asyncio
    async def test_watcher_does_not_recopy_on_second_create(self, db, tmp_path):
        """(c) Creating second profile.md for same type does NOT copy pack again.

        Starter knowledge is only copied once.  If the memory directory already
        contains the files (from a previous profile creation), they are NOT
        overwritten — preserving any user customizations.
        """
        from src.vault import ensure_default_templates, ensure_vault_profile_dirs

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)
        ensure_vault_profile_dirs(data_dir, "coding")

        profile_path = tmp_path / "vault" / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="created",
        )

        # First creation — copies starter knowledge
        await on_profile_changed([change], db=db, data_dir=data_dir)

        # Customize a starter file (simulates user edits)
        memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
        (memory_dir / "common-pitfalls.md").write_text("# My custom pitfalls\nEdited by user.")

        # Delete DB record and re-create to simulate second profile creation
        await db.delete_profile("coding")
        await on_profile_changed([change], db=db, data_dir=data_dir)

        # User customization must be preserved
        assert (memory_dir / "common-pitfalls.md").read_text() == (
            "# My custom pitfalls\nEdited by user."
        )

    @pytest.mark.asyncio
    async def test_watcher_no_pack_creates_profile_without_error(self, db, tmp_path):
        """(d) Agent-type with no matching knowledge pack creates profile without error.

        Knowledge packs are optional.  An agent-type that doesn't match any
        template (e.g. a custom type like 'devops') should still create the
        profile successfully with no starter knowledge.
        """
        from src.vault import ensure_default_templates, ensure_vault_profile_dirs

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)
        ensure_vault_profile_dirs(data_dir, "custom-devops")

        custom_profile = """\
---
id: custom-devops
name: Custom DevOps Agent
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""
        profile_path = tmp_path / "vault" / "agent-types" / "custom-devops" / "profile.md"
        _create_file(str(profile_path), custom_profile)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/custom-devops/profile.md",
            operation="created",
        )

        # Should not raise — profile created, no starter knowledge needed
        await on_profile_changed([change], db=db, data_dir=data_dir)

        profile = await db.get_profile("custom-devops")
        assert profile is not None
        assert profile.name == "Custom DevOps Agent"

        # Memory directory may or may not exist, but no knowledge files
        memory_dir = tmp_path / "vault" / "agent-types" / "custom-devops" / "memory"
        if memory_dir.exists():
            md_files = list(memory_dir.glob("*.md"))
            assert md_files == [], "No knowledge files should be present for custom type"

    @pytest.mark.asyncio
    async def test_watcher_memory_collection_ensured_on_create(self, db, tmp_path):
        """(e) Knowledge pack files are indexed into agent-type memory collection.

        When a profile is created via the command handler, ensure_agent_type_collection
        is called so the memory collection is ready for indexing starter knowledge.
        This test verifies the integration point in _cmd_create_profile.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.vault import ensure_default_templates

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)

        # Build a mock orchestrator with memory manager and DB
        mock_memory_mgr = MagicMock()
        mock_memory_mgr.ensure_agent_type_collection = AsyncMock(return_value=True)

        mock_orchestrator = MagicMock()
        mock_orchestrator.memory_manager = mock_memory_mgr
        mock_orchestrator.db = db

        mock_config = MagicMock()
        mock_config.data_dir = data_dir

        # Patch _cmd_create_profile's dependencies to test the call path
        with patch("src.command_handler.CommandHandler.__init__", return_value=None):
            from src.command_handler import CommandHandler

            handler = CommandHandler.__new__(CommandHandler)
            handler.orchestrator = mock_orchestrator
            handler.config = mock_config

            # Set up vault path to return a path that doesn't exist
            vault_path = tmp_path / "vault" / "agent-types" / "coding" / "profile.md"
            handler._vault_profile_path = MagicMock(return_value=str(vault_path))

            result = await handler._cmd_create_profile({"id": "coding", "name": "Coding Agent"})

        assert result.get("created") == "coding"
        # Verify ensure_agent_type_collection was called for memory indexing
        mock_memory_mgr.ensure_agent_type_collection.assert_awaited_once_with("coding")

    @pytest.mark.asyncio
    async def test_starter_tag_identifies_starter_content(self, db, tmp_path):
        """(f) #starter tag allows users to identify and optionally remove starter content.

        Users should be able to find all starter files by searching for the
        #starter tag in YAML frontmatter.  This test verifies that all copied
        starter files across all agent types are discoverable via this tag.
        """
        import yaml

        from src.vault import (
            _STARTER_KNOWLEDGE,
            ensure_default_templates,
            ensure_vault_profile_dirs,
        )

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)

        # Create profiles for all known agent types through the watcher
        for agent_type in _STARTER_KNOWLEDGE:
            ensure_vault_profile_dirs(data_dir, agent_type)

            profile_md = f"""\
---
id: {agent_type}
name: {agent_type.title()} Agent
---

## Config
```json
{{"model": "claude-sonnet-4-6"}}
```
"""
            profile_path = (
                tmp_path / "vault" / "agent-types" / agent_type / "profile.md"
            )
            _create_file(str(profile_path), profile_md)

            change = VaultChange(
                path=str(profile_path),
                rel_path=f"agent-types/{agent_type}/profile.md",
                operation="created",
            )
            await on_profile_changed([change], db=db, data_dir=data_dir)

        # Now search all memory dirs for files with #starter tag
        starter_files_found: list[str] = []
        for agent_type in _STARTER_KNOWLEDGE:
            memory_dir = tmp_path / "vault" / "agent-types" / agent_type / "memory"
            for md_file in sorted(memory_dir.glob("*.md")):
                content = md_file.read_text()
                if content.startswith("---"):
                    lines = content.strip().splitlines()
                    end_idx = next(
                        (i for i, line in enumerate(lines[1:], 1) if line == "---"),
                        None,
                    )
                    if end_idx:
                        fm = yaml.safe_load("\n".join(lines[1:end_idx]))
                        if fm and "starter" in fm.get("tags", []):
                            starter_files_found.append(f"{agent_type}/{md_file.name}")

        # Every file from every knowledge pack should be discoverable via #starter
        expected_count = sum(len(files) for files in _STARTER_KNOWLEDGE.values())
        assert len(starter_files_found) == expected_count, (
            f"Expected {expected_count} starter files, found {len(starter_files_found)}: "
            f"{starter_files_found}"
        )

    @pytest.mark.asyncio
    async def test_starter_files_are_copies_not_symlinks(self, db, tmp_path):
        """(g) Starter files are copies (not symlinks) — editing them does not affect template.

        Starter knowledge files must be independent copies of the templates.
        Modifying a copied file must not alter the original template, ensuring
        user customizations are safe.
        """
        from src.vault import ensure_default_templates, ensure_vault_profile_dirs

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)
        ensure_vault_profile_dirs(data_dir, "coding")

        profile_path = tmp_path / "vault" / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, data_dir=data_dir)

        memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
        template_dir = tmp_path / "vault" / "templates" / "knowledge" / "coding"

        for filename in ("common-pitfalls.md", "git-conventions.md"):
            copied_file = memory_dir / filename
            template_file = template_dir / filename

            # Verify the file is NOT a symlink
            assert not copied_file.is_symlink(), f"{filename} should not be a symlink"

            # Read original template content
            original_template = template_file.read_text()

            # Modify the copied file
            copied_file.write_text("# User-customized content\nThis replaces the starter.")

            # Verify the template is unchanged
            assert template_file.read_text() == original_template, (
                f"Editing {filename} in memory/ must not affect the template"
            )

    @pytest.mark.asyncio
    async def test_watcher_no_starter_copy_without_data_dir(self, db, tmp_path):
        """Watcher does not attempt starter knowledge copy when data_dir is None."""
        from src.vault import ensure_default_templates, ensure_vault_profile_dirs

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)
        ensure_vault_profile_dirs(data_dir, "coding")

        profile_path = tmp_path / "vault" / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="created",
        )

        # Call without data_dir — starter knowledge should NOT be copied
        await on_profile_changed([change], db=db, data_dir=None)

        # Profile synced to DB
        profile = await db.get_profile("coding")
        assert profile is not None

        # But no starter knowledge files copied (memory dir may exist from
        # ensure_vault_profile_dirs but should be empty of knowledge files)
        memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
        md_files = list(memory_dir.glob("*.md")) if memory_dir.exists() else []
        assert md_files == [], "No knowledge files should be copied without data_dir"

    @pytest.mark.asyncio
    async def test_watcher_no_starter_copy_on_modify(self, db, tmp_path):
        """Watcher does not copy starter knowledge on profile modification (only creation)."""
        from src.vault import ensure_default_templates, ensure_vault_profile_dirs

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)
        ensure_vault_profile_dirs(data_dir, "coding")

        # Pre-create the profile in DB so on_profile_changed treats it as update
        await db.create_profile(AgentProfile(id="coding", name="Old Name"))

        profile_path = tmp_path / "vault" / "agent-types" / "coding" / "profile.md"
        _create_file(str(profile_path), FULL_PROFILE)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/coding/profile.md",
            operation="modified",
        )
        await on_profile_changed([change], db=db, data_dir=data_dir)

        # Profile updated in DB
        profile = await db.get_profile("coding")
        assert profile.name == "Coding Agent"

        # No starter knowledge copied on modify
        memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
        md_files = list(memory_dir.glob("*.md")) if memory_dir.exists() else []
        assert md_files == [], "Starter knowledge should not be copied on modify"

    @pytest.mark.asyncio
    async def test_watcher_no_starter_copy_for_orchestrator(self, db, tmp_path):
        """Orchestrator profile does not get starter knowledge copied."""
        from src.vault import ensure_default_templates

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)

        orchestrator_profile = """\
---
id: orchestrator
name: Orchestrator
tags: [profile, orchestrator]
---

# Orchestrator

## Role
You are the orchestrator.

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""
        profile_path = tmp_path / "vault" / "orchestrator" / "profile.md"
        _create_file(str(profile_path), orchestrator_profile)

        change = VaultChange(
            path=str(profile_path),
            rel_path="orchestrator/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, data_dir=data_dir)

        # Profile synced to DB
        profile = await db.get_profile("orchestrator")
        assert profile is not None

        # No starter knowledge for orchestrator
        orch_memory = tmp_path / "vault" / "orchestrator" / "memory"
        md_files = list(orch_memory.glob("*.md")) if orch_memory.exists() else []
        assert md_files == [], "Orchestrator should not get starter knowledge"

    @pytest.mark.asyncio
    async def test_watcher_copies_code_review_starter_knowledge(self, db, tmp_path):
        """Watcher copies code-review knowledge pack for code-review profile."""
        from src.vault import ensure_default_templates, ensure_vault_profile_dirs

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)
        ensure_vault_profile_dirs(data_dir, "code-review")

        cr_profile = """\
---
id: code-review
name: Code Review Agent
---

## Config
```json
{"model": "claude-sonnet-4-6"}
```
"""
        profile_path = tmp_path / "vault" / "agent-types" / "code-review" / "profile.md"
        _create_file(str(profile_path), cr_profile)

        change = VaultChange(
            path=str(profile_path),
            rel_path="agent-types/code-review/profile.md",
            operation="created",
        )
        await on_profile_changed([change], db=db, data_dir=data_dir)

        memory_dir = tmp_path / "vault" / "agent-types" / "code-review" / "memory"
        assert (memory_dir / "review-checklist.md").is_file()
        assert (memory_dir / "review-process.md").is_file()

    @pytest.mark.asyncio
    async def test_cmd_create_profile_returns_starter_knowledge(self, db, tmp_path):
        """_cmd_create_profile includes starter_knowledge list in result."""
        from unittest.mock import MagicMock, patch

        from src.vault import ensure_default_templates

        data_dir = str(tmp_path)
        ensure_default_templates(data_dir)

        mock_orchestrator = MagicMock()
        mock_orchestrator.memory_manager = None  # No memory manager
        mock_orchestrator.db = db

        mock_config = MagicMock()
        mock_config.data_dir = data_dir

        with patch("src.command_handler.CommandHandler.__init__", return_value=None):
            from src.command_handler import CommandHandler

            handler = CommandHandler.__new__(CommandHandler)
            handler.orchestrator = mock_orchestrator
            handler.config = mock_config

            vault_path = tmp_path / "vault" / "agent-types" / "coding" / "profile.md"
            handler._vault_profile_path = MagicMock(return_value=str(vault_path))

            result = await handler._cmd_create_profile({"id": "coding", "name": "Coding Agent"})

        assert result.get("created") == "coding"
        assert "starter_knowledge" in result, "Result should include starter_knowledge list"
        assert "common-pitfalls.md" in result["starter_knowledge"]
        assert "git-conventions.md" in result["starter_knowledge"]
