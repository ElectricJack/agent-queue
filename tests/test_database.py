import pytest
from src.database import Database
from src.models import (
    Project,
    Task,
    Agent,
    TaskStatus,
    AgentState,
    RepoSourceType,
    Workspace,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


class TestProjectCRUD:
    async def test_create_and_get_project(self, db):
        project = Project(id="p-1", name="alpha", credit_weight=3.0)
        await db.create_project(project)
        result = await db.get_project("p-1")
        assert result.name == "alpha"
        assert result.credit_weight == 3.0

    async def test_list_projects(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        projects = await db.list_projects()
        assert len(projects) == 2

    async def test_update_project(self, db):
        await db.create_project(Project(id="p-1", name="alpha", credit_weight=1.0))
        await db.update_project("p-1", credit_weight=5.0)
        result = await db.get_project("p-1")
        assert result.credit_weight == 5.0

    async def test_get_nonexistent_project_returns_none(self, db):
        result = await db.get_project("nope")
        assert result is None


class TestTaskCRUD:
    async def test_create_and_get_task(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        task = Task(id="t-1", project_id="p-1", title="Do thing", description="Details")
        await db.create_task(task)
        result = await db.get_task("t-1")
        assert result.title == "Do thing"
        assert result.status == TaskStatus.DEFINED

    async def test_update_task_status(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="X", description="Y"))
        await db.update_task("t-1", status=TaskStatus.READY)
        result = await db.get_task("t-1")
        assert result.status == TaskStatus.READY

    async def test_list_tasks_by_project(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))
        tasks = await db.list_tasks(project_id="p-1")
        assert len(tasks) == 1
        assert tasks[0].id == "t-1"

    async def test_list_tasks_by_status(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A", description="D", status=TaskStatus.READY)
        )
        await db.create_task(
            Task(id="t-2", project_id="p-1", title="B", description="D", status=TaskStatus.DEFINED)
        )
        tasks = await db.list_tasks(project_id="p-1", status=TaskStatus.READY)
        assert len(tasks) == 1
        assert tasks[0].id == "t-1"

    async def test_get_subtasks(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="Parent", description="D"))
        await db.create_task(
            Task(id="t-2", project_id="p-1", title="Child", description="D", parent_task_id="t-1")
        )
        subtasks = await db.get_subtasks("t-1")
        assert len(subtasks) == 1
        assert subtasks[0].id == "t-2"


class TestTaskDependencies:
    async def test_add_and_get_dependencies(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-1", title="B", description="D"))
        await db.add_dependency("t-2", depends_on="t-1")
        deps = await db.get_dependencies("t-2")
        assert deps == {"t-1"}

    async def test_check_dependencies_met(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A", description="D", status=TaskStatus.DEFINED)
        )
        await db.create_task(Task(id="t-2", project_id="p-1", title="B", description="D"))
        await db.add_dependency("t-2", depends_on="t-1")
        assert not await db.are_dependencies_met("t-2")

        await db.update_task("t-1", status=TaskStatus.COMPLETED)
        assert await db.are_dependencies_met("t-2")


class TestTaskMetadata:
    async def test_set_and_get(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.set_task_meta("t-1", "session_id", "sess-abc123")
        assert await db.get_task_meta("t-1", "session_id") == "sess-abc123"

    async def test_get_missing_key_returns_none(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        assert await db.get_task_meta("t-1", "nonexistent") is None

    async def test_upsert_overwrites(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.set_task_meta("t-1", "session_id", "first")
        await db.set_task_meta("t-1", "session_id", "second")
        assert await db.get_task_meta("t-1", "session_id") == "second"

    async def test_json_values(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.set_task_meta("t-1", "config", {"retries": 3, "timeout": 60})
        result = await db.get_task_meta("t-1", "config")
        assert result == {"retries": 3, "timeout": 60}

    async def test_get_all(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.set_task_meta("t-1", "key1", "val1")
        await db.set_task_meta("t-1", "key2", 42)
        result = await db.get_all_task_meta("t-1")
        assert result == {"key1": "val1", "key2": 42}

    async def test_delete(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.set_task_meta("t-1", "key1", "val1")
        await db.delete_task_meta("t-1", "key1")
        assert await db.get_task_meta("t-1", "key1") is None

    async def test_isolated_between_tasks(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-1", title="B", description="D"))
        await db.set_task_meta("t-1", "session_id", "sess-1")
        await db.set_task_meta("t-2", "session_id", "sess-2")
        assert await db.get_task_meta("t-1", "session_id") == "sess-1"
        assert await db.get_task_meta("t-2", "session_id") == "sess-2"

    async def test_delete_task_cleans_up_metadata(self, db):
        """Deleting a task must also remove its task_metadata rows."""
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.set_task_meta("t-1", "session_id", "sess-abc")
        await db.set_task_meta("t-1", "workspace", "/tmp/ws")
        # Delete the task — metadata should be cleaned up
        await db.delete_task("t-1")
        assert await db.get_task("t-1") is None
        # Verify no orphaned metadata remains (would cause FK errors)
        assert await db.get_all_task_meta("t-1") == {}


class TestAgentCRUD:
    async def test_create_and_get_agent(self, db):
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        await db.create_agent(agent)
        result = await db.get_agent("a-1")
        assert result.name == "claude-1"
        assert result.state == AgentState.IDLE

    async def test_update_agent_state(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.update_agent("a-1", state=AgentState.BUSY, current_task_id="t-1")
        result = await db.get_agent("a-1")
        assert result.state == AgentState.BUSY
        assert result.current_task_id == "t-1"

    async def test_list_idle_agents(self, db):
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_agent(
            Agent(id="a-2", name="claude-2", agent_type="claude", state=AgentState.BUSY)
        )
        idle = await db.list_agents(state=AgentState.IDLE)
        assert len(idle) == 1
        assert idle[0].id == "a-1"


class TestTokenLedger:
    async def test_record_and_sum_tokens(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.record_token_usage("p-1", "a-1", "t-1", 5000)
        await db.record_token_usage("p-1", "a-1", "t-1", 3000)
        total = await db.get_project_token_usage("p-1")
        assert total == 8000


class TestEvents:
    async def test_log_and_retrieve_events(self, db):
        await db.log_event("task_created", project_id="p-1", task_id="t-1")
        events = await db.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "task_created"


class TestAtomicTransition:
    async def test_atomic_task_agent_update(self, db):
        """Task and agent state update atomically."""
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A", description="D", status=TaskStatus.READY)
        )
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.assign_task_to_agent("t-1", "a-1")
        task = await db.get_task("t-1")
        agent = await db.get_agent("a-1")
        assert task.status == TaskStatus.ASSIGNED
        assert task.assigned_agent_id == "a-1"
        assert agent.state == AgentState.BUSY
        assert agent.current_task_id == "t-1"


class TestWorkspaces:
    async def test_create_and_get_workspace(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/workspace",
                source_type=RepoSourceType.LINK,
            )
        )
        ws = await db.get_workspace("ws-1")
        assert ws is not None
        assert ws.workspace_path == "/tmp/workspace"
        assert ws.project_id == "p-1"
        assert ws.source_type == RepoSourceType.LINK

    async def test_get_nonexistent_workspace_returns_none(self, db):
        ws = await db.get_workspace("no-ws")
        assert ws is None

    async def test_list_workspaces_by_project(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-2",
                workspace_path="/tmp/ws2",
                source_type=RepoSourceType.CLONE,
            )
        )
        workspaces = await db.list_workspaces(project_id="p-1")
        assert len(workspaces) == 1
        assert workspaces[0].id == "ws-1"

    async def test_delete_workspace(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.delete_workspace("ws-1")
        ws = await db.get_workspace("ws-1")
        assert ws is None

    async def test_acquire_workspace(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.LINK,
            )
        )
        ws = await db.acquire_workspace("p-1", "a-1", "t-1")
        assert ws is not None
        assert ws.locked_by_agent_id == "a-1"
        assert ws.locked_by_task_id == "t-1"

    async def test_acquire_workspace_none_available(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="claude-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-1", title="B", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.acquire_workspace("p-1", "a-1", "t-1")
        ws = await db.acquire_workspace("p-1", "a-2", "t-2")
        assert ws is None

    async def test_release_workspace(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.acquire_workspace("p-1", "a-1", "t-1")
        await db.release_workspace("ws-1")
        ws = await db.get_workspace("ws-1")
        assert ws.locked_by_agent_id is None
        assert ws.locked_by_task_id is None

    async def test_release_workspaces_for_agent(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.acquire_workspace("p-1", "a-1", "t-1")
        count = await db.release_workspaces_for_agent("a-1")
        assert count == 1
        ws = await db.get_workspace("ws-1")
        assert ws.locked_by_agent_id is None

    async def test_get_project_workspace_path(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.LINK,
            )
        )
        path = await db.get_project_workspace_path("p-1")
        assert path == "/tmp/ws1"

    async def test_get_project_workspace_path_no_workspaces(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        path = await db.get_project_workspace_path("p-1")
        assert path is None

    async def test_count_available_workspaces(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-1",
                workspace_path="/tmp/ws2",
                source_type=RepoSourceType.LINK,
            )
        )
        assert await db.count_available_workspaces("p-1") == 2

        await db.acquire_workspace("p-1", "a-1", "t-1")
        assert await db.count_available_workspaces("p-1") == 1

    async def test_count_available_workspaces_no_workspaces(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        assert await db.count_available_workspaces("p-1") == 0
