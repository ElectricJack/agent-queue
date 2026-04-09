"""Shared data model types for the agent-queue system.

This module is the shared vocabulary of the entire system. Every component —
orchestrator, scheduler, database, Discord bot, agent adapters — communicates
through the enums and dataclasses defined here. Keeping them in one place
prevents circular imports and ensures a single source of truth for the
structure of tasks, agents, projects, and hooks.

See specs/models-and-state-machine.md for the full behavioral specification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    """The states a task can occupy in the orchestrator's state machine.

    These map directly to the state machine defined in VALID_TASK_TRANSITIONS
    (see src/state_machine.py). The orchestrator's main loop drives tasks
    through these states based on events like dependency resolution, agent
    completion, rate limiting, and human approval.

    Note: transitions are not enforced by the state machine in production —
    the orchestrator writes directly via db.update_task(). The state machine
    module is used only for validation logging. See specs/models-and-state-machine.md.
    """

    DEFINED = "DEFINED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_INPUT = "WAITING_INPUT"
    PAUSED = "PAUSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskEvent(Enum):
    """Events that trigger transitions between TaskStatus states.

    These are grouped into: core lifecycle events (DEPS_MET through PR_MERGED),
    retry/failure events (RETRY, MAX_RETRIES), administrative overrides
    (ADMIN_SKIP, ADMIN_STOP, ADMIN_RESTART), and error recovery events
    (PR_CLOSED, TIMEOUT, EXECUTION_ERROR, RECOVERY). Each (TaskStatus, TaskEvent)
    pair maps to exactly one target TaskStatus in the transitions table.
    """

    DEPS_MET = "DEPS_MET"
    ASSIGNED = "ASSIGNED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    TOKENS_EXHAUSTED = "TOKENS_EXHAUSTED"
    AGENT_QUESTION = "AGENT_QUESTION"
    HUMAN_REPLIED = "HUMAN_REPLIED"
    INPUT_TIMEOUT = "INPUT_TIMEOUT"
    RESUME_TIMER = "RESUME_TIMER"
    PR_CREATED = "PR_CREATED"
    PR_MERGED = "PR_MERGED"
    RETRY = "RETRY"
    MAX_RETRIES = "MAX_RETRIES"
    MERGE_FAILED = "MERGE_FAILED"
    MERGE_SUCCEEDED = "MERGE_SUCCEEDED"
    # Administrative / recovery events
    ADMIN_SKIP = "ADMIN_SKIP"
    ADMIN_STOP = "ADMIN_STOP"
    ADMIN_RESTART = "ADMIN_RESTART"
    PR_CLOSED = "PR_CLOSED"
    PLAN_FOUND = "PLAN_FOUND"
    PLAN_APPROVED = "PLAN_APPROVED"
    PLAN_REJECTED = "PLAN_REJECTED"
    PLAN_DELETED = "PLAN_DELETED"
    SUBTASKS_COMPLETED = "SUBTASKS_COMPLETED"
    TIMEOUT = "TIMEOUT"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    RECOVERY = "RECOVERY"


class TaskType(Enum):
    """Categorizes the kind of work a task represents.

    Used by the Discord UI to display type-specific emoji tags and by the
    chat agent to help the LLM understand the nature of each task at a
    glance. The plan parser can auto-assign a type when creating subtasks
    from a plan file.

    Values are lowercase strings stored directly in the ``task_type`` column.
    """

    FEATURE = "feature"
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    TEST = "test"
    DOCS = "docs"
    CHORE = "chore"
    RESEARCH = "research"
    PLAN = "plan"
    SYNC = "sync"


# Convenience set for validation without constructing enum members.
TASK_TYPE_VALUES = frozenset(t.value for t in TaskType)


class AgentState(Enum):
    """Tracks the runtime state of an agent process from the orchestrator's perspective.

    .. deprecated::
        Legacy enum — will be removed once the orchestrator is fully migrated
        to the workspace-as-agent model.  New code should use workspace
        lock state (locked = busy, unlocked = idle) instead.
    """

    IDLE = "IDLE"
    BUSY = "BUSY"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class AgentResult(Enum):
    """The outcome reported by an agent adapter when a task execution finishes.

    The orchestrator maps these to TaskEvents: COMPLETED and FAILED are
    straightforward; PAUSED_TOKENS and PAUSED_RATE_LIMIT cause the task to
    enter PAUSED with a resume_after timestamp, allowing the orchestrator to
    automatically retry once the rate limit window or token budget resets.
    WAITING_INPUT indicates the agent is blocked on a human question —
    the task transitions to WAITING_INPUT and a notification is sent.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED_TOKENS = "paused_tokens"
    PAUSED_RATE_LIMIT = "paused_rate_limit"
    WAITING_INPUT = "waiting_input"


class ProjectStatus(Enum):
    """Lifecycle state of a project. PAUSED projects are skipped by the scheduler."""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class VerificationType(Enum):
    """How a task's output should be verified before it can move to COMPLETED.

    AUTO_TEST runs test commands from TaskContext; QA_AGENT spawns a separate
    verification agent; HUMAN requires manual approval via Discord.
    """

    AUTO_TEST = "auto_test"
    QA_AGENT = "qa_agent"
    HUMAN = "human"


class RepoSourceType(Enum):
    """How a project's repository was set up — cloned from a URL, linked to
    an existing local path, or initialized as a new git repo."""

    CLONE = "clone"
    LINK = "link"
    INIT = "init"


@dataclass
class RepoConfig:
    """Describes a git repository associated with a project.

    The GitManager uses this to clone, link, or initialize the repo and to
    create per-task worktrees branching from default_branch.

    Repos are purely git config (URL, default branch, source type) — they
    no longer determine filesystem layout. Workspace paths are managed by
    the workspaces table.
    """

    id: str
    project_id: str
    source_type: RepoSourceType
    url: str = ""
    source_path: str = ""
    checkout_base_path: str = ""
    default_branch: str = "main"


@dataclass
class Project:
    """A project is the unit of scheduling and resource allocation.

    The scheduler distributes agent capacity across projects proportionally
    to their credit_weight. Each project may have its own Discord channel
    and token budget. max_concurrent_agents caps how many agents can work
    on this project simultaneously.

    Repo configuration (repo_url, repo_default_branch) is embedded directly
    on the project — one repo per project.  Multiple workspaces per project
    are managed via the Workspace model (see ``workspaces`` table).
    """

    id: str
    name: str
    credit_weight: float = 1.0
    max_concurrent_agents: int = 2
    status: ProjectStatus = ProjectStatus.ACTIVE
    total_tokens_used: int = 0
    budget_limit: int | None = None
    discord_channel_id: str | None = None  # Per-project Discord channel
    repo_url: str = ""
    repo_default_branch: str = "main"
    default_profile_id: str | None = None  # fallback profile for tasks in this project


@dataclass
class Task:
    """The fundamental unit of work in the system.

    A task moves through the TaskStatus state machine from DEFINED to
    COMPLETED (or BLOCKED). It carries everything the orchestrator needs:
    scheduling metadata (priority, project_id), execution context (repo_id,
    branch_name, assigned_agent_id), lifecycle tracking (retry_count,
    resume_after), and plan-generation lineage (parent_task_id, plan_source,
    is_plan_subtask).
    """

    id: str
    project_id: str
    title: str
    description: str
    priority: int = 100
    status: TaskStatus = TaskStatus.DEFINED
    verification_type: VerificationType = VerificationType.AUTO_TEST
    retry_count: int = 0
    max_retries: int = 3
    parent_task_id: str | None = None
    repo_id: str | None = None
    assigned_agent_id: str | None = None
    branch_name: str | None = None
    resume_after: float | None = None  # unix timestamp
    requires_approval: bool = False
    pr_url: str | None = None
    plan_source: str | None = None  # path to archived plan file that generated this task
    is_plan_subtask: bool = False  # True if auto-generated from a plan
    task_type: TaskType | None = None  # categorization: feature, bugfix, refactor, etc.
    profile_id: str | None = None  # which AgentProfile to configure the agent with
    preferred_workspace_id: str | None = (
        None  # hint: use this workspace (e.g. for merge-conflict tasks)
    )
    attachments: list[str] = field(
        default_factory=list
    )  # absolute paths to attached files (images, etc.)
    auto_approve_plan: bool = False  # if True, auto-approve any plan this task generates
    skip_verification: bool = False  # if True, skip git verification on completion


@dataclass
class Agent:
    """Represents a registered agent process (e.g., a Claude Code instance).

    .. deprecated::
        Legacy dataclass — will be removed once the orchestrator is fully
        migrated to the workspace-as-agent model.  New code should use
        :class:`WorkspaceAgent` instead.
    """

    id: str
    name: str
    agent_type: str  # "claude", "codex", "cursor", "aider"
    state: AgentState = AgentState.IDLE
    current_task_id: str | None = None
    pid: int | None = None
    last_heartbeat: float | None = None
    total_tokens_used: int = 0
    session_tokens_used: int = 0


@dataclass
class WorkspaceAgent:
    """A workspace viewed as an agent slot — the new workspace-as-agent model.

    An "agent" is simply a workspace execution context.  Idle (unlocked)
    workspaces are idle agents; locked workspaces are busy agents.  There is
    no separate agent registry — agents are derived from the workspaces table.
    """

    workspace_id: str
    project_id: str
    workspace_name: str | None
    state: str  # "idle" or "busy"
    current_task_id: str | None = None
    current_task_title: str | None = None


@dataclass
class Workspace:
    """A project-scoped workspace directory where agents execute tasks.

    Each project can have multiple workspaces (e.g. separate clones or linked
    directories).  Agents dynamically acquire a workspace lock when assigned a
    task and release it on completion — no manual agent-to-workspace mapping.
    """

    id: str
    project_id: str
    workspace_path: str
    source_type: RepoSourceType  # clone or link (per-workspace)
    name: str | None = None
    locked_by_agent_id: str | None = None
    locked_by_task_id: str | None = None
    locked_at: float | None = None


@dataclass
class AgentProfile:
    """A capability bundle that configures agents for specific task types.

    Profiles define what tools, MCP servers, model overrides, and system prompt
    additions an agent should receive when executing a task.  They are resolved
    at task execution time (not during scheduling) to keep the scheduler
    deterministic and profile-unaware.

    Resolution cascade: task.profile_id → project.default_profile_id → None
    (system default).  See specs/agent-profiles.md.
    """

    id: str  # slug: "reviewer", "web-developer"
    name: str  # display name
    description: str = ""
    model: str = ""  # override model (empty = use default)
    permission_mode: str = ""  # override (empty = use default)
    allowed_tools: list[str] = field(default_factory=list)  # tool whitelist
    mcp_servers: dict[str, dict] = field(default_factory=dict)  # name -> server config
    system_prompt_suffix: str = ""  # appended to agent instructions
    install: dict = field(default_factory=dict)  # auto-install manifest (future)


@dataclass
class TaskContext:
    """The input bundle passed to an agent adapter when executing a task.

    This is the adapter's entire view of the work to be done: what to build
    (description, acceptance_criteria), how to verify it (test_commands),
    where to work (checkout_path, branch_name), and what tools/context are
    available. The orchestrator constructs this from the Task, its criteria,
    context entries, and tool permissions stored in the database.
    """

    description: str
    task_id: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    checkout_path: str = ""
    branch_name: str = ""
    attached_context: list[str] = field(default_factory=list)
    image_paths: list[str] = field(
        default_factory=list
    )  # absolute paths to images the agent should examine
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    resume_session_id: str | None = None  # fork from this session on reopen


@dataclass
class AgentOutput:
    """The result returned by an agent adapter after task execution.

    The orchestrator uses result to determine the next state transition,
    summary for Discord notifications, files_changed for commit/PR decisions,
    and tokens_used for budget tracking. On failure, error_message provides
    context for retry logic. When result is WAITING_INPUT, question contains
    the agent's question for human review.
    """

    result: AgentResult
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    tokens_used: int = 0
    error_message: str | None = None
    exit_code: int | None = None
    question: str | None = None
    session_id: str | None = None


@dataclass
class ProjectFactsheet:
    """Typed access to a project's factsheet YAML frontmatter.

    The factsheet is a structured YAML-frontmatter + markdown file at
    ``memory/{project_id}/factsheet.md`` that serves as the quick-reference
    card for a project.  This dataclass provides typed access to the YAML
    frontmatter fields for programmatic use.

    Fields correspond to the YAML structure defined in
    ``FACTSHEET_SEED_TEMPLATE`` (see ``src/prompts/memory_consolidation.py``).
    """

    raw_yaml: dict[str, Any] = field(default_factory=dict)
    body_markdown: str = ""

    # Convenience accessors for common fields
    @property
    def project_name(self) -> str:
        return self.raw_yaml.get("project", {}).get("name", "")

    @property
    def project_id(self) -> str:
        return self.raw_yaml.get("project", {}).get("id", "")

    @property
    def urls(self) -> dict[str, str | None]:
        return self.raw_yaml.get("urls", {})

    @property
    def tech_stack(self) -> dict[str, Any]:
        return self.raw_yaml.get("tech_stack", {})

    @property
    def contacts(self) -> dict[str, str | None]:
        return self.raw_yaml.get("contacts", {})

    @property
    def key_paths(self) -> dict[str, str | None]:
        return self.raw_yaml.get("key_paths", {})

    @property
    def environments(self) -> list[dict[str, Any]]:
        return self.raw_yaml.get("environments", [])

    @property
    def last_updated(self) -> str:
        return self.raw_yaml.get("last_updated", "")

    def get_field(self, dotted_key: str, default: Any = None) -> Any:
        """Retrieve a nested YAML value using dot notation.

        Example: ``get_field("urls.github")`` returns the GitHub URL.
        """
        keys = dotted_key.split(".")
        current: Any = self.raw_yaml
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return default
            if current is None:
                return default
        return current

    def set_field(self, dotted_key: str, value: Any) -> None:
        """Set a nested YAML value using dot notation.

        Creates intermediate dicts as needed.
        Example: ``set_field("urls.github", "https://github.com/user/repo")``
        """
        keys = dotted_key.split(".")
        current = self.raw_yaml
        for key in keys[:-1]:
            if key not in current or not isinstance(current.get(key), dict):
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value


@dataclass
class MemoryContext:
    """Structured memory context with tiered priority for agent injection.

    Each field contains pre-formatted markdown text ready for injection into
    the agent's context. The orchestrator assembles these tiers in priority
    order (factsheet first, then profile, topic context, notes, recent tasks,
    and semantic search) and trims to fit the configured token budget.
    """

    factsheet: str = ""  # Project factsheet (Tier 0, highest priority — always included)
    profile: str = ""  # Project profile (Tier 1, always included)
    project_docs: str = ""  # Project documentation (CLAUDE.md etc., Tier 1.5)
    topic_context: str = ""  # L2 topic-filtered knowledge (Tier 2, on-demand by topic)
    detected_topics: list[str] = field(default_factory=list)  # Topics detected from task context
    notes: str = ""  # Relevant notes matched by semantic search
    recent_tasks: str = ""  # Recent task summaries for continuity
    search_results: str = ""  # Semantic search results (current behavior)
    memory_folder: str = ""  # Path to project memory folder for agent reference
    tasks_folder: str = ""  # Path to task records folder (outside memory tree)

    def to_context_block(self) -> str:
        """Assemble all tiers into a single markdown context block."""
        sections = []
        if self.factsheet:
            sections.append(f"## Project Factsheet\n{self.factsheet}")
        if self.profile:
            sections.append(f"## Project Profile\n{self.profile}")
        if self.project_docs:
            sections.append(f"## Project Documentation\n{self.project_docs}")
        if self.topic_context:
            topic_label = ", ".join(self.detected_topics) if self.detected_topics else "detected"
            sections.append(
                f"## Topic Context ({topic_label})\n"
                "The following knowledge was pre-loaded based on topics detected "
                f"in your task description.\n\n{self.topic_context}"
            )
        if self.notes:
            sections.append(f"## Relevant Notes\n{self.notes}")
        if self.recent_tasks:
            sections.append(f"## Recent Tasks\n{self.recent_tasks}")
        if self.search_results:
            sections.append(f"## Relevant Context from Project Memory\n{self.search_results}")
        if self.memory_folder:
            tasks_ref = f"- **Task memories:** `{self.tasks_folder}`\n" if self.tasks_folder else ""
            sections.append(
                "## Project Memory Reference\n"
                "This project has a memory system with historical context, past decisions, "
                "and institutional knowledge from previous work. The context above was "
                "automatically retrieved based on relevance to your task.\n\n"
                "If you need additional historical context, you can browse markdown files "
                "in the memory folder using the Read tool:\n"
                f"{tasks_ref}"
                f"- **Project profile:** `{self.memory_folder}profile.md`\n"
                f"- **Factsheet:** `{self.memory_folder}factsheet.md`\n"
                f"- **Knowledge base:** `{self.memory_folder}knowledge/` "
                "(topic files: architecture, conventions, decisions, etc.)"
            )
        return "\n\n".join(sections)

    @property
    def is_empty(self) -> bool:
        return not any(
            [
                self.factsheet,
                self.profile,
                self.project_docs,
                self.topic_context,
                self.notes,
                self.recent_tasks,
                self.search_results,
                self.memory_folder,
            ]
        )


@dataclass
class Hook:
    """Definition of an automated hook that runs in response to events or on a schedule.

    Hooks allow project-level automation without manual intervention: they can
    be triggered periodically, by cron, or by task lifecycle events (via the
    EventBus). Each hook defines context-gathering steps, an LLM prompt
    template, and cooldown/budget limits to prevent runaway costs.
    See specs/hooks.md.
    """

    id: str
    project_id: str
    name: str
    enabled: bool = True
    trigger: str = "{}"  # JSON: {"type": "periodic", "interval_seconds": 7200}
    context_steps: str = "[]"  # JSON array of step configs
    prompt_template: str = ""  # Template with {{step_0}}, {{event}} placeholders
    llm_config: str | None = None  # JSON: {"provider": "anthropic", "model": "..."}
    cooldown_seconds: int = 3600
    max_tokens_per_run: int | None = None
    last_triggered_at: float | None = None  # epoch seconds; persisted across restarts
    source_hash: str | None = None  # content hash of source rule for idempotent reconciliation
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class HookRun:
    """A single execution record of a Hook.

    Captures the full lifecycle of one hook invocation: why it fired
    (trigger_reason), what context was gathered, what prompt was sent to the
    LLM, and what actions resulted. Used for auditing and debugging hook
    behavior.
    """

    id: str
    hook_id: str
    project_id: str
    trigger_reason: str  # "periodic", "cron", "event:task_completed", "manual"
    status: str = "running"  # running, completed, failed, skipped
    event_data: str | None = None
    context_results: str | None = None
    prompt_sent: str | None = None
    llm_response: str | None = None
    actions_taken: str | None = None
    skipped_reason: str | None = None
    tokens_used: int = 0
    started_at: float = 0.0
    completed_at: float | None = None


class PhaseResult(Enum):
    """Outcome of a single completion pipeline phase."""

    CONTINUE = "continue"
    STOP = "stop"
    ERROR = "error"


@dataclass
class CompletionPhase:
    """Descriptor for one phase in the completion pipeline."""

    name: str
    builtin: bool = True
    blocking: bool = True


@dataclass
class PipelineContext:
    """Passed through each phase of the completion pipeline."""

    task: Task
    agent: Agent
    output: AgentOutput
    workspace_path: str | None
    workspace_id: str | None
    repo: RepoConfig | None
    default_branch: str = "main"
    project: Project | None = None
    pr_url: str | None = None
    plan_needs_approval: bool = False
    verification_reopened: bool = False
