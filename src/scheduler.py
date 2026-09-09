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
from dataclasses import dataclass, field, replace
from collections.abc import Mapping

from src.agents.routing import resolve_agent_profile, resolve_task_profile, task_agent_mismatch
from src.assignment_routing import EffectiveAssignmentRoute
from src.models import (
    Agent,
    AgentProfile,
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
    # Durable DB eligibility for hierarchy-enabled READY tasks.  ``None``
    # preserves pure legacy/unit callers; production always supplies the
    # snapshot computed from materialized origin rows.
    hierarchy_runnable_task_ids: set[str] | None = None
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
    # Provider-level cooldowns: maps profile_id (e.g. "claude-opus") to the
    # Unix timestamp when the cooldown expires.  Agents whose profile is
    # cooled-down are excluded from scheduling until the timestamp passes.
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

    # Execution metadata is a read-only snapshot. None retains compatibility
    # for callers that do not provide profiles/classes (legacy unit fixtures).
    profiles: dict[str, AgentProfile] | None = None
    harness_registry: object | None = None
    intelligence_classes: dict | None = None
    # ``None`` preserves legacy pure-scheduler fixtures. The orchestrator
    # always supplies a mapping; a missing task key then means "not routed".
    assignment_routes: Mapping[str, EffectiveAssignmentRoute] | None = None


def idle_workers(state: SchedulerState, *, include_cooldown: bool = False) -> list[Agent]:
    """Global worker availability shared by matching and diagnostics."""
    import time

    now = state.now or time.time()
    return [
        agent for agent in state.agents
        if agent.state == AgentState.IDLE and agent.current_task_id is None
        and agent.enabled and agent.role == "worker"
        and getattr(agent, "deleted_at", None) is None
        and (include_cooldown or state.provider_cooldowns.get(agent.profile_id, 0) <= now)
    ]

def routing_mismatch(task: Task, agent: Agent, state: SchedulerState) -> str | None:
    """Use the same execution compatibility rule as pre-launch admission."""
    profiles = state.profiles or {}
    project = next((p for p in state.projects if p.id == task.project_id), None)
    profile = resolve_task_profile(task, project, profiles)
    if state.profiles is not None and task.profile_id and profile is None:
        return f"required profile '{task.profile_id}' is not available"
    required_provider = None
    if state.assignment_routes is not None:
        route = state.assignment_routes.get(task.id)
        if route is None:
            return "awaiting intelligence route"
        task = replace(task, intelligence_class=route.intelligence_class)
        required_provider = route.provider
    elif not task.intelligence_class and profile is not None:
        # Compatibility for direct Scheduler callers that predate route-aware
        # snapshots. Production always passes assignment_routes.
        task = replace(task, intelligence_class=profile.default_class or None)
    return task_agent_mismatch(
        task, agent, task_profile=profile,
        agent_profile=resolve_agent_profile(agent, profiles),
        harness_registry=state.harness_registry,
        intelligence_classes=state.intelligence_classes,
        required_provider=required_provider,
    )


def _workspace_available(task: Task, locks: dict[str, str | None]) -> bool:
    """Check if a task's preferred workspace is available (unlocked).

    Tasks without a preferred_workspace_id are always eligible.
    When locks is empty (e.g. in tests), no filtering is applied.
    """
    if not task.preferred_workspace_id or not locks:
        return True
    return locks.get(task.preferred_workspace_id) is None


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
    atype = agent.profile_id
    if atype not in max_by_type:
        return True  # no limit for this type

    limit = max_by_type[atype]

    # Count agents of the same type currently working on this project.
    # An agent is "active on a project" if it is BUSY and its current
    # task belongs to the project.
    count = 0
    for a in state.agents:
        if a.profile_id != atype or a.id in assigned_agents:
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

        idle_agents = idle_workers(state)
        if not idle_agents:
            return []

        # Group ready tasks by project
        ready_by_project: dict[str, list[Task]] = {}
        for task in state.tasks:
            if (
                task.status == TaskStatus.READY
                and not task.is_blocked
                and (
                    state.hierarchy_runnable_task_ids is None
                    or task.id in state.hierarchy_runnable_task_ids
                )
            ):
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
                # Also filter out tasks whose preferred workspace is locked.
                available = [
                    t
                    for t in ready_by_project.get(project.id, [])
                    if t.id not in assigned_tasks
                    and _workspace_available(t, state.workspace_locks)
                    and routing_mismatch(t, agent, state) is None
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


# ---------------------------------------------------------------------------
# Worker pools (swarm-work-model §11) — desired-state pool sizing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolKey:
    """Identifies one worker pool — a profile, fleet-wide.

    A pool used to be keyed ``(project_id, profile_id)``, which quietly
    multiplied ``min_active`` / ``max_active`` by the number of active
    projects: one profile across five active projects was five independent
    pools with five independent floors and ceilings.  The *configuration*
    was always global (profiles are global, and ``pool_scale`` already
    deprecated its ``project_id``), so the key is global now too, and
    *which* project each authorised start lands in is decided afterwards by
    :func:`place_pool_actions`.
    """

    profile_id: str


@dataclass
class PoolProjectSupply:
    """One project's share of a pool's observed sessions, this tick.

    The same five counters :class:`PoolSupply` carries, restricted to a
    single project.  Sizing never reads these — the aggregate is
    authoritative there — but placement does: this breakdown is what lets
    :func:`place_pool_actions` choose where a start goes, and which idle
    session a drain takes, without a second measurement pass.
    """

    running_idle: int = 0
    running_busy: int = 0
    starting: int = 0
    draining: int = 0
    idle_session_ids: list[str] = field(default_factory=list)  # oldest first


@dataclass
class PoolSupply:
    """Observed pool session counts for one :class:`PoolKey`, this tick.

    The top-level counters are fleet-wide sums over every project running
    this profile, and they are the only thing :func:`size_pools` reads.
    ``by_project`` carries the same numbers broken down per project, for the
    placement step that follows.
    """

    running_idle: int = 0  # running, claim_phase NULL, task_id NULL
    running_busy: int = 0  # running, task_id set (any claim_phase)
    starting: int = 0  # state == 'starting'
    draining: int = 0  # desired_state == 'stopped'
    idle_session_ids: list[str] = field(default_factory=list)  # oldest first
    by_project: dict[str, PoolProjectSupply] = field(default_factory=dict)


@dataclass(frozen=True)
class PoolAction:
    """One start or drain to execute for a pool this tick."""

    key: PoolKey
    kind: str  # "start" | "drain"
    count: int
    session_ids: tuple[str, ...] = ()  # for drains


def size_pools(
    *,
    supply: dict[PoolKey, PoolSupply],
    demand: dict[PoolKey, int],
    bounds: dict[PoolKey, tuple[int, int | None]],  # (min_active, max_active)
    global_cap: int | None,
    surplus_since: dict[PoolKey, float],
    now: float,
    scale_down_grace: float,
    max_starts_per_tick: int,
    max_drains_per_tick: int,
) -> tuple[list[PoolAction], dict[PoolKey, float]]:
    """Desired-state pool sizing (swarm-work-model §11.1).  Pure — no I/O, no clock.

    ``want = busy + ready``; ``desired = clamp(want, min_active, max_active)``
    (``max_active=None`` is unbounded); ``desired`` is then floored at
    ``busy + starting`` so a launch already in flight or a task already
    claimed never gets undercut mid-task.  All three quantities are now
    fleet-wide sums, so that floor protects a launch in flight in project A
    and a task held in project B alike.

    Scale-up hands out ``max_starts_per_tick`` one start at a time,
    round-robin across the pools that still want more and bounded by the
    box-wide ``global_cap``, so a saturated fleet fair-shares whatever
    headroom is left instead of starving later pools in iteration order.
    Per-project caps are deliberately *not* considered here: they are a
    property of a project, not of a pool, and belong to
    :func:`place_pool_actions` — the only step that still knows projects
    exist.

    Scale-down only touches idle sessions, oldest first, and only after a
    pool has been in continuous surplus for ``scale_down_grace`` seconds
    (tracked via ``surplus_since``, returned updated) — never mid-task, and
    never flapping on a one-tick dip in demand.  The ``session_ids`` on a
    drain action are the fleet-wide oldest-first idle set; placement
    re-selects which of them actually go, because "oldest in the fleet" is a
    poor answer once a quiet project's one warm worker is in the running.
    """
    actions: list[PoolAction] = []
    new_surplus: dict[PoolKey, float] = {}
    keys = sorted(set(supply) | set(demand) | set(bounds), key=lambda k: k.profile_id)
    desired: dict[PoolKey, int] = {}
    current: dict[PoolKey, int] = {}
    for key in keys:
        sup = supply.get(key, PoolSupply())
        lo, hi = bounds.get(key, (0, None))
        want = sup.running_busy + demand.get(key, 0)
        d = max(lo, want)
        if hi is not None:
            d = min(d, hi)
        d = max(d, sup.running_busy + sup.starting)
        desired[key] = d
        current[key] = sup.running_idle + sup.running_busy + sup.starting

    # --- scale up: round-robin across profiles, under the global cap ------
    starts: dict[PoolKey, int] = {k: 0 for k in keys}
    used_global = sum(current.values())
    budget = max_starts_per_tick
    progressed = True
    while budget > 0 and progressed:
        progressed = False
        for key in keys:
            if current[key] + starts[key] >= desired[key]:
                continue
            if global_cap is not None and used_global >= global_cap:
                continue
            starts[key] += 1
            used_global += 1
            budget -= 1
            progressed = True
            if budget == 0:
                break
    for key in keys:
        if starts[key]:
            actions.append(PoolAction(key=key, kind="start", count=starts[key]))

    # --- scale down: grace, then idle sessions oldest-first, bounded per tick
    drains_left = max_drains_per_tick
    for key in keys:
        sup = supply.get(key, PoolSupply())
        surplus = current[key] - desired[key]
        if surplus <= 0:
            continue
        since = surplus_since.get(key, now)
        new_surplus[key] = since
        if now - since < scale_down_grace or drains_left == 0:
            continue
        n = min(surplus, sup.running_idle, drains_left)
        if n <= 0:
            continue
        actions.append(
            PoolAction(key=key, kind="drain", count=n, session_ids=tuple(sup.idle_session_ids[:n]))
        )
        drains_left -= n
    return actions, new_surplus


# ---------------------------------------------------------------------------
# Placement — which project each sized start or drain applies to.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlacementCandidate:
    """One project's standing as a home for a pool's next start or drain.

    Everything :func:`place_pool_actions` is allowed to know about a
    project, gathered once by ``_measure_pools``.  ``starting`` is broken
    out of ``live`` because both the unserved-deficit ordering and the drain
    surplus rule are defined against it: a launch already in flight serves
    the ready queue exactly as an idle worker does, and neither ordering can
    be computed from ``live`` alone.
    """

    project_id: str
    ready: int  # ready tasks for this profile in this project
    live: int  # idle + busy + starting pool sessions, this profile
    project_live_total: int  # all pool sessions in this project, any profile
    project_cap: int | None  # project.max_concurrent_agents
    workspace_capacity: int  # count_available_workspaces
    quarantined: bool  # (project, profile) inside its backoff window
    warm_floor: int  # profile.min_per_project
    idle_session_ids: tuple[str, ...] = ()  # oldest first
    starting: int = 0  # of ``live``, how many are still booting


@dataclass(frozen=True)
class PlacedStart:
    """``count`` starts for *key*, in *project_id*, and why they went there."""

    key: PoolKey
    project_id: str
    count: int
    reason: str  # "warm_floor" | "deficit" | "spread"


@dataclass(frozen=True)
class PlacedDrain:
    """The idle sessions of *key* in *project_id* that this tick stops."""

    key: PoolKey
    project_id: str
    session_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlacementStarvation:
    """*key* was authorised ``wanted`` starts and no project could take them.

    Invisible before this existed: the start was attempted, the launch
    returned ``None``, and the only trace was a log line.  ``reasons`` maps
    each candidate project to the first predicate it failed, which is the
    whole diagnosis — "every project is quarantined" and "every project is
    out of worktree slots" want very different operator responses.
    """

    key: PoolKey
    wanted: int
    reasons: dict[str, str]


#: Why a candidate could not take a start, in the order the predicates are
#: evaluated.  Reported per project on a :class:`PlacementStarvation`.
_NO_CANDIDATES = "no active project runs this profile"


def _start_ineligibility(
    cand: PlacementCandidate, *, live: int, total: int, capacity: int, unserved: int
) -> str:
    """First start predicate *cand* fails under this tick's running state, or ``""``.

    Every argument is a *working* value, not the measured one: within a tick
    a start already placed here has consumed a workspace, taken a slot under
    the project cap and put itself in front of the ready queue, and the next
    placement decision has to see all three.
    """
    if cand.quarantined:
        return "quarantined"
    if capacity <= 0:
        return "no workspace capacity"
    if cand.project_cap is not None and total >= cand.project_cap:
        return f"at project cap ({cand.project_cap})"
    if live >= cand.warm_floor and cand.idle_session_ids and unserved <= 0:
        # An idle worker is already sitting here with nothing queued behind
        # it: it will claim the next ready task itself, so launching beside
        # it only manufactures a drain candidate two minutes from now.
        return "an idle worker is already available"
    return ""


def place_pool_actions(
    *,
    actions: list[PoolAction],
    candidates: dict[PoolKey, list[PlacementCandidate]],
) -> tuple[list[PlacedStart], list[PlacedDrain], list[PlacementStarvation]]:
    """Assign each sized start/drain to a project.  Pure — no I/O, no clock.

    :func:`size_pools` decides *how many* workers a profile should have
    fleet-wide; this decides *where* they go, which is the half of the
    problem that still cares about projects.  A worker is bound to one
    project for its lifetime (its workspace and its token's scope fence are
    both minted at launch), so this is the only moment the choice is made.

    Starts are handed out one at a time, re-sorting after each, so a budget
    of two does not pile onto whichever project sorts first:

    1. projects below their ``warm_floor``, largest shortfall first — a
       stated per-project reservation outranks raw demand;
    2. then by unserved deficit ``ready - (idle + starting)``, largest first;
    3. then by fewest ``live`` sessions, spreading rather than concentrating;
    4. then ``project_id`` ascending, so the result is deterministic and a
       test can assert on it.

    Drains invert it: a project at or below its warm floor is excluded
    outright, and the rest are ordered by idle surplus *relative to their own
    demand* (``idle - max(0, ready - starting)``) so a quiet project's single
    warm worker is not reaped first simply for being idle and old.

    Returns ``(placed_starts, placed_drains, starvations)``; a start budget
    that no project could absorb comes back as a
    :class:`PlacementStarvation` rather than being dropped silently.
    """
    placed_starts: list[PlacedStart] = []
    placed_drains: list[PlacedDrain] = []
    starvations: list[PlacementStarvation] = []

    for action in actions:
        cands = {c.project_id: c for c in candidates.get(action.key, [])}
        # Working copies: placements within this tick move these, the
        # measured ``PlacementCandidate`` values stay as observed.
        live = {pid: c.live for pid, c in cands.items()}
        total = {pid: c.project_live_total for pid, c in cands.items()}
        capacity = {pid: c.workspace_capacity for pid, c in cands.items()}
        idle_ids = {pid: list(c.idle_session_ids) for pid, c in cands.items()}
        # Starts placed here this tick.  A worker on its way into a project
        # serves that project's ready queue exactly as a booting one does,
        # so the unserved-deficit ordering has to count it or a single deep
        # backlog swallows the whole budget on stale arithmetic.
        pending = {pid: 0 for pid in cands}

        def unserved(c: PlacementCandidate) -> int:
            pid = c.project_id
            return c.ready - (len(idle_ids[pid]) + c.starting + pending[pid])

        if action.kind == "start":
            # (project_id, reason) -> count, insertion-ordered so the caller
            # emits one event per project in the order placement chose them.
            chosen: dict[tuple[str, str], int] = {}
            unplaced = 0
            blocked: dict[str, str] = {}
            for _ in range(action.count):
                eligible = []
                blocked = {}
                for pid, cand in cands.items():
                    why = _start_ineligibility(
                        cand,
                        live=live[pid],
                        total=total[pid],
                        capacity=capacity[pid],
                        unserved=unserved(cand),
                    )
                    if why:
                        blocked[pid] = why
                        continue
                    eligible.append(cand)
                if not eligible:
                    unplaced = action.count - sum(chosen.values())
                    break
                eligible.sort(
                    key=lambda c: (
                        0 if live[c.project_id] < c.warm_floor else 1,
                        -max(0, c.warm_floor - live[c.project_id]),
                        -unserved(c),
                        live[c.project_id],
                        c.project_id,
                    )
                )
                pick = eligible[0]
                pid = pick.project_id
                if live[pid] < pick.warm_floor:
                    reason = "warm_floor"
                elif unserved(pick) > 0:
                    reason = "deficit"
                else:
                    reason = "spread"
                chosen[(pid, reason)] = chosen.get((pid, reason), 0) + 1
                live[pid] += 1
                total[pid] += 1
                capacity[pid] -= 1
                pending[pid] += 1
            for (pid, reason), count in chosen.items():
                placed_starts.append(
                    PlacedStart(key=action.key, project_id=pid, count=count, reason=reason)
                )
            if unplaced:
                starvations.append(
                    PlacementStarvation(
                        key=action.key,
                        wanted=unplaced,
                        reasons=blocked or {"": _NO_CANDIDATES},
                    )
                )
            continue

        # --- drains ------------------------------------------------------
        taken: dict[str, list[str]] = {}
        for _ in range(action.count):
            eligible = [
                c for pid, c in cands.items() if idle_ids[pid] and live[pid] > c.warm_floor
            ]
            if not eligible:
                break
            eligible.sort(
                key=lambda c: (
                    -(len(idle_ids[c.project_id]) - max(0, c.ready - c.starting)),
                    c.project_id,
                )
            )
            pid = eligible[0].project_id
            taken.setdefault(pid, []).append(idle_ids[pid].pop(0))
            live[pid] -= 1
        for pid, session_ids in taken.items():
            placed_drains.append(
                PlacedDrain(key=action.key, project_id=pid, session_ids=tuple(session_ids))
            )

    return placed_starts, placed_drains, starvations
