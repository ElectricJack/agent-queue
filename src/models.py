"""Shared data model types for the agent-queue system.

This module is the shared vocabulary of the entire system. Every component —
orchestrator, scheduler, database, Discord bot, agent adapters — communicates
through the enums and dataclasses defined here. Keeping them in one place
prevents circular imports and ensures a single source of truth for the
structure of tasks, agents, projects, and hooks.

See specs/models-and-state-machine.md for the full behavioral specification.
"""

from __future__ import annotations

import hashlib
import json
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
    # A pool-worker claim of a READY task (swarm-work-model §10): goes
    # straight to IN_PROGRESS, no ASSIGNED hop for pulled work.
    CLAIMED = "CLAIMED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    TOKENS_EXHAUSTED = "TOKENS_EXHAUSTED"
    AGENT_QUESTION = "AGENT_QUESTION"
    HUMAN_REPLIED = "HUMAN_REPLIED"
    INPUT_TIMEOUT = "INPUT_TIMEOUT"
    RESUME_TIMER = "RESUME_TIMER"
    RETRY = "RETRY"
    MAX_RETRIES = "MAX_RETRIES"
    MERGE_FAILED = "MERGE_FAILED"
    MERGE_SUCCEEDED = "MERGE_SUCCEEDED"
    # Administrative / recovery events
    ADMIN_SKIP = "ADMIN_SKIP"
    ADMIN_STOP = "ADMIN_STOP"
    ADMIN_RESTART = "ADMIN_RESTART"
    SUBTASKS_COMPLETED = "SUBTASKS_COMPLETED"
    # A ``conditional-blocks`` contingency whose dependency succeeded: the
    # edge can never fire again, so the task is disposed of as a no-op
    # (work-graph design §3.1).
    CONDITIONAL_DEAD = "CONDITIONAL_DEAD"
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


# ── Integration policy ─────────────────────────────────────────────────────
#
# How a task's finished work reaches the project's default branch:
#
# ``direct``       — the completion pipeline merges the task branch into the
#                    default branch (and pushes) as soon as the worker task
#                    completes.  For projects with zero review policy (docs,
#                    sandboxes).
# ``pull_request`` — the worker pushes its branch and opens a PR; the task
#                    completes *unmerged*.  Review tasks and ``pr-merged``
#                    gates (default-pipeline playbook) decide when the work
#                    lands.
#
# The value is resolved through a policy chain — task override → project
# policy → config ``integration.default_mode`` — via
# :func:`resolve_integration_mode`.  ``None`` at any level means "inherit".
INTEGRATION_MODE_DIRECT = "direct"
INTEGRATION_MODE_PULL_REQUEST = "pull_request"
INTEGRATION_MODES = frozenset({INTEGRATION_MODE_DIRECT, INTEGRATION_MODE_PULL_REQUEST})


def resolve_integration_mode_with_source(
    task_mode: str | None,
    *,
    parent_task_mode: str | None = None,
    project_mode: str | None = None,
    default_mode: str = INTEGRATION_MODE_PULL_REQUEST,
) -> tuple[str, str]:
    """Resolve the effective integration mode and where it came from.

    Chain: plan-subtask parent's task-level override (a plan's subtasks all
    integrate the way the plan does) → task override → project policy →
    system default.  Unknown values fall through to the next level so a
    corrupted row degrades to policy rather than crashing the pipeline.

    Returns ``(mode, source)`` where source is one of ``"parent"``,
    ``"task"``, ``"project"``, ``"default"``.
    """
    for candidate, source in (
        (parent_task_mode, "parent"),
        (task_mode, "task"),
        (project_mode, "project"),
    ):
        if candidate in INTEGRATION_MODES:
            return candidate, source
    if default_mode in INTEGRATION_MODES:
        return default_mode, "default"
    return INTEGRATION_MODE_PULL_REQUEST, "default"


def resolve_integration_mode(
    task_mode: str | None,
    *,
    parent_task_mode: str | None = None,
    project_mode: str | None = None,
    default_mode: str = INTEGRATION_MODE_PULL_REQUEST,
) -> str:
    """Resolve the effective integration mode for a task (see above)."""
    mode, _ = resolve_integration_mode_with_source(
        task_mode,
        parent_task_mode=parent_task_mode,
        project_mode=project_mode,
        default_mode=default_mode,
    )
    return mode


class WorkspaceMode(Enum):
    """Lock mode for workspace access during task execution.

    Controls how agents share (or don't share) workspace resources.
    The coordination playbook specifies this when creating tasks.

    See docs/specs/design/agent-coordination.md §7 (Workspace Strategy).
    """

    EXCLUSIVE = "exclusive"  # One agent, one workspace (current default)
    #: **Deprecated — an alias for EXCLUSIVE.**  It once meant "multiple
    #: agents, same repo, different branches", implemented by a fallback that
    #: derived a throwaway worktree from a locked clone.  That fallback is
    #: retired (worktree-execution §7.4): parallel work in one repo is
    #: provided by worktree *slots*, selected by the workspace kind's ``mode``
    #: rather than by a per-task lock mode.  A task that sets this now gets
    #: exclusive locking, and when clones are exhausted the same PAUSED +
    #: 60 s backoff every exclusive task gets.  Kept as an accepted value so
    #: existing task rows and callers keep working.
    BRANCH_ISOLATED = "branch-isolated"
    DIRECTORY_ISOLATED = (
        "directory-isolated"  # Multiple agents, same branch, different dirs (deferred — stub only)
    )


# Convenience set for validation without constructing enum members.
WORKSPACE_MODE_VALUES = frozenset(m.value for m in WorkspaceMode)


class DepType(Enum):
    """Typed dependency edge kinds on ``task_dependencies.dep_type``.

    See docs/specs/design/work-graph.md §3.  The first four are *blocking*
    edges — they feed the ``tasks.is_blocked`` projection, each with its own
    satisfaction rule.  The rest are provenance/association only and never
    affect readiness.

    Satisfaction rules (design §3.1):

    ``BLOCKS``
        satisfied when the dependency is COMPLETED.
    ``PARENT_CHILD``
        satisfied when the container has been *released* — its status is
        not DEFINED.
    ``WAITS_FOR``
        dynamic fan-in: satisfied when every task with a ``parent-child``
        edge to the target is COMPLETED (vacuously true with zero children;
        children added later re-block the waiter).
    ``CONDITIONAL_BLOCKS``
        satisfied only on *terminal* failure of the dependency: status
        BLOCKED, or FAILED with ``retry_count >= max_retries``.
    """

    BLOCKS = "blocks"
    PARENT_CHILD = "parent-child"
    WAITS_FOR = "waits-for"
    CONDITIONAL_BLOCKS = "conditional-blocks"
    DISCOVERED_FROM = "discovered-from"
    RELATED = "related"
    DUPLICATES = "duplicates"
    SUPERSEDES = "supersedes"


# Convenience set for validation without constructing enum members.
DEP_TYPE_VALUES = frozenset(d.value for d in DepType)

BLOCKING_DEP_TYPES = frozenset(
    {
        DepType.BLOCKS.value,
        DepType.PARENT_CHILD.value,
        DepType.WAITS_FOR.value,
        DepType.CONDITIONAL_BLOCKS.value,
    }
)
"""Edge kinds that gate readiness — everything else is informational."""

HOLD_LABEL_PREFIX = "hold:"
"""Convention (design §6): a ``hold:<who>`` label withholds a task from the
ready frontier.  Held tasks stay visible and unblocked — they are filtered
when deciding what to *do*, never what must *exist*."""


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
    RETIRED = "RETIRED"


class ClaimResult(Enum):
    """Result codes of ``task_claim`` (swarm-work-model §10)."""

    CLAIMED = "claimed"
    NO_READY_WORK = "no_ready_work"
    CLAIM_CONFLICT = "claim_conflict"
    PREPARE_FAILED = "prepare_failed"
    CLAIM_IN_PROGRESS = "claim_in_progress"
    NOT_ADMISSIBLE = "not_admissible"
    SESSION_EXHAUSTED = "session_exhausted"
    DRAIN_REQUESTED = "drain_requested"
    STALE_CLAIM = "stale_claim"
    OUT_OF_SCOPE = "out_of_scope"


CLAIM_PHASES = ("claiming", "preparing", "active")


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
    an existing local path, initialized as a new git repo, or created as a
    git worktree for branch-isolated workspace sharing."""

    CLONE = "clone"
    LINK = "link"
    INIT = "init"
    WORKTREE = "worktree"  # Git worktree of another workspace (branch-isolated mode)


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
    assignment_playbook_id: str | None = None
    # Project-level integration policy: "direct" | "pull_request" | None
    # (None = inherit the system default, config ``integration.default_mode``).
    integration_mode: str | None = None


@dataclass
class ProjectConstraint:
    """Temporary scheduling constraint on a project.

    Constraints are set by workflows, playbooks, or admins to temporarily
    restrict how the scheduler assigns work to a project. They persist
    until explicitly released via ``release_project_constraint``.

    Fields:
        project_id: The project this constraint applies to.
        exclusive: If True, only one agent may work on the project at a
            time (overrides ``max_concurrent_agents`` to 1).
        max_agents_by_type: Per-agent-type concurrency limits, e.g.
            ``{"claude": 2, "codex": 1}``. When set, the scheduler checks
            how many agents of each type are active and enforces the cap.
        pause_scheduling: If True, the scheduler skips this project
            entirely — no new tasks are assigned until released.
        created_by: Identifier of who/what set the constraint (e.g.
            workflow ID, admin username). Informational only.
        created_at: Unix timestamp when the constraint was created.
    """

    project_id: str
    exclusive: bool = False
    max_agents_by_type: dict[str, int] = field(default_factory=dict)
    pause_scheduling: bool = False
    created_by: str | None = None
    created_at: float = 0.0


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
    # Per-task integration-policy override: "direct" | "pull_request" | None
    # (None = inherit — project policy, then config ``integration.default_mode``).
    # Resolved via ``resolve_integration_mode``; consumed by the completion
    # pipeline (_phase_verify/_phase_integrate) and the execution-rules prompt.
    integration_mode: str | None = None
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
    skip_verification: bool = False  # if True, skip git verification on completion
    workflow_id: str | None = None  # FK to workflows table (coordination playbooks)
    affinity_agent_id: str | None = None  # preferred agent ID for context continuity
    affinity_reason: str | None = None  # why: "context", "workspace", "type"
    workspace_mode: WorkspaceMode | None = None  # lock mode for workspace access
    dedup_key: str | None = None
    discord_thread_id: str | None = None
    intelligence_class: str | None = None
    # Provenance and swarm counters (swarm-work-model §9).  ``claim_epoch``
    # and ``filed_count`` are written by Plan 2; they ride on the model so
    # ``_row_to_task`` is complete from the first migration.
    created_by_kind: str | None = None
    created_by_id: str | None = None
    claim_epoch: int = 0
    filed_count: int = 0
    # Persisted blocked-state projection (work-graph design §4).  Pure
    # derived data: 1 iff some blocking edge is unsatisfied or an attached
    # gate is unresolved.  Recomputed in-transaction by the query layer —
    # never set by hand.
    is_blocked: bool = False
    created_at: float = 0.0  # unix timestamp when the task was created
    updated_at: float = 0.0  # unix timestamp when the task was last updated


@dataclass(frozen=True)
class AssignmentOption:
    """One class/provider route the ordinary worker flock can execute."""

    intelligence_class: str
    provider: str
    configured_capacity: int
    idle_count: int
    busy_count: int
    availability: str = "unknown"


@dataclass(frozen=True)
class TaskAssignmentRoute:
    """The current successful playbook-derived assignment decision."""

    task_id: str
    project_id: str
    input_hash: str
    task_updated_at: float
    options_hash: str
    intelligence_class: str
    provider: str | None
    playbook_id: str
    playbook_version: int
    playbook_run_id: str
    reason: str
    decided_at: float


@dataclass
class TaskCompletion:
    """Append-only account of one accepted ``task_close`` invocation."""

    id: str
    task_id: str
    outcome: str
    work_outcome: str | None = None
    failure_class: str | None = None
    changes: str = ""
    verification: str = ""
    tests: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    branch: str | None = None
    commits: list[str] = field(default_factory=list)
    pr_url: str | None = None
    summary: str = ""
    notes: str = ""
    completed_at: float = 0.0


@dataclass
class Agent:
    """Durable global worker identity; projects belong to its current task.

    Profiles specialize task capabilities without changing this definition.
    Explicit execution overrides take effect on the next session.
    """

    id: str
    name: str
    profile_id: str  # soft reference to agent_profiles.id
    state: AgentState = AgentState.IDLE
    current_task_id: str | None = None
    pid: int | None = None
    last_heartbeat: float | None = None
    total_tokens_used: int = 0
    session_tokens_used: int = 0
    created_at: float = 0.0

    role: str = "worker"
    enabled: bool = True
    harness: str | None = None
    model: str | None = None
    intelligence_class: str | None = None
    deleted_at: float | None = None


@dataclass
class WorkspaceAgent:
    """A workspace viewed as an agent slot — the new workspace-as-agent model.

    An "agent" is simply a workspace execution context.  Idle (unlocked)
    workspaces are idle agents; locked workspaces are busy agents.  There is
    derived API view of an Agent that currently holds a workspace lock —
    not a persisted entity. The persisted record stays :class:`Agent`.
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
    # Soft ref to (workspace_kinds.project_id, workspace_kinds.id); resolved
    # at use time against the project-scoped row, then the system row.
    # Nullable during the workspaces-v2 migration window (spec §3.2 / §9.5).
    kind_id: str | None = None
    locked_by_agent_id: str | None = None
    locked_by_task_id: str | None = None
    locked_at: float | None = None
    lock_mode: WorkspaceMode | None = None  # lock mode used for current lock (None = unlocked)
    enabled: bool = True  # disabled workspaces are skipped by acquire_workspace
    # Worktree slots (worktree-execution spec §3.2).  ``slot_index`` is NULL
    # for clones, links and base rows; 0..N-1 for slot worktrees.
    # ``base_workspace_id`` is a soft self-reference to the base clone's row.
    slot_index: int | None = None
    base_workspace_id: str | None = None

    @property
    def is_slot(self) -> bool:
        """True when this row is a worktree slot (worktree-execution §2.2)."""
        return self.slot_index is not None and self.base_workspace_id is not None


# Sentinel project_id for system-wide workspace_kinds rows. See spec §3.1.
SYSTEM_KIND_SCOPE = "__system__"

# ── Workspace kind git-provisioning modes (worktree-execution §2.1) ───────
#: One base clone + N slot worktrees; agents run in the slots.
KIND_MODE_WORKTREE = "worktree"
#: Legacy: every workspace row is a full clone, exclusively locked.
KIND_MODE_EXCLUSIVE_CLONE = "exclusive-clone"
#: Deferred stub: same branch, different directories (monorepos).
KIND_MODE_DIRECTORY_ISOLATED = "directory-isolated"

WORKSPACE_KIND_MODES = frozenset(
    {KIND_MODE_WORKTREE, KIND_MODE_EXCLUSIVE_CLONE, KIND_MODE_DIRECTORY_ISOLATED}
)


@dataclass
class WorkspaceKind:
    """Definition of a workspace type. See spec §3.1.

    Lives in the ``workspace_kinds`` table.  System rows use
    ``project_id = SYSTEM_KIND_SCOPE``; project-scoped rows shadow them at
    resolution time.
    """

    project_id: str  # SYSTEM_KIND_SCOPE for system-wide rows
    id: str
    description: str = ""
    writable: bool = True
    lockable: bool = True
    is_git_repo: bool = True
    repo_url: str | None = None
    # Lowercase enum value: "exclusive" | "branch_isolated" | "directory_isolated"
    default_lock_mode: str | None = None
    auto_attach: bool = False
    # Git provisioning strategy (worktree-execution §2.1).  Meaningful only
    # when ``is_git_repo``.  One of :data:`WORKSPACE_KIND_MODES`.
    #
    # ``None`` is a distinct third state, produced only by the markdown
    # *parser*: it means "the frontmatter says nothing about mode", which
    # ``upsert_workspace_kind`` coalesces to "leave the stored value alone".
    # Defaulting an absent key to ``worktree`` instead silently flips every
    # upgrading install's ``project-repo`` kind from ``exclusive-clone`` to
    # ``worktree`` on the first daemon start — and on every start after.
    # Rows read back from the DB always carry a concrete value.
    mode: str | None = KIND_MODE_WORKTREE
    # Shell commands run once inside a freshly created slot, and again when
    # the list changes (worktree-execution §3.6).  Operator-authored config.
    worktree_setup: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def setup_hash(self) -> str:
        """Stable digest of ``worktree_setup`` — drives re-run semantics."""
        return worktree_setup_hash(self.worktree_setup)


def worktree_setup_hash(commands: list[str] | None) -> str:
    """SHA-256 over the ordered ``worktree_setup`` command list.

    Kept module-level so the slot manager can hash a raw list without
    materializing a :class:`WorkspaceKind`.
    """
    payload = json.dumps(list(commands or []), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class MergeSlot:
    """Per-project integration lease. See worktree-execution §3.3 / §4.1.

    One row per project in ``merge_slots``; ``holder_task_id is None`` means
    the slot is free.  ``expires_at`` is a lease so a crashed holder cannot
    starve integration forever.
    """

    project_id: str
    holder_task_id: str | None = None
    acquired_at: float | None = None
    expires_at: float | None = None
    updated_at: float = 0.0

    def is_held(self, now: float) -> bool:
        """True when a live (non-expired) holder owns the slot."""
        if self.holder_task_id is None:
            return False
        return self.expires_at is None or self.expires_at > now


#: Filename of the per-slot sentinel, relative to the slot directory.
WORKTREE_SENTINEL_NAME = ".aq-worktree.json"


@dataclass
class WorktreeSentinel:
    """Contents of ``<slot>/.aq-worktree.json``. See worktree-execution §2.5.

    The filesystem half of adoption and doctoring.  The DB row stays
    authoritative for locks; this file is what lets a directory be
    recognised after a crash, and what records which ``worktree_setup``
    revision the slot was provisioned with.
    """

    slot: str
    slot_index: int
    base_workspace_id: str
    project_id: str
    workspace_id: str = ""
    task_id: str | None = None
    branch: str | None = None
    created_at: float = 0.0
    assigned_at: float | None = None
    daemon_epoch: str = ""
    setup_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "slot_index": self.slot_index,
            "base_workspace_id": self.base_workspace_id,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "branch": self.branch,
            "created_at": self.created_at,
            "assigned_at": self.assigned_at,
            "daemon_epoch": self.daemon_epoch,
            "setup_hash": self.setup_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorktreeSentinel":
        """Tolerant parse — unknown keys ignored, missing keys defaulted.

        Sentinels are read from disk after crashes and across versions, so a
        strict parse would turn a recoverable slot into an unrecoverable one.
        """
        return cls(
            slot=str(data.get("slot", "")),
            slot_index=int(data.get("slot_index", -1)),
            base_workspace_id=str(data.get("base_workspace_id", "")),
            project_id=str(data.get("project_id", "")),
            workspace_id=str(data.get("workspace_id", "") or ""),
            task_id=data.get("task_id"),
            branch=data.get("branch"),
            created_at=float(data.get("created_at") or 0.0),
            assigned_at=(
                float(data["assigned_at"]) if data.get("assigned_at") is not None else None
            ),
            daemon_epoch=str(data.get("daemon_epoch", "") or ""),
            setup_hash=str(data.get("setup_hash", "") or ""),
        )


@dataclass
class ResolvedRequirement:
    """A task's request for one workspace of a given kind. See spec §6.1.

    ``position`` is part of the canonical lock order ``(kind_id, position)``.
    ``preferred_workspace_id`` is only set on the synthesized project-repo
    requirement (carrying ``Task.preferred_workspace_id``).
    """

    kind_id: str
    alias: str | None = None
    position: int = 0
    preferred_workspace_id: str | None = None


@dataclass
class WorkspaceAttachment:
    """A workspace bound to a task at acquisition time. See spec §8.1.

    Distinct from ``Task.attachments`` (file attachments — see naming note in
    spec §6.2).  Renaming was deliberate to avoid the collision.
    """

    requirement: ResolvedRequirement
    workspace: Workspace
    kind: WorkspaceKind

    @property
    def kind_id(self) -> str:
        return self.requirement.kind_id

    @property
    def alias(self) -> str | None:
        return self.requirement.alias

    @property
    def workspace_path(self) -> str:
        return self.workspace.workspace_path

    @property
    def writable(self) -> bool:
        return self.kind.writable

    @property
    def lockable(self) -> bool:
        return self.kind.lockable


@dataclass
class WorkspaceAttachmentSet:
    """All workspace attachments acquired for a task. See spec §6.2."""

    attachments: list[WorkspaceAttachment] = field(default_factory=list)

    def by_kind(self, kind_id: str) -> list[WorkspaceAttachment]:
        return [a for a in self.attachments if a.kind_id == kind_id]

    def first_of_kind(self, kind_id: str) -> WorkspaceAttachment | None:
        for a in self.attachments:
            if a.kind_id == kind_id:
                return a
        return None

    @property
    def primary_path(self) -> str | None:
        """Path of the project-repo attachment if present (spec §8.1)."""
        a = self.first_of_kind("project-repo")
        return a.workspace_path if a else None


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
    # -- Normalized capability namespaces (Playbook V2 Package 0 §3.1) -------
    # Authored in the profile markdown's ``## Capabilities`` block.  ``None``
    # means "not authored — run the legacy adapter over ``allowed_tools``";
    # ``[]`` means "explicitly none".  Keeping those two apart is the whole
    # basis of the audit/enforce split, so nothing may backfill NULL to [].
    # Resolved into a ``CapabilityPolicy`` by
    # ``src.profiles.capabilities.capability_policy_for``.
    harness_tools: list[str] | None = None
    aq_commands: list[str] | None = None
    plugin_tools: list[str] | None = None
    # Names of MCP servers this profile uses.  The names are resolved at
    # task launch against the in-memory MCP registry (system + project
    # scope) which is sourced from ``vault/[projects/<pid>/]mcp-servers/*.md``.
    # Profiles do not store inline server configs anymore — see
    # ``src/profiles/mcp_registry.py`` and the inline-config migration in
    # ``src/profiles/mcp_inline_migration.py``.
    mcp_servers: list[str] = field(default_factory=list)
    system_prompt_suffix: str = ""  # appended to agent instructions
    install: dict = field(default_factory=dict)  # auto-install manifest (future)
    # Optional override: when set, agent-type memory for this profile lives at
    # ``agenttype_{memory_scope_id}`` instead of ``agenttype_{id}``.  Lets
    # multiple profiles share one memory scope (e.g. claude-opus and
    # claude-sonnet both set ``memory_scope_id='claude'``).  None = use id.
    memory_scope_id: str | None = None
    # Which runtime executes tasks for this profile.  Empty (the default)
    # means the profile runs as a **session**: a CLI wrapped in tmux,
    # selected by ``harness``.  That is the path for every coding agent.
    #
    # The only non-empty value is ``"supervisor"`` — the in-process,
    # tool-call-only daemon brain, which has no CLI to wrap and therefore no
    # harness.  ``claude_sdk`` and ``acpx`` were removed in the tmux-harness
    # migration; a profile still naming them is rejected at parse time
    # rather than silently falling back.  Sourced from the ``## Config``
    # JSON block of the profile markdown.
    runtime: str = ""
    # -- Named-session pass-through storage (supervisor-agent spec §3.2/§7) --
    # Validated at profile parse time; the harness *schema* (what "claude"
    # means) is owned by the session-runtime spec, so as far as this layer
    # is concerned these are opaque storage.
    #
    # ``harness``         — session-runtime harness id (any string).
    # ``lifecycle``       — "task" (default) | "named".
    # ``mode``            — "always" | "on_demand"; named lifecycle only.
    # ``wake_mode``       — "resume" | "fresh"; named lifecycle only.
    # ``idle_timeout``    — seconds before an on_demand session sleeps.
    # ``max_session_age`` — seconds before a named session is recycled.
    harness: str | None = None
    lifecycle: str = "task"
    mode: str | None = None
    wake_mode: str | None = None
    idle_timeout: int | None = None
    default_class: str = ""
    needs_workspace: bool = True
    # When True, this profile MUST NOT mutate its acquired workspace.  It is
    # a declarative statement of write *intent*: profiles with
    # ``read_only: true`` do not list write/edit/commit/push tools, and the
    # shipped reviewer profile is checked for that.  It deliberately does
    # **not** change workspace acquisition any more — skipping the lock used
    # to hand read-only agents the kind's base checkout, which is the one
    # workspace no session may touch (see
    # :mod:`src.orchestrator.base_workspace`).
    read_only: bool = False
    # Opt-in escape hatch for the base-checkout guard.  A session's
    # ``work_dir`` may not be a base workspace — the clone that hosts the
    # slot worktrees, routinely a human's own checkout — unless its profile
    # sets ``allow_base_checkout: true``.  Nothing shipped sets it.
    allow_base_checkout: bool = False
    max_session_age: int | None = None
    # lifecycle: pool (swarm-work-model §9).  NULL = unlimited claims.
    min_active: int | None = None
    max_active: int | None = None
    max_claims_per_session: int | None = None


@dataclass
class Message:
    """One row of the ``messages`` table — the single inter-agent/user queue.

    Carries every user↔session and session↔session exchange (supervisor-agent
    design §6).  Timestamps are Float epoch seconds, matching every other
    table in this schema; the application sets them, never the database.

    ``from_kind`` is one of ``session`` | ``user`` | ``system``;
    ``to_kind`` is one of ``session`` | ``task`` | ``profile`` | ``user``.
    Both are enforced by named CHECK constraints on the table.
    """

    id: str
    project_id: str | None
    from_kind: str
    from_id: str
    to_kind: str
    to_id: str
    body: str
    subject: str | None = None
    thread_id: str | None = None
    priority: int = 100
    created_at: float = 0.0
    delivered_at: float | None = None
    read_at: float | None = None
    archive_after_inject: bool = False
    archived_at: float | None = None
    reply_to_id: str | None = None
    via: str | None = None
    body_kind: str | None = None
    pane_open: str | None = None  # JSON-encoded {view, args}


#: Legal ``messages.from_kind`` values (mirrors ``ck_messages_from_kind``).
MESSAGE_FROM_KINDS: frozenset[str] = frozenset({"session", "user", "system"})

#: Legal ``messages.to_kind`` values (mirrors ``ck_messages_to_kind``).
MESSAGE_TO_KINDS: frozenset[str] = frozenset({"session", "task", "profile", "user"})


@dataclass
class TaskContext:
    """The input bundle passed to a platform when executing a task.

    This is the platform's entire view of the work to be done: what to build
    (description, acceptance_criteria), how to verify it (test_commands),
    where to work (checkout_path, branch_name), and what tools/context are
    available. The orchestrator constructs this from the Task, its criteria,
    context entries, and tool permissions stored in the database.
    """

    description: str
    task_id: str = ""
    l0_role: str = ""  # L0 Identity tier (~50 tokens, always present at task start)
    # Project-specific role override, applied AFTER l0_role. Sourced from the
    # scoped profile's system_prompt_suffix so the agent sees: base agent-type
    # role (l0_role) + project specialisation (project_override_role).
    project_override_role: str = ""
    l1_facts: str = ""  # L1 Critical Facts tier (~200 tokens, always present at task start)
    l1_guidance: str = ""  # L1 Guidance tier (~300 tokens, deterministic behavioral rules)
    l2_context: str = ""  # L2 Topic Context tier (~500 tokens, semantic search results)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    # Filesystem path where the agent should run.  Optional: supervisor-platform
    # tasks have no workspace and pass ``None``; subprocess-platform tasks always
    # carry a path.  Existing readers normalise empty/None as appropriate.
    checkout_path: str | None = ""
    branch_name: str = ""
    attached_context: list[str] = field(default_factory=list)
    image_paths: list[str] = field(
        default_factory=list
    )  # absolute paths to images the agent should examine
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    # Extra directories the agent is allowed to read/write outside of its
    # primary workspace (cwd). Typically includes the project's vault
    # memory directory so consolidation and memory-aware tasks can edit
    # their own knowledge without needing a separate MCP indirection.
    add_dirs: list[str] = field(default_factory=list)
    # All workspaces attached to this task at acquisition time
    # (workspaces-v2 spec §8.1).  Includes the project-repo (the same
    # path as ``checkout_path``) plus any explicitly-requested kinds and
    # auto-attached kinds (e.g. vault).  Runtimes derive cwd + extra dirs
    # from this set with deduplication against ``add_dirs`` (spec §7.1).
    # Empty list for tasks dispatched before the orchestrator captures an
    # attachment set (e.g. Supervisor singleton, legacy code paths).
    workspace_attachments: list["WorkspaceAttachment"] = field(default_factory=list)
    resume_session_id: str | None = None  # fork from this session on reopen
    # The resolved AgentProfile for this task. Platforms read it for
    # allowed_tools, model overrides, etc.  Singleton platforms (Supervisor)
    # rely on this since they can't carry the profile in their constructor.
    profile: "AgentProfile | None" = None


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
    topic_memories: str = ""  # L2 memories filtered by topic frontmatter (spec §2)
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
        if self.topic_context or self.topic_memories:
            topic_label = ", ".join(self.detected_topics) if self.detected_topics else "detected"
            l2_parts: list[str] = []
            l2_parts.append(
                f"## Topic Context ({topic_label})\n"
                "The following knowledge was pre-loaded based on topics detected "
                "in your task description."
            )
            if self.topic_context:
                l2_parts.append(self.topic_context)
            if self.topic_memories:
                l2_parts.append(
                    "### Related Memories\n"
                    "These past insights matched the detected topics:\n\n" + self.topic_memories
                )
            sections.append("\n\n".join(l2_parts))
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
                self.topic_memories,
                self.notes,
                self.recent_tasks,
                self.search_results,
                self.memory_folder,
            ]
        )


# Hook and HookRun dataclasses removed (playbooks spec §13 Phase 3).
# All automation is now managed through playbooks.


class PlaybookRunStatus(Enum):
    """Valid statuses for a playbook execution run.

    These map directly to the state machine defined in VALID_PLAYBOOK_RUN_TRANSITIONS
    (see src/playbooks/state_machine.py).  The PlaybookRunner drives runs through
    these states based on events like terminal node reached, node failure, token
    budget exhaustion, and human-in-the-loop pause/resume.

    See docs/specs/design/playbooks.md §6 (Run Persistence).
    """

    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class PlaybookRunEvent(Enum):
    """Events that trigger transitions between PlaybookRunStatus states.

    Each (PlaybookRunStatus, PlaybookRunEvent) pair maps to exactly one target
    PlaybookRunStatus in the transitions table.  See src/playbooks/state_machine.py.

    Event groups:

    - **Completion:** TERMINAL_REACHED — graph walk reached a terminal node.
    - **Failure:** NODE_FAILED (node execution error), TRANSITION_FAILED
      (transition evaluation error), GRAPH_ERROR (missing entry/node).
    - **Budget:** BUDGET_EXCEEDED — token budget exhausted mid-run.
    - **Human-in-the-loop:** HUMAN_WAIT (node has ``wait_for_human``),
      HUMAN_RESUMED (human provided input to resume),
      PAUSE_TIMEOUT (pause timeout expired without human input).
    """

    TERMINAL_REACHED = "TERMINAL_REACHED"
    NODE_FAILED = "NODE_FAILED"
    TRANSITION_FAILED = "TRANSITION_FAILED"
    GRAPH_ERROR = "GRAPH_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    HUMAN_WAIT = "HUMAN_WAIT"
    HUMAN_RESUMED = "HUMAN_RESUMED"
    EVENT_WAIT = "EVENT_WAIT"
    EVENT_RESUMED = "EVENT_RESUMED"
    PAUSE_TIMEOUT = "PAUSE_TIMEOUT"


@dataclass
class PlaybookRun:
    """A single execution record of a playbook graph.

    Tracks the full lifecycle of one playbook invocation: which playbook
    was executed, the trigger event, accumulated conversation history,
    the path taken through the graph (node trace), and token usage.

    For paused runs (human-in-the-loop), the conversation history is
    serialised so the run can resume exactly where it left off, even
    across process restarts.

    See docs/specs/design/playbooks.md §6 (Run Persistence).
    """

    run_id: str
    playbook_id: str
    playbook_version: int
    trigger_event: str = "{}"  # JSON-serialised event dict
    status: str = "running"
    current_node: str | None = None
    conversation_history: str = "[]"  # JSON-serialised message list
    node_trace: str = "[]"  # JSON list of {node_id, started_at, completed_at, status}
    tokens_used: int = 0
    started_at: float = 0.0
    completed_at: float | None = None
    error: str | None = None
    pinned_graph: str | None = None  # JSON-serialised compiled graph for version pinning
    paused_at: float | None = None  # Unix timestamp when the run was paused
    waiting_for_event: str | None = (
        None  # Event type the run is waiting for (event-triggered pause)
    )
    event_id: str | None = None  # Stable dedup key from the triggering event


@dataclass
class Workflow:
    """A coordination workflow spawned by a playbook run.

    Workflows track the lifecycle of a coordination playbook execution:
    which stages have run, which tasks were created, and agent affinity
    preferences for context continuity.  The workflow is created in the
    first node of a coordination playbook and updated as the playbook
    progresses through its graph.

    See docs/specs/design/agent-coordination.md §6 (Workflow Runtime).
    """

    workflow_id: str
    playbook_id: str
    playbook_run_id: str
    project_id: str
    status: str = "running"  # running, paused, completed, failed
    current_stage: str | None = None
    task_ids: list[str] = field(default_factory=list)
    agent_affinity: dict[str, str] = field(default_factory=dict)
    stages: list[dict] = field(default_factory=list)  # stage history for pipeline view
    created_at: float = 0.0
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
    verification_reopened: bool = False
    #: True when ``aq task close`` was issued by a *live* session that is
    #: still sitting at its prompt.  Git verification uses it to hand the
    #: fixable issues straight back to that agent instead of reopening the
    #: task to READY -- which the session reconciler reads as "live session,
    #: task not IN_PROGRESS" and drains, killing the very worker that was
    #: asked to fix them.
    close_session_live: bool = False
    #: Set by ``_phase_verify`` when it chose the in-session retry: the task
    #: stays IN_PROGRESS under its claim and the close is refused with
    #: feedback.
    verification_retry_in_session: bool = False
    #: The fixable issues behind ``verification_retry_in_session``.
    verification_issues: list[str] = field(default_factory=list)
    #: Rendered feedback text handed back to the agent on an in-session retry.
    verification_feedback: str = ""
    #: ``--work-outcome`` from ``aq task close`` (``shipped`` | ``no-op`` |
    #: ``blocked`` | ``abandoned``), empty when the agent gave none.  Git
    #: verification reads ``no-op`` as "this task produced no code": there is
    #: nothing to push, PR or merge, so the require-a-PR gate does not apply.
    work_outcome: str = ""
    #: Set by ``_run_completion_pipeline`` when the task branch carried no
    #: commits ahead of its base, asked *before* integration could merge the
    #: branch away.  The close path folds it into the ``no_code`` flag on
    #: ``task.completed`` so the review rules never spawn a reviewer for an
    #: empty diff (task bright-forge-78).  False also means "not asked" —
    #: the question is only answerable on the paths where the branch is
    #: still unmerged at that point.
    branch_no_commits: bool = False


@dataclass(frozen=True)
class SessionRecord:
    """One row of the ``sessions`` table — an OS-level agent run.

    Maps 1:1 to ``src/database/tables.py::sessions``.  Frozen because a
    session row is *observed* state: the reconciler writes through
    ``update_session`` and re-reads, rather than mutating a shared object a
    concurrent tick might be holding.

    ``task_id`` is None for named (persistent) sessions.  ``state`` is one
    of ``starting | running | draining | stopped | sleeping | quarantined``;
    "stalled" is derived from the lease TTL versus ``last_activity`` and is
    deliberately never stored.  ``desired_state`` is the intent ``state``
    converges toward -- see
    ``docs/superpowers/specs/2026-08-27-session-desired-state-design.md``.

    ``epoch`` is provenance (which daemon run launched this), not a validity
    test — an older-epoch session is still adoptable.  ``instance_token`` is
    the kill fence: it is compared against the observed process before any
    signal, so a name-reusing successor is never hit.
    """

    id: str
    project_id: str | None
    profile_id: str
    harness: str
    provider: str
    name: str
    lifecycle: str
    work_dir: str
    epoch: str
    instance_token: str
    started_at: float
    task_id: str | None = None
    state: str = "starting"
    #: Desired state -- ``running | sleeping | stopped``.  Written by
    #: whoever forms the intent (lens cold-start, idle drain, terminal
    #: verdict, operator); ``state`` is written by whoever observes.
    desired_state: str = "running"
    session_key: str | None = None
    last_activity: float | None = None
    restarts: int = 0
    quarantined_at: float | None = None
    sleep_reason: str | None = None
    ended_at: float | None = None
    end_reason: str | None = None
    # Pool lifecycle (swarm-work-model §9–§11).
    claims: int = 0
    agent_id: str | None = None
    claim_phase: str | None = None  # claiming | preparing | active
    claim_phase_at: float | None = None
    last_claim_epoch: int | None = None
    last_claim_result: str | None = None  # claimed | prepare_failed | released

    llm_provider: str | None = None
    model: str | None = None
    intelligence_class: str | None = None

    #: True when this launch actually wired the harness's subagent hooks
    #: (``SubagentStart`` / ``SubagentStop`` -> ``aq subagent event``).
    #: Recorded from the :class:`~src.sessions.provider.SessionSpec` at
    #: insert, because it is a property of the argv this process was
    #: started with, not of whatever the harness file says today.
    hooks_provisioned: bool = False
