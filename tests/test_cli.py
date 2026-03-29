"""Unit tests for the AgentQueue CLI.

Tests CLI commands, formatters, and client operations against an
in-memory SQLite database.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.cli.client import CLIClient
from src.cli.formatters import (
    format_agent_table,
    format_hook_table,
    format_project_table,
    format_status_overview,
    format_task_detail,
    format_task_table,
)
from src.cli.styles import STATUS_ICONS, STATUS_STYLES, priority_style
from src.database import Database
from src.models import (
    Agent,
    AgentState,
    Hook,
    Project,
    ProjectStatus,
    Task,
    TaskStatus,
    TaskType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database file path."""
    return str(tmp_path / "test.db")


@pytest.fixture
async def db(tmp_db):
    """Initialize a temporary database with test data."""
    database = Database(tmp_db)
    await database.initialize()

    # Create test project
    project = Project(
        id="test-project",
        name="Test Project",
        credit_weight=1.0,
        max_concurrent_agents=2,
        status=ProjectStatus.ACTIVE,
        total_tokens_used=1000,
    )
    await database.create_project(project)

    # Create agents FIRST (tasks have FK references to agents)
    agents = [
        Agent(
            id="agent-1",
            name="Claude Worker 1",
            agent_type="claude",
            state=AgentState.BUSY,
            current_task_id=None,  # Set after task creation
            session_tokens_used=5000,
            total_tokens_used=50000,
        ),
        Agent(
            id="agent-2",
            name="Claude Worker 2",
            agent_type="claude",
            state=AgentState.IDLE,
            total_tokens_used=30000,
        ),
    ]
    for a in agents:
        await database.create_agent(a)

    # Create test tasks (after agents exist for FK)
    tasks = [
        Task(
            id="task-alpha",
            project_id="test-project",
            title="Implement feature A",
            description="Build the first feature",
            priority=100,
            status=TaskStatus.IN_PROGRESS,
            task_type=TaskType.FEATURE,
            assigned_agent_id="agent-1",
        ),
        Task(
            id="task-beta",
            project_id="test-project",
            title="Fix critical bug",
            description="The login page crashes on mobile",
            priority=200,
            status=TaskStatus.READY,
            task_type=TaskType.BUGFIX,
        ),
        Task(
            id="task-gamma",
            project_id="test-project",
            title="Write documentation",
            description="Document the API endpoints",
            priority=50,
            status=TaskStatus.COMPLETED,
            task_type=TaskType.DOCS,
        ),
        Task(
            id="task-delta",
            project_id="test-project",
            title="Awaiting approval task",
            description="Needs human review",
            priority=100,
            status=TaskStatus.AWAITING_APPROVAL,
        ),
    ]
    for t in tasks:
        await database.create_task(t)

    # Update agent-1's current_task_id now that task exists
    await database.update_agent("agent-1", current_task_id="task-alpha")

    # Create test hook
    hook = Hook(
        id="hook-daily",
        project_id="test-project",
        name="Daily Review",
        enabled=True,
        trigger='{"type": "periodic", "interval": 86400}',
        context_steps='[]',
        prompt_template="Review all tasks",
        cooldown_seconds=3600,
        created_at=1700000000.0,
        updated_at=1700000000.0,
    )
    await database.create_hook(hook)

    yield database
    await database.close()


@pytest.fixture
def client(tmp_db, db):
    """Create a CLIClient pointing to the test database."""
    return CLIClient(db_path=tmp_db)


@pytest.fixture
def runner():
    """Create a Click test runner."""
    return CliRunner()


# ---------------------------------------------------------------------------
# Style tests
# ---------------------------------------------------------------------------


class TestStyles:
    def test_status_icons_complete(self):
        """Every TaskStatus should have an icon."""
        for status in TaskStatus:
            assert status.value in STATUS_ICONS, f"Missing icon for {status.value}"

    def test_status_styles_complete(self):
        """Every TaskStatus should have a style."""
        for status in TaskStatus:
            assert status.value in STATUS_STYLES, f"Missing style for {status.value}"

    def test_priority_style_ranges(self):
        assert "red" in priority_style(200)
        assert "yellow" in priority_style(150)
        assert "white" in priority_style(100)
        assert "dim" in priority_style(30)


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------


class TestCLIClient:
    async def test_list_tasks(self, client):
        async with client:
            tasks = await client.list_tasks()
            assert len(tasks) == 4

    async def test_list_tasks_by_project(self, client):
        async with client:
            tasks = await client.list_tasks(project_id="test-project")
            assert len(tasks) == 4

    async def test_list_tasks_active_only(self, client):
        async with client:
            tasks = await client.list_tasks(active_only=True)
            # Should exclude COMPLETED
            assert all(t.status != TaskStatus.COMPLETED for t in tasks)

    async def test_list_tasks_by_status(self, client):
        async with client:
            tasks = await client.list_tasks(status=TaskStatus.READY)
            assert len(tasks) == 1
            assert tasks[0].id == "task-beta"

    async def test_get_task(self, client):
        async with client:
            t = await client.get_task("task-alpha")
            assert t is not None
            assert t.title == "Implement feature A"

    async def test_get_task_not_found(self, client):
        async with client:
            t = await client.get_task("nonexistent")
            assert t is None

    async def test_search_tasks(self, client):
        async with client:
            results = await client.search_tasks("bug")
            assert len(results) == 1
            assert results[0].id == "task-beta"

    async def test_search_tasks_case_insensitive(self, client):
        async with client:
            results = await client.search_tasks("FEATURE")
            assert len(results) == 1
            assert results[0].id == "task-alpha"

    async def test_create_task(self, client):
        async with client:
            t = await client.create_task(
                project_id="test-project",
                title="New task",
                description="A brand new task",
                priority=150,
                task_type="feature",
            )
            assert t.id  # Should have generated an ID
            assert t.title == "New task"
            assert t.status == TaskStatus.DEFINED

            # Verify persisted
            fetched = await client.get_task(t.id)
            assert fetched is not None
            assert fetched.title == "New task"

    async def test_list_agents(self, client):
        async with client:
            agents = await client.list_agents()
            assert len(agents) == 2

    async def test_list_projects(self, client):
        async with client:
            projects = await client.list_projects()
            assert len(projects) == 1
            assert projects[0].id == "test-project"

    async def test_list_hooks(self, client):
        async with client:
            hooks = await client.list_hooks()
            assert len(hooks) == 1
            assert hooks[0].name == "Daily Review"

    async def test_count_tasks_by_status(self, client):
        async with client:
            counts = await client.count_tasks_by_status()
            assert counts.get("IN_PROGRESS", 0) == 1
            assert counts.get("READY", 0) == 1
            assert counts.get("COMPLETED", 0) == 1

    async def test_client_file_not_found(self):
        client = CLIClient(db_path="/nonexistent/path.db")
        with pytest.raises(FileNotFoundError):
            await client.connect()


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------


class TestFormatters:
    def test_format_task_table(self):
        tasks = [
            Task(
                id="t1",
                project_id="proj",
                title="Test task",
                description="desc",
                priority=100,
                status=TaskStatus.IN_PROGRESS,
                task_type=TaskType.FEATURE,
            ),
        ]
        table = format_task_table(tasks)
        assert table.title == "Tasks"
        assert table.row_count == 1

    def test_format_task_table_empty(self):
        table = format_task_table([], title="Empty")
        assert table.title == "Empty"

    def test_format_task_detail(self):
        task = Task(
            id="t1",
            project_id="proj",
            title="Detailed task",
            description="Full description\nwith multiple lines",
            priority=150,
            status=TaskStatus.IN_PROGRESS,
            task_type=TaskType.BUGFIX,
            assigned_agent_id="agent-1",
            branch_name="fix/bug-123",
        )
        panel = format_task_detail(
            task,
            deps_on=["dep-1"],
            dependents=["dep-2"],
            subtask_stats=(2, 5),
        )
        assert panel.title is not None

    def test_format_agent_table(self):
        agents = [
            Agent(
                id="a1",
                name="Worker",
                agent_type="claude",
                state=AgentState.BUSY,
                current_task_id="t1",
                session_tokens_used=1000,
            ),
        ]
        table = format_agent_table(agents)
        assert table.row_count == 1

    def test_format_hook_table(self):
        hooks = [
            Hook(
                id="h1",
                project_id="proj",
                name="My Hook",
                enabled=True,
                trigger='{"type": "periodic"}',
                context_steps="[]",
                prompt_template="do stuff",
                cooldown_seconds=60,
                created_at=1700000000.0,
                updated_at=1700000000.0,
            ),
        ]
        table = format_hook_table(hooks)
        assert table.row_count == 1

    def test_format_project_table(self):
        projects = [
            Project(
                id="p1",
                name="Project 1",
                status=ProjectStatus.ACTIVE,
                max_concurrent_agents=3,
            ),
        ]
        table = format_project_table(projects)
        assert table.row_count == 1

    def test_format_status_overview(self):
        projects = [Project(id="p1", name="P1", status=ProjectStatus.ACTIVE)]
        agents = [
            Agent(id="a1", name="W1", agent_type="claude", state=AgentState.BUSY),
            Agent(id="a2", name="W2", agent_type="claude", state=AgentState.IDLE),
        ]
        counts = {"IN_PROGRESS": 3, "READY": 5, "COMPLETED": 10}
        panel = format_status_overview(projects, agents, counts)
        assert panel.title is not None


# ---------------------------------------------------------------------------
# CLI command tests (Click runner)
# ---------------------------------------------------------------------------


class TestCLICommands:
    """Test CLI commands using Click's CliRunner.

    These tests patch the database path to use the test DB fixture.
    """

    def test_version(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_help(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "AgentQueue CLI" in result.output

    def test_task_help(self, runner):
        result = runner.invoke(cli, ["task", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "create" in result.output
        assert "details" in result.output
        assert "approve" in result.output
        assert "stop" in result.output
        assert "restart" in result.output
        assert "search" in result.output

    def test_agent_help(self, runner):
        result = runner.invoke(cli, ["agent", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output

    def test_hook_help(self, runner):
        result = runner.invoke(cli, ["hook", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "runs" in result.output

    def test_project_help(self, runner):
        result = runner.invoke(cli, ["project", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output

    def test_status_missing_db(self, runner):
        """Status should fail gracefully when DB doesn't exist."""
        result = runner.invoke(cli, ["--db", "/nonexistent/db.sqlite", "status"])
        assert result.exit_code != 0

    def test_task_list_with_db(self, runner, tmp_db, db):
        """task list should work against the test database."""
        result = runner.invoke(cli, ["--db", tmp_db, "task", "list", "--all"])
        assert result.exit_code == 0
        assert "task-alpha" in result.output or "Tasks" in result.output

    def test_task_list_active(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "task", "list"])
        assert result.exit_code == 0

    def test_task_list_by_status(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "task", "list", "-s", "READY"])
        assert result.exit_code == 0

    def test_task_list_by_project(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "task", "list", "-p", "test-project"])
        assert result.exit_code == 0

    def test_task_details(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "task", "details", "task-alpha"])
        assert result.exit_code == 0
        assert "Implement feature A" in result.output

    def test_task_details_not_found(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "task", "details", "nonexistent"])
        assert result.exit_code != 0

    def test_task_search(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "task", "search", "bug"])
        assert result.exit_code == 0

    def test_task_search_no_results(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "task", "search", "zzzznotfound"])
        assert result.exit_code == 0
        assert "No tasks matched" in result.output

    def test_task_create_flags(self, runner, tmp_db, db):
        """Create a task using CLI flags (non-interactive)."""
        result = runner.invoke(cli, [
            "--db", tmp_db,
            "task", "create",
            "-p", "test-project",
            "-t", "CLI-created task",
            "-d", "Created via CLI test",
            "--priority", "150",
            "--type", "feature",
        ])
        assert result.exit_code == 0
        assert "Task created" in result.output

    def test_task_approve(self, runner, tmp_db, db):
        result = runner.invoke(cli, [
            "--db", tmp_db,
            "task", "approve", "task-delta", "-y",
        ])
        assert result.exit_code == 0
        assert "approved" in result.output

    def test_task_stop(self, runner, tmp_db, db):
        result = runner.invoke(cli, [
            "--db", tmp_db,
            "task", "stop", "task-alpha", "-y",
        ])
        assert result.exit_code == 0
        assert "stopped" in result.output

    def test_task_restart(self, runner, tmp_db, db):
        result = runner.invoke(cli, [
            "--db", tmp_db,
            "task", "restart", "task-gamma", "-y",
        ])
        assert result.exit_code == 0
        assert "restarted" in result.output

    def test_agent_list(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "agent", "list"])
        assert result.exit_code == 0

    def test_agent_details(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "agent", "details", "agent-1"])
        assert result.exit_code == 0
        assert "Claude Worker 1" in result.output

    def test_hook_list(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "hook", "list"])
        assert result.exit_code == 0

    def test_hook_details(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "hook", "details", "hook-daily"])
        assert result.exit_code == 0
        assert "Daily Review" in result.output

    def test_project_list(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "project", "list"])
        assert result.exit_code == 0

    def test_project_details(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "project", "details", "test-project"])
        assert result.exit_code == 0
        assert "Test Project" in result.output

    def test_status_with_db(self, runner, tmp_db, db):
        result = runner.invoke(cli, ["--db", tmp_db, "status"])
        assert result.exit_code == 0
        assert "AgentQueue Status" in result.output
