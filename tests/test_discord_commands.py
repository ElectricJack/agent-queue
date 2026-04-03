import pytest
from src.models import (
    Task,
    Agent,
    AgentOutput,
    AgentResult,
    TaskStatus,
    AgentState,
    Workspace,
    RepoSourceType,
)
from src.discord.notifications import (
    format_task_started,
    format_task_completed,
    format_task_failed,
    format_task_blocked,
    format_agent_question,
    format_budget_warning,
    format_pr_created,
    format_chain_stuck,
    format_stuck_defined_task,
    format_task_started_embed,
)


class TestNotificationFormatting:
    def test_format_task_started(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Add feature",
            description="D",
            branch_name="feat/add-feature",
        )
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        result = format_task_started(task, agent)
        assert "Task Started" in result
        assert "t-1" in result
        assert "Add feature" in result
        assert "p-1" in result
        assert "claude-1" in result
        assert "feat/add-feature" in result
        assert "IN_PROGRESS" in result

    def test_format_task_started_no_branch(self):
        task = Task(id="t-2", project_id="p-1", title="Quick fix", description="D")
        agent = Agent(id="a-1", name="claude-2", agent_type="claude")
        result = format_task_started(task, agent)
        assert "Task Started" in result
        assert "t-2" in result
        assert "claude-2" in result
        assert "Branch" not in result

    def test_format_task_started_with_workspace(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Add feature",
            description="D",
            branch_name="feat/add-feature",
        )
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        ws = Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path="/home/user/workspaces/ws-1",
            source_type=RepoSourceType.CLONE,
            name="workspace-alpha",
        )
        result = format_task_started(task, agent, workspace=ws)
        assert "workspace-alpha" in result
        assert "Workspace" in result

    def test_format_task_started_with_workspace_no_name(self):
        task = Task(id="t-1", project_id="p-1", title="Add feature", description="D")
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        ws = Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path="/home/user/workspaces/ws-1",
            source_type=RepoSourceType.CLONE,
        )
        result = format_task_started(task, agent, workspace=ws)
        assert "/home/user/workspaces/ws-1" in result
        assert "Workspace" in result

    def test_format_task_started_embed(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Add feature",
            description="D",
            branch_name="feat/add-feature",
        )
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        embed = format_task_started_embed(task, agent)
        assert embed is not None
        assert "Task Started" in embed.title
        assert "Add feature" in embed.title
        # Verify fields contain expected values
        field_names = [f.name for f in embed.fields]
        field_values = [f.value for f in embed.fields]
        assert "Task ID" in field_names
        assert "Agent" in field_names
        assert "Status" in field_names
        assert "Branch" in field_names
        assert "`t-1`" in field_values
        assert "claude-1" in field_values
        assert "`feat/add-feature`" in field_values
        # Verify IN_PROGRESS status color (amber = 0xF39C12)
        assert embed.color.value == 0xF39C12

    def test_format_task_started_embed_no_branch(self):
        task = Task(id="t-2", project_id="p-1", title="Quick fix", description="D")
        agent = Agent(id="a-1", name="claude-2", agent_type="claude")
        embed = format_task_started_embed(task, agent)
        field_names = [f.name for f in embed.fields]
        assert "Branch" not in field_names
        assert len(embed.fields) == 4  # Task ID, Project, Agent, Status

    def test_format_task_started_embed_with_workspace(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Add feature",
            description="D",
            branch_name="feat/add-feature",
        )
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        ws = Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path="/home/user/workspaces/ws-1",
            source_type=RepoSourceType.CLONE,
            name="workspace-alpha",
        )
        embed = format_task_started_embed(task, agent, workspace=ws)
        field_names = [f.name for f in embed.fields]
        field_values = [f.value for f in embed.fields]
        assert "Workspace" in field_names
        assert "`workspace-alpha`" in field_values

    def test_format_task_started_embed_with_workspace_no_name(self):
        task = Task(id="t-1", project_id="p-1", title="Add feature", description="D")
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        ws = Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path="/home/user/workspaces/ws-1",
            source_type=RepoSourceType.CLONE,
        )
        embed = format_task_started_embed(task, agent, workspace=ws)
        field_names = [f.name for f in embed.fields]
        field_values = [f.value for f in embed.fields]
        assert "Workspace" in field_names
        assert "`/home/user/workspaces/ws-1`" in field_values

    def test_format_task_completed(self):
        task = Task(id="t-1", project_id="p-1", title="Fix bug", description="D")
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        output = AgentOutput(
            result=AgentResult.COMPLETED,
            summary="Fixed it",
            tokens_used=5000,
            files_changed=["src/foo.py"],
        )
        result = format_task_completed(task, agent, output)
        assert "t-1" in result
        assert "Fix bug" in result
        assert "p-1" in result  # project context
        assert "claude-1" in result
        assert "5,000" in result
        assert "src/foo.py" in result

    def test_format_task_failed(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Broken",
            description="D",
            retry_count=1,
            max_retries=3,
        )
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        output = AgentOutput(result=AgentResult.FAILED, error_message="Syntax error in foo.py")
        result = format_task_failed(task, agent, output)
        assert "Failed" in result
        assert "p-1" in result  # project context
        assert "1/3" in result
        assert "Syntax error" in result

    def test_format_task_blocked(self):
        task = Task(id="t-1", project_id="p-1", title="Stuck", description="D", max_retries=3)
        result = format_task_blocked(task)
        assert "Blocked" in result
        assert "p-1" in result  # project context
        assert "Manual intervention" in result

    def test_format_agent_question(self):
        task = Task(id="t-1", project_id="p-1", title="Auth", description="D")
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        result = format_agent_question(task, agent, "Which auth provider?")
        assert "Question" in result
        assert "p-1" in result  # project context
        assert "claude-1" in result  # agent name
        assert "Which auth provider?" in result

    def test_format_budget_warning(self):
        result = format_budget_warning("alpha", 80000, 100000)
        assert "80%" in result
        assert "alpha" in result

    def test_format_pr_created_includes_project(self):
        task = Task(id="t-1", project_id="p-1", title="Add feature", description="D")
        result = format_pr_created(task, "https://github.com/org/repo/pull/42")
        assert "PR Created" in result
        assert "p-1" in result  # project context
        assert "https://github.com/org/repo/pull/42" in result

    def test_format_chain_stuck_includes_project(self):
        blocked = Task(
            id="t-1", project_id="p-1", title="Blocker", description="D", status=TaskStatus.BLOCKED
        )
        stuck = [
            Task(
                id="t-2",
                project_id="p-1",
                title="Downstream 1",
                description="D",
                status=TaskStatus.DEFINED,
            ),
        ]
        result = format_chain_stuck(blocked, stuck)
        assert "Chain Stuck" in result
        assert "t-2" in result

    def test_format_stuck_defined_task_includes_project(self):
        task = Task(
            id="t-1", project_id="p-1", title="Waiting", description="D", status=TaskStatus.DEFINED
        )
        blocking = [("t-0", "Blocker", "BLOCKED")]
        result = format_stuck_defined_task(task, blocking, stuck_hours=3.5)
        assert "Stuck" in result
        assert "3.5" in result


class TestProjectContextPrefixing:
    """Verify the bot's static _prepend_project_tag helper."""

    def test_prepend_project_tag(self):
        from src.discord.bot import AgentQueueBot

        result = AgentQueueBot._prepend_project_tag("**Task Started:** `t-1`", "my-project")
        assert result == "[`my-project`] **Task Started:** `t-1`"
