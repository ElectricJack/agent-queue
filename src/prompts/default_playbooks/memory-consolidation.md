---
id: memory-consolidation
triggers:
  - timer.24h
scope: system
llm_config:
  provider: gemini
  model: gemini-2.5-pro
transition_llm_config:
  provider: gemini
  model: gemini-2.5-flash
---

# Memory Consolidation

Decide which projects need a consolidation pass, then create one
supervisor task per target project. **Do not** perform the
consolidation itself — the supervisor task does that with full tool
access.

Each step must end with a **single JSON object** in the response so the
runner can extract structured output for downstream `for_each` loops.
Prose reasoning before the JSON is fine, but the final line must be
pure JSON (no code fence).

## Step 1 — Pick the target projects

Branch on the incoming event:

1. **Project-scoped manual run** — `event.project_id` is set.
   The user invoked this from a specific project's channel. Emit
   exactly that one project. Skip every other rule. This path never
   applies churn thresholds — if the user asked, they want a task.

2. **System-wide manual run** — `event.type == "manual"` and no
   `project_id`. The user invoked from the system channel and wants a
   pass for every active project. Call `list_projects`, keep only
   those with `status == "ACTIVE"`, and emit all of them. Again, no
   churn thresholds — manual intent wins.

3. **Timer run** — `event.type == "timer.24h"` (or any
   non-manual type). Call `list_projects`, filter to `ACTIVE`, then
   for each one:
   - Call `read_project_memory_file` with `project_id` and
     `path: "consolidation.md"`. The tool returns the file contents
     when present, or `{"missing": true, ...}` when it doesn't
     exist. Treat a missing file as `last_consolidated: null`.
     Otherwise parse the `last_consolidated` value from the YAML
     frontmatter at the top of `content`.
   - Call `count_project_memory_files` with `project_id`, `path:
     "insights"`, and (if known) `newer_than: <last_consolidated>`
     to get the churn count. When `last_consolidated` is `null`,
     omit `newer_than` to count all insights. A missing directory
     returns `{"count": 0, "missing": true}`.
   - Use these tools — do **not** attempt to access the vault via
     `read_file` or `list_directory`. Those are sandboxed to the
     workspace and cannot see the system vault at
     `~/.agent-queue/vault/`.

   Keep projects where **either**:
   - `churn_count >= 5` and `last_consolidated` is null or older than
     3 days, or
   - `last_consolidated` is null and the project has at least 3
     insights total (bootstrap the first pass).

End the step with:

```json
{"targets": [
  {
    "id": "<project_id>",
    "name": "<project_name>",
    "last_consolidated": "<iso-or-null>",
    "churn_count": <int-or-null>
  },
  ...
]}
```

`last_consolidated` and `churn_count` may be `null` on the manual
paths — the supervisor task can recompute them. If `targets` is empty
(timer run with no qualifying projects), the playbook ends cleanly at
the next node.

## Step 2 — Create one consolidation task per target

For **each** entry in `targets`, call `create_task` **once** with:

- `project_id`: the project's id
- `priority`: `40`
- `title`: `Consolidate memory: <project_name>`

Do **not** pass `agent_type`. The scheduler's `agent_type` field is a
hard filter for workspace-bound agent instances (e.g. `"claude"`,
`"codex"`) — setting it to `"supervisor"` leaves the task forever
READY because no workspace agent advertises that type. The project's
default profile already gives the executing agent the `memory_*`
tools it needs via the agent-queue MCP.
- `description`: read
  `/mnt/d/Dev/agent-queue2/src/prompts/consolidation_task.md` via the
  `read_file` tool and substitute the placeholders (the full prompt
  instructs the executing agent to edit the vault markdown files
  directly with Read/Edit/Write/Bash — no extra tools required beyond
  the default Claude toolset)
  `{project_id}`, `{project_name}`,
  `{insights_dir}` (→ `~/.agent-queue/vault/projects/<project_id>/memory/insights`),
  `{knowledge_dir}` (→ `~/.agent-queue/vault/projects/<project_id>/memory/knowledge`),
  `{last_consolidated}` (→ value from Step 1, or `never` if null),
  `{churn_count}` (→ value from Step 1, or `unknown` if null).

Do **not** pre-delete anything — the consolidation task owns all vault
writes.

End the step with:

```json
{"tasks_created": [{"project_id": "<id>", "task_id": "<task-id>"}, ...]}
```

## Step 3 — No-op terminal

Log a single line summarising how many tasks were created. End the
playbook.
