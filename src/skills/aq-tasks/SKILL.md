---
name: aq-tasks
description: Task lifecycle in the aq daemon — get / list / show / close / reopen / edit tasks, work with dependencies, results, and archives. Use whenever you need to inspect the queue, understand your assigned task, close work with a summary, or manage a task graph. Also covers the reads that answer "why isn't X running".
allowed-tools:
  - Bash
---

# aq tasks — Working with tasks from the CLI

Every task operation runs through `aq task <subcommand>`. Run
`aq task --help` for the current subcommand list; `aq task <cmd> --help`
for arguments.

## Read paths (safe, no side effects)

Available to any caller, including a non-elevated worker or reviewer session:

```bash
aq task show <task_id>              # full detail for one task
aq task comments <task_id>          # durable comment history, newest first
aq task children --task-id <id> [--recursive] [--status S] [--limit N] [--offset N]
                                    # direct or recursive children of a container
aq task progress --task-id <id>     # computed group progress: counts, waves,
                                    # max parallelism — never stored, always
                                    # derived from the current graph
```

Operator reads — a local CLI caller or an elevated (supervisor / triage)
session. A plain worker or reviewer token gets `out of scope: <command>`,
so do not build a workflow on them:

```bash
aq task list                        # active tasks, current project scope
aq task list --status IN_PROGRESS   # filter by status
aq --json --brief task list         # scriptable projection.  --json and
                                    # --brief are group-level flags: they go
                                    # before the subcommand, not after it.
aq task get --task-id <task_id>     # single-row summary
aq task deps --task-id <task_id>    # dependency graph for one task
aq task get-tree --task-id <task_id>    # subtask tree, expanded
aq task get-result --task-id <task_id>  # result payload from a completed task
```

`aq task explain` — the daemon's "why isn't this running?" answer — has no
usable CLI form right now: the generated command exposes no `--task-id`, so
it cannot name a task (tracked by `bright-forge-33`). Read the blockers off
`aq task show <id>` and `aq task deps --task-id <id>` instead, and quote what
they say rather than theorizing.

A worker or reviewer session reaches exactly one task — its own — plus,
for a reviewer, the single task named by its review's `discovered-from`
edge. Anything else is `out of scope: task_id mismatch`.

## Findings and task comments

Keep confirmed findings in the task description so the next worker starts with the
current understanding. Preserve the original goal, requirements, and acceptance criteria.
Read the description with `aq task show` before updating the complete text. Use the
expected value to avoid overwriting concurrent edits:

```bash
aq task set <id> --description "<original requirements plus confirmed findings>" \
  --expected-description "<description just read>"
aq task comment <id> --body "Finding: ... Evidence: ... Decision/next step: ..."
aq task comments <id> --limit 50 --offset 0
```

On a description conflict, re-read and merge; never retry blindly without the expected
value. Comments append attributed, timestamped history without rewriting the description.
Use them for meaningful progress, evidence, test outcomes, decisions, and blockers. Keep
hypotheses clearly marked and omit secrets. There is no need to invent findings or post
noise after every command. `--note` remains a legacy task-context field, not the comment log.

Save useful findings as you discover them and **before close, handoff, or waiting for input**.
Do not leave discoveries only in terminal output or the final close summary. New sessions
receive a bounded recent-comment excerpt in prime; read full history if needed. A comment
is not approval, an escalation, or a user notification: still use the question/message
workflow when input is needed. `task comment` resolves `--claim-epoch` like `task set`.

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

`aq task close`'s TASK_ID argument is optional: omit it (as the loop above
does) and the daemon closes whichever task this session currently holds —
which is the only sane form for a pool worker, whose task changes with every
claim. The same goes for `aq task heartbeat`.

`task_close --claim-next` attempts another claim after closing. By default,
AQ retires the conversation after one task and returns `drain_requested` or
`session_exhausted`; stop and let AQ start fresh context for the next task.
Do not send `/clear` yourself. Operators can explicitly disable this policy
with `swarm.fresh_context_per_task: false` to retain the multi-task loop.
`aq task heartbeat`,
`aq task set`, and `aq handoff` all accept `--claim-epoch` too — every one of
them reads it from `.aq/claim.json` (falling back to `$AQ_CLAIM_EPOCH`)
automatically, so you don't normally need to pass it by hand. A pool session
whose `claim_epoch` no longer matches (task reassigned, claim expired) gets
`stale_claim` back from any of these — read the correct one from
`.aq/claim.json` and stop, rather than retrying blindly.

## Reopen + provide input (rejection loop)

If a reviewer or human rejects the work, they call `reopen_with_feedback`
on your task. That transitions it back to `READY` with feedback attached.
When you pick it up, `aq task show <id>` shows the feedback in the
description.

`WAITING_INPUT` waits on a *human*, not on you — there is no worker-side
command for it. The two answering surfaces are both operator ones:

```bash
aq system provide-input --task-id <id> --input "<the answer>"
aq question answer <question_id> --body "<the answer>"
```

## Creating tasks

`create_task` is on the agent surface: a worker or reviewer session may
file emergent work it discovers. A worker-filed task starts DEFINED with a
`discovered-from` edge back to the filing task; a root filing also opens a
routing gate, so triage — not the filer — dedupes and routes it. `--from-spec`,
`--graph` and `create_task_graph` stay elevated/supervisor-only.

```bash
# Ad-hoc task creation.  --reason is required on a worker-filed task and is
# stored on the discovered-from edge back to the task you hold.
aq task create --project <pid> --title "..." --description "..." \
  --profile worker-standard-medium-claude --priority 50 --reason "why this exists"

# From a spec (preferred for multi-task graphs)
aq task create --from-spec vault/projects/<pid>/specs/<slug>.md
aq task create --from-spec <path> --dry-run   # validate first, always

# Create under an existing container (single task or a --from-spec graph)
aq task create --project <pid> --title "..." --description "..." \
  --profile worker-standard-medium-claude --parent <container_task_id>

# Explicitly at project level — the opt-out for cross-cutting work filed from
# inside an epic's child task.  Mutually exclusive with --parent.
aq task create --project <pid> --title "..." --description "..." \
  --root --reason "why this exists"
```

Always pass at least `--project` and `--title` from a worker session: with
neither, `aq task create` drops into its interactive wizard, whose first step
calls `list_projects` — a command a worker token refuses with
`out of scope: list_projects`.

## Cooking a formula

A formula is a reusable, parameterised task-graph template in the vault
(`vault/[projects/<pid>/]formulas/<name>.md` — see
`docs/specs/design/formulas.md`). `formula_list` and `formula_show` are
read-only and available to any session; `formula_cook` **creates** the
graph and is **not** agent-scoped — it is elevated/supervisor-only, same
restriction as `create_task_graph`:

```bash
aq formula list --project-id <pid>                # what's available
aq formula show review-and-fix --var branch=feat/x # resolve + validate, no write
aq formula cook review-and-fix -p <pid> \
  --var branch=feat/x --var fixer=coding --dry-run  # then drop --dry-run
```

## Hierarchy

Any task can become a **container** just by gaining a child — there is no
separate group entity, and progress is always computed live from the graph,
never stored. Ids are hierarchical and **immutable**: a child created under
`swift-falcon` gets `swift-falcon.1`, its own child gets `swift-falcon.1.1`,
and so on — an id never changes once assigned, and structural depth is
capped at 3 (root = 1).

```bash
aq task reparent --task-id <task_id> --parent-id <new_parent_id>  # move under another container
aq task reparent --task-id <task_id> --root           # detach to root (clears parent)
aq task delete --task-id <task_id> --cascade          # delete a container + its whole subtree
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
| `manually_paused_descendants` | abandon refused: resume hand-paused descendants first |
| `cycle_check_skipped` | internal: the bulk graph-creation path was handed a task that is not a fresh leaf |

## Dependencies

```bash
aq task add-dependency --task-id <task_id> --depends-on <upstream_id> --dep-type blocks
aq task remove-dependency --task-id <task_id> --depends-on <upstream_id>
```

Dependency types: `blocks`, `parent-child`, `waits-for`,
`conditional-blocks`, `discovered-from`, `related`, `duplicates`,
`supersedes`. Only the first four gate readiness.

## Archives

Completed / failed tasks eventually archive:

```bash
aq task list-archived --project-id <pid>   # what has been archived
aq task archive --task-id <task_id>        # archive one COMPLETED/FAILED/BLOCKED task
```

Archiving is one-way: there is no restore or unarchive command, and an
archived id can never be recreated in a different project.

## Rules of thumb

- Read before writing. `aq task show <id>` before any mutation (`aq task get`
  is an operator read and a worker token is refused it).
- Explain non-obvious moves. When you close a task with `--outcome pass`, the
  summary should tell the reviewer what you did and why; link to relevant findings and comments.
- File emergent work rather than widening your own scope: `aq task create`
  from a worker session is expected, and lands behind a routing gate for
  triage. Don't build task *graphs* from a worker session — `--graph`,
  `--from-spec` and `formula cook` are supervisor-only.
- Don't retry an `out of scope: <command>` error. It is a property of your
  token, not a transient failure; say so in a comment and close or ask instead.
