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

Starting is delegated to the session lens rather than reimplemented in the reconciler
(the lens owns token minting, the global-supervisor cases and work_dir resolution).
Failed starts spend the stall ladder's `max_restarts` budget and end in `quarantined`, so
a misconfigured named session costs a bounded number of attempts rather than one per tick.
See `docs/superpowers/specs/2026-08-27-session-desired-state-design.md`.

Still deferred: profile-declared session *pools* (starting a session that has no row at
all) and recycle-on-drift. Both are writable now that intent is representable; both need
the [supervisor-agent](supervisor-agent.md) routing story settled first.

### 4.3 Heartbeats, leases, and the stall ladder

`agents.last_heartbeat` is fed from two sources: transcript `in-turn` activity (§4.5) and
explicit `aq task heartbeat` calls (the prompt instructs agents to call it before long
commands). A lease TTL of ~8 minutes without either marks the task **stalled** — not dead.

Stalled tasks climb a ladder, each rung a typed event:

1. **Nudge** (`task.stalled` → `task.nudged`): inject *"No progress for N min on task
   `<id>`: `aq task close`, or keep working."* via the provider's nudge pipeline
   (`stall_reminder` in `src/sessions/reconciler.py`). The reminder names the task id once
   and stays within two rows of an 80-column pane: a submit is confirmed only by finding
   the exact text in the composer, and Claude Code 2.1.286 in an 80x24 pool pane shows only
   the last 7 rows of taller input. The previous wording (four inline commands, the id
   three times) ran to 9 rows for a 72-character repair id and could never be delivered.
   Agent-question replay treats every wording the daemon has typed as machine input
   (`_MACHINE_STALL` in `src/sessions/questions.py`), not a reply.
2. **Backoff and repeat** up to 3 nudges.
3. **Interrupt + restart** (`task.restarted`): C-c, kill, relaunch with `--resume` so
   conversation context survives.
4. **Quarantine** (`task.quarantined`): session `quarantined_at` set, task
   `needs_attention`. No further automatic action; a human (or the supervisor agent)
   decides.

A session whose CLI is parked on its provider's usage-limit screen is not climbed at all:
before each rung the ladder checks the pane's tail against a strict set of the CLIs' own
blocking limit messages and, on a match, stops the process and applies the exit path's
`rate_limit` verdict instead (provider-failover D13, `src/sessions/usage_limit_screen.py`;
`provider_failover.mode: enforce` only).

**A composer AQ may not touch.** A nudge the composer guard defers is not a failed
attempt, so it spends no rung and no backoff — unless the refusal says *no person is
typing*. The refusal carries a structured `NudgeReason` (`src/sessions/provider.py`) rather
than prose to be parsed:

| Reason | Producer's evidence | Ladder |
|---|---|---|
| `draft` | the composer was read and holds text | holds — a person is writing |
| `terminal_busy` | copy mode, an attached client, or no known input line | holds |
| `recent_input` | keys were accepted within the quiet window | holds |
| `stale_frame` | OpenCode's box at its exact idle geometry holds text on the rows *beside* the cursor, where typed input cannot be | escalates |
| `unreadable` | tmux refused the read, the pane moved between the two reads, or AQ's own pending injection could not be identified or cleared | escalates |

A refusal of either escalating kind is then put to evidence **independent of the
composer**, and the answer is three-valued (`StalledDeferral`, `_deferral_verdict`):

| Verdict | Evidence | What the ladder does |
|---|---|---|
| `hold` | a person is at the composer, or the harness's own record shows the conversation moving within the lease | nothing spent, nothing announced — as before |
| `report` | no record exists to ask, or the one named cannot be resolved or stat'ed | nothing spent; **announced** — a WARNING and `task.stalled` with `evidence="unverified"`, quoting how long the pane has shown the same thing |
| `escalate` | that record is older than the lease (`harness_progress`: the reader-resolved transcript's mtime) | the rung is spent with nothing typed: `task.stalled` carries `deferred_reason`, no `task.nudged` follows, and the existing backoff → restart/quarantine (or pool termination) path runs |

**Unknown is not stalled, and it is not silent either.** `hold`-worthy evidence is the
progress record: a person typing into a composer writes nothing there. When there *is* no
such record — `opencode` has no reader and no session identity (neither opencode harness
declares `session_id_flag`, so `session_key` is null and there is nothing to scope a lookup
to), or the file cannot be resolved or stat'ed — that is unknown, and unknown must never
release a claim. Such a stall is **announced** instead: a WARNING naming session, task,
idle time and reason, plus `task.stalled` with `evidence="unverified"`, rate-limited to
one announcement per `_STALL_REPORT_INTERVAL_SECONDS` (900 s) per holder instance. That
announcement is the part that was actually missing — `vivid-quest-44.3` produced **zero**
`task.stalled` events while holding its task through 116 identical refusals over 78
minutes.

**Nothing about the terminal can upgrade that answer.** A pane that has stopped moving is
what an agent *between two writes* looks like as much as what a wedged TUI looks like — one
long tool leaves the composer untouched for minutes — so the screen is read
(`peek` on the pane tail, hashed: the whole screen, never the composer box, because the
composer is what the refusal already described) and **quoted, never spent**. Digests are
keyed by `(session id, instance token)`, so a relaunched session inherits nothing from its
predecessor's observations; the clock is the time the screen last *changed*, not the time it
was last sampled, and a failed read leaves the previous reading untouched rather than
re-stamping it. Both caches are pruned by age.

Closing this last gap for `opencode` needs **scoped, per-session** activity evidence for a
harness AQ cannot read — report R7's missing OpenCode session identity, since neither
opencode harness declares `session_id_flag` and there is therefore nothing to scope a store
lookup to. Until that exists, a wedged OpenCode holder is reported to a person every
`_STALL_REPORT_INTERVAL_SECONDS` (900 s) rather than terminated on a reading of a terminal.

Named/supervisor sessions, durable waits and instance-token fencing are unchanged: they are
filtered before the ladder, and every rung still addresses the session by its exact
instance.

`stuck_timeout_seconds` (config `agents.stuck_timeout_seconds`) stays as the final backstop
above the ladder, applied by the reconciler rather than `asyncio.wait_for`.

### 4.4 Daemon restart: adoption, not reset

On boot the reconciler runs an **adoption pass**: `provider.list_running("s-") + ("n-")`
cross-referenced with a process-table scan for `AQ_SESSION_ID`. Live sessions keep their
tasks IN_PROGRESS and their rows are re-bound to the new daemon epoch; dead sessions (row
says running, no process) go through the exit classifier. The blanket reset in
`_recover_stale_state` no longer applies to session-runtime tasks; `aq daemon start --reset`
remains the explicit admin escape hatch that kills and resets everything. Sessions from an
older epoch are adoptable — epoch is provenance, not a validity test; the instance token is
what fences kills.

### 4.5 Observation: transcripts, peek, activity, tokens

**Transcript readers** are the structured progress channel. Per-harness readers resolve the
harness's own session log from `work_dir` + session key (Claude
`~/.claude/projects/<slug>/*.jsonl`; Codex `~/.codex/sessions/…`; Gemini `~/.gemini/tmp`),
poll ~2 s, normalize entries, and produce: `notify.*` events for the dashboard (replacing
the SDK message callback; Discord no longer streams a per-task thread — it consumes task
activity for the hourly digest only), token usage into the token ledger
(`db.record_token_usage`), model/context-% for session views, and `in-turn`/`idle` activity
for the heartbeat. **This is the signal; pane text is a hint.**

Claude and Codex have readers as of 2026-08-27; Gemini does not, and until it does a
Gemini session's heartbeat rides on pane activity alone — the exact signal the paragraph
above says not to trust. Where a harness picks its own conversation id instead of taking
ours (Codex has no `--session-id`), the reader also reports it via
`discover_session_key`, and the watcher writes it onto the row: that is the only place the
daemon can learn a key it did not assign, and without it restart-with-resume is impossible
for that harness.

Claude content UUIDs identify transcript events, while `message.id` identifies an API
call. Usage accounting keys the provider, transcript conversation and API call separately
from displayed content. Missing API IDs fall back to the event UUID. A durable per-call
record retains the maximum observed value of each counter (uncached input, output, cache
reads and cache writes); later partial/final observations append only positive increases
to the ledger. Lower, missing or repeated counters cannot recharge a call. The progress
record and ledger delta commit together, so retries, concurrent watchers and replay across
AQ session incarnations are idempotent. Attribution stays with the first recorded usage.
Accounting failures leave the byte checkpoint retryable. On adoption, consumed Claude
records supply legacy UUIDs: existing ledger rows seed per-call maxima without modifying
or deleting the original rows. Historical inflation requires a separate evidenced
correction; it is never repaired implicitly during ingest.

Historical reconciliation is read-only and uses a frozen transcript/ledger window.
Only unambiguous UUID matches with exact counter agreement and complete call coverage
qualify for a proposed compensating adjustment. Reports retain original row IDs and
hashes, deterministic correction IDs, signed category deltas and their inverse. Applying
any adjustment requires a separate command with idempotency and evidence preconditions;
the reporting tool has no apply mode. Token volumes do not imply subscription quota
percentages.

Codex rollout date folders and filenames use local time, while `session_meta` timestamps
use UTC. Keyless discovery searches the UTC launch date and adjacent dates, then accepts
only a unique match for the exact working directory and launch timestamp (−10/+60 seconds).
A known conversation key remains authoritative; discovery never follows the newest file
in a reused workspace. Adoption preserves the original launch time across daemon restarts.

On first adopting a Codex rollout, the watcher backfills its latest valid quota reading
with the original transcript timestamp, independently of the durable byte checkpoint and
historical token/output replay guard. This recovers existing rollouts after a discovery
outage without charging old tokens, replaying messages, or making old quotas look fresh.
`aq doctor --check providers.usage_activity_gap` warns when the newest provider quota
confirmation trails that provider's newest live session activity by more than 30 minutes.
With no snapshot, a live session needs 30 minutes of activity since launch before warning;
providers without quota feeds are excluded. The check is report-only.

**Peek** is `capture-pane` — for humans (`aq session peek`, dashboard) and
as the SSE fallback when no transcript is found. **Activity** from the provider is pane
activity with poke discounting (our own nudges must not look like agent progress).

**Streaming API:** `GET /api/sessions/{id}/stream` (SSE) replays transcript history then
tails it, falling back to peek diffs for transcript-less harnesses.

### 4.6 Hooks

Hook **wiring** is owned here (which events, when installed, suppression rules); payload
**content** is owned by [aq-surface](aq-surface.md). Per-harness hook file templates are written into the
work_dir (or merged via Claude's `--settings <path>`) at spec-build time when the harness
declares `supports_hooks`:

- `SessionStart` → `aq prime --hook-json` (suppressed when the bootstrap already rode argv
  on this start; active after compaction and on resume).
- `PreCompact` → `aq handoff --auto` — writes the handoff note, **no restart** (Gas City's
  gc-flp1 scar: restarting on every compaction loops).
- ~~`UserPromptSubmit` → `aq inbox --inject`~~ — **removed 2026-08-27.** The command was
  a Phase S1 stub that returned immediately, so the hook cost ~1.3 s of interpreter
  startup per prompt and delivered nothing. Messages queued mid-turn reach the agent by
  nudge as soon as it goes idle, by transcript-tail fallback, or by prime at session
  start. Reinstating prompt-boundary injection means measuring it against nudge first.
- **No Stop hook.** Completion is explicit (§4.1); a Stop hook would re-introduce
  exit-as-signal.

## 5. Providers and the Capability Model

```
class SessionProvider(ABC):
    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Cap]]     # ATTACH, PEEK, NUDGE, ACTIVITY, RELAUNCH

    async def start(spec: SessionSpec) -> SessionHandle    # detached; returns immediately
    async def stop(h, *, grace: float) -> None             # SIGTERM tree → SIGKILL → kill-session
    async def interrupt(h) -> None                         # C-c
    async def is_running(h) -> bool                        # runtime artifact present
    async def process_alive(h, process_names) -> bool      # agent process alive (≠ is_running)
    async def list_running(prefix) -> list[SessionHandle]  # adoption; PartialListError on error
    async def nudge(h, text) -> None                       # inject + submit; raises NotSubmitted
    async def peek(h, lines) -> str
    async def last_activity(h) -> float | None
    async def attach_command(h) -> str                     # "tmux -u -L aq attach -t s-<id>"
    async def set_meta(h, key, value) / get_meta(h, key)
```

Callers gate on `capabilities`, never on provider name. `is_running` and `process_alive`
are deliberately distinct: a pane can exist with a dead agent (`remain-on-exit`), and the
classifier needs both facts.

### 5.1 tmux provider (default)

One tmux server per daemon: every call is `tmux -u -L aq <sub…>`. Server health is probed
(`has-session -t =__aq_probe__`) before any create — a degraded socket makes tmux unlink
and rebind, orphaning sessions. Creation: `new-session -d -s <name> -c <work_dir> -e K=V …
'<command>'` with the agent as the pane's **initial process** (never typed into a shell),
then `window-size latest` (tmux 3.3 pins detached sessions at 80×24 otherwise),
`remain-on-exit on` (crash forensics), `mouse off`, `monitor-activity off`.

**Readiness:** poll `#{pane_current_command}` until it is not a shell, then either sleep
`ready_delay_ms` or poll `capture-pane` every 200 ms for `ready_prompt_prefix` (Claude
`❯ `, NBSP-normalized), budget `ready_delay_ms + 5 s` clamped to [5 s, 60 s]. Timeout is
non-fatal unless the pane died, in which case the pane's last output is written to
`start-stderr.log` under the daemon's session state dir and the launch fails.

**Startup dialogs:** a data-driven dismissal table (patterns + key responses from the
harness profile) run under a **shared budget** (~8 s, 500 ms poll) covering trust-folder,
theme, "Bypass Permissions mode", resume selector, MCP trust, and rate-limit dialogs (the
rate-limit dialog answers *Stop* and the session is quarantined with
`sleep_reason=rate_limit`). The budget is shared, not per-dialog — Gas City's 9×8 s
per-dialog budgets blew the start deadline.
A rule's `pattern` is a literal substring unless the row sets `"is_regex": true`;
an alternation (`A|B`) written without the flag is matched literally and never fires —
the parser warns on that shape, and the shipped harnesses flag every alternation.

**Nudge pipeline:** per-session lock → find the agent pane by `process_names` (never by
window index) → **resubmit check** (does the input line already hold *this* nudge's
marker?) → text via `send-keys -l` when ≤ 4 KB, else `load-buffer` +
`paste-buffer -p -d` (bracketed paste) → debounce (~500 ms) → `Escape` only for harnesses
that need it (per-harness `skip_escape_before_enter`; claude/codex skip) → `Enter`,
**confirmed** by busy-indicator poll on a widening backoff (four attempts, ~7 s worst
case; an ink composer under a repaint storm can take most of a second to redraw) →
still unconfirmed: the composer is cleared with the harness's `composer_clear_keys`
and `NotSubmitted` is raised for the caller to re-queue. Copy-mode is cancelled first
(a parked pane swallows keys).

**Never leave typed text behind.** The composer is the interlock for every later
nudge — `_require_empty_composer` refuses to type into a non-empty one — so text
abandoned there is not "a retry pending", it is a permanent stall with the stall
ladder frozen on its current rung. Three things prevent that, in order: the widening
Enter backoff; the resubmit check, which recognises the daemon's own marker on the
input line and presses Enter instead of deferring (this is also what recovers a
composer left dirty by a *previous* daemon process); and the per-harness clear keys.
The resubmit check fires for *any* AQ injection still in the composer exactly as
typed, not only one identical to the nudge at hand: the stall reminder names its idle
minutes, so a retry never matched the text a previous pass left, and deferring on "a
different AQ injection" froze the worker until someone typed Enter by hand. The old
injection is submitted, then the new nudge is typed into the emptied composer.
When the text still cannot be moved, `NotSubmitted` carries `composer_dirty=True`,
the reconciler logs it at **WARNING** with the session name and task id, and emits
`session.nudge_unsubmitted` — the event behind the dashboard's "message stuck" and
the `sessions.stuck_composer` doctor check. Its `--fix` presses the same Enter an
operator would send by hand. For AQ text the composer shows collapsed or windowed, it
clears the text instead (see below).

**Recognising an empty composer.** The guard accepts only layouts it knows: a bare
prompt with nothing after it, Claude's prompt between its two borders, or Codex's
dim `Ask Codex to do anything` placeholder above its footer. The Codex footer is
recognised by shape, not wording: a blank separator, the model row
(`GPT-6-Sol xhigh · <cwd>`, or `NN% context left` on older builds), then at most one
hint or notice row and padding. That row's text changed twice on 2026-09-27
(`? for shortcuts`, `← for agents · ? for shortcuts`, right-aligned
`⚠ 2 warnings · f2 to view`) and each wording the guard did not list left every idle
Codex worker unwakeable; the dim placeholder under the cursor is what proves the input
empty. A hidden terminal cursor defers everywhere except between Claude's borders:
Claude Code 2.1 can keep the cursor hidden for a pane's whole life, painting its own
inverse-video cursor cell at the input instead. Everything else defers — which also
means an unrecognised *idle* layout silently blocks delivery to that worker, so every
refusal's reason is logged and surfaced by `messages.idle_worker_backlog`. Claude's
prompt suggestions are disabled in AQ's `--settings` file
(`promptSuggestionEnabled: false`): a suggestion is ghost text that, with `NO_COLOR`,
cannot be told apart from a draft.

**Recognising typed text.** Codex and Claude wrap long input themselves, onto
explicit rows with a two-space continuation indent that `capture-pane -J` cannot
join, breaking at word boundaries. Marker and identity checks therefore compare text
with all whitespace removed: every visible character, in order, must match. A
row-by-row match read an 80-column stall reminder as never typed (so it sat in the
composer, unsubmitted) and, after Enter, a wrapped one as submitted.

**Text the composer cannot show verbatim.** Measured on Claude Code 2.1.286 in an
80x24 pool pane (2026-10-01, `clear-lantern-82`), Claude shows some input in a form
AQ cannot check. A typed burst longer than 800 characters collapses to
`[Pasted text #N]`, or to `[Pasted text #N +M lines]` where M is the number of
newlines. N counts pastes for the life of the Claude process. Input taller than the
composer's row window (seven rows at 24 lines) shows only its last rows, with `❯`
drawn on the first visible one. Neither can be confirmed as the exact AQ injection,
so neither is ever submitted. Neither is a draft either, so neither may block
delivery. The provider classifies the composer against its durable record:

- *exact*: the injection verbatim. It is submitted by the resubmit check.
- *collapsed*: exactly one placeholder and nothing else. It is attributed by the
  placeholder AQ recorded when it saw its own text collapse. A record without one
  (written before this rule, or by a daemon that died first) is attributed only when
  its text would collapse (over 800 characters) and its newline count equals M.
- *windowed*: a proper suffix of the injection that ends with its marker.
- *unknown*: anything else (a human draft, edited AQ text, a placeholder AQ cannot
  attribute). No key is sent and the record is kept.

A collapsed or windowed AQ injection is **cleared, never submitted**. The clear uses
the harness's `composer_clear_keys` and runs only while the pane is out of copy mode,
has no attached client, and has had no dashboard input for 2 s. Keys go one batch per
visible row, and the composer is re-read after each batch. Every screen in between
must still show a contiguous piece of the injection; otherwise the clear stops and
leaves the rest. Claude's `C-u` kills one visual row or one newline and does nothing
on an empty composer, so repeating it converges. Ctrl-C and Escape are never used:
Ctrl-C interrupts a turn and arms exit on an empty composer, and Escape interrupts a
turn and opens rewind when pressed twice.

Clearing removes only text the agent never saw. The message behind it stays pending
and is redelivered, delivery is acknowledged only by a confirmed submit, and nothing
is handled twice. The clear runs in three places:

1. Right after AQ typed the text. The composer was verified empty under the session
   lock a moment before, so any placeholder now showing is AQ's own. AQ records it
   durably before clearing, so a daemon restart mid-clear still attributes it
   exactly.
2. When a later nudge finds a stale record, from an earlier pass or a previous daemon
   process. The new nudge is then typed into the emptied composer.
3. From `aq doctor --check sessions.stuck_composer --fix`.

Without clear keys the text stays, is reported `unreadable`, and nudges defer as
before. The provider never shortens or rewrites the text it is given. A caller's
terminal notification is one short line that points at a durable body
(supervisor-agent §6.1).

**Kill:** pane pid → descendants (`pgrep -P` + process group) → SIGTERM, 2 s grace
(100 ms orphans Claude), SIGKILL survivors → `kill-session`. Every kill checks
`AQ_INSTANCE_TOKEN` before signaling so a name-reusing successor is never hit.

**State cache:** one `list-panes -a` + one `ps -eo pid,ppid,comm,args` per reconciler tick,
TTL 2 s. `ps` failure ⇒ optimistically alive ("never reap on a failed secondary probe");
"no server" ⇒ **unknown, not dead** — keep last-known-good, defer destructive actions.

### 5.2 subprocess provider (fallback)

For hosts without tmux: detached process group, stdout/stderr to a log file, env markers
identical. Capabilities: no ATTACH, no PEEK, no NUDGE — `nudge` raises immediately (the
stall ladder skips to restart), `peek` returns `""`. Liveness still works (process table),
transcripts still work (they are the harness's own files), so tasks complete normally via
`aq task close`. It is a degraded mode, not a parallel feature set.

### 5.3 fake provider (tests)

In-memory sessions with scriptable behavior (die after N s, become ready, swallow nudges,
report activity). All reconciler, scheduler, and cascade tests run against it; a
conformance suite pins the semantics every provider must share.

## 6. Harness Profiles

`vault/harnesses/<name>.md` (system scope; `vault/projects/<pid>/harnesses/<name>.md`
overrides per project — same precedence rule as profiles). Markdown with a JSON config
block, mirroring `docs/specs/design/profiles.md`. Fields:

`command`, `args`, `base` (inheritance from another harness), `prompt_mode` (`arg` | `flag`
| `none`), `permission_flag`, `resume` (style `flag` | `subcommand`, plus fork support),
`session_id_flag`, `ready_delay_ms`, `ready_prompt_prefix`, `process_names`,
`skip_escape_before_enter`, `supports_hooks` + hook file templates, `instructions_file`
(`CLAUDE.md` / `AGENTS.md`), `transcript_paths`, `dialogs`, `model_flag`, `effort_flag`,
`env`.

Ship `claude` first; then `codex`, `gemini`, `opencode` (replacing the `acpx` fan-out).
Agent-type profiles gain `harness: claude` plus `model`, `permission_mode`,
`codex_full_auto`, `claude_dangerously_skip_permissions`, `lifecycle`, `wake_mode`,
`max_session_age`, and `idle_timeout`. The two provider-specific permission booleans
default to `false` and may be enabled only with their matching harness. Files sync to an
in-memory registry via the existing vault watcher, following the
`src/profiles/parser.py` / `mcp_registry.py` pattern — the file is the source of truth
(principle #1).

**Seeding and upgrades.** `ensure_default_harnesses` copies
`src/sessions/default_harnesses/*.md` into `vault/harnesses/` at startup. The vault copy
is the source of truth once it exists, but a copy that is byte-identical to a version aq
once shipped (`src/sessions/harness_manifest.py` records every such sha256) is refreshed in
place so a shipped fix reaches existing installs. Any other content is an operator edit:
left alone, logged at WARNING, reported by `aq doctor --check harness.drift`, and restored
on request with `aq vault reset-harness <name>`. Changing a shipped file means adding its
new hash to the manifest; a test fails otherwise.

## 7. Surfaces

**Config** (`~/.agent-queue/config.yaml`, `sessions:` block): `enabled`, `provider`,
`tmux_socket`, `lease_ttl_seconds` (default 480), `idle_stop_grace_seconds` (default 60),
`stop_intent_report_seconds` (default 600), `stall_max_nudges`, `stall_backoff_seconds`,
`max_restarts`, `restart_window_seconds`, `restart_backoff_seconds`, `dialog_budget_seconds`,
`state_cache_ttl_seconds`, `transcript_poll_seconds`, `adopt_on_start`. Per-profile knobs
(lifecycle, wake_mode, timeouts) live in profile markdown, not config.yaml.

**CLI** (semantics here; envelope and plumbing in [aq-surface](aq-surface.md)):

| Command | Semantics |
|---|---|
| `aq session list` | Sessions with lifecycle, state, task, harness, last activity, restarts |
| `aq session peek <id> [-n N]` | Last N pane lines (empty on subprocess provider) |
| `aq session attach <id>` | Prints/execs the provider attach command |
| `aq session nudge <id> "<text>"` | Inject + submit; reports NotSubmitted |
| `aq session logs <id> [-f]` | Normalized transcript entries, follow mode tails |
| `aq session kill <id>` | Fenced kill; task goes through the exit classifier |
| `aq session drain-ack` | Agent-facing: mark own session (from `AQ_SESSION_ID`) drain-acked |

**API:** session CRUD-read endpoints via CommandHandler auto-exposure; `GET
/api/sessions/{id}/stream` (SSE) for live output.

**Events:** `session.started` / `.ready` / `.adopted` / `.exited` / `.drain_acked` /
`.sleeping` / `.recycled` / `.quarantined`; `task.stalled` / `.nudged` / `.restarted` /
`.quarantined`; transcript-sourced `notify.task_message`. All cross-component signaling
rides the EventBus (principle #7) — the dashboard subscribes to all of it and the Discord
digest to task activity alone, and the reconciler never calls them.

## 8. Failure Modes and Edge Cases

- **Dropped submit.** A nudge can paste without submitting (Enter races bracketed paste,
  and a dashboard terminal attaching or detaching resizes the pane mid-submit). Submit is
  confirmed by busy-poll on a widening backoff; `NotSubmitted` re-queues rather than
  assuming delivery, and the text is either resubmitted, cleared, or reported dirty —
  never silently abandoned in the composer (observed live 2026-09-02, task
  `stark-journey-63`: one manual `tmux send-keys Enter` cleared a nudge that had been
  stuck for hours while the log said "will retry" at info level).
- **AQ text the composer cannot show verbatim** (observed live 2026-10-01, task
  `clear-lantern-82`). A multi-line task-comment notification was typed into a Claude
  pool worker. It showed as `[Pasted text #1 +6 lines]` when over 800 characters, or
  as its last seven rows when shorter, so it was never confirmed. Every later nudge
  deferred on "input is unknown", including a supervisor's deploy-ready notice with a
  stage deadline pending, until a supervisor pressed Ctrl-C by hand. Notifications are
  now one-line pointers, and AQ clears its own collapsed or windowed text (never
  submitting it) as §5.1 "Text the composer cannot show verbatim" describes.
- **Nudging a busy agent** can interleave with its typing. Nudges are debounced, locked
  per-session, and policy (when to deliver vs. queue) belongs to [supervisor-agent](supervisor-agent.md);
  the provider only guarantees inject-and-confirm or a typed failure.
- **Readiness timeout with a live pane** is non-fatal — some harnesses paint slowly; the
  nudge/dialog machinery recovers. Only a dead pane fails the launch (with
  `start-stderr.log` evidence).
- **Rate limit at startup** (dialog) vs **mid-run** (pane text at exit): both converge on
  PAUSED(`rate_limit`) + provider cooldown; the session is not restarted into the limit.
- **`ps` failure / tmux "no server"** must never cause reaping. Unknown ≠ dead; the
  classifier acts only on positive evidence of death.
- **PID recycling and name reuse.** Kills are double-fenced: process identified via env
  markers, then instance token compared before any signal.
- **Daemon dies mid-launch:** a session may exist with no row, or a row with no session.
  Adoption reconciles both directions (orphan session → adopt if markers match a known
  task, else quarantine-kill; orphan row → exit-classify).
- **Two daemons on one host** are separated by tmux socket name and epoch; the reconciler
  refuses to adopt sessions whose `AQ_API_URL` points at a different daemon.
- **Transcript path missing** (harness changed layout, slug mismatch): watching degrades to
  peek-diff streaming and `aq task heartbeat` keeps the lease alive; a `session.transcript_missing`
  warning event fires once.
- **Quarantine is terminal-by-default:** nothing auto-releases it; `aq session kill` +
  task retry or supervisor intervention is the exit.

## 9. Interactions with Other Specs

- **[worktree-execution](worktree-execution.md)** owns worktree slots, branches, the reaper, and the merge slot.
  This spec consumes `work_dir` (the slot worktree) in SessionSpec and records it on the
  session row; the reaper's liveness guard queries this spec's process-table scan.
- **[supervisor-agent](supervisor-agent.md)** owns the `messages` table, reply protocol, and when a message
  becomes a nudge vs. an inbox injection. It consumes `nudge`, named-session wake, and the
  SSE stream.
- **[aq-surface](aq-surface.md)** owns the `aq` CLI envelope, `aq prime`/`handoff`/`inbox` content, hook
  payload formats, and REST auth. This spec owns which hooks fire and the session/task
  state transitions those commands cause.
- **[trust-and-ops](trust-and-ops.md)** owns env scrubbing rules, API token scoping, and the
  skip-permissions-inside-worktree trust argument that `permission_flag` relies on.
- **[feature-pauses](feature-pauses.md)** owns the memory/playbooks pause switches; `aq prime` keeps L1/L2
  slots empty-but-present so memory plugs back in without touching this spec.
- **[workspaces-v2](workspaces-v2.md)** remains the workspace kind/instance model; sessions attach to
  acquired workspaces, they do not change acquisition semantics.

## 10. Deferred

- Remote providers (k8s, ssh), and a structured-socket provider (Gas City's `herdr`
  direction) if pane scraping proves too brittle even in its confined role.
- Harnesses beyond claude/codex/gemini/opencode (cursor-agent, copilot, pi, …) — additive
  markdown once the first four are certified.
- `RELAUNCH` capability use (`respawn-pane -k`) for command-drift-only recycling — the ABC
  reserves the capability; v1 always does full kill + start (respawn-pane drops env).
- Warm pool workers (named sessions pre-claimed for task work) — needs [supervisor-agent](supervisor-agent.md)
  routing first.
- fsnotify transcript watching (poll ~2 s is sufficient and portable in v1).
- Windows-native provider; per-session resource limits (cgroup/systemd scopes).
