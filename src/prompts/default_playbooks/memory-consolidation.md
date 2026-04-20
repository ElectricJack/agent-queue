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

Each step must end with a **single JSON object** in the response so the
runner can extract structured output for downstream `for_each` loops.
Prose reasoning before the JSON is fine, but the final line must be
pure JSON (no code fence).

## Step 1 — Enumerate active projects

Call `list_projects`. Filter to projects whose status is `ACTIVE` (skip
`ARCHIVED`, `PAUSED`). For each remaining project, call
`memory_search` with the project id to confirm it has at least one
insight — skip projects with zero insights.

End the step with a JSON object of shape:

```json
{"projects": [{"id": "<project_id>", "name": "<project_name>"}, ...]}
```

If no projects qualify, emit `{"projects": []}` and the playbook will
end cleanly.

## Step 2 — Read each project's consolidation marker

For each project in `projects`, read
`~/.agent-queue/vault/projects/<project_id>/memory/consolidation.md`.
Parse the frontmatter. If the file is missing, treat
`last_consolidated` as `null`.

End this step with a JSON object of shape:

```json
{"projects_with_consolidation_data": [
  {
    "id": "<project_id>",
    "name": "<project_name>",
    "last_consolidated": "<iso-timestamp-or-null>"
  },
  ...
]}
```

## Step 3 — Churn check

For each entry in `projects_with_consolidation_data`, count the
insights whose `updated` timestamp is newer than `last_consolidated`.
Use `memory_search` with `top_k: 500` scoped to the project, or fall
back to listing `vault/projects/<project_id>/memory/insights/` and
counting files with a newer mtime.

Mark a project as qualifying when **either** of these is true:

- `churn_count >= 10` AND `last_consolidated` is older than 7 days (or
  null).
- `last_consolidated` is `null` and the project has at least 5
  insights total — the first pass is the most valuable one.

End this step with:

```json
{"qualifying_projects": [
  {
    "id": "<project_id>",
    "name": "<project_name>",
    "last_consolidated": "<iso-or-null>",
    "churn_count": <int>
  },
  ...
]}
```

If `qualifying_projects` is empty, the playbook ends cleanly at the
next node.

## Step 4 — Create one consolidation task per qualifying project

For **each** entry in `qualifying_projects`, call `create_task` **once**
with:

- `project_id`: the project's id
- `agent_type`: `"supervisor"`
- `priority`: `40`
- `title`: `Consolidate memory: <project_name>`
- `description`: read
  `/mnt/d/Dev/agent-queue2/src/prompts/consolidation_task.md` via the
  `read_file` tool and substitute the placeholders
  `{project_id}`, `{project_name}`,
  `{insights_dir}` (→ `~/.agent-queue/vault/projects/<project_id>/memory/insights`),
  `{knowledge_dir}` (→ `~/.agent-queue/vault/projects/<project_id>/memory/knowledge`),
  `{last_consolidated}` (→ the value from Step 3 or `never`),
  `{churn_count}` (→ the value from Step 3).

Do **not** pre-delete anything — the consolidation task is responsible
for all vault writes.

End this step with:

```json
{"tasks_created": [{"project_id": "<id>", "task_id": "<task-id>"}, ...]}
```

## Step 5 — No-op terminal

Log a single line summarising how many tasks were created. End the
playbook.
