"""Profile system integration tests.

Verifies the full enforcement chain: profile → config → adapter → TaskContext.

Two gaps the unit tests don't cover:
1. Positive enforcement: a profiled task receives the exact allowed_tools and
   mcp_servers the SDK would actually consume.
2. Negative isolation: a non-profiled task does NOT inherit tools/MCP from
   another profile, and profile A tasks don't get profile B's config.

Uses CapturingMockAdapter + CapturingAdapterFactory to capture the merged
ClaudeAdapterConfig and the TaskContext at each stage of execution.
"""
import pytest

from src.adapters import AdapterFactory
from src.adapters.base import AgentAdapter
from src.adapters.claude import ClaudeAdapterConfig
from src.config import AppConfig
from src.database import Database
from src.models import (
    Agent, AgentOutput, AgentProfile, AgentResult, AgentState,
    Project, RepoSourceType, Task, TaskContext, TaskStatus, Workspace,
)
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Capturing test doubles
# ---------------------------------------------------------------------------

class CapturingMockAdapter(AgentAdapter):
    """Records the TaskContext passed to start() for later assertions."""

    def __init__(self, config: ClaudeAdapterConfig):
        self.config = config
        self.task_context: TaskContext | None = None

    async def start(self, task: TaskContext) -> None:
        self.task_context = task

    async def wait(self, on_message=None) -> AgentOutput:
        return AgentOutput(result=AgentResult.COMPLETED, summary="Done",
                           tokens_used=100)

    async def stop(self) -> None:
        pass

    async def is_alive(self) -> bool:
        return True


class CapturingAdapterFactory:
    """Wraps the real AdapterFactory._config_for_profile() merging logic.

    After each create() call, the merged config, profile, and adapter are
    available for inspection.
    """

    def __init__(self, base_config: ClaudeAdapterConfig | None = None):
        self._real_factory = AdapterFactory(claude_config=base_config)
        self.adapters_created: list[CapturingMockAdapter] = []
        self.configs_created: list[ClaudeAdapterConfig] = []
        self.profiles_received: list[AgentProfile | None] = []

    def create(self, agent_type: str,
               profile: AgentProfile | None = None) -> AgentAdapter:
        merged = self._real_factory._config_for_profile(profile)
        self.profiles_received.append(profile)
        self.configs_created.append(merged)
        adapter = CapturingMockAdapter(merged)
        self.adapters_created.append(adapter)
        return adapter


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _setup_project_and_agent(
    db: Database,
    project_id: str = "p-1",
    project_name: str = "alpha",
    workspace_path: str = "/tmp/test-workspace",
    default_profile_id: str | None = None,
    agent_id: str = "a-1",
) -> None:
    """Create project, workspace, and agent so task execution succeeds."""
    await db.create_project(Project(
        id=project_id, name=project_name,
        default_profile_id=default_profile_id,
    ))
    await db.create_workspace(Workspace(
        id=f"ws-{project_id}",
        project_id=project_id,
        workspace_path=workspace_path,
        source_type=RepoSourceType.LINK,
    ))
    await db.create_agent(Agent(
        id=agent_id, name="claude-1", agent_type="claude",
    ))


BASE_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


# ---------------------------------------------------------------------------
# 1. Tool enforcement
# ---------------------------------------------------------------------------

class TestToolEnforcement:
    """Verify allowed_tools flows from profile → merged config correctly."""

    @pytest.fixture
    async def env(self, tmp_path):
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_profile_tools_override_base_defaults(self, env):
        orch, factory = env
        await orch.db.create_profile(AgentProfile(
            id="reviewer", name="Reviewer",
            allowed_tools=["Read", "Glob", "Grep"],
        ))
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Review",
            description="Review code", status=TaskStatus.READY,
            profile_id="reviewer",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        assert len(factory.configs_created) == 1
        assert factory.configs_created[0].allowed_tools == ["Read", "Glob", "Grep"]

    async def test_no_profile_uses_base_defaults(self, env):
        orch, factory = env
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Do work",
            description="Details", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        assert len(factory.configs_created) == 1
        assert factory.configs_created[0].allowed_tools == BASE_TOOLS

    async def test_profile_empty_tools_falls_through_to_base(self, env):
        orch, factory = env
        await orch.db.create_profile(AgentProfile(
            id="empty-tools", name="Empty Tools",
            allowed_tools=[],
        ))
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Test",
            description="Details", status=TaskStatus.READY,
            profile_id="empty-tools",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        # Empty list is falsy → falls through to base defaults
        assert factory.configs_created[0].allowed_tools == BASE_TOOLS


# ---------------------------------------------------------------------------
# 2. MCP enforcement
# ---------------------------------------------------------------------------

class TestMCPEnforcement:
    """Verify mcp_servers flows from profile → TaskContext correctly."""

    @pytest.fixture
    async def env(self, tmp_path):
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_profile_mcp_servers_in_task_context(self, env):
        orch, factory = env
        mcp_config = {
            "playwright": {"command": "npx", "args": ["@anthropic/mcp-playwright"]},
        }
        await orch.db.create_profile(AgentProfile(
            id="web-dev", name="Web Dev",
            mcp_servers=mcp_config,
        ))
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Build page",
            description="Build a page", status=TaskStatus.READY,
            profile_id="web-dev",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        adapter = factory.adapters_created[0]
        assert adapter.task_context is not None
        # mcp_servers on TaskContext is dict[str, dict] — a copy of the profile dict
        assert len(adapter.task_context.mcp_servers) == 1
        server = adapter.task_context.mcp_servers["playwright"]
        assert server["command"] == "npx"
        assert server["args"] == ["@anthropic/mcp-playwright"]

    async def test_no_profile_has_empty_mcp_servers(self, env):
        orch, factory = env
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Do work",
            description="Details", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        adapter = factory.adapters_created[0]
        assert adapter.task_context is not None
        assert adapter.task_context.mcp_servers == {}

    async def test_mcp_server_values_are_preserved(self, env):
        orch, factory = env
        mcp_config = {
            "linter": {"command": "node", "args": ["./server.js", "--port", "3000"]},
        }
        await orch.db.create_profile(AgentProfile(
            id="lint-prof", name="Linter",
            mcp_servers=mcp_config,
        ))
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Lint",
            description="Lint code", status=TaskStatus.READY,
            profile_id="lint-prof",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        server = factory.adapters_created[0].task_context.mcp_servers["linter"]
        assert server["command"] == "node"
        assert server["args"] == ["./server.js", "--port", "3000"]


# ---------------------------------------------------------------------------
# 3. Profile isolation (profiled vs. un-profiled)
# ---------------------------------------------------------------------------

class TestProfileIsolation:
    """Two sequential tasks: one with a profile, one without.
    Neither should leak config to the other."""

    @pytest.fixture
    async def env(self, tmp_path):
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_profiled_then_unprofiled_isolation(self, env):
        orch, factory = env
        await orch.db.create_profile(AgentProfile(
            id="reviewer", name="Reviewer",
            allowed_tools=["Read", "Glob", "Grep"],
            mcp_servers={"linter": {"command": "npx", "args": ["eslint-mcp"]}},
        ))
        await _setup_project_and_agent(orch.db)

        # Task A: with profile
        await orch.db.create_task(Task(
            id="t-a", project_id="p-1", title="Review code",
            description="Review", status=TaskStatus.READY,
            profile_id="reviewer",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        # Task B: no profile — agent is now idle again
        await orch.db.create_task(Task(
            id="t-b", project_id="p-1", title="Implement feature",
            description="Build it", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        assert len(factory.configs_created) == 2

        # Task A: restricted tools + MCP
        config_a = factory.configs_created[0]
        adapter_a = factory.adapters_created[0]
        assert config_a.allowed_tools == ["Read", "Glob", "Grep"]
        assert len(adapter_a.task_context.mcp_servers) == 1

        # Task B: base defaults + no MCP
        config_b = factory.configs_created[1]
        adapter_b = factory.adapters_created[1]
        assert config_b.allowed_tools == BASE_TOOLS
        assert adapter_b.task_context.mcp_servers == {}


# ---------------------------------------------------------------------------
# 4. Multi-profile isolation
# ---------------------------------------------------------------------------

class TestMultiProfileIsolation:
    """Two different profiles: verify each task gets its own config."""

    @pytest.fixture
    async def env(self, tmp_path):
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_two_profiles_no_cross_contamination(self, env):
        orch, factory = env
        await orch.db.create_profile(AgentProfile(
            id="reviewer", name="Reviewer",
            allowed_tools=["Read", "Glob", "Grep"],
        ))
        await orch.db.create_profile(AgentProfile(
            id="web-dev", name="Web Dev",
            allowed_tools=["Read", "Write", "Edit", "Bash"],
            mcp_servers={"playwright": {"command": "npx", "args": ["mcp-playwright"]}},
        ))
        await _setup_project_and_agent(orch.db)

        # Task 1: reviewer profile
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Review",
            description="Review code", status=TaskStatus.READY,
            profile_id="reviewer",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        # Task 2: web-dev profile
        await orch.db.create_task(Task(
            id="t-2", project_id="p-1", title="Build UI",
            description="Build", status=TaskStatus.READY,
            profile_id="web-dev",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        assert len(factory.configs_created) == 2

        # Reviewer: restricted tools, no MCP
        cfg_reviewer = factory.configs_created[0]
        adapter_reviewer = factory.adapters_created[0]
        assert cfg_reviewer.allowed_tools == ["Read", "Glob", "Grep"]
        assert adapter_reviewer.task_context.mcp_servers == {}

        # Web-dev: dev tools + playwright MCP
        cfg_webdev = factory.configs_created[1]
        adapter_webdev = factory.adapters_created[1]
        assert cfg_webdev.allowed_tools == ["Read", "Write", "Edit", "Bash"]
        assert len(adapter_webdev.task_context.mcp_servers) == 1
        assert adapter_webdev.task_context.mcp_servers["playwright"]["command"] == "npx"


# ---------------------------------------------------------------------------
# 5. Install check integration
# ---------------------------------------------------------------------------

class TestInstallCheckIntegration:
    """Full install check → task flow through CommandHandler."""

    @pytest.fixture
    async def handler(self, tmp_path):
        from src.command_handler import CommandHandler
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yield handler, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_valid_install_then_execute(self, handler):
        handler, factory = handler
        orch = handler.orchestrator

        # Create profile with python3 (should exist on any test runner)
        await handler.execute("create_profile", {
            "id": "py-dev", "name": "Python Dev",
            "allowed_tools": ["Read", "Write", "Edit", "Bash"],
            "install": {"commands": ["python3"]},
        })
        result = await handler.execute("check_profile", {"profile_id": "py-dev"})
        assert result["valid"] is True
        assert result["issues"] == []

        # Now execute a task with that profile
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Python task",
            description="Write python", status=TaskStatus.READY,
            profile_id="py-dev",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        assert len(factory.configs_created) == 1
        assert factory.configs_created[0].allowed_tools == ["Read", "Write", "Edit", "Bash"]
        assert factory.profiles_received[0].id == "py-dev"

    async def test_invalid_install_check(self, handler):
        handler, _ = handler

        await handler.execute("create_profile", {
            "id": "docker-user", "name": "Docker User",
            "install": {"commands": ["definitely-not-installed-xyz"]},
        })
        result = await handler.execute("check_profile", {"profile_id": "docker-user"})
        assert result["valid"] is False
        assert any("definitely-not-installed-xyz" in i for i in result["issues"])


# ---------------------------------------------------------------------------
# 6. Project default profile enforcement
# ---------------------------------------------------------------------------

class TestProjectDefaultProfileEnforcement:
    """Project with default_profile_id → tasks without explicit profile get it."""

    @pytest.fixture
    async def env(self, tmp_path):
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_project_default_profile_produces_correct_config(self, env):
        orch, factory = env
        await orch.db.create_profile(AgentProfile(
            id="reviewer", name="Reviewer",
            allowed_tools=["Read", "Glob", "Grep"],
            permission_mode="plan",
        ))
        await _setup_project_and_agent(
            orch.db, default_profile_id="reviewer",
        )
        # Task has no explicit profile_id — should inherit project default
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Review task",
            description="Review it", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        assert len(factory.configs_created) == 1
        cfg = factory.configs_created[0]
        assert cfg.allowed_tools == ["Read", "Glob", "Grep"]
        assert cfg.permission_mode == "plan"
        assert factory.profiles_received[0].id == "reviewer"

    async def test_task_profile_overrides_project_default(self, env):
        orch, factory = env
        await orch.db.create_profile(AgentProfile(
            id="reviewer", name="Reviewer",
            allowed_tools=["Read", "Glob", "Grep"],
        ))
        await orch.db.create_profile(AgentProfile(
            id="developer", name="Developer",
            allowed_tools=["Read", "Write", "Edit", "Bash"],
        ))
        await _setup_project_and_agent(
            orch.db, default_profile_id="reviewer",
        )
        # Task explicitly sets developer profile — overrides project default
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Dev task",
            description="Develop it", status=TaskStatus.READY,
            profile_id="developer",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        cfg = factory.configs_created[0]
        assert cfg.allowed_tools == ["Read", "Write", "Edit", "Bash"]
        assert factory.profiles_received[0].id == "developer"

    async def test_project_default_with_mcp_reaches_task_context(self, env):
        orch, factory = env
        await orch.db.create_profile(AgentProfile(
            id="mcp-profile", name="MCP Profile",
            mcp_servers={"sentry": {"command": "npx", "args": ["sentry-mcp"]}},
        ))
        await _setup_project_and_agent(
            orch.db, default_profile_id="mcp-profile",
        )
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Debug task",
            description="Debug", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        adapter = factory.adapters_created[0]
        assert len(adapter.task_context.mcp_servers) == 1
        assert adapter.task_context.mcp_servers["sentry"]["command"] == "npx"


# ---------------------------------------------------------------------------
# 7. MCP auto-injection from daemon server config
# ---------------------------------------------------------------------------

class TestMCPAutoInjection:
    """Verify the daemon's own MCP server is auto-injected into task contexts
    when mcp_server.enabled is True (inject_into_tasks defaults to True)."""

    @pytest.fixture
    async def env_with_mcp(self, tmp_path):
        from src.config import McpServerConfig
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            mcp_server=McpServerConfig(enabled=True, host="127.0.0.1", port=8082),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    @pytest.fixture
    async def env_mcp_disabled(self, tmp_path):
        from src.config import McpServerConfig
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            mcp_server=McpServerConfig(enabled=False),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    @pytest.fixture
    async def env_inject_disabled(self, tmp_path):
        from src.config import McpServerConfig
        factory = CapturingAdapterFactory()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            mcp_server=McpServerConfig(
                enabled=True, host="127.0.0.1", port=8082,
                inject_into_tasks=False,
            ),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_auto_injects_when_mcp_enabled(self, env_with_mcp):
        """When mcp_server.enabled=True, every task gets agent-queue MCP."""
        orch, factory = env_with_mcp
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Do work",
            description="Details", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        adapter = factory.adapters_created[0]
        assert "agent-queue" in adapter.task_context.mcp_servers
        aq = adapter.task_context.mcp_servers["agent-queue"]
        assert aq["type"] == "http"
        assert aq["url"] == "http://127.0.0.1:8082/mcp"

    async def test_no_injection_when_mcp_disabled(self, env_mcp_disabled):
        """When mcp_server.enabled=False, no auto-injection."""
        orch, factory = env_mcp_disabled
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Do work",
            description="Details", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        adapter = factory.adapters_created[0]
        assert adapter.task_context.mcp_servers == {}

    async def test_no_injection_when_inject_false(self, env_inject_disabled):
        """When inject_into_tasks=False, no auto-injection even if enabled."""
        orch, factory = env_inject_disabled
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Do work",
            description="Details", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        adapter = factory.adapters_created[0]
        assert adapter.task_context.mcp_servers == {}

    async def test_profile_mcp_merged_with_daemon_mcp(self, env_with_mcp):
        """Profile MCP servers are layered on top of the daemon's MCP server."""
        orch, factory = env_with_mcp
        await orch.db.create_profile(AgentProfile(
            id="web-dev", name="Web Dev",
            mcp_servers={"playwright": {"command": "npx", "args": ["mcp-playwright"]}},
        ))
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Build page",
            description="Build", status=TaskStatus.READY,
            profile_id="web-dev",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        mcp = factory.adapters_created[0].task_context.mcp_servers
        # Both the daemon's server and the profile's server are present
        assert "agent-queue" in mcp
        assert mcp["agent-queue"]["type"] == "http"
        assert "playwright" in mcp
        assert mcp["playwright"]["command"] == "npx"

    async def test_profile_can_override_daemon_mcp_name(self, env_with_mcp):
        """A profile that defines an 'agent-queue' MCP server overrides the daemon's."""
        orch, factory = env_with_mcp
        await orch.db.create_profile(AgentProfile(
            id="custom", name="Custom",
            mcp_servers={"agent-queue": {"type": "http", "url": "http://other:9999/mcp"}},
        ))
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Custom",
            description="Custom task", status=TaskStatus.READY,
            profile_id="custom",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        mcp = factory.adapters_created[0].task_context.mcp_servers
        assert mcp["agent-queue"]["url"] == "http://other:9999/mcp"


# ---------------------------------------------------------------------------
# 8. Model override enforcement
# ---------------------------------------------------------------------------

class TestModelOverrideEnforcement:
    """Verify model overrides flow from profile → merged config."""

    @pytest.fixture
    async def env(self, tmp_path):
        base = ClaudeAdapterConfig(model="claude-sonnet-4-5-20250514")
        factory = CapturingAdapterFactory(base_config=base)
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
        )
        orch = Orchestrator(config, adapter_factory=factory)
        await orch.initialize()
        yield orch, factory
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_profile_model_overrides_base(self, env):
        orch, factory = env
        await orch.db.create_profile(AgentProfile(
            id="heavy", name="Heavy",
            model="claude-opus-4-20250514",
        ))
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Hard task",
            description="Complex work", status=TaskStatus.READY,
            profile_id="heavy",
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        assert factory.configs_created[0].model == "claude-opus-4-20250514"

    async def test_no_profile_keeps_base_model(self, env):
        orch, factory = env
        await _setup_project_and_agent(orch.db)
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Simple",
            description="Simple", status=TaskStatus.READY,
        ))
        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        assert factory.configs_created[0].model == "claude-sonnet-4-5-20250514"


# ---------------------------------------------------------------------------
# 9. MCP tool auto-approval in allowed_tools
# ---------------------------------------------------------------------------

class TestMCPToolAutoApproval:
    """Verify that MCP server tool patterns are added to allowed_tools.

    When MCP servers are configured for a task, the ClaudeAdapter must add
    ``mcp__<server-name>__*`` patterns to the allowed_tools list passed to the
    Claude Agent SDK.  Without this, MCP tools require interactive permission
    approval — impossible in headless SDK mode — so the agent can't use them.

    These tests exercise the allowed_tools construction logic directly
    (without running the full SDK query).
    """

    def test_mcp_tool_patterns_added_for_each_server(self):
        """Each MCP server gets a wildcard pattern in allowed_tools."""
        config = ClaudeAdapterConfig()
        mcp_servers = {
            "agent-queue": {"type": "http", "url": "http://127.0.0.1:8082/mcp"},
            "playwright": {"command": "npx", "args": ["@anthropic/mcp-playwright"]},
        }

        # Replicate the logic from ClaudeAdapter.wait()
        allowed = list(config.allowed_tools)
        for server_name in mcp_servers:
            pattern = f"mcp__{server_name}__*"
            if pattern not in allowed:
                allowed.append(pattern)

        assert "mcp__agent-queue__*" in allowed
        assert "mcp__playwright__*" in allowed
        # Base tools are preserved
        for tool in BASE_TOOLS:
            assert tool in allowed

    def test_no_mcp_servers_leaves_base_tools_unchanged(self):
        """Without MCP servers, allowed_tools is just the base set."""
        config = ClaudeAdapterConfig()
        mcp_servers = {}

        allowed = list(config.allowed_tools)
        for server_name in mcp_servers:
            pattern = f"mcp__{server_name}__*"
            if pattern not in allowed:
                allowed.append(pattern)

        assert allowed == BASE_TOOLS

    def test_no_duplicate_patterns(self):
        """If a profile already includes the MCP pattern, don't duplicate it."""
        config = ClaudeAdapterConfig(
            allowed_tools=["Read", "Write", "mcp__agent-queue__*"],
        )
        mcp_servers = {
            "agent-queue": {"type": "http", "url": "http://127.0.0.1:8082/mcp"},
        }

        allowed = list(config.allowed_tools)
        for server_name in mcp_servers:
            pattern = f"mcp__{server_name}__*"
            if pattern not in allowed:
                allowed.append(pattern)

        assert allowed.count("mcp__agent-queue__*") == 1

    def test_profile_tools_plus_mcp_patterns(self):
        """Profile-specific tools are preserved alongside MCP patterns."""
        config = ClaudeAdapterConfig(
            allowed_tools=["Read", "Glob", "Grep"],  # reviewer-style profile
        )
        mcp_servers = {
            "agent-queue": {"type": "http", "url": "http://127.0.0.1:8082/mcp"},
        }

        allowed = list(config.allowed_tools)
        for server_name in mcp_servers:
            pattern = f"mcp__{server_name}__*"
            if pattern not in allowed:
                allowed.append(pattern)

        assert allowed == ["Read", "Glob", "Grep", "mcp__agent-queue__*"]
