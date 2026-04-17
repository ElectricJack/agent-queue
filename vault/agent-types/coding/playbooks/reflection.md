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

For each insight worth preserving, save it to memory using
`memory_store`. Each insight should be:
  - **Specific and actionable** — not "tests are important" but
    "pytest with `--tb=short -q` runs 3x faster on this codebase and
    catches the same failures"
  - **Tagged appropriately** — include relevant tags: language/framework
    names, problem categories (`#async`, `#migration`, `#testing`,
    `#debugging`, `#performance`), confidence level (`#verified` if
    confirmed by multiple tasks, `#provisional` if first occurrence)
  - **Scoped correctly** — project-specific conventions go to project
    memory (default scope), cross-project coding wisdom goes to
    agent-type memory (set `scope` to `agenttype_coding`)

Do not save trivial observations or one-off flukes. The bar for a
memory is: "would a future coding agent benefit from knowing this
before starting a similar task?"

## Consolidate existing memories

Before storing new insights, use `memory_recall` to check for related
existing memories. This helps avoid duplicates and gives you context
for writing better insights.

  - **Merge duplicates** — `memory_store` automatically merges related
    content (similarity 0.8-0.95) and deduplicates near-identical
    content (> 0.95). If you find an existing memory that is clearly
    wrong or superseded, use `memory_delete` with its `chunk_hash`.
  - **Correct outdated insights** — if this task contradicts an existing
    memory, store the corrected version via `memory_store` (which will
    auto-merge), then `memory_delete` the old entry if it's clearly
    wrong.

Keep consolidation lightweight. Only touch memories directly related
to the current task's domain.

## Skip conditions

If the task was trivial (very low token count, simple file edit, no
meaningful patterns to extract), skip the reflection entirely. Not
every task produces insights worth capturing.

If a very similar reflection ran recently for the same project and
domain (check memory for recent reflection entries), keep this run
brief — only capture genuinely new information.
