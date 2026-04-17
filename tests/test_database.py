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
    WorkspaceMode,
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
        assert ws.lock_mode == WorkspaceMode.EXCLUSIVE  # default

    async def test_acquire_workspace_lock_mode(self, db):
        """acquire_workspace stores the requested lock_mode on the workspace."""
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
        ws = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws is not None
        assert ws.lock_mode == WorkspaceMode.BRANCH_ISOLATED

        # Verify persisted via get_workspace
        ws2 = await db.get_workspace("ws-1")
        assert ws2.lock_mode == WorkspaceMode.BRANCH_ISOLATED

    async def test_release_workspace_clears_lock_mode(self, db):
        """release_workspace clears lock_mode along with other lock columns."""
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
        await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED)
        await db.release_workspace("ws-1")
        ws = await db.get_workspace("ws-1")
        assert ws.locked_by_agent_id is None
        assert ws.lock_mode is None

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


class TestExclusiveWorkspaceModeBackwardCompat:
    """Roadmap 7.4.5 — Exclusive workspace mode backward compatibility.

    Verifies that the lock_mode="exclusive" (default) behaves identically
    to the pre-lock-mode workspace locking, and that mode conflicts are
    properly handled.
    """

    async def test_exclusive_blocks_second_agent(self, db):
        """(a) Workspace acquired with lock_mode=exclusive blocks a second agent."""
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

        # First agent acquires with explicit exclusive mode
        ws = await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws is not None
        assert ws.locked_by_agent_id == "a-1"
        assert ws.lock_mode == WorkspaceMode.EXCLUSIVE

        # Second agent cannot acquire the same workspace
        ws2 = await db.acquire_workspace("p-1", "a-2", "t-2", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws2 is None

    async def test_second_agent_returns_none_not_exception(self, db):
        """(b) Second agent's acquisition attempt returns None (not an exception).

        When all workspaces are exclusively locked, the second agent gets a
        clean None return — no exception raised, no partial state change.
        """
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
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-1",
                workspace_path="/tmp/ws2",
                source_type=RepoSourceType.LINK,
            )
        )

        # First agent locks ws-1, second agent locks ws-2
        await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.EXCLUSIVE)
        await db.acquire_workspace("p-1", "a-2", "t-2", lock_mode=WorkspaceMode.EXCLUSIVE)

        # Third agent — all workspaces are locked, should get None (not raise)
        await db.create_agent(Agent(id="a-3", name="claude-3", agent_type="claude"))
        await db.create_task(Task(id="t-3", project_id="p-1", title="C", description="D"))
        result = await db.acquire_workspace("p-1", "a-3", "t-3", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert result is None

        # Verify existing locks are untouched (no side effects from failed acquire)
        ws1 = await db.get_workspace("ws-1")
        assert ws1.locked_by_agent_id == "a-1"
        assert ws1.lock_mode == WorkspaceMode.EXCLUSIVE
        ws2 = await db.get_workspace("ws-2")
        assert ws2.locked_by_agent_id == "a-2"
        assert ws2.lock_mode == WorkspaceMode.EXCLUSIVE

    async def test_exclusive_release_allows_next_agent(self, db):
        """(c) Releasing an exclusive lock allows the next agent to acquire."""
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

        # First agent acquires exclusively
        ws = await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws is not None

        # Second agent blocked
        ws2 = await db.acquire_workspace("p-1", "a-2", "t-2", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws2 is None

        # Release the exclusive lock
        await db.release_workspace("ws-1")

        # Verify workspace is fully unlocked
        released = await db.get_workspace("ws-1")
        assert released.locked_by_agent_id is None
        assert released.locked_by_task_id is None
        assert released.locked_at is None
        assert released.lock_mode is None

        # Now second agent can acquire
        ws3 = await db.acquire_workspace("p-1", "a-2", "t-2", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws3 is not None
        assert ws3.locked_by_agent_id == "a-2"
        assert ws3.locked_by_task_id == "t-2"
        assert ws3.lock_mode == WorkspaceMode.EXCLUSIVE

    async def test_exclusive_identical_to_pre_lockmode_behavior(self, db):
        """(d) Exclusive mode behavior is identical to pre-lock-mode behavior.

        The original acquire_workspace (before lock_mode was added) locked a
        workspace by setting locked_by_agent_id/task_id/locked_at and blocked
        any second acquisition on the same workspace. Exclusive mode must
        produce the same semantics — the only addition is the lock_mode field.
        """
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

        # Acquire using the default (no explicit lock_mode) — same as legacy call
        ws = await db.acquire_workspace("p-1", "a-1", "t-1")
        assert ws is not None

        # All legacy lock fields are set
        assert ws.locked_by_agent_id == "a-1"
        assert ws.locked_by_task_id == "t-1"
        assert ws.locked_at is not None

        # New field is set to EXCLUSIVE by default
        assert ws.lock_mode == WorkspaceMode.EXCLUSIVE

        # Blocking behavior: second acquire returns None (same as pre-lock-mode)
        ws2 = await db.acquire_workspace("p-1", "a-2", "t-2")
        assert ws2 is None

        # Release and verify all columns cleared (same as pre-lock-mode release)
        await db.release_workspace("ws-1")
        released = await db.get_workspace("ws-1")
        assert released.locked_by_agent_id is None
        assert released.locked_by_task_id is None
        assert released.locked_at is None
        assert released.lock_mode is None

        # Re-acquire after release works (same as pre-lock-mode)
        ws3 = await db.acquire_workspace("p-1", "a-2", "t-2")
        assert ws3 is not None
        assert ws3.locked_by_agent_id == "a-2"

    async def test_no_explicit_lock_mode_defaults_to_exclusive(self, db):
        """(e) Workspace acquired without explicit lock_mode defaults to exclusive."""
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

        # Acquire without specifying lock_mode
        ws = await db.acquire_workspace("p-1", "a-1", "t-1")
        assert ws is not None

        # Returned workspace shows exclusive
        assert ws.lock_mode == WorkspaceMode.EXCLUSIVE

        # Persisted in DB as exclusive
        ws_from_db = await db.get_workspace("ws-1")
        assert ws_from_db.lock_mode == WorkspaceMode.EXCLUSIVE

    async def test_cannot_downgrade_exclusive_to_branch_isolated(self, db):
        """(f) Mixing exclusive and branch-isolated on same repo is rejected.

        When a workspace at a given path is locked exclusively, no other
        workspace at the same path can be acquired — even with
        branch-isolated mode. The path-level conflict check blocks any
        second acquisition on a path that is already exclusively locked,
        regardless of the requested lock mode. You cannot downgrade from
        exclusive.

        Uses two projects sharing the same workspace path (the unique
        constraint is per-project, but the path-level lock check is global).
        """
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="claude-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))

        # Two workspace records in different projects pointing at the same path
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/shared-repo",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-2",
                workspace_path="/tmp/shared-repo",
                source_type=RepoSourceType.LINK,
            )
        )

        # First agent locks exclusively via project p-1
        ws = await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws is not None
        assert ws.lock_mode == WorkspaceMode.EXCLUSIVE

        # Second agent tries to acquire via project p-2 at the same path,
        # requesting branch-isolated mode — must be rejected because the
        # path is already exclusively locked
        ws2 = await db.acquire_workspace(
            "p-2", "a-2", "t-2", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws2 is None


class TestBranchIsolatedWorkspaceMode:
    """Roadmap 7.4.2 — Branch-isolated workspace lock mode.

    Verifies that the lock_mode="branch-isolated" allows multiple agents to
    share workspace paths when all participants use BRANCH_ISOLATED mode,
    while properly rejecting mixed-mode conflicts.
    """

    async def test_branch_isolated_allows_same_path_cross_project(self, db):
        """(a) Two BRANCH_ISOLATED locks on the same path coexist.

        When two workspace records from different projects point at the same
        filesystem path and both are acquired with BRANCH_ISOLATED mode,
        the path-level lock check permits both acquisitions.
        """
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="claude-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))

        # Two workspace records in different projects, same path
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/shared-repo",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-2",
                workspace_path="/tmp/shared-repo",
                source_type=RepoSourceType.LINK,
            )
        )

        # First agent acquires BRANCH_ISOLATED
        ws = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws is not None
        assert ws.lock_mode == WorkspaceMode.BRANCH_ISOLATED

        # Second agent acquires BRANCH_ISOLATED at same path — allowed
        ws2 = await db.acquire_workspace(
            "p-2", "a-2", "t-2", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws2 is not None
        assert ws2.lock_mode == WorkspaceMode.BRANCH_ISOLATED
        assert ws2.locked_by_agent_id == "a-2"

    async def test_branch_isolated_blocked_by_exclusive(self, db):
        """(b) EXCLUSIVE lock on a path blocks BRANCH_ISOLATED acquisition.

        When a workspace path is exclusively locked, no BRANCH_ISOLATED
        acquisition is allowed on the same path. This is the same as the
        existing test in TestExclusiveWorkspaceModeBackwardCompat but
        verifies it from the BI perspective.
        """
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="claude-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))

        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/shared-repo",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-2",
                workspace_path="/tmp/shared-repo",
                source_type=RepoSourceType.LINK,
            )
        )

        # Exclusive lock first
        ws = await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws is not None

        # BRANCH_ISOLATED blocked by existing EXCLUSIVE
        ws2 = await db.acquire_workspace(
            "p-2", "a-2", "t-2", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws2 is None

    async def test_exclusive_blocked_by_branch_isolated(self, db):
        """(c) BRANCH_ISOLATED lock on a path blocks EXCLUSIVE acquisition.

        When a workspace path is locked with BRANCH_ISOLATED, an EXCLUSIVE
        request on the same path is rejected — EXCLUSIVE requires sole
        access.
        """
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="claude-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))

        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/shared-repo",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-2",
                workspace_path="/tmp/shared-repo",
                source_type=RepoSourceType.LINK,
            )
        )

        # BRANCH_ISOLATED lock first
        ws = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws is not None

        # EXCLUSIVE blocked by existing BRANCH_ISOLATED
        ws2 = await db.acquire_workspace("p-2", "a-2", "t-2", lock_mode=WorkspaceMode.EXCLUSIVE)
        assert ws2 is None

    async def test_find_branch_isolated_base(self, db):
        """(d) find_branch_isolated_base returns BI-locked workspace for sharing."""
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.CLONE,
            )
        )

        # No BI-locked workspace yet
        base = await db.find_branch_isolated_base("p-1")
        assert base is None

        # Lock with BRANCH_ISOLATED
        await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED)

        # Now find_branch_isolated_base should return the workspace
        base = await db.find_branch_isolated_base("p-1")
        assert base is not None
        assert base.id == "ws-1"
        assert base.lock_mode == WorkspaceMode.BRANCH_ISOLATED

    async def test_find_branch_isolated_base_ignores_exclusive(self, db):
        """(e) find_branch_isolated_base ignores exclusively-locked workspaces."""
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

        # Lock with EXCLUSIVE
        await db.acquire_workspace("p-1", "a-1", "t-1", lock_mode=WorkspaceMode.EXCLUSIVE)

        # find_branch_isolated_base should not return exclusively-locked workspace
        base = await db.find_branch_isolated_base("p-1")
        assert base is None

    async def test_find_branch_isolated_base_no_workspaces(self, db):
        """(f) find_branch_isolated_base returns None for project without workspaces."""
        await db.create_project(Project(id="p-1", name="alpha"))
        base = await db.find_branch_isolated_base("p-1")
        assert base is None

    async def test_branch_isolated_release_allows_reacquisition(self, db):
        """(g) Releasing a BRANCH_ISOLATED lock allows reacquisition."""
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

        # First agent acquires
        ws = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws is not None

        # Second agent can't acquire same workspace (it's locked by a-1)
        ws2 = await db.acquire_workspace(
            "p-1", "a-2", "t-2", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws2 is None  # Only 1 workspace, already locked

        # Release first agent's lock
        await db.release_workspace("ws-1")

        # Now second agent can acquire
        ws3 = await db.acquire_workspace(
            "p-1", "a-2", "t-2", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws3 is not None
        assert ws3.locked_by_agent_id == "a-2"
        assert ws3.lock_mode == WorkspaceMode.BRANCH_ISOLATED

    async def test_worktree_workspace_source_type(self, db):
        """(h) Workspace with source_type=WORKTREE can be created and acquired."""
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-wt-1",
                project_id="p-1",
                workspace_path="/tmp/ws1/.worktrees-repo/my-branch",
                source_type=RepoSourceType.WORKTREE,
                name="worktree:ws-1",
            )
        )

        # Acquire the worktree workspace
        ws = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.BRANCH_ISOLATED
        )
        assert ws is not None
        assert ws.source_type == RepoSourceType.WORKTREE
        assert ws.lock_mode == WorkspaceMode.BRANCH_ISOLATED

        # Verify persisted
        ws2 = await db.get_workspace("ws-wt-1")
        assert ws2.source_type == RepoSourceType.WORKTREE
        assert ws2.locked_by_agent_id == "a-1"

    async def test_multiple_branch_isolated_cross_project_simultaneous(self, db):
        """(i) Three agents can hold BRANCH_ISOLATED locks on same path simultaneously.

        Tests the scenario where three different projects share a workspace
        path and all acquire with BRANCH_ISOLATED mode — all three should
        succeed.
        """
        for i in range(1, 4):
            await db.create_project(Project(id=f"p-{i}", name=f"project-{i}"))
            await db.create_agent(Agent(id=f"a-{i}", name=f"claude-{i}", agent_type="claude"))
            await db.create_task(
                Task(id=f"t-{i}", project_id=f"p-{i}", title=f"Task {i}", description="D")
            )
            await db.create_workspace(
                Workspace(
                    id=f"ws-{i}",
                    project_id=f"p-{i}",
                    workspace_path="/tmp/shared-mono-repo",
                    source_type=RepoSourceType.LINK,
                )
            )

        # All three acquire BRANCH_ISOLATED — all should succeed
        results = []
        for i in range(1, 4):
            ws = await db.acquire_workspace(
                f"p-{i}", f"a-{i}", f"t-{i}", lock_mode=WorkspaceMode.BRANCH_ISOLATED
            )
            results.append(ws)

        assert all(ws is not None for ws in results)
        assert all(ws.lock_mode == WorkspaceMode.BRANCH_ISOLATED for ws in results)
        assert results[0].locked_by_agent_id == "a-1"
        assert results[1].locked_by_agent_id == "a-2"
        assert results[2].locked_by_agent_id == "a-3"


class TestDirectoryIsolatedWorkspaceModeStub:
    """Roadmap 7.4.6 — Directory-isolated workspace mode (deferred stub).

    The ``directory-isolated`` mode is designed for monorepo workflows where
    multiple agents work on the same branch in different directories.  The mode
    is accepted by the data model and persisted, but **not yet implemented** in
    the orchestrator.  These tests verify the stub behavior:

    - The enum value is valid and can be stored/retrieved.
    - At the DB level, directory-isolated currently falls through to exclusive-
      like locking (no special handling).
    - The orchestrator rejects directory-isolated at execution time (tested
      separately in test_orchestrator.py when that mode is implemented).

    See docs/specs/design/agent-coordination.md §7 (Workspace Strategy).
    """

    async def test_directory_isolated_enum_value_exists(self, db):
        """The DIRECTORY_ISOLATED enum value is defined and valid."""
        assert WorkspaceMode.DIRECTORY_ISOLATED.value == "directory-isolated"
        assert "directory-isolated" in {m.value for m in WorkspaceMode}

    async def test_directory_isolated_stored_on_task(self, db):
        """workspace_mode='directory-isolated' can be persisted on a task."""
        await db.create_project(Project(id="p-1", name="alpha"))
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Monorepo task",
            description="Work in packages/auth/",
            workspace_mode=WorkspaceMode.DIRECTORY_ISOLATED,
        )
        await db.create_task(task)
        retrieved = await db.get_task("t-1")
        assert retrieved.workspace_mode == WorkspaceMode.DIRECTORY_ISOLATED

    async def test_directory_isolated_stored_on_workspace_lock(self, db):
        """acquire_workspace with DIRECTORY_ISOLATED stores the lock mode."""
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/monorepo",
                source_type=RepoSourceType.LINK,
            )
        )
        ws = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.DIRECTORY_ISOLATED
        )
        assert ws is not None
        assert ws.lock_mode == WorkspaceMode.DIRECTORY_ISOLATED

        # Verify persisted
        ws2 = await db.get_workspace("ws-1")
        assert ws2.lock_mode == WorkspaceMode.DIRECTORY_ISOLATED

    async def test_directory_isolated_blocks_like_exclusive_for_now(self, db):
        """Until fully implemented, DIRECTORY_ISOLATED blocks like EXCLUSIVE.

        The DB-level path conflict check treats DIRECTORY_ISOLATED the same
        as EXCLUSIVE — no special directory-scoped locking exists yet.  A
        second agent requesting DIRECTORY_ISOLATED on the same path is
        blocked (falls through to the catch-all exclusive check).
        """
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_agent(Agent(id="a-2", name="claude-2", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_task(Task(id="t-2", project_id="p-2", title="B", description="D"))

        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/monorepo",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-2",
                workspace_path="/tmp/monorepo",
                source_type=RepoSourceType.LINK,
            )
        )

        # First agent acquires with DIRECTORY_ISOLATED
        ws = await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.DIRECTORY_ISOLATED
        )
        assert ws is not None

        # Second agent is blocked — no directory-scoped locking yet
        ws2 = await db.acquire_workspace(
            "p-2", "a-2", "t-2", lock_mode=WorkspaceMode.DIRECTORY_ISOLATED
        )
        assert ws2 is None

    async def test_directory_isolated_release_clears_lock_mode(self, db):
        """Releasing a DIRECTORY_ISOLATED lock clears lock_mode to None."""
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))
        await db.create_task(Task(id="t-1", project_id="p-1", title="A", description="D"))
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/monorepo",
                source_type=RepoSourceType.LINK,
            )
        )
        await db.acquire_workspace(
            "p-1", "a-1", "t-1", lock_mode=WorkspaceMode.DIRECTORY_ISOLATED
        )
        await db.release_workspace("ws-1")
        ws = await db.get_workspace("ws-1")
        assert ws.locked_by_agent_id is None
        assert ws.lock_mode is None
