from src.models import (
    TaskStatus, TaskEvent, AgentState, AgentResult,
    ProjectStatus, VerificationType,
    Project, Task, Agent, RepoConfig, TaskContext, AgentOutput,
)


class TestTaskStatus:
    def test_all_states_exist(self):
        expected = {
            "DEFINED", "READY", "ASSIGNED", "IN_PROGRESS",
            "WAITING_INPUT", "PAUSED",
            "AWAITING_APPROVAL", "AWAITING_PLAN_APPROVAL",
            "COMPLETED", "FAILED", "BLOCKED",
        }
        assert {s.value for s in TaskStatus} == expected


class TestTaskEvent:
    def test_all_events_exist(self):
        expected = {
            "DEPS_MET", "ASSIGNED", "AGENT_STARTED",
            "AGENT_COMPLETED", "AGENT_FAILED", "TOKENS_EXHAUSTED",
            "AGENT_QUESTION", "HUMAN_REPLIED", "INPUT_TIMEOUT",
            "RESUME_TIMER",
            "PR_CREATED", "PR_MERGED",
            "RETRY", "MAX_RETRIES",
            "MERGE_FAILED", "MERGE_SUCCEEDED",
            # Administrative / recovery events
            "ADMIN_SKIP", "ADMIN_STOP", "ADMIN_RESTART",
            "PR_CLOSED", "PLAN_FOUND", "PLAN_APPROVED",
            "PLAN_REJECTED", "PLAN_DELETED",
            "TIMEOUT", "EXECUTION_ERROR", "RECOVERY",
        }
        assert {e.value for e in TaskEvent} == expected


class TestAgentState:
    def test_all_states_exist(self):
        expected = {"IDLE", "BUSY", "PAUSED", "ERROR"}
        assert {s.value for s in AgentState} == expected


class TestTask:
    def test_create_minimal_task(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Do something",
            description="Details here",
        )
        assert task.status == TaskStatus.DEFINED
        assert task.priority == 100
        assert task.retry_count == 0
        assert task.max_retries == 3
        assert task.parent_task_id is None

    def test_task_fields(self):
        task = Task(
            id="t-2",
            project_id="p-1",
            title="Test",
            description="Desc",
            priority=50,
            verification_type=VerificationType.HUMAN,
        )
        assert task.priority == 50
        assert task.verification_type == VerificationType.HUMAN


class TestAgent:
    def test_create_agent(self):
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        assert agent.state == AgentState.IDLE
        assert agent.current_task_id is None
        assert agent.total_tokens_used == 0


class TestProject:
    def test_create_project(self):
        project = Project(id="p-1", name="alpha")
        assert project.credit_weight == 1.0
        assert project.max_concurrent_agents == 2
        assert project.status == ProjectStatus.ACTIVE
        assert project.budget_limit is None
