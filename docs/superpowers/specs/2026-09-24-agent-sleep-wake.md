# Agent sleep/wake and durable waits — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [exclusive job queue](2026-09-24-exclusive-job-queue.md) ·
[wake context compaction](2026-09-24-wake-context-compaction.md) ·
[managed long-running commands](2026-09-24-managed-long-running-commands.md) ·
[supervisor narrative updates](2026-09-24-supervisor-narrative-updates.md) ·
[Discord voice transcripts](2026-09-24-discord-voice-transcripts.md) ·
[realtime agent collaboration](2026-09-24-realtime-agent-collaboration.md) ·
[session desired state](2026-08-27-session-desired-state-design.md) ·
`docs/specs/design/session-runtime.md` §4.2 · `docs/specs/design/aq-surface.md` §6

## 1. The ask

"An agent that kicks off a queued job goes dormant — no heartbeats, no tokens — and is woken
with the result."

The operator's framing widens it: the job queue, hourly supervisor updates, voice
transcripts and realtime collaboration "all want the same thing underneath — agents that can
wait on something without burning tokens, and a supervisor that gets notified when it
finishes. Designing that once could cover all four." So this spec designs a **shared
primitive** — a durable *wait* that a session (or the supervisor) registers, that some other
subsystem satisfies, and that wakes its owner with a result — and the **dormancy** side:
what a waiting agent's session, task, workspace and lease look like while it waits.

Context compaction at the moment of waking is split out into
[wake context compaction](2026-09-24-wake-context-compaction.md).

## 2. What exists today

### 2.1 Where the tokens actually go while an agent "waits"

An interactive CLI sitting at its prompt makes no model calls as far as we know (not
verified per harness; Claude Code may make small housekeeping calls). Tokens are spent per
*turn*, and each turn re-sends the whole context. What forces turns during a wait today:

- **The stall ladder.** `SessionReconciler._step_stall_ladder`
  (`src/sessions/reconciler.py:1300`) treats silence past `sessions.lease_ttl_seconds`
  (480 s, `src/config.py:1312`) as a stall and nudges up to `stall_max_nudges` (3) times,
  `stall_backoff_seconds` (300 s) apart, with the text "No progress for N min … keep working
  and run `aq task heartbeat`". Every delivered nudge is a full model turn. After the rungs
  it interrupts, kills, transitions the task PAUSED (`session_stalled_restart`) and relaunches
  with `--resume` (`_carry_resume_key`, `reconciler.py:1984`) — a full context reload.
- **The heartbeat instruction.** `BOOTSTRAP_PROMPT` (`src/sessions/spec.py:81`) tells every
  task agent to run `aq task heartbeat` before any command that will be quiet for minutes,
  because the lease has "exactly two feeds — the provider's `last_activity` and an explicit
  `aq task heartbeat`". To hold a lease across a 40-minute queued run the agent has to be
  awake at least every 8 minutes. `_cmd_task_heartbeat` (`src/commands/session_commands.py:1323`)
  is two cheap DB writes; the cost is the turn around it.
- **Harness tool timeouts.** A long command blocking a Bash tool call costs nothing while
  blocked, but tool calls have ceilings, so a long wait turns into `sleep N; check` loops or
  background-and-poll — each poll a turn. (Claude Code's own background-task completion
  notification is a harness-native wake; whether our tmux `last_activity` sees the agent as
  alive while it waits on one is **unverified**.)
- **`aq test` slot waits.** `src/cli/test_runner.py` blocks on `sem.acquire(... on_wait=...)`
  up to `resources.test_wait_timeout` (default 1800 s, `test_runner.py:135`) and exits 75 on
  timeout; the agent then retries. While queued it is inside one tool call; nothing
  heartbeats the lease for it (no heartbeat call in `test_runner.py`), so whether the stall
  ladder fires depends on whether pane redraws advance tmux `window_activity`
  (`TmuxProvider.last_activity`, `src/sessions/tmux.py:583`) — unverified.
- **The backstop.** `_step_backstop` (`reconciler.py:1817`) force-kills a *task-lifecycle*
  session whose age since `started_at` exceeds `agents.stuck_timeout_seconds`, then BLOCKs
  the task. It measures age, not inactivity, so heartbeats do not help. Note the default is
  inconsistent: the dataclass says 1800 (`src/config.py:368`) but the YAML loader defaults a
  present `agents:` section to 0 (`src/config.py:4002`).
- **Cache expiry.** A wake-up turn after a long idle is likely a prompt-cache miss (provider
  cache TTLs are minutes by default — vendor behaviour, verify per provider), so it pays full
  input price on the whole context. The transcript watcher already records per-turn
  `input/output/cache_read/cache_write` into `token_ledger`
  (`src/sessions/transcripts/watcher.py:445`, `src/database/tables.py:~880`), so the real
  cost of nudge turns is measurable today (join to `task.nudged` events).

### 2.2 Existing "suspend until X, resume" machinery

| Mechanism | Where | Durable? | Shape |
|---|---|---|---|
| Playbook V2 waits | `src/playbooks/waits.py` (`WAIT_KINDS = event, timer, human, agent_task`), table `playbook_waits` (`tables.py:1953`) | yes | Inert predicate (`match` = flat dotted-path → literal), `deadline_at`, states `active/claimed/expired/cleared`. Owner is a run: `run_id` FK → `playbook_v2_runs` ON DELETE CASCADE, `snapshot_version` NOT NULL. |
| `ChildTaskReconciler` | `src/playbooks/engine.py:3651`, ticked via `reconcile_playbook_child_tasks` (`src/commands/playbook_commands.py:684`) | scans durable state | A *scan*, not a bus subscriber, because "a task reaches a terminal status from some thirty call sites … several of which emit nothing". `settled_child_waits` (`src/database/queries/playbook_run_queries.py:1402`) joins waits to `tasks`/`archived_tasks`. Idempotent: the wait is cleared in the boundary that takes the edge. |
| Agent questions | `src/sessions/questions.py` (`is_waiting` :467, `backstop_activity_at` :486) | yes (`agent_questions`) | The session **stays live, idle in tmux**; the stall ladder and backstop skip it (`_waiting_for_question`, `reconciler.py:1229`); the answer is typed in. Task stays IN_PROGRESS. This is option A below, already built for one wait kind. |
| Rate-limit sleep | `_apply_rate_limit_cooldown` (`reconciler.py:1148`) | yes | Session `state=sleeping, sleep_reason=rate_limit`; task PAUSED with `resume_after`; relaunch later with resume key. Workspace released. |
| Named-session sleep/wake | `_step_named` (`reconciler.py:1659`), `SessionLens._ensure_started_locked` (`src/messages/session_lens.py:231`) | yes (`sessions.desired_state`) | Supervisor drains to `sleeping` after profile `idle_timeout` (supervisor: 2700 s, `wake_mode: resume`); a pending message wakes it with `--resume <session_key>`. **Only supervisor-named sessions** may be woken this way — the lens refuses task sessions (`session_lens.py:235`: "spawning one from the message path would race the orchestrator"). |
| Slot-starved pause | `_resume_slot_starved_tasks` (`src/orchestrator/monitoring.py:90`) | **no** — `self._slot_starved_pauses` is in memory | Event-shaped early resume of a timer pause. |
| Message delivery | `MessageDeliveryEngine.run_delivery_pass` (`src/messages/delivery.py:74`) | yes (`messages`) | For `session`/`task` recipients: `idle` → nudge one line (`pending[:1]`), `busy` → skip, `sleeping` → `ensure_started` (supervisor only), `absent` → task messages "ride into prime" at the next start. The nudge text is a pointer: ``Handle `aq message status <id> --json`.`` (`_render_nudge`, `delivery.py:388`). |

### 2.3 Task and session state vocabulary

- `TaskStatus` (`src/models.py:21`): `PAUSED` with `resume_after IS NULL` is the **operator
  hold** sentinel fenced by `_not_manually_paused`; a timed PAUSED is promoted back to READY
  by `_resume_paused_tasks` (`monitoring.py:28`) and re-enters the scheduler — it may land on
  a different agent and workspace.
- `WAITING_INPUT` exists and is counted *active* almost everywhere (`src/agents/service.py:12`,
  `metrics_queries.py:44`, `terminal_stream.py:24`, `delivery_branches.py:92`) but **nothing
  writes it any more**: questions keep the task IN_PROGRESS, and `aq system provide-input`
  is labelled legacy (`src/cli/inventory.py:162`, `task_commands.py:5034` moves it to READY).
- Session `state` transitions are enforced (`session_queries.py:71`): `sleeping → stopped |
  quarantined` only; a wake creates a **new** session row.
- Resume is Claude-only in practice: `claude.md` declares `resume.style: flag`; `codex.md`
  and `gemini.md` declare `none` (with reasons). `_validated_resume_key`
  (`src/orchestrator/execution.py:672`) drops a key whose transcript is not under the new
  `work_dir` — so **resume requires the same workspace path** (Claude's transcript dir is
  keyed by `{work_dir_slug}`).
- `docs/specs/design/feature-pauses.md` is about switching whole subsystems off by config,
  not task pauses; it is not a precedent here.

## 3. Gaps

1. No way for a task agent to say "I am waiting on X" to the daemon. The only recognised
   wait is an agent question.
2. The stall ladder, heartbeat prompt and backstop assume every quiet agent is either
   working or stuck; there is no third state.
3. No generic durable wait. `playbook_waits` is structurally a run's wait (FK, snapshot
   version); agent questions are their own table; slot-starvation is in memory.
4. No path to wake a *task* session with a result: the lens refuses to start one, a
   re-launch goes through READY and the scheduler, and only Claude can resume.
5. No result hand-back format: where the job's output lives, how big it is, what the agent
   sees first (→ [compaction spec](2026-09-24-wake-context-compaction.md)).
6. The supervisor can be woken by a message, but nothing lets it *subscribe* ("tell me when
   task T / job J finishes") short of a playbook.

## 4. Implementation options

### Option A — Idle in the pane, exempt from the ladder, nudged with the result

Sketch: `aq wait <kind> <ref> [--deadline 2h]` registers a wait and returns; the agent ends
its turn. The session stays live at its prompt. `_waiting_for_question` generalises to
"has an active wait" for both `_step_stall_ladder` and `_step_backstop` (mirroring
`is_waiting`/`backstop_activity_at`). When satisfied, the waker writes a `messages` row
(`body_kind="wait_result"`, to the task) and the existing delivery engine nudges the idle
session with the pointer.

- Touches: `reconciler.py` (two exemptions), `questions.py` pattern, a small waits table or
  task metadata, `delivery.py` (new body kind), `BOOTSTRAP_PROMPT`/prime guidance, a new
  `aq wait` command + capability grant in shipped profiles.
- Pros: small; reuses a proven pattern; works for all three harnesses (nudge only needs
  `Cap.NUDGE`); resume-free; wake latency ≈ one delivery cycle (5 s).
- Cons: holds a tmux process, memory, a worktree slot, an agent and a pool seat for the
  whole wait — dormancy in tokens only, not in resources. One cache-miss turn on wake.
  A deadline must still exist or a lost satisfier strands the session forever.
- Size: **M**.

### Option B — End the session, park the task, relaunch on wake

Sketch: on `aq wait --sleep`, the daemon stops the session with `desired_state=sleeping`,
carries the resume key, and parks the task in a waiting status that keeps its workspace lock
and claim. On satisfaction the orchestrator relaunches *in place* (same workspace, same
agent) with `--resume` where supported, fresh + hand-off note otherwise.

- Touches: task status/state machine (new `WAITING` or repurposed `WAITING_INPUT`), every
  status set listed in §2.3, `_launch_session_for_task`/scheduler (a resume-in-place path that
  bypasses READY priority competition), workspace release rules (must *not* release),
  `_step_orphans` (a sleeping session with an open task must not be treated as orphaned),
  pool lifecycle (`reconciler.py:392` tears down a sleeping pool session only when it holds
  no task — held-task sleep is undefined), dashboard/graph status rendering.
- Pros: real dormancy — no process, frees agent capacity (the durable worker could even take
  other work if the workspace is not needed), survives daemon restarts naturally.
- Cons: large; resume is Claude-only; resume reloads the full transcript (the "huge context"
  problem); a slept task that keeps its worktree still holds a slot; relaunch can fail
  (provider down, harness gone) and needs the whole failure ladder.
- Size: **L–XL**.

### Option C — A generic durable wait record + one wake reconciler (the substrate)

Sketch: a new table (working name `waits`, or `agent_waits`) that any subsystem can satisfy
and any owner can hold:

```
wait_id, owner_kind (task | session | supervisor | playbook_run?), owner_id, project_id,
kind (job | task | message | timer | event | human), match JSONB (inert, as waits.py),
deadline_at, state (active | satisfied | delivered | expired | cancelled),
result_ref (pointer: job id / file / message id), result_digest (bounded text),
wake_policy (nudge | resume | fresh | notify_only), dormancy (idle | sleep),
created_at, satisfied_at, delivered_at
```

A `WaitReconciler` cascade step, modelled on `ChildTaskReconciler`, **scans durable state**
(jobs table, tasks, messages, timers) rather than subscribing to the bus, marks waits
satisfied, then delivers each by `wake_policy` — via the message engine for a live or
supervisor owner (A), or via a task-lifecycle relaunch for a slept task (B). Producers may
also write satisfaction directly (`satisfy_wait(wait_id, result_ref, digest)`) for sources
that have no scannable state.

- Touches: `tables.py` + an alembic revision, a `waits_queries.py`, a reconciler ticked in
  `Orchestrator.run_one_cycle` near step 12a, command(s) `wait_register|wait_list|wait_cancel`
  (auto-exposed via CommandHandler/MCP/CLI), plus A and/or B as delivery modes.
- Pros: one primitive for all consumers (§5.1); restart-safe; retrofits the one-off waits
  (questions, slot starvation, rate-limit resume) over time; observable (`aq wait list`,
  dashboard).
- Cons: overlaps `playbook_waits` — two wait tables unless we generalise the playbook one
  (its FK + `snapshot_version` make that a migration of a hot table). Scanning adds
  per-cycle queries per kind.
- Size: **L** for the substrate + Option A delivery; B on top is another **L**.

### Option D — Harness-native waiting only

Sketch: teach agents to use the harness's own background-task + completion notification
(Claude Code `run_in_background`), and only fix the ladder so it recognises "harness is
waiting on a background process" (e.g. via process table: `src/sessions/proctable.py`, or a
hook).

- Pros: nearly free to build; no daemon wake path.
- Cons: Claude-specific (unverified for Codex/Gemini); the result is whatever the process
  printed; the daemon cannot see or enforce the wait; nothing for the supervisor or
  cross-agent consumers; still holds all resources.
- Size: **S**, but covers only one of four consumers.

## 5. Initial take (provisional)

Build **Option C with Option A as the first delivery mode**, and defer Option B.

- The operator's point is that four features want one mechanism; C is that mechanism, and
  `ChildTaskReconciler` already proved "scan durable state, idempotent resume" in this
  codebase.
- A is where most of the *token* waste goes away (no nudges, no heartbeat polling, no
  stall restarts) for a modest change, and it already exists for questions.
- B is where the *resource* win is, but it drags in a task-status change, the scheduler,
  pools and the Claude-only resume constraint. Revisit once A has shipped and we have
  `token_ledger` numbers on how often waits exceed, say, 30 minutes. B's wake-up content is
  the [compaction spec](2026-09-24-wake-context-compaction.md)'s problem either way.
- Keep the task IN_PROGRESS during an A-wait (as questions do) — do not revive
  `WAITING_INPUT` until B needs a distinct status.
- Do **not** generalise `playbook_waits` in the first cut; borrow its inert-predicate format
  (`WaitSpec.match`, `correlation_key`) so a later merge is mechanical.

### 5.1 Consumers

| Consumer | Wait kind / owner | What satisfies it | Delivery |
|---|---|---|---|
| [Exclusive job queue](2026-09-24-exclusive-job-queue.md) (and `aq test` slot waits) | `job`, owner = task | job row reaches terminal state | nudge task session with `result_ref` + digest; the job submit command could register the wait implicitly |
| [Managed long-running commands](2026-09-24-managed-long-running-commands.md) | `job` (same) | managed process exits | same |
| [Supervisor narrative updates](2026-09-24-supervisor-narrative-updates.md) | `timer` (hourly) and `task`/`event` subscriptions, owner = supervisor | deadline / task settled | message to `supervisor-<pid>`; the lens already wakes a sleeping supervisor with `--resume` |
| [Discord voice transcripts](2026-09-24-discord-voice-transcripts.md) | `job` (transcription), owner = supervisor or the task the drop created | transcription finishes | message with transcript pointer + digest |
| [Realtime agent collaboration](2026-09-24-realtime-agent-collaboration.md) | `message` (thread correlation) or `task`, owner = task | peer replies / peer task settles | nudge; this is the one consumer where wake latency matters |
| Existing one-offs (later) | `human` (agent questions), `timer` (rate-limit resume), slot availability | as today | retrofit, not required for v1 |

The supervisor half ("a supervisor that gets notified when it finishes") falls out of
`owner_kind = supervisor` + `wake_policy = notify_only`: the reconciler writes a message and
the existing delivery engine does the rest.

## 6. Open questions

1. **Who registers the wait — the agent or the producer?** An explicit `aq wait` is
   visible and teachable; implicit registration on `aq job submit` removes a step the agent
   can forget. Decides the agent surface and whether job submit needs a task scope.
2. **Does a waiting task keep its workspace lock and slot?** Resume needs the same
   `work_dir` (`_validated_resume_key`); releasing frees a slot but forces fresh start +
   checkpoint (the failover pattern). Decides whether B can ever free a slot.
3. **Task status during a wait: stay IN_PROGRESS, revive `WAITING_INPUT`, or add
   `WAITING`?** Decides how many status sets change and how dashboard/digest/metrics show a
   waiting task (the digest already excludes "a worker parked on a human answer" from
   active).
4. **Deadline semantics.** Is a deadline mandatory? What happens at expiry — wake with
   `expired`, fail the task, escalate? A lost satisfier must not strand a session or a slot
   forever. Decides the failure contract.
5. **How is an A-wait distinguished from a stuck agent that *claims* to be waiting?**
   Should the ladder re-check the satisfier's liveness (job still queued/running) rather than
   trust the wait row? Decides whether stall exemption can be abused/drift.
6. **What happens if the session dies mid-wait** (crash, daemon restart, provider outage)?
   Does the wait survive and wake a relaunched session, and does `_step_exits` treat a
   waiting session's death as productive or a failure?
7. **Wake policy per harness.** With `resume.style: none` for Codex and Gemini, is B ever
   worth it for them, or do they always go fresh + hand-off? Decides whether B is
   Claude-only.
8. **Pool sessions.** A `lifecycle: pool` worker holding a task: may it sleep? Today a
   sleeping pool session with a held task has no defined path (`reconciler.py:392`).
   Decides whether B covers swarm mode.
9. **One wait table or two?** Merge with `playbook_waits` now, later, or never? Decides
   whether playbook `wait` steps and agent waits share the reconciler.
10. **Scan vs. push for satisfaction.** Scanning is restart-safe but costs a query per kind
    per cycle; direct `satisfy_wait` is cheap but loses events on crash. Probably both
    (scan as backstop) — confirm.
11. **Multiple concurrent waits per task / wait-any vs. wait-all?** Decides the match
    model (single predicate vs. a set).
12. **Measurement first?** Should step 0 be a `token_ledger` query attributing turns to
    `task.nudged` / heartbeat-only turns, to size the win before building? Also verify
    whether tmux `window_activity` advances during a long blocked tool call.
13. **Fix the `stuck_timeout_seconds` default mismatch** (1800 vs 0) as part of this, or
    separately? A waiting session interacts with it directly.

## 7. Dependencies and sequencing

1. Measure (Q12) — no code, one query and a live check of pane activity during a blocked
   tool call.
2. Wait table + `WaitReconciler` + `aq wait` + ladder/backstop exemption + nudge delivery
   (C + A). Needs a migration (operator runs `aq db upgrade`) and a profile grant; existing
   vault profiles need `aq agent profile-reseed --grants-only` (see `CLAUDE.md` "Upgrading an
   existing install").
3. First producer: [exclusive job queue](2026-09-24-exclusive-job-queue.md) (can land in
   parallel; the wait kind `job` just needs its table to scan).
4. Supervisor subscriptions (`owner_kind=supervisor`) → unblocks
   [narrative updates](2026-09-24-supervisor-narrative-updates.md) and
   [voice transcripts](2026-09-24-discord-voice-transcripts.md).
5. [Wake context compaction](2026-09-24-wake-context-compaction.md) result digests — needed
   before any large result is delivered, even in mode A.
6. Option B (sleep tier), gated on measured long-wait frequency and on the compaction spec.

## 8. Non-goals

- Making idle CLIs cheaper in themselves (they already spend nothing between turns).
- Replacing the stall ladder: it stays the answer for genuinely stuck agents.
- Cross-daemon or cross-host waits.
- Codex/Gemini resume support (tracked in their harness files; a dependency of B, not of
  this spec).
- A general event-subscription language for agents; `match` stays a flat inert predicate.
