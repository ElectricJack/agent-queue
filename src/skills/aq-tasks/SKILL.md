---
name: aq-tasks
description: Task lifecycle in the aq daemon — get / list / show / close / reopen / edit tasks, work with dependencies, results, and archives. Use whenever you need to inspect the queue, understand your assigned task, close work with a summary, or manage a task graph. Also covers `aq task explain` for "why isn't X running".
allowed-tools:
  - Bash
---

# aq tasks — Working with tasks from the CLI

Every task operation runs through `aq task <subcommand>`. Run
`aq task --help` for the current subcommand list; `aq task <cmd> --help`
for arguments.

## Read paths (safe, no side effects)

```bash
aq task list                        # active tasks, current project scope
aq task list --status IN_PROGRESS   # filter by status
aq task list --json --brief         # scriptable projection
aq task show <task_id>              # full detail for one task
aq task get <task_id>               # single-row summary
aq task explain <task_id>           # "why isn't this running?" — blockers,
                                    # gates, budget, cooldown, lease info.
                                    # Quote the actual reason it returns,
                                    # don't theorize.
aq task deps <task_id>              # dependency graph for one task
aq task tree <task_id>              # subtask tree, expanded
aq task result <task_id>            # result payload from a completed task
aq task children <id> [--recursive] [--status S] [--limit N] [--offset N]
                                    # direct or recursive children of a container
aq task progress <id>               # computed group progress: counts, waves,
                                    # max parallelism — never stored, always
                                    # derived from the current graph
```

## The close-a-task loop

Every assigned task ends with `aq task close`. Two common shapes:

```bash
# Success — commit + push happened, PR opened, summary explains what changed.
aq task close --task-id <id> \
  --outcome success \
  --summary "Refactored X to use Y. Tests: 42/42. Commits: abc123, def456."

# Blocked — you couldn't complete because of missing context.
aq task close --task-id <id> \
  --outcome needs_context \
  --summary "Task references spec §7 but the spec file is not in the vault."

# Failed — you tried, it broke, and the failure needs human eyes.
aq task close --task-id <id> \
  --outcome failure \
  --summary "Test suite deadlocks under xdist. Reproducer in tests/xxx."
```

Rules the daemon enforces:
- If the profile has `needs_workspace: true` (all worker profiles), the
  summary is required — the daemon rejects empty summaries with
  `summary is required`.
- Every terminal close also captures the git commit HEAD of the
  workspace automatically. Don't try to pass a `--commit-sha` flag.

## Reopen + provide input (rejection loop)

If a reviewer or human rejects the work, they call `reopen_with_feedback`
on your task. That transitions it back to `READY` with feedback attached.
When you pick it up, `aq task get <id>` shows the feedback in the
description.

If a task is `WAITING_INPUT`, respond with:

```bash
aq task input-response --task-id <id> --response "<the answer>"
```

## Creating tasks (elevated / supervisor only)

Non-elevated worker sessions cannot create tasks — the daemon returns
`out of scope: create_task`. If you're the supervisor, or you're running
a task whose profile is elevated:

```bash
# Ad-hoc task creation
aq task create --project <pid> --title "..." --description "..." \
  --profile worker-standard --priority 50

# From a spec (preferred for multi-task graphs)
aq task create --from-spec vault/projects/<pid>/specs/<slug>.md
aq task create --from-spec <path> --dry-run   # validate first, always

# Create under an existing container (single task or a --from-spec graph)
aq task create --project <pid> --title "..." --description "..." \
  --profile worker-standard --parent <container_task_id>
```

## Hierarchy

Any task can become a **container** just by gaining a child — there is no
separate group entity, and progress is always computed live from the graph,
never stored. Ids are hierarchical and **immutable**: a child created under
`swift-falcon` gets `swift-falcon.1`, its own child gets `swift-falcon.1.1`,
and so on — an id never changes once assigned, and structural depth is
capped at 3 (root = 1).

```bash
aq task reparent <task_id> --parent <new_parent_id>   # move under another container
aq task reparent <task_id> --root                     # detach to root (clears parent)
aq task delete <task_id> --cascade                    # delete a container + its whole subtree
aq task close --task-id <id> --abandon-children \
  --outcome ... --summary "..."                       # close a container, abandoning
                                                        # any still-open descendants
```

Rules the daemon enforces: a container with open children refuses a plain
`close`/`delete` — and so does *any* transition to COMPLETED (merge, approval,
session close), unless it is an administrative forced close;
`--abandon-children` and `--cascade` are refused while any descendant has a
live session; adding a child under a COMPLETED container is refused.

A successful `reparent` emits **`task.reparented`** on the bus, carrying
`task_id`, `project_id`, `title`, `old_parent` and `new_parent` (either parent
is `null` at the root). Playbooks can trigger on it.

Failures come back as `hierarchy.<code>`. The full list (also in `aq schema`'s
`hierarchy_error` enum):

| Code | Meaning |
|---|---|
| `not_found` | the task or the requested parent does not exist |
| `self_parent` | a task cannot be its own parent |
| `cross_project` | parent and child live in different projects |
| `container_closed` | the parent is COMPLETED and cannot take children |
| `cycle` | the move would close a loop over blocking edges |
| `depth` | structural or naming depth would exceed 3 |
| `open_children` | close/complete refused: a direct child is non-terminal |
| `open_descendants` | archive refused: a *deeper* descendant is non-terminal |
| `has_children` | delete refused: the task is a container (use `--cascade`) |
| `live_descendants` | abandon/cascade refused: a descendant has a live session |
| `cycle_check_skipped` | internal: the bulk graph-creation path was handed a task that is not a fresh leaf |

## Dependencies

```bash
aq task dep add <task_id> --depends-on <upstream_id> --type blocks
aq task dep remove <task_id> --depends-on <upstream_id>
```

Dependency types: `blocks`, `parent-child`, `waits-for`,
`conditional-blocks`, `discovered-from`, `related`, `duplicates`,
`supersedes`. Only the first four gate readiness.

## Archives

Completed / failed tasks eventually archive. Query and restore:

```bash
aq task list-archived --project <pid>
aq task restore <task_id>
```

## Rules of thumb

- Read before writing. `aq task get` / `aq task show` before any mutation.
- Explain non-obvious moves. When you close a task with `success`, the
  summary is your one chance to tell the reviewer what you did and why.
- Don't create tasks from a worker session. That's the supervisor's
  job — message the supervisor if you notice missing work.
