from src.models import (
    TaskStatus,
    TaskEvent,
    AgentState,
    ProjectStatus,
    VerificationType,
    Project,
    Task,
    Agent,
)


class TestTaskStatus:
    def test_all_states_exist(self):
        expected = {
            "DEFINED",
            "READY",
            "ASSIGNED",
            "IN_PROGRESS",
            "WAITING_INPUT",
            "PAUSED",
            "AWAITING_APPROVAL",
            "AWAITING_PLAN_APPROVAL",
            "COMPLETED",
            "FAILED",
            "BLOCKED",
        }
        assert {s.value for s in TaskStatus} == expected


class TestTaskEvent:
    def test_all_events_exist(self):
        expected = {
            "DEPS_MET",
            "ASSIGNED",
            "AGENT_STARTED",
            "AGENT_COMPLETED",
            "AGENT_FAILED",
            "TOKENS_EXHAUSTED",
            "AGENT_QUESTION",
            "HUMAN_REPLIED",
            "INPUT_TIMEOUT",
            "RESUME_TIMER",
            "PR_CREATED",
            "PR_MERGED",
            "RETRY",
            "MAX_RETRIES",
            "MERGE_FAILED",
            "MERGE_SUCCEEDED",
            # Administrative / recovery events
            "ADMIN_SKIP",
            "ADMIN_STOP",
            "ADMIN_RESTART",
            "PR_CLOSED",
            "PLAN_FOUND",
            "PLAN_APPROVED",
            "PLAN_REJECTED",
            "PLAN_DELETED",
            "SUBTASKS_COMPLETED",
            "TIMEOUT",
            "EXECUTION_ERROR",
            "RECOVERY",
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
        agent = Agent(id="a-1", name="claude-1", profile_id="claude")
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


class TestWorkspacesV2:
    def test_workspace_kind_defaults(self):
        from src.models import SYSTEM_KIND_SCOPE, WorkspaceKind

        k = WorkspaceKind(project_id=SYSTEM_KIND_SCOPE, id="vault")
        assert k.writable is True
        assert k.lockable is True
        assert k.is_git_repo is True
        assert k.auto_attach is False
        assert k.repo_url is None
        assert k.default_lock_mode is None

    def test_workspace_attachment_set_helpers(self):
        from src.models import (
            RepoSourceType,
            ResolvedRequirement,
            SYSTEM_KIND_SCOPE,
            Workspace,
            WorkspaceAttachment,
            WorkspaceAttachmentSet,
            WorkspaceKind,
        )

        kind = WorkspaceKind(project_id=SYSTEM_KIND_SCOPE, id="project-repo")
        ws = Workspace(
            id="w1",
            project_id="p1",
            workspace_path="/tmp/ws1",
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
        req = ResolvedRequirement(kind_id="project-repo", position=0)
        att = WorkspaceAttachment(requirement=req, workspace=ws, kind=kind)
        s = WorkspaceAttachmentSet(attachments=[att])

        assert s.primary_path == "/tmp/ws1"
        assert s.first_of_kind("project-repo") is att
        assert s.first_of_kind("vault") is None
        assert s.by_kind("project-repo") == [att]
        assert att.kind_id == "project-repo"
        assert att.writable is True
        assert att.lockable is True

    def test_primary_path_none_when_no_project_repo(self):
        from src.models import (
            RepoSourceType,
            ResolvedRequirement,
            SYSTEM_KIND_SCOPE,
            Workspace,
            WorkspaceAttachment,
            WorkspaceAttachmentSet,
            WorkspaceKind,
        )

        vault_kind = WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE, id="vault",
            writable=True, lockable=False, is_git_repo=False, auto_attach=True,
        )
        ws = Workspace(
            id="v1", project_id="p1", workspace_path="/vault",
            source_type=RepoSourceType.LINK, kind_id="vault",
        )
        req = ResolvedRequirement(kind_id="vault", position=10000)
        att = WorkspaceAttachment(requirement=req, workspace=ws, kind=vault_kind)
        s = WorkspaceAttachmentSet(attachments=[att])

        assert s.primary_path is None
        assert s.first_of_kind("vault") is att
