---
id: coding-reflection
triggers:
  - task.completed
  - task.failed
scope: agent-type:coding
cooldown: 30
---

# Coding Agent Reflection

After a coding agent completes or fails a task, step back and extract
reusable insights from the work. The goal is to build up the coding
agent type's collective memory so that future tasks benefit from past
experience.

## Review the task record

Start by reading the task record for the triggering event. Pull the
task description, status, result summary, token usage, duration, and
the project it belongs to. If the task has an associated diff, read
that too.

Search agent-type memory for any existing insights related to this
task's domain (the languages, frameworks, or tools involved). Keep
these in mind as you analyze — you may confirm, refute, or extend
existing knowledge.

## Extract insights from completed tasks

If the task completed successfully:

Identify what strategies worked well. Look for patterns worth
remembering:
  - Build/test commands or flags that resolved tricky failures
  - Effective approaches to common coding problems (async patterns,
    database migration sequences, dependency resolution)
  - Project conventions the agent discovered or followed (naming,
    structure, testing patterns, import styles)
  - Tool usage patterns that saved time (linter configs, test
    selectors, git workflows)

Note the token usage and duration. If the task was unusually expensive
(high token count relative to the change size), note what caused the
bloat — it may indicate the task description was too vague, the agent
explored dead ends, or the codebase needed prerequisite work.

If the task was cheap and fast, note what made it efficient — a
well-scoped description, clear acceptance criteria, or prior memory
that guided the agent.

## Extract insights from failed tasks

If the task failed:

Identify the root cause category:
  - **Environment issues** — missing dependencies, wrong Python
    version, broken virtualenv, flaky CI
  - **Specification gaps** — ambiguous requirements, missing context,
    contradictory instructions
  - **Code complexity** — task too large, needed to be split, circular
    dependencies, untested legacy code
  - **Tool/API issues** — rate limits, API changes, broken integrations
  - **Agent limitations** — exceeded context window, went in circles,
    misunderstood the codebase

Check if this failure matches a pattern already in memory. If so,
the existing insight may need strengthening (mark as `#verified`) or
updating with the new failure's specifics.

If this is a new failure pattern, capture it with enough detail that
a future agent can avoid or quickly resolve the same issue. Include
the error signature, what was tried, and what would have helped.

## Write insights to memory

For each insight worth preserving, save it to the coding agent-type
memory using `memory_save`. Each insight should be:
  - **Specific and actionable** — not "tests are important" but
    "pytest with `--tb=short -q` runs 3x faster on this codebase and
    catches the same failures"
  - **Tagged appropriately** — include relevant tags: language/framework
    names, problem categories (`#async`, `#migration`, `#testing`,
    `#debugging`, `#performance`), confidence level (`#verified` if
    confirmed by multiple tasks, `#provisional` if first occurrence)
  - **Scoped correctly** — project-specific conventions go to project
    memory (via `memory_save` with project scope), cross-project
    coding wisdom goes to agent-type memory

Do not save trivial observations or one-off flukes. The bar for a
memory is: "would a future coding agent benefit from knowing this
before starting a similar task?"

## Consolidate existing memories

After writing new insights, review the agent-type memory for
consolidation opportunities. Use the consolidation tools to act on
what you find:

  - **Merge duplicates** — search agent-type memory for entries related
    to the insights you just saved. If you find overlapping memories,
    use `memory_save` (which auto-merges related content) to create a
    unified version, then `memory_delete` to remove the weaker or
    redundant entry by its `chunk_hash`.
  - **Update outdated insights** — if recent evidence confirms or
    contradicts an existing memory, use `memory_update` to change its
    content or tags directly. Bump `#provisional` to `#verified` if
    confirmed by this task. Mark as `#contested` if contradicted.
    Change the content to reflect current understanding.
  - **Promote cross-project patterns** — use `memory_search_by_tag` to
    check if a project-specific insight also appears in other projects'
    memories. If the same pattern has been discovered independently in
    multiple projects, use `memory_promote` to copy it from project
    scope to agent-type scope (set `target_scope` to
    `agenttype_coding` and `delete_source` to true).
  - **Archive stale memories** — if an insight hasn't been retrieved
    recently and the current task provides no supporting evidence,
    use `memory_delete` to remove clearly outdated knowledge. Don't
    delete aggressively — only remove entries that are demonstrably
    wrong or superseded.

Keep consolidation lightweight. Only touch memories that are directly
related to the current task's domain. Do not attempt a full audit of
all agent-type memory in every reflection run.

## Skip conditions

If the task was trivial (very low token count, simple file edit, no
meaningful patterns to extract), skip the reflection entirely. Not
every task produces insights worth capturing.

If a very similar reflection ran recently for the same project and
domain (check memory for recent reflection entries), keep this run
brief — only capture genuinely new information.
