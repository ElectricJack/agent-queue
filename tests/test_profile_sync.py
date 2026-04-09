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
"""

from __future__ import annotations

import os
import time

import pytest

from src.database import Database
from src.models import AgentProfile
from src.profile_parser import parse_profile
from src.profile_sync import (
    PROFILE_PATTERNS,
    ProfileSyncResult,
    derive_profile_id,
    on_profile_changed,
    register_profile_handlers,
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
