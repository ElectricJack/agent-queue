"""Proportional fair-share scheduler for assigning tasks to idle agents.

Uses a purely deterministic algorithm -- zero LLM calls. Every token budget
is spent on agent work, not on deciding *which* work to do.

The scheduling algorithm runs in two phases each time an idle agent needs
a task:

1. **Min-task guarantee** -- Projects that have completed zero tasks in the
   current scheduling window are prioritized first.  This ensures every
   active project gets at least one task assigned before proportional
   allocation kicks in.

2. **Deficit-based proportional allocation** -- Among projects that already
   have at least one completion, the scheduler picks the project whose
   actual token usage ratio is furthest *below* its target ratio (derived
   from ``credit_weight``).  This gradually converges each project toward
   its fair share of total agent time.

Both phases respect per-project concurrency limits (``max_concurrent_agents``),
per-project / global budget caps, and workspace availability (a project with
all workspaces locked cannot receive new assignments even if it has quota).

Key design properties:

- **Pure function** — the scheduler takes a snapshot (``SchedulerState``) and
  returns actions with zero side effects, zero LLM calls, and zero I/O.
- **Starvation-free** — ``min_task_guarantee`` ensures every active project
  eventually receives at least one task per scheduling window.
- **Convergent** — deficit-based proportional allocation gradually steers
  each project toward its fair share; short-term imbalances self-correct
  over multiple scheduling rounds.

Concrete example of deficit-based scheduling::

    Projects: A (weight=3), B (weight=1)
    Total weight = 4 → target ratios: A=75%, B=25%
    Current window usage: A=1000 tokens, B=500 tokens
    Total tokens = 1500 → actual ratios: A=66.7%, B=33.3%

    Deficits:  A = 66.7% - 75% = -8.3%  (under-served)
               B = 33.3% - 25% = +8.3%  (over-served)

    → Project A sorts first because its deficit is more negative.
    → The scheduler assigns A's highest-priority READY task next.

    Over multiple rounds, this converges: A will keep getting priority
    until its actual usage ratio approaches 75%.

Time complexity: O(A × P × log P) per cycle, where A = idle agents and
P = active projects.  Both are typically small (<10), so scheduling is
effectively instant.

Integration with the orchestrator:

    The orchestrator's ``_schedule()`` method builds a ``SchedulerState``
    snapshot from DB queries each cycle, passes it to ``Scheduler.schedule()``,
    and receives back a list of ``AssignAction`` objects.  The orchestrator
    then launches background asyncio tasks for each assignment.

    See ``src/orchestrator.py::_schedule()`` for snapshot construction.
    See ``specs/scheduler-and-budget.md`` for the full specification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.models import (
    Agent,
    AgentState,
    Project,
    ProjectConstraint,
    ProjectStatus,
    Task,
    TaskStatus,
    TaskType,
)

logger = logging.getLogger(__name__)


@dataclass
class AssignAction:
    """A scheduling decision: assign one specific task to one specific agent.

    This is the output type of the scheduler -- a list of these actions is
    returned each scheduling round, one per idle agent that received work.
    The orchestrator is responsible for actually executing the assignment
    (updating the database, starting the agent process, etc.).
    """

    agent_id: str
    task_id: str
    project_id: str


@dataclass
class SchedulerState:
    """A snapshot of all system state the scheduler needs to make decisions.

    The scheduler is a pure function: given a SchedulerState, it returns a
    list of AssignActions with no side effects.  This stateless/functional
    design makes the algorithm easy to test and reason about -- the
    orchestrator builds this snapshot each tick, and the scheduler never
    touches the database or any external resource.

    All "window" fields (token usage, completed counts) are scoped to the
    ``rolling_window_hours`` configured in the scheduling config.  The
    rolling window creates a "forgetting" mechanism: old usage ages out,
    so a project that was over-served yesterday can still receive fair
    allocation today.  The orchestrator computes these from DB queries
    filtered by ``time.time() - window_hours * 3600``.
    """

    projects: list[Project]
    tasks: list[Task]
    agents: list[Agent]
    # Token usage within the rolling window, keyed by project_id.
    # This is the numerator for computing each project's "actual ratio"
    # in the deficit calculation (actual_ratio = usage / total_usage).
    project_token_usage: dict[str, int]
    # Number of agents currently executing tasks for each project.
    # Used to enforce ``project.max_concurrent_agents`` limits.
    project_active_agent_counts: dict[str, int]
    # Number of tasks completed per project within the rolling window.
    # Projects with zero completions get priority via min_task_guarantee.
    tasks_completed_in_window: dict[str, int]
    # Available (unlocked) workspaces per project.  A hard constraint:
    # the scheduler cannot assign more tasks than physical workspaces.
    # Empty dict = no workspace tracking (e.g., in tests).
    project_available_workspaces: dict[str, int] = field(default_factory=dict)
    # Maps workspace_id → locked_by_task_id (None if free).
    # Used to enforce workspace affinity: tasks with a preferred_workspace_id
    # are only assigned when that workspace is unlocked.
    workspace_locks: dict[str, str | None] = field(default_factory=dict)
    # Global token budget across all projects (None = unlimited).
    global_budget: int | None = None
    # Total tokens used across all projects in the rolling window.
    global_tokens_used: int = 0
    # Provider-level cooldowns: maps agent_type (e.g. "claude") to the
    # Unix timestamp when the cooldown expires.  Agents of a cooled-down
    # type are excluded from scheduling until the timestamp passes.
    # This supports per-provider session limits without affecting other
    # provider types.
    provider_cooldowns: dict[str, float] = field(default_factory=dict)
    # Active project constraints, keyed by project_id.  The scheduler
    # checks these to enforce exclusive access, per-type agent limits,
    # and scheduling pauses.  Constraints are set via set_project_constraint
    # and persist until explicitly released via release_project_constraint.
    project_constraints: dict[str, ProjectConstraint] = field(default_factory=dict)
    # Current wall-clock time (Unix timestamp).  Used for bounded-wait
    # affinity: when a task's preferred agent is busy, the scheduler
    # defers assignment for up to ``affinity_wait_seconds`` before
    # falling back to any idle agent.  0.0 disables time-based logic.
    now: float = 0.0
    # Maximum seconds to wait for a busy affinity agent before falling
    # back to assigning any idle agent.  Sourced from
    # ``config.scheduling.affinity_wait_seconds``.
    affinity_wait_seconds: float = 120.0


def _workspace_available(task: Task, locks: dict[str, str | None]) -> bool:
    """Check if a task's preferred workspace is available (unlocked).

    Tasks without a preferred_workspace_id are always eligible.
    When locks is empty (e.g. in tests), no filtering is applied.
    """
    if not task.preferred_workspace_id or not locks:
        return True
    return locks.get(task.preferred_workspace_id) is None


def _task_agent_type_matches(task: Task, agent: Agent) -> bool:
    """Check if a task's agent_type requirement is satisfied by the agent.

    Agent type matching is a **hard constraint** — tasks with an explicit
    ``agent_type`` are only assigned to agents whose ``agent_type`` field
    matches exactly.  Tasks without an ``agent_type`` requirement match
    any agent regardless of the agent's type.

    Agents may advertise multiple type capabilities via a comma-separated
    ``agent_type`` string (e.g. ``"coding,code-review"``).  A task matches
    if its required type appears anywhere in the agent's type list.

    This enforces the type-matching dimension of agent affinity described
    in the agent-coordination spec §3 (Core Concepts): "a review task
    should go to a review agent, not a coding agent."

    Unlike agent-id affinity (which is advisory and uses soft ordering),
    type matching is a filter — mismatched tasks are excluded from
    consideration entirely, and will stay queued until a matching agent
    becomes available.
    """
    if not task.agent_type:
        return True  # no type requirement → any agent is fine
    # Support comma-separated multiple type capabilities on agents
    agent_types = {t.strip() for t in agent.agent_type.split(",")} if agent.agent_type else set()
    if task.agent_type in agent_types:
        return True
    logger.debug(
        "Agent type mismatch: task %s requires type '%s' but agent %s has type '%s'",
        task.id,
        task.agent_type,
        agent.id,
        agent.agent_type,
    )
    return False


def _is_scheduling_paused(project_id: str, constraints: dict[str, ProjectConstraint]) -> bool:
    """Return True if a project has an active pause_scheduling constraint."""
    c = constraints.get(project_id)
    return bool(c and c.pause_scheduling)


def _agent_type_allowed(
    agent: Agent,
    project_id: str,
    max_by_type: dict[str, int],
    state: "SchedulerState",
    assigned_agents: set[str],
) -> bool:
    """Check if assigning *agent* would violate a per-agent-type limit.

    Only agent types listed in ``max_by_type`` are constrained; unlisted
    types are unrestricted.  Returns True if the assignment is allowed.
    """
    atype = agent.agent_type
    if atype not in max_by_type:
        return True  # no limit for this type

    limit = max_by_type[atype]

    # Count agents of the same type currently working on this project.
    # An agent is "active on a project" if it is BUSY and its current
    # task belongs to the project.
    count = 0
    for a in state.agents:
        if a.agent_type != atype or a.id in assigned_agents:
            continue
        if a.state == AgentState.BUSY and a.current_task_id:
            for t in state.tasks:
                if t.id == a.current_task_id and t.project_id == project_id:
                    count += 1
                    break

    return count < limit


class Scheduler:
    @staticmethod
    def schedule(state: SchedulerState) -> list[AssignAction]:
        """Assign READY tasks to idle agents using proportional fair-share.

        Algorithm steps:
        1. Bail out early if the global token budget is exhausted.
        2. Collect idle agents and group READY tasks by project.
        3. For each idle agent (in order), rank active projects by:
           a. Min-task guarantee -- projects with zero completions in the
              window sort first (phase 1).
           b. Deficit -- among the rest, the project whose actual token
              usage is furthest below its ``credit_weight`` share sorts
              first (phase 2).
        4. Walk the ranked project list; skip any project that has hit its
           budget cap or concurrency limit.  Pick the highest-priority
           READY task from the first eligible project.
        5. Record the assignment and move to the next idle agent.

        Returns a list of :class:`AssignAction` -- one per agent that was
        matched with a task.  May be empty if no work can be assigned.
        """
        # Check global budget
        if state.global_budget is not None and state.global_tokens_used >= state.global_budget:
            return []

        import time as _time

        now = _time.time()
        idle_agents = [
            a
            for a in state.agents
            if a.state == AgentState.IDLE and state.provider_cooldowns.get(a.agent_type, 0) <= now
        ]
        if not idle_agents:
            return []

        # Group ready tasks by project
        ready_by_project: dict[str, list[Task]] = {}
        for task in state.tasks:
            if task.status == TaskStatus.READY:
                ready_by_project.setdefault(task.project_id, []).append(task)

        # Sort tasks within each project by priority (lower = higher priority),
        # then by creation order (id as a proxy for FIFO within same priority).
        # This determines which task the scheduler picks when a project is selected.
        for tasks in ready_by_project.values():
            tasks.sort(key=lambda t: (t.priority, t.id))

        # ── SYNC-task exclusivity ──────────────────────────────────────
        # When a SYNC task exists for a project (in any active state), it
        # needs exclusive access to the project's workspaces.  Block all
        # non-SYNC tasks from being scheduled:
        #
        # • SYNC task is READY → only schedule the SYNC task, nothing else
        # • SYNC task is ASSIGNED/IN_PROGRESS → don't schedule anything
        #   new (the sync workflow will pause the project once it starts,
        #   but there's a window between assignment and execution where
        #   the project is still ACTIVE)
        #
        # This prevents the race where resuming a project with a queued
        # sync task causes regular tasks to start alongside (or before)
        # the sync workflow.
        projects_with_active_sync: set[str] = set()
        for task in state.tasks:
            if task.task_type == TaskType.SYNC and task.status in (
                TaskStatus.ASSIGNED,
                TaskStatus.IN_PROGRESS,
            ):
                projects_with_active_sync.add(task.project_id)

        for pid in list(ready_by_project):
            if pid in projects_with_active_sync:
                # SYNC task already running/assigned — block ALL new tasks
                del ready_by_project[pid]
            elif any(t.task_type == TaskType.SYNC for t in ready_by_project[pid]):
                # SYNC task is READY — only allow the SYNC task to be scheduled
                ready_by_project[pid] = [
                    t for t in ready_by_project[pid] if t.task_type == TaskType.SYNC
                ]

        # Filter to active projects with ready tasks.
        # Also enforce project constraints:
        # - pause_scheduling=True → skip the project entirely
        active_projects = [
            p
            for p in state.projects
            if p.status == ProjectStatus.ACTIVE
            and p.id in ready_by_project
            and not _is_scheduling_paused(p.id, state.project_constraints)
        ]
        if not active_projects:
            return []

        # Calculate totals for proportional ratio computation.
        # ``total_weight`` is the denominator for target ratios (each
        # project's target = credit_weight / total_weight).
        # ``total_tokens`` is the denominator for actual ratios (each
        # project's actual = tokens_used / total_tokens).
        # We clamp total_tokens to at least 1 to avoid division by zero
        # during the first scheduling round before any tokens are used.
        total_weight = sum(p.credit_weight for p in active_projects)
        total_tokens = sum(state.project_token_usage.values()) or 1  # avoid div/0

        # Track assignments made in this scheduling round.  These sets
        # prevent double-assignment: an agent or task matched once won't be
        # considered again in the same round.  ``round_agent_counts`` is a
        # mutable copy of the live counts so that assignments within this
        # round are reflected in subsequent concurrency-limit checks.
        actions: list[AssignAction] = []
        assigned_agents: set[str] = set()
        assigned_tasks: set[str] = set()
        round_agent_counts: dict[str, int] = dict(state.project_active_agent_counts)

        for agent in idle_agents:
            if agent.id in assigned_agents:
                continue

            # Sort projects by scheduling priority using a two-level key:
            #
            # Level 1 — Min-task guarantee (binary):
            #   Projects with zero completions in the window sort first
            #   (has_guarantee=0).  This ensures starvation prevention:
            #   every active project gets at least one task before
            #   proportional allocation kicks in.
            #
            # Level 2 — Deficit score (continuous):
            #   Among projects at the same guarantee level, the one whose
            #   actual token usage ratio is furthest *below* its target
            #   ratio (derived from credit_weight) sorts first.  A negative
            #   deficit means the project is under-served relative to its
            #   weight; a positive deficit means over-served.
            #
            # Together these produce a fair ordering: starved projects go
            # first, then under-served projects, then over-served ones.
            def project_sort_key(p: Project) -> tuple[int, float]:
                completed = state.tasks_completed_in_window.get(p.id, 0)
                has_guarantee = 1 if completed > 0 else 0  # 0 = needs guarantee (sorts first)
                target_ratio = p.credit_weight / total_weight
                actual_ratio = state.project_token_usage.get(p.id, 0) / total_tokens
                deficit = actual_ratio - target_ratio  # negative = below target
                return (has_guarantee, deficit)

            sorted_projects = sorted(active_projects, key=project_sort_key)

            for project in sorted_projects:
                # Check per-project budget
                if (
                    project.budget_limit is not None
                    and state.project_token_usage.get(project.id, 0) >= project.budget_limit
                ):
                    continue

                # Check concurrency limit.
                # When exclusive=True constraint is active, override to 1.
                max_agents = project.max_concurrent_agents
                constraint = state.project_constraints.get(project.id)
                if constraint and constraint.exclusive:
                    max_agents = 1
                current_agents = round_agent_counts.get(project.id, 0)
                if current_agents >= max_agents:
                    continue

                # Check per-agent-type limits from constraints.
                if (
                    constraint
                    and constraint.max_agents_by_type
                    and not _agent_type_allowed(
                        agent, project.id, constraint.max_agents_by_type, state, assigned_agents
                    )
                ):
                    continue

                # Skip projects with no available workspaces.
                # Workspace availability is a hard physical constraint: each
                # agent execution needs an exclusive workspace lock, so we
                # can't assign more tasks than there are unlocked workspaces.
                # When project_available_workspaces is empty (e.g. in tests),
                # this check is skipped — the orchestrator handles the
                # "no workspace" case gracefully in _prepare_workspace.
                if (
                    state.project_available_workspaces
                    and state.project_available_workspaces.get(project.id, 0) <= 0
                ):
                    continue

                # Pick highest priority ready task not yet assigned.
                # Also filter out tasks whose preferred workspace is locked
                # and tasks whose agent_type doesn't match the current agent.
                available = [
                    t
                    for t in ready_by_project.get(project.id, [])
                    if t.id not in assigned_tasks
                    and _workspace_available(t, state.workspace_locks)
                    and _task_agent_type_matches(t, agent)
                ]
                if not available:
                    continue

                # ── Agent affinity ordering (four tiers) ─────────────────
                #
                #  0 — Task prefers *this* agent: prioritize it.
                #  1 — Task has no affinity, or affinity wait expired
                #      (fallback): treat normally.
                #  2 — Task prefers *another* idle agent: defer so that
                #      agent can pick it up instead.
                #  3 — Task prefers a busy agent and bounded wait has
                #      NOT expired: defer (wait for preferred agent).
                #
                # Within each tier the existing priority/id ordering
                # (set by the pre-sort above) is preserved.
                #
                # Tier 3 implements the *bounded wait* from the spec:
                # when a task's preferred agent is busy, the scheduler
                # defers assignment for up to ``affinity_wait_seconds``
                # (measured from task.created_at).  After the wait
                # expires the task falls through to tier 1 so any idle
                # agent can pick it up — preventing starvation.
                #
                # This is advisory — if the only available tasks are in
                # tier 2 or 3, the current agent still picks one up (no
                # starvation).
                idle_agent_ids = {a.id for a in idle_agents if a.id not in assigned_agents}
                busy_agent_ids = {
                    a.id
                    for a in state.agents
                    if a.state == AgentState.BUSY and a.id not in idle_agent_ids
                }
                wait_limit = state.affinity_wait_seconds
                sched_now = state.now  # 0.0 disables time-based wait

                def _affinity_key(t: Task) -> tuple[int, int, str]:
                    aff = t.affinity_agent_id
                    if aff == agent.id:
                        # Tier 0 — task prefers *this* agent
                        return (0, t.priority, t.id)
                    if aff and aff in idle_agent_ids:
                        # Tier 2 — another idle agent is preferred
                        return (2, t.priority, t.id)
                    if (
                        aff
                        and aff in busy_agent_ids
                        and sched_now > 0
                        and wait_limit > 0
                        and t.created_at > 0
                    ):
                        # Preferred agent is busy — bounded wait?
                        waited = sched_now - t.created_at
                        if waited < wait_limit:
                            # Tier 3 — still within wait window
                            return (3, t.priority, t.id)
                    # Tier 1 — no affinity, unknown agent, or wait expired
                    return (1, t.priority, t.id)

                available.sort(key=_affinity_key)

                # If every candidate is in the bounded-wait tier (3),
                # skip this project for the current agent — the
                # preferred agents may become idle next cycle.  This
                # prevents assigning work to a non-preferred agent when
                # the wait window hasn't expired yet.
                #
                # We only skip if ALL tasks are tier 3; if at least one
                # task is in tier 0/1/2 we proceed normally (no
                # starvation).
                top_tier = _affinity_key(available[0])[0]
                if top_tier == 3:
                    continue

                task = available[0]

                # Log affinity reason for debugging when present.
                if task.affinity_reason:
                    aff_tier = top_tier
                    logger.debug(
                        "Affinity: task %s → agent %s "
                        "(preferred=%s, reason=%s, tier=%d)",
                        task.id,
                        agent.id,
                        task.affinity_agent_id,
                        task.affinity_reason,
                        aff_tier,
                    )

                actions.append(
                    AssignAction(
                        agent_id=agent.id,
                        task_id=task.id,
                        project_id=project.id,
                    )
                )
                assigned_agents.add(agent.id)
                assigned_tasks.add(task.id)
                round_agent_counts[project.id] = current_agents + 1
                break

        return actions
