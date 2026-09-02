---
tags: [design, sessions, runtime, tmux, harnesses, lifecycle]
---

# Session Runtime — tmux-First Provider Model

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #7 events not coupling, #9 simple interfaces, #10 fewer moving parts)
**Related:** [[worktree-execution]], [[supervisor-agent]], [[aq-surface]], [[trust-and-ops]], [[feature-pauses]], [[workspaces-v2]], [[specs/orchestrator]], `docs/analysis/framework-overhaul-todo.md` (Workstream A, D1)

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
- Worktree lifecycle (owned by [[worktree-execution]]), message routing and nudge-delivery
  policy (owned by [[supervisor-agent]]), the CLI envelope and hook payload content (owned
  by [[aq-surface]]), env scrubbing and trust boundaries (owned by [[trust-and-ops]]).

## 3. Concepts

| Concept | Definition |
|---|---|
| **Session** | One OS-level agent run: a tmux session (or subprocess) whose initial process is the harness CLI. Persisted as a `sessions` row. |
| **SessionProvider** | Pluggable backend that creates/observes/kills sessions. Registry: `tmux` (default), `subprocess` (fallback), `fake` (tests). |
| **SessionSpec** | Immutable launch description built from profile + harness + task: name, work_dir, argv, env, prompt, readiness hints, dialogs, lifecycle. |
| **Harness** | A markdown profile in `vault/harnesses/<name>.md` describing one CLI agent: command, prompt delivery, resume flags, readiness prompt, process names, hooks, transcript paths, dialogs. |
| **Lifecycle** | `task` — one session per task, killed after drain-ack; `named` — persistent (supervisor, warm workers), sleeps and wakes. |
| **Session name** | Task sessions `s-<task_id>`; named sessions `n-<profile>[--<project>]`. Charset `^[a-zA-Z0-9_-]+$` (profile/project ids are sanitized into it). Consumers address named sessions by a *logical name* (e.g. `supervisor-<project_id>`, see [[supervisor-agent]]); the session manager maps logical names to provider names — other specs never construct provider names directly. |
| **Epoch / instance token** | `AQ_DAEMON_EPOCH` identifies the daemon run that launched a session; `AQ_INSTANCE_TOKEN` uniquely fences one launch so kills never hit a same-named successor. |

**Identity and liveness.** Every session's environment carries `AQ_SESSION_ID`,
`AQ_TASK_ID`, `AQ_PROJECT_ID`, `AQ_PROFILE`, `AQ_DAEMON_EPOCH`, `AQ_INSTANCE_TOKEN`,
`AQ_WORK_DIR`, `AQ_API_URL`, `AQ_API_TOKEN`; `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` are
stripped (today's `isolated_env` in `src/runtimes/_subprocess.py`). Liveness and adoption
are decided by scanning the process table for these markers (`/proc/<pid>/environ`) — never
PID files, never tmux session names alone (names get reused; PIDs get recycled). The
`AQ_API_URL`/`AQ_API_TOKEN` pair is how `aq` inside the session reaches the daemon; token
scoping is specified in [[trust-and-ops]].

## 4. Behavioral Model

### 4.1 Task sessions

The harness runs as a **full interactive CLI** in the pane — `claude "<bootstrap>"` with
the prompt as a positional argument, never `claude -p` print mode. The TUI stays attachable
and human-readable; prompts larger than ~1 KB are written to a temp file and delivered via
`sh -c '… exec <cmd> "$__aq_prompt"'` (tmux's `new-session` buffer is ~2 KB).

The bootstrap prompt is deliberately short: *"You are running task `<id>` in `<work_dir>`.
Run `aq prime` and follow it. When done: `aq task close …` then `aq session drain-ack`."*
The full prompt (role, project override, task, attachments, workspaces block — L1/L2 slots
reserved while memory is paused per [[feature-pauses]]) renders to `<work_dir>/.aq/prompt.md`
and is returned by `aq prime` (envelope: [[aq-surface]]). `work_dir` is the task's slot
worktree, prepared before launch per [[worktree-execution]].

**Completion is explicit, and only explicit:**

1. Agent runs `aq task close <id> --outcome … --work-outcome … [--commit …] [--notes …]`
   (outcome metadata schema owned by the work-graph substrate). The daemon runs the
   completion pipeline (commit/push/PR/verify — `_run_completion_pipeline`,
   `src/orchestrator/git_ops.py`) and transitions the task.
2. Agent runs `aq session drain-ack`. The reconciler sees the ack, kills the session
   (instance-token-fenced), and marks the row `stopped`.

**Process exit with the task still IN_PROGRESS is a failure signal**, routed through the
exit classifier:

| Evidence | Verdict |
|---|---|
| Rate-limit text in the final pane capture | Task → PAUSED (`rate_limit`) with provider cooldown; session `sleep_reason=rate_limit` |
| Rapid crash (death within `restart_window` of start) | Restart with backoff, `--resume <session_key>` when the harness supports it; after `max_restarts` inside `restart_window` → quarantine |
| Task already closed, session lingering | Normal drain path (kill, `stopped`) |
| Productive death (ran long, exited, task open) | `needs_attention` / re-queue per retry policy — never silently READY |

Restart counters (`restarts`) and `quarantined_at` are **persisted on the session row**, so
the ladder survives daemon restarts.

### 4.2 Named sessions

Named sessions (`lifecycle: named` on the profile — the supervisor, warm pool workers) are
persistent interactive CLIs. Work arrives as nudges and inbox injections (delivery policy:
[[supervisor-agent]]). Behavior knobs live on the profile:

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
the [[supervisor-agent]] routing story settled first.

### 4.3 Heartbeats, leases, and the stall ladder

`agents.last_heartbeat` is fed from two sources: transcript `in-turn` activity (§4.5) and
explicit `aq task heartbeat` calls (the prompt instructs agents to call it before long
commands). A lease TTL of ~8 minutes without either marks the task **stalled** — not dead.

Stalled tasks climb a ladder, each rung a typed event:

1. **Nudge** (`task.stalled` → `task.nudged`): inject *"no progress for N min: report
   status, finish, or `aq ask`"* via the provider's nudge pipeline.
2. **Backoff and repeat** up to 3 nudges.
3. **Interrupt + restart** (`task.restarted`): C-c, kill, relaunch with `--resume` so
   conversation context survives.
4. **Quarantine** (`task.quarantined`): session `quarantined_at` set, task
   `needs_attention`. No further automatic action; a human (or the supervisor agent)
   decides.

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
poll ~2 s, normalize entries, and produce: `notify.*` events for Discord thread streaming
and the dashboard (replacing the SDK message callback), token usage into the token ledger
(`db.record_token_usage`), model/context-% for session views, and `in-turn`/`idle` activity
for the heartbeat. **This is the signal; pane text is a hint.**

Claude and Codex have readers as of 2026-08-27; Gemini does not, and until it does a
Gemini session's heartbeat rides on pane activity alone — the exact signal the paragraph
above says not to trust. Where a harness picks its own conversation id instead of taking
ours (Codex has no `--session-id`), the reader also reports it via
`discover_session_key`, and the watcher writes it onto the row: that is the only place the
daemon can learn a key it did not assign, and without it restart-with-resume is impossible
for that harness.

**Peek** is `capture-pane` — for humans (`aq session peek`, dashboard, Discord `/peek`) and
as the SSE fallback when no transcript is found. **Activity** from the provider is pane
activity with poke discounting (our own nudges must not look like agent progress).

**Streaming API:** `GET /api/sessions/{id}/stream` (SSE) replays transcript history then
tails it, falling back to peek diffs for transcript-less harnesses.

### 4.6 Hooks

Hook **wiring** is owned here (which events, when installed, suppression rules); payload
**content** is owned by [[aq-surface]]. Per-harness hook file templates are written into the
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
window index) → text via `send-keys -l` when ≤ 4 KB, else `load-buffer` +
`paste-buffer -p -d` (bracketed paste) → debounce (~500 ms) → `Escape` only for harnesses
that need it (per-harness `skip_escape_before_enter`; claude/codex skip) → `Enter`,
**confirmed** by busy-indicator poll, up to 3 attempts → still unconfirmed raises
`NotSubmitted` and the caller re-queues. Copy-mode is cancelled first (a parked pane
swallows keys).

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
Agent-type profiles gain `harness: claude` plus `model`, `permission_mode`, `lifecycle`,
`wake_mode`, `max_session_age`, `idle_timeout`. Files sync to an in-memory registry via the
existing vault watcher, following the `src/profiles/parser.py` / `mcp_registry.py` pattern
— the file is the source of truth (principle #1).

## 7. Surfaces

**Config** (`~/.agent-queue/config.yaml`, `sessions:` block): `enabled`, `provider`,
`tmux_socket`, `lease_ttl_seconds` (default 480), `stall_max_nudges`, `stall_backoff_seconds`,
`max_restarts`, `restart_window_seconds`, `restart_backoff_seconds`, `dialog_budget_seconds`,
`state_cache_ttl_seconds`, `transcript_poll_seconds`, `adopt_on_start`. Per-profile knobs
(lifecycle, wake_mode, timeouts) live in profile markdown, not config.yaml.

**CLI** (semantics here; envelope and plumbing in [[aq-surface]]):

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
rides the EventBus (principle #7) — Discord and dashboard subscribe, the reconciler never
calls them.

## 8. Failure Modes and Edge Cases

- **Dropped submit.** A nudge can paste without submitting (Enter races bracketed paste).
  Submit is confirmed by busy-poll; `NotSubmitted` re-queues rather than assuming delivery.
- **Nudging a busy agent** can interleave with its typing. Nudges are debounced, locked
  per-session, and policy (when to deliver vs. queue) belongs to [[supervisor-agent]];
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

- **[[worktree-execution]]** owns worktree slots, branches, the reaper, and the merge slot.
  This spec consumes `work_dir` (the slot worktree) in SessionSpec and records it on the
  session row; the reaper's liveness guard queries this spec's process-table scan.
- **[[supervisor-agent]]** owns the `messages` table, reply protocol, and when a message
  becomes a nudge vs. an inbox injection. It consumes `nudge`, named-session wake, and the
  SSE stream.
- **[[aq-surface]]** owns the `aq` CLI envelope, `aq prime`/`handoff`/`inbox` content, hook
  payload formats, and REST auth. This spec owns which hooks fire and the session/task
  state transitions those commands cause.
- **[[trust-and-ops]]** owns env scrubbing rules, API token scoping, and the
  skip-permissions-inside-worktree trust argument that `permission_flag` relies on.
- **[[feature-pauses]]** owns the memory/playbooks pause switches; `aq prime` keeps L1/L2
  slots empty-but-present so memory plugs back in without touching this spec.
- **[[workspaces-v2]]** remains the workspace kind/instance model; sessions attach to
  acquired workspaces, they do not change acquisition semantics.

## 10. Deferred

- Remote providers (k8s, ssh), and a structured-socket provider (Gas City's `herdr`
  direction) if pane scraping proves too brittle even in its confined role.
- Harnesses beyond claude/codex/gemini/opencode (cursor-agent, copilot, pi, …) — additive
  markdown once the first four are certified.
- `RELAUNCH` capability use (`respawn-pane -k`) for command-drift-only recycling — the ABC
  reserves the capability; v1 always does full kill + start (respawn-pane drops env).
- Warm pool workers (named sessions pre-claimed for task work) — needs [[supervisor-agent]]
  routing first.
- fsnotify transcript watching (poll ~2 s is sufficient and portable in v1).
- Windows-native provider; per-session resource limits (cgroup/systemd scopes).
