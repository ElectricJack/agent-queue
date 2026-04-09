"""Tests for RuleManager -- rule file I/O and CRUD."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def storage_root(tmp_path):
    """Temporary storage root mimicking ~/.agent-queue."""
    return str(tmp_path)


@pytest.fixture
def rule_manager(storage_root):
    from src.rule_manager import RuleManager

    return RuleManager(storage_root=storage_root)


def test_save_rule_creates_file(rule_manager, storage_root):
    """save_rule writes a markdown file with YAML frontmatter."""
    result = rule_manager.save_rule(
        id="rule-test",
        project_id="my-project",
        rule_type="passive",
        content="# Test Rule\n\n## Intent\nDo the thing.",
    )
    assert result["success"] is True
    assert result["id"] == "rule-test"

    rule_path = os.path.join(
        storage_root, "vault", "projects", "my-project", "playbooks", "rule-test.md"
    )
    assert os.path.isfile(rule_path)

    with open(rule_path) as f:
        raw = f.read()
    assert "id: rule-test" in raw
    assert "type: passive" in raw
    assert "# Test Rule" in raw


def test_save_rule_global_scope(rule_manager, storage_root):
    """project_id=None saves to global rules directory."""
    result = rule_manager.save_rule(
        id="rule-global",
        project_id=None,
        rule_type="passive",
        content="# Global Rule\n\n## Intent\nApply everywhere.",
    )
    assert result["success"] is True

    rule_path = os.path.join(storage_root, "vault", "system", "playbooks", "rule-global.md")
    assert os.path.isfile(rule_path)


def test_save_rule_auto_generates_id(rule_manager):
    """When id is omitted, save_rule generates one from the title."""
    result = rule_manager.save_rule(
        id=None,
        project_id="my-project",
        rule_type="passive",
        content="# Keep Tunnel Open\n\n## Intent\nMonitor tunnel.",
    )
    assert result["success"] is True
    assert result["id"].startswith("rule-")
    assert "tunnel" in result["id"].lower() or len(result["id"]) > 5


def test_save_rule_updates_existing(rule_manager, storage_root):
    """Saving with same id overwrites the file and updates timestamp."""
    rule_manager.save_rule(
        id="rule-update",
        project_id="proj",
        rule_type="passive",
        content="# V1\n\nOriginal.",
    )
    time.sleep(0.05)  # ensure timestamp differs
    result = rule_manager.save_rule(
        id="rule-update",
        project_id="proj",
        rule_type="passive",
        content="# V2\n\nUpdated.",
    )
    assert result["success"] is True

    loaded = rule_manager.load_rule("rule-update")
    assert "V2" in loaded["content"]


def test_load_rule(rule_manager):
    """load_rule returns full rule data with frontmatter fields."""
    rule_manager.save_rule(
        id="rule-load",
        project_id="proj",
        rule_type="active",
        content="# Active Rule\n\n## Trigger\nEvery 5 minutes.",
    )

    loaded = rule_manager.load_rule("rule-load")
    assert loaded is not None
    assert loaded["id"] == "rule-load"
    assert loaded["type"] == "active"
    assert loaded["project_id"] == "proj"
    assert "Active Rule" in loaded["content"]
    assert "created" in loaded
    assert "updated" in loaded
    assert isinstance(loaded["hooks"], list)


def test_load_rule_not_found(rule_manager):
    """load_rule returns None for nonexistent rule."""
    assert rule_manager.load_rule("nonexistent") is None


def test_delete_rule(rule_manager, storage_root):
    """delete_rule removes the file from disk."""
    rule_manager.save_rule(
        id="rule-delete",
        project_id="proj",
        rule_type="passive",
        content="# Delete Me",
    )
    result = rule_manager.delete_rule("rule-delete")
    assert result["success"] is True
    assert rule_manager.load_rule("rule-delete") is None


def test_delete_rule_not_found(rule_manager):
    """delete_rule returns error for nonexistent rule."""
    result = rule_manager.delete_rule("nonexistent")
    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_browse_rules_project_and_global(rule_manager):
    """browse_rules returns both project-scoped and global rules."""
    rule_manager.save_rule("rule-proj", "proj", "passive", "# Proj Rule")
    rule_manager.save_rule("rule-glob", None, "passive", "# Global Rule")

    rules = rule_manager.browse_rules("proj")
    ids = [r["id"] for r in rules]
    assert "rule-proj" in ids
    assert "rule-glob" in ids


def test_browse_rules_summary_truncation(rule_manager):
    """browse_rules returns first 200 chars as summary."""
    long_intent = "A" * 500
    rule_manager.save_rule(
        "rule-long",
        "proj",
        "passive",
        f"# Long Rule\n\n## Intent\n{long_intent}",
    )
    rules = rule_manager.browse_rules("proj")
    long_rule = [r for r in rules if r["id"] == "rule-long"][0]
    assert len(long_rule["summary"]) <= 200


def test_get_rules_for_prompt(rule_manager):
    """get_rules_for_prompt returns formatted markdown for PromptBuilder."""
    rule_manager.save_rule("rule-style", "proj", "passive", "# Code Style\n\n## Intent\nUse black.")
    rule_manager.save_rule("rule-glob", None, "active", "# Monitor\n\n## Trigger\nEvery 5 min.")

    text = rule_manager.get_rules_for_prompt("proj")
    assert "Applicable Rules" in text
    assert "Code Style" in text
    assert "Monitor" in text
    assert "[Passive]" in text
    assert "[Active]" in text


def test_get_rules_for_prompt_empty(rule_manager):
    """get_rules_for_prompt returns empty string when no rules exist."""
    text = rule_manager.get_rules_for_prompt("proj")
    assert text == ""


# ------------------------------------------------------------------
# Async hook generation and deletion (Task 3)
# ------------------------------------------------------------------


@pytest.fixture
def mock_db():
    """Mock database that tracks hook create/delete calls."""
    db = AsyncMock()
    db.create_hook = AsyncMock()
    db.delete_hook = AsyncMock()
    db.get_hook = AsyncMock(return_value=None)
    db.list_hooks_by_id_prefix = AsyncMock(return_value=[])
    return db


@pytest.fixture
def rule_manager_with_db(storage_root, mock_db):
    from src.rule_manager import RuleManager

    return RuleManager(storage_root=storage_root, db=mock_db)


def test_async_delete_rule_removes_hooks(rule_manager_with_db, mock_db):
    """async_delete_rule removes associated hooks from the database."""
    rm = rule_manager_with_db

    # Save rule and manually set hooks in frontmatter
    rm.save_rule(
        id="rule-with-hooks",
        project_id="proj",
        rule_type="active",
        content="# Active Rule\n\n## Trigger\nEvery 5 min.",
    )
    rm._update_rule_hooks("rule-with-hooks", ["hook-from-rule"])

    result = asyncio.run(rm.async_delete_rule("rule-with-hooks"))
    assert result["success"] is True
    assert "hook-from-rule" in result["hooks_removed"]

    # Verify delete_hook was called on DB
    mock_db.delete_hook.assert_any_call("hook-from-rule")


def test_async_save_active_rule_generates_hooks(rule_manager_with_db, mock_db):
    """async_save_rule generates hooks for active rules with triggers."""
    rm = rule_manager_with_db

    result = asyncio.run(
        rm.async_save_rule(
            id="rule-periodic",
            project_id="proj",
            rule_type="active",
            content=("# Check Status\n\n## Trigger\nEvery 10 minutes.\n\n## Logic\nRun check."),
        )
    )
    assert result["success"] is True
    assert len(result["hooks_generated"]) == 1

    # Verify hook was created in DB
    mock_db.create_hook.assert_called_once()
    created_hook = mock_db.create_hook.call_args[0][0]
    assert created_hook.project_id == "proj"
    assert "rule-periodic" in created_hook.prompt_template


def test_reconcile_regenerates_hooks(rule_manager_with_db, mock_db):
    """reconcile regenerates hooks for all active rules."""
    rm = rule_manager_with_db

    rm.save_rule(
        id="rule-active",
        project_id="proj",
        rule_type="active",
        content=("# Active Rule\n\n## Trigger\nEvery 10 min.\n\n## Logic\nCheck status."),
    )

    result = asyncio.run(rm.reconcile())
    assert result["rules_scanned"] > 0
    assert result["hooks_regenerated"] > 0
    assert result["active_rules"] > 0
    mock_db.create_hook.assert_called()


def test_reconcile_unchanged_rules_report_zero_regenerated(storage_root, mock_db):
    """reconcile reports 0 hooks_regenerated when rules haven't changed.

    When existing hooks already have matching source_hash values,
    _generate_hooks_for_rule returns [] (skipped), so reconcile should
    not count them as regenerated.
    """
    from src.models import Hook
    from src.rule_manager import RuleManager, _compute_source_hash

    content = "# My Rule\n\n## Trigger\nEvery 60 minutes.\n\n## Logic\nDo something."

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    rm.save_rule(
        id="my-rule",
        project_id="proj",
        rule_type="active",
        content=content,
    )

    # Compute the hash the same way the code does
    trigger_config = rm._parse_trigger(content)
    current_hash = _compute_source_hash(trigger_config, content)

    # Simulate existing hook with matching source_hash (i.e. unchanged)
    existing_hook = Hook(
        id="rule-my-rule-abc123",
        project_id="proj",
        name="Rule: My Rule",
        trigger='{"type": "periodic", "interval_seconds": 3600}',
        source_hash=current_hash,
    )
    mock_db.list_hooks_by_id_prefix = AsyncMock(return_value=[existing_hook])

    result = asyncio.run(rm.reconcile())
    assert result["rules_scanned"] >= 1
    assert result["active_rules"] >= 1
    assert result["hooks_regenerated"] == 0
    assert result["hooks_unchanged"] >= 1
    # create_hook should NOT have been called since nothing changed
    mock_db.create_hook.assert_not_called()


def test_reconcile_preserves_last_triggered_at(storage_root, mock_db):
    """Reconciliation preserves last_triggered_at from old hooks.

    When hooks are deleted and recreated during reconciliation, the new hooks
    should inherit the last_triggered_at timestamp from the old hooks so
    periodic hooks don't re-fire immediately after a restart.
    """
    from src.models import Hook
    from src.rule_manager import RuleManager

    # Simulate an old hook with a recent last_triggered_at
    old_timestamp = time.time() - 300  # 5 minutes ago
    old_hook = Hook(
        id="rule-my-rule-abc123",
        project_id="proj",
        name="Rule: My Rule",
        trigger='{"type": "periodic", "interval_seconds": 3600}',
        last_triggered_at=old_timestamp,
    )
    mock_db.list_hooks_by_id_prefix = AsyncMock(return_value=[old_hook])
    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=1)

    rm = RuleManager(storage_root=storage_root, db=mock_db)

    rm.save_rule(
        id="my-rule",
        project_id="proj",
        rule_type="active",
        content=("# My Rule\n\n## Trigger\nEvery 60 minutes.\n\n## Logic\nDo something."),
    )

    result = asyncio.run(rm.reconcile())
    assert result["hooks_regenerated"] > 0

    # The newly created hook should have the old timestamp
    created_hook = mock_db.create_hook.call_args[0][0]
    assert created_hook.last_triggered_at == old_timestamp


def test_hook_generation_uses_llm_expansion(storage_root, mock_db):
    """Hook generation calls supervisor.expand_rule_prompt when available."""
    from src.rule_manager import RuleManager

    mock_supervisor = MagicMock()
    mock_supervisor.expand_rule_prompt = AsyncMock(
        return_value="Check tunnel with: pgrep -f cloudflared"
    )
    mock_orch = MagicMock()
    mock_orch._supervisor = mock_supervisor

    rm = RuleManager(
        storage_root=storage_root,
        db=mock_db,
        orchestrator=mock_orch,
    )
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-tunnel",
            project_id="proj",
            rule_type="active",
            content=(
                "# Keep Tunnel Open\n\n## Trigger\nEvery 5 minutes."
                "\n\n## Logic\nCheck if tunnel is running."
            ),
        )
    )
    assert result["success"] is True
    mock_supervisor.expand_rule_prompt.assert_called_once()
    created_hook = mock_db.create_hook.call_args[0][0]
    assert "pgrep" in created_hook.prompt_template
    assert "rule-tunnel" not in created_hook.prompt_template


def test_global_rule_generates_hooks_for_all_projects(storage_root, mock_db):
    """Global rules (project_id=None) create one hook per project."""
    from src.rule_manager import RuleManager
    from src.models import Project

    mock_db.list_projects = AsyncMock(
        return_value=[
            Project(id="proj-a", name="A"),
            Project(id="proj-b", name="B"),
        ]
    )

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-global",
            project_id=None,
            rule_type="active",
            content=("# Global Check\n\n## Trigger\nEvery 10 minutes.\n\n## Logic\nDo stuff."),
        )
    )
    assert result["success"] is True
    assert len(result["hooks_generated"]) == 2
    assert mock_db.create_hook.call_count == 2

    # Verify hooks target different projects
    pids = {mock_db.create_hook.call_args_list[i][0][0].project_id for i in range(2)}
    assert pids == {"proj-a", "proj-b"}


def test_global_rule_hooks_include_project_context(storage_root, mock_db):
    """Global rule hooks should include per-project context in their prompt."""
    from src.rule_manager import RuleManager
    from src.models import Project

    mock_db.list_projects = AsyncMock(
        return_value=[
            Project(id="proj-a", name="A"),
            Project(id="proj-b", name="B"),
        ]
    )

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-global-ctx",
            project_id=None,
            rule_type="active",
            content=(
                "# Global Sync\n\n## Trigger\nEvery hour."
                "\n\n## Logic\nSync workspaces for this project."
            ),
        )
    )
    assert result["success"] is True
    assert mock_db.create_hook.call_count == 2

    # Each hook's prompt should include its specific project context
    for call in mock_db.create_hook.call_args_list:
        hook = call[0][0]
        assert f"scoped to project `{hook.project_id}`" in hook.prompt_template
        assert "MUST target project" in hook.prompt_template

    # Verify proj-a hook mentions proj-a, not proj-b
    hook_a = mock_db.create_hook.call_args_list[0][0][0]
    hook_b = mock_db.create_hook.call_args_list[1][0][0]
    assert f"`{hook_a.project_id}`" in hook_a.prompt_template
    assert f"`{hook_b.project_id}`" in hook_b.prompt_template


def test_project_scoped_rule_no_extra_context(storage_root, mock_db):
    """Project-scoped rules should NOT add redundant project context prefix."""
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-scoped",
            project_id="my-proj",
            rule_type="active",
            content=(
                "# Scoped Rule\n\n## Trigger\nEvery 30 minutes."
                "\n\n## Logic\nDo project-specific stuff."
            ),
        )
    )
    assert result["success"] is True
    assert mock_db.create_hook.call_count == 1

    hook = mock_db.create_hook.call_args_list[0][0][0]
    # Project-scoped hooks should not have the global context prefix
    assert "**Project context:**" not in hook.prompt_template


def test_global_rule_no_projects_creates_no_hooks(storage_root, mock_db):
    """Global rules with no projects in DB create no hooks."""
    from src.rule_manager import RuleManager

    mock_db.list_projects = AsyncMock(return_value=[])

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-empty",
            project_id=None,
            rule_type="active",
            content=("# Empty\n\n## Trigger\nEvery 5 minutes.\n\n## Logic\nCheck."),
        )
    )
    assert result["success"] is True
    assert len(result["hooks_generated"]) == 0
    mock_db.create_hook.assert_not_called()


def test_hook_generation_falls_back_without_supervisor(
    rule_manager_with_db,
    mock_db,
):
    """Hook generation uses static template when supervisor is unavailable."""
    result = asyncio.run(
        rule_manager_with_db.async_save_rule(
            id="rule-no-sup",
            project_id="proj",
            rule_type="active",
            content=("# Check Status\n\n## Trigger\nEvery 10 minutes.\n\n## Logic\nRun check."),
        )
    )
    assert result["success"] is True
    created_hook = mock_db.create_hook.call_args[0][0]
    assert "rule-no-sup" in created_hook.prompt_template
    assert "Follow the rule" in created_hook.prompt_template


def test_hook_generation_falls_back_on_llm_failure(storage_root, mock_db):
    """Hook generation falls back to static template when LLM fails."""
    from src.rule_manager import RuleManager

    mock_supervisor = MagicMock()
    mock_supervisor.expand_rule_prompt = AsyncMock(return_value=None)
    mock_orch = MagicMock()
    mock_orch._supervisor = mock_supervisor

    rm = RuleManager(
        storage_root=storage_root,
        db=mock_db,
        orchestrator=mock_orch,
    )
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-fail",
            project_id="proj",
            rule_type="active",
            content=("# Failing\n\n## Trigger\nEvery 5 minutes.\n\n## Logic\nDo stuff."),
        )
    )
    assert result["success"] is True
    created_hook = mock_db.create_hook.call_args[0][0]
    assert "rule-fail" in created_hook.prompt_template


def test_parse_trigger_periodic():
    """_parse_trigger extracts periodic triggers."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Trigger\nEvery 5 minutes.\n\n## Logic\nDo stuff."
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "periodic", "interval_seconds": 300}


def test_parse_trigger_event():
    """_parse_trigger extracts event-based triggers."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Trigger\nWhen a task is completed.\n\n## Logic\nReview."
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "event", "event_type": "task.completed"}


def test_parse_trigger_none():
    """_parse_trigger returns None for unparseable triggers."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Intent\nNo trigger here."
    result = RuleManager._parse_trigger(content)
    assert result is None


# ------------------------------------------------------------------
# CommandHandler integration (Task 4)
# ------------------------------------------------------------------


def _make_command_handler(storage_root, mock_db):
    """Create a CommandHandler with a mocked orchestrator and RuleManager.

    Patches sys.modules for missing dependencies (discord, aiosqlite)
    so CommandHandler can be imported in test environments.
    """
    import sys

    # Ensure discord and aiosqlite stubs exist so command_handler can import
    mods_to_stub = {}
    for mod_name in [
        "discord",
        "discord.ext",
        "discord.ext.commands",
        "discord.ui",
        "aiosqlite",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()
            mods_to_stub[mod_name] = True

    from src.rule_manager import RuleManager
    from src.command_handler import CommandHandler

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    orch = MagicMock()
    orch.db = mock_db
    orch.rule_manager = rm
    orch.config = MagicMock()
    return CommandHandler(orch, orch.config), rm


def test_cmd_save_rule(storage_root, mock_db):
    """CommandHandler.execute('save_rule') creates a rule file."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    result = asyncio.run(
        handler.execute(
            "save_rule",
            {
                "project_id": "proj",
                "type": "passive",
                "content": "# Check Code Style\n\n## Intent\nEnforce linting.",
            },
        )
    )
    assert result.get("success") is True or "id" in result


def test_cmd_browse_rules(storage_root, mock_db):
    """CommandHandler.execute('browse_rules') returns rule list."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    rm.save_rule("rule-1", "proj", "passive", "# Rule One")

    result = asyncio.run(handler.execute("browse_rules", {"project_id": "proj"}))
    assert "rules" in result
    assert any(r["id"] == "rule-1" for r in result["rules"])


def test_cmd_load_rule(storage_root, mock_db):
    """CommandHandler.execute('load_rule') returns full rule."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    rm.save_rule("rule-2", "proj", "passive", "# Rule Two\n\nContent here.")

    result = asyncio.run(handler.execute("load_rule", {"id": "rule-2"}))
    assert result.get("id") == "rule-2"
    assert "Rule Two" in result.get("content", "")


def test_cmd_delete_rule(storage_root, mock_db):
    """CommandHandler.execute('delete_rule') removes a rule."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    rm.save_rule("rule-3", "proj", "passive", "# Rule Three")

    result = asyncio.run(handler.execute("delete_rule", {"id": "rule-3"}))
    assert result.get("success") is True


def test_cmd_refresh_hooks(storage_root, mock_db):
    """CommandHandler.execute('refresh_hooks') reconciles hooks from rules."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    # Save a passive rule (no hooks to generate, but should be scanned)
    rm.save_rule("rule-scan", "proj", "passive", "# Passive Rule")

    result = asyncio.run(handler.execute("refresh_hooks", {}))
    assert result.get("success") is True
    assert result.get("rules_scanned", 0) >= 1
    assert "errors" in result


def test_cmd_refresh_hooks_no_rule_manager(storage_root, mock_db):
    """refresh_hooks returns error when rule manager is not initialized."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    handler.orchestrator.rule_manager = None

    result = asyncio.run(handler.execute("refresh_hooks", {}))
    assert "error" in result


# ------------------------------------------------------------------
# Orchestrator integration (Task 7)
# ------------------------------------------------------------------


def test_orchestrator_initializes_rule_manager(tmp_path):
    """Orchestrator creates RuleManager during initialize()."""
    import sys

    # Ensure discord/aiosqlite stubs exist so orchestrator can import
    for mod_name in [
        "discord",
        "discord.ext",
        "discord.ext.commands",
        "discord.ui",
        "aiosqlite",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    from unittest.mock import patch, AsyncMock

    from src.orchestrator import Orchestrator

    config = MagicMock()
    config.data_dir = str(tmp_path)
    config.database_path = str(tmp_path / "test.db")
    config.global_token_budget_daily = 1000000
    config.hook_engine = MagicMock()
    config.hook_engine.enabled = False
    config.memory = MagicMock()
    config.memory.enabled = False
    config._config_path = None
    config.llm_logging = MagicMock()
    config.llm_logging.enabled = False
    config.llm_logging.retention_days = 30
    config.agent_profiles = []

    orch = Orchestrator(config)

    # Mock DB and recovery so initialize() doesn't need real sqlite
    orch.db = MagicMock()
    orch.db.initialize = AsyncMock()
    orch.db.list_agents = AsyncMock(return_value=[])
    orch.db.list_workspaces = AsyncMock(return_value=[])
    orch.db.list_tasks = AsyncMock(return_value=[])
    orch.db.list_projects = AsyncMock(return_value=[])
    orch.db.list_profiles = AsyncMock(return_value=[])

    with patch.object(orch, "_sync_profiles_from_config", new_callable=AsyncMock):
        asyncio.run(orch.initialize())

    assert hasattr(orch, "rule_manager")
    assert orch.rule_manager is not None


# ------------------------------------------------------------------
# Default rule installation (Task 8)
# ------------------------------------------------------------------


def test_install_default_rules(storage_root):
    """install_defaults creates global rules from bundled templates."""
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=storage_root)

    installed = rm.install_defaults()
    assert len(installed) >= 2

    # Verify rules are browseable
    rules = rm.browse_rules(None)
    ids = [r["id"] for r in rules]
    assert any("reflection" in rid for rid in ids)
    assert any("review" in rid for rid in ids)


def test_install_defaults_idempotent(storage_root):
    """install_defaults does not duplicate rules on re-run."""
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=storage_root)

    rm.install_defaults()
    rm.install_defaults()

    rules = rm.browse_rules(None)
    # Count rules -- should not have duplicates
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids))


# ------------------------------------------------------------------
# Rule file watcher (Phase 1)
# ------------------------------------------------------------------


def test_get_all_rule_dirs(storage_root):
    """_get_all_rule_dirs returns existing vault playbook directories."""
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=storage_root)

    # Create vault playbook dirs
    os.makedirs(os.path.join(storage_root, "vault", "projects", "proj-a", "playbooks"))
    os.makedirs(os.path.join(storage_root, "vault", "system", "playbooks"))

    dirs = rm._get_all_rule_dirs()
    paths = {d[0] for d in dirs}
    pids = {d[1] for d in dirs}

    assert len(dirs) == 2
    assert any("proj-a" in p for p in paths)
    assert any("system" in p for p in paths)
    assert "proj-a" in pids
    assert None in pids  # global scope


def test_start_and_stop_file_watcher(storage_root):
    """start_file_watcher creates a watcher and stop cleans up."""
    from src.event_bus import EventBus
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=storage_root)
    os.makedirs(os.path.join(storage_root, "vault", "projects", "proj", "playbooks"))
    bus = EventBus()

    async def _run():
        await rm.start_file_watcher(bus)
        assert rm._rule_file_watcher is not None
        assert rm._watcher_task is not None
        assert not rm._watcher_task.done()

        await rm.stop_file_watcher()
        assert rm._rule_file_watcher is None

    asyncio.run(_run())


def test_on_rule_folder_changed_ignores_non_rule_watches(storage_root):
    """_on_rule_folder_changed ignores events from non-rule watches."""
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=storage_root, db=AsyncMock())

    async def _run():
        # Event from hook engine file watcher — should be ignored
        await rm._on_rule_folder_changed(
            {
                "watch_id": "hook-123",
                "changes": [{"path": "test.md", "operation": "modified"}],
                "path": "/some/dir",
            }
        )
        # No errors, no DB calls
        rm._db.delete_hooks_by_id_prefix.assert_not_called()

    asyncio.run(_run())


def test_on_rule_folder_changed_modified(storage_root, mock_db):
    """_on_rule_folder_changed reconciles modified active rules."""
    from src.rule_manager import RuleManager

    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=0)

    rm = RuleManager(storage_root=storage_root, db=mock_db)

    # Create an active rule file on disk (vault location)
    rm.save_rule(
        id="rule-watch-test",
        project_id="proj",
        rule_type="active",
        content="# Watch Test\n\n## Trigger\nEvery 10 minutes.\n\n## Logic\nDo it.",
    )
    rules_dir = os.path.join(storage_root, "vault", "projects", "proj", "playbooks")

    async def _run():
        await rm._on_rule_folder_changed(
            {
                "watch_id": "rule-watcher-proj",
                "changes": [{"path": "rule-watch-test.md", "operation": "modified"}],
                "path": rules_dir,
            }
        )
        # Should have created hooks
        mock_db.create_hook.assert_called()
        created_hook = mock_db.create_hook.call_args[0][0]
        assert created_hook.project_id == "proj"

    asyncio.run(_run())


def test_on_rule_folder_changed_deleted(storage_root, mock_db):
    """_on_rule_folder_changed cleans up hooks for deleted rules."""
    from src.rule_manager import RuleManager

    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=2)

    rm = RuleManager(storage_root=storage_root, db=mock_db)

    async def _run():
        await rm._on_rule_folder_changed(
            {
                "watch_id": "rule-watcher-proj",
                "changes": [{"path": "rule-deleted.md", "operation": "deleted"}],
                "path": "/some/dir",
            }
        )
        mock_db.delete_hooks_by_id_prefix.assert_called_once_with("rule-rule-deleted-")

    asyncio.run(_run())


def test_on_rule_folder_changed_passive_rule_skipped(storage_root, mock_db):
    """_on_rule_folder_changed skips passive rules (no hook generation)."""
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=storage_root, db=mock_db)

    # Create a passive rule file on disk (vault location)
    rm.save_rule(
        id="rule-passive",
        project_id="proj",
        rule_type="passive",
        content="# Passive Rule\n\n## Intent\nJust guidance.",
    )
    rules_dir = os.path.join(storage_root, "vault", "projects", "proj", "playbooks")

    async def _run():
        await rm._on_rule_folder_changed(
            {
                "watch_id": "rule-watcher-proj",
                "changes": [{"path": "rule-passive.md", "operation": "modified"}],
                "path": rules_dir,
            }
        )
        # No hooks should be created for passive rules
        mock_db.create_hook.assert_not_called()

    asyncio.run(_run())


# ------------------------------------------------------------------
# Orphan hook migration tests
# ------------------------------------------------------------------


def test_migrate_orphan_hooks_converts_direct_hooks(storage_root):
    """migrate_orphan_hooks creates rule files from non-rule-prefixed hooks."""
    import json

    from src.models import Hook
    from src.rule_manager import RuleManager

    orphan_hook = Hook(
        id="my-custom-hook",
        project_id="test-project",
        name="Nightly Cleanup",
        trigger=json.dumps({"type": "periodic", "interval_seconds": 86400}),
        prompt_template="Clean up old temp files and report.",
        cooldown_seconds=3600,
    )
    rule_hook = Hook(
        id="rule-existing-abc123",
        project_id="test-project",
        name="Rule: Existing Rule",
        trigger=json.dumps({"type": "periodic", "interval_seconds": 300}),
        prompt_template="Already managed by a rule.",
        cooldown_seconds=150,
    )

    mock_db = AsyncMock()
    mock_db.list_hooks = AsyncMock(return_value=[orphan_hook, rule_hook])
    mock_db.delete_hook = AsyncMock()
    # Also need these for reconcile's hook generation
    mock_db.list_hooks_by_id_prefix = AsyncMock(return_value=[])
    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=0)
    mock_db.create_hook = AsyncMock()

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    stats = asyncio.run(rm.migrate_orphan_hooks())

    assert stats["migrated"] == 1
    assert stats["skipped"] == 1
    assert stats["errors"] == 0

    # Verify the orphan hook was deleted
    mock_db.delete_hook.assert_called_once_with("my-custom-hook")

    # Verify a rule file was created
    rules = rm.browse_rules("test-project")
    assert len(rules) == 1
    assert rules[0]["type"] == "active"
    assert "Nightly Cleanup" in rules[0]["name"]

    # Load rule and verify content
    loaded = rm.load_rule(rules[0]["id"])
    assert loaded is not None
    assert "Nightly Cleanup" in loaded["content"]
    assert "Every 1 day" in loaded["content"]
    assert "Clean up old temp files" in loaded["content"]
    assert "Cooldown: 3600 seconds" in loaded["content"]


def test_migrate_orphan_hooks_idempotent(storage_root):
    """Running migrate_orphan_hooks twice doesn't create duplicate rules."""
    import json

    from src.models import Hook
    from src.rule_manager import RuleManager

    orphan = Hook(
        id="direct-hook-1",
        project_id="proj",
        name="My Hook",
        trigger=json.dumps({"type": "event", "event_type": "task.completed"}),
        prompt_template="Check results.",
        cooldown_seconds=600,
    )

    mock_db = AsyncMock()
    mock_db.list_hooks = AsyncMock(return_value=[orphan])
    mock_db.delete_hook = AsyncMock()
    mock_db.list_hooks_by_id_prefix = AsyncMock(return_value=[])
    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=0)
    mock_db.create_hook = AsyncMock()

    rm = RuleManager(storage_root=storage_root, db=mock_db)

    # First migration
    stats1 = asyncio.run(rm.migrate_orphan_hooks())
    assert stats1["migrated"] == 1

    # Second migration — same hook still in DB (simulate it not being deleted yet)
    mock_db.delete_hook.reset_mock()
    stats2 = asyncio.run(rm.migrate_orphan_hooks())
    # Should still delete the hook but not create a duplicate rule
    assert stats2["migrated"] == 1
    mock_db.delete_hook.assert_called_once_with("direct-hook-1")

    # Only one rule file should exist
    rules = rm.browse_rules("proj")
    assert len(rules) == 1


def test_migrate_orphan_hooks_no_db(storage_root):
    """migrate_orphan_hooks returns empty stats when no DB is available."""
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=storage_root, db=None)
    stats = asyncio.run(rm.migrate_orphan_hooks())
    assert stats == {"migrated": 0, "skipped": 0, "errors": 0}


def test_migrate_orphan_hooks_event_trigger(storage_root):
    """Event-based trigger hooks are correctly reverse-parsed."""
    import json

    from src.models import Hook
    from src.rule_manager import RuleManager

    hook = Hook(
        id="event-hook",
        project_id="proj",
        name="On Task Fail",
        trigger=json.dumps({"type": "event", "event_type": "task.failed"}),
        prompt_template="Analyze the failure.",
        cooldown_seconds=300,
    )

    mock_db = AsyncMock()
    mock_db.list_hooks = AsyncMock(return_value=[hook])
    mock_db.delete_hook = AsyncMock()
    mock_db.list_hooks_by_id_prefix = AsyncMock(return_value=[])
    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=0)
    mock_db.create_hook = AsyncMock()

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    asyncio.run(rm.migrate_orphan_hooks())

    rules = rm.browse_rules("proj")
    loaded = rm.load_rule(rules[0]["id"])
    assert "When task failed" in loaded["content"]


def test_reverse_parse_trigger_periodic():
    """_reverse_parse_trigger handles periodic triggers."""
    from src.rule_manager import RuleManager

    assert (
        RuleManager._reverse_parse_trigger('{"type": "periodic", "interval_seconds": 3600}')
        == "Every 1 hour."
    )
    assert (
        RuleManager._reverse_parse_trigger('{"type": "periodic", "interval_seconds": 300}')
        == "Every 5 minutes."
    )
    assert (
        RuleManager._reverse_parse_trigger('{"type": "periodic", "interval_seconds": 86400}')
        == "Every 1 day."
    )
    assert (
        RuleManager._reverse_parse_trigger('{"type": "periodic", "interval_seconds": 45}')
        == "Every 45 seconds."
    )


def test_reverse_parse_trigger_event():
    """_reverse_parse_trigger handles event triggers."""
    from src.rule_manager import RuleManager

    result = RuleManager._reverse_parse_trigger('{"type": "event", "event_type": "task.completed"}')
    assert result == "When task completed."


def test_reverse_parse_trigger_cron():
    """_reverse_parse_trigger handles cron triggers."""
    from src.rule_manager import RuleManager

    result = RuleManager._reverse_parse_trigger('{"type": "cron", "cron": "0 */6 * * *"}')
    assert result == "Cron schedule: `0 */6 * * *`."


def test_reverse_parse_trigger_empty():
    """_reverse_parse_trigger handles empty/invalid input."""
    from src.rule_manager import RuleManager

    assert "No trigger" in RuleManager._reverse_parse_trigger("{}")
    assert "Unknown" in RuleManager._reverse_parse_trigger("not json")
    assert "No trigger" in RuleManager._reverse_parse_trigger("")


def test_reconcile_includes_migration_stats(storage_root):
    """reconcile() runs orphan migration and includes stats."""
    import json

    from src.models import Hook
    from src.rule_manager import RuleManager

    orphan = Hook(
        id="orphan-1",
        project_id="proj",
        name="Orphan Hook",
        trigger=json.dumps({"type": "periodic", "interval_seconds": 600}),
        prompt_template="Do something.",
        cooldown_seconds=300,
    )

    mock_db = AsyncMock()
    mock_db.list_hooks = AsyncMock(return_value=[orphan])
    mock_db.delete_hook = AsyncMock()
    mock_db.list_hooks_by_id_prefix = AsyncMock(return_value=[])
    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=0)
    mock_db.create_hook = AsyncMock()

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    stats = asyncio.run(rm.reconcile())

    assert stats["orphan_hooks_migrated"] == 1
    # The migrated rule should also have been reconciled (hook regenerated)
    assert stats["rules_scanned"] >= 1


# ------------------------------------------------------------------
# Extended trigger parsing tests
# ------------------------------------------------------------------


def test_parse_trigger_periodic_days():
    """_parse_trigger handles 'every N day(s)' patterns."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Trigger\nEvery 1 day.\n\n## Logic\nDo stuff."
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "periodic", "interval_seconds": 86400}

    content2 = "# Test\n\n## Trigger\nEvery 3 days.\n\n## Logic\nDo stuff."
    result2 = RuleManager._parse_trigger(content2)
    assert result2 == {"type": "periodic", "interval_seconds": 3 * 86400}


def test_parse_trigger_every_hour_no_number():
    """_parse_trigger handles 'every hour' without a number (implies 1)."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Trigger\nCheck every hour.\n\n## Logic\nDo stuff."
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "periodic", "interval_seconds": 3600}


def test_parse_trigger_weekly_daily():
    """_parse_trigger handles 'weekly' and 'daily' shorthand."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Trigger\nWeekly (every Monday at 9:00 AM UTC)\n"
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "periodic", "interval_seconds": 604800}

    content2 = "# Test\n\n## Trigger\nDaily check.\n"
    result2 = RuleManager._parse_trigger(content2)
    assert result2 == {"type": "periodic", "interval_seconds": 86400}


def test_parse_trigger_section_variants():
    """_parse_trigger finds '## Triggers' (plural) and '## Trigger Schedule'."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Triggers\nEvery 10 minutes.\n\n## Logic\nDo stuff."
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "periodic", "interval_seconds": 600}

    content2 = (
        "# Test\n\n## Trigger Schedule\n**Frequency:** Every 30 minutes\n\n## Logic\nDo stuff."
    )
    result2 = RuleManager._parse_trigger(content2)
    assert result2 == {"type": "periodic", "interval_seconds": 1800}


def test_parse_trigger_inline_bold_marker():
    """_parse_trigger finds **Trigger:** inline markers as fallback."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n**Trigger:** When a task is completed.\n\n## Logic\nDo."
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "event", "event_type": "task.completed"}


def test_parse_trigger_error_agent_crash():
    """_parse_trigger handles error/crash event triggers."""
    from src.rule_manager import RuleManager

    content = (
        "# Test\n\n## Trigger\nWhen an agent crash error occurs (`error.agent_crash` event).\n"
    )
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "event", "event_type": "error.agent_crash"}


def test_parse_trigger_explicit_event_type():
    """_parse_trigger picks up backticked event types like `task.started`."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Trigger\nOn `task.started` event.\n"
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "event", "event_type": "task.started"}


# ------------------------------------------------------------------
# Duplicate rule cleanup tests
# ------------------------------------------------------------------


def test_cleanup_duplicate_rules(storage_root):
    """_cleanup_duplicate_rules removes rule-rule-* files when canonical exists."""
    from src.rule_manager import RuleManager

    # Create canonical rule in vault playbook location
    rules_dir = os.path.join(storage_root, "vault", "projects", "test-project", "playbooks")
    os.makedirs(rules_dir, exist_ok=True)

    canonical_content = (
        "---\nid: rule-restart-daemon\ntype: active\n"
        "project_id: test-project\nhooks: []\n---\n\n"
        "# Restart Daemon\n\n## Trigger\nWhen task completed.\n"
    )
    with open(os.path.join(rules_dir, "rule-restart-daemon.md"), "w") as f:
        f.write(canonical_content)

    # Create duplicate rule-rule-* file
    duplicate_content = (
        "---\nid: rule-rule-restart-daemon\ntype: active\n"
        "project_id: test-project\nhooks:\n- rule-rule-rule-restart-daemon-abc123\n---\n\n"
        "# Rule: Restart Daemon\n\n## Trigger\nWhen task completed.\n"
    )
    with open(os.path.join(rules_dir, "rule-rule-restart-daemon.md"), "w") as f:
        f.write(duplicate_content)

    mock_db = AsyncMock()
    mock_db.delete_hook = AsyncMock()
    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=0)

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    removed = asyncio.run(rm._cleanup_duplicate_rules())

    assert removed == 1
    assert not os.path.exists(os.path.join(rules_dir, "rule-rule-restart-daemon.md"))
    assert os.path.exists(os.path.join(rules_dir, "rule-restart-daemon.md"))
    # Should have cleaned up the hook from the duplicate
    mock_db.delete_hook.assert_called_with("rule-rule-rule-restart-daemon-abc123")


def test_migrate_orphan_hooks_strips_rule_prefix(storage_root):
    """migrate_orphan_hooks strips 'Rule: ' from hook names to avoid duplicates."""
    from src.models import Hook
    from src.rule_manager import RuleManager

    orphan = Hook(
        id="legacy-restart-hook",
        project_id="my-project",
        name="Rule: Restart Daemon",
        trigger='{"type": "event", "event_type": "task.completed"}',
        context_steps="[]",
        prompt_template="Restart the daemon.",
        cooldown_seconds=60,
    )

    mock_db = AsyncMock()
    mock_db.list_hooks = AsyncMock(return_value=[orphan])
    mock_db.delete_hook = AsyncMock()

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    stats = asyncio.run(rm.migrate_orphan_hooks())

    assert stats["migrated"] == 1

    # The rule file should be rule-restart-daemon.md, NOT rule-rule-restart-daemon.md
    expected_path = os.path.join(
        storage_root, "vault", "projects", "my-project", "playbooks", "rule-restart-daemon.md"
    )
    unexpected_path = os.path.join(
        storage_root,
        "vault",
        "projects",
        "my-project",
        "playbooks",
        "rule-rule-restart-daemon.md",
    )
    assert os.path.isfile(expected_path), f"Expected rule file at {expected_path}"
    assert not os.path.isfile(unexpected_path), f"Should NOT create duplicate at {unexpected_path}"
