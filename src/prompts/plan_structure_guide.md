---
name: plan-structure-guide
description: Instructions for agents producing well-structured implementation plans
category: task
variables:
  - name: max_steps
    description: Maximum number of implementation phases allowed
    required: true
tags: [planning, agent, structure]
version: 1
---

## Plan Structure Requirements

When producing an implementation plan, follow these formatting rules
to ensure the plan can be automatically parsed into subtasks.

IMPORTANT: Each phase becomes a separate subtask executed by an agent.
Phases should be COARSE — each one should represent a substantial chunk
of work containing MANY concrete steps. Do NOT create a separate
phase for every small action (e.g. "create file X", "add import Y").
Instead, group related work into broad phases. Fewer, larger
phases are ALWAYS preferred.

### DO:
- Use action-verb headings: "## Implement X", "## Create Y", "## Add Z"
- Use numbered phases: "## Phase 1: Database Layer and Migrations"
- Group related work AGGRESSIVELY: a phase like "Build API Endpoints"
  should include creating routes, adding validation, writing handlers,
  AND writing tests for those endpoints — all in one phase
- Include a numbered outline of ALL concrete steps within each phase
  (so the agent can see the full scope of work at a glance)
- Include detailed descriptions for each step after the outline
- Put implementation phases under a "## Implementation Plan" container heading

### DON'T:
- Don't include overview/summary sections as separate headings
- Don't include design discussion, architecture review, or rationale sections
- Don't include sections labeled "Future Work", "Out of Scope", or "Background"
- Don't write a design document when asked to implement something

### Plan File Location

**Always write your plan to `.claude/plan.md`** — this is the ONLY location the
system checks. Do NOT write `plan.md` at the project root or any other location.

### Plan File Lifecycle

When your task completes, `.claude/plan.md` will be **automatically parsed and
converted into follow-up subtasks**.

- **If you only wrote the plan** (did NOT implement it): leave the plan file in place
  so the system creates subtasks to execute each phase.
- **If you implemented the plan yourself** (both planned AND executed the work in a
  single task): **DELETE the plan file** before completing, or add `auto_tasks: false`
  to the plan's YAML frontmatter. Leaving a completed plan file behind creates
  duplicate/unnecessary follow-up tasks.

### Ideal Plan Structure:

```markdown
# Implementation Plan: [Feature Name]

## Implementation Plan

### Phase 1: [Action Verb] [Broad Area of Work]

[High-level description of this phase]

Steps in this phase:
1. [Step within this phase]
2. [Another step within this phase]
3. [Yet another step]
4. [More steps as needed]

[Detailed description for step 1]

[Detailed description for step 2]

...

Files to modify/create:
- `path/to/file1.py`
- `path/to/file2.py`

**Estimated effort:** ~N hours

### Phase 2: [Action Verb] [Broad Area of Work]

[Description covering multiple related changes]

Steps in this phase:
1. [Step]
2. [Step]
3. [Step]
4. [Step]

[Detailed descriptions for each step]

**Estimated effort:** ~N hours
```
