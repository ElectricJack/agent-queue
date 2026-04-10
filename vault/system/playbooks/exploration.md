---
id: parallel-exploration
triggers:
  - type: task.created
    filter:
      task_type: EXPLORATION
scope: system
---

# Parallel Exploration

Coordinate a multi-agent investigation when an exploration task is created.
Instead of assigning the exploration to a single agent, fan out into multiple
parallel research tracks, then converge through review and synthesis.

This playbook builds a dependency DAG that the scheduler executes. Parallelism
is not directed explicitly — it emerges because the exploration subtasks have
no dependencies on each other. The scheduler runs them concurrently if agents
are available.

## Analyze the exploration request

Read the triggering task's description, acceptance criteria, and any attached
context (linked specs, referenced files, project knowledge). Determine what
is being explored — a design question, a technology choice, an implementation
strategy, a performance investigation, etc.

Identify 2–3 distinct angles worth investigating in parallel. Each angle
should represent a meaningfully different approach, not minor variations of
the same idea. Good angles diverge early: different libraries, different
architectures, different algorithms, different trade-off priorities.

If the exploration is narrow enough that parallel investigation adds no
value (e.g. "check if library X supports feature Y"), skip the fan-out.
Create a single task and a summary task that depends on it.

## Create exploration subtasks

For each angle identified (up to 3), create a coding task using
`create_task`:

- **Title:** a short description of the specific angle being explored
  (e.g. "Explore Redis-based caching approach", "Explore SQLite FTS5
  for search")
- **Description:** what to investigate, what to prototype or spike,
  what questions to answer. Include enough context from the parent
  exploration task that the assigned agent can work independently.
- **Task type:** coding
- **Workspace mode:** `branch-isolated` — each exploration works on
  its own branch so agents don't interfere with each other. Use
  branch names like `explore/{parent-task-id}/approach-a`,
  `explore/{parent-task-id}/approach-b`, etc.
- **Dependencies:** none — these tasks are independent. The scheduler
  will run them concurrently when agents are available.
- **Priority:** inherit from the parent exploration task

Track the IDs of all created subtasks. Record the workflow's
`agent_affinity` mapping so later stages can reference which agent
explored which angle.

## Create the review task

Create a single review task using `create_task`:

- **Title:** "Review exploration results: {parent task title}"
- **Agent type:** `code-review`
- **Description:** compare the approaches explored in the subtasks.
  For each approach, evaluate: correctness, complexity, performance
  characteristics, maintenance burden, alignment with project
  conventions, and risk. Recommend which approach to adopt, with
  rationale. If none of the approaches are viable, explain why and
  suggest what to try next.
- **Dependencies:** all exploration subtasks — the scheduler will not
  start this task until every subtask has completed.
- **Priority:** one level higher than the exploration subtasks, so
  the review is scheduled promptly once unblocked.

Include the branch names of each exploration subtask in the review
task's description so the reviewer knows where to find each approach's
code.

## Create the summary task

Create a final synthesis task using `create_task`:

- **Title:** "Implement chosen approach: {parent task title}"
- **Agent type:** `coding`
- **Description:** based on the review's recommendation, implement the
  chosen approach on the project's main development branch. Merge or
  cherry-pick from the winning exploration branch. Archive the
  unchosen branches (delete remote branches, optionally tag them for
  reference). Update the parent exploration task's description or
  notes with a summary of what was explored and why the chosen
  approach won.
- **Dependencies:** the review task — this task starts only after
  the reviewer has made a recommendation.
- **Affinity:** prefer the agent that explored the chosen approach
  (it already has context on that code). Use `affinity_agent_id`
  from the workflow's agent affinity map, with
  `affinity_reason: "context"`.
- **Priority:** inherit from the parent exploration task.

## Handle edge cases

If any exploration subtask fails:
  The review task still depends on all subtasks, so it will not start
  until the failed task is resolved. Check the failure reason. If it
  is transient (rate limit, timeout), restart the failed subtask. If
  it is a substantive failure (the approach hit a dead end), mark the
  subtask as completed with a note that this angle was not viable —
  the reviewer should account for this in the comparison.

If the review task recommends none of the approaches:
  Create a new exploration task with the reviewer's feedback
  incorporated, suggesting alternative angles. Do not loop
  indefinitely — if this is the second round of exploration with
  no viable result, escalate to the human operator via a note on
  the parent task.

If fewer than 2 agents are available:
  The scheduler handles this automatically. Exploration subtasks
  run sequentially if only one agent is free. The playbook does not
  need to adjust — parallelism is the scheduler's concern, not the
  playbook's.
