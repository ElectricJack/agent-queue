# Managed long-running commands — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [exclusive job queue](2026-09-24-exclusive-job-queue.md) ·
[agent sleep/wake](2026-09-24-agent-sleep-wake.md) ·
[wake context compaction](2026-09-24-wake-context-compaction.md) ·
[smart test selection](2026-09-24-smart-test-selection.md) ·
[resource-aware planner](2026-09-24-resource-aware-planner.md) ·
existing: [pane console stream](2026-08-22-pane-console-stream-design.md),
[live pane streaming](2026-08-25-live-pane-streaming-design.md),
[resource gating guide](../../guides/resource-gating.md),
[development integration guide](../../guides/development-integration.md)

## 1. The ask

"Managed long-running bash: output preserved and fed back on wake."

An agent should be able to hand a long command — a test run, a build, a
benchmark — to Agent Queue instead of holding it inside its own harness Bash
call. AQ runs it, captures the output durably, and when the agent is next
woken (or is sitting idle waiting) it is handed the result: exit code, a
duration, and the output trimmed so the model sees the part that matters
(failures first, then the tail), with the full log one command away.

The underlying pain: a full `aq test` run is ~20 minutes plus slot queueing;
harness Bash tools are not built for that, and when the session is stopped,
restarted or put to sleep in the meantime, the run and its output are lost.

## 2. What exists today

**Harness Bash tools (external, not in this repo — unverified here).** Claude
Code's Bash tool has a default timeout of 2 min and a configurable maximum
(10 min by default, raised via `BASH_MAX_TIMEOUT_MS`); it also has a
`run_in_background` mode whose output lives in the harness process and whose
completion is reported back into the *same* conversation. Codex and Gemini
equivalents have not been checked. Both modes die with the harness: every
harness Bash call runs under `setsid`, and AQ's session stop deliberately
sweeps whatever it left behind (below).

**`aq test`** — `src/cli/test_runner.py`. `test_command` (line 692) takes the
full-suite lock when `_is_full_suite` (line 395, ≥ `_FULL_SUITE_SHARE = 0.5` of
modules) says so, then one `flock` slot from `SlotSemaphore`
(`src/resources/semaphore.py`, lock dir `<data_dir>/locks/test-slots`,
`default_lock_dir` line 58). It mints `AQ_TEST_RUN_ID` (`_new_test_run_id`,
line 79), runs pytest via `_run_forwarding_signals` (line 464, forwards
SIGINT/SIGTERM/SIGHUP, keeps the flock fd inheritable), and turns exit 5 into a
visible failure. Exit 75 = no slot (EX_TEMPFAIL). Output goes straight to the
caller's terminal — nothing is stored.

**Slot attribution and the orphan reaper** — `src/resources/test_runs.py`.
`holder_identity` records pid, task id (from env or `.aq/claim.json`), session
id/name, `session_root` (topmost ancestor carrying the same
`AQ_INSTANCE_TOKEN`), a token digest and `test_run_id`. `held_slots` classifies
holders `live` / `orphaned` / `unattributed`; `reap_orphans` kills orphaned runs
by `AQ_TEST_RUN_ID` through `kill_marked_sync`. **An `unattributed` holder (no
session marker) is reported, never reaped.**

**Session sweep** — `src/sessions/proctable.py`. `kill_marked` (line 316) kills
every process carrying the session's `AQ_INSTANCE_TOKEN`; it is called from
`TmuxProvider.stop` (`src/sessions/tmux.py:511`) and the subprocess provider's
stop (`src/sessions/subprocess.py:173`). This was added because a detached
full-suite run outlived its session and held a slot for over an hour
(2026-09-24). Consequence for this feature: *anything a session starts that
carries its token dies when the session stops* — which is exactly wrong for a
run that should outlive a sleep.

**Load attribution** — `src/resources/procs.py` walks `/proc`, attributes
processes by `AQ_TASK_ID` / `AQ_SESSION_NAME` in environ, else by worktree slot
in cwd.

**A daemon-side command runner with classified output already exists** —
`src/integration/development_validation.py::run_check` (line 207). The
development publisher uses it for "selected validation": `bash -c` in its own
session (process group), stdout+stderr merged, a 256 KiB in-memory parse tail,
budgets that exclude slot-queue time (read from the `AQ_TEST_SLOT_REPORT` file,
`src/resources/slot_report.py`), and a classified result: `passed` / `failed` /
`infrastructure` with `infra_reason`, `failing_tests` (from
`parse_pytest_output`, line 122), pytest `summary` counts, and `output` = the
last `OUTPUT_TAIL_CHARS = 8000` characters. That is very close to the result
envelope this feature needs; it is just not durable (evidence is a JSON blob on
a `development_deliveries` row) and not available to agents.

**A daemon-side streamable command registry also exists** —
`src/api/streams.py` (`aq stream start|tail|kill`, `src/cli/streams.py`).
`StreamRegistry` (line 139) is **in memory** ("Not a `tables.py` row: a stream is
short-lived and its output can be large"), ring-buffered
(`buffer_max_lines=5000`, `buffer_max_bytes=2 MiB`), SSE with `ConsoleFrame`
seq/replay, `_validate_cwd` (line 232: within any workspace/repo), per-session
concurrency cap (`streams.max_concurrent_per_session = 3`), retention sweep
(`streams.retention_seconds = 300`, `StreamsConfig` in `src/config.py:1444`).
Starting requires `scope.kind == "local"` or an elevated scope (`_can_start`,
line 255) — **a worker session cannot start one**. `_spawn_and_pump` spawns with
the **daemon's** environment and pipes; a daemon restart loses every stream.

**Waking and delivering to an idle agent** — `src/messages/delivery.py`
(`MessageDeliveryEngine`) asks `SessionLens.activity` for
`idle|busy|sleeping|absent`: `idle` → nudge (one message, one line —
`_render_nudge` sends a pointer like ``Handle `aq message status <id> --json`.``
because Codex re-wraps long input), `sleeping` → `ensure_started` (supervisor
only today), `absent` → task recipients ride into `aq prime` at next start.
`src/sessions/questions.py` is the other precedent: a durable, CAS-fenced
answer delivered to a waiting session with a persisted delivery lease.

**Stall ladder** — `SessionReconciler._step_stall_ladder`
(`src/sessions/reconciler.py:1300`): a running task session whose
`last_activity` is older than `sessions.lease_ttl_seconds` (default 480,
`src/config.py:1312`) is nudged up to `stall_max_nudges=3` times with
`stall_backoff_seconds=300`, then restarted. `last_activity` is advanced by the
transcript watcher (`src/sessions/transcripts/watcher.py`), the claim path and
`aq task heartbeat` (`session_commands.py:1323`). Sessions waiting on an agent
question are exempt (`_waiting_for_question`, line 1229). **Whether a
foreground 20-minute Bash call writes transcript activity while it runs is
unverified**; if it does not, the ladder nudges an agent that is legitimately
waiting.

**Data dir layout (relevant parts)** — `<data_dir>/sessions/<name>/` (session
state, `out.log` for the subprocess provider, `start-stderr.log`),
`<data_dir>/logs/agent-queue.log` (rotated, `logging.log_file_max_bytes` 50 MB ×
5), `<data_dir>/logs/llm/` (30-day `llm_logging.retention_days`),
`<data_dir>/locks/test-slots/`, `<data_dir>/development-integration/slot-reports/`,
`<data_dir>/backups/branch-deletions/`. There is no per-command output store.

**Env hygiene** — `src/sessions/env.py::build_session_env` + `src/env_scrub.py`
give sessions scrubbed env plus the nine `AQ_*` markers, `AQ_DB_SCOPE=worker`,
sentinel DB URLs, and per-session caps (`src/resources/limits.py::
session_env_caps`, `PYTEST_XDIST_AUTO_NUM_WORKERS`, thread caps, `nice`, optional
cgroup scope via `wrap_session_argv`).

## 3. Gaps

1. No durable record of "a command an agent asked AQ to run": no id, no owner,
   no output file, no exit code that survives a daemon or session restart.
2. The two daemon-side runners (`run_check`, `StreamRegistry`) are each half of
   it: one classifies but is private to the publisher; the other streams but is
   in-memory, operator-only and runs with the daemon's environment.
3. Nothing delivers a finished result back to the owning agent. The message
   substrate can, but no producer creates such a message.
4. Lifetime is tied to the wrong thing: session-started processes die with the
   session (`kill_marked`), while daemon-started ones have no owner at all.
   What we want is "lives as long as the *task* (or an explicit TTL), not the
   session".
5. The stall ladder cannot tell "waiting on my managed run" from "stuck".
6. Slot attribution: a run with no `AQ_INSTANCE_TOKEN` is `unattributed` and
   never reaped, so a daemon-owned `aq test` would be invisible to
   `aq test --aq-reap-orphans` unless attribution learns a run marker.
7. No model-oriented trimming: 8000 chars of tail misses a collection error at
   the top and wastes space on passing-test noise.

## 4. Implementation options

### Option A — `aq run -- <cmd>`: a daemon-managed runner with a durable row

Sketch. New command contract `run_start` (auto-exposed to CLI/MCP), plus
`run_status`, `run_result`, `run_output` (byte range / grep), `run_cancel`,
`run_list`. `aq run [--title T] [--timeout 3600] [--notify nudge|prime|none]
-- <cmd…>` returns immediately with `run_id`. The daemon:

- inserts a `command_runs` row (id, task_id, session_id, project_id, principal,
  argv, cwd, env digest, state `queued|running|exited|killed|lost`, exit_code,
  signal, started/ended, output_path, output_bytes, truncated, result JSON);
- spawns `bash -c` **detached** (`start_new_session`), stdout/stderr redirected
  to `<data_dir>/runs/<run_id>/output.log` (a file, not a pipe — so a daemon
  restart does not SIGPIPE it; the subprocess provider already does this), a
  tiny wrapper writes `exit_code` next to it, env carries `AQ_RUN_ID`;
- on exit, builds the result envelope (reusing `parse_pytest_output` /
  `classify` from `development_validation.py`), then files a message
  `to_kind=task` so `MessageDeliveryEngine` nudges an idle session or it rides
  into prime.

Touches: new `src/runs/` (runner, trimming, adoption), `tables.py` + alembic
revision, `src/commands/` mixin + contract, `src/cli/` (auto-derived mostly),
`src/prime/sections.py` ("## Finished runs"), `test_runs.holder_identity`
(recognise `AQ_RUN_ID` → owning task), reconciler stall carve-out, doctor check
for disk use and lost runs, profile grants (`run_*`) + reseed notes.

Pros: survives session stop and sleep by construction; one owner model; the
dashboard can stream the file; restart adoption by env marker is the same
technique session adoption already uses (`scan_by_env_marker`).
Cons: the daemon becomes a process parent for arbitrary agent commands (see
security §6); duplicating what the job queue will need anyway if built
separately. **Size: L.**

### Option B — the job queue owns execution; this spec is its output/transport contract

Sketch. The [exclusive job queue](2026-09-24-exclusive-job-queue.md) decides
*when* and *where* a job runs (slots, exclusivity, priority). This spec
defines only: the job's output store (path, caps, retention), the result
envelope (§5 trimming), the delivery rule (message to the task; nudge/prime),
the stall-ladder carve-out, and the dashboard stream. `aq run` becomes a thin
"submit a job of kind `shell`" and `aq test --aq-detach` "submit a job of kind
`pytest`".

Touches: the same result/delivery pieces as A; execution lives in the job
queue's worker.
Pros: one executor, one queue, one lifetime policy; `aq test`'s slot semaphore
can eventually be re-expressed as job-queue capacity rather than living beside
it. Cons: blocks on the job queue design; risk of two specs each assuming the
other owns a piece (process parenting, restart adoption). **Size: M** for this
part on top of the queue.

### Option C — tmux side-window per command

Sketch. `aq run` opens a new window in the agent's own tmux session
(`new-window`), runs the command there with `pipe-pane` (or `tee`) into
`<data_dir>/runs/<id>/output.log`, and a wrapper writes the exit code. The
daemon watches the file for completion.

Pros: a human attaching to the session sees the run live; env and cwd are
exactly the agent's; no daemon process-parenting; cheap to build.
Cons: dies with the session — it carries `AQ_INSTANCE_TOKEN` and lives in the
session's tmux session, so stop/sleep kills it unless we special-case both;
no equivalent for the `subprocess` provider (the unconfigured default,
`SessionsConfig.provider`); `capture-pane`/`pipe-pane` output includes terminal
control sequences. **Size: M**, but does not meet "survives sleep".

### Option D — client-side detached wrapper (no daemon execution)

Sketch. `aq run` forks a `setsid` child from the agent's shell, strips
`AQ_INSTANCE_TOKEN` (so `kill_marked` spares it) and sets `AQ_RUN_ID`; the
child writes output to the data dir and POSTs the result to the API on exit.
Pros: smallest change; runs with the session's scrubbed env. Cons: it is the
exact "detached shell reparented to init" shape the 2026-09-24 incident was
about — now deliberately unowned; the reaper must learn a new owner rule;
nothing restarts or cancels it if the POST fails. **Size: S–M.** Plausible as
a stepping stone only.

## 5. Initial take

*Provisional.* Build **the contract first (Option B's scope) and a minimal
executor behind it (Option A's shape)**, so the job queue can take over
execution later without changing what agents see:

- **Result envelope** (one JSON, reusing `run_check`'s keys): `run_id`,
  `command`, `cwd`, `exit_code`, `signal`, `outcome`
  (`passed|failed|infrastructure|cancelled|lost`), `infra_reason`,
  `duration_seconds`, `slot_wait_seconds`, `summary` (pytest counts when
  present), `failing_tests[]` (id + reason, capped at `MAX_FAILING_TESTS=100`),
  `excerpt` (trimmed text, below), `output_bytes`, `truncated`, `output_ref`.
- **Trimming for the model** (target ~6–8 KB, configurable): (1) a one-line
  header (outcome, exit, duration, counts); (2) for pytest, each failing test's
  traceback block from the `FAILURES`/`ERRORS` sections, longest first
  truncated, until half the budget; (3) the first error-looking lines from the
  head (collection errors, `ImportError`, command-not-found) — 10–20 lines;
  (4) the tail for the remainder; (5) a footer: "full log: `aq run output
  <id> [--grep …] [--range …]`". Non-pytest commands get head+tail only.
- **Storage**: `<data_dir>/runs/<run_id>/{output.log,exit_code,result.json}`;
  per-run cap (e.g. 64 MiB — keep the first 1 MiB and a rolling tail beyond
  that); retention by age (e.g. 14 days, mirroring the `expired` branch
  window) and by total size, swept by a doctor check with `--fix`.
- **Env**: the run gets a session-equivalent environment built through
  `build_session_env` + `session_env_caps` (never the daemon's), keeps
  `AQ_DB_SCOPE=worker`, gets `AQ_RUN_ID` and `AQ_TASK_ID`, **not** the
  session's `AQ_INSTANCE_TOKEN`. cwd must resolve inside the task's own
  workspace (`AQ_WORK_DIR`), which is stricter than `streams._validate_cwd`.
- **Lifetime**: bound to the task, not the session — cancelled when the task
  reaches a terminal status or is re-claimed by another agent; otherwise runs
  to its timeout.
- **Delivery**: on completion, a message `to_kind=task` with a short nudge
  line ("run r-… exited 1 after 18m: `aq run result r-…`") plus a prime
  section listing unread results. The sleep/wake spec decides whether a
  finished run *wakes* a sleeping agent.
- **Stall carve-out**: a task session with a `running` managed run is treated
  like `_waiting_for_question` — no stall rung while it waits, bounded by the
  run's own timeout.

Start with pytest as the only first-class classifier and treat everything
else as opaque shell.

## 6. Open questions

1. **Who owns execution — this feature or the job queue?** Decides whether
   §5's minimal executor is built at all, and who writes the process-parenting
   and restart-adoption code once.
2. **What does the process run as and with which environment?** The daemon's
   env contains the real DB URL and provider keys; `streams._spawn_and_pump`
   already inherits it. Options: rebuild the session env from the session row
   (needs the spec builder at run time), or have the CLI send its own env and
   scrub it server-side. Decides the security posture of the whole feature.
3. **Is a new grant (`run_start`) needed, or is the argument "workers already
   have arbitrary Bash" sufficient?** A daemon-parented process is different in
   one respect: it outlives the session. Should a profile opt in?
4. **Lifetime rule.** Task terminal → kill is clear. What about a task that is
   PAUSED, re-claimed by another worker, or failed over to another provider
   (`src/orchestrator/provider_failover.py`) — does the run's result go to the
   new session?
5. **Does a finished run wake a sleeping agent, or only annotate its next
   wake?** Depends on the [agent sleep/wake](2026-09-24-agent-sleep-wake.md)
   design; affects cost (a wake is a model turn).
6. **Slot interplay.** A managed `aq test` still takes a slot from inside the
   run; its holder record has no `session_root`. Extend `holder_identity` /
   `_classify` with an `AQ_RUN_ID` → `command_runs` owner lookup, or let the
   runner take the slot itself (job-queue territory)?
7. **Does a managed run count as activity for the stall ladder, or as an
   explicit exemption?** Exemption is simpler but lets a hung command pin a
   session; activity-from-output-growth is more honest but noisier.
8. **Restart semantics.** On daemon restart: adopt still-running processes by
   `AQ_RUN_ID` (as sessions are adopted), mark rows whose process is gone and
   whose `exit_code` file is missing as `lost`, and deliver `lost` as an
   `infrastructure` outcome? Confirm `lost` should never be retried
   automatically.
9. **Trimming budget and shape.** Is ~8 KB right for every harness? Should the
   trimmed excerpt be cached in `result.json` (stable across reads) or computed
   per read (can adapt to the reader's budget — see
   [wake context compaction](2026-09-24-wake-context-compaction.md))?
10. **Dashboard streaming.** Reuse `ConsoleFrame` SSE by tailing the file
    (cheap, one reader per watched run like `PaneBroadcaster`), or push frames
    over `/ws`? Should the existing in-memory `StreamRegistry` be re-based on
    the durable store and retired?
11. **Concurrency caps.** Per session (streams uses 3), per task, box-wide?
    Or entirely the job queue's call?
12. **Should `aq test` detach by default when stdout is not a TTY and the run
    is full-suite?** Tempting, but it changes a command the publisher's
    `run_check` depends on synchronously.
13. **Codex/Gemini.** Do their shell tools have usable background modes, and
    do we need this feature more for them than for Claude Code? Unverified.

## 7. Dependencies and sequencing

- **Before:** decide Q1 with the [exclusive job queue](2026-09-24-exclusive-job-queue.md)
  author; decide Q5 with [agent sleep/wake](2026-09-24-agent-sleep-wake.md).
- **Step 1 (independent, S):** extract `run_check`'s classification and a new
  trimming function into a shared module (`src/runs/result.py` or similar) with
  unit tests against recorded pytest outputs; the publisher keeps using it.
- **Step 2 (M):** `command_runs` table + output store + retention/doctor check +
  `run_*` commands, executor minimal or delegated.
- **Step 3 (S–M):** delivery (message + prime section) and the stall carve-out.
- **Step 4 (S):** `aq test --aq-detach` sugar; slot attribution by `AQ_RUN_ID`.
- **Step 5 (M):** dashboard view (task panel "Runs", live tail), possibly
  folding `StreamRegistry` onto the durable store.
- Feeds [smart test selection](2026-09-24-smart-test-selection.md) (a selected
  run is a natural managed run) and the
  [resource-aware planner](2026-09-24-resource-aware-planner.md) (run durations
  become planning data).

## 8. Non-goals

- A general CI system or remote execution; runs stay on the AQ box.
- Interactive commands (stdin); runs get `/dev/null`.
- Replacing the harness Bash tool for short commands.
- Changing `aq test`'s synchronous default or its slot semantics in this spec.
- Letting a worker run commands outside its own task workspace.
