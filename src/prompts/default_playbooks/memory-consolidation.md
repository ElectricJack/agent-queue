---
id: memory-consolidation
triggers:
  - timer.24h
scope: system
---

# Memory Consolidation

Nightly pass: for each active project, decide whether its
`memory/insights/` vault directory has changed enough to warrant a
consolidation task. If so, create one supervisor task per project. Do
not perform the consolidation itself in this playbook — the supervisor
task does the judgement work with full tool access.

## Step 1 — Enumerate active projects

List projects with at least one insight written in the last 30 days
(query the memory service or `list_projects`). Skip archived/paused
projects.

## Step 2 — Read each project's consolidation marker

The marker is **per-project**, not shared. For each active project read
`~/.agent-queue/vault/projects/{project_id}/memory/consolidation.md`.
Its frontmatter carries this project's `last_consolidated` ISO
timestamp and last-run stats. If the file does not exist, treat the
project as never-consolidated.

## Step 3 — Churn check

For each active project, count the number of insights whose `updated`
timestamp is newer than this project's `last_consolidated`. Use
`memory_search` with `top_k: 500` and filter results by `updated_at`
against the marker's epoch, or fall back to listing the
`memory/insights/` directory and counting files with a newer mtime.

Skip projects where:
- fewer than **10** insights have changed since the last run, AND
- the last run was less than **7 days** ago.

For never-consolidated projects with 5 or more existing insights, always
run — the first pass is the highest-value one.

## Step 4 — Create one consolidation task per qualifying project

For each qualifying project, read
`src/prompts/consolidation_task.md` from the repository, substitute the
placeholders `{project_id}`, `{project_name}`, `{insights_dir}`,
`{knowledge_dir}`, `{last_consolidated}`, `{churn_count}`, then call
`create_task` with:

- `agent_type`: `supervisor`
- `priority`: `40`
- `title`: `Consolidate memory: <project_name>`
- `description`: the formatted template from above
- `project_id`: the project id
- `task_type`: `maintenance` if available, else leave unset

Do not pre-delete anything — the task is responsible for all writes.

## Step 5 — No-op when nothing qualifies

If no projects qualify on this tick, record a single log line and end
cleanly. Do not create empty summary notes or otherwise spam memory.
