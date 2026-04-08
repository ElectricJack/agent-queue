---
tags: [design, coordination, multi-agent, workflows]
---

# Agent Coordination — Playbook-Driven Multi-Agent Workflows

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#2 visible and editable, #3 structure guides intelligence, #7 events not coupling)
**Related:** [[playbooks]], [[vault-and-memory]], [[scheduler-and-budget]], [[models-and-state-machine]], [[orchestrator]]

---

## 1. Problem Statement

The current system has rigid, hard-coded rules for how agents work together:

**Fixed concurrency model.** `max_concurrent_agents` per project is a static number
in config. There's no way to say "run up to 3 coding agents in parallel, but only 1
if a code review agent is also active" or "this type of work needs exclusive access."
The scheduler treats all tasks as interchangeable units competing for agent slots.

**No workflow-level coordination.** When a feature needs coding → review → QA, this
is modeled as separate tasks with dependency edges. But the dependency system is
purely sequential (wait for completion, then start). There's no way to express
patterns like "start the review as soon as coding reaches a PR, not after it fully
completes" or "run the linter in parallel with the review" or "if review finds
issues, route back to the original coding agent, not a random one."

**Implicit pipelines.** Multi-step workflows emerge from task dependencies and
plan-based subtask generation, but there's no first-class concept of a workflow.
You can't see, modify, or reason about the pipeline as a whole — only individual
tasks and their edges.

**Workspace contention is brute-force.** Workspaces use exclusive locks — one agent
per workspace. If two agents could safely work on different parts of the same repo
(different directories, different branches), they can't. The lock is all-or-nothing.

**No agent affinity.** When a task fails and is retried, or when a code review finds
issues that need fixing, the system assigns whatever idle agent is available. Context
from the original work — what the agent learned about the codebase, what approaches
it tried — is lost. There's no concept of "prefer the agent that already has context
for this work."

**One-size-fits-all scheduling.** The deficit-based scheduler optimizes for fair
resource distribution across projects. But some workflows need different strategies:
latency-sensitive work should be scheduled immediately; batch work can wait; dependent
task chains should be prioritized to unblock downstream work.

---

## 2. Vision

Agent coordination rules should be defined as **[[playbooks]]** — the same system
used for automation. A coordination playbook describes how agents collaborate on a
type of work: what pipeline stages exist, how work flows between agents, and what
happens when something goes wrong.

This means coordination patterns are:
- **Visible** — a human can read the playbook and understand the workflow
- **Editable** — change the coordination strategy by editing a markdown file
- **Per-project or per-workflow** — different work types can use different patterns
- **Evolvable** — the system can learn better coordination patterns over time

The scheduler becomes a **policy executor** rather than a policy definer. It still
handles the mechanics of agent assignment, but the coordination playbooks define the
strategy.

---

## 3. Core Concepts

### Coordination Playbook

A coordination playbook is a regular [[playbooks|playbook]] (same format, same
execution model) whose purpose is to orchestrate multi-agent work rather than
execute a single task. It triggers on events like `task.created` or
`workflow.started` and its actions involve creating tasks, assigning agents, and
managing the flow between them.

Coordination playbooks live alongside other playbooks in the
[[vault-and-memory#4. Vault Structure|vault]]:
- `vault/system/playbooks/` for system-wide coordination patterns
- `vault/projects/{id}/playbooks/` for project-specific coordination
- `vault/agent-types/{type}/playbooks/` for agent-type-specific behavior

### Workflow

A workflow is a **runtime instance** of a coordination playbook — a running
multi-agent process. It has:
- An ID and a reference to its source playbook
- A set of tasks it manages
- State tracking (which stage is active, what's pending)
- A lifecycle (created → running → completed/failed)

Workflows are the unit of coordination. The scheduler reasons about workflows, not
just individual tasks.

### Stage

A stage is a phase of a workflow where one or more agents work in parallel on
related tasks. Stages have:
- Entry conditions (prior stage completed, or event received)
- Agent requirements (type, count, affinity preferences)
- Exit conditions (all tasks complete, or threshold met)
- Failure handling (retry, escalate, route back to prior stage)

### Agent Affinity

A preference (not a hard requirement) for which agent should handle a task. Affinity
is based on:
- **Context continuity** — prefer the agent that previously worked on related tasks
  (it has relevant conversation history and workspace state). Over time,
  [[vault-and-memory#11. The Self-Improvement Loop|agent-type memory]] reduces the
  cost of context loss, but affinity still helps for in-flight workflows.
- **Workspace locality** — prefer an agent that already has the workspace locked
- **Type matching** — a review task should go to a review agent, not a coding agent.
  Agent types are defined by [[vault-and-memory#7. Profiles as Markdown|profiles]]
  in the vault.

Affinity is advisory. The scheduler respects it when possible but overrides it when
the preferred agent is unavailable or when it would cause starvation.

---

## 4. Coordination Playbook Examples

### Example 1: Feature Pipeline

```markdown
---
id: feature-pipeline
triggers:
  - type: task.created
    filter:
      task_type: FEATURE
scope: system
---

# Feature Pipeline

When a feature task is created, coordinate coding, review, and QA.

Start by assigning the feature task to a coding agent. Prefer an agent
that has recently worked on this project — context continuity matters
more than immediate availability.

When the coding agent creates a PR, create two tasks: a code review
task (agent type: code-review) and a QA task (agent type: qa). Both
depend on the coding task — the scheduler will run them concurrently
since neither depends on the other.

If the review requests changes:
  Create a fix task assigned to the original coding agent (affinity).
  The fix task depends on the review task. Create new review and QA
  tasks that depend on the fix. Limit to 3 review cycles.

If QA finds failures:
  Create a bugfix task for the original coding agent (affinity).
  Create a new QA task that depends on the bugfix.

When both review and QA pass (both complete with no issues), auto-merge
the PR if the project allows auto-merge. Otherwise, notify for human
merge.
```

### Example 2: Parallel Exploration

```markdown
---
id: parallel-exploration
triggers:
  - type: task.created
    filter:
      task_type: EXPLORATION
scope: system
---

# Parallel Exploration

When an exploration task is created, investigate multiple approaches.

Create up to 3 subtasks, each exploring a different angle. Each is a
coding task on a separate branch (workspace mode: branch-isolated).
None depend on each other — the scheduler will run them concurrently
if agents are available.

Create a review task (agent type: code-review) that depends on all
3 exploration tasks. The reviewer compares the approaches and
recommends the best one.

Create a summary task that depends on the review, implementing the
chosen approach and archiving the unchosen branches.
```

### Example 3: Exclusive Access Work

```markdown
---
id: database-migration
triggers:
  - type: task.created
    filter:
      labels: [migration, database]
scope: project
---

# Database Migration

Database migrations require exclusive access to the workspace.
No other agents should be working on this project while a migration
runs.

When a migration task is created, wait for all currently running
tasks on this project to complete (do not start new ones). Then
assign the migration to a coding agent with exclusive project access.

When the migration completes, run the full test suite via a QA agent
before allowing other work to resume.
```

---

## 5. How Coordination Playbooks Change the Scheduler

The scheduler currently makes all decisions. With coordination playbooks, the
decision-making splits:

### What the Scheduler Owns: All Concurrency Decisions

The scheduler owns **when and whether** tasks run concurrently. It already has the
dependency DAG — if tasks B and C both depend on A, and A completes, the scheduler
knows B and C are both `READY` and can run them in parallel if agents are available.
Parallelism is an emergent property of the dependency graph, not an explicit
directive.

The scheduler continues to handle:
- **Concurrency** — algorithmically determined from task dependencies and agent
  availability. If two tasks are both READY, they can run in parallel.
- **Agent lifecycle** — track idle/busy state, health, heartbeats
- **Resource accounting** — token budgets, rate limits, cost tracking
- **Fairness** — deficit-based allocation across projects
- **Assignment mechanics** — lock workspace, launch adapter process

### What Coordination Playbooks Own: Workflow Structure

Coordination playbooks define **what work exists and how it relates** — they build
the DAG that the scheduler then executes. Playbooks handle:

- **Task creation and dependencies** — breaking work into tasks with dependency
  edges that express the correct ordering. The scheduler infers concurrency from
  these edges.
- **Agent type requirements** — this task needs a coding agent, that one needs a
  reviewer
- **Affinity preferences** — prefer the agent with existing context (advisory)
- **Flow control** — when a stage completes, create the next stage's tasks.
  When review requests changes, create a fix task that depends on the review.
- **Constraints** — "this migration needs exclusive project access" or "max 1
  review agent at a time." These are **limits** the scheduler respects, not
  scheduling decisions.

**The playbook never says "run these in parallel."** It says "B and C both depend
on A" — and the scheduler infers that B and C can run concurrently once A
completes. The playbook defines structure; the scheduler determines execution.

### The Interface Between Them

Coordination playbooks communicate with the scheduler through **commands and events**,
not by reaching into the scheduler's internals:

**Commands the playbook can issue** (via Supervisor tools):
- `create_task` — with dependencies, agent type, affinity, priority
- `set_project_constraint` — temporary constraints (exclusive access,
  max agents of a type, pause new assignments)
- `release_project_constraint` — remove a previously set constraint

**Events the playbook listens to:**
- `task.completed`, `task.failed` — stage transitions
- `git.pr.created` — trigger review stage
- `git.commit` — trigger parallel linting
- `workflow.stage.completed` — internal stage progression

**Fallback:** Tasks created without a coordination playbook (simple standalone tasks)
are scheduled exactly as today — the deficit-based algorithm assigns them. Playbooks
are opt-in, not required.

---

## 6. Workflow Runtime

### Workflow State

Workflows are tracked in the database alongside tasks:

| Field | Type | Description |
|---|---|---|
| `workflow_id` | str | Unique identifier |
| `playbook_id` | str | Source coordination playbook |
| `playbook_run_id` | str | The PlaybookRun driving this workflow |
| `project_id` | str | Project this workflow operates in |
| `status` | str | `running`, `paused`, `completed`, `failed` |
| `current_stage` | str | Active stage name |
| `task_ids` | JSON | All tasks created by this workflow |
| `agent_affinity` | JSON | Maps task/stage to preferred agent IDs |
| `created_at` | float | Unix timestamp |
| `completed_at` | float | Null until finished |

### Workflow ↔ PlaybookRun Relationship

A coordination playbook is executed by the same
[[playbooks#6. Execution Model|PlaybookRunner]] as any other playbook. The
`workflow_id` is created in the first node of the playbook and tracked through
the conversation. The playbook's nodes create tasks, listen for events, and manage
stage transitions — all through the standard node → LLM → tools → transition flow.

This means: **no new execution engine is needed.** Coordination is just another
thing [[playbooks]] can do.

### Agent Affinity Implementation

When a coordination playbook creates a task, it can specify affinity:

```
create_task(
    title="Address review feedback on auth module",
    agent_type="coding",
    affinity_agent_id="agent-3",    # Prefer this agent
    affinity_reason="context",       # Why: context continuity
    priority=2,                      # Higher priority for unblocking
)
```

The scheduler checks affinity during assignment:
1. If the preferred agent is idle → assign to it
2. If the preferred agent is busy but will be idle soon (has one in-progress task
   nearing completion) → wait up to N seconds before falling back
3. If the preferred agent is unavailable → assign any matching agent type

Affinity is a **preference with bounded wait**, not a hard lock. This prevents
affinity from causing starvation.

---

## 7. Workspace Strategy

The current exclusive-lock model is the correct default — most coordination patterns
don't need shared workspace access. But some workflows benefit from relaxed locking:

### Lock Modes

| Mode | Description | Use Case |
|---|---|---|
| `exclusive` | One agent, one workspace. Current behavior. | Default, database migrations |
| `branch-isolated` | Multiple agents, same repo, different branches. Agents must not touch each other's branches. | Parallel exploration, feature + review on same repo |
| `directory-isolated` | Multiple agents, same branch, different directories. Requires explicit directory scoping in the task. | Monorepo with independent packages |

The coordination playbook specifies the lock mode when creating tasks:

```
create_task(
    title="Explore approach A",
    workspace_mode="branch-isolated",
    branch="explore/approach-a",
)
```

**Branch-isolated** is the most practical relaxation. Agents work on separate branches
within the same workspace clone. Git handles isolation. The only contention point is
shared git state (fetch, gc), which can be serialized with a lightweight mutex.

**Directory-isolated** is future work and needs careful design around shared files
(configs, lock files, root-level changes). Deferred.

---

## 8. Relationship to Existing Systems

### What Changes

| Current | New |
|---|---|
| `max_concurrent_agents` is a static config number | Still the default, but playbooks can set temporary constraints (exclusive access, type limits) |
| Task dependencies are edges in a flat DAG | Playbooks build richer DAGs with agent type requirements, affinity, and stage semantics. Scheduler still determines concurrency from the DAG. |
| Scheduler picks any idle agent for any ready task | Playbooks express agent type and affinity preferences; scheduler respects them during assignment |
| Workspace locks are always exclusive | Branch-isolated mode allows parallel work on separate branches |
| Pipeline structure is implicit in task dependencies | Workflows are first-class, visible, editable — the playbook that built the DAG is readable in the vault |

### What Stays the Same

- Scheduler's core loop (tick-based, snapshot, assign)
- Token budgets and rate limiting
- Task state machine (all existing states and events)
- Adapter system (one adapter per agent per task)
- The EventBus as the communication backbone

### Backward Compatibility

Tasks created without a coordination playbook are scheduled exactly as today. The
coordination system is **purely additive** — it extends the scheduler's capabilities
without changing its default behavior. Existing task dependencies, priorities, and
the deficit-based algorithm all continue to work.

---

## 9. Default Coordination Playbooks

Ship with sensible defaults that encode current best practices:

| Playbook | Triggers On | Behavior |
|---|---|---|
| `feature-pipeline` | FEATURE task created | Code → Review → QA pipeline with affinity |
| `bugfix-pipeline` | BUGFIX task created | Code → QA (skip review for small fixes) |
| `review-cycle` | PR created | Assign reviewer, handle feedback loops |
| `exploration` | EXPLORATION task | Parallel multi-agent investigation |

These are starting points. Users customize or replace them by editing the markdown
in the [[vault-and-memory#4. Vault Structure|vault]] — per
[[guiding-design-principles#2. Everything is visible and editable|principle #2]].

---

## 10. Migration Path

### Phase 1: Workflow Tracking

- Add `workflow_id` field to tasks (nullable, backward-compatible)
- Add workflow table to database
- No behavior changes — workflows are just metadata at this point

### Phase 2: Coordination Commands

- Add scheduler commands (`set_project_concurrency`, `pause_project_scheduling`,
  `request_exclusive_access`, `assign_task_to_agent`)
- Add `affinity_agent_id` to task creation
- These work via direct command calls, no playbooks yet

### Phase 3: Coordination Playbooks

- Write default coordination playbooks
- Playbook executor gains ability to listen for task events mid-run
  (long-running playbooks that park and resume as stages progress)
- Wire playbook triggers to `task.created` events with type filters

### Phase 4: Workspace Relaxation

- Implement branch-isolated lock mode
- Update workspace acquisition to check lock mode
- Add git mutex for shared operations (fetch, gc)

---

## 11. Open Questions

1. **Long-running playbook instances.** A feature pipeline playbook might run for
   hours across multiple stages. The current PlaybookRun model assumes relatively
   short runs. How do long-running coordination playbooks interact with
   `wait_for_human` and conversation history limits? Should they use a series of
   event-triggered resumptions rather than one long-running instance?

2. **Coordination playbook failures.** If the coordination playbook itself crashes
   mid-workflow, what happens to the tasks it created? They should continue running
   (they're independent entities) but who manages the stage transitions? An orphan
   recovery mechanism is needed.

3. **Scheduler autonomy vs. playbook control.** How much authority does a coordination
   playbook have over the scheduler? Can it truly override scheduling decisions, or
   only express preferences? If two coordination playbooks conflict (both want
   exclusive project access), who wins?

4. **Agent state preservation.** Affinity is about preferring an agent that has
   context. But agent state (conversation history, workspace knowledge) is currently
   ephemeral — lost when a task ends. Should the system preserve agent context between
   tasks in a workflow? This could be expensive but would make affinity much more
   valuable.

5. **Dynamic workflow modification.** Can a coordination playbook modify its own
   workflow mid-run? For example, adding an extra QA stage if review flagged
   security concerns. This is natural in the playbook model (the LLM decides) but
   needs the workflow tracker to handle structural changes.

6. **Visualization.** The dashboard should show workflows as pipelines — stages
   with tasks in each, agent assignments, and current progress. How does this
   relate to the playbook graph visualization in `playbooks.md`?
