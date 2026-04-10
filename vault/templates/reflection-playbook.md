---
id: {{AGENT_TYPE}}-reflection
triggers:
  - task.completed
  - task.failed
scope: agent-type:{{AGENT_TYPE}}
cooldown: 30
---

# {{AGENT_TYPE_TITLE}} Agent Reflection

<!--
  TEMPLATE: Generic reflection playbook for any agent type.

  To create a new reflection playbook for an agent type:
  1. Copy this file to vault/agent-types/<type>/playbooks/reflection.md
  2. Replace {{AGENT_TYPE}} with the agent type slug (e.g., code-review, qa, devops)
  3. Replace {{AGENT_TYPE_TITLE}} with the display name (e.g., Code Review, QA, DevOps)
  4. Customize the "Extract insights" sections with domain-specific patterns
     (see the inline guidance comments for each section)
  5. Adjust skip condition thresholds if needed

  Reference implementation: vault/agent-types/coding/playbooks/reflection.md
-->

After a {{AGENT_TYPE}} agent completes or fails a task, step back and extract
reusable insights from the work. The goal is to build up the {{AGENT_TYPE}}
agent type's collective memory so that future tasks benefit from past
experience.

## Review the task record

Start by reading the task record for the triggering event. Pull the
task description, status, result summary, token usage, duration, and
the project it belongs to. If the task produced output artifacts
(diffs, reports, logs, reviews), read those too.

Search agent-type memory for any existing insights related to this
task's domain. Keep these in mind as you analyze -- you may confirm,
refute, or extend existing knowledge.

## Extract insights from completed tasks

If the task completed successfully:

Identify what strategies worked well. Look for patterns worth
remembering:

<!--
  CUSTOMIZE: Replace or extend these bullet points with domain-specific
  success patterns for your agent type. Examples by type:

  code-review:
    - Review strategies that caught real bugs vs. generated noise
    - Effective comment phrasing that led to author action
    - Patterns for reviewing large diffs efficiently (file ordering,
      focus areas, when to request splitting)
    - Project-specific style and convention knowledge

  qa:
    - Test strategies that found real bugs vs. false positives
    - Effective test data generation approaches
    - Environment setup patterns that prevented flaky tests
    - Coverage strategies that maximized defect detection per test

  devops:
    - Deployment strategies that minimized downtime or risk
    - Infrastructure configuration patterns that proved reliable
    - Monitoring and alerting setups that caught issues early
    - Rollback procedures that worked smoothly

  documentation:
    - Documentation structures that users found navigable
    - Effective approaches to explaining complex systems
    - Patterns for keeping docs in sync with code changes
    - Cross-referencing strategies that reduced duplication

  research:
    - Search strategies that found relevant information efficiently
    - Source evaluation patterns that identified reliable references
    - Synthesis approaches that produced actionable recommendations
    - Effective ways to scope and bound open-ended investigations
-->

  - Approaches or strategies that produced high-quality results
  - Domain-specific techniques that resolved tricky problems
  - Project conventions the agent discovered or followed
  - Tool usage patterns that saved time or improved output quality
  - Effective workflows or sequences of operations

Note the token usage and duration. If the task was unusually expensive
(high token count relative to the output size or complexity), note what
caused the bloat -- it may indicate the task description was too vague,
the agent explored dead ends, or prerequisite information was missing.

If the task was cheap and fast, note what made it efficient -- a
well-scoped description, clear acceptance criteria, or prior memory
that guided the agent.

## Extract insights from failed tasks

If the task failed:

Identify the root cause category:

<!--
  CUSTOMIZE: Adjust these root cause categories to match your agent
  type's common failure modes. The categories below are a general
  starting point. Add domain-specific categories and remove any that
  don't apply.
-->

  - **Environment issues** -- missing dependencies, access problems,
    unavailable services, configuration errors
  - **Specification gaps** -- ambiguous requirements, missing context,
    contradictory instructions, unclear acceptance criteria
  - **Scope issues** -- task too large, needed to be split, conflicting
    objectives, dependencies not met
  - **Tool/API issues** -- rate limits, API changes, broken integrations,
    unavailable external services
  - **Agent limitations** -- exceeded context window, went in circles,
    misunderstood the problem domain, insufficient domain knowledge

Check if this failure matches a pattern already in memory. If so,
the existing insight may need strengthening (mark as `#verified`) or
updating with the new failure's specifics.

If this is a new failure pattern, capture it with enough detail that
a future agent can avoid or quickly resolve the same issue. Include
the error signature, what was tried, and what would have helped.

## Write insights to memory

For each insight worth preserving, save it to the {{AGENT_TYPE}}
agent-type memory using `memory_save`. Each insight should be:

  - **Specific and actionable** -- not vague platitudes but concrete
    techniques, commands, configurations, or strategies that a future
    agent can directly apply
  - **Tagged appropriately** -- include relevant tags: domain keywords,
    problem categories, confidence level (`#verified` if confirmed by
    multiple tasks, `#provisional` if first occurrence)
  - **Scoped correctly** -- project-specific knowledge goes to project
    memory (via `memory_save` with project scope), cross-project
    {{AGENT_TYPE}} wisdom goes to agent-type memory

Do not save trivial observations or one-off flukes. The bar for a
memory is: "would a future {{AGENT_TYPE}} agent benefit from knowing
this before starting a similar task?"

## Consolidate existing memories

After writing new insights, review the agent-type memory for
consolidation opportunities. Use the consolidation tools to act on
what you find:

  - **Merge duplicates** -- search agent-type memory for entries related
    to the insights you just saved. If you find overlapping memories,
    use `memory_save` (which auto-merges related content) to create a
    unified version, then `memory_delete` to remove the weaker or
    redundant entry by its `chunk_hash`.
  - **Update outdated insights** -- if recent evidence confirms or
    contradicts an existing memory, use `memory_update` to change its
    content or tags directly. Bump `#provisional` to `#verified` if
    confirmed by this task. Mark as `#contested` if contradicted.
    Change the content to reflect current understanding.
  - **Promote cross-project patterns** -- use `memory_search_by_tag` to
    check if a project-specific insight also appears in other projects'
    memories. If the same pattern has been discovered independently in
    multiple projects, use `memory_promote` to copy it from project
    scope to agent-type scope (set `target_scope` to
    `agenttype_{{AGENT_TYPE}}` and `delete_source` to true).
  - **Archive stale memories** -- if an insight hasn't been retrieved
    recently and the current task provides no supporting evidence,
    use `memory_delete` to remove clearly outdated knowledge. Don't
    delete aggressively -- only remove entries that are demonstrably
    wrong or superseded.

Keep consolidation lightweight. Only touch memories that are directly
related to the current task's domain. Do not attempt a full audit of
all agent-type memory in every reflection run.

## Skip conditions

If the task was trivial (very low token count, minimal output, no
meaningful patterns to extract), skip the reflection entirely. Not
every task produces insights worth capturing.

If a very similar reflection ran recently for the same project and
domain (check memory for recent reflection entries), keep this run
brief -- only capture genuinely new information.
