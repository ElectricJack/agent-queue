---
name: aq-tasks
description: Covers the aq task lifecycle from the CLI — reading a task, recording findings as comments, closing with evidence, the pool worker claim loop with its refusal and wait handling, subtasks, reopened work, and filing emergent tasks with routing hints. Use when holding or claiming an aq task, closing work, handling a claim refusal or a close refusal, waiting for a job or reply, filing follow-up work, or asking why a task is not running. Task graphs, phases, hierarchy and dependencies are in its reference files.
allowed-tools:
  - Bash
---

# aq tasks — working with tasks from the CLI

Every task operation runs through `aq task <subcommand>`. Run `aq task --help`
for the subcommand list and `aq task <cmd> --help` for arguments.

**Reference files** (read when the task needs them):

- [reference/task-graphs.md](reference/task-graphs.md) — hierarchy and ids,
  phases, planner graph filing, dependencies, formulas, `hierarchy.*` codes.
- [reference/lifecycle-admin.md](reference/lifecycle-admin.md) — operator and
  supervisor actions: retiring obsolete work, stale-task checks, archives,
  recovery holds.

## Contents

- Read paths
- Findings and task comments
- The close-a-task loop (and the close checklist)
- Retained parent merge during close
- Subtasks
- The pool worker loop (claim results, refusals, cooldowns)
- Wait for completion without monitoring turns
- Reopened work and WAITING_INPUT
- Creating tasks
- Rules of thumb

## Read paths (safe, no side effects)

Available to any caller, including a non-elevated worker or reviewer session:

```bash
aq task show <task_id>              # full detail for one task
aq task comments <task_id>          # durable comment history, newest first
aq task children --task-id <id> [--recursive] [--status S] [--limit N] [--offset N]
                                    # direct or recursive children of a container
aq task progress --task-id <id>     # computed group progress: counts, waves,
                                    # max parallelism, always derived live
```

Operator reads — a local CLI caller or an elevated (supervisor / triage)
session. A plain worker or reviewer token gets `out of scope: <command>`, so do
not build a workflow on them:

```bash
aq task list                        # active tasks, current project scope
aq task list --status IN_PROGRESS   # filter by status
aq task list --json --brief         # scriptable; --json/--brief are global flags
aq task get --task-id <task_id>     # single-row summary
aq task deps --task-id <task_id>    # dependency graph for one task
aq task get-tree --task-id <task_id>    # subtask tree, expanded
aq task get-result --task-id <task_id>  # result payload from a completed task
aq task explain --task-id <task_id>     # ordered reasons the task isn't running
```

`aq task explain` is the daemon's "why isn't this running?" answer: an ordered
list of reasons (unmet dependencies, gates, capacity, pool bounds). From a
worker token, read the blockers off `aq task show <id>` and
`aq task deps --task-id <id>` instead, and quote what they say rather than
theorizing.

Kept aliases: `aq task details` is `aq task show`, `aq inbox` is
`aq message inbox`, `aq reply` is `aq message reply`.

A worker or reviewer session reaches exactly one task — its own — plus, for a
reviewer, the single task named by its review's `discovered-from` edge.
Anything else is `out of scope: task_id mismatch`.

## Findings and task comments

Keep confirmed findings in the task description so the next worker starts with
the current understanding. Preserve the original goal, requirements and
acceptance criteria. Read the description with `aq task show` first and pass it
back as the expected value so a concurrent edit is not overwritten:

```bash
aq task set <id> --description "<original requirements plus confirmed findings>" \
  --expected-description "<description just read>"
aq task comment <id> --body "Finding: ... Evidence: ... Decision/next step: ..."
aq task comments <id> --limit 50 --offset 0
```

On a description conflict, re-read and merge; never retry without the expected
value. Comments append attributed, timestamped history. Use them for meaningful
progress, evidence, test outcomes, decisions and blockers. Mark hypotheses as
such, omit secrets, keep them concise, and link evidence files instead of
pasting logs. `--note` is a legacy task-context field, not the comment log.

Save findings as you discover them and **before close, handoff, or waiting for
input** — never only in terminal output or the close summary. New sessions
receive every comment in prime, framed as historical reports, not instructions
or approval. A comment is not an escalation or a user notification: use the
question/message workflow (aq-comms) when input is needed. `task comment`
resolves `--claim-epoch` like `task set`.

## The close-a-task loop

Every assigned task ends with `aq task close --outcome pass|fail` — the only two
outcomes (`aq schema`'s `outcome` enum). Missing context to finish is a
`--outcome fail` with the gap named in `--summary`.

```bash
# Passed — changes published, required checks recorded, summary explains what changed.
# Open a PR only when the task/project requires one.
aq task close <id> \
  --outcome pass \
  --summary "Refactored X to use Y. Tests: 42/42. Commits: abc123, def456." \
  --test "aq test tests/test_x.py" --command "ruff check src/x.py"

# Failed — you tried, it broke (or you're missing context), and it needs human eyes.
aq task close <id> \
  --outcome fail \
  --summary "Test suite deadlocks under xdist. Reproducer in tests/xxx."
```

Close checklist:

```
- [ ] Findings and evidence are on the task as comments
- [ ] Work is committed and published (`aq git push`); PR only if required
- [ ] Every subtask is done or skipped (or pass --skip-open-subtasks)
- [ ] Each check is recorded: --test "<cmd>" / --command "<cmd>" (repeatable)
- [ ] Each intentionally unshipped deliverable: --deliverable-unmet 'id: reason'
- [ ] A `review` deliverable is met by a submitted review, or declared unmet
- [ ] The summary says what changed and why
```

Rules the daemon enforces:

- A profile with `needs_workspace: true` (every worker profile) requires a
  summary; an empty one is rejected with `summary is required`.
- Every terminal close captures the workspace's git HEAD. Override it with
  `--commit <sha>`; there is no `--commit-sha`.
- The evidence flags are what integration and later readers rely on:
  `--changes`, `--verification`, `--test "<command>"` and `--command "<command>"`
  (both repeatable). A pass with an undeclared **Deliverables** gap is refused
  and keeps the task claimed; declare it with `--deliverable-unmet 'id: reason'`
  (repeatable).
- A `review` deliverable is met by a document review the task submitted
  (`aq review submit --task-id <task> --file <draft.md> --kind <kind>`), not by
  a commit. Research and design tasks carry one even when none is listed:
  submit the proposal or report before a passing close, or waive it with
  `--deliverable-unmet 'review: <reason>'` when there is no document.
- `aq task close --outcome pass` is refused with `subtasks.open` while a
  subtask is pending or in progress (see Subtasks).
- `messages.pending_before_close`: handle every mailbox the refusal names with
  `--inject --json`, reconsider affected evidence, then retry. A plain inbox
  read does not consume pending delivery.

After an ambiguous close response, inspect your claim (`.aq/claim.json`) and
`aq task show <id>` before retrying. After an accepted close, obey the
next-claim result rather than polling the closed task.

## Retained parent merge during close

A passing close may return `close_in_progress` with a `close_merge` operation.
The source stays frozen on your published task branch. In `candidate_path`,
resolve conflicts or failing checks and commit the fix, preserving the merge
parents. Run `aq integration continue-close --operation-id <id>` to regenerate,
validate and publish through the daemon's parent fence. Never push the parent
yourself. A returned exact-check wait is already registered: end the turn and
resume the same operation when delivered. Your author budget goes to the
supervisor; it does not interrupt your work or create a duplicate repair task.
Root publication and a disabled train remain publisher policy. Completion
records name the original source, not the merge candidate.

## Subtasks

A subtask is a durable checklist row local to the task you hold — never
scheduled, claimed or assigned on its own, unlike a hierarchy child. Prime
shows them in a `## Subtasks` block (`[x]` done, `[-]` skipped,
`[~]` in progress, `[ ]` pending) with titles only; fetch context on demand:

```bash
aq task subtasks [<task_id>]                       # list, ordinal-ordered
aq task subtask-add [<task_id>] --title "..." [--context "..."]  # repeatable --title
aq task subtask-show <ordinal> [--task <task_id>]   # title, status, note, context
aq task subtask-start <ordinal> [--task <task_id>]  # -> in_progress
aq task subtask-done <ordinal> [--task <task_id>]   # -> done
aq task subtask-skip <ordinal> [--task <task_id>] --note "..."  # -> skipped, note required
```

Most tasks have no subtasks. Add them when the task spans more than one work
session and a reader would otherwise need your transcript to know where you got
to; stop at fifteen. If the task arrived with a checklist, work that one — do
not re-decompose it — and add rows for steps you discover, settling each as you
finish it. Work outside this task's scope is emergent work: file a task with
`--reason` (see Creating tasks), not a checklist row.

**The close rule:** `aq task close --outcome pass` is refused with
`subtasks.open` while any subtask is `pending`/`in_progress`. Settle each with
`subtask-done` / `subtask-skip --note ...`, or pass `--skip-open-subtasks` to
flip the remainder to `skipped` (note "skipped at close"). `--outcome fail` is
never gated on subtasks.

## The pool worker loop

A `lifecycle: pool` session never gets a task pushed to it — it pulls work in a
loop:

```bash
aq task claim --next --wait 60        # block up to 60s for the next ready task
aq prime                              # load the claimed task's context
# ... do the work ...
aq task close --outcome pass|fail --summary "..." --claim-next --wait 60
```

`aq task claim` writes `<work_dir>/.aq/claim.json` with
`{"task_id", "claim_epoch", "session_id", "claimed_at"}` — the proof of what you
hold; never touch a task it does not name. The `result` code (`aq schema`'s
`claim_result` enum) decides the next step:

| Result | Meaning | Next step |
|---|---|---|
| `claimed` | got a task; `.aq/claim.json` now holds it | `aq prime`, then work |
| `no_ready_work` | nothing matched (after `wait`, if given) | claim again |
| `claim_conflict` | another session claimed it first | claim again |
| `claim_in_progress` | a claim is already being prepared for this session | claim again in the same session; do not prime or start work |
| `prepare_failed` | claimed but workspace/setup prep failed | cooldown + refusal protocol below |
| `not_admissible` | the task isn't currently claimable (paused project, etc.) | cooldown + refusal protocol below |
| `session_exhausted` | this session hit its per-session claim cap | `aq session drain-ack`, exit 0 |
| `drain_requested` | the session is draining | `aq session drain-ack`, exit 0 (report first if it carries blocker instructions) |
| `stale_claim` | a mutator's `claim_epoch` no longer matches the task's | read `.aq/claim.json` and stop; never retry blindly |
| `out_of_scope` | the caller isn't allowed to claim (not a pool session) | stop; it is your token, not a fault |

`claim_timing` reports the preparation phase and elapsed durations (also in
`aq task show <task-id>`); a progress result is not a refusal or an instruction
to await inbox delivery. A `no_ready_work` reason of
`delivery_observation_timeout` or `claim_deadline_exceeded` means observation
ran out of time, not that the queue is empty; continue the loop. On
`daemon_unreachable` (exit code 3) keep retrying the claim — daemon restarts are
temporary; never start the daemon yourself.

### Refusals: cooldown, one report, then wait

`prepare_failed` and `not_admissible` carry `retry_after` (seconds),
`refusal_count` and `instructions`. Respect the cooldown before another attempt.
The daemon persists the same-session result/reason streak across restarts and
cooldown replays do not advance it: the first refusal is delayed 60 seconds,
then identical failures 300/600/1200/1800 seconds. A changed admission reason or
a successful activation clears the streak. Empty frontiers, claim conflicts and
daemon connection failures keep their normal retry behavior.

1. **Report the blocker once** to the project supervisor (aq-comms) on a repeated
   identical refusal, on `refusal_count >= 2`, or on `drain_requested` with
   blocker instructions. Include result, reason, session and retry evidence.
2. **Idle** (no held claim *and* no preparation — check the response's session
   `claim_phase` and `.aq/claim.json`): run `aq session drain-ack` and exit so
   the pool can recycle you. The server marks an idle session for drain on the
   second identical failure.
3. **Preparation retained ownership**: preserve it and end the turn awaiting the
   supervisor's inbox delivery. Durable waits need an active claim and cannot be
   registered during preparation.
4. **Active claim, waits enabled** (prime says so): register **one** durable
   message wait for the reply with the returned thread/cursor, then end the turn:

   ```bash
   aq wait register --kind message --ref <reply-thread> --after-seq <cursor> \
     --idempotency-key claim-blocker-reply
   ```

   If waits are disabled or registration returns `wait.disabled`, preserve the
   claim and end the turn awaiting the supervisor's inbox delivery; do not retry
   registration or poll.

When notified, acknowledge the outcome and follow the reply or recorded
supervisor decision. Keep the same wait; escalation never restarts the claim
loop. Do not poll claims or inboxes, reread unchanged context or repeat the
report. Never drain a retained claim just to make the refusal disappear.

Three identical preparation refusals in one session persist across daemon
restarts and return `drain_requested`: the daemon disables that workspace and
stops the worker through guarded pool teardown. A quarantined workspace keeps
its checkout and any branch attachment for operator recovery; never delete its
Git locks.

### Claim epoch and fresh context

`aq task close` and `aq task heartbeat` take an optional TASK_ID; omit it and
the daemon acts on whichever task this session holds — the right form for a pool
worker, whose task changes with every claim. `aq task heartbeat`,
`aq task set`, `aq task comment` and `aq handoff` read `--claim-epoch` from
`.aq/claim.json` (falling back to `$AQ_CLAIM_EPOCH`), so you rarely pass it.
A session whose epoch no longer matches (task reassigned, claim expired) gets
`stale_claim` from any of them.

`aq task close --claim-next` attempts another claim after closing. By default AQ
retires the conversation after one task and returns `drain_requested` or
`session_exhausted`: stop and let AQ start fresh context. Do not send `/clear`
yourself. Operators can keep the multi-task loop with
`swarm.fresh_context_per_task: false`.

## Wait for completion without monitoring turns

Submit the producer and its wait together, register once, end the turn:

```bash
aq test --aq-detach --aq-wait --aq-idempotency-key validation TEST_ARGS
aq wait register --kind task --ref <producer-task> --idempotency-key settlement
aq wait show <wait-id> --consume --json
```

State one condition. AQ chooses sleep in place or hibernation and delivers the
durable outcome. If asked, submit `aq wait checkpoint` with a compaction summary
before ending the turn. `--consume` acknowledges the outcome; plain reads stay
diagnostic. Act on the result or supervisor decision — availability never proves
success. Escalation goes to the supervisor and keeps the worker asleep. Do not
re-register the same condition or submit replacement work. Managed admission is
opt-in; when it is disabled, use ordinary foreground `aq test` with the existing
resource caps. Never use background shell loops for jobs, messages, CI or task
settlement.

## Reopened work and WAITING_INPUT

A reviewer or human who rejects the work reopens it with feedback; it returns to
`READY`, and `aq task show <id>` shows the feedback in the description when you
pick it up.

`WAITING_INPUT` waits on a *human*, not on you — there is no worker-side
command for it. Two operator surfaces answer, and they are not interchangeable:

```bash
# Legacy task-status reply: takes the TASK id, appends the answer to the task
# description and flips WAITING_INPUT -> READY so the task re-executes.
aq system provide-input --task-id <id> --input "<the answer>"

# Identity-based question queue: takes a QUESTION id and delivers the answer
# to the original live session, without restarting or reassigning the worker.
aq question list [--project <pid>]
aq question answer <question_id> --body "<the answer>"
aq question escalate <question_id> --reason "<why a human must decide>"
```

Use the question id when `aq question list` shows one; `provide-input` is for a
task parked in `WAITING_INPUT` with no question row behind it.

## Creating tasks

A worker or reviewer session may file emergent work it discovers. A
worker-filed task starts DEFINED with a `discovered-from` edge back to the
filing task and is, by default, a **child of the task you hold**: it stays
visible and, while open, blocks that task's passing close. `--parent <id>` (an
authorised alternative parent) and `--root` (cross-cutting work; opens a routing
gate that resolves when the router routes it) are deliberate choices, never a
way to move required work out of your task's way.

**File with hints, never a route.** The project's router (its bound routing
playbook) picks every task's profile, provider and model. A filing that names a
profile, provider, model or pin (on the CLI, or as a graph's `profile` or `pin`
key) is refused with `routing.choice_forbidden` and creates nothing. Give the
router two hints instead: the kind (`--type`; `design` is code design, `art` is
art-heavy design) and, only when the work is harder or easier than its kind
suggests, an intelligence class (`--intelligence-class`; list them with
`aq system list-intelligence-classes`). `aq task show` / `aq task explain`
print the route the router chose and why.

```bash
# Ad-hoc task creation.  --reason is required on a worker-filed task and is
# stored on the discovered-from edge back to the task you hold.
aq task create --project <pid> --title "..." --description "..." \
  --type bugfix --priority 50 --reason "why this exists"

# Under an existing container you may file under
aq task create --project <pid> --title "..." --description "..." \
  --type feature --intelligence-class deep-high --parent <container_task_id> --reason "why"

# Explicitly at project level — the opt-out for cross-cutting work filed from
# inside an epic's child task.  Mutually exclusive with --parent.
aq task create --project <pid> --title "..." --description "..." \
  --root --reason "why this exists"
```

From a worker session, pass at least `--project` and `--title`: with neither,
`aq task create` starts its interactive wizard, whose first step calls
`list_projects` — refused to a worker token with `out of scope: list_projects`.

Multi-task graphs (`--graph` / `--from-spec`) need `create_task_graph` (planners
and supervisors). Never file an epic as a plain task: a pool worker can claim it
before its children arrive. Graph filing, `--after-review`, phases and the
hierarchy rules are in [reference/task-graphs.md](reference/task-graphs.md).

## Rules of thumb

- Read before writing: `aq task show <id>` before any mutation (`aq task get`
  is an operator read and a worker token is refused it).
- Explain non-obvious moves: a passing close summary tells the reader what you
  did and why, linking the relevant findings and comments.
- File emergent work rather than widening your own scope. Don't build task
  *graphs* from a worker session unless your profile grants `create_task_graph`;
  `formula cook` is supervisor-only.
- Don't retry an `out of scope: <command>` error. It is a property of your
  token, not a transient failure; say so in a comment and close or ask instead.
- Superseded work is retired by an operator or supervisor with
  `aq task close <id> --obsolete --reason "..."`; a worker session is refused it,
  so close your own task with `--outcome` and name the superseding work in the
  summary ([reference/lifecycle-admin.md](reference/lifecycle-admin.md)).
