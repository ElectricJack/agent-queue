"""Tests for the Agent Profiles feature.

Covers:
- Database CRUD for agent_profiles table
- Profile resolution cascade (task → project → None)
- AdapterFactory._config_for_profile() merging
- CommandHandler profile commands
- Task/project profile_id and default_profile_id
- Config loading from YAML
- Orchestrator profile sync at startup
- Profile enforcement through adapter factory (v2)
- Tool validation, install manifest, discovery (v2)
- Export/import roundtrip (v2)
"""

import pytest

from src.adapters import AdapterFactory
from src.adapters.base import AgentAdapter
from src.adapters.claude import ClaudeAdapterConfig
from src.config import AppConfig, AgentProfileConfig, load_config
from src.database import Database
from src.known_tools import validate_tool_names
from src.models import (
    Agent,
    AgentOutput,
    AgentProfile,
    AgentResult,
    AgentState,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def sample_profile():
    return AgentProfile(
        id="reviewer",
        name="Code Reviewer",
        description="Read-only code review agent",
        model="claude-sonnet-4-5-20250514",
        permission_mode="plan",
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        mcp_servers={"linter": {"command": "npx", "args": ["eslint-mcp"]}},
        system_prompt_suffix="You are a code reviewer. Report findings — do not modify code.",
    )


# ---------------------------------------------------------------------------
# Database CRUD
# ---------------------------------------------------------------------------


class TestProfileDatabaseCRUD:
    async def test_create_and_get_profile(self, db, sample_profile):
        await db.create_profile(sample_profile)
        result = await db.get_profile("reviewer")
        assert result is not None
        assert result.id == "reviewer"
        assert result.name == "Code Reviewer"
        assert result.description == "Read-only code review agent"
        assert result.model == "claude-sonnet-4-5-20250514"
        assert result.permission_mode == "plan"
        assert result.allowed_tools == ["Read", "Glob", "Grep", "Bash"]
        assert result.mcp_servers == {"linter": {"command": "npx", "args": ["eslint-mcp"]}}
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
        await db.update_profile("reviewer", name="Senior Reviewer", model="")
        result = await db.get_profile("reviewer")
        assert result.name == "Senior Reviewer"
        assert result.model == ""
        # Other fields unchanged
        assert result.allowed_tools == ["Read", "Glob", "Grep", "Bash"]

    async def test_update_profile_json_fields(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.update_profile(
            "reviewer",
            allowed_tools=["Read", "Glob"],
            mcp_servers={"new": {"command": "test"}},
        )
        result = await db.get_profile("reviewer")
        assert result.allowed_tools == ["Read", "Glob"]
        assert result.mcp_servers == {"new": {"command": "test"}}

    async def test_delete_profile(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.delete_profile("reviewer")
        result = await db.get_profile("reviewer")
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
                profile_id="reviewer",
            )
        )
        task = await db.get_task("t-1")
        assert task.profile_id == "reviewer"

        await db.delete_profile("reviewer")
        task = await db.get_task("t-1")
        assert task.profile_id is None

    async def test_delete_profile_clears_project_references(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(
            Project(
                id="p-1",
                name="test",
                default_profile_id="reviewer",
            )
        )
        project = await db.get_project("p-1")
        assert project.default_profile_id == "reviewer"

        await db.delete_profile("reviewer")
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
                profile_id="reviewer",
            )
        )
        task = await db.get_task("t-1")
        assert task.profile_id == "reviewer"

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
        await db.update_task("t-1", profile_id="reviewer")
        task = await db.get_task("t-1")
        assert task.profile_id == "reviewer"

    async def test_clear_task_profile_id(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(Project(id="p-1", name="test"))
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test",
                description="Test",
                profile_id="reviewer",
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
                default_profile_id="reviewer",
            )
        )
        project = await db.get_project("p-1")
        assert project.default_profile_id == "reviewer"

    async def test_create_project_without_default_profile(self, db):
        await db.create_project(Project(id="p-1", name="test"))
        project = await db.get_project("p-1")
        assert project.default_profile_id is None

    async def test_update_project_default_profile(self, db, sample_profile):
        await db.create_profile(sample_profile)
        await db.create_project(Project(id="p-1", name="test"))
        await db.update_project("p-1", default_profile_id="reviewer")
        project = await db.get_project("p-1")
        assert project.default_profile_id == "reviewer"


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
        await orch.db.create_profile(AgentProfile(id="reviewer", name="Reviewer"))
        await orch.db.create_project(Project(id="p-1", name="test"))
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Test",
            description="Test",
            profile_id="reviewer",
        )
        profile = await orch._resolve_profile(task)
        assert profile is not None
        assert profile.id == "reviewer"

    async def test_resolve_project_default_profile(self, orch):
        """Task without profile_id, project with default → use project's default."""
        await orch.db.create_profile(AgentProfile(id="reviewer", name="Reviewer"))
        await orch.db.create_project(
            Project(
                id="p-1",
                name="test",
                default_profile_id="reviewer",
            )
        )
        task = Task(id="t-1", project_id="p-1", title="Test", description="Test")
        profile = await orch._resolve_profile(task)
        assert profile is not None
        assert profile.id == "reviewer"

    async def test_resolve_no_profile(self, orch):
        """Task without profile_id, project without default → None."""
        await orch.db.create_project(Project(id="p-1", name="test"))
        task = Task(id="t-1", project_id="p-1", title="Test", description="Test")
        profile = await orch._resolve_profile(task)
        assert profile is None

    async def test_task_profile_overrides_project_default(self, orch):
        """Task profile_id takes precedence over project default_profile_id."""
        await orch.db.create_profile(AgentProfile(id="reviewer", name="Reviewer"))
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
            profile_id="reviewer",
        )
        profile = await orch._resolve_profile(task)
        assert profile.id == "reviewer"

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
# AdapterFactory._config_for_profile() merging
# ---------------------------------------------------------------------------


class TestConfigForProfile:
    def test_no_profile_returns_base_config(self):
        base = ClaudeAdapterConfig(
            model="claude-sonnet-4-5-20250514",
            permission_mode="acceptEdits",
            allowed_tools=["Read", "Write", "Edit", "Bash"],
        )
        factory = AdapterFactory(claude_config=base)
        result = factory._config_for_profile(None)
        assert result is base

    def test_profile_overrides_model(self):
        base = ClaudeAdapterConfig(model="claude-sonnet-4-5-20250514")
        factory = AdapterFactory(claude_config=base)
        profile = AgentProfile(id="test", name="Test", model="claude-opus-4-20250514")
        result = factory._config_for_profile(profile)
        assert result.model == "claude-opus-4-20250514"

    def test_profile_empty_model_falls_through(self):
        base = ClaudeAdapterConfig(model="claude-sonnet-4-5-20250514")
        factory = AdapterFactory(claude_config=base)
        profile = AgentProfile(id="test", name="Test", model="")
        result = factory._config_for_profile(profile)
        assert result.model == "claude-sonnet-4-5-20250514"

    def test_profile_overrides_allowed_tools(self):
        base = ClaudeAdapterConfig(
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        )
        factory = AdapterFactory(claude_config=base)
        profile = AgentProfile(
            id="reviewer",
            name="Reviewer",
            allowed_tools=["Read", "Glob", "Grep"],
        )
        result = factory._config_for_profile(profile)
        assert result.allowed_tools == ["Read", "Glob", "Grep"]

    def test_profile_empty_tools_falls_through(self):
        base = ClaudeAdapterConfig(
            allowed_tools=["Read", "Write", "Edit"],
        )
        factory = AdapterFactory(claude_config=base)
        profile = AgentProfile(id="test", name="Test", allowed_tools=[])
        result = factory._config_for_profile(profile)
        assert result.allowed_tools == ["Read", "Write", "Edit"]

    def test_profile_overrides_permission_mode(self):
        base = ClaudeAdapterConfig(permission_mode="acceptEdits")
        factory = AdapterFactory(claude_config=base)
        profile = AgentProfile(id="test", name="Test", permission_mode="plan")
        result = factory._config_for_profile(profile)
        assert result.permission_mode == "plan"

    def test_full_override(self):
        base = ClaudeAdapterConfig(
            model="claude-sonnet-4-5-20250514",
            permission_mode="acceptEdits",
            allowed_tools=["Read", "Write", "Edit", "Bash"],
        )
        factory = AdapterFactory(claude_config=base)
        profile = AgentProfile(
            id="reviewer",
            name="Reviewer",
            model="claude-opus-4-20250514",
            permission_mode="plan",
            allowed_tools=["Read", "Glob"],
        )
        result = factory._config_for_profile(profile)
        assert result.model == "claude-opus-4-20250514"
        assert result.permission_mode == "plan"
        assert result.allowed_tools == ["Read", "Glob"]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigProfileLoading:
    def test_load_profiles_from_yaml(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
discord:
  bot_token: "test-token"
  guild_id: "123456"
agent_profiles:
  reviewer:
    name: "Code Reviewer"
    allowed_tools:
      - Read
      - Glob
      - Grep
    system_prompt_suffix: "You are a code reviewer."
  web-dev:
    name: "Web Developer"
    model: "claude-opus-4-20250514"
    mcp_servers:
      playwright:
        command: npx
        args: ["@anthropic/mcp-playwright"]
""")
        config = load_config(str(config_path))
        assert len(config.agent_profiles) == 2

        # Find reviewer
        reviewer = next(p for p in config.agent_profiles if p.id == "reviewer")
        assert reviewer.name == "Code Reviewer"
        assert reviewer.allowed_tools == ["Read", "Glob", "Grep"]
        assert reviewer.system_prompt_suffix == "You are a code reviewer."

        # Find web-dev
        webdev = next(p for p in config.agent_profiles if p.id == "web-dev")
        assert webdev.name == "Web Developer"
        assert webdev.model == "claude-opus-4-20250514"
        assert "playwright" in webdev.mcp_servers

    def test_no_profiles_section(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "discord:\n  bot_token: test-token\n  guild_id: '123'\nscheduling:\n  rolling_window_hours: 48\n"
        )
        config = load_config(str(config_path))
        assert config.agent_profiles == []


# ---------------------------------------------------------------------------
# Orchestrator profile sync from config
# ---------------------------------------------------------------------------


class TestProfileSyncFromConfig:
    async def test_sync_creates_profiles(self, tmp_path):
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            agent_profiles=[
                AgentProfileConfig(
                    id="reviewer",
                    name="Reviewer",
                    allowed_tools=["Read", "Glob"],
                ),
            ],
        )
        orch = Orchestrator(config)
        await orch.initialize()

        profile = await orch.db.get_profile("reviewer")
        assert profile is not None
        assert profile.name == "Reviewer"
        assert profile.allowed_tools == ["Read", "Glob"]
        await orch.db.close()

    async def test_sync_updates_existing_profiles(self, tmp_path):
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            agent_profiles=[
                AgentProfileConfig(id="reviewer", name="Reviewer v1"),
            ],
        )
        orch = Orchestrator(config)
        await orch.initialize()
        await orch.db.close()

        # Second startup with updated profile
        config2 = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            agent_profiles=[
                AgentProfileConfig(id="reviewer", name="Reviewer v2"),
            ],
        )
        orch2 = Orchestrator(config2)
        await orch2.initialize()
        profile = await orch2.db.get_profile("reviewer")
        assert profile.name == "Reviewer v2"
        await orch2.db.close()


# ---------------------------------------------------------------------------
# Command handler integration
# ---------------------------------------------------------------------------


class TestProfileCommands:
    @pytest.fixture
    async def handler(self, tmp_path):
        from src.command_handler import CommandHandler

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
                "id": "reviewer",
                "name": "Code Reviewer",
                "allowed_tools": ["Read", "Glob", "Grep"],
            },
        )
        assert result.get("created") == "reviewer"

        result = await handler.execute("list_profiles", {})
        assert result["count"] == 1
        assert result["profiles"][0]["id"] == "reviewer"

    async def test_get_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "reviewer",
                "name": "Reviewer",
            },
        )
        result = await handler.execute("get_profile", {"profile_id": "reviewer"})
        assert result["id"] == "reviewer"
        assert result["name"] == "Reviewer"

    async def test_edit_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "reviewer",
                "name": "Reviewer",
            },
        )
        result = await handler.execute(
            "edit_profile",
            {
                "profile_id": "reviewer",
                "name": "Senior Reviewer",
                "allowed_tools": ["Read"],
            },
        )
        assert result.get("updated") == "reviewer"
        assert "name" in result["fields"]
        assert "allowed_tools" in result["fields"]

    async def test_delete_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "reviewer",
                "name": "Reviewer",
            },
        )
        result = await handler.execute("delete_profile", {"profile_id": "reviewer"})
        assert result.get("deleted") == "reviewer"

        result = await handler.execute("get_profile", {"profile_id": "reviewer"})
        assert "error" in result

    async def test_create_duplicate_profile_fails(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "reviewer",
                "name": "Reviewer",
            },
        )
        result = await handler.execute(
            "create_profile",
            {
                "id": "reviewer",
                "name": "Another Reviewer",
            },
        )
        assert "error" in result
        assert "already exists" in result["error"]

    async def test_create_task_with_profile(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "reviewer",
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
                "profile_id": "reviewer",
            },
        )
        assert result.get("profile_id") == "reviewer"

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
                "id": "reviewer",
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
                "profile_id": "reviewer",
            },
        )
        assert result.get("updated") == task_id
        assert "profile_id" in result["fields"]

    async def test_edit_task_clear_profile_id(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "reviewer",
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
                "profile_id": "reviewer",
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
                "id": "reviewer",
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
                "default_profile_id": "reviewer",
            },
        )
        assert result.get("updated") == pid
        assert "default_profile_id" in result["fields"]

        project = await handler.orchestrator.db.get_project(pid)
        assert project.default_profile_id == "reviewer"

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
                "id": "reviewer",
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
                "profile_id": "reviewer",
            },
        )
        task_id = result["created"]

        result = await handler.execute("get_task", {"task_id": task_id})
        assert result["profile_id"] == "reviewer"


# ---------------------------------------------------------------------------
# Profile enforcement — verify profile reaches the adapter factory (v2)
# ---------------------------------------------------------------------------


class MockAdapter(AgentAdapter):
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000):
        self._result = result
        self._tokens = tokens

    async def start(self, task):
        pass

    async def wait(self, on_message=None):
        return AgentOutput(result=self._result, summary="Done", tokens_used=self._tokens)

    async def stop(self):
        pass

    async def is_alive(self):
        return True


class MockAdapterFactory:
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000):
        self.result = result
        self.tokens = tokens
        self.last_profile = None
        self.create_calls = []

    def create(self, agent_type: str, profile=None) -> AgentAdapter:
        self.last_profile = profile
        self.create_calls.append({"agent_type": agent_type, "profile": profile})
        return MockAdapter(result=self.result, tokens=self.tokens)


async def _create_project_with_workspace(
    db,
    project_id: str = "p-1",
    name: str = "alpha",
    workspace_path: str = "/tmp/test-workspace",
    default_profile_id: str | None = None,
) -> None:
    """Create a project and an associated workspace so task execution succeeds."""
    await db.create_project(
        Project(
            id=project_id,
            name=name,
            default_profile_id=default_profile_id,
        )
    )
    await db.create_workspace(
        Workspace(
            id=f"ws-{project_id}",
            project_id=project_id,
            workspace_path=workspace_path,
            source_type=RepoSourceType.LINK,
        )
    )


class TestProfileEnforcement:
    """Verify profiles flow from DB through orchestrator to adapter factory."""

    @pytest.fixture
    async def setup(self, tmp_path):
        factory = MockAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        if orch._running_tasks:
            import asyncio

            await asyncio.gather(*orch._running_tasks.values(), return_exceptions=True)
            orch._running_tasks.clear()
        await orch.shutdown()

    async def test_execute_task_passes_profile_to_adapter_factory(self, setup):
        orch, factory = setup
        await orch.db.create_profile(
            AgentProfile(
                id="reviewer",
                name="Reviewer",
                allowed_tools=["Read", "Glob", "Grep"],
            )
        )
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(
            Agent(
                id="a-1",
                name="claude-1",
                agent_type="claude",
            )
        )
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Review",
                description="Review code",
                status=TaskStatus.READY,
                profile_id="reviewer",
            )
        )
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()
        assert factory.last_profile is not None
        assert factory.last_profile.id == "reviewer"

    async def test_execute_task_no_profile_passes_none(self, setup):
        orch, factory = setup
        await _create_project_with_workspace(orch.db)
        await orch.db.create_agent(
            Agent(
                id="a-1",
                name="claude-1",
                agent_type="claude",
            )
        )
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Do work",
                description="Details",
                status=TaskStatus.READY,
            )
        )
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()
        assert factory.last_profile is None

    async def test_execute_task_project_default_profile_passed(self, setup):
        orch, factory = setup
        await orch.db.create_profile(
            AgentProfile(
                id="developer",
                name="Developer",
                allowed_tools=["Read", "Write", "Edit", "Bash"],
            )
        )
        await _create_project_with_workspace(
            orch.db,
            default_profile_id="developer",
        )
        await orch.db.create_agent(
            Agent(
                id="a-1",
                name="claude-1",
                agent_type="claude",
            )
        )
        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Build feature",
                description="Build it",
                status=TaskStatus.READY,
            )
        )
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()
        assert factory.last_profile is not None
        assert factory.last_profile.id == "developer"


# ---------------------------------------------------------------------------
# Discovery & validation (v2)
# ---------------------------------------------------------------------------


class TestToolValidation:
    async def test_create_profile_with_valid_tools_no_warnings(self, tmp_path):
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
                "id": "reviewer",
                "name": "Code Reviewer",
                "allowed_tools": ["Read", "Glob", "Grep"],
                "system_prompt_suffix": "You are a code reviewer.",
            },
        )
        result = await handler.execute("export_profile", {"profile_id": "reviewer"})
        assert "yaml" in result
        assert "reviewer" in result["yaml"]
        assert "Code Reviewer" in result["yaml"]
        await orch.db.close()

    async def test_import_profile_from_yaml(self, tmp_path):
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
        from src.command_handler import CommandHandler

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
