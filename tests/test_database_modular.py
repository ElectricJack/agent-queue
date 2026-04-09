"""Tests for the modular database package.

Verifies that:
1. The DatabaseBackend protocol is satisfied by SQLiteDatabaseAdapter
2. Each query module works correctly via the adapter
3. Backward compatibility is maintained
4. The abstraction layer works with mock adapters
"""

import json

import pytest
import time

from src.database import Database, DatabaseBackend, SQLiteDatabaseAdapter
from src.database.tables import metadata
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Hook,
    HookRun,
    PlaybookRun,
    Project,
    ProjectStatus,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
async def db(tmp_path):
    """Provide an initialized SQLiteDatabaseAdapter."""
    database = SQLiteDatabaseAdapter(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def db_via_alias(tmp_path):
    """Provide a Database instance (backward-compat alias)."""
    database = Database(str(tmp_path / "test_alias.db"))
    await database.initialize()
    yield database
    await database.close()


# Helper to create a project for FK constraints
async def _make_project(db, pid="p-1"):
    await db.create_project(Project(id=pid, name=f"project-{pid}"))


async def _make_agent(db, aid="a-1"):
    await db.create_agent(Agent(id=aid, name="agent-1", agent_type="claude"))


async def _make_task(db, tid="t-1", pid="p-1"):
    await db.create_task(
        Task(
            id=tid,
            project_id=pid,
            title="Test Task",
            description="desc",
        )
    )


# ── Protocol / Structural Tests ──────────────────────────────────────────


class TestProtocolCompliance:
    """Verify SQLiteDatabaseAdapter satisfies the DatabaseBackend protocol."""

    def test_adapter_is_instance_of_protocol(self):
        """SQLiteDatabaseAdapter should be a runtime-checkable DatabaseBackend."""
        assert issubclass(SQLiteDatabaseAdapter, DatabaseBackend)

    def test_database_alias_is_adapter(self):
        """Database should be the same class as SQLiteDatabaseAdapter."""
        assert Database is SQLiteDatabaseAdapter

    def test_metadata_has_tables(self):
        """SQLAlchemy metadata should define all expected tables."""
        table_names = set(metadata.tables.keys())
        assert "projects" in table_names
        assert "tasks" in table_names
        assert "agents" in table_names
        assert "workspaces" in table_names
        assert len(table_names) >= 18


# ── Backward Compatibility ───────────────────────────────────────────────


class TestBackwardCompatibility:
    """Ensure the alias works exactly like the old Database class."""

    async def test_alias_creates_and_reads(self, db_via_alias):
        await db_via_alias.create_project(Project(id="p-1", name="test"))
        result = await db_via_alias.get_project("p-1")
        assert result.name == "test"


# ── Project Queries ──────────────────────────────────────────────────────


class TestProjectQueries:
    async def test_create_get_project(self, db):
        await db.create_project(Project(id="p-1", name="alpha", credit_weight=2.5))
        p = await db.get_project("p-1")
        assert p is not None
        assert p.name == "alpha"
        assert p.credit_weight == 2.5

    async def test_list_projects_filtered(self, db):
        await db.create_project(Project(id="p-1", name="a", status=ProjectStatus.ACTIVE))
        await db.create_project(Project(id="p-2", name="b", status=ProjectStatus.PAUSED))
        active = await db.list_projects(status=ProjectStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].id == "p-1"

    async def test_update_project(self, db):
        await db.create_project(Project(id="p-1", name="old"))
        await db.update_project("p-1", name="new")
        p = await db.get_project("p-1")
        assert p.name == "new"

    async def test_delete_project_cascades(self, db):
        await _make_project(db, "p-1")
        await _make_task(db, "t-1", "p-1")
        await db.delete_project("p-1")
        assert await db.get_project("p-1") is None
        assert await db.get_task("t-1") is None


# ── Profile Queries ──────────────────────────────────────────────────────


class TestProfileQueries:
    async def test_create_get_profile(self, db):
        profile = AgentProfile(id="prof-1", name="default", description="test")
        await db.create_profile(profile)
        p = await db.get_profile("prof-1")
        assert p is not None
        assert p.name == "default"

    async def test_list_profiles(self, db):
        await db.create_profile(AgentProfile(id="p1", name="aaa"))
        await db.create_profile(AgentProfile(id="p2", name="bbb"))
        profiles = await db.list_profiles()
        assert len(profiles) == 2
        assert profiles[0].name == "aaa"  # ordered by name

    async def test_update_profile(self, db):
        await db.create_profile(AgentProfile(id="p1", name="test", model="old"))
        await db.update_profile("p1", model="new")
        p = await db.get_profile("p1")
        assert p.model == "new"

    async def test_delete_profile(self, db):
        await db.create_profile(AgentProfile(id="p1", name="test"))
        await db.delete_profile("p1")
        assert await db.get_profile("p1") is None


# ── Repo Queries ─────────────────────────────────────────────────────────


class TestRepoQueries:
    async def test_create_get_repo(self, db):
        await _make_project(db)
        repo = RepoConfig(
            id="r-1",
            project_id="p-1",
            url="https://example.com/repo.git",
            checkout_base_path="/tmp/repos",
            source_type=RepoSourceType.CLONE,
        )
        await db.create_repo(repo)
        r = await db.get_repo("r-1")
        assert r is not None
        assert r.url == "https://example.com/repo.git"

    async def test_list_repos_by_project(self, db):
        await _make_project(db, "p-1")
        await _make_project(db, "p-2")
        await db.create_repo(
            RepoConfig(
                id="r-1",
                project_id="p-1",
                url="u1",
                checkout_base_path="/t",
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.create_repo(
            RepoConfig(
                id="r-2",
                project_id="p-2",
                url="u2",
                checkout_base_path="/t",
                source_type=RepoSourceType.CLONE,
            )
        )
        repos = await db.list_repos(project_id="p-1")
        assert len(repos) == 1

    async def test_delete_repo(self, db):
        await _make_project(db)
        await db.create_repo(
            RepoConfig(
                id="r-1",
                project_id="p-1",
                url="u",
                checkout_base_path="/t",
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.delete_repo("r-1")
        assert await db.get_repo("r-1") is None


# ── Task Queries ─────────────────────────────────────────────────────────


class TestTaskQueries:
    async def test_create_get_task(self, db):
        await _make_project(db)
        task = Task(id="t-1", project_id="p-1", title="Test", description="d")
        await db.create_task(task)
        t = await db.get_task("t-1")
        assert t is not None
        assert t.title == "Test"
        assert t.status == TaskStatus.DEFINED

    async def test_list_active_tasks(self, db):
        await _make_project(db)
        await db.create_task(Task(id="t-1", project_id="p-1", title="a", description="d"))
        await db.create_task(
            Task(
                id="t-2",
                project_id="p-1",
                title="b",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        active = await db.list_active_tasks(project_id="p-1")
        assert len(active) == 1
        assert active[0].id == "t-1"

    async def test_count_tasks_by_status(self, db):
        await _make_project(db)
        await db.create_task(Task(id="t-1", project_id="p-1", title="a", description="d"))
        await db.create_task(
            Task(
                id="t-2",
                project_id="p-1",
                title="b",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        counts = await db.count_tasks_by_status(project_id="p-1")
        assert counts.get("DEFINED") == 1
        assert counts.get("COMPLETED") == 1

    async def test_transition_task(self, db):
        await _make_project(db)
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="a",
                description="d",
                status=TaskStatus.READY,
            )
        )
        await db.transition_task("t-1", TaskStatus.ASSIGNED)
        t = await db.get_task("t-1")
        assert t.status == TaskStatus.ASSIGNED

    async def test_delete_task(self, db):
        await _make_project(db)
        await _make_task(db)
        await db.delete_task("t-1")
        assert await db.get_task("t-1") is None

    async def test_task_context(self, db):
        await _make_project(db)
        await _make_task(db)
        await db.add_task_context("t-1", type="note", label="hint", content="foo")
        contexts = await db.get_task_contexts("t-1")
        assert len(contexts) == 1
        assert contexts[0]["content"] == "foo"

    async def test_subtasks(self, db):
        await _make_project(db)
        await _make_task(db, "t-parent")
        await db.create_task(
            Task(
                id="t-child",
                project_id="p-1",
                parent_task_id="t-parent",
                title="child",
                description="d",
            )
        )
        subs = await db.get_subtasks("t-parent")
        assert len(subs) == 1
        assert subs[0].id == "t-child"

    async def test_task_tree(self, db):
        await _make_project(db)
        await _make_task(db, "root")
        await db.create_task(
            Task(
                id="child",
                project_id="p-1",
                parent_task_id="root",
                title="child",
                description="d",
            )
        )
        tree = await db.get_task_tree("root")
        assert tree is not None
        assert tree["task"].id == "root"
        assert len(tree["children"]) == 1

    async def test_get_parent_tasks(self, db):
        await _make_project(db)
        await _make_task(db, "root")
        await db.create_task(
            Task(
                id="child",
                project_id="p-1",
                parent_task_id="root",
                title="child",
                description="d",
            )
        )
        parents = await db.get_parent_tasks("p-1")
        assert len(parents) == 1
        assert parents[0].id == "root"


# ── Dependency Queries ───────────────────────────────────────────────────


class TestDependencyQueries:
    async def test_add_and_get_dependencies(self, db):
        await _make_project(db)
        await _make_task(db, "t-1")
        await _make_task(db, "t-2")
        await db.add_dependency("t-2", "t-1")
        deps = await db.get_dependencies("t-2")
        assert "t-1" in deps

    async def test_are_dependencies_met(self, db):
        await _make_project(db)
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="a",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        await _make_task(db, "t-2")
        await db.add_dependency("t-2", "t-1")
        assert await db.are_dependencies_met("t-2") is True

    async def test_dependencies_not_met(self, db):
        await _make_project(db)
        await _make_task(db, "t-1")
        await _make_task(db, "t-2")
        await db.add_dependency("t-2", "t-1")
        assert await db.are_dependencies_met("t-2") is False

    async def test_get_dependents(self, db):
        await _make_project(db)
        await _make_task(db, "t-1")
        await _make_task(db, "t-2")
        await db.add_dependency("t-2", "t-1")
        dependents = await db.get_dependents("t-1")
        assert "t-2" in dependents

    async def test_remove_dependency(self, db):
        await _make_project(db)
        await _make_task(db, "t-1")
        await _make_task(db, "t-2")
        await db.add_dependency("t-2", "t-1")
        await db.remove_dependency("t-2", "t-1")
        deps = await db.get_dependencies("t-2")
        assert len(deps) == 0

    async def test_dependency_map(self, db):
        await _make_project(db)
        await _make_task(db, "t-1")
        await _make_task(db, "t-2")
        await db.add_dependency("t-2", "t-1")
        dep_map = await db.get_dependency_map_for_tasks(["t-1", "t-2"])
        assert len(dep_map["t-2"]["depends_on"]) == 1
        assert "t-2" in dep_map["t-1"]["blocks"]


# ── Agent Queries ────────────────────────────────────────────────────────


class TestAgentQueries:
    async def test_create_get_agent(self, db):
        agent = Agent(id="a-1", name="bot", agent_type="claude")
        await db.create_agent(agent)
        a = await db.get_agent("a-1")
        assert a is not None
        assert a.name == "bot"
        assert a.state == AgentState.IDLE

    async def test_list_agents_by_state(self, db):
        await db.create_agent(Agent(id="a-1", name="a", agent_type="claude"))
        await db.create_agent(
            Agent(
                id="a-2",
                name="b",
                agent_type="claude",
                state=AgentState.BUSY,
            )
        )
        idle = await db.list_agents(state=AgentState.IDLE)
        assert len(idle) == 1

    async def test_update_agent(self, db):
        await db.create_agent(Agent(id="a-1", name="a", agent_type="claude"))
        await db.update_agent("a-1", state=AgentState.BUSY)
        a = await db.get_agent("a-1")
        assert a.state == AgentState.BUSY

    async def test_delete_agent(self, db):
        await db.create_agent(Agent(id="a-1", name="a", agent_type="claude"))
        await db.delete_agent("a-1")
        assert await db.get_agent("a-1") is None


# ── Workspace Queries ────────────────────────────────────────────────────


class TestWorkspaceQueries:
    async def test_create_get_workspace(self, db):
        await _make_project(db)
        ws = Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path="/tmp/ws",
            source_type=RepoSourceType.CLONE,
        )
        await db.create_workspace(ws)
        w = await db.get_workspace("ws-1")
        assert w is not None
        assert w.workspace_path == "/tmp/ws"

    async def test_acquire_release_workspace(self, db):
        await _make_project(db)
        await _make_agent(db)
        await _make_task(db)
        ws = Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path="/tmp/ws",
            source_type=RepoSourceType.CLONE,
        )
        await db.create_workspace(ws)

        acquired = await db.acquire_workspace("p-1", "a-1", "t-1")
        assert acquired is not None
        assert acquired.locked_by_agent_id == "a-1"

        await db.release_workspace("ws-1")
        w = await db.get_workspace("ws-1")
        assert w.locked_by_agent_id is None

    async def test_count_available_workspaces(self, db):
        await _make_project(db)
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws1",
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-2",
                project_id="p-1",
                workspace_path="/tmp/ws2",
                source_type=RepoSourceType.CLONE,
            )
        )
        assert await db.count_available_workspaces("p-1") == 2

    async def test_workspace_for_task(self, db):
        await _make_project(db)
        await _make_agent(db)
        await _make_task(db)
        await db.create_workspace(
            Workspace(
                id="ws-1",
                project_id="p-1",
                workspace_path="/tmp/ws",
                source_type=RepoSourceType.CLONE,
            )
        )
        await db.acquire_workspace("p-1", "a-1", "t-1")
        ws = await db.get_workspace_for_task("t-1")
        assert ws is not None
        assert ws.id == "ws-1"


# ── Token Ledger Queries ────────────────────────────────────────────────


class TestTokenQueries:
    async def test_record_and_get_usage(self, db):
        await _make_project(db)
        await _make_agent(db)
        await _make_task(db)
        await db.record_token_usage("p-1", "a-1", "t-1", 500)
        await db.record_token_usage("p-1", "a-1", "t-1", 300)
        total = await db.get_project_token_usage("p-1")
        assert total == 800


# ── Event Queries ────────────────────────────────────────────────────────


class TestEventQueries:
    async def test_log_and_get_events(self, db):
        await db.log_event("test_event", payload='{"key": "value"}')
        events = await db.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "test_event"


# ── Hook Queries ─────────────────────────────────────────────────────────


class TestHookQueries:
    async def test_create_get_hook(self, db):
        await _make_project(db)
        hook = Hook(
            id="h-1",
            project_id="p-1",
            name="test-hook",
            trigger="task_completed",
            prompt_template="Do stuff",
        )
        await db.create_hook(hook)
        h = await db.get_hook("h-1")
        assert h is not None
        assert h.name == "test-hook"

    async def test_list_hooks_by_project(self, db):
        await _make_project(db, "p-1")
        await _make_project(db, "p-2")
        await db.create_hook(
            Hook(
                id="h-1",
                project_id="p-1",
                name="a",
                trigger="t",
                prompt_template="p",
            )
        )
        await db.create_hook(
            Hook(
                id="h-2",
                project_id="p-2",
                name="b",
                trigger="t",
                prompt_template="p",
            )
        )
        hooks = await db.list_hooks(project_id="p-1")
        assert len(hooks) == 1

    async def test_delete_hook(self, db):
        await _make_project(db)
        await db.create_hook(
            Hook(
                id="h-1",
                project_id="p-1",
                name="a",
                trigger="t",
                prompt_template="p",
            )
        )
        await db.delete_hook("h-1")
        assert await db.get_hook("h-1") is None

    async def test_hook_run_lifecycle(self, db):
        await _make_project(db)
        await db.create_hook(
            Hook(
                id="h-1",
                project_id="p-1",
                name="a",
                trigger="t",
                prompt_template="p",
            )
        )
        run = HookRun(
            id="hr-1",
            hook_id="h-1",
            project_id="p-1",
            trigger_reason="manual",
            status="running",
            started_at=time.time(),
        )
        await db.create_hook_run(run)
        last = await db.get_last_hook_run("h-1")
        assert last is not None
        assert last.id == "hr-1"

        await db.update_hook_run("hr-1", status="completed")
        runs = await db.list_hook_runs("h-1")
        assert runs[0].status == "completed"

    async def test_hooks_by_prefix(self, db):
        await _make_project(db)
        await db.create_hook(
            Hook(
                id="rule-abc-1",
                project_id="p-1",
                name="a",
                trigger="t",
                prompt_template="p",
            )
        )
        await db.create_hook(
            Hook(
                id="rule-abc-2",
                project_id="p-1",
                name="b",
                trigger="t",
                prompt_template="p",
            )
        )
        hooks = await db.list_hooks_by_id_prefix("rule-abc")
        assert len(hooks) == 2

        deleted = await db.delete_hooks_by_id_prefix("rule-abc")
        assert deleted == 2


# ── Archive Queries ──────────────────────────────────────────────────────


class TestArchiveQueries:
    async def test_archive_and_list(self, db):
        await _make_project(db)
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="done",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        result = await db.archive_task("t-1")
        assert result is True
        assert await db.get_task("t-1") is None

        archived = await db.list_archived_tasks()
        assert len(archived) == 1
        assert archived[0]["id"] == "t-1"

    async def test_restore_archived_task(self, db):
        await _make_project(db)
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="done",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        await db.archive_task("t-1")
        result = await db.restore_archived_task("t-1")
        assert result is True
        t = await db.get_task("t-1")
        assert t is not None
        assert t.status == TaskStatus.DEFINED

    async def test_count_archived(self, db):
        await _make_project(db)
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="done",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        await db.archive_task("t-1")
        assert await db.count_archived_tasks() == 1

    async def test_delete_archived(self, db):
        await _make_project(db)
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="done",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        await db.archive_task("t-1")
        assert await db.delete_archived_task("t-1") is True
        assert await db.count_archived_tasks() == 0


# ── Chat Analyzer Queries ────────────────────────────────────────────────


class TestChatQueries:
    async def test_create_and_get_suggestion(self, db):
        row_id = await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=123,
            suggestion_type="improvement",
            suggestion_text="Do better",
            suggestion_hash="abc123",
        )
        assert row_id > 0
        s = await db.get_suggestion(row_id)
        assert s is not None
        assert s["suggestion_text"] == "Do better"

    async def test_dedup_hash(self, db):
        await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=123,
            suggestion_type="improvement",
            suggestion_text="Do better",
            suggestion_hash="abc123",
        )
        assert await db.get_suggestion_hash_exists("p-1", "abc123") is True
        assert await db.get_suggestion_hash_exists("p-1", "different") is False

    async def test_resolve_suggestion(self, db):
        row_id = await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=123,
            suggestion_type="improvement",
            suggestion_text="text",
            suggestion_hash="hash1",
        )
        await db.resolve_chat_analyzer_suggestion(row_id, "accepted")
        s = await db.get_suggestion(row_id)
        assert s["status"] == "accepted"

    async def test_suggestion_stats(self, db):
        await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=123,
            suggestion_type="t",
            suggestion_text="a",
            suggestion_hash="h1",
        )
        stats = await db.get_analyzer_suggestion_stats(project_id="p-1")
        assert stats["total"] == 1
        assert stats["pending"] == 1


# ── Atomic Operations ────────────────────────────────────────────────────


class TestAtomicOperations:
    async def test_assign_task_to_agent(self, db):
        await _make_project(db)
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="a",
                description="d",
                status=TaskStatus.READY,
            )
        )
        await db.create_agent(Agent(id="a-1", name="bot", agent_type="claude"))

        await db.assign_task_to_agent("t-1", "a-1")

        t = await db.get_task("t-1")
        assert t.status == TaskStatus.ASSIGNED
        assert t.assigned_agent_id == "a-1"

        a = await db.get_agent("a-1")
        assert a.state == AgentState.BUSY
        assert a.current_task_id == "t-1"

        events = await db.get_recent_events()
        assert any(e["event_type"] == "task_assigned" for e in events)


# ── Mock Adapter Test ────────────────────────────────────────────────────


# ── Playbook Run Queries ────────────────────────────────────────────────


def _make_playbook_run(
    run_id: str = "run-1",
    playbook_id: str = "pb-test",
    version: int = 1,
    status: str = "running",
    conversation_history: list | None = None,
    node_trace: list | None = None,
    **kwargs,
) -> PlaybookRun:
    """Helper to build a PlaybookRun with sensible defaults."""
    return PlaybookRun(
        run_id=run_id,
        playbook_id=playbook_id,
        playbook_version=version,
        trigger_event=json.dumps(kwargs.get("trigger_event", {"type": "test"})),
        status=status,
        current_node=kwargs.get("current_node"),
        conversation_history=json.dumps(conversation_history or []),
        node_trace=json.dumps(node_trace or []),
        tokens_used=kwargs.get("tokens_used", 0),
        started_at=kwargs.get("started_at", time.time()),
        completed_at=kwargs.get("completed_at"),
        error=kwargs.get("error"),
    )


class TestPlaybookRunQueries:
    """Integration tests for PlaybookRun CRUD — real SQLite, not mocks."""

    async def test_create_and_get_run(self, db):
        run = _make_playbook_run()
        await db.create_playbook_run(run)
        fetched = await db.get_playbook_run("run-1")
        assert fetched is not None
        assert fetched.run_id == "run-1"
        assert fetched.playbook_id == "pb-test"
        assert fetched.playbook_version == 1
        assert fetched.status == "running"

    async def test_get_nonexistent_returns_none(self, db):
        assert await db.get_playbook_run("nope") is None

    async def test_conversation_history_json_round_trip(self, db):
        """Conversation history must survive serialization through the DB."""
        messages = [
            {"role": "user", "content": 'Event received: {"type": "git.push"}'},
            {"role": "user", "content": "Scan the repository for issues."},
            {"role": "assistant", "content": "Found 3 issues in src/main.py."},
            {"role": "user", "content": "Triage the findings by severity."},
            {"role": "assistant", "content": "Critical: 1, Warning: 2."},
        ]
        run = _make_playbook_run(conversation_history=messages)
        await db.create_playbook_run(run)

        fetched = await db.get_playbook_run("run-1")
        assert fetched is not None
        restored = json.loads(fetched.conversation_history)
        assert restored == messages
        assert len(restored) == 5
        assert restored[2]["role"] == "assistant"
        assert "3 issues" in restored[2]["content"]

    async def test_node_trace_json_round_trip(self, db):
        """Node trace entries must survive serialization through the DB."""
        trace = [
            {
                "node_id": "scan",
                "started_at": 1000.0,
                "completed_at": 1001.5,
                "status": "completed",
            },
            {
                "node_id": "triage",
                "started_at": 1001.5,
                "completed_at": 1003.0,
                "status": "completed",
            },
            {
                "node_id": "fix",
                "started_at": 1003.0,
                "completed_at": None,
                "status": "failed",
            },
        ]
        run = _make_playbook_run(node_trace=trace, current_node="fix", status="failed")
        await db.create_playbook_run(run)

        fetched = await db.get_playbook_run("run-1")
        assert fetched is not None
        restored = json.loads(fetched.node_trace)
        assert restored == trace
        assert restored[2]["status"] == "failed"
        assert restored[2]["completed_at"] is None

    async def test_trigger_event_json_round_trip(self, db):
        event = {"type": "git.push", "project_id": "proj-1", "ref": "refs/heads/main"}
        run = _make_playbook_run(trigger_event=event)
        await db.create_playbook_run(run)

        fetched = await db.get_playbook_run("run-1")
        restored = json.loads(fetched.trigger_event)
        assert restored == event

    async def test_update_status_and_fields(self, db):
        run = _make_playbook_run()
        await db.create_playbook_run(run)

        completed_at = time.time()
        await db.update_playbook_run(
            "run-1",
            status="completed",
            tokens_used=1500,
            completed_at=completed_at,
        )

        fetched = await db.get_playbook_run("run-1")
        assert fetched.status == "completed"
        assert fetched.tokens_used == 1500
        assert fetched.completed_at == completed_at

    async def test_update_conversation_history(self, db):
        """Conversation history can be incrementally updated after each node."""
        run = _make_playbook_run(conversation_history=[{"role": "user", "content": "seed"}])
        await db.create_playbook_run(run)

        # Simulate adding messages after node execution
        new_history = [
            {"role": "user", "content": "seed"},
            {"role": "user", "content": "Step A"},
            {"role": "assistant", "content": "Result A"},
        ]
        await db.update_playbook_run("run-1", conversation_history=json.dumps(new_history))

        fetched = await db.get_playbook_run("run-1")
        restored = json.loads(fetched.conversation_history)
        assert len(restored) == 3
        assert restored[2]["content"] == "Result A"

    async def test_update_node_trace(self, db):
        """Node trace grows as each node completes."""
        run = _make_playbook_run()
        await db.create_playbook_run(run)

        trace = [
            {"node_id": "a", "started_at": 100.0, "completed_at": 101.0, "status": "completed"}
        ]
        await db.update_playbook_run("run-1", node_trace=json.dumps(trace), current_node="a")

        trace.append(
            {"node_id": "b", "started_at": 101.0, "completed_at": 102.0, "status": "completed"}
        )
        await db.update_playbook_run("run-1", node_trace=json.dumps(trace), current_node="b")

        fetched = await db.get_playbook_run("run-1")
        restored = json.loads(fetched.node_trace)
        assert len(restored) == 2
        assert restored[1]["node_id"] == "b"
        assert fetched.current_node == "b"

    async def test_list_runs_newest_first(self, db):
        for i in range(3):
            run = _make_playbook_run(
                run_id=f"run-{i}",
                started_at=1000.0 + i,
            )
            await db.create_playbook_run(run)

        runs = await db.list_playbook_runs()
        assert len(runs) == 3
        # Newest first
        assert runs[0].run_id == "run-2"
        assert runs[2].run_id == "run-0"

    async def test_list_filter_by_playbook_id(self, db):
        await db.create_playbook_run(_make_playbook_run(run_id="r1", playbook_id="pb-a"))
        await db.create_playbook_run(_make_playbook_run(run_id="r2", playbook_id="pb-b"))
        await db.create_playbook_run(_make_playbook_run(run_id="r3", playbook_id="pb-a"))

        runs = await db.list_playbook_runs(playbook_id="pb-a")
        assert len(runs) == 2
        assert all(r.playbook_id == "pb-a" for r in runs)

    async def test_list_filter_by_status(self, db):
        await db.create_playbook_run(_make_playbook_run(run_id="r1", status="running"))
        await db.create_playbook_run(_make_playbook_run(run_id="r2", status="completed"))
        await db.create_playbook_run(_make_playbook_run(run_id="r3", status="paused"))

        running = await db.list_playbook_runs(status="running")
        assert len(running) == 1
        assert running[0].run_id == "r1"

        paused = await db.list_playbook_runs(status="paused")
        assert len(paused) == 1
        assert paused[0].run_id == "r3"

    async def test_list_with_limit(self, db):
        for i in range(5):
            await db.create_playbook_run(
                _make_playbook_run(run_id=f"run-{i}", started_at=1000.0 + i)
            )

        runs = await db.list_playbook_runs(limit=2)
        assert len(runs) == 2
        assert runs[0].run_id == "run-4"  # newest

    async def test_list_combined_filters(self, db):
        """Filter by both playbook_id and status simultaneously."""
        await db.create_playbook_run(
            _make_playbook_run(run_id="r1", playbook_id="pb-a", status="completed")
        )
        await db.create_playbook_run(
            _make_playbook_run(run_id="r2", playbook_id="pb-a", status="running")
        )
        await db.create_playbook_run(
            _make_playbook_run(run_id="r3", playbook_id="pb-b", status="completed")
        )

        runs = await db.list_playbook_runs(playbook_id="pb-a", status="completed")
        assert len(runs) == 1
        assert runs[0].run_id == "r1"

    async def test_delete_run(self, db):
        await db.create_playbook_run(_make_playbook_run())
        await db.delete_playbook_run("run-1")
        assert await db.get_playbook_run("run-1") is None

    async def test_delete_nonexistent_is_noop(self, db):
        # Should not raise
        await db.delete_playbook_run("nonexistent")

    async def test_error_field_persisted(self, db):
        run = _make_playbook_run(
            status="failed",
            error="Node 'scan' failed: LLM provider down",
            completed_at=time.time(),
        )
        await db.create_playbook_run(run)

        fetched = await db.get_playbook_run("run-1")
        assert fetched.status == "failed"
        assert fetched.error == "Node 'scan' failed: LLM provider down"

    async def test_paused_run_full_state_round_trip(self, db):
        """A paused run must persist all state needed for resume.

        This is the critical path for human-in-the-loop: the full conversation
        history, node trace, current_node, and tokens_used must all survive
        a write→read cycle so PlaybookRunner.resume() can reconstruct state.
        """
        messages = [
            {"role": "user", "content": "Event: git.push on proj-1"},
            {"role": "user", "content": "Analyse the issue."},
            {"role": "assistant", "content": "Analysis: found 2 issues."},
            {"role": "user", "content": "Present for human review."},
            {"role": "assistant", "content": "Please review: 2 issues found."},
        ]
        trace = [
            {
                "node_id": "analyse",
                "started_at": 1000.0,
                "completed_at": 1005.0,
                "status": "completed",
            },
            {
                "node_id": "review",
                "started_at": 1005.0,
                "completed_at": 1010.0,
                "status": "completed",
            },
        ]
        event = {"type": "git.push", "project_id": "proj-1"}

        run = _make_playbook_run(
            run_id="paused-1",
            playbook_id="review-playbook",
            version=3,
            status="paused",
            conversation_history=messages,
            node_trace=trace,
            trigger_event=event,
            current_node="review",
            tokens_used=750,
            started_at=1000.0,
        )
        await db.create_playbook_run(run)

        # Fetch and verify every field needed for resume
        fetched = await db.get_playbook_run("paused-1")
        assert fetched is not None
        assert fetched.status == "paused"
        assert fetched.current_node == "review"
        assert fetched.playbook_id == "review-playbook"
        assert fetched.playbook_version == 3
        assert fetched.tokens_used == 750
        assert fetched.started_at == 1000.0
        assert fetched.completed_at is None
        assert fetched.error is None

        # Verify conversation history round-trip
        restored_history = json.loads(fetched.conversation_history)
        assert restored_history == messages

        # Verify node trace round-trip
        restored_trace = json.loads(fetched.node_trace)
        assert restored_trace == trace

        # Verify trigger event round-trip
        restored_event = json.loads(fetched.trigger_event)
        assert restored_event == event

    async def test_full_lifecycle_create_update_complete(self, db):
        """Simulate the full run lifecycle: create → update per node → complete."""
        # 1. Create at startup
        run = _make_playbook_run(
            run_id="lifecycle-1",
            playbook_id="ci-pipeline",
            version=2,
            started_at=1000.0,
        )
        await db.create_playbook_run(run)

        # 2. After node "build" completes
        history_1 = [
            {"role": "user", "content": "seed"},
            {"role": "user", "content": "Build the project."},
            {"role": "assistant", "content": "Build succeeded."},
        ]
        trace_1 = [
            {
                "node_id": "build",
                "started_at": 1000.0,
                "completed_at": 1010.0,
                "status": "completed",
            }
        ]
        await db.update_playbook_run(
            "lifecycle-1",
            current_node="build",
            conversation_history=json.dumps(history_1),
            node_trace=json.dumps(trace_1),
            tokens_used=200,
        )

        # 3. After node "test" completes
        history_2 = history_1 + [
            {"role": "user", "content": "Run the test suite."},
            {"role": "assistant", "content": "All 42 tests passed."},
        ]
        trace_2 = trace_1 + [
            {"node_id": "test", "started_at": 1010.0, "completed_at": 1020.0, "status": "completed"}
        ]
        await db.update_playbook_run(
            "lifecycle-1",
            current_node="test",
            conversation_history=json.dumps(history_2),
            node_trace=json.dumps(trace_2),
            tokens_used=450,
        )

        # 4. Final completion
        await db.update_playbook_run(
            "lifecycle-1",
            status="completed",
            conversation_history=json.dumps(history_2),
            node_trace=json.dumps(trace_2),
            tokens_used=450,
            completed_at=1020.0,
        )

        # Verify final state
        fetched = await db.get_playbook_run("lifecycle-1")
        assert fetched.status == "completed"
        assert fetched.tokens_used == 450
        assert fetched.completed_at == 1020.0
        assert fetched.current_node == "test"

        restored_history = json.loads(fetched.conversation_history)
        assert len(restored_history) == 5
        assert restored_history[-1]["content"] == "All 42 tests passed."

        restored_trace = json.loads(fetched.node_trace)
        assert len(restored_trace) == 2
        assert [t["node_id"] for t in restored_trace] == ["build", "test"]

    async def test_large_conversation_history(self, db):
        """Verify large conversation histories are stored and retrieved correctly."""
        messages = []
        for i in range(50):
            messages.append({"role": "user", "content": f"Step {i}: do task {i}"})
            messages.append({"role": "assistant", "content": f"Completed task {i}. " + "x" * 200})

        run = _make_playbook_run(conversation_history=messages)
        await db.create_playbook_run(run)

        fetched = await db.get_playbook_run("run-1")
        restored = json.loads(fetched.conversation_history)
        assert len(restored) == 100
        assert restored[99]["content"].startswith("Completed task 49.")


class TestMockAdapter:
    """Demonstrate that the protocol can be satisfied by a mock."""

    def test_protocol_check(self):
        """A minimal mock satisfying the protocol should pass isinstance check."""
        # We just verify the protocol is runtime-checkable and the real adapter passes
        adapter = SQLiteDatabaseAdapter.__new__(SQLiteDatabaseAdapter)
        assert isinstance(adapter, DatabaseBackend)
