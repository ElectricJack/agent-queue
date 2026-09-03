"""Tests for the Agent Profiles feature.

Covers:
- Database CRUD for agent_profiles table
- Profile resolution cascade (task → project → None)
- CommandHandler profile commands
- Task/project profile_id and default_profile_id
- Config loading from YAML
- Orchestrator profile sync at startup
- Profile enforcement through adapter factory (v2)
- Tool validation, install manifest, discovery (v2)
- Export/import roundtrip (v2)
"""

from pathlib import Path

import pytest

from src.config import AgentProfileConfig, AppConfig, load_config
from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    Project,
    Task,
    TaskStatus,
)
from src.orchestrator import Orchestrator
from src.profiles.parser import parse_profile
from tests.session_dispatch_helpers import (
    create_session_profile,
    create_session_project,
    drain_running_tasks,
    fake_provider,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def sample_profile():
    return AgentProfile(
        id="test-reviewer",
        name="Code Reviewer",
        description="Read-only code review agent",
        model="claude-sonnet-4-5-20250514",
        permission_mode="plan",
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        mcp_servers=["linter"],
        system_prompt_suffix="You are a code reviewer. Report findings — do not modify code.",
    )


# ---------------------------------------------------------------------------
# Database CRUD
# ---------------------------------------------------------------------------


class TestProfileDatabaseCRUD:
    async def test_create_and_get_profile(self, db, sample_profile):
        await db.create_profile(sample_profile)
        result = await db.get_profile("test-reviewer")
        assert result is not None
        assert result.id == "test-reviewer"
        assert result.name == "Code Reviewer"
        assert result.description == "Read-only code review agent"
        assert result.model == "claude-sonnet-4-5-20250514"
        assert result.permission_mode == "plan"
        assert result.allowed_tools == ["Read", "Glob", "Grep", "Bash"]
        assert result.mcp_servers == ["linter"]
        assert "do not modify code" in result.system_prompt_suffix

    async def test_get_nonexistent_profile(self, db):
        result = await db.get_profile("nonexistent")
        assert result is None

    async def test_list_profiles(self, db):
        await db.create_profile(AgentProfile(id="a", name="Alpha"))
        await db.create_profile(AgentProfile(id="b", name="Beta"))
        profiles = await db.list_profiles()
        assert len(profiles) == 2
        # Sorted by name
        assert profiles[0].name == "Alpha"
        assert profiles[1].name == "Beta"

    async def test_list_profiles_empty(self, db):
        profiles = await db.list_profiles()
        assert profiles == []

    async def test_update_profile(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.update_profile("test-reviewer", name="Senior Reviewer", model="")
        result = await db.get_profile("test-reviewer")
        assert result.name == "Senior Reviewer"
        assert result.model == ""
        # Other fields unchanged
        assert result.allowed_tools == ["Read", "Glob", "Grep", "Bash"]

    async def test_update_profile_json_fields(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.update_profile(
            "test-reviewer",
            allowed_tools=["Read", "Glob"],
            mcp_servers=["new"],
        )
        result = await db.get_profile("test-reviewer")
        assert result.allowed_tools == ["Read", "Glob"]
        assert result.mcp_servers == ["new"]

    async def test_delete_profile(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.delete_profile("test-reviewer")
        result = await db.get_profile("test-reviewer")
        assert result is None

    async def test_delete_profile_clears_task_references(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(Project(id="p-1", name="test"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Test",
                profile_id="test-reviewer",
            )
        )
        task = await db.get_task("t-1")
        assert task.profile_id == "test-reviewer"

        await db.delete_profile("test-reviewer")
        task = await db.get_task("t-1")
        assert task.profile_id is None

    async def test_delete_profile_clears_project_references(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(
            Project(
                id="p-1",
                name="test",
                default_profile_id="test-reviewer",
            )
        )
        project = await db.get_project("p-1")
        assert project.default_profile_id == "test-reviewer"

        await db.delete_profile("test-reviewer")
        project = await db.get_project("p-1")
        assert project.default_profile_id is None


# ---------------------------------------------------------------------------
# Task and Project profile_id fields
# ---------------------------------------------------------------------------


class TestTaskProfileId:
    async def test_create_task_with_profile_id(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(Project(id="p-1", name="test"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Review code",
                description="Review the PR",
                profile_id="test-reviewer",
            )
        )
        task = await db.get_task("t-1")
        assert task.profile_id == "test-reviewer"

    async def test_create_task_without_profile_id(self, db):
        await db.create_project(Project(id="p-1", name="test"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Do thing",
                description="Details",
            )
        )
        task = await db.get_task("t-1")
        assert task.profile_id is None

    async def test_update_task_profile_id(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(Project(id="p-1", name="test"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Test",
            )
        )
        await db.update_task("t-1", profile_id="test-reviewer")
        task = await db.get_task("t-1")
        assert task.profile_id == "test-reviewer"

    async def test_clear_task_profile_id(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(Project(id="p-1", name="test"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Test",
                profile_id="test-reviewer",
            )
        )
        await db.update_task("t-1", profile_id=None)
        task = await db.get_task("t-1")
        assert task.profile_id is None


class TestProjectDefaultProfileId:
    async def test_create_project_with_default_profile(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(
            Project(
                id="p-1",
                name="test",
                default_profile_id="test-reviewer",
            )
        )
        project = await db.get_project("p-1")
        assert project.default_profile_id == "test-reviewer"

    async def test_create_project_without_default_profile(self, db):
        await db.create_project(Project(id="p-1", name="test"))
        project = await db.get_project("p-1")
        assert project.default_profile_id is None

    async def test_update_project_default_profile(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(Project(id="p-1", name="test"))
        await db.update_project("p-1", default_profile_id="test-reviewer")
        project = await db.get_project("p-1")
        assert project.default_profile_id == "test-reviewer"


# ---------------------------------------------------------------------------
# Profile resolution cascade
# ---------------------------------------------------------------------------


class TestProfileResolution:
    """Test the _resolve_profile cascade: task → project → None."""

    @pytest.fixture
    async def orch(self, tmp_path):
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        o = Orchestrator(config)
        await o.initialize()
        yield o
        await o.db.close()

    async def test_resolve_task_profile(self, orch):
        """Task with profile_id → use task's profile."""
        await orch.db.create_profile(AgentProfile(id="test-reviewer", name="Reviewer"))
        await orch.db.create_project(Project(id="p-1", name="test"))
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Test",
            description="Test",
            profile_id="test-reviewer",
        )
        profile = await orch._resolve_profile(task)
        assert profile is not None
        assert profile.id == "test-reviewer"

    async def test_resolve_project_default_profile(self, orch):
        """Task without profile_id, project with default → use project's default."""
        await orch.db.create_profile(AgentProfile(id="test-reviewer", name="Reviewer"))
        await orch.db.create_project(
            Project(
                id="p-1",
                name="test",
                default_profile_id="test-reviewer",
            )
        )
        task = Task(id="t-1", project_id="p-1", title="Test", description="Test")
        profile = await orch._resolve_profile(task)
        assert profile is not None
        assert profile.id == "test-reviewer"

    async def test_resolve_no_profile(self, orch):
        """No task profile, no project default, no profiles registered → None."""
        for p in await orch.db.list_profiles():
            await orch.db.delete_profile(p.id)
        await orch.db.create_project(Project(id="p-1", name="test"))
        task = Task(id="t-1", project_id="p-1", title="Test", description="Test")
        profile = await orch._resolve_profile(task)
        assert profile is None

    @staticmethod
    async def _only_profiles(orch, *profile_ids: str) -> None:
        """Reduce the registered profile set to exactly *profile_ids*.

        ``initialize()`` seeds a shipped profile set, so pin the selector's
        input rather than asserting against whatever happens to be seeded.
        """
        keep = set(profile_ids)
        for p in await orch.db.list_profiles():
            if p.id not in keep:
                await orch.db.delete_profile(p.id)
            keep.discard(p.id)
        for pid in keep:
            await orch.db.create_profile(AgentProfile(id=pid, name=pid))

    async def test_resolve_falls_back_to_system_default(self, orch):
        """No task profile and no project default → system-wide default.

        The reconciler builds the agent row from the same third rung, so
        dispatch must agree rather than running the task profile-less.
        """
        await self._only_profiles(orch, "claude-opus")
        await orch.db.create_project(Project(id="p-1", name="test"))
        task = Task(id="t-1", project_id="p-1", title="Test", description="Test")

        profile = await orch._resolve_profile(task)

        assert profile is not None
        assert profile.id == "claude-opus"

    async def test_system_default_fallback_is_persisted(self, orch):
        """The fallback is written to the project so the choice is stable."""
        await self._only_profiles(orch, "claude-opus")
        await orch.db.create_project(Project(id="p-1", name="test"))
        task = Task(id="t-1", project_id="p-1", title="Test", description="Test")

        await orch._resolve_profile(task)

        project = await orch.db.get_project("p-1")
        assert project.default_profile_id == "claude-opus"

    async def test_retired_project_override_never_wins(self, orch):
        """Project-scoped profiles were retired — a leftover row must not resolve."""
        await self._only_profiles(orch, "claude-opus", "project:p-1:claude-opus")
        await orch.db.create_project(Project(id="p-1", name="test"))
        task = Task(id="t-1", project_id="p-1", title="Test", description="Test")

        profile = await orch._resolve_profile(task)

        assert profile.id == "claude-opus"

    async def test_task_profile_overrides_project_default(self, orch):
        """Task profile_id takes precedence over project default_profile_id."""
        await orch.db.create_profile(AgentProfile(id="test-reviewer", name="Reviewer"))
        await orch.db.create_profile(AgentProfile(id="developer", name="Developer"))
        await orch.db.create_project(
            Project(
                id="p-1",
                name="test",
                default_profile_id="developer",
            )
        )
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Test",
            description="Test",
            profile_id="test-reviewer",
        )
        profile = await orch._resolve_profile(task)
        assert profile.id == "test-reviewer"

    async def test_resolve_missing_profile_returns_none(self, orch):
        """Task references a profile_id that doesn't exist → None."""
        await orch.db.create_project(Project(id="p-1", name="test"))
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Test",
            description="Test",
            profile_id="nonexistent",
        )
        profile = await orch._resolve_profile(task)
        assert profile is None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigProfileLoading:
    def test_load_profiles_from_yaml(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
database:
  url: "sqlite:///:memory:"
discord:
  bot_token: "test-token"
  guild_id: "123456"
agent_profiles:
  test-reviewer:
    name: "Code Reviewer"
    allowed_tools:
      - Read
      - Glob
      - Grep
    system_prompt_suffix: "You are a code reviewer."
  web-dev:
    name: "Web Developer"
    model: "claude-opus-4-20250514"
    harness: claude
    claude_dangerously_skip_permissions: true
    mcp_servers:
      playwright:
        command: npx
        args: ["@anthropic/mcp-playwright"]
""")
        config = load_config(str(config_path))
        assert len(config.agent_profiles) == 2

        # Find reviewer
        reviewer = next(p for p in config.agent_profiles if p.id == "test-reviewer")
        assert reviewer.name == "Code Reviewer"
        assert reviewer.allowed_tools == ["Read", "Glob", "Grep"]
        assert reviewer.system_prompt_suffix == "You are a code reviewer."

        # Find web-dev
        webdev = next(p for p in config.agent_profiles if p.id == "web-dev")
        assert webdev.name == "Web Developer"
        assert webdev.model == "claude-opus-4-20250514"
        assert webdev.harness == "claude"
        assert webdev.claude_dangerously_skip_permissions is True
        assert "playwright" in webdev.mcp_servers

    def test_no_profiles_section(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            'database:\n  url: "sqlite:///:memory:"\n'
            "discord:\n  bot_token: test-token\n  guild_id: '123'\n"
            "scheduling:\n  rolling_window_hours: 48\n"
        )
        config = load_config(str(config_path))
        assert config.agent_profiles == []


# ---------------------------------------------------------------------------
# Orchestrator profile sync from config
# ---------------------------------------------------------------------------


class TestProfileSyncFromConfig:
    async def test_sync_creates_profiles(self, tmp_path):
        config = AppConfig(
            data_dir=str(tmp_path / "data"),
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            agent_profiles=[
                AgentProfileConfig(
                    id="test-reviewer",
                    name="Reviewer",
                    harness="codex",
                    codex_full_auto=True,
                    allowed_tools=["Read", "Glob"],
                ),
            ],
        )
        orch = Orchestrator(config)
        await orch.initialize()

        profile = await orch.db.get_profile("test-reviewer")
        assert profile is not None
        assert profile.name == "Reviewer"
        assert profile.harness == "codex"
        assert profile.codex_full_auto is True
        assert profile.allowed_tools == ["Read", "Glob"]
        await orch.db.close()

    async def test_vault_markdown_is_source_of_truth_across_restarts(self, tmp_path):
        """Vault markdown wins over subsequent YAML edits.

        First startup writes vault/agent-types/reviewer/profile.md from YAML;
        on second startup the vault-to-DB sync (profiles/sync.py) runs after
        _sync_profiles_from_config and reasserts the vault values, so YAML
        edits only take effect if the vault markdown is also updated.
        """
        config = AppConfig(
            data_dir=str(tmp_path / "data"),
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            agent_profiles=[
                AgentProfileConfig(id="test-reviewer", name="Reviewer v1"),
            ],
        )
        orch = Orchestrator(config)
        await orch.initialize()
        await orch.db.close()

        # Second startup with updated YAML but untouched vault markdown.
        config2 = AppConfig(
            data_dir=str(tmp_path / "data"),
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            agent_profiles=[
                AgentProfileConfig(id="test-reviewer", name="Reviewer v2"),
            ],
        )
        orch2 = Orchestrator(config2)
        await orch2.initialize()
        profile = await orch2.db.get_profile("test-reviewer")
        # Vault markdown, written on first startup, still says "Reviewer v1",
        # and the vault sync runs after YAML sync — so vault wins.
        assert profile.name == "Reviewer v1"
        await orch2.db.close()


# ---------------------------------------------------------------------------
# Command handler integration
# ---------------------------------------------------------------------------


class TestProfileCommands:
    @pytest.fixture
    async def handler(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yield handler
        await orch.db.close()

    async def test_create_and_list_profiles(self, handler):
        result = await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Code Reviewer",
                "allowed_tools": ["Read", "Glob", "Grep"],
            },
        )
        assert result.get("created") == "test-reviewer"

        result = await handler.execute("list_profiles", {})
        # Orchestrator initialize also installs baseline profiles (supervisor,
        # claude-code) from the default vault markdown, so the list includes
        # those alongside the one we created.
        profile_ids = {p["id"] for p in result["profiles"]}
        assert "test-reviewer" in profile_ids
        assert result["count"] == len(result["profiles"])

    def test_profile_mutation_tool_schemas_expose_autonomous_permission_opt_ins(self):
        from src.tools.definitions import _ALL_TOOL_DEFINITIONS

        definitions = {item["name"]: item for item in _ALL_TOOL_DEFINITIONS}
        for command in ("create_profile", "edit_profile"):
            properties = definitions[command]["input_schema"]["properties"]
            assert properties["harness"]["type"] == "string"
            assert properties["codex_full_auto"]["type"] == "boolean"
            assert properties["claude_dangerously_skip_permissions"]["type"] == "boolean"

    async def test_get_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        result = await handler.execute("get_profile", {"profile_id": "test-reviewer"})
        assert result["id"] == "test-reviewer"
        assert result["name"] == "Reviewer"

    async def test_edit_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        result = await handler.execute(
            "edit_profile",
            {
                "profile_id": "test-reviewer",
                "name": "Senior Reviewer",
                "allowed_tools": ["Read"],
            },
        )
        assert result.get("updated") == "test-reviewer"
        assert "name" in result["fields"]
        assert "allowed_tools" in result["fields"]

    async def test_create_edit_and_get_autonomous_permission_opt_ins(self, handler):
        result = await handler.execute(
            "create_profile",
            {
                "id": "autonomous-codex",
                "name": "Autonomous Codex",
                "harness": "codex",
                "codex_full_auto": True,
            },
        )
        assert result.get("created") == "autonomous-codex"

        detail = await handler.execute(
            "get_profile", {"profile_id": "autonomous-codex"}
        )
        assert detail["harness"] == "codex"
        assert detail["codex_full_auto"] is True
        assert detail["claude_dangerously_skip_permissions"] is False

        result = await handler.execute(
            "edit_profile",
            {"profile_id": "autonomous-codex", "codex_full_auto": False},
        )
        assert result.get("updated") == "autonomous-codex"
        detail = await handler.execute(
            "get_profile", {"profile_id": "autonomous-codex"}
        )
        assert detail["codex_full_auto"] is False

    async def test_edit_rejects_non_boolean_opt_in_without_mutating_vault(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "safe-codex",
                "name": "Safe Codex",
                "harness": "codex",
                "codex_full_auto": True,
            },
        )
        vault_path = handler._vault_profile_path("safe-codex")
        before = Path(vault_path).read_text(encoding="utf-8")

        result = await handler.execute(
            "edit_profile",
            {"profile_id": "safe-codex", "codex_full_auto": "false"},
        )

        assert "error" in result
        assert "must be a boolean" in result["error"]
        assert Path(vault_path).read_text(encoding="utf-8") == before
        profile = await handler.db.get_profile("safe-codex")
        assert profile.codex_full_auto is True

    async def test_edit_false_disables_legacy_claude_permission_alias(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "legacy-claude",
                "name": "Legacy Claude",
                "harness": "claude",
            },
        )
        vault_path = Path(handler._vault_profile_path("legacy-claude"))
        vault_path.write_text(
            "---\nid: legacy-claude\nname: Legacy Claude\n---\n"
            "## Config\n```json\n"
            '{"harness": "claude", "permission_mode": "bypassPermissions"}\n'
            "```\n",
            encoding="utf-8",
        )
        from src.profiles.sync import sync_profile_text_to_db

        await sync_profile_text_to_db(
            vault_path.read_text(encoding="utf-8"),
            handler.db,
            source_path=str(vault_path),
            fallback_id="legacy-claude",
        )

        result = await handler.execute(
            "edit_profile",
            {
                "profile_id": "legacy-claude",
                "claude_dangerously_skip_permissions": False,
            },
        )

        assert result.get("updated") == "legacy-claude"
        parsed = parse_profile(vault_path.read_text(encoding="utf-8"))
        assert "permission_mode" not in parsed.config
        assert "claude_dangerously_skip_permissions" not in parsed.config
        profile = await handler.db.get_profile("legacy-claude")
        assert profile.permission_mode == ""
        assert profile.claude_dangerously_skip_permissions is False

    async def test_delete_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        result = await handler.execute("delete_profile", {"profile_id": "test-reviewer"})
        assert result.get("deleted") == "test-reviewer"

        result = await handler.execute("get_profile", {"profile_id": "test-reviewer"})
        assert "error" in result

    async def test_create_duplicate_profile_fails(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        result = await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Another Reviewer",
            },
        )
        assert "error" in result
        assert "already exists" in result["error"]

    async def test_create_task_with_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        await handler.execute("create_project", {"name": "test"})
        projects = await handler.orchestrator.db.list_projects()
        pid = projects[0].id

        result = await handler.execute(
            "create_task",
            {
                "project_id": pid,
                "title": "Review code",
                "profile_id": "test-reviewer",
            },
        )
        assert result.get("profile_id") == "test-reviewer"

    async def test_create_task_with_invalid_profile_fails(self, handler):
        await handler.execute("create_project", {"name": "test"})
        projects = await handler.orchestrator.db.list_projects()
        pid = projects[0].id

        result = await handler.execute(
            "create_task",
            {
                "project_id": pid,
                "title": "Test",
                "profile_id": "nonexistent",
            },
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_edit_task_profile_id(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        await handler.execute("create_project", {"name": "test"})
        projects = await handler.orchestrator.db.list_projects()
        pid = projects[0].id

        result = await handler.execute(
            "create_task",
            {
                "project_id": pid,
                "title": "Test",
            },
        )
        task_id = result["created"]

        result = await handler.execute(
            "edit_task",
            {
                "task_id": task_id,
                "profile_id": "test-reviewer",
            },
        )
        assert result.get("updated") == task_id
        assert "profile_id" in result["fields"]

    async def test_edit_task_clear_profile_id(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        await handler.execute("create_project", {"name": "test"})
        projects = await handler.orchestrator.db.list_projects()
        pid = projects[0].id

        result = await handler.execute(
            "create_task",
            {
                "project_id": pid,
                "title": "Test",
                "profile_id": "test-reviewer",
            },
        )
        task_id = result["created"]

        result = await handler.execute(
            "edit_task",
            {
                "task_id": task_id,
                "profile_id": None,
            },
        )
        assert result.get("updated") == task_id

        task = await handler.orchestrator.db.get_task(task_id)
        assert task.profile_id is None

    async def test_edit_project_default_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        await handler.execute("create_project", {"name": "test"})
        projects = await handler.orchestrator.db.list_projects()
        pid = projects[0].id

        result = await handler.execute(
            "edit_project",
            {
                "project_id": pid,
                "default_profile_id": "test-reviewer",
            },
        )
        assert result.get("updated") == pid
        assert "default_profile_id" in result["fields"]

        project = await handler.orchestrator.db.get_project(pid)
        assert project.default_profile_id == "test-reviewer"

    async def test_edit_project_invalid_profile_fails(self, handler):
        await handler.execute("create_project", {"name": "test"})
        projects = await handler.orchestrator.db.list_projects()
        pid = projects[0].id

        result = await handler.execute(
            "edit_project",
            {
                "project_id": pid,
                "default_profile_id": "nonexistent",
            },
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_get_task_includes_profile_id(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Reviewer",
            },
        )
        await handler.execute("create_project", {"name": "test"})
        projects = await handler.orchestrator.db.list_projects()
        pid = projects[0].id

        result = await handler.execute(
            "create_task",
            {
                "project_id": pid,
                "title": "Test",
                "profile_id": "test-reviewer",
            },
        )
        task_id = result["created"]

        result = await handler.execute("get_task", {"task_id": task_id})
        assert result["profile_id"] == "test-reviewer"


# ---------------------------------------------------------------------------
# Profile enforcement — verify profile reaches the session launch (v2)
# ---------------------------------------------------------------------------


class TestProfileEnforcement:
    """Profiles flow from the DB through the orchestrator into the session launch.

    These once asserted on the profile handed to a runtime adapter factory.
    The runtime subsystem is gone: ``_execute_task`` resolves the profile
    (task → project default → backfill) and builds a session from it, so the
    evidence is now the launched session — the ``sessions`` row's
    ``profile_id`` and the ``AQ_PROFILE`` marker in the spec the provider
    received (``tests/session_dispatch_helpers.py``).
    """

    async def _dispatch(self, orch, *, profile_id: str | None = None):
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Review",
                description="Review code",
                status=TaskStatus.READY,
                profile_id=profile_id,
            )
        )
        await orch.run_one_cycle()
        await drain_running_tasks(orch)
        return await orch.db.get_task("t-1")

    async def _launched_profile(self, orch) -> str:
        session = await orch.db.get_session_for_task("t-1")
        assert session is not None and session.state == "running"
        spec = fake_provider(orch).starts[-1]
        assert spec.env["AQ_PROFILE"] == session.profile_id
        return session.profile_id

    async def test_dispatch_launches_session_with_task_profile(self, session_orch):
        """An explicit ``task.profile_id`` wins over the project default."""
        orch = session_orch
        await create_session_project(orch)  # default profile "claude"
        await create_session_profile(orch, "test-reviewer", allowed_tools=["Read", "Glob", "Grep"])

        task = await self._dispatch(orch, profile_id="test-reviewer")

        assert task.status == TaskStatus.IN_PROGRESS
        assert await self._launched_profile(orch) == "test-reviewer"

    async def test_dispatch_no_profile_uses_backfilled_project_default(self, session_orch):
        """A task with no profile_id in a project with no default_profile_id
        does not fall through to built-in defaults: the AgentReconciler
        backfills a system default so the task is dispatchable, and
        _resolve_profile then resolves to it.
        """
        orch = session_orch
        for existing in await orch.db.list_profiles():
            await orch.db.delete_profile(existing.id)
        await create_session_profile(orch, "developer")
        await create_session_project(orch, default_profile_id=None)

        task = await self._dispatch(orch)

        backfilled = (await orch.db.get_project("p-1")).default_profile_id
        assert backfilled == "developer"
        assert task.status == TaskStatus.IN_PROGRESS
        assert await self._launched_profile(orch) == backfilled

    async def test_dispatch_with_no_profile_anywhere_launches_no_session(self, session_orch):
        """With an empty agent_profiles table there is nothing to backfill.

        The adapter used to receive ``None`` and run on its built-in
        defaults.  A session has no such fallback — a task with no profile
        has no harness, so dispatch refuses rather than launching something
        unconfigured.
        """
        from src.scheduler import AssignAction

        orch = session_orch
        for existing in await orch.db.list_profiles():
            await orch.db.delete_profile(existing.id)
        await create_session_project(orch, default_profile_id=None)
        await orch.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Do work",
                description="Details",
                status=TaskStatus.READY,
            )
        )

        with pytest.raises(RuntimeError, match="no session harness"):
            await orch._execute_task(AssignAction("a-1", "t-1", "p-1"))

        assert (await orch.db.get_project("p-1")).default_profile_id is None
        assert await orch.db.get_session_for_task("t-1") is None
        assert fake_provider(orch).starts == []

    async def test_dispatch_project_default_profile_launched(self, session_orch):
        orch = session_orch
        await create_session_project(orch, default_profile_id="developer")

        task = await self._dispatch(orch)

        assert task.status == TaskStatus.IN_PROGRESS
        assert await self._launched_profile(orch) == "developer"


# ---------------------------------------------------------------------------
# Discovery & validation (v2)
# ---------------------------------------------------------------------------


class TestToolValidation:
    async def test_create_profile_with_valid_tools_no_warnings(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        result = await handler.execute(
            "create_profile",
            {
                "id": "valid",
                "name": "Valid Profile",
                "allowed_tools": ["Read", "Write", "Edit"],
            },
        )
        assert result.get("created") == "valid"
        assert "warnings" not in result
        await orch.db.close()

    async def test_create_profile_with_unknown_tools_has_warnings(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        result = await handler.execute(
            "create_profile",
            {
                "id": "typos",
                "name": "Typo Profile",
                "allowed_tools": ["Read", "Typo", "FakeGlob"],
            },
        )
        assert result.get("created") == "typos"
        assert "warnings" in result
        assert any("Typo" in w for w in result["warnings"])
        await orch.db.close()

    async def test_edit_profile_with_unknown_tools_has_warnings(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        await handler.execute(
            "create_profile",
            {
                "id": "test",
                "name": "Test",
            },
        )
        result = await handler.execute(
            "edit_profile",
            {
                "profile_id": "test",
                "allowed_tools": ["Read", "Oops"],
            },
        )
        assert result.get("updated") == "test"
        assert "warnings" in result
        await orch.db.close()


class TestListAvailableTools:
    async def test_list_available_tools(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        result = await handler.execute("list_available_tools", {})
        assert "tools" in result
        assert "mcp_servers" in result
        tool_names = [t["name"] for t in result["tools"]]
        assert "Read" in tool_names
        assert "Write" in tool_names
        assert len(result["mcp_servers"]) >= 1
        await orch.db.close()


# ---------------------------------------------------------------------------
# Check / install profile (v2)
# ---------------------------------------------------------------------------


class TestCheckProfile:
    async def test_check_profile_empty_manifest(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        await handler.execute(
            "create_profile",
            {
                "id": "plain",
                "name": "Plain",
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "plain"})
        assert result["profile_id"] == "plain"
        assert result["valid"] is True
        assert result["issues"] == []
        await orch.db.close()

    async def test_check_profile_missing_command(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        await handler.execute(
            "create_profile",
            {
                "id": "docker-user",
                "name": "Docker User",
                "install": {"commands": ["definitely-not-a-real-command-xyz"]},
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "docker-user"})
        assert result["profile_id"] == "docker-user"
        assert result["valid"] is False
        assert any("definitely-not-a-real-command-xyz" in i for i in result["issues"])
        await orch.db.close()

    async def test_check_nonexistent_profile(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        result = await handler.execute("check_profile", {"profile_id": "nope"})
        assert "error" in result
        await orch.db.close()


# ---------------------------------------------------------------------------
# Export / import roundtrip (v2)
# ---------------------------------------------------------------------------


class TestExportImport:
    async def test_export_profile_yaml(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        await handler.execute(
            "create_profile",
            {
                "id": "test-reviewer",
                "name": "Code Reviewer",
                "allowed_tools": ["Read", "Glob", "Grep"],
                "system_prompt_suffix": "You are a code reviewer.",
            },
        )
        result = await handler.execute("export_profile", {"profile_id": "test-reviewer"})
        assert "yaml" in result
        assert "test-reviewer" in result["yaml"]
        assert "Code Reviewer" in result["yaml"]
        await orch.db.close()

    async def test_export_import_preserves_autonomous_permission_opt_ins(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        await handler.execute(
            "create_profile",
            {
                "id": "claude-autonomous",
                "name": "Claude Autonomous",
                "harness": "claude",
                "claude_dangerously_skip_permissions": True,
            },
        )

        exported = await handler.execute(
            "export_profile", {"profile_id": "claude-autonomous"}
        )
        assert "claude_dangerously_skip_permissions: true" in exported["yaml"]
        assert "codex_full_auto" not in exported["yaml"]

        imported = await handler.execute(
            "import_profile",
            {"source": exported["yaml"], "id": "claude-autonomous-copy"},
        )
        assert imported.get("imported") is True
        profile = await orch.db.get_profile("claude-autonomous-copy")
        assert profile.harness == "claude"
        assert profile.claude_dangerously_skip_permissions is True
        assert profile.codex_full_auto is False
        await orch.db.close()

    async def test_import_rejects_non_boolean_opt_in_before_writing(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)

        result = await handler.execute(
            "import_profile",
            {
                "source": """
agent_profile:
  id: unsafe
  name: Unsafe
  harness: codex
  codex_full_auto: "false"
"""
            },
        )

        assert "error" in result
        assert "must be a boolean" in result["error"]
        assert not (tmp_path / "data" / "vault" / "agent-types" / "unsafe").exists()
        assert await orch.db.get_profile("unsafe") is None
        await orch.db.close()

    async def test_import_profile_from_yaml(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yaml_text = """
agent_profile:
  id: imported
  name: "Imported Profile"
  allowed_tools: [Read, Write]
  system_prompt_suffix: "Imported profile."
"""
        result = await handler.execute("import_profile", {"source": yaml_text})
        assert result.get("imported") is True
        assert result["name"] == "Imported Profile"

        # Verify it's in the DB
        profile = await orch.db.get_profile("imported")
        assert profile is not None
        assert profile.allowed_tools == ["Read", "Write"]
        await orch.db.close()

    async def test_export_import_roundtrip(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        await handler.execute(
            "create_profile",
            {
                "id": "original",
                "name": "Original",
                "allowed_tools": ["Read", "Glob", "Grep", "Bash"],
                "mcp_servers": {"linter": {"command": "npx", "args": ["eslint-mcp"]}},
                "system_prompt_suffix": "You are a code reviewer.",
            },
        )
        export_result = await handler.execute("export_profile", {"profile_id": "original"})
        yaml_text = export_result["yaml"]

        # Import with different ID and name
        import_result = await handler.execute(
            "import_profile",
            {
                "source": yaml_text,
                "id": "copy",
                "name": "Copy of Original",
            },
        )
        assert import_result.get("imported") is True

        copy = await orch.db.get_profile("copy")
        assert copy is not None
        assert copy.name == "Copy of Original"
        assert copy.allowed_tools == ["Read", "Glob", "Grep", "Bash"]
        assert "linter" in copy.mcp_servers
        await orch.db.close()

    async def test_import_profile_with_install_reports_readiness(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yaml_text = """
agent_profile:
  id: with-deps
  name: "With Deps"
  install:
    commands: ["definitely-not-a-real-command-xyz"]
"""
        result = await handler.execute("import_profile", {"source": yaml_text})
        assert result.get("imported") is True
        # Should report readiness check since install has commands
        assert result.get("ready") is False or "manual" in result
        await orch.db.close()

    async def test_import_duplicate_fails_without_overwrite(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yaml_text = """
agent_profile:
  id: dupe
  name: "Dupe"
"""
        await handler.execute("import_profile", {"source": yaml_text})
        result = await handler.execute("import_profile", {"source": yaml_text})
        assert "error" in result
        assert "already exists" in result["error"]
        await orch.db.close()

    async def test_import_with_overwrite(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yaml_text = """
agent_profile:
  id: dupe
  name: "Version 1"
"""
        await handler.execute("import_profile", {"source": yaml_text})
        yaml_text2 = """
agent_profile:
  id: dupe
  name: "Version 2"
"""
        result = await handler.execute(
            "import_profile",
            {
                "source": yaml_text2,
                "overwrite": True,
            },
        )
        assert result.get("imported") is True
        profile = await orch.db.get_profile("dupe")
        assert profile.name == "Version 2"
        await orch.db.close()


# ---------------------------------------------------------------------------
# mcp_servers shape over the typed API (regression)
# ---------------------------------------------------------------------------


class TestProfileMcpServersShape:
    """``mcp_servers`` is a ``list[str]`` of registry names on every edit path.

    The dashboard's profile drawer sends one payload for both the global and
    the project-scoped route.  The global ``edit_profile``/``create_profile``
    request models used to declare ``mcp_servers`` as a legacy ``name ->
    {command, args}`` object, so saving a system profile with no servers
    selected was rejected with ``422 dict_type`` before the handler ever ran.
    """

    @pytest.fixture
    async def handler(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yield handler
        await orch.db.close()

    def _api(self, handler):
        from fastapi import FastAPI

        from src.api.auth import LOCAL_SCOPE
        from src.api.codegen import build_category_routers
        from src.api.dependencies import get_command_handler

        app = FastAPI()
        for router in build_category_routers():
            if router.prefix == "/api/agent":
                app.include_router(router)
        app.dependency_overrides[get_command_handler] = lambda: handler

        @app.middleware("http")
        async def bind_scope(request, call_next):
            request.state.scope = LOCAL_SCOPE
            return await call_next(request)

        return app

    async def _client(self, handler):
        import httpx

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._api(handler)), base_url="http://test"
        )

    async def test_global_edit_accepts_an_empty_server_list(self, handler):
        created = await handler.execute(
            "create_profile",
            {"id": "mcp-shape-supervisor", "name": "Supervisor", "mcp_servers": ["playwright"]},
        )
        assert created.get("created") == "mcp-shape-supervisor", created
        assert (await handler.db.get_profile("mcp-shape-supervisor")).mcp_servers == ["playwright"]
        async with await self._client(handler) as client:
            resp = await client.post(
                "/api/agent/edit-profile",
                json={
                    "profile_id": "mcp-shape-supervisor",
                    "name": "Supervisor",
                    "allowed_tools": ["Read"],
                    "mcp_servers": [],
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["updated"] == "mcp-shape-supervisor"
        profile = await handler.db.get_profile("mcp-shape-supervisor")
        assert profile.mcp_servers == []

    async def test_global_edit_accepts_a_list_of_server_names(self, handler):
        await handler.execute(
            "create_profile", {"id": "mcp-shape-supervisor", "name": "Supervisor"}
        )
        async with await self._client(handler) as client:
            resp = await client.post(
                "/api/agent/edit-profile",
                json={
                    "profile_id": "mcp-shape-supervisor",
                    "mcp_servers": ["playwright", "github"],
                },
            )
        assert resp.status_code == 200, resp.text
        profile = await handler.db.get_profile("mcp-shape-supervisor")
        assert profile.mcp_servers == ["playwright", "github"]

    async def test_global_create_accepts_a_list_of_server_names(self, handler):
        async with await self._client(handler) as client:
            resp = await client.post(
                "/api/agent/create-profile",
                json={
                    "id": "mcp-shape-reviewer",
                    "name": "Reviewer",
                    "mcp_servers": ["playwright"],
                },
            )
        assert resp.status_code == 200, resp.text
        profile = await handler.db.get_profile("mcp-shape-reviewer")
        assert profile.mcp_servers == ["playwright"]

    async def test_project_scoped_profile_routes_are_gone(self, handler):
        """Project-scoped profile CRUD was retired with the concept itself."""
        assert await handler.execute(
            "create_project_profile", {"project_id": "proj", "agent_type": "coding"}
        ) == {"error": "Unknown command: create_project_profile"}
        async with await self._client(handler) as client:
            for route in (
                "/api/agent/create-project-profile",
                "/api/agent/edit-project-profile",
                "/api/agent/delete-project-profile",
                "/api/agent/list-project-profiles",
            ):
                resp = await client.post(route, json={"project_id": "proj", "agent_type": "coding"})
                assert resp.status_code == 404, f"{route} is still routed"

    async def test_legacy_inline_mapping_is_reduced_to_its_keys(self, handler):
        """Older MCP callers still send the pre-registry inline config dict."""
        await handler.execute(
            "create_profile", {"id": "mcp-shape-supervisor", "name": "Supervisor"}
        )
        result = await handler.execute(
            "edit_profile",
            {
                "profile_id": "mcp-shape-supervisor",
                "name": "Supervisor",
                "mcp_servers": {"playwright": {"command": "npx", "args": ["playwright-mcp"]}},
            },
        )
        assert result.get("updated") == "mcp-shape-supervisor"
        profile = await handler.db.get_profile("mcp-shape-supervisor")
        assert profile.mcp_servers == ["playwright"]

    async def test_edit_profile_writes_default_class(self, handler):
        await handler.execute("create_profile", {"id": "coder", "name": "Coder"})
        async with await self._client(handler) as client:
            resp = await client.post(
                "/api/agent/edit-profile",
                json={"profile_id": "coder", "default_class": "standard-medium"},
            )
        assert resp.status_code == 200, resp.text
        profile = await handler.db.get_profile("coder")
        assert profile.default_class == "standard-medium"

    async def test_edit_profile_writes_install(self, handler):
        await handler.execute("create_profile", {"id": "coder", "name": "Coder"})
        async with await self._client(handler) as client:
            resp = await client.post(
                "/api/agent/edit-profile",
                json={"profile_id": "coder", "install": {"npm": ["eslint-mcp"]}},
            )
        assert resp.status_code == 200, resp.text
        profile = await handler.db.get_profile("coder")
        assert profile.install == {"npm": ["eslint-mcp"]}
