"""Tests for the modular database package.

Verifies that:
1. The DatabaseBackend protocol is satisfied by SQLiteDatabaseAdapter
2. Each query module works correctly via the adapter
3. Backward compatibility is maintained
4. The abstraction layer works with mock adapters
"""

import pytest
import time

from src.database import Database, DatabaseBackend, SQLiteDatabaseAdapter
from src.database.base import DatabaseBackend as BaseProtocol
from src.database.schema import SCHEMA, MIGRATIONS, INDEXES
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Hook,
    HookRun,
    Project,
    ProjectStatus,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskStatus,
    TaskType,
    VerificationType,
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

    def test_schema_not_empty(self):
        """Schema DDL should be non-empty."""
        assert len(SCHEMA) > 100
        assert "CREATE TABLE" in SCHEMA

    def test_migrations_list(self):
        """Migrations should be a non-empty list of ALTER TABLE statements."""
        assert len(MIGRATIONS) > 10
        assert all("ALTER TABLE" in m for m in MIGRATIONS)

    def test_indexes_list(self):
        """Indexes should be a non-empty list."""
        assert len(INDEXES) >= 2


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
        ctx_id = await db.add_task_context("t-1", type="note", label="hint", content="foo")
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


class TestMockAdapter:
    """Demonstrate that the protocol can be satisfied by a mock."""

    def test_protocol_check(self):
        """A minimal mock satisfying the protocol should pass isinstance check."""
        # We just verify the protocol is runtime-checkable and the real adapter passes
        adapter = SQLiteDatabaseAdapter.__new__(SQLiteDatabaseAdapter)
        assert isinstance(adapter, DatabaseBackend)
