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

Every assigned task ends with `aq task close --outcome pass|fail` — those are
the only two outcomes (`aq schema`'s `outcome` enum). If you're missing
context to finish, say so in `--summary` and close `--outcome fail`. Two
common shapes:

```bash
# Passed — commit + push happened, PR opened, summary explains what changed.
aq task close <id> \
  --outcome pass \
  --summary "Refactored X to use Y. Tests: 42/42. Commits: abc123, def456."

# Failed — you tried, it broke (or you're missing context), and it needs human eyes.
aq task close <id> \
  --outcome fail \
  --summary "Test suite deadlocks under xdist. Reproducer in tests/xxx."
```

Rules the daemon enforces:
- If the profile has `needs_workspace: true` (all worker profiles), the
  summary is required — the daemon rejects empty summaries with
  `summary is required`.
- Every terminal close also captures the git commit HEAD of the
  workspace automatically. Don't try to pass a `--commit-sha` flag.

## The pool worker loop (swarm-work-model §10)

A `lifecycle: pool` session never gets a task pushed to it — it pulls work
in a loop instead:

```bash
aq task claim --next --wait 60        # block up to 60s for the next ready task
aq prime                              # load the claimed task's context
# ... do the work ...
aq task close --outcome pass|fail --summary "..." --claim-next --wait 60
```

`aq task claim` writes `<work_dir>/.aq/claim.json` with
`{"task_id", "claim_epoch", "session_id", "claimed_at"}` and returns a
`result` code (`aq schema`'s `claim_result` enum):

| Result | Meaning |
|---|---|
| `claimed` | got a task; `.aq/claim.json` now holds it |
| `no_ready_work` | nothing matched (after `wait`, if given) |
| `claim_conflict` | another session claimed it first — retry |
| `prepare_failed` | claimed but workspace/setup prep failed |
| `claim_in_progress` | a claim is already being prepared for this session |
| `not_admissible` | the task isn't currently claimable (paused project, etc.) |
| `session_exhausted` | this session hit its per-session claim cap |
| `drain_requested` | the session is draining — stop claiming |
| `stale_claim` | a mutator's `claim_epoch` no longer matches the task's current one |
| `out_of_scope` | the caller isn't allowed to claim (not a pool session) |

`task_close --claim-next` chains straight into the next claim after closing,
so the loop above is really one command per iteration. `aq task heartbeat`,
`aq task set`, and `aq handoff` all accept `--claim-epoch` too — every one of
them reads it from `.aq/claim.json` (falling back to `$AQ_CLAIM_EPOCH`)
automatically, so you don't normally need to pass it by hand. A pool session
whose `claim_epoch` no longer matches (task reassigned, claim expired) gets
`stale_claim` back from any of these — read the correct one from
`.aq/claim.json` and stop, rather than retrying blindly.

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
aq task close <id> --abandon-children \
  --outcome pass|fail --summary "..."                 # close a container, abandoning
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
- Explain non-obvious moves. When you close a task with `--outcome pass`, the
  summary is your one chance to tell the reviewer what you did and why.
- Don't create tasks from a worker session. That's the supervisor's
  job — message the supervisor if you notice missing work.
