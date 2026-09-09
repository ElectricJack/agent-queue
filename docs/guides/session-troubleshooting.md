# Troubleshooting worker sessions

What to do when a worker is running but nothing is happening, or has stopped
and left something behind. Every recovery here is a real command with a real
diagnosis in front of it.

If you have not met sessions yet, read [Sessions](../concepts/sessions.md)
first — this guide assumes the vocabulary on that page (session, claim, claim
epoch, lease, nudge, drain).

> **Scope.** Most commands here are operator commands. Inside a worker's own
> session the token is scoped to that worker's task, and operator surfaces
> answer `out of scope: <command>` — that is the scope working, not a fault.
> Run these from a normal shell on the machine running the daemon.

## Start here: three commands that answer most questions

```bash
aq task explain <task-id>     # why isn't this task running?
aq session list --live        # which sessions exist right now
aq doctor                     # is anything structurally wrong?
```

`aq task explain` is the one to reach for first. It returns an ordered list of
reasons — graph blockers first, then capacity, then the operational flags this
guide is about — and for several of the failures below it prints the recovery
command directly in its answer
([`_cmd_explain_task`](../../src/commands/task_commands.py)).

To go from a task to the terminal that is working on it:

```bash
aq session list --live
aq session show <session-id>
aq session peek <session-id> -n 60
```

`peek` shows the last lines of the visible screen. It is the fastest way to
learn that a session is sitting on a login prompt, a permission dialog, or a
question nobody answered.

## A session that has gone quiet

**Symptom.** A task is `IN_PROGRESS`, its session is `running`, and nothing has
changed for a long time.

**What AQ is already doing.** Silence past the lease
(`sessions.lease_ttl_seconds`, 480 s by default) starts the stall ladder:
up to `sessions.stall_max_nudges` nudges spaced `stall_backoff_seconds` apart,
then an interrupt-and-restart with the task paused on a backoff, then
quarantine once `sessions.max_restarts` is spent
([`_step_stall_ladder`](../../src/sessions/reconciler.py)). You usually do not
need to do anything.

**Diagnose.**

```bash
aq session peek <session-id> -n 60
aq session logs <session-id> -n 40
```

`peek` is the live screen; `logs` is the harness's own transcript, normalized.
Each labels its source, so you know whether you are reading the transcript or
a peek-diff fallback. Three shapes are common:

| What the screen shows | What it means | What to do |
|---|---|---|
| An idle prompt with the work apparently done | the agent finished and never ran `aq task close` | the ladder's first nudge asks exactly this; wait one rung, or nudge by hand |
| A long-running command | not stalled at all; the agent should have heartbeated | nothing — but see the note below |
| A prompt waiting for an answer | the agent is blocked | answer it, or resolve the question through the normal escalation path |

> **Note for agent authors.** Anything that will run quiet for more than a few
> minutes should call `aq task heartbeat <task-id>` first. On providers without
> a pane the only other activity signal is a log file's mtime, so a long silent
> build looks exactly like a stall. The shipped bootstrap prompt says so
> ([`BOOTSTRAP_PROMPT`](../../src/sessions/spec.py)).

**Nudge it yourself:**

```bash
aq session nudge <session-id> "Status? If the work is done, close the task."
```

A `not_submitted` result is a real outcome, not a transport error: the text was
typed but Enter could not be confirmed. See the next section.

## A session that stopped responding to nudges

**Symptom.** The stall ladder stopped climbing. Messages never arrive. The
session looks alive and does nothing.

**Cause.** A nudge is typed into the harness's input line and submitted with
Enter, and Enter races the composer's repaint. When Enter loses, the text stays
in the input line — and the next nudge refuses to type into a non-empty
composer. Nothing self-heals from there.

**Diagnose and fix.**

```bash
aq doctor --check sessions.stuck_composer
aq doctor --check sessions.stuck_composer --fix
```

The provider remembers the marker of any nudge it typed and could not confirm,
so this is a read of provider state plus one screen capture per suspect session
— never a scan of every pane
([`src/doctor/session_checks.py`](../../src/doctor/session_checks.py)). The fix
presses Enter, gated on that same marker still being on the input line, so it
can only ever submit text the daemon typed. A human's draft never carries the
marker and is never touched.

Attaching a dashboard terminal and resizing the pane is the reliable way to
*cause* this, because the resize repaints the composer under the Enter.

## A session that quarantines at startup

**Symptom.** A session goes `starting` → `quarantined` within seconds, over and
over, and no work happens.

**Diagnose.**

```bash
aq session peek <session-id> -n 40
aq doctor --check harness.binaries
```

Startup runs the harness's declared dialog rules against the live pane. Most
rules dismiss something (a trust screen, a theme picker); two shipped rules
deliberately **quarantine** instead, because keystrokes cannot fix them:

| Rule | Harness | Meaning | Fix |
|---|---|---|---|
| `rate-limit` | [claude](../../src/sessions/default_harnesses/claude.md) | the provider is refusing work | wait out the cooldown; the exit classifier already paused the task |
| `login-required` | [codex](../../src/sessions/default_harnesses/codex.md) | the CLI is not authenticated | run `codex login` on the host |
| `login-required` | [gemini](../../src/sessions/default_harnesses/gemini.md) | the CLI is not authenticated | run `gemini` interactively once, or set `GEMINI_API_KEY` in the daemon environment |

If the pane is empty and the session died immediately, the executable is
probably not on the daemon's `PATH`. That failure is diagnosed before any file
is written and reports only the executable's basename
([`require_session_executable`](../../src/sessions/provider.py)):

```text
Executable 'codex' was not found or is not executable in the session PATH.
```

Add the installation directory to the daemon's `PATH` and restart the daemon.

## A session that keeps crashing right after start

**Symptom.** The task cycles through `PAUSED` and back, restart count climbing.

**What it means.** A process that dies within `sessions.restart_window_seconds`
of starting (600 s by default) is
classified `rapid_crash` — a launch or configuration problem, not work
([`classify_exit`](../../src/sessions/exit_classifier.py)). AQ restarts with
backoff and quarantines once the restart budget is spent, rather than retrying
forever.

**Diagnose.**

```bash
aq session logs <session-id> -n 40
aq doctor --check harness.drift
```

`harness.drift` compares each vault harness copy against every version AQ has
shipped, and reports it as `current`, `stale`, `edited` or `missing`. An
`edited` copy with a parse error is a common cause of a harness that launches
but immediately fails. To discard a local edit and get the shipped file back:

```bash
aq vault reset-harness --dry-run        # report status, write nothing
aq vault reset-harness claude
```

The running daemon's vault watcher picks the change up without a restart.

## Stale ownership: a row and a process that disagree

The database row and the operating system can disagree in both directions, and
AQ handles each one differently. Knowing which one you are looking at is most
of the diagnosis.

| Direction | What AQ does | The flag you see |
|---|---|---|
| Session alive, task already closed or gone | stops the session (normal drain) | — |
| Task `IN_PROGRESS`, session row not live | blocks the task and frees the agent and workspace | `needs_attention: session_not_live` |
| Session over `agents.stuck_timeout_seconds` | force-kills, blocks the task | `needs_attention: stuck_timeout` |
| Pool session terminated while holding a task | releases the claim, blocks the task | `needs_attention: exited_holding_task` |

```bash
aq task explain <task-id>
```

```text
needs_attention: session_not_live
```

**Recovery.** These are terminal for the *task*, not the queue: the work is
intact, the workspace and agent are released, and the task is `BLOCKED`
pending a decision.

```bash
aq task restart <task-id>               # try again from the top
aq task resume --task-id <task-id>      # continue a paused task from its checkpoint
```

> **Warning.** A row saying `stopped` is not proof that a process stopped. It
> means "AQ last observed it stopped". Code that must actually know calls
> [`confirm_stopped`](../../src/sessions/provider.py) — a fresh, cache-bypassing
> probe that answers "no" when it cannot tell, and which the `subprocess`
> provider does not implement at all. When you are about to do something
> destructive by hand (deleting a worktree, force-pushing a branch a worker may
> still own), confirm with the operating system, not with a status column:
>
> ```bash
> tmux -L aq list-sessions | grep <session-name>
> ```

Related structural checks:

```bash
aq doctor --check claims.holder_consistency
aq doctor --check pools.orphan_agents
aq doctor --check tasks.stale_attention
```

## `stale_claim`: the task moved on without you

**Symptom.** A worker's command is refused:

```text
claim epoch 1 is not current for solid-grove.8 (current 2); the task is no longer yours
```

**What it means.** Exactly what it says, and it is a safety feature. Claiming a
task bumps `tasks.claim_epoch`; every task-mutating command carries the epoch
from the worker's `.aq/claim.json`, and
[`_assert_session_owns`](../../src/commands/claim_commands.py) refuses anything
stale. Somebody else — a retry, a reclaim after a stall, an operator restart —
now holds this task.

**What a worker should do.** Stop working on it. Do not force anything. Check
what you actually hold:

```bash
cat .aq/claim.json
```

If the file names a different task than the one you were working on, the
session has already been handed new work. If the file is missing, you hold
nothing; claim again.

**Related result codes** from `aq task claim`
([`ClaimResult`](../../src/models.py)):

| Result | Meaning | What to do |
|---|---|---|
| `no_ready_work` | nothing matches this profile right now | claim again, optionally with `--wait` |
| `claim_conflict` | somebody won the race | claim again |
| `claim_in_progress` | this session is already mid-claim | let it finish |
| `not_admissible` | the project is paused, out of budget, or inactive | wait as instructed; a paused project will not clear on its own |
| `session_exhausted` | this session has used its claim budget | exit cleanly |
| `drain_requested` | the daemon wants this worker to stop | exit cleanly; the pool starts a fresh one if it needs the capacity |
| `prepare_failed` | the workspace could not be prepared | see the next section |
| `out_of_scope` | the task is not this session's to touch | do not retry |

## `slot_reset_failed`: the workspace could not be made clean

**Symptom.** A claim comes back `prepare_failed`, and the task carries
`needs_attention: slot_reset_failed`.

**What it means.** Before a pool worker is handed a task, its worktree slot is
reset to a clean state. That reset failed — a lock file, a dirty checkout that
cannot be discarded, a missing directory. The claim is released so nothing is
half-held, and the failure is recorded with enough detail to act on
([`claim_commands.py`](../../src/commands/claim_commands.py)).

**Diagnose.**

```bash
aq task explain <task-id>
```

```text
needs_attention: slot_reset_failed: <reset error> (attempt 2; automatic retry).
After fixing the cause: aq task resume --task-id <task-id>
```

The attempt counter is the important part. The first two failures release the
task back to `READY` with an exponential preparation backoff, so AQ retries on
its own. **On the third**, the task goes to `BLOCKED` and the retry becomes
`manual` — that is AQ declining to loop on a fault it cannot fix.

**Recover.**

1. Look at the recorded reason (`slot_reset_failure` metadata, printed by
   `explain`) and fix the underlying cause in the workspace.
2. Check the slot itself:
   ```bash
   aq doctor --check pools.stale_worktree_checkouts
   ```
3. Then release the task:
   ```bash
   aq task resume --task-id <task-id>
   ```

A successful preparation clears the checkpoint, the backoff ladder and the
attention flag together, so a later unrelated failure starts from zero.

**The related flag** is `integration_prepare_failed`, which means the branch
fence — not the filesystem — was the problem. That one intentionally retains
every binding for recovery rather than unlocking a checkout an integration
fence still names; see
[development integration](development-integration.md).

## A pool worker that stopped asking for work

**Symptom.** A `p-` session is `running`, holds no task, and never claims
anything.

**What AQ does.** A healthy worker may sit in one server-side long poll for
`swarm.claim_wait_max` seconds and then need a scheduler tick before the next
one, so silence alone is not evidence. The check requires *both* a stale
`prepare_failed` result and no later claim for a bounded grace — two full poll
windows, and never less than `swarm.prepare_timeout`
([`_step_abandoned_pool_claim_loop`](../../src/sessions/reconciler.py)). The
session is then recycled behind a database compare-and-set, so a late claim
cannot lose a race with its own teardown.

**Check the pool rather than the session:**

```bash
aq pool status
aq doctor --check pools.stuck
aq doctor --check pools.preparing_stuck
```

See [worker pools](worker-pools.md) for what the sizing is trying to do.

## Pausing, draining and stopping — which one do you want?

These are four different actions and they are not interchangeable.

| You want | Command | What actually happens |
|---|---|---|
| This task to stop and keep its work | `aq task pause --task-id <id>` | the workspace is checkpointed to a Git ref, the slot is freed, the task waits for an explicit resume |
| That task to continue | `aq task resume --task-id <id>` | the checkpoint is validated, then restored onto the same branch |
| This session to stop taking new work | `aq session sleep <session-id>` | **intent only.** Nothing is signalled; the reconciler drains it on a later tick |
| That session running again | `aq session wake <session-id>` | named sessions only; the next tick starts it |
| This session to stop **now** | `aq session kill <session-id>` | fenced kill of the process tree; the task is *not* transitioned — the next tick classifies the exit exactly as it would a crash |

`aq session sleep` on a pool session holding a claim is deliberately deferred:
public session control must not interrupt a worker mid-task. The session drains
once the claim is released.

### What a pause preserves

`aq task pause` takes a Git checkpoint before the slot can be reused:
`HEAD`, the staged index, and the unstaged and untracked working tree are each
committed to unreachable objects and parked under `refs/aq/task-pauses/<hex>`
([`src/orchestrator/task_checkpoint.py`](../../src/orchestrator/task_checkpoint.py)).
The live index and branch are not touched.

`aq task resume` validates that checkpoint before anything is reset. Three
refusals, each leaving the workspace untouched:

```text
Paused task checkpoint repository is missing; workspace was left unchanged
Paused checkpoint belongs to a different repository; workspace was left unchanged
Paused task branch changed since its checkpoint; workspace was left unchanged
```

Two other messages are worth recognising:

```text
Task is paused; execution cleanup is still running. Retry Resume.
Task is paused, but its workspace could not be preserved: <reason>. Retry Resume.
```

Both are honest transient states — retry the resume. If the checkpoint truly
cannot be taken after its bounded retries, the pause completes anyway and
records *why* no checkpoint exists, because a half-paused task is worse than a
paused one carrying a note.

## After a daemon restart

**Expected behaviour.** In-flight work survives. On boot AQ lists the live
sessions each provider can see, and adopts the rows whose instance token
matches, keeping their tasks `IN_PROGRESS`
([`adopt_on_start`](../../src/sessions/reconciler.py)). Dead rows fall through
to the exit classifier. A live process with *no* row is killed — with no row
there is nothing to match against, and an unreachable agent writing to a
workspace the scheduler believes is free is the worse outcome.

**When enumeration fails**, the entire name prefix is deferred: nothing is
adopted and nothing is reaped, this tick or the next, until the provider can
answer. That is the "unknown is not dead" rule, and it is why a temporarily
unreachable tmux server does not produce a massacre.

**The provider matters here.** `SubprocessProvider.list_running` only knows
sessions this daemon process started, so after a restart it enumerates nothing:
no session is mis-reaped, but none is re-adopted either. The shipped default is
`subprocess`, because it is the provider every host can construct; **this
install sets `sessions.provider: tmux`** in `~/.agent-queue/config.yaml`, for
that reason among others.

**Check what happened:**

```bash
aq session list --live
aq doctor --check sessions.env_markers
```

`sessions.env_markers` catches a different restart hazard: a daemon started
from *inside* an agent's terminal inherits that harness's control variables and
behaves strangely. Restart it from a clean shell.

## Related pages

* [Sessions](../concepts/sessions.md) — the mechanism behind every symptom
  here, including epoch fencing and the reconciler's tick order.
* [Harness reference](../reference/harnesses.md) — dialog rules, resume support
  and per-CLI differences, which explain several startup failures.
* [Terminals and claims](../reference/terminals-and-claims.md) — the live pane
  and interactive terminal, and what the claim file guarantees.
* [Worker pools](worker-pools.md) — sizing, placement and drain policy for
  `p-` sessions.
* [Migrations](migrations.md) — why a worker may never run Alembic, and what
  the refusal message means.

## Source and tests

Implementation: [`src/sessions/reconciler.py`](../../src/sessions/reconciler.py),
[`src/sessions/exit_classifier.py`](../../src/sessions/exit_classifier.py),
[`src/doctor/session_checks.py`](../../src/doctor/session_checks.py),
[`src/orchestrator/task_checkpoint.py`](../../src/orchestrator/task_checkpoint.py).

```bash
aq test tests/test_session_reconciler.py tests/test_session_doctor.py tests/test_session_commands.py
```
