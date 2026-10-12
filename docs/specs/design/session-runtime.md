---
tags: [design, sessions, runtime, tmux, harnesses, lifecycle]
---

# Session Runtime — tmux-First Provider Model

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../../README.md) for what AQ does today, and
> see [historical material](../../history/README.md) for how this material is
> organised.

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [guiding-design-principles](guiding-design-principles.md) (#1 files as source of truth, #7 events not coupling, #9 simple interfaces, #10 fewer moving parts)
**Related:** [worktree-execution](worktree-execution.md), [supervisor-agent](supervisor-agent.md), [aq-surface](aq-surface.md), [trust-and-ops](trust-and-ops.md), [feature-pauses](feature-pauses.md), [workspaces-v2](workspaces-v2.md), [specs/orchestrator](../orchestrator.md), `docs/analysis/framework-overhaul-todo.md` (Workstream A, D1)

---

## 1. Problem Statement

Today an agent is a stream the daemon blocks on. `src/runtimes/claude_sdk.py` drives the
Claude Agent SDK in-process; `src/runtimes/acpx.py` pipes NDJSON from an `acpx` subprocess.
Both implement the `Runtime` ABC (`src/runtimes/base.py`): `start(task)` then `wait(on_message)`
— a coroutine that holds the agent's entire life inside one `asyncio.Task` in
`_execute_task` (`src/orchestrator/execution.py`). The consequences:

- **Agents die with the daemon.** A restart aborts every in-flight task; `_recover_stale_state`
  (`src/orchestrator/core.py`) blanket-resets IN_PROGRESS → READY and BUSY → IDLE because
  nothing can survive the process. Long tasks are un-restartable by construction.
- **Agents are invisible.** There is no terminal to attach to, no pane to peek at. Progress
  is whatever the SDK callback forwards; a wedged agent looks identical to a slow one.
- **Completion is inferred, not declared.** Process exit is the success signal, so a crash,
  a rate-limit death, and a finished task all arrive through the same `AgentOutput`, and
  classification lives in fragile string matching on error messages.
- **A new harness is a Python module.** Supporting Codex/Gemini meant a protocol adapter
  (`acpx`) with its own streaming quirks; every CLI difference becomes runtime code.
- **`stuck_timeout_seconds` is the only stall defense** — a single blunt `asyncio.wait_for`
  that kills work instead of nudging it.

The session runtime replaces this with the Gas City model: each agent is a fully contained
interactive CLI session that the daemon **starts, observes, nudges, and adopts — never a
stream it blocks on**. The daemon reconciles desired state against observed state on its
existing ~5 s cascade.

## 2. Goals and Non-Goals

**Goals**

1. Agents are independent OS sessions: they survive daemon restarts, humans can attach to
   them, and the daemon re-adopts them on boot.
2. Harness-agnostic: a new CLI agent (claude, codex, gemini, opencode, …) is a markdown
   file in the vault, not a Python module.
3. The execution pipeline becomes launch-and-return; completion, failure, and progress
   arrive as events. The orchestrator stays deterministic (zero LLM calls).
4. Structured channels for truth: completion via explicit `aq task close` + drain-ack;
   liveness via process-table env markers; progress via harness transcript files. Pane-text
   scraping is confined to readiness, startup dialogs, and nudge-submit confirmation.
5. A stall ladder (nudge → restart-with-resume → quarantine) replaces kill-on-timeout as
   the primary defense; `stuck_timeout_seconds` remains only as a backstop.

**Non-Goals (now)**

- Kubernetes / ssh / remote providers. The provider ABC leaves room; nothing is built.
- ACP transport. `acpx` is deleted after dual-run; revisit only if a harness ships no CLI.
- Windows-native sessions. The daemon targets Linux/WSL2; tmux is POSIX-only. The
  `subprocess` provider is the degraded fallback, not a Windows story.
- Print-mode execution (`claude -p`, `stream-json`). Task sessions are always interactive.
- Worktree lifecycle (owned by [worktree-execution](worktree-execution.md)), message routing and nudge-delivery
  policy (owned by [supervisor-agent](supervisor-agent.md)), the CLI envelope and hook payload content (owned
  by [aq-surface](aq-surface.md)), env scrubbing and trust boundaries (owned by [trust-and-ops](trust-and-ops.md)).

## 3. Concepts

| Concept | Definition |
|---|---|
| **Session** | One OS-level agent run: a tmux session (or subprocess) whose initial process is the harness CLI. Persisted as a `sessions` row. |
| **SessionProvider** | Pluggable backend that creates/observes/kills sessions. Registry: `tmux` (default), `subprocess` (fallback), `fake` (tests). |
| **SessionSpec** | Immutable launch description built from profile + harness + task: name, work_dir, argv, env, prompt, readiness hints, dialogs, lifecycle. |
| **Harness** | A markdown profile in `vault/harnesses/<name>.md` describing one CLI agent: command, prompt delivery, resume flags, readiness prompt, process names, hooks, transcript paths, dialogs. |
| **Lifecycle** | `task` — one session per task, killed after drain-ack; `named` — persistent (supervisor, warm workers), sleeps and wakes. |
| **Session name** | Task sessions `s-<task_id>`; named sessions `n-<profile>[--<project>]`. Charset `^[a-zA-Z0-9_-]+$` (profile/project ids are sanitized into it). Consumers address named sessions by a *logical name* (e.g. `supervisor-<project_id>`, see [supervisor-agent](supervisor-agent.md)); the session manager maps logical names to provider names — other specs never construct provider names directly. |
| **Epoch / instance token** | `AQ_DAEMON_EPOCH` identifies the daemon run that launched a session; `AQ_INSTANCE_TOKEN` uniquely fences one launch so kills never hit a same-named successor. |

**Identity and liveness.** Every session's environment carries `AQ_SESSION_ID`,
`AQ_TASK_ID`, `AQ_PROJECT_ID`, `AQ_PROFILE`, `AQ_DAEMON_EPOCH`, `AQ_INSTANCE_TOKEN`,
`AQ_WORK_DIR`, `AQ_API_URL`, `AQ_API_TOKEN`; `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` are
stripped (today's `isolated_env` in `src/runtimes/_subprocess.py`). Liveness and adoption
are decided by scanning the process table for these markers (`/proc/<pid>/environ`) — never
PID files, never tmux session names alone (names get reused; PIDs get recycled). The
`AQ_API_URL`/`AQ_API_TOKEN` pair is how `aq` inside the session reaches the daemon; token
scoping is specified in [trust-and-ops](trust-and-ops.md).

## 4. Behavioral Model

### 4.1 Task sessions

The harness runs as a **full interactive CLI** in the pane — `claude "<bootstrap>"` with
the prompt as a positional argument, never `claude -p` print mode. The TUI stays attachable
and human-readable; prompts larger than ~1 KB are written to a temp file and delivered via
`sh -c '… exec <cmd> "$__aq_prompt"'` (tmux's `new-session` buffer is ~2 KB).

The bootstrap prompt is deliberately short: *"You are running task `<id>` in `<work_dir>`.
Run `aq prime` and follow it. When done: `aq task close …` then `aq session drain-ack`."*
The full prompt (role, project override, task, attachments, workspaces block — L1/L2 slots
reserved while memory is paused per [feature-pauses](feature-pauses.md)) renders to `<work_dir>/.aq/prompt.md`
and is returned by `aq prime` (envelope: [aq-surface](aq-surface.md)). `work_dir` is the task's slot
worktree, prepared before launch per [worktree-execution](worktree-execution.md).

**Completion is explicit, and only explicit:**

1. Agent runs `aq task close <id> --outcome … --work-outcome … [--commit …] [--notes …]`
   (outcome metadata schema owned by the work-graph substrate). The daemon runs the
   completion pipeline (commit/push/PR/verify — `_run_completion_pipeline`,
   `src/orchestrator/git_ops.py`) and transitions the task.
2. Agent runs `aq session drain-ack`. The reconciler sees the ack, kills the session
   (instance-token-fenced), and marks the row `stopped`.

A pool worker's ack also writes `desired_state=stopped`, and its teardown waits until the held
task is closed. One task can never be closed: a delegate of an integration operation that no
longer needs it (the operation ended, or the delegate's stage expired and the operation moved on).
Its close is refused with a retirement record and `next_step: aq session drain-ack`. When the
agent's own ack is recorded and durable integration state proves that retirement, the reconciler
stops the worker through the ordinary pool teardown instead of waiting. The proof follows
`get_retired_integration_writer`, and any seat a running operation, including a `human_required`
one, could still hand back is not retired. The task stays unclosed and an attached integration
owner keeps the stopped session's binding for owner recovery (design: hierarchical integration
trains, pull-model writer handoff).

The other wait with no end is a close that already committed. `aq task close` makes the task
terminal before it hands the branch back, saves the completion record and releases the claim. A
daemon restart in between leaves the worker bound to a finished task, and its attached branch owner
keeps the displaced-claim release from detaching it. When the agent's own ack is recorded and
`get_settled_pool_claim` proves the claim settled, the reconciler stops the worker through the same
teardown. The proof has three parts: the task is `COMPLETED` or `FAILED` with no holder, under
this session's own claim epoch; a completion record or a `code`/`noop` delivery receipt was written
after this session's attempt began; and no running integration operation owns the task in any seat.
Before the record is saved or delivery writes a receipt, the accepted-close marker counts instead.
The terminal transition that accepts a close writes `accepted_close` (`{completion_id, session_id,
claim_epoch}`) in its own transaction, and the marker proves the close when it names this session
and its last claim epoch. `close_session_id` never counts: `aq task close` writes it before the
close is accepted, so it outlives a refused close. The reconciler also waits while a completion of
the task still holds its control lock in this daemon, so an ack after a timed-out close never stops
the worker mid-handoff.
The task's status and completion stay as they are. The owner keeps the stopped session's binding,
and owner recovery releases it after preserving unpushed work.

The completion record itself survives that window. Before the transition, `aq task close` drafts
the record as `pending_completion` task metadata, bound to the attempt's identity: completion id,
closing session and claim epoch. Every status write that accepts the close stores the same identity
as `accepted_close` metadata in its own transaction. That covers the plain terminal and retry
transitions, the verification reopen, review approval, repair-delegate completion and managed-parent
suspension or completion. At daemon start, before stale-state recovery moves any task, a draft is
saved under its own id (idempotently) only when `accepted_close` names it exactly and the task is
still on that claim epoch. Every other draft is dropped. The task's status is never evidence: a task
requeued `READY`, redefined, failed or cancelled by another path has no matching identity, so no
record or pass is invented. A refused or failed close deletes its own draft unless its transition
had committed.

**Process exit with the task still IN_PROGRESS is a failure signal**, routed through the
exit classifier:

| Evidence | Verdict |
|---|---|
| Pool session with persisted `desired_state=stopped` | Normal drain, even with an open task; never quarantine the pool for this requested exit |
| Rate-limit text in the final pane capture | Task → PAUSED (`rate_limit`) with provider cooldown; session `sleep_reason=rate_limit` |
| Rapid crash (death within `restart_window` of start) | Restart with backoff, `--resume <session_key>` when the harness supports it; after `max_restarts` inside `restart_window` → quarantine |
| Task already closed, session lingering | Normal drain path (kill, `stopped`) |
| Productive death (ran long, exited, task open) | `needs_attention` / re-queue per retry policy — never silently READY |

Restart counters (`restarts`) and `quarantined_at` are **persisted on the session row**, so
the ladder survives daemon restarts.

After observing a dead pool process and capturing its final output, the reconciler
rereads the session before classifying it. An operator kill can persist stop intent
while those probes await; an earlier live-session snapshot must not turn that requested
stop into a rapid crash. A session already stopped or removed, or replaced by another
instance during the probes, is not classified from that stale observation.

### 4.2 Named sessions

The global supervisor's terminal always has system scope (`project_id = NULL`),
including explicit starts and resumes from the agent flock. Its launch form shows
access to all projects and has no project selector. A caller-supplied project is
ignored for the canonical global supervisor before project validation or live-session
reuse; worker terminals retain their optional project attachment and scope checks.
The supervisor's launch/resume instructions explicitly describe its global authority;
reconnecting a live terminal reuses that exact session and never asks for a project.

Supervisor idle sleep requires both terminal inactivity and no outstanding supervision
work. The global supervisor checks all projects; a project supervisor checks its own
project. Outstanding work includes non-completed, non-archived tasks (including failed,
blocked, paused, or waiting tasks), open gates, active integration batches, and live
task-bearing sessions. The global check also includes live playbook runs. Archived
projects and task history do not keep a supervisor awake, but a live task-bearing
session remains a responsibility even if its project was archived. Idle pool workers
without a task do not count. Failure to read this state defers automatic sleep.
This check must use bounded existence queries and must not fabricate terminal activity
or mark the message transport busy: a quiet supervisor can still accept messages.
Once the outstanding work clears, the existing inactivity timeout applies. Other named
sessions retain their profile-based idle policy.

Named sessions (`lifecycle: named` on the profile — the supervisor, warm pool workers) are
persistent interactive CLIs. Work arrives as nudges and inbox injections (delivery policy:
[supervisor-agent](supervisor-agent.md)). Behavior knobs live on the profile:

- `wake_mode: resume | fresh` — wake a sleeping session with `--resume <session_key>` or a
  clean start.
- `idle_timeout` — no transcript activity and no pending work for this long → the
  reconciler drains the session to `sleeping` (`sleep_reason=idle_timeout`).
- `max_session_age` (+ deterministic jitter) — a session older than this is recycled: the
  reconciler triggers `aq handoff` (writes a handoff note the successor receives via
  `aq prime`), kills, and relaunches. Jitter prevents fleet-wide simultaneous recycling.

The reconciler builds the **desired set** of named sessions each tick from profiles with
`lifecycle: named` (per project where project-scoped) and converges: missing+wanted →
start (or wake), present+unwanted → drain, config drift → recycle via handoff.

**The state machine is enforced** (2026-08-27). `update_session` validates every write to
`state` against the transition table in `session_queries.py` and raises
`InvalidSessionTransition` on an illegal edge; re-writing the state a row already has is a
no-op, not a violation. Two edges are load-bearing: nothing revives a `stopped` row (a
restart produces a *new* row, so a revived one would put two live rows under one name),
and `quarantined` is terminal in code rather than only in prose. Raising is safe for the
reconciler, whose steps are individually guarded — a bad edge fails one step loudly
instead of corrupting the row.

**Intent is a column, not an inference** (2026-08-27). `sessions.state` is the runtime
projection — what was last observed. `sessions.desired_state` (`running | sleeping |
stopped`) is what the daemon wants. Collapsing both into `state` is what limited
convergence to one direction: "sleeping" and "should be sleeping" were the same value, so
a wake branch would have fought the drain branch every tick.

Intent is written by whoever *forms* it — the lens on cold start, the reconciler on idle
drain or a terminal verdict, an operator via `aq session sleep | wake | kill`. Draining
writes both fields at once, so a drained session stops being wanted at the moment it stops
running. **Waking is always explicit**; nothing infers it from activity.

An explicit `desired_state=sleeping` on a starting or running named session stops its
process on the next reconciler tick, independently of idle timeout, outstanding work,
or pending input submission. A failed stop leaves the runtime state live for retry.
Recording sleep intent or transitioning to sleeping cancels that session instance's
active cron schedules and archives undelivered scheduled prompts in the same transaction.
An explicit wake received during the stop remains wanted and can start a successor.
Every wake resolves the current profile's intelligence class and model, even when
resuming the previous conversation; historical session settings are not launch settings.
