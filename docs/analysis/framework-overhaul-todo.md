---
tags: [analysis, todo, pre-spec, runtime, worktrees, supervisor, playbooks, memory, messaging]
date: 2026-08-19
status: v3 — direction locked; specs drafted (see §0b)
---

# Agent Queue — Framework Overhaul: Pre-Spec Todo

**What this is.** A working list of changes to make to agent-queue, derived from
[comparison-gascity-beads.md](comparison-gascity-beads.md) and the direction decisions
below. It is deliberately *pre-spec*: each workstream states intent, the shape we think is
right, concrete todo items, and open questions — enough to write real specs in
`docs/specs/design/` afterwards. Tags:

- ✅ **decided** — direction set (Jack, 2026-08-19)
- ⏸ **paused** — keep the code, switch it off, revisit after the core is tuned
- 🟡 **proposed** — follows from the analysis; needs a yes/no
- ❓ **open** — needs a decision or investigation before spec

Code references are to Gas City (`../gascity`) and Beads (`../beads`) where we borrow, and
to agent-queue modules where we change.

---

## 0b. Specs drafted (2026-08-19)

All eight workstreams now have paired **design** and **implementation** specs:

| Workstream | Design spec | Implementation spec |
|---|---|---|
| A Session runtime | [session-runtime](../specs/design/session-runtime.md) | [session-runtime](../specs/implementation/session-runtime.md) |
| W Worktrees | [worktree-execution](../specs/design/worktree-execution.md) | [worktree-execution](../specs/implementation/worktree-execution.md) |
| D Work graph | [work-graph](../specs/design/work-graph.md) | [work-graph](../specs/implementation/work-graph.md) |
| B Supervisor | [supervisor-agent](../specs/design/supervisor-agent.md) | [supervisor-agent](../specs/implementation/supervisor-agent.md) |
| C aq surface | [aq-surface](../specs/design/aq-surface.md) | [aq-surface](../specs/implementation/aq-surface.md) |
| F Messaging/UI | [messaging-rework](../specs/design/messaging-rework.md) | [messaging-rework](../specs/implementation/messaging-rework.md) |
| E/P Pauses | [feature-pauses](../specs/design/feature-pauses.md) | [feature-pauses](../specs/implementation/feature-pauses.md) |
| G Trust & ops | [trust-and-ops](../specs/design/trust-and-ops.md) | [trust-and-ops](../specs/implementation/trust-and-ops.md) |

When a spec and this todo disagree, the spec wins (it is later and more detailed).

---

## 0. Direction decisions

| # | Decision | Status |
|---|---|---|
| D1 | **Session runtime, tmux-first.** Replace the in-process runtime layer (`claude_sdk` via claude-agent-sdk, `acpx`) with a session-provider model, Gas City style: each agent is a fully contained CLI session the daemon starts, observes, nudges and adopts — never a stream the daemon blocks on. | ✅ |
| D2 | **Algorithmic orchestrator, no hardcoded roles.** Scheduling, cascade, gates, leases, worktrees stay deterministic Python. The *supervisor* becomes a configured long-running agent (a profile, not code) that acts on the orchestration layer through `aq`/MCP, talks to the user via chat/dashboard, and turns specs into task graphs with context and dependencies. | ✅ |
| D3 | **Playbooks: pause, don't replace.** The concept has merit; it is half-baked and under-tested. Switch off (feature flag), freeze the code, bring back properly once the core is tuned. The orders + formulas + judgment-tasks shape from the comparison is the *candidate* comeback design (§8), not current work. | ⏸ ✅ |
| D4 | **Memory: pause, don't strip.** Disable the memory plugin and L1/L2 injection while re-architecting; keep tiers, reflection, consolidation code. We do **not** adopt Gas City's "the ledger is the memory" model (§1). What we do adopt now is the *delivery plumbing* (`aq prime`, hooks) and a task **work-state contract**, so memory plugs back in cleanly. | ⏸ ✅ |
| D5 | **Worktree-based parallel execution.** One base repo per project, a worktree + branch per task by default, robust lifecycle (create/reap/merge), parallel work-streams bounded by caps rather than by pre-provisioned clones. | ✅ |
| D6 | **`aq` CLI is the primary surface** for agents and humans (Beads/Gas City style: `--json` everywhere, prime/handoff/close/heartbeat/ask for agents; session attach/peek/nudge/explain for humans). MCP slimmed to a task-scoped set. | ✅ |
| D7 | **Messaging: framework first, then rethink.** Remove Telegram. Discord keeps **per-task threads to observe and interact with the running agent** and gate/approval buttons; the mirrored command surface and project wizard go; project-channel chat routes to the supervisor agent. Dashboard becomes the primary UI. | ✅ |
| D8 | **Work-graph substrate** from the comparison (typed edges, gates as records, `explain`, labels, outcome metadata, enforced state machine) as the base the above sit on. | 🟡 |
| D9 | **Multi-project stays first-class.** One daemon manages N projects (Gas City: N *rigs* in a city, each a registered repo with its own bead namespace and agent scope; a machine-wide supervisor manages N cities). Keep our single-DB `project_id` model, cross-project fair-share scheduling, per-project budgets/caps/profile overrides/channels; add per-project worktree bases and optional per-project supervisor sessions. | ✅ |

**Goes away now:** `src/runtimes/claude_sdk.py` (1.2k), `acpx.py` (0.6k) + `claude-agent-sdk`/acpx deps;
the in-process Supervisor chat loop wiring (Discord/Telegram/CLI → `Supervisor.chat()`);
`src/telegram/` (1.6k, `python-telegram-bot`); Discord's 122 mirrored command handlers and
project wizard (most of 12.1k).
**Paused (flag off, code frozen):** `src/playbooks/` (10.3k), workflow coordination modules,
memory plugin load + L1/L2 prompt layers, reflection engine, chat analyzer `observe()`,
`src/runtimes/supervisor.py` + `src/chat_providers/` (dormant until the playbook comeback
decides their fate).
**Stays and grows:** `CommandHandler`, schema, vault, workspaces v2 (reworked around
worktrees), scheduler/cascade, `src/api/` + `dashboard/`, plugins, `aq` CLI, MCP (slimmed).

---

## 1. How Gas City handles "memory" — and why we keep ours

Jack's question: *"If beads are tasks but beads are also memory, how do you execute on one
bead and still have agents aware of past learnings?"*

**Short answer: Gas City has no learning memory; it has durable *work state*. An agent on one
bead is aware of the past only to the extent (a) the state was written onto beads/mail and
(b) its prompt tells it to go look.** From the code and packs:

1. **The bead carries the state needed to resume *this* work.** The Gas Town polecat
   prompt defines a metadata contract — `work_dir`, `branch` (`polecat/<bead-id>`),
   `target`, `existing_pr`, `pr_url`, `rejection_reason` — "recorded early for crash
   recovery"; on "Rejection-Aware Resume" a *different* polecat reads `rejection_reason`,
   resumes the branch and fixes only that. The core pack's `mol-do-work.toml` closes with
   typed outcomes (`gc.outcome`, `gc.work_outcome=shipped|no-op|blocked|abandoned`,
   `gc.work_commit`, `gc.work_branch`, `gc.work_verification`) and `--notes "Done: …"`.
   Molecule position (`gc bd mol current`) is durable state. "Memory" here = *anyone can pick
   this bead up mid-flight*, not "the fleet learned something".
2. **Mail carries hand-offs and escalations.** `gc handoff` mails yourself a `HANDOFF:` note
   and restarts; `PreCompact` hooks run `gc handoff --auto`; `gc prime --hook` on
   `SessionStart` injects the note and unread mail into the next session.
3. **Cross-task "learnings" arrive by three mechanisms, none automatic:** `bd remember →
   bd prime` (keyed KV injected wholesale at prime, substring search only); the `cass` pack
   fragment ("check past agent sessions before starting from scratch" via `cass search
   --json`); human-authored prompt templates/fragments/skills/overlays.
4. **Persistent agents remember via the harness** (`wake_mode=resume` → `claude --resume`);
   ephemeral workers (`fresh`) remember nothing but the bead.
5. **The ledger is queryable history** (closed beads, notes, comments, Dolt history, `bd
   search`; Beads' LLM compaction keeps it cheap) — nothing reads it unprompted.

No reflection, no extraction, no retrieval by topic — by philosophy ("the model IS the skill
system"). **We are not adopting that.** Our tiers + reflection are the differentiator; they
are paused (D4) only so the core can be tuned without them in the loop. What we take: the
**work-state contract** (it is task state, not memory — Workstream W/E), **`aq prime` +
hooks** as the delivery path so memory survives compaction and works with any CLI harness,
and **handoff** as a first-class protocol.

---

## 2. Workstream A — Session runtime: tmux-first provider model  ✅

### A.0 Goals, non-goals, constraints

Goals: agents are independent OS sessions (survive daemon restart, attachable by humans,
harness-agnostic, observable); the daemon reconciles rather than blocks; a new CLI harness is
a config file, not a Python module.

Non-goals now: Kubernetes/ssh/remote providers; ACP transport (drop `acpx`; revisit only if a
harness has no CLI); Windows-native sessions.

Constraints: tmux is POSIX-only. The daemon already targets Linux/WSL2 (`setup.sh`, `run.sh`,
WSL2 note in `src/cli/daemon.py`); Gas City ships Linux/darwin only and documents WSL 2. Keep a
**`subprocess` provider** as the no-tmux fallback (no attach/peek/nudge — Gas City's is the
same: stdout to `/dev/null`, control socket, fire-and-forget).

Honest caveat from Gas City's own docs: scraping tmux pane text is brittle — their `herdr`
provider design is "the stated answer to scraping tmux text output", yet tmux remains their
default/fallback and `tmux.go` is ~4k lines of quirk handling. **Design rule: pane scraping
only for readiness, startup dialogs and nudge-submit confirmation; structured channels
(`aq` calls from the agent, harness transcripts, process table) for completion, outcomes,
liveness and streaming.**

### A.1 Provider interface (replaces `Runtime` ABC in `src/runtimes/base.py`)

Gas City's `Provider` has 19 methods + ~15 optional capability interfaces
(`internal/runtime/runtime.go:137-230`). Start smaller:

```
class SessionProvider(ABC):
    name: ClassVar[str]; capabilities: ClassVar[frozenset[Cap]]   # ATTACH, PEEK, NUDGE, ACTIVITY, RELAUNCH
    async def start(self, spec: SessionSpec) -> SessionHandle        # detached; returns immediately
    async def stop(self, h, *, grace: float) -> None                  # SIGTERM tree → SIGKILL → kill-session
    async def interrupt(self, h) -> None                              # C-c
    async def is_running(self, h) -> bool                             # runtime artifact present
    async def process_alive(self, h, process_names) -> bool           # agent process alive
    async def list_running(self, prefix) -> list[SessionHandle]       # adoption; PartialList on error
    async def nudge(self, h, text) -> None                            # inject + submit; raises NotSubmitted
    async def peek(self, h, lines) -> str                             # capture-pane
    async def last_activity(self, h) -> float | None
    async def attach_command(self, h) -> str                          # "tmux -L aq attach -t s-<id>"
    async def set_meta/get_meta(self, h, key[, value])
```

`SessionSpec = {session_name, work_dir, command: list[str], env, prompt|None, prompt_mode,
ready: {delay_ms, prompt_prefix, process_names}, lifecycle: task|named, dialogs}`.
Registry: `tmux` (default), `subprocess`, `fake` (tests).

### A.2 tmux provider — what to implement (from `internal/runtime/tmux/{tmux,adapter,interaction,state_cache}.go`)

- One tmux server per daemon: `tmux -u -L aq …`; probe `has-session -t =__aq_probe__` before
  creating (never create against a dead socket).
- Names `s-<task_id>` (tasks), `n-<profile>` (named sessions); `^[a-zA-Z0-9_-]+$`.
- Create: `new-session -d -s <name> -c <work_dir> -e K=V … '<command>'`; agent is the pane's
  initial process; then `window-size latest`, `remain-on-exit on`, `mouse off`,
  `monitor-activity off`. Prompts >~1 KB via temp file + `sh -c '… exec <cmd> "$__aq_prompt"'`
  (new-session buffer limit) — but we keep argv tiny anyway (A.4).
- Env markers on every session: `AQ_SESSION_ID`, `AQ_TASK_ID`, `AQ_PROFILE`, `AQ_DAEMON_EPOCH`,
  `AQ_INSTANCE_TOKEN`, `AQ_WORK_DIR`, `AQ_API_URL`, `AQ_API_TOKEN` (+ strip `CLAUDECODE`,
  `CLAUDE_CODE_ENTRYPOINT`). Process-table scans (`/proc/<pid>/environ`) on these are the
  truth for liveness and adoption — never PID files.
- Readiness: poll `#{pane_current_command}` until not a shell; then `ready_delay_ms` or poll
  `capture-pane` (200 ms) for `ready_prompt_prefix` (Claude `❯ `, normalize NBSP); budget
  clamped [5s, 60s]; timeout non-fatal unless the pane died → `start-stderr.log`.
- Startup dialogs (`internal/runtime/dialog.go`): shared 8 s budget, 500 ms poll — trust
  folder, theme, "Bypass Permissions mode", resume selector, MCP trust, rate-limit (choose
  Stop → quarantine). Patterns live in the harness profile.
- Nudge: per-session lock; find the agent pane by `process_names`; `send-keys -l` (≤4 KB) else
  `load-buffer`+`paste-buffer -p -d`; 500 ms debounce; `Escape` only for harnesses that need
  it (Claude/Codex don't); `Enter` and **confirm** by busy-indicator poll up to 3×; raise
  `NotSubmitted` → re-queue. Cancel copy-mode first.
- Peek: `capture-pane -p -t <s> -S -<n>`. Activity: max `#{window_activity}` with poke
  discounting. Idle/busy from pane text as a hint; transcript `in-turn` (A.6) is the signal.
- Stop: pane pid → `pgrep -P` descendants + process group → SIGTERM, 2 s, SIGKILL →
  `kill-session`; every kill fenced by `AQ_INSTANCE_TOKEN`.
- State cache: one `list-panes -a` + one `ps -eo pid,ppid,comm,args` per tick (TTL 2 s);
  `ps` failure ⇒ optimistic alive; "no server" ⇒ unknown, keep last-known-good, defer
  destructive actions. Relaunch via `respawn-pane -k` for command-only drift.

### A.3 Harness profiles (replace `acpx` fan-out and the SDK)

`vault/harnesses/<name>.md` (system, project override), fields from
`internal/worker/builtin/profiles.go` (17 builtins): `command`, `args`, `base:` inheritance,
`prompt_mode arg|flag|none`, `print_args` (`-p`), `permission_flag`
(`--dangerously-skip-permissions`), `resume` (`--resume {key}` / Codex `resume` subcommand /
fork `--fork-session`), `session_id_flag`, `ready_delay_ms`, `ready_prompt_prefix`,
`process_names`, `skip_escape_before_enter`, `supports_hooks` + hook file templates,
`instructions_file` (`CLAUDE.md`/`AGENTS.md`), `transcript_paths`, `dialogs`, `model_flag`,
`effort_flag`, `env`. Ship `claude` first; then `codex`, `gemini`, `opencode`, `cursor-agent`,
`copilot`, `pi`. Profiles reference `harness: claude` + `model`, `permission_mode`,
`lifecycle`, `wake_mode`, `max_session_age`, `idle_timeout`, `append_fragments`.

### A.4 Lifecycles, prompt delivery, hooks

- **`task` sessions (Gastown-style — decided 2026-08-19)**: the harness runs as a full
  **interactive** CLI in the pane — `claude "<bootstrap>"` (positional prompt arg), *not*
  `claude -p` print mode. That is how Gas Town/Gas City run every worker: the TUI stays
  attachable and human-readable, the prompt rides argv (temp-file exec above ~1 KB), progress
  is read from the harness's own transcript files, and the session ends when the agent has
  explicitly closed its work and acknowledged drain — never on print-mode process exit.
  Bootstrap is short: "You are running task `<id>` in `<work_dir>`. Run `aq prime` and follow
  it. When done: `aq task close ...` then `aq session drain-ack`." The full prompt (profile
  role, project override, task, attachments, workspace block — and L1/L2 once memory is back)
  renders to `<work_dir>/.aq/prompt.md` and is returned by `aq prime`. After drain-ack (or on
  the exit classifier's verdict) the reconciler kills the session.
- **`named` sessions** (supervisor, warm pool workers): same interactive CLI, but persistent;
  work arrives as nudges/inbox; `wake_mode resume|fresh`; idle sleep after `idle_timeout`; `max_session_age
  (+jitter)` recycles with handoff.
- **Hooks** (Claude: merged `--settings <path>`; others: per-harness templates as in Gas
  City's `internal/hooks/config/claude.json` and core pack `overlay/per-provider/*`):
  `SessionStart → aq prime --hook-json` (suppressed if delivered via argv); `PreCompact → aq
  handoff --auto` (write note, **no restart** — Gas City's `gc-flp1` lesson);
  `UserPromptSubmit → aq inbox --inject` (pending human replies/messages; 15 s timeout, exit 0).
  No `Stop` hook; completion is explicit.
- **Permissions**: default skip-permissions inside the task's isolated worktree (Gas City does
  this for every harness); documented in the trust model (G.3).

### A.5 Completion, outcomes, liveness (Gas City: no `gc done`, no Stop hook)

- **Explicit**: `aq task close <id> --outcome pass|fail --failure-class transient|hard
  --work-outcome shipped|no-op|blocked|abandoned --commit <sha> --notes "…"` (CLI or slim
  MCP tool), followed by `aq session drain-ack` — the Gastown completion protocol. Process
  exit is a failure signal, not the success path.
- **Process exit** with the task still IN_PROGRESS ⇒ classify (Gas City
  `DecideSessionExit`): rate-limit pane text → PAUSED(rate_limit); rapid crash → restart with
  backoff, quarantine after `max_restarts`/`restart_window` (**persist counters on the
  task**); productive death with task open ⇒ `needs_attention`/re-queue per policy. Never
  silently READY.
- **Heartbeat/lease**: `agents.last_heartbeat` from transcript `in-turn` activity and from
  `aq task heartbeat` (the prompt tells agents to call it before long commands, as
  `mol-do-work` does). Lease TTL 5–10 min ⇒ *stalled*, not dead.
- **Stall ladder**: stalled → nudge ("no progress for N min: report status, finish, or `aq
  ask`") → backoff → 3× → interrupt/restart with `--resume` → quarantine; typed events
  `task.stalled/nudged/restarted/quarantined`. `stuck_timeout_seconds` stays as backstop.
- **Adoption on daemon restart**: `list_running` + process-table scan by `AQ_SESSION_ID`;
  live sessions keep IN_PROGRESS, dead ones go through the exit classifier. Replaces the
  blanket reset in `_recover_stale_state` (keep `--reset` as an admin escape hatch).

### A.6 Observation, streaming, tokens

- Peek for humans/dashboard/Discord. **Transcript readers** (Gas City `internal/sessionlog`:
  Claude `~/.claude/projects/<slug>/<id>.jsonl`, Codex `~/.codex/sessions/…`, Gemini
  `~/.gemini/tmp`): resolve path from `work_dir` + session key; poll 2 s (fsnotify when
  available); normalize entries; tail gives model, context %, activity, token usage for the
  ledger. **This feeds `notify.*` events (Discord thread streaming, dashboard) instead of the
  SDK message callback.**
- `GET /api/sessions/{id}/stream` (SSE): transcript history + tail, peek fallback.
- ✅ Decided (per Gastown): interactive sessions + transcript tailing + peek. No `-p` print
  mode and no `stream-json` pipe-pane for task sessions.

### A.7 Human surfaces

`aq session list|peek|attach|nudge|logs -f|kill`; dashboard session views (A.7 → F);
Discord task thread (F.1): stream + reply-as-nudge.

### A.8 Removal & replacement plan

1. `SessionProvider` + `tmux` + `fake`; `SessionSpec` from profile+harness; old runtimes
   behind a flag. 2. Route `claude_sdk` profiles to `tmux/claude` task sessions; dual-run on a test
   project; compare outcomes/tokens. 3. Adoption/reconcile in the cascade; stall ladder;
   transcript readers; SSE; Discord thread streaming from transcripts. 4. Hooks + `aq prime` +
   handoff. 5. `codex`, `gemini`, `opencode` harnesses (replaces `acpx`). 6. Delete
   `claude_sdk.py`, `acpx.py`, NDJSON helpers, deps, `acpx` in `setup.sh`; slim MCP (G.2).

### A.9 Testing

`fake` provider + conformance suite all providers must pass; cascade/scheduler tests on
`fake`; tmux integration job (Linux CI, `tmux -L aq-test`): create, readiness with a stub agent
that prints a prompt, nudge-submit-confirm, peek, kill-tree, adoption after simulated daemon
restart, instance-token fence; dialog/nudge quirk tables as data with goldens per harness.

### A.10 Gas City scar tissue to pre-empt (each a test or an explicit non-goal)

Dropped submits (confirm Enter by busy-poll; re-queue); per-harness Escape semantics; detached
TUIs drop pastes (SIGWINCH via `resize-pane`); copy-mode parks swallow keys; readiness regex is
bootstrap-only; tmux 3.3 window-size pin; `respawn-pane` drops env; new-session ~2 KB buffer;
`#{session_activity}` stale when detached; 100 ms SIGTERM grace orphans Claude (use 2 s); PID
recycling (start-time check); name reuse (scan by session id); `ps` failure must not reap;
PreCompact hook restarting every compaction; hook `--claim` minting orphan claims. Still open
on their side: nudge input collision (#1216), nudge interrupting work (#1275), tmux default
binding drift across versions, systemd moving panes into `tmux-spawn-*.scope` (identify by
descendant inspection), "peek is not free" (2 s state cache).

### A. Todo

- [ ] Spec `docs/specs/design/session-runtime.md`.
- [ ] `src/sessions/`: `provider.py`, `tmux.py`, `subprocess.py`, `fake.py`, `spec.py`,
      `dialogs.py`, `state_cache.py`, `transcripts/` (readers).
- [ ] `vault/harnesses/*.md` + parser + sync.
- [ ] `aq prime|handoff|inbox --inject|task close|task heartbeat|ask|session *`; hook
      templates; Claude `--settings` merge.
- [ ] `sessions` table (`id, task_id, profile, harness, provider, name, state, session_key,
      work_dir, epoch, instance_token, started_at, last_activity, restarts, quarantine`).
- [ ] Cascade: adoption pass; lease/stall ladder; exit classifier; persisted restart counters.
- [ ] Dual-run; delete SDK/acpx; conformance + tmux integration tests.

---

## 3. Workstream W — Worktree-based parallel execution  ✅

### W.0 Today vs target

Today (`src/orchestrator/workspace.py`, `workspace_attachments.py`, `git/manager.py`):
workspaces are **pre-provisioned clones** (`workspaces` rows, `source_type clone|link|init|
worktree`), one exclusive lock per clone; `branch-isolated` is a *fallback* that creates a
worktree from an already-locked clone (`_create_branch_isolated_worktree`, path
`<parent>/.worktrees-<base>/<slug>/`); parallelism per project is bounded by how many clones
exist; a per-base `asyncio.Lock` serializes git ops; `_recover_stale_state` deletes worktree
workspaces on boot; `directory-isolated` is stubbed. Gas City: per-bead/per-agent worktrees
under `.gc/worktrees/<rig>/…`, prepared by `pre_start`, reaped by
`bead_worktree_reaper.go` only after a liveness check (`bead_worktree_liveness.go`) and
merged-branch detection, `auto_reap_closed_bead_worktrees`, `prune-branches` order; polecats
commit on `polecat/<bead-id>`; the Refinery merges. Beads: `bd worktree`, a merge slot.

Target: **worktree per task is the default**; one *base* per project; N parallel tasks on the
same repo without N clones; lifecycle is explicit and crash-safe; merges are serialized.

### W.1 Model

- `workspace_kinds.project-repo` gets `mode: worktree` (default) | `exclusive-clone`
  (legacy) | `directory-isolated` (later). A project has one **base workspace** — its normal
  clone with the default branch checked out — used for fetch/branch/worktree ops, never as an
  agent cwd while worktrees exist.
- **Per-agent worktrees, in-repo (decided 2026-08-19)**:
  `worktree_path = <base_repo>/.aq/worktrees/<agent_slot>/`, hidden and git-ignored. A
  worktree belongs to an agent slot and is **reused across tasks**: on assignment the cascade
  fetches, hard-resets, and checks out a fresh per-task branch `aq/<task_id>` (from
  `project.repo_default_branch` unless `base_branch` is given) — the same reuse pattern
  `aprepare_for_task` already implements for clones, and the same shape as Gas Town's
  per-polecat worktrees. `work_dir` + `branch` are recorded on the task (work-state contract).
  Mechanics to get right: ignore `.aq/` via `<base>/.git/info/exclude` (no commit needed, works
  for repos we don't own); `git worktree add` inside the repo's own directory is fine once
  ignored; worktree count = agent-slot count, so parallelism stays cap-bounded; stale
  registrations cleaned with `git worktree prune`.
- Locks become **per-base-repo git mutex + per-slot worktree ownership**, not a per-clone
  exclusive lock; `acquire_for_task` keeps all-or-nothing semantics for *other* kinds
  (`vault`, `readonly-dir`, package kinds).
- Shared-workspace tasks (explicit `workspace_mode: shared` on an existing clone) remain for
  tasks that must run in place (e.g. deploy scripts) — locked exclusively as today.

### W.2 Lifecycle & reaping

- Create (first use of a slot): `git worktree add <base>/.aq/worktrees/<slot> origin/<default>`
  under the base mutex; per task: fetch, hard-reset, `git switch -c aq/<task> origin/<default>`
  inside the slot's worktree; sentinel `.aq-worktree.json` (slot, task id, created_at, epoch).
- While running: worktree is the session `work_dir`; hooks/overlays (`.claude/settings.json`,
  `.aq/prompt.md`) live inside it.
- On close: the **branch** is the durable artifact, not the worktree. `shipped` → branch
  pushed/PR'd; the slot worktree is reset for the next task. `blocked|abandoned|failed` →
  branch kept for `retain_failed_for` (default 7 d) for forensics/retry. **Reaper** = cascade
  step for *slots*, not tasks: a slot worktree is removed only when the slot is retired or
  project caps shrink — and only after checking *no live session uses it* (process table by
  `AQ_TASK_ID`/cwd); merged `aq/<task>` branches are pruned (local; remote per policy) →
  event. Never reap on partial information (Gas City's rule).
- On daemon restart: *adopt* worktrees (they belong to agent slots, not to the daemon run);
  no wholesale deletion. `aq workspace doctor` finds orphans (dir without task, task without dir,
  stale `.git/worktrees` entries → `git worktree prune`).

### W.3 Integration (parallel streams landing)

- Completion pipeline stays algorithmic: commit (if agent didn't), push `aq/<task>`,
  PR via `gh` or local merge per project policy; **rebase-before-merge** (exists) under a
  per-project **merge slot** (Beads `bd merge-slot`) so only one task integrates at a time;
  conflicts ⇒ task `needs_attention` with `rejection_reason`/conflict files on the task
  metadata, optional auto-spawn "resolve conflict" continuation that resumes the same branch
  (Gas Town's rejection-aware resume).
- `PR merged` / `CI green` become gates (D) swept by the cascade; merge events fire
  `task.merged`; reaper follows.

### W.4 Parallel work-streams

- Concurrency per project = `max_concurrent_agents` (+ per-profile caps) = worktree slot
  count — no longer limited by clone count. Dependency graph + typed edges (D) give ordering; independent tasks
  fan out into parallel worktrees automatically.
- `requires_kinds` still supports multi-repo tasks (one worktree per repo kind).
- ✅ Caches (decided — best judgment): per-kind `worktree_setup` commands in the
  workspace-kind markdown, run once when a slot worktree is created (`npm ci`, symlink shared
  caches), like Gas City `session_setup`. Per-agent slot reuse already amortizes most of the
  cost — a slot keeps its `node_modules` across tasks. No global symlink magic in v1.
- ❓ Directory-isolated (monorepo: same branch, different dirs) — after the above.

### W. Todo

- [ ] Spec update `workspaces-v2.md` → per-agent worktree mode, base workspace, lifecycle,
      reaper, merge slot; retire the "branch-isolated fallback" path.
- [ ] `GitManager`: `aworktree_add/remove/prune/list`, `amerge_slot_*`, conflict reporting.
- [ ] Cascade steps: `worktree-prepare` (pre-session), `worktree-reaper`, `merge-slot`.
- [ ] Task work-state fields: `work_dir`, `branch`, `pr_url`, `rejection_reason`, `merged_at`.
- [ ] `aq workspace list|doctor|reap`, `aq task branch`; dashboard workspace view.
- [ ] Tests: parallel tasks on one repo; restart mid-task keeps worktrees; reaper liveness
      guard; merge-slot serialization; conflict → needs_attention path.

---

## 3b. Multi-project (Gas City "rigs")  ✅

Gas City: a **city** registers many **rigs** (`gc rig add <path>`, `[[rigs]]` with `name,
path, prefix, default_branch, imports, patches, max_active_sessions, default_sling_target,
formulas_dir`); each rig has its own bead-ID prefix and `.beads/` store, rig-scoped agents are
instantiated once per rig (`rig/agent`), city-scoped agents (mayor, control-dispatcher) may
serve any rig, cross-store routing is refused except for city-scoped agents ("cook and sling
in the store the worker reads"), and a `cross-rig-deps` core order bridges dependencies across
rigs. A machine-wide supervisor (`~/.gc/cities.toml`) runs many cities. So rig ≈ our project;
city ≈ our daemon install.

What we keep (already better for our use): one DB with `project_id` (cross-project queries,
deps and scheduling are trivial — no cross-store refusal), `credit_weight` fair-share and
per-project `max_concurrent_agents`/`budget_limit`, project-scoped profile/MCP/kind/playbook
overrides in the vault, per-project Discord channels. What to add in this overhaul:

- [ ] Per-project **worktree base** and worktree root (W), per-project merge slot.
- [ ] Per-project **supervisor** option (`mode: on_demand` named session scoped to a project,
      `vault/projects/<id>/agent-types/supervisor/profile.md` override) alongside a
      system-level one; message routing by project channel.
- [ ] Per-project harness/provider overrides (`vault/projects/<id>/harnesses/`), caps per
      profile per project (the deferred `max_agents_by_type`).
- [x] ✅ Cross-project dependencies are **allowed, explicitly** (decided 2026-08-19): keep
      the single-DB edge, surface the other project in `aq task explain`, the dashboard graph,
      and the supervisor's view; readiness treats them like any blocking edge.
- [ ] Dashboard: project switcher already exists (`pages/project/*`); add system-wide
      sessions/gates views across projects.

---

## 4. Workstream B — Supervisor as a configured agent over an algorithmic orchestrator  ✅

### B.0 Split of responsibilities

| Algorithmic orchestrator (Python, zero LLM) | Supervisor agent (a profile, long-lived session) |
|---|---|
| cascade, scheduler (deficit/affinity/caps), typed-edge readiness, gates sweep, leases/stall ladder, exit classification, adoption, worktree lifecycle, merge slot, token ledger, events | reads the state (`aq task list/explain`, `aq project ready`), **acts on the orchestration layer** (`aq task create --graph`, `dep add`, `label`, `priority`, `gate resolve`, `session nudge`, `task reopen --feedback`), **talks to the user** (Discord project channel / dashboard chat → its session; replies via `aq reply` or transcript), **authors specs** (`vault/projects/<id>/specs/<slug>.md`) and **turns them into task graphs with well-defined context and dependencies** |

Nothing in Python references "supervisor" as a role; it is `vault/agent-types/supervisor/
profile.md` (shipped default): `harness: claude`, `lifecycle: named`, `wake_mode: resume`,
`allowed_tools` = orchestration + vault write, no repo worktree (uses the `vault` kind +
read-only project dir). **Scope (decided 2026-08-19): one supervisor per project,
`mode: on_demand`** — it wakes on the first message to that project's channel/dashboard chat,
sleeps after `idle_timeout`, and `--resume` preserves its conversation across sleeps.
Rationale: context stays project-shaped (its vault scope, its specs, its channel); token cost
scales with active projects only; the ~10-30 s cold-start on the first message after idle is
acceptable for a planning/oversight role. An install-wide shared supervisor remains possible
later purely as config (a named session not bound to one project).

### B.1 Message path (chat & dashboard)

`POST /api/sessions/{name}/message` (dashboard chat, Discord project channel, `aq chat`) →
`messages` row → nudge (or `UserPromptSubmit` inject if mid-turn) → agent replies with
`aq reply <msg-id> "…"` (or we tail the transcript's assistant turn) → `message.replied`
event → Discord/dashboard. Same `messages` table serves agent↔agent notes and task-thread
replies (F.1).

### B.2 Spec → tasks

- `aq task create --graph graph.json` (nodes with title, description, acceptance criteria,
  `context` refs — spec path + section, files, URLs — profile, labels, `needs`, `parent`) in
  one transaction; `aq task create --from-spec <path>` when the spec carries a fenced task
  graph. Task context today (`task_context` rows, `attachments`) already holds this; add
  `context.type = spec_ref`.
- Spec docs live in the vault (`vault/projects/<id>/specs/`), are auto-attached to child
  tasks, and rendered into `.aq/prompt.md`/`aq prime` as the L2-equivalent *task context*
  (works while memory is paused).
- The supervisor prompt template teaches: explain before acting, create graphs not loose
  tasks, attach specs, use `aq task explain` when asked "why isn't X running", resolve gates
  only when the human said so.

### B.3 What the old Supervisor's functions become

| today (`src/runtimes/supervisor.py`) | now |
|---|---|
| `chat()` Discord/Telegram/CLI loop | supervisor session + message relay (B.1) |
| playbook node execution | paused with playbooks (§8) |
| `break_plan_into_tasks()` | supervisor/planner agent writes the graph; `aq task create --graph` validates/creates |
| `observe()` chat analyzer | paused; revisit with the Discord rethink |
| `reflect()` | paused with memory |
| tool-call-only runtime (`profile.runtime = supervisor`) | task session with a restricted-tools profile |

### B. Todo

- [ ] Spec `named-sessions.md` (lifecycle, mode, message relay, reply protocol).
- [ ] `messages` table + `aq reply|inbox|send` + `/api/sessions/{name}/message`.
- [ ] `aq task create --graph|--from-spec`; `context.type=spec_ref`; vault `specs/` dir.
- [ ] Default `supervisor` (and `planner`, `reviewer`) profiles as shipped markdown.
- [ ] Per-profile caps (`max_active`, `min_active`) in profiles (the deferred category
      filter), consumed by the scheduler.
- [ ] Unwire `Supervisor.chat()` from Discord/CLI; keep the module dormant as reference for
      the new supervisor profile (decided); revisit deletion at the playbook comeback.

---

## 5. Workstream C — `aq` CLI as the primary surface  ✅

- Agent-facing: `aq prime`, `aq task show|set|heartbeat|close|ask|handoff`, `aq message
  send|inbox|reply`, `aq memory save|search` (no-ops while paused), `aq session drain-ack`.
  Works from any harness via REST + `AQ_API_URL/AQ_API_TOKEN`; `--json` everywhere with a
  versioned envelope (`schema_version, data, pagination`), `--brief`, `aq schema` (enums).
- Human-facing: `aq task list|explain|graph`, `aq project ready`, `aq session
  list|peek|attach|nudge|logs -f|kill`, `aq gate list|resolve`, `aq workspace *`, `aq doctor`,
  `aq chat <name>`.
- MCP: task-scoped allowlist by default (~8 tools) instead of ~127; profiles may widen.
- Todo: [ ] CLI spec refresh; [ ] envelope + `--brief`; [ ] `aq` agent-surface commands;
  [ ] token auth for agent sessions; [ ] G.2 MCP allowlist.

---

## 6. Workstream D — Work-graph substrate  🟡

Prerequisites for A/W/B explainability; condensed from the comparison.

- [ ] `task_dependencies.dep_type` (`blocks` default, `parent-child`, `waits-for`,
      `conditional-blocks`; non-blocking `discovered-from|related|duplicates|supersedes`).
- [ ] Persisted `is_blocked` + `aq task explain` (blockers, worktree/workspace, caps, budget,
      constraints, affinity wait, cooldown, gate pending, lease/stall) + `aq project ready`.
- [ ] `gates` table (`human|timer|pr-merged|ci-run|event|task`) blocking via edges; cascade
      `gate-sweep`; `aq gate *`; then collapse `AWAITING_APPROVAL`/`AWAITING_PLAN_APPROVAL`/
      `WAITING_INPUT` into gates with a compatibility projection.
- [ ] `task_labels` + filters; metadata-first rule (execution hints, holds).
- [ ] Outcome metadata (`outcome`, `failure_class`, `work_outcome`, `work_commit`,
      `work_branch`, `verification`, `close_notes`); retry policy consults `failure_class`.
- [ ] Enforce the state machine (`transition_task` raises; `force=True`), observed vs desired
      agent state.
- [ ] Groups with computed progress (waves/parallelism) — useful for supervisor-created
      graphs; `after_seq` event replay + payload-registry test.

---

## 7. Workstream E — Memory: paused, plumbing ready  ⏸

- [ ] **Pause switch**: `memory.enabled=false` → plugin not loaded, prompt builder skips
      L1/L2, `memory_*` commands return `{success:false, error:"memory paused"}`; reflection
      engine and consolidation playbook off with playbooks. No deletion.
- [ ] Keep building the **delivery path** memory will use: `aq prime` assembles L0 role →
      project override → *task context (spec refs, attachments)* → workspaces block → tool
      guidance; slots for L1/L2 remain.
- [ ] **Work-state contract** on tasks (W) is core, not memory.
- [ ] **Handoff** (`aq handoff [--auto]` → `task_context(type=handoff)` → next session via
      prime) is core.
- [ ] Comeback plan (later): re-enable plugin; decide SQLite-FTS fallback; agent-initiated
      `aq memory search` fragment; reflection as a judgment task triggered by the comeback
      playbook/order mechanism; memory health view; transcript search (we'll have readers).

---

## 8. Workstream P — Playbooks: paused, comeback design noted  ⏸

- [ ] **Pause switch**: `playbooks.enabled=false` → `PlaybookManager` not started, vault
      watcher ignores `playbooks/`, `playbook_*` commands return "paused", `playbook_runs`
      untouched; `OrphanWorkflowRecovery`/stage-resume handlers off. Freeze `src/playbooks/`
      (bug fixes only).
- [ ] Housekeeping the cascade needs now (gate sweep, reaper, archive, log cleanup) stays as
      **hardcoded cascade steps** — do not build an orders engine just for these yet.
- Candidate comeback shape (from the comparison; decide later): **orders** (trigger + command
  or formula, zero LLM) for schedules/events; **formulas** (deterministic steps → tasks +
  typed edges + gates, per-step profile, `vars`, `extends`, `retry/check/for_each/waits-for/
  finalize`, `aq formula show|run|cook --dry-run`) for repeatable pipelines; **judgment tasks**
  (ordinary tasks with a profile) where playbook nodes used LLM decisions; human checkpoints =
  gates. Alternatively: keep playbooks' markdown authoring but make compile deterministic
  (fenced graph block; LLM as drafting aid) and run nodes as tasks instead of a Supervisor
  conversation. Test strategy either way: pure compiler with goldens; end-to-end on the `fake`
  provider.

---

## 9. Workstream F — Messaging & UI (after A/W/B)  ✅

### F.1 Discord — keep task threads as the observe/interact surface

Keep: (1) **per-task thread** created at session start; stream from transcript readers/peek
with in-place edits (the existing per-stream worker stays); (2) **thread replies → the
agent**: a reply becomes a `messages` row addressed to the task and is delivered by `nudge`
(or answers an open `aq ask` gate); `/peek` shows the pane; (3) **gate/approval buttons**
(`resolve`), `needs_attention` pings; (4) **project channel chat → supervisor session**
(B.1); (5) a minimal set of status slash commands. Remove: the 122 mirrored command handlers,
project wizard, ad-hoc views; Telegram entirely. ✅ The adapter runs **out of process** over
REST/WS (decided 2026-08-19; Gas City's `extmsg` adapters are external too) so it can crash,
restart, or be replaced without touching the daemon.

### F.2 Dashboard — primary UI

Existing `dashboard/` (React 19, TanStack Query, generated TS client, WS event stream; pages
for tasks, playbooks, profiles, workspaces, config, events). Add: sessions (list/state/peek
auto-refresh/attach command/nudge/logs), task explain + graph (typed edges, gates), gates/
approvals inbox, supervisor chat, worktrees view, harness editor, doctor, costs. Rule: **API
first** — every dashboard feature is a CommandHandler command + response model.

### F. Todo

- [ ] Remove `src/telegram/` + dep. [ ] Discord thread pipeline on transcripts; reply→nudge;
      gate buttons; channel chat→supervisor. [ ] Strip mirrored commands/wizard.
- [ ] Dashboard: sessions, explain/graph, gates, chat, worktrees (in that order).

---

## 10. Workstream G — Ops & engineering  🟡

- [ ] G.1 `aq doctor --fix` (config, migrations, vault parse errors, harness binaries, tmux
      server, stale leases, orphan worktrees/sessions, WAL size, stuck tasks).
- [ ] G.2 Task-scoped MCP allowlist; measure context cost via `prompt_analytics.jsonl`.
- [ ] G.3 Trust-boundaries doc + env scrubbing for session env; never interpolate task or
      chat text into `sh -c`.
- [ ] G.4 Invariant/docs-sync tests (tables ⇄ docs, `_cmd_*` ⇄ MCP/CLI, event payload
      registry, state-machine enforcement, harness goldens).
- [ ] G.5 Vault packs (`aq pack import <git>@<sha>`) — later; ship defaults in-tree for now.
- [ ] G.6 `aq costs` with pricing. G.7 Lightweight evidence file per substantial change.

---

## 11. Sequencing

```
Phase 0  Pause & substrate   memory off, playbooks off, Telegram removed; D: typed edges, is_blocked,
                             explain, labels, outcome metadata, gates table; G.2 MCP allowlist; G.3 trust doc
Phase 1  Sessions + worktrees A (claude harness, fake provider, adoption, stall ladder, transcripts, prime/
                             handoff/hooks) + W (worktree-per-task, reaper, merge slot) — built together:
                             a session's work_dir is its worktree; Discord thread streaming moves to transcripts
Phase 2  Supervisor & CLI    B (named sessions, messages, supervisor profile, spec→graph) + C (aq surface,
                             envelope) + F.1 (thread reply→nudge, channel chat→supervisor, strip commands)
Phase 3  Dashboard           F.2 sessions/explain/gates/chat/worktrees; G.1 doctor
Phase 4  Comebacks           E (memory re-enabled through prime), P (playbooks redesigned), G.5 packs
```

Phase 1's two halves can be split between people; B depends on A (named sessions); F.1
depends on A (transcripts) and B (messages); P/E comebacks depend on A–C being stable.

---

## 12. Decisions on the open questions (answered 2026-08-19)

1. **How does a task agent run in tmux / how do we watch it? → the Gastown way.**
   Interactive CLI session (prompt as a shell argument, no `-p` print mode); watch via the
   harness's own transcript files + `capture-pane` peeks + pane activity; completion is the
   agent explicitly running `aq task close ...` then `aq session drain-ack`; process exit is
   treated as a failure signal. Folded into A.4/A.5/A.6.
2. **Worktrees → per-agent, inside the repo.** `<base_repo>/.aq/worktrees/<agent_slot>/`,
   hidden and ignored via `.git/info/exclude`; slot worktrees are reused across tasks with a
   fresh `aq/<task>` branch each time. Folded into W.1/W.2.
3. **Shared build caches → best judgment applied.** Per-kind `worktree_setup` commands run
   once at slot-worktree creation; slot reuse amortizes installs. No global cache system in v1.
4. **Supervisor scope → one per project, on-demand.** Wakes on first message, sleeps when
   idle, `--resume` keeps its conversation; an install-wide shared supervisor stays possible
   as pure config later. Folded into B.
5. **Discord adapter → separate process** over the REST/WS API.
6. **Old Supervisor code → keep switched off** as reference for building the new supervisor
   profile/prompt; delete no earlier than the playbook comeback.
7. **Hierarchical ids → yes** for graph children (`<parent>.1`, `<parent>.1.2`).
8. **Platform → WSL2/Linux only** for the daemon; confirmed.
9. **Cross-project dependencies → allowed, explicitly** — shown in `aq task explain` and the
   dashboard; readiness treats them like any other blocking edge (§3b).

No open questions remain for the Phase 0-2 specs; new ones get logged here as they appear.

---

## Appendix — Concept crosswalk (proposed)

| Gas City / Beads | Agent Queue (proposed) |
|---|---|
| session provider (`tmux`, `subprocess`, `fake`) | `SessionProvider` (`src/sessions/`) |
| harness profile (`builtin/profiles.go`) | `vault/harnesses/<name>.md` |
| agent (`agent.toml` + `prompt.template.md`) | `vault/agent-types/<id>/profile.md` (+ `lifecycle`, `mode`, `wake_mode`, `harness`) |
| named session (`mode=always`) / mayor | profile `lifecycle: long_lived, mode: always` / `supervisor` profile |
| `gc prime --hook` / `bd prime` | `aq prime --hook-json` |
| `gc handoff` | `aq handoff` |
| `gc runtime drain-ack` | `aq session drain-ack` |
| bead metadata `gc.outcome/work_outcome/…`, polecat `work_dir/branch/pr_url/rejection_reason` | task work-state contract |
| per-bead worktree + reaper + refinery | worktree-per-task + reaper + merge slot (W) |
| mail / nudge | `messages` table + provider `nudge` |
| gate bead / `bd gate` | `gates` table + cascade sweep |
| `gc doctor` / `bd doctor` | `aq doctor` |
| `gc session attach/peek/logs` | `aq session attach/peek/logs` + dashboard + Discord thread |
| formula / order (later) | playbook comeback candidates (§8) |
| extmsg adapters | thin Discord adapter over API |
