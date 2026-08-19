---
tags: [analysis, comparison, architecture, gascity, beads]
date: 2026-08-19
---

# Agent Queue vs. Gas City vs. Beads — Architecture Comparison

**Purpose.** A deep, code-grounded comparison of three systems that all try to make
fleets of AI coding agents do real engineering work durably: **Agent Queue** (this repo),
**Gas City** (`gastownhall/gascity`, orchestration SDK) and **Beads** (`gastownhall/beads`,
graph issue tracker / agent work ledger). Part I describes each architecture on its own
terms; Part II puts them side by side; Part III is the actionable list — what Agent Queue
should borrow, what it should *not* borrow, and where it is already ahead.

**Method.** Local clones were read directly: `agent-queue` (HEAD 2026-05-10, 1,767 commits),
`gascity` (HEAD `b3f125f4b`, 2026-08-19, 5,638 commits), `beads` (HEAD `0b2ac312`, 2026-08-19,
10,663 commits), plus the freshly `gc init`-ed instance at `../city`. Claims cite files.
Where something is inferred rather than read, it says so.

---

## 0. TL;DR

| | **Agent Queue** | **Gas City** | **Beads** |
|---|---|---|---|
| One-line | Self-improving orchestration daemon; Discord/MCP/CLI controlled | Role-free orchestration *SDK*: reconcile a fleet of CLI agents against declarative config, run formula graphs | Dolt-backed dependency-graph issue tracker designed as the durable "memory" and work frontier for agents |
| Language / size | Python asyncio, ~106k src LOC, ~137k test LOC | Go, ~581k src LOC, ~1.0M test LOC | Go, ~341k src LOC, ~456k test LOC |
| Unit of work | `tasks` row (11-state enum) + `task_dependencies` | **bead** (everything is a bead: task, mail, session, wait, gate, convoy, order-run) | **issue/bead** with typed edges, hash IDs, `is_blocked` projection |
| Workflow definition | Markdown **playbook** → LLM-compiled JSON graph of LLM decision nodes | TOML **formula** → deterministically compiled graph of beads + control beads, driven by the orchestrator | TOML **formula** → proto → **molecule** (epic + children), walked via `bd ready` |
| Scheduler | Deterministic 5 s cascade + deficit fair-share; launches subprocess per task | 30 s patrol: build desired state → reconcile sessions; pools sized by `scale_check`; work pulled via `work_query` | None — "the graph decides"; agents `bd ready --claim` |
| Agent runtime | `claude_sdk`, `acpx` (14+ ACP agents), in-process `supervisor` | tmux / subprocess / ACP / k8s / ssh / herdr / exec providers; 16 harness profiles | n/a (CLI used *by* agents) |
| Persistence | SQLite/Postgres (state) + Obsidian vault markdown (knowledge) + Milvus (memory) | Bead store (bd/Dolt, file, or exec) + `.gc/events.jsonl` | Dolt (versioned SQL; sync over `refs/dolt/data`) |
| Human-in-the-loop | Discord threads, WAITING_INPUT, PR/plan approval states, playbook `wait_for_human` | tmux attach/peek, mail, human gates, dashboard, extmsg fabric | `human` gates, `bd human`, comments/labels |
| Memory / learning | **Yes** — 4-tier scoped memory, reflection engine, consolidation playbooks | No learning loop; bead ledger + mail + prompt templates | `bd remember` KV injected by `bd prime`; LLM compaction of old closed issues |
| Distinctive strength | Closed self-improvement loop; single command surface; typed workspace model | Durability-by-construction (crash adoption, reconcile); zero hardcoded roles; exec orders | Ready-frontier correctness (CAS claims, leases, typed edges); context-frugal agent UX |
| Distinctive weakness | Single-process, advisory state machine, LLM-dependent playbook compile, Discord-centric UX | Enormous surface (60-field agent config, ~70 commands), tmux+Dolt ops burden, no learning | ~120 commands, Dolt-only, node-local leases, orchestration vestiges |

**Headline lessons for Agent Queue** (detail in Part III): (1) make the task graph richer
and *explainable* — typed edges, fan-in, persisted ready/blocked projection, `explain`;
(2) model gates/approvals/waits as blocking records in the graph rather than task statuses;
(3) add a deterministic, LLM-free "formula" layer that materializes task graphs, keeping
LLM playbook nodes for judgment only; (4) reconcile/adopt on restart instead of reset,
and replace wall-clock timeouts with heartbeats/leases + a nudge ladder; (5) add
zero-LLM "orders" for housekeeping and event automation; (6) cut the per-task context
tax (130 MCP tools injected per task) with a `prime`-style minimal surface and provider
hooks; (7) ship `aq doctor`, `aq task explain`, and importable vault "packs".

---

# Part I — Architectures

## 1. Agent Queue

### 1.1 Positioning and principles

A single-process, single-machine orchestration daemon for AI coding agents whose stated
differentiator is *"the system gets better with use"* (`CLAUDE.md`, `profile.md`,
`docs/specs/design/guiding-design-principles.md`). Ten principles drive the design: human-readable
files (the vault) are the source of truth and DB/Milvus are derived caches; **zero LLM calls
for orchestration**; "structure guides, intelligence decides" (playbooks = graphs of LLM
decision points); events over coupling (`src/event_bus.py`); "specificity wins" (project →
agent-type → system); plugins own their dependencies; fewer moving parts.

Target user: a solo developer or small team running several repos, managing agents from
a phone via Discord. Control surfaces — Discord bot (`src/discord/`), Telegram
(`src/telegram/`, `src/messaging/`), embedded MCP + FastAPI REST (`src/embedded_mcp.py`,
`src/api/app.py`), and the `aq` CLI (`src/cli/app.py`) — all route into one
`CommandHandler` (`src/commands/handler.py`, 13 mixins, **134 `_cmd_*` methods**), so feature
parity is structural.

### 1.2 Domain model

- **Task** (`src/models.py::Task`, table `tasks`): `id` (adjective-noun slug), `project_id`,
  `title`, `description`, `priority` (int, lower = higher, default 100), `status`,
  `verification_type` (`auto_test|qa_agent|human`), `retry_count`/`max_retries` (3),
  `parent_task_id` (plan subtasks), `assigned_agent_id`, `branch_name`, `resume_after`,
  `requires_approval`, `pr_url`, `task_type` (`feature|bugfix|refactor|test|docs|chore|research|plan|sync`),
  `profile_id`, `preferred_workspace_id`, `attachments`, `workflow_id`,
  `affinity_agent_id`/`affinity_reason`, `workspace_mode` (`exclusive|branch-isolated|directory-isolated` —
  the last is stubbed). Side tables: `task_criteria`, `task_context`, `task_metadata` (KV),
  `task_tools`, `task_results`, `task_workspace_requirements`, `archived_tasks`.
- **TaskStatus**: `DEFINED, READY, ASSIGNED, IN_PROGRESS, WAITING_INPUT, PAUSED,
  AWAITING_APPROVAL, AWAITING_PLAN_APPROVAL, COMPLETED, FAILED, BLOCKED` (11 states, 29
  `TaskEvent`s). `src/state_machine.py` defines the legal transitions but
  `db.transition_task()` **validates and logs only — it does not enforce** (documented in
  `models.py` and `profile.md`).
- **Dependencies**: `task_dependencies(task_id, depends_on_task_id)` — a real DAG with cycle
  check on insert, a single implicit edge type ("blocks"), promotion DEFINED→READY in
  `_check_defined_tasks`. Parent/child is a separate `parent_task_id` used for plan-generated
  subtasks. **No labels/tags.**
- **Project**: `credit_weight` (fair share), `max_concurrent_agents`, `budget_limit`,
  Discord channel ids, repo, `default_profile_id`; `project_constraints` are temporary
  scheduler limits set by playbooks/admins.
- **Agent**: a *project execution slot*, not a persona — `profile_id` is reassigned per tick by
  `AgentReconciler` (`src/orchestrator/agent_reconciler.py`), which lazily creates idle rows up
  to `max_concurrent_agents`. `agents.last_heartbeat` exists but nothing in the orchestrator
  writes it; liveness is a 30-minute `asyncio.wait_for`.
- **AgentProfile** (`agent_profiles`, source of truth in `vault/agent-types/<id>/profile.md`
  with project override): `model`, `permission_mode`, `allowed_tools`, `mcp_servers` (registry
  names), `system_prompt_suffix`, **`runtime`** (`claude_sdk|acpx|supervisor`), `agent_name`.
- **Workspaces v2**: `workspace_kinds` (`project-repo`, `vault`, `readonly-dir` + project kinds),
  `workspaces` bound by `kind_id` with lock columns, `task_workspace_requirements`. Acquisition is
  all-or-nothing in canonical `(kind_id, position)` order via compare-and-set `UPDATE … WHERE
  locked_by_agent_id IS NULL` (`src/orchestrator/workspace_attachments.py`,
  `workspace_queries.py`). Branch-isolated mode creates git worktrees.
- **Workflow** (`workflows`): runtime instance of a coordination playbook — `current_stage`,
  `task_ids`, `agent_affinity`, `stages` history. **PlaybookRun** (`playbook_runs`): status,
  `current_node`, `conversation_history`, `node_trace`, `pinned_graph`, `waiting_for_event`.
- Other tables: `token_ledger`, `rate_limits`, `events` (append log), `plugins`, `plugin_data`,
  `chat_analyzer_suggestions`, `system_config`, `repos`. 25 tables, 27 Alembic revisions.

### 1.3 Process model

`src/main.py` builds the `Orchestrator` (`src/orchestrator/core.py`, composed of
`WorkspaceMixin, ExecutionMixin, MonitoringMixin, GitOpsMixin, ApprovalMixin, ContextMixin,
EventsMixin, SyncWorkflowMixin`), the daemon-wide `Supervisor`, the runtime registry, the
messaging adapter and the embedded MCP/REST server, then calls `run_one_cycle()` every **5 s**:

1. **Promotion cascade** (order is load-bearing): `_check_awaiting_approval` →
   `_resume_paused_tasks` → `_check_defined_tasks` (deps met → READY) →
   `_check_plan_parent_completion` → `_check_stuck_defined_tasks` → `_check_failed_blocked_tasks`.
2. **Schedule & launch**: `AgentReconciler.reconcile()`; `_schedule()` snapshots projects,
   tasks, agents, token windows, free workspaces, locks, constraints, provider cooldowns into
   a `SchedulerState` and calls the pure `Scheduler.schedule()` (`src/scheduler.py`: deficit-based
   proportional allocation by `credit_weight`, `(priority, id)` ordering, four-tier affinity
   with `affinity_wait_seconds=120`). Each assignment becomes
   `asyncio.create_task(_execute_task_safe(action))`.
3. **Housekeeping**: `TimerService.tick()` (synthetic `timer.*`/`cron.*` events for
   playbooks), plugin cron, `VaultWatcher.check()` (mtime polling), `WorkspaceSpecWatcher`,
   `OrphanWorkflowRecovery.check_periodic()`, hourly LLM-log cleanup and auto-archive,
   paused-playbook timeout sweep.

**Execution pipeline** (`src/orchestrator/execution.py`): `asyncio.wait_for(stuck_timeout_seconds=1800)`
+ crash handling (timeout → BLOCKED, exception → READY, locks released); inside: assign,
IN_PROGRESS, resolve profile, `RuntimeRegistry.create(profile.runtime)`, acquire workspaces
(none free → PAUSED 60 s), open Discord thread, assemble prompt (`PromptBuilder` + L0/L1/L2
via the memory service), resolve MCP servers — **the daemon's own `agent-queue` MCP endpoint
is auto-injected into every task** (`config.mcp_server.inject_into_tasks=True`; 132 tool
definitions minus 5 exclusions in `src/mcp_registration.py`) — build `TaskContext`, run
`adapter.start/wait` with rate-limit backoff, record tokens, then branch on
`completed|failed|paused_tokens|paused_rate_limit|waiting_input`. COMPLETED runs the
completion pipeline (commit / plan-discover / verify / merge) which may yield
`AWAITING_APPROVAL` (PR polled via `gh`) or `AWAITING_PLAN_APPROVAL`.

**Runtimes** (`src/runtimes/`): `Runtime` ABC (`start/wait/stop/is_alive`, `Capability` enum,
`requires_workspace`). `ClaudeSDKRuntime` wraps the `claude` CLI via claude-agent-sdk (session
fork/resume, permission modes, MCP injection). `ACPXRuntime` spawns
`acpx --format json --approve-all <agent> exec <prompt>` and streams NDJSON to ~14 ACP agents.
`Supervisor` (`src/runtimes/supervisor.py`, 2.1k LOC) is both the chat brain (Discord/Telegram/CLI
conversations, playbook nodes, `break_plan_into_tasks`, `observe()` chat analyzer,
`reflect()`) and a tool-call-only runtime singleton.

**Crash recovery**: `_recover_stale_state` on startup resets BUSY agents → IDLE, releases all
workspace locks, deletes worktree workspaces, resets IN_PROGRESS → READY ("intentionally
aggressive"). `OrphanWorkflowRecovery` re-emits missed `workflow.stage.completed` events.

**Discord streaming**: orchestrator emits transport-agnostic `notify.*` events; the Discord
handler keeps one in-place-edited message per `stream_id`, decoupled by a per-stream worker
so 429 sleeps can't back up the serial EventBus (commit `89e20167`).

### 1.4 Playbooks, workflows, supervisor

Playbooks are markdown files in `vault/{system,projects/<id>,agent-types/<t>}/playbooks/*.md`
with YAML frontmatter (`triggers, scope, cooldown, max_tokens, llm_config`). The
`PlaybookCompiler` makes **one LLM call** to turn the markdown into JSON validated against
`src/playbook_schema.json` (nodes with `prompt`, `transitions[{when,goto,otherwise}]`,
`wait_for_human`, `for_each`, `timeout_seconds`, `summarize_before`). `PlaybookManager`
maps EventBus triggers (with payload filters) to playbooks, enforces per-scope cooldown and
`max_concurrent_playbook_runs=2`. `PlaybookRunner` walks the graph: each node is a
`Supervisor.chat()` call; transitions are a cheap separate LLM call unless `goto` is
unconditional or `when` is a structured expression; runs persist after every node; human
checkpoints pause the run (`PlaybookResumeHandler`).

Coordination workflows are playbooks that call `create_task` (with deps, affinity,
`workspace_mode`, `requires_kinds`), `set/release_project_constraint`,
`create_workflow`/`advance_workflow_stage`, then pause on `wait_for_event`;
`WorkflowStageResumeHandler` resumes them. The scheduler still owns all concurrency —
parallelism is emergent from the dependency DAG. Bundled playbooks: `memory-consolidation`
(timer.24h), `task-outcome`, `system-health-check`, `vibecop-weekly-scan`, `dependency-audit`,
`codebase-inspector`, agent-type `reflection`.

### 1.5 Memory and self-improvement

Two things are called "reflection": (a) `ReflectionEngine` (`src/reflection.py`) — the
Supervisor's post-action self-check with deep/standard/light tiers, `max_depth=3`, per-cycle
token caps and an hourly circuit breaker; (b) the playbook-driven loop in
`docs/specs/design/self-improvement.md`: `task.completed/failed` → reflection playbook writes
insights to `vault/agent-types/<t>/memory/`; `memory-consolidation` runs daily; the memory
plugin's extractor mines events.

Memory tiers (budgets in `src/prompt_builder.py`): L0 identity (~50 tok, profile `## Role`),
L1 facts (~200 tok, `facts.md` KV via `facts_parser.py`), L2 topic context (~500 tok, semantic
search on the task description), L3 explicit `memory_search`. Storage is the **external
`aq-memory` plugin** (memsearch fork over Milvus, one collection per scope:
`aq_system / aq_orchestrator / aq_agenttype_{t} / aq_project_{id}`). Core only ships the
parsers and calls `plugin_registry.get_service("memory")`. Prompt assembly order: L0 role →
project override → L1 facts → L1 guidance → L2 → identity template → project context →
context blocks (task, upstream, workspaces…). Memory health/audit metrics are specced but not
implemented in-tree.

### 1.6 Profiles, MCP registry, vault, plugins

Profile markdown is hybrid: English sections (`## Role`, `## Rules`) are injected; JSON blocks
(`## Config`, `## Tools`, `## MCP Servers`) are parsed deterministically and synced to
`agent_profiles`. MCP servers live only in the vault (`vault/mcp-servers/<name>.md`, project
shadows by name), loaded into `McpRegistry`, probed with a 10 s timeout. The vault
(`~/.agent-queue/vault/`) holds playbooks, profiles, MCP servers, workspace kinds, facts,
memory, notes, references, knowledge — all watched by mtime polling.

Plugins (`src/plugins/`): `Plugin`/`InternalPlugin` ABCs, `PluginContext`
(register command/tool/event/service, emit/subscribe, `invoke_llm`, KV data, `@cron`),
`TrustLevel` (`internal|external`), `PluginPermission`, registry with circuit breaker,
git-based install. Internal: `aq-files`, `aq-git`, `aq-notes`, `aq-vibecop`, an `inbox` Gmail
poller; external: `aq-memory`. Chat providers (Anthropic, Gemini, Ollama) and messaging
platforms are pluggable.

### 1.7 Engineering practice

Specs-first (`docs/specs/*.md`, `docs/specs/design/*.md`, 15.6k lines; "when spec and code
disagree, the spec is correct"), ruff, pytest-asyncio + xdist, 197 test files (~6.6k tests),
Alembic discipline. Observed docs drift: `profile.md` lists a `compiled_playbooks` table that
does not exist (compiled playbooks are JSON on disk via `src/playbooks/store.py`); the roadmap
summary table ("42 of 226 done") is stale relative to code; the workspaces spec's
dialect-specific locking (`BEGIN IMMEDIATE`/`SKIP LOCKED`) is not what the code does (portable CAS).

---

## 2. Gas City

### 2.1 Positioning and philosophy

Gas City (`gc`) is the **orchestration platform extracted from Gas Town**, Steve Yegge's
role-based multi-agent system. Its thesis (`AGENTS.md`): *"ZERO hardcoded roles… If a line of
Go references a specific role name, it's a bug."* Gas Town's Mayor/Deacon/Witness/Refinery/
Polecat/Crew/Dog become a **pack** (`examples/gastown/`, `gastownhall/gascity-packs`); Ralph
loops or Agent-Teams are other packs. Beads is its universal persistence substrate:
*"Everything is a bead: tasks, mail, molecules, convoys, and epics"* — and sessions, waits,
gates, order-tracking records too.

Other quoted rules that shape the code: *"Keep judgment out of Go. Go handles transport, not
reasoning… An `if stuck then restart` is framework intelligence. Move the decision to the
prompt."* The **Primitive Test** (`engdocs/contributors/primitive-test.md`): a primitive must be
atomic, must become more useful as models improve, and must keep judgment out of Go.
*"No status files — query live state; the process table is the single source of truth."*
*"Plugins become orders."* Permanent exclusions: no skills system, no capability flags, no
MCP/tool registration, no decision logic in Go. Commands are trusted operator code; bead text
is untrusted data (`docs/reference/trust-boundaries.md`).

### 2.2 The six primitives

From `docs/getting-started/how-gas-city-works.md`:

| Primitive | Role | Definition |
|---|---|---|
| **Agent** (WHO) | configured worker | name + provider (harness) + prompt template + scope (`city`/`rig`) + ~60 tunables. A running agent is a **session**; identical sessions form a **pool** sized each tick by `scale_check`, bounded by `min/max_active_sessions`. |
| **Bead** (WHAT) | unit of work | `ID, Title, Status (open/in_progress/closed), Type, Priority, Assignee, ParentID, Needs, Labels, Metadata, Dependencies, Ephemeral, DeferUntil, IsBlocked` (`internal/beads/beads.go`). A **convoy** is a container bead grouping work; blocking `needs` edges hide a bead from `ready` "which is how ordering happens with no central scheduler." |
| **Formula** (HOW) | written-down method | TOML file of `[[steps]]` with `needs`, `[vars]`, `extends`, `condition`, `loop`; applying it **materializes beads** so the run outlives the file and any session. |
| **Rig** (WHERE) | external project | a git repo registered with `gc rig add`; own bead-ID prefix and agent scope. |
| **Pack** (CONFIGURES) | unit of configuration | directory with `pack.toml` declaring `agents/ formulas/ orders/ skills/ mcp/ commands/ doctor/ template-fragments/ overlay/`; the city *is* the root pack; imports are pinned by `source` + `version="sha:…"`. |
| **Event** (OBSERVE) | append-only record | monotonic `seq`; ~90 typed events (`bead.created/closed`, `session.woke/crashed`, `convoy.*`, `order.fired/completed`…); `gc events --follow`, SSE; event-triggered orders close the loop. |

Underneath: **orchestrator** (controller), **bead store**, **event bus** — "none of this
machinery knows what your agents do."

Other vocabulary: **Order** = trigger (`cooldown|cron|condition|event|manual|webhook`) +
(formula ⊕ `exec` shell command), in `orders/<name>.toml`; *health patrol is one kind of
order*. **Sling** (`gc sling <target> <bead|formula|"text">`) = create + route in one motion.
**Hook** (`gc hook --claim`) = the agent's work slot, backed by its `work_query`. **Wait** =
durable, bead-backed session wait on a dependency bead. **Gate** = blocking condition bead
(timer / GitHub PR / human) swept mechanically every 30 s by the core pack's `gate-sweep`
order. **Mail** = `type=message` bead (unread = open, threads via labels). **Nudge** = text
injected into a live session, queued durably while asleep. **Handoff** (`gc handoff`) = mail
self a summary + restart the session. **Doctor** = ~68 diagnostic checks + pack checks, `--fix`.

### 2.3 Configuration model

Two files split definition from deployment: **`pack.toml`** (reusable definitions) and
**`city.toml`** (this deployment: rigs, providers, scale, patches). The local instance at
`../city` is minimal and pack-composed:

```toml
# city.toml
[workspace]       provider = "pi"
[providers.pi]    base = "builtin:pi"   ready_delay_ms = 0
[defaults.rig.imports.gc]
source = "https://github.com/gastownhall/gascity-packs/tree/main/gascity/roles"
version = "sha:3b3b89f2…"

# pack.toml
[pack]            name = "city"  schema = 2
[imports.bd]      source = ".../gascity/tree/main/examples/bd"                         version = "sha:f895c0f…"
[imports.core]    source = ".../gascity/tree/main/internal/bootstrap/packs/core"      version = "sha:f895c0f…"
[imports.gc]      source = ".../gascity-packs/tree/main/gascity"                      version = "sha:3b3b89f…"
[[named_session]] template = "mayor"  mode = "always"
```

Only `agents/mayor/prompt.template.md` is local; everything else comes from imported packs
(`core`: gc-* skills, `mol-do-work`/`mol-review-quorum` formulas, housekeeping orders, the
control-dispatcher agent; `bd`: Dolt provider, dog pool, backup/compaction orders; `gc`:
roles). `.gc/` holds `controller.lock`, `events.jsonl`, `sessions/<name>/start-stderr.log`,
runtime state; `.beads/` holds the Dolt data with `issue_prefix: ci`.

`city.toml` top-level tables (`internal/config/config.go`): `[workspace]`, `[providers.<name>]`
(`base="builtin:claude"`, `command`, `args`, `prompt_mode=arg|flag|none`, `ready_delay_ms`,
`supports_acp`, `resume_flag`, `options_schema`…), `[upstreams]`, `[imports.*]`, `[[rigs]]`,
`[patches]`, `[storage]` (six storage classes → bindings), `[beads]`
(`provider=bd|file|exec:`), `[session]` (`provider=tmux|subprocess|acp|exec:|k8s`),
`[mail]`, `[events]`, `[daemon]` (`patrol_interval=30s`, `max_restarts=5`,
`restart_window=1h`, `formula_v2=true`, `max_wakes_per_tick=5`…), `[orders]`, `[api]`,
`[convergence]`, `[doctor]`, `[[service]]`, `[[webhook]]`, `[github]`, `[extmsg]`,
`[agent_defaults]`, `[[pricing]]`. Layering: root city.toml → `[defaults.rig.imports]` → root
pack → includes → city imports → rig imports → city patches → rig overrides → pack globals →
formula layers → `agent_defaults`. Config revision = SHA-256 of all sources; fsnotify reload.

The `Agent` type alone has ~60 fields (`docs/reference/config.md`): `work_query`, `sling_query`,
`scale_check`, `min/max_active_sessions`, `drain_timeout`, `idle_timeout`,
`max_session_age(+jitter)`, `wake_mode=resume|fresh`, `on_boot`, `on_death`, `pre_start`,
`session_setup`, `session_live`, `overlay_dir`, `default_sling_formula`,
`inject_fragments`/`append_fragments`, `depends_on`, `resume_command`, `lifecycle=one_shot`,
`prompt_mode`, `option_defaults`, `upstream`, …

### 2.4 Runtime / process model

The canonical daemon is the machine-wide **supervisor** (`gc supervisor run`): flock on
`~/.gc/supervisor.lock`, unix socket + HTTP API/dashboard, city registry `~/.gc/cities.toml`,
one `CityRuntime` goroutine per city. `(*CityRuntime).run` (`cmd/gc/city_runtime.go`) boots
through retried phases — **adoption barrier** (every provider-reported live session gets a
session bead: crash adoption, never respawn) → config reload → startup orders → route
recovery → startup reconcile — then ticks on `patrol_interval` (30 s) or pokes. Per tick:
`on_death` hooks → config reload if dirty → FS-pressure gate → **order dispatch** (before
reconcile so formulas aren't starved) → route recovery → session-bead snapshot → corpse/
orphan/stale reaping (process-table scan) → demand snapshot → **build desired state** (named
sessions, pools via `scale_check`, bead-assigned work, waits) → **`reconcileSessionBeads`**
(start/stop/drain/wake, ≤`max_wakes_per_tick`) → execution completions → wisp GC → services →
convergence tick. Every tick runs in `safeTick` (panic-recovering).

Liveness is never read from PID files: `Provider.IsRunning/ProcessAlive/ListRunning`,
process-table scans for `GC_SESSION_ID`/`GC_CITY_PATH` env markers, and session beads.
"Unknown" (`ErrRuntimeUnavailable`) makes destructive arms defer.

**Providers** (`internal/runtime/runtime.go` `Provider` interface:
`Start/Stop/Interrupt/IsRunning/Attach/ProcessAlive/Nudge/Peek/ListRunning/GetLastActivity/…`):
`tmux` (default and fallback, `tmux -L <city>`), `subprocess`, `exec:<script>`, `acp`
(JSON-RPC over stdio), `k8s`, `ssh:<host>`, `hybrid`, `herdr`, `t3bridge`, `fake`. 16 harness
profiles (`internal/worker/builtin/profiles.go`).

**Session lifecycle** (`internal/session/`): explicit reducer `state_machine.go`
(`ErrIllegalTransition` → HTTP 409); `lifecycle_projection.go` separates persisted BaseState,
DesiredState (`undesired/desired-asleep/desired-running/desired-blocked`) and RuntimeProjection
(`alive/missing/fresh-creating/stale-creating`). **Idle sleep**: wake reasons are recomputed
each tick, never stored; with none, a session drains to `asleep`; `wake_mode=resume` reuses
the provider session key (`claude --resume`), `fresh` starts clean.

**Health patrol**: drift = `runtime.ConfigFingerprint()` mismatch → drain + restart;
crash-loop quarantine after `max_restarts` in `restart_window`; a per-session circuit breaker
persisted in bead metadata; stall ladders (`cmd/gc/idle_nudge.go`, `nudge_backstop.go`,
`execution_backstop.go`): observe → nudge (90 s grace) → backoff → give up after 3 → typed
event + drain. (The local instance shows this in action — 169 `bd__dog-ci-*` session dirs whose
`start-stderr.log` show the Pi harness crashing on Node 18, respawned repeatedly within an hour:
durable, but the patrol keeps feeding a crash loop until quarantine.)

### 2.5 Work routing and formulas

**Ready**: `status=open`, `DeferUntil` passed, type not in `readyExcludeTypes`
(`merge-request, gate, molecule, step, convoy, message, session, agent, role, rig`), no
`gc:session`/`gc:order-tracking` label, no open `blocks`/`waits-for`/`conditional-blocks`
edge. Holds are labels (`hold:mayor`, `hold:external`), filtered when deciding what an agent
should *do*, never when deciding which sessions must exist.

**How work reaches an agent**: each agent has an `EffectiveWorkQuery` — a generated shell
script with three tiers: assigned+in_progress (crash recovery) → assigned+ready → routed pool
demand (`bd ready --metadata-field gc.routed_to=$target --unassigned …`). Default
`sling_query` is `bd update {} --set-metadata gc.routed_to=<template>` — **routing is
metadata, not assignee**. Workers claim with `gc hook --claim` (`bd update <id> --claim`,
fenced to the live session token so a stale session cannot claim).

**Formulas**: v1 — the slung agent is the engine; steps become a molecule (root + children) or a
single-bead *wisp*. v2 (`[requires] formula_compiler=">=2.0.0"`) — `internal/formula/` emits a
workflow root, step beads linked by `blocks` edges, an injected `workflow-finalize` sink, and
**control beads** (`retry`/`ralph` loops with `gc.max_attempts`, `check`, `fanout` over
`gc.for_each`, `drain` scattering a convoy into ≤100 unit convoys, `scope-check`). Steps carry
`metadata = { "gc.run_target"="<agent>", "gc.provider"=…, "opt_model"=… }` so each step routes
to a different agent/pool. Agents report via metadata — `gc.outcome=pass|fail`,
`gc.failure_class=transient|hard`, `gc.work_outcome=shipped|no-op|blocked|abandoned`,
`gc.work_commit`. Control beads are themselves routed work executed by the core pack's
`control-dispatcher` agent (`gc convoy control --serve`). Verbs: `gc formula cook`
(materialize only), `gc sling … --formula`, orders. `gc formula show` previews; `gc formula
version-check` detects drift; `extends` composes bases (child step with same `id` overrides in
place); `[vars]` support `required`/`enum`/`default`.

**Orders** (`internal/orders/order.go`): `formula ⊕ exec`, `scope=city|rig`, `trigger`,
`interval`, `schedule`, `check`, `on` (event), `pool`, `timeout`, `idempotent`,
`no_work_gate`. Core pack exec orders: `gate-sweep` (30 s), `orphan-sweep` (5 m: reset beads
assigned to dead agents), `wisp-compact`, `prune-branches`, `cascade-nudge-on-blocker-close`
(on `bead.closed`), `notify-on-human-gate-creation`. All order state lives in tracking beads;
the local instance logged 1,006 fired / 899 completed / 106 failed in ~75 minutes.

### 2.6 Communication, context, storage, ops

- **Comms**: mail (`gc mail send/inbox/…`, injected on `UserPromptSubmit` via provider hooks),
  nudges (durable queue), handoff (mail-to-self + restart; `PreCompact` hook →
  `gc handoff --auto`), `gc session attach/peek/submit`, dashboard, `gc events --follow`,
  `internal/extmsg` fabric for external chat (Telegram/Discord adapters are *external*, over
  `/v0/extmsg/*`; no bot ships in-tree).
- **Context**: Go `text/template` prompt templates with `PromptContext` (CityRoot, AgentName,
  RigName, WorkQuery, …), `template-fragments/` `{{define}}` blocks, overlays that write
  `CLAUDE.md`/`AGENTS.md`, skills materialized into `.claude/skills/`, and `gc prime --hook`
  on `SessionStart`. **No reflection or self-improvement loop**; "the bead store is the
  memory."
- **Storage**: `[beads] provider = bd` (default; a managed Dolt `sql-server` per city, accessed
  through the beads Go library with the `bd` CLI as fallback), `file`, or `exec:`. Each rig has its own `.beads/` prefix; six semantic storage classes can be split
  across bindings. `.gc/events.jsonl` append-only, rotated at 256 MiB.
- **Ops**: ~90 typed events (every type must register a payload — CI-enforced); `gc doctor`
  (~68 checks, `--fix`); `gc trace` over reconciler traces; `gc session logs` parses provider
  transcripts; React dashboard embedded in the supervisor; Huma/OpenAPI 3.1 API (127 paths);
  `gc costs` with `[[pricing]]`.
- **Engineering**: 3,659 Go files (60% tests), 1.7:1 test:src ratio, `TESTING.md` (101 KB:
  p95 PR feedback < 5 min, async tests wait for facts not time, resource ledger ratchets),
  conformance suites binding every provider to one contract, 24 CI workflows, 275
  `release-gates/*.md` PASS/FAIL records, architectural invariants as tests
  (`TestEveryKnownEventTypeHasRegisteredPayload`, `TestOpenAPISpecInSync`,
  `TestGCNonTestFilesStayOnWorkerBoundary`). The project tracks its own work in beads.

### 2.7 Strengths / weaknesses

**Strengths**: genuinely role-free core with a six-primitive mental model; durable by
construction (crash adoption, reconcile-to-desired, all state in beads, Dolt history); v2
formula graphs with per-step routing and control beads are a real fleet workflow engine;
broad provider/harness coverage with an exec escape hatch on every plane; mechanical
housekeeping as LLM-free exec orders; exceptional engineering discipline.

**Weaknesses**: ~580k LOC with a 182k-line flat `cmd/gc` main package, ~70 commands, ~60
agent fields, many compat paths; tmux always required plus Dolt ≥ 2.1 + `bd` + `flock` + `jq`
for the default store (Dolt ops care is a recurring theme); Unix-only in practice; no learning
loop; "judgment-free Go" is partly aspirational (stall ladders, circuit breakers, drain
heuristics *are* `if stuck then restart` logic); many moving parts per agent turn (provider
hooks shelling out to `gc` on every prompt, nudge sidecars, event-driven nudge orders).

---

## 3. Beads

### 3.1 Positioning

*"A distributed graph issue tracker for AI agents, powered by Dolt."* The problem statement
(`docs/core-concepts/index.md`): *"Coding agents lose their memory every time a session ends.
Markdown plans rot, TODO comments scatter, and a crashed agent takes its context with it."*
The whole product loop: `bd create → dependency graph → bd ready → bd update --claim →
bd close → blockers released → bd ready`; *"the graph — not a human dispatcher — decides what
is workable next."*

`engdocs/PROJECT_CHARTER.md` draws three fences: an **orchestration boundary** ("Beads should
not know about orchestration layers built on top of it"), a **storage boundary** ("Dolt
provides storage, versioning, sync, merge behavior, concurrency, and crash safety" — enforced
by a `depguard` rule confining `dolthub/` imports to `internal/storage/`), and a **schema
boundary** ("Use issue metadata first"). Beads was born as Gas Town's data plane; 1.0 removed
Gas Town concepts ("Beads is now fully standalone"). Targets agents *and* humans (`bd human`
lists the ~15 commands people need; every command has `--json`).

### 3.2 Data model

`internal/types/types.go` (2,267 lines), public alias surface in `issueops/`:

- **Issue**: `ID`, `ContentHash`; `Title`, `Description`, `Design`, `AcceptanceCriteria`,
  `Notes`, `SpecID`; `Status`, `Priority` (0–4), `IssueType`, `IsBlocked` (persisted readiness
  projection); `Assignee`, `Owner`, `EstimatedMinutes`; `CreatedAt/By`, `UpdatedAt`,
  `StartedAt`, `ClosedAt`, `CloseReason`, `ClosedBySession`; leasing `LeaseExpiresAt`,
  `HeartbeatAt`, `LeaseGrantedNode`; `RowVersion` (the `row_lock` CAS cell); `DueAt`,
  `DeferUntil`; `ExternalRef`, `SourceSystem`; `Metadata json.RawMessage` (**the sanctioned
  extension point**); compaction fields; `Labels`, `Dependencies`, `Comments`; messaging
  `Sender`, `Ephemeral`, `NoHistory`, `StorageClass`; `Pinned`, `IsTemplate`; molecule/gate
  fields `BondedFrom`, `AwaitType` (`gh:run|gh:pr|timer|human|mail`), `AwaitID`, `Timeout`,
  `Waiters`, `SourceFormula`, `MolType`, `WorkType`.
- **Statuses**: `open, in_progress, blocked, deferred, closed, pinned, hooked` + custom
  statuses with a category (`active` ⇒ appears in ready). Dependency-blocked issues *stay
  `open`*; graph blocking is the `is_blocked` projection.
- **Types**: `bug, feature, task, epic, chore, decision, message, molecule, gate, spike,
  story, milestone` + custom.
- **Dependency types**: workflow (affect ready) `blocks`, `parent-child`,
  `conditional-blocks` (B runs only if A fails), `waits-for` (fan-in over dynamic children);
  association `related`, `discovered-from`; graph links `replies-to`, `relates-to`,
  `duplicates`, `supersedes`; also `tracks`, `caused-by`, `validates`, `delegated-from`;
  custom types allowed (≤ 32 chars).
- **IDs**: `sha256(title|description|creator|unixNano|nonce)` → base36, adaptive length
  3–8 chosen by a birthday-bound (`max_collision_prob 0.25`; 0–500 issues → 4 chars, 501–1500
  → 5, …); hierarchical children `parent.N` (depth ≤ 3); `engdocs/COLLISION_MATH.md`.
- **Labels** (with parent inheritance and `bd label propagate`), **comments**, **events**
  (`created, updated, claimed, status_changed, closed, reopened, dependency_added, …`
  written in the same transaction), **provenance events**, an `interactions.jsonl` audit log,
  and an opt-in cursor-resumable events journal ("a binlog, not a notification").

**Ready computation** (`internal/storage/sqlbuild/ready.go`): `status IN (open,in_progress)`,
`pinned=0`, `is_blocked=0`, `ephemeral=0`, `defer_until` passed, children of deferred parents
excluded, type not in (`merge-request, gate, molecule, rig, …`). `is_blocked` is recomputed to
a fixpoint **in-transaction** on every dependency/status change
(`issueops/blocked_state.go`); `bd ready --explain` returns `{Ready[], Blocked[], Cycles[][]}`.
Benchmarks: GetReadyWork on 10k issues ≈ 30 ms.

**Claim semantics** (`issueops/claim.go`): read pre-image in tx; check
`assignee == "" || actorMatches || assignee ∈ claim.pools`; then
`UPDATE … SET assignee=?, status='in_progress', row_lock=<fresh random> WHERE id=? AND
row_lock=? AND status IN (claimable)`. `rowsAffected==0` → re-read and classify: idempotent
re-claim by same actor succeeds; otherwise typed `ClaimConflictError`. The `row_lock` cell
exists because Dolt merges different cells of one row silently — rewriting one shared cell
forces a serialization conflict. A claim grants a **lease** (5 min TTL) in a node-local
table; `bd heartbeat` extends without a Dolt commit; `bd reclaim` reverts expired leases to
open. `bd ready --claim` takes the first claimable row, walking past rows racing agents took.
`--if-assignee/--if-status` give generic CAS. Close guards: `ErrCloseBlocked`,
`ErrCloseOpenChildren`. "Beads has no agent registry" — assignees are strings.

### 3.3 Storage and sync

**Dolt is the only backend** (embedded in-process via `dolthub/driver`, single writer under
flock; or `server` mode against `dolt sql-server` for multi-writer; a `proxied-server`
unit-of-work stack for a team server). Write discipline: every logical write is
`BEGIN … CALL DOLT_COMMIT … COMMIT` — one Dolt commit per command; `bd batch` collapses many.
The pluggable-backend effort (`PROPOSAL-pluggable-storage-backends.md`) was **rolled back**
("keep Beads as simple as possible"); what survived is the role-based `Storage` interface
(~60 methods + ~30 role accessors), the `backend/` seam and conformance suites. 65 schema
migrations; a schema-skew guard refuses to open a newer DB.

**Sync** rides the existing git remote under `refs/dolt/data` (`bd dolt push/pull`; also
DoltHub, S3, GCS, file). `.beads/issues.jsonl` is a *passive export*, not the sync protocol.
Git hooks (`bd hooks install`) refresh the export and add agent identity trailers.
**Federation** (peer-to-peer between workspaces, sovereignty tiers), contributor vs maintainer
routing (`bd init --contributor` → `~/.beads-planning`), stealth mode, `BEADS_DIR` git-free use.
`bd serve` exposes a loopback HTTP `/v0` API with OpenAPI spec and an events watch stream for
long-running clients.

### 3.4 Agent-facing surface and memory

- **`bd prime`**: "essential Beads workflow context in AI-optimized markdown"; detects MCP
  mode (~50 tokens) vs CLI mode (~1–2k tokens); `--hook-json` for SessionStart hooks
  (Claude Code / Gemini / Codex); injects memories (capped); `.beads/PRIME.md` overrides;
  carries the "SESSION CLOSE PROTOCOL" and *"Do NOT use TodoWrite, TaskCreate, or markdown
  files for task tracking."*
- **`bd setup <recipe>`** installs per-tool integration (claude hooks + CLAUDE.md block,
  codex skill, cursor rules, copilot, factory, aider, windsurf, kiro, …) in `full` or
  `minimal` (~60% smaller) profiles, with hash-marked managed blocks.
- **MCP server** (`integrations/beads-mcp`, Python) wraps the CLI — and its own README says:
  *"CLI + hooks approach is recommended over MCP. It uses ~1-2k tokens vs 10-50k for MCP
  schemas."*
- **JSON conventions**: `--json` everywhere, `JSONSchemaVersion`, optional envelope with
  pagination, `--brief` lite projections, `bd schema` publishes enums. Metadata carries
  execution hints (`execution_agent_type`, `execution_suggested_model`,
  `execution_reasoning_effort`, `execution_parallel_group`) — deliberately *not* first-class
  columns.
- **Memory**: `bd remember/recall/forget/memories` — keyed strings in the Dolt `config`
  table, injected wholesale by `bd prime`, substring search only, no scoping/embeddings.
- **Compaction** ("semantic memory decay"): opt-in `bd admin compact` calls an LLM
  (Haiku via `anthropic-sdk-go`) to summarize closed issues ≥ 30 days old into
  Summary/Key Decisions/Resolution, snapshotting originals for `bd restore`; tier 2 (90 d)
  unimplemented. (Confusingly, top-level `bd compact` squashes Dolt commits.)
- **Messages**: issues with `type: message`, `sender`, `assignee`=recipient, open/closed =
  unread/read, threads via `replies-to`; `bd mail` delegates to an orchestrator
  (`BEADS_MAIL_DELEGATE`).

### 3.5 Workflow machinery in beads itself

More than the charter implies: epics/hierarchy (`bd children`, `bd epic status/close-eligible`,
`bd graph`), **formulas → protos → molecules** (`bd cook`, `bd mol pour/wisp/bond/squash/
distill/current/progress`), **gates** (`bd gate create/check/resolve` over `human|timer|gh:run|
gh:pr|bead`), `waits-for` fan-in, **swarm** (`bd swarm validate/create/status` computes
ready-front waves and max parallelism — "COMPUTED from beads, not stored separately"),
**merge slot** (one exclusive-access bead per project), **wisps** (ephemeral, TTL classes),
`due_at`/`defer_until`, `bd stale`, `bd orphans`, `bd lint`, `bd human`, `.beads/hooks/`
on_create/on_update/on_close. **No scheduler**: `bd reclaim` "must be run from a supervisor
on a timer."

### 3.6 Engineering and surface

~120 root commands in `cmd/bd/` (332 files) — well past the project's own "30+ commands is a
discoverability problem" rule; overlapping names (`bd compact` vs `bd admin compact`, `bd tag`
vs `bd label add`, `bd link` vs `bd dep add`). Tests: contract tests across three storage
stacks via `backend/conformance`, a differential regression harness (found 70+ bugs during
the SQLite→Dolt move), benchmarks tied to PRs, schema-skew guard, cross-version CI.
Contribution rules are unusually explicit for AI-written PRs: "Prove the bug in beads itself…
no orchestrator", "One layer per PR", "A 13K-line diff will not be reviewed",
`Agent-Signature:` trailers, "Landing the Plane" (not done until `git push` succeeds).
Five tracker integrations (GitHub/GitLab/Jira/Linear/Notion/ADO), Homebrew/npm/PyPI/winget/
Nix distribution, native Windows.

### 3.7 Strengths / weaknesses

**Strengths**: graph-native ready frontier with transitive blocking, fan-in gates, conditional
edges and an in-transaction `is_blocked` projection; correct agent concurrency primitives (CAS
claims with typed conflicts, idempotent re-claim, leases/heartbeat/reclaim, claim pools,
`bd batch`); offline-first versioned storage with sync over the git remote; thought-through
agent ergonomics (`bd prime`, minimal-vs-full profiles, JSON schema versioning,
stdin/body-file for shell-hostile text, metadata execution hints); candid docs.

**Weaknesses**: sheer surface area; Dolt coupling (CGO for embedded, server mode for
multi-writer, history hygiene); node-local leases; orchestration vestiges contradict the
charter; memory is simple KV; docs occasionally lag code; three storage stacks with
per-command duals.

---

# Part II — Side by side

## 4. Concept mapping

| Concern | Agent Queue | Gas City | Beads |
|---|---|---|---|
| Unit of work | `tasks` row; 11 statuses | bead (`open/in_progress/closed`) | issue/bead (`open/in_progress/blocked/deferred/closed/pinned/hooked`+custom) |
| Grouping | `parent_task_id` (plan subtasks), `workflows` | convoy bead, epic, molecule root, workflow root | epic (`parent.N` ids), molecule, convoy (`tracks`) |
| Ordering | `task_dependencies` (one edge type) | `needs` → `blocks` edges; `waits-for`, `conditional-blocks` | `blocks`, `parent-child`, `conditional-blocks`, `waits-for` + non-blocking link types |
| "What can run now" | cascade promotes DEFINED→READY; scheduler picks | `bd ready` (+ `gc.routed_to` metadata, hold labels) | `bd ready` over persisted `is_blocked` |
| Claiming | orchestrator assigns (`assign_task_to_agent`) | `gc hook --claim` → `bd update --claim` fenced to session token | `bd update --claim` CAS on `row_lock`; leases |
| Worker identity | agent = project slot, profile per task | agent = config template; session = running instance (a bead) | assignee string; no registry |
| Role definition | `vault/agent-types/<id>/profile.md` (+ project override) | `agents/<name>/{agent.toml,prompt.template.md}` in packs | n/a |
| Project | `projects` row + Discord channels + repo | rig (`[[rigs]]`, own bead prefix) | one `.beads/` per repo; routing/federation across |
| Workflow definition | markdown playbook → LLM-compiled JSON | TOML formula (v1 molecule / v2 graph + control beads) | TOML formula → proto → molecule |
| Workflow execution | `PlaybookRunner` (LLM per node) + scheduler | orchestrator + control-dispatcher over beads; agents work steps | agent walks `bd ready --mol` |
| Human gate | `AWAITING_APPROVAL`, `AWAITING_PLAN_APPROVAL`, `WAITING_INPUT`, playbook `wait_for_human` | human gate bead + mail/nudge order; `gc session attach` | `gate` issue `await_type=human`; `bd human` |
| External gate (CI/PR) | `_check_awaiting_approval` polls `gh` | `gh:pr`/`gh:run`/timer gates swept by `gate-sweep` order | `bd gate check` |
| Scheduled / event automation | playbook triggers (`timer.*`, `cron.*`, events) + plugin `@cron` | orders: `cron|cooldown|condition|event|webhook|manual`, exec ⊕ formula | none (hooks only) |
| Agent runtime | `claude_sdk`, `acpx`, `supervisor` | tmux/subprocess/acp/k8s/ssh/herdr/exec | n/a |
| Workspace isolation | `workspace_kinds` + locks; worktrees | per-bead/per-agent worktrees under `.gc/worktrees/`; `work_dir` | `bd worktree`; merge slot |
| Restart behaviour | reset BUSY/IN_PROGRESS, release all locks | adopt live sessions; reconcile to desired | leases expire → `bd reclaim` |
| Liveness | 30-min `wait_for`; `last_heartbeat` unused | process table + provider `IsRunning`; idle/progress stall ladders | lease TTL 5 m + `bd heartbeat` |
| Agent↔agent comms | none (Discord threads; `provide_input`) | mail beads, nudges, handoff | message issues, comments, labels |
| Context delivery | 5-layer prompt builder (L0–L2 memory) + MCP server injection | prompt templates + fragments + overlays + `gc prime` hook + mail inject | `bd prime` via SessionStart hook, `AGENTS.md` block |
| Memory | scoped 4-tier Milvus (plugin) + facts.md + reflection | bead ledger; none | `bd remember` KV; LLM compaction |
| Events | `events` table + EventBus + websocket | `.gc/events.jsonl` with `seq`, SSE, ~90 typed events | events table, journal, `bd events` |
| Extensibility | plugins (tools/commands/events/cron), runtimes, chat providers | packs (agents/formulas/orders/skills/commands/doctor), exec providers | hooks, metadata, `backend/` seam, tracker integrations |
| Config | `~/.agent-queue/config.yaml` + vault markdown | `city.toml` + `pack.toml` + imports + patches | `.beads/config.yaml`, `bd config` |
| Primary UX | Discord (threads, streaming), CLI, MCP | CLI, tmux attach, dashboard, API | CLI (`--json`), MCP, HTTP |
| Storage | SQLite/Postgres + vault + Milvus | bd/Dolt or file or exec; events JSONL | Dolt only |
| Tests | ~6.6k pytest | 1.0M LOC Go tests, conformance, release gates | 456k LOC Go tests, conformance, differential |

## 5. Similarities

1. **Durable work outside the agent's context window.** All three reject "the plan lives in
   the chat". AQ: tasks/workflows/playbook runs in SQL + vault; GC/Beads: beads.
2. **Files as configuration, human-editable.** AQ vault markdown; GC TOML packs + markdown
   prompt templates; Beads TOML formulas and `PRIME.md`. All three treat human-readable files
   as authoritative over runtime caches.
3. **Dependency DAGs drive ordering, not a human dispatcher.** AQ's cascade promotes tasks
   whose deps are met; GC/Beads compute the ready frontier from `blocks` edges.
4. **Written-down multi-step methods** — playbooks / formulas — reusable across runs, with
   variables and human checkpoints.
5. **Human-in-the-loop as a first-class pause.** AQ statuses + Discord; GC human gates + mail;
   Beads human gates.
6. **Event streams for observability and automation.** AQ EventBus + `events` table feed
   playbooks; GC events feed orders and the dashboard; Beads events/journal feed hooks.
7. **Multi-harness ambition.** AQ's `acpx` runtime and GC's providers both target Claude,
   Codex, Gemini, Cursor, etc.; Beads ships setup recipes for 14+ tools.
8. **Agent-generated PR/merge workflows** with worktree isolation and PR gating.
9. **Deterministic orchestration core, LLM at the edges.** AQ's principle #2 and GC's "judgment
   out of Go" are the same instinct; Beads has no orchestration at all.
10. **Scoped/layered configuration with "most specific wins".** AQ project → agent-type →
    system; GC city patches → rig overrides → pack defaults.
11. **Serious test investment** relative to codebase size (AQ 1.3:1, GC 1.7:1, Beads 1.3:1
    test:src LOC).

## 6. Differences

### 6.1 Philosophical

| Axis | Agent Queue | Gas City | Beads |
|---|---|---|---|
| What improves over time | **The system** (memory, reflection, consolidation) | The *models* ("a primitive must become more useful as models improve") | The *graph* (compaction, links) |
| Where judgment lives | LLM in playbook nodes, Supervisor, compile step | Prompts only; orchestrator is transport | Nowhere (data plane) |
| Roles | Profiles are config, but Supervisor/plan-parser/reflection are built-in roles | Zero roles in code, period | None |
| Control plane | Chat-first (Discord/Telegram), "manage from your phone" | CLI/tmux-first, operator-centric, dashboard | CLI-first, agent-centric |
| Scope | One machine, one daemon, N projects | Machine-wide supervisor, N cities, N rigs, remote providers (k8s/ssh) | One repo (federation across) |
| Simplicity stance | "Favor fewer moving parts" | Maximal configurability, many escape hatches | "Keep beads as simple as possible" (yet 120 commands) |
| Source of work | Humans via chat, playbooks, plan parser | Humans via `gc sling`/mayor, orders, formulas | Humans and agents via `bd create` |

### 6.2 Technical

1. **Work graph richness.** Beads/GC: typed edges (`blocks`, `parent-child`,
   `conditional-blocks`, `waits-for`, provenance links), labels, metadata, persisted
   `is_blocked`, `ready --explain`, cycle detection at write time, swarm wave analysis.
   AQ: one implicit edge type + parent pointer, no labels, readiness computed by scanning
   each cycle, blockers only in logs (`_log_scheduler_blockers`).
2. **Status model.** AQ encodes *why* a task is waiting in its status (11 values: approval,
   plan approval, input, paused, blocked…). GC/Beads keep three statuses and push "why" into
   the graph (gate beads, wait beads, hold labels, `defer_until`, `is_blocked`).
3. **Workflow compilation.** AQ: LLM compiles markdown → graph, LLM evaluates transitions;
   non-deterministic, costs tokens, needs cooldown/caps. GC/Beads: deterministic TOML compile,
   previewable, versionable, composable via `extends`, vars validated before any bead exists.
   The *execution* also differs: AQ's playbook run is a conversation (history carried node to
   node inside one Supervisor); GC's v2 run is a graph of durable beads each worked by
   whichever agent/pool the step routes to, with control beads for retry/fan-out/finalize.
4. **Restart semantics.** AQ resets state on boot (IN_PROGRESS → READY, locks wiped,
   worktrees deleted). GC adopts live sessions and reconciles; Beads uses leases. AQ's approach
   is simpler and safe only because its subprocesses die with the daemon.
5. **Liveness.** AQ: wall-clock 30-min timeout per task; GC: process-table + activity +
   stall ladders with nudge-before-kill; Beads: lease heartbeat.
6. **Zero-LLM automation.** GC exec orders and Beads hooks do housekeeping without tokens. AQ's
   only non-LLM automation is hardcoded cycle steps + plugin `@cron`; anything declarative
   (playbooks) costs an LLM call per node even for mechanical work
   (e.g. `system-health-check`, `memory-consolidation` orchestration).
7. **Context delivery cost.** AQ injects ~130 MCP tool schemas into every coding agent by
   default; Beads explicitly measures and warns that MCP schemas cost 10–50k tokens vs ~1–2k
   for CLI+hooks; GC delivers context as templates/files/hooks (no tool registry by design).
8. **Inter-agent messaging.** GC mail/nudge/handoff and Beads messages are durable records
   injected at the next turn; AQ has no agent↔agent mailbox (communication goes through the
   orchestrator and Discord threads).
9. **Storage.** AQ: three stores (SQL, vault, Milvus) each with a clear role. GC/Beads: one
   store for all work (Dolt/bd), versioned with history, synced via git remotes; heavier ops.
10. **Distribution of knowledge.** GC packs are pinned git imports of agents+formulas+orders
    shared across cities; AQ's vault is per-install (starter packs are on the roadmap, plugins
    are importable but knowledge/profiles are not).
11. **Observability tooling.** GC: doctor (~68 checks, `--fix`), trace, typed events with
    schemas, dashboard, costs; Beads: doctor, `ready --explain`, swarm, benchmarks. AQ:
    health endpoints, llm logs, pipeline views, chain health — fewer self-diagnosis tools.
12. **Security model.** GC documents trust boundaries and strips secret-looking env from
    helpers; AQ relies on plugin trust levels and SDK permission modes; no written trust model
    for playbook-authored commands or env propagation.
13. **What AQ has that neither has**: scoped semantic memory with tiers, a reflection engine,
    consolidation playbooks, facts.md, a chat analyzer that proposes tasks from conversation,
    a typed multi-kind workspace model with ordered all-or-nothing acquisition, plugin trust
    levels, a single command surface mirrored to Discord/MCP/REST/CLI, multi-provider LLM for
    internal calls, Discord live streaming.

---

# Part III — What Agent Queue can learn

Each item names the source of the idea, the AQ modules it touches, and a rough
impact/effort. Items are grouped; within groups they are ordered by value. A summary table
is at the end of the section.

## 7. Work model

### 7.1 Typed dependency edges + fan-in/conditional edges  (Beads, GC)
`task_dependencies` has one implicit meaning. Add `dep_type` (`blocks` default,
`parent-child`, `waits-for` for fan-in over dynamic children, `conditional-blocks` for
"run only if A fails", non-blocking `discovered-from`/`related`/`duplicates`/`supersedes`).
`are_dependencies_met` (`src/database/queries/dependency_queries.py`) then consults only
blocking types. This directly answers the agent-coordination spec's own complaint that "the
dependency system is purely sequential" and gives plan subtasks a provenance edge
(`discovered-from`) instead of overloading `parent_task_id`. Migration: add column with
default `'blocks'`; no behaviour change for existing rows.
*Impact high / effort medium.*

### 7.2 Persist the readiness projection; `aq task explain`  (Beads `is_blocked`, `bd ready --explain`; GC `trace reasons`)
Today readiness is recomputed by scanning DEFINED tasks every 5 s and the reasons a READY task
was not scheduled go to logs (`_log_scheduler_blockers`). Persist `is_blocked` recomputed
in-transaction on dependency/status change (cheap for AQ's scale, but it makes the state
queryable and indexable), and add `explain_task` / `aq task explain <id>` returning a
structured reason list: open blockers, workspace unavailable, project cap, budget, constraint,
affinity wait, provider cooldown, approval pending. Also `aq project ready` = the frontier.
*Impact high (debuggability) / effort low–medium.*

### 7.3 Labels + metadata as the sanctioned extension point  (Beads schema boundary, GC routing/holds)
Add a `task_labels` table (AQ has `task_metadata` KV but no labels/filters) with label filters
on `list_tasks`/`ready`. Adopt the rule "prefer metadata before adding first-class columns":
e.g. execution hints (`execution_model`, `reasoning_effort`, `parallel_group`) and holds
(`hold:human`, `hold:budget`) as labels/metadata instead of new columns or statuses. GC's
insight that routing (`gc.routed_to`) and holds are metadata — filtered when deciding what to
*do*, never when deciding what must *exist* — is a good discipline for the scheduler.
*Impact medium / effort low.*

### 7.4 Gates/waits as blocking records, not task statuses  (GC gates & waits, Beads gate issues)
`AWAITING_APPROVAL`, `AWAITING_PLAN_APPROVAL`, `WAITING_INPUT`, parts of `PAUSED`, and playbook
`wait_for_human`/`wait_for_event` all encode "something external must happen". Model a single
**gate** entity (`type = human | timer | pr-merged | ci-run | event | bead`, `await_id`,
`timeout`, `waiters`) that *blocks* dependents through a normal edge; the cascade's one
`gate-sweep` step closes satisfied gates (poll `gh`, check timers, match events). Benefits:
the status enum shrinks (11 → ~6: DEFINED/READY/ASSIGNED/IN_PROGRESS/COMPLETED/FAILED), the
same mechanism serves tasks, workflows and playbooks, gates are visible in `explain`, and
"approve" becomes "resolve gate" everywhere (Discord button, CLI, MCP). Keep the existing
statuses as a compatibility projection during migration.
*Impact high / effort high (touches state machine, cascade, approvals mixin, Discord views).*

### 7.5 Enforce the state machine  (GC session reducer → `ErrIllegalTransition`/409)
`transition_task` validates and logs but applies anyway. Flip it to enforce (with an explicit
`force=True` admin override), one transition at a time behind a feature flag, fixing call sites
that currently rely on direct `update_task(status=…)`. GC's `lifecycle_projection.go` split
(persisted base state vs desired state vs runtime projection) is also a useful pattern for the
agent row: `agents.state` today mixes "what it is" and "what we want".
*Impact medium (correctness) / effort medium.*

### 7.6 Outcome metadata from agents  (GC `gc.outcome`, `gc.failure_class`, `gc.work_outcome`)
`task_results` records success/failure; retries are counted, not classified. Ask runtimes to
report `outcome=pass|fail`, `failure_class=transient|hard`,
`work_outcome=shipped|no-op|blocked|abandoned`, `work_commit` (via a tiny MCP tool or a
trailing JSON block), and make the retry policy consult `failure_class` (transient → retry
with backoff; hard → BLOCKED immediately with the reason). This also improves the reflection
inputs.
*Impact medium / effort low.*

### 7.7 Epics/convoys: a generic group with computed progress  (GC convoy, Beads epic/swarm)
`parent_task_id` + `_check_plan_parent_completion` already approximate an epic for plan
subtasks. Generalize: any task may be a container; `get_group_progress` computes
ready/blocked/done waves from the graph ("status computed, not stored" — Beads swarm); the
`workflows.stages` JSON could then be derived rather than duplicated.
*Impact low–medium / effort low.*

## 8. Orchestrator loop and runtime

### 8.1 Reconcile and adopt on restart instead of reset  (GC adoption barrier, "no status files")
`_recover_stale_state` resets IN_PROGRESS → READY, wipes locks and deletes worktrees. With
`claude_sdk` session ids and `acpx` session handles already recorded in `task_metadata`
(`last_session_id`), a restart could: (a) leave tasks whose session can be *resumed* in
IN_PROGRESS and resume them with the stored session id instead of starting over; (b) keep
worktrees whose branch has uncommitted work; (c) only release locks of tasks that are truly
dead. Build a `desired` vs `observed` diff at boot like `reconcileSessionBeads` rather than a
blanket reset. Also: persist the circuit-breaker/restart counters per task (GC stores them in
bead metadata) so a crash-looping task is quarantined across daemon restarts.
*Impact high (wasted work, tokens) / effort medium.*

### 8.2 Heartbeats/leases instead of a 30-minute wall clock  (Beads leases, GC activity stalls)
`agents.last_heartbeat` exists but is never written. Update it from every runtime stream
message (`on_message`), and define a lease: no message for `lease_ttl` (e.g. 5–10 min) →
*progress stall*, not death. Then a reaper step in the cascade (like `bd reclaim` /
GC `orphan-sweep`) decides per task. The 30-min `stuck_timeout_seconds` becomes a backstop.
*Impact medium–high / effort low.*

### 8.3 Stall ladder: observe → nudge → backoff → kill  (GC `idle_nudge.go`, `nudge_backstop.go`)
Before killing a stalled agent, inject a nudge ("no progress for N minutes — summarize status,
finish, or call `ask_human`"). `claude_sdk` and `acpx` both support mid-session input
(`provide_input` exists for WAITING_INPUT). Typed events `task.stalled` → `task.nudged` →
`task.killed` make the ladder observable and playbook-triggerable.
*Impact medium / effort low–medium.*

### 8.4 Per-profile session caps and demand-driven pools  (GC `min/max_active_sessions`, `scale_check`)
The deferred "category filter" (review agents vs coding agents) maps cleanly onto GC's per-agent
caps nested under rig/workspace caps. Add optional `max_active` (and `min_active` for warm
standby) to profiles; the scheduler already has `max_agents_by_type` in `project_constraints`
— promote it to a profile field resolved project → system. A `demand_query` per profile
(SQL/command returning how many READY tasks want this profile) would let AQ size slots to
demand rather than to the static `max_concurrent_agents`.
*Impact medium / effort medium.*

### 8.5 Zero-LLM "orders" in the vault  (GC exec orders; "plugins become orders")
AQ's principle #2 is zero-LLM orchestration, yet declarative automation today is only
playbooks (LLM per node) or Python plugin `@cron`. Add an **order** primitive:
`vault/[projects/<id>/]orders/<name>.md` with frontmatter `trigger` (`cron`, `interval`,
`event` + payload filter — reuse `PlaybookManager`'s trigger matching, `condition` command,
`cooldown`, `manual`), and an `action` that is either a CommandHandler command with args or a
playbook id (agent-driven). Move the hardcoded housekeeping in `run_one_cycle()` (archive,
LLM-log cleanup, orphan recovery, stuck checks, gate sweep from 7.4, worktree prune) into
default system orders so they are visible, tunable, and disable-able — exactly what GC's
`core` pack does. Record each firing as an event (`order.fired/completed/failed`).
*Impact high (aligns with stated principles, cuts LLM spend) / effort medium.*

### 8.6 Event log replay cursor and typed payload registry  (GC `seq`, `--after-cursor`, CI-enforced payloads)
`events.id` is already monotonic; expose `list_events --after <id>` and a websocket
`after_seq` so external watchers and orders resume without loss, and turn the Phase-0 event
schema registry into a test: every emitted event type has a registered payload schema
(`TestEveryKnownEventTypeHasRegisteredPayload`).
*Impact low–medium / effort low.*

## 9. Workflows and playbooks

### 9.1 A deterministic formula layer that materializes task graphs  (GC v2 formulas, Beads molecules)
Keep markdown playbooks for **judgment** (triage, review verdicts, plan breakdown), but add a
**formula**: a structured definition (YAML frontmatter or a fenced `yaml`/`toml` block in the
same markdown file) with `steps[]{id,title,profile,needs,vars,condition,gate,retry}` that
compiles *without an LLM* into real tasks + typed edges + gates in one transaction
(`create_formula_run` → tasks). The run is then just the task graph: crash-safe, visible in
`aq task list --group <run>`, schedulable by the existing deterministic scheduler, with
per-step profiles (GC's `gc.run_target`) and per-step runtimes (`claude_sdk` vs `acpx:codex`
vs `supervisor`). Include `vars` with `required/enum/default` validated before anything is
created, `extends` for composition, `aq formula show` to preview the graph, and a
`--dry-run`. This is the cleanest fix for two known AQ gaps at once: LLM-dependent, costly
playbook compilation for *mechanical* pipelines, and workflow state (`workflows.stages`)
duplicated outside the task graph. Coordination playbooks can then become a few formula files
plus small judgment playbooks at review points.
*Impact high / effort high (new compiler ~1k LOC, but reuses tasks/deps/scheduler).*

### 9.2 Control nodes: retry-with-class, check loops, fan-out, fan-in, finalize  (GC control beads)
With 7.1 and 9.1 in place, add formula constructs: `retry {max_attempts, on: transient}`,
`check` (a step whose fail re-opens its predecessor up to N times — GC's "ralph" loop; AQ's
`reopen_with_feedback` already forks the session), `for_each` over a runtime-discovered set
(GC `drain`, AQ playbooks already have `for_each`), `waits-for` fan-in, and an auto-appended
`finalize` step that closes the group and emits `workflow.completed`.
*Impact medium–high / effort medium (after 9.1).*

### 9.3 Make playbook compilation deterministic where possible; LLM as author-assist  (GC "Primitive Test")
Today the compiler's LLM call is the only way from markdown to graph. Accept a structured
graph block in the playbook file (`## Graph` fenced YAML) that compiles deterministically,
and offer `aq playbook draft "<description>"` where the LLM *writes* that block for the human
to commit. Also prefer structured `when` expressions over natural-language transitions in
shipped playbooks. Result: reproducible compiles, no `compile` token cost, reviewable diffs.
*Impact medium / effort low–medium.*

### 9.4 Queue playbook runs instead of rejecting at the concurrency cap
`max_concurrent_playbook_runs=2` rejects triggers beyond the cap; GC orders and Beads
molecules simply wait in the store. Persist pending runs (`playbook_runs.status='queued'`)
and let the cascade start them.
*Impact low / effort low.*

## 10. Agent-facing surface and context

### 10.1 Cut the per-task MCP tax; add `aq prime`  (Beads `bd prime`, MCP-vs-CLI measurement)
Every task gets the `agent-queue` MCP server with ~127 tool schemas by default. Beads measured
10–50k tokens for MCP schemas vs 1–2k for CLI+hooks; for a coding agent that mostly needs
`memory_save`, `memory_search`, `ask_human`, `report_progress`, `create_subtask`,
`get_task`, that is pure context tax on *every* task. Two changes: (a) a **task-scoped tool
surface** — a small default allowlist (profile `## Tools` can widen it) registered as a
second MCP endpoint or filtered per session; (b) an `aq prime` command (and a
`bd prime`-style minimal markdown block) that tells the agent how to use the `aq` CLI for
everything else, delivered via provider hooks (10.2). Measure before/after with the existing
`prompt_analytics.jsonl`.
*Impact high (cost + quality per task) / effort low–medium.*

### 10.2 Provider hooks: SessionStart, PreCompact → handoff, UserPromptSubmit → inject  (Beads `bd setup`, GC hooks + `gc handoff`)
For `claude_sdk` (and `acpx` agents that support hooks), install hooks into the task's
workspace or settings: `SessionStart` → `aq prime --hook-json` (re-inject L0/L1 + task
pointer after compaction — AQ's context is currently delivered once, up front, and is lost on
compaction); `PreCompact` → write a handoff note into `task_context`; `UserPromptSubmit` →
inject pending human replies/mail. GC's `handoff` (mail yourself + restart with the note) is
a clean protocol for AQ's `paused_tokens` path: restart the task with a fresh session whose
prompt is the handoff note instead of the full original prompt.
*Impact medium–high / effort medium.*

### 10.3 Durable inter-agent messages  (Beads message issues, GC mail/nudge)
Multi-agent workflows (code → review → fix) need a way for one task to leave a note for
another's next turn without a human relay. A `messages` record (`from_task`, `to_task|to_profile`,
`thread`, `read_at`) injected into the recipient's next prompt/turn (via 10.2 or the prompt
builder), plus `send_message`/`inbox` commands. Reviews can then route feedback to the
original coding agent (affinity already exists) as a message rather than a reopen.
*Impact medium / effort medium.*

### 10.4 JSON output discipline for the CLI  (Beads `--json`, envelope, schema version, `--brief`)
`aq` already talks to REST; make `--json` universal with a versioned envelope
(`schema_version`, `data`, `pagination`), `--brief` projections that skip descriptions, and
`aq schema` that prints enums (statuses, task types, dep types) so agents never guess.
*Impact low–medium / effort low.*

## 11. Knowledge distribution and configuration

### 11.1 Importable vault packs with pinned versions  (GC packs/imports, roadmap 4.3 "starter knowledge packs")
Let a vault scope be imported from a git URL at a pinned sha: profiles, playbooks, formulas,
orders, facts, workspace kinds, MCP server definitions — `aq pack import <url>@<sha>`,
`aq pack list`, lockfile, local overrides shadow imported files by path (AQ's project-shadows-
system rule already does this). This is how GC shares Gas Town as config and how AQ could ship
"starter agent types" or team conventions without copying.
*Impact medium–high / effort medium.*

### 11.2 Split "definition" from "deployment" in config  (GC `pack.toml` vs `city.toml`)
AQ's `config.yaml` mixes machine deployment (tokens, ports, Discord ids) with behaviour
defaults (scheduling, reflection, playbooks). Packs (11.1) give behaviour a home; keep
`config.yaml` for deployment. A `config explain <key>` (GC `gc config explain`) that shows
the resolved value and which layer supplied it would help with the vault's shadowing rules.
*Impact low–medium / effort low.*

### 11.3 Template fragments / prompt composition  (GC `template-fragments`, `append_fragments`, overlays)
The prompt builder has fixed layers; shipped text lives in `src/prompts/`. Allow vault-level
named fragments (`vault/[projects/<id>/]fragments/<name>.md`) that profiles list in
`append_fragments`, so teams compose prompts without editing Python. Provider-specific
fragments (GC's `templateFirst . "…-claude" "…-default"`) would let one profile speak
`claude_sdk` and `acpx:codex` dialects.
*Impact low–medium / effort low.*

## 12. Operations and engineering

### 12.1 `aq doctor --fix`  (GC doctor ~68 checks + pack checks; Beads doctor)
Checks: config validity, DB/migration head, vault parse errors (profiles, playbooks, kinds,
MCP files), playbook compile failures, MCP probe failures, stale workspace locks, orphan
worktrees, SQLite WAL size, LLM log size, missing binaries (`claude`, `acpx`, `gh`, `git`),
Discord connectivity, memory plugin health, tasks stuck > threshold, playbook runs paused
past timeout. Plugins/packs contribute checks. Many of these exist as scattered commands;
the win is one entry point with `--json` and `--fix`.
*Impact medium–high / effort low–medium.*

### 12.2 Invariant tests and docs-sync tests  (GC CI invariants, Beads docsync)
Cheap tests that would have caught the drift found during this review: every table in
`tables.py` is listed in `profile.md`/`docs/specs/database.md` (and vice versa); every
`_cmd_*` has a docstring-derived schema and appears in MCP registration or the exclusion
list; every emitted event type is in the schema registry; state-machine enforcement; roadmap
status table regenerated from code markers.
*Impact medium (trust in docs) / effort low.*

### 12.3 Trust boundaries and env scrubbing  (GC `trust-boundaries.md`)
Write the trust model down: vault files and config are trusted operator code; task text,
Discord messages, playbook LLM output, PR bodies are untrusted data and must never be
interpolated into shell (AQ's git/PR paths and playbook-driven commands should use argv, not
`sh -c`). Strip secret-looking env (`*TOKEN*`, `*API_KEY*`, …) from subprocess runtimes
unless explicitly configured (the `claude_sdk` runtime already scrubs `CLAUDECODE`).
*Impact medium (security) / effort low.*

### 12.4 Cost accounting with pricing  (GC `gc costs`, `[[pricing]]`)
`token_ledger` tracks tokens; add per-model pricing config and `aq costs` by project/profile/
playbook/day. Small, but it makes the reflection and playbook spend visible — which matters
for deciding which automation should become zero-LLM orders (8.5).
*Impact low–medium / effort low.*

### 12.5 Release-gate / PR evidence records  (GC `release-gates/`, Beads PR guidelines)
For an AI-heavy codebase, GC's per-change PASS/FAIL gate file and Beads' "one layer per PR,
prove the bug with no orchestrator, `Agent-Signature` trailer" are practical discipline. AQ's
spec-first rule could be extended with a lightweight `docs/gates/<change>.md` (acceptance
criteria, test evidence, spec diff) for substantial changes.
*Impact low / effort low.*

## 13. What *not* to copy

- **tmux as the session substrate.** GC needs tmux even as fallback; AQ's SDK/ACP subprocess
  model is simpler, cross-platform, and already streams. Keep it.
- **Dolt as the store.** Versioned, mergeable issue history is attractive, but the ops burden
  (server mode for multi-writer, CGO, GC/compaction hygiene, corruption runbooks) contradicts
  AQ's "fewer moving parts". SQLite/Postgres + the `events` table + `archived_tasks` give
  enough audit; if git-synced work state is ever needed, export/import JSONL like Beads'
  passive export rather than adopting Dolt.
- **Command sprawl.** ~120 `bd` and ~70 `gc` commands violate both projects' own rules. AQ's
  134 commands are already near the edge — grow via labels/metadata/formulas, not new verbs.
- **60-field agent config.** Prefer AQ's small profile + vault overrides; add fields only
  with a default and a doctor check.
- **Provider hooks that shell out on every turn** as the *primary* delivery mechanism. Use
  hooks (10.2) for re-injection and handoff, but keep AQ's in-process prompt builder as the
  main path.
- **"Everything is a bead" taken literally.** Unifying gates/waits into the task graph (7.4)
  is worth it; modelling sessions, order-runs and mail as tasks is not — AQ already has
  tables with clearer semantics.
- **Machine-wide supervisor / multi-city** — out of scope for AQ's single-daemon design
  unless multi-node becomes a goal.

## 14. Where Agent Queue is ahead (keep and double down)

- **The learning loop.** Neither GC nor Beads improves itself. Scoped memory tiers, facts.md,
  reflection, consolidation, and the chat analyzer are AQ's moat; the priority is to make the
  loop *measurable* (memory health/retrieval metrics are specced in `self-improvement.md` §6
  but not built) and to make memory available even when the plugin is absent (a degraded
  SQLite FTS backend for L1/L2 would honour "plugins own their dependencies" while keeping
  the headline feature always on).
- **Single command surface** mirrored to Discord/MCP/REST/CLI — GC and Beads each maintain
  parallel CLI/API/MCP layers by hand.
- **Typed workspace kinds** with all-or-nothing ordered acquisition — richer than GC's
  per-bead worktrees and Beads' merge slot.
- **Multi-kind runtime registry** (`claude_sdk`, `acpx`, `supervisor`) selected per profile,
  with the Supervisor as an in-process tool-only runtime for cheap triage work.
- **Chat-first control plane with live streaming** — nothing comparable ships in GC/Beads.
- **Vault as Obsidian-browsable knowledge** with watchers, project shadowing, and markdown
  profiles — more approachable than TOML packs for humans who read and edit knowledge.
- **Plugin trust levels and permissions** — GC has no plugin system by design; Beads has
  hooks only.

## 15. Prioritized summary

| # | Improvement | From | Touches | Impact | Effort |
|---|---|---|---|---|---|
| 7.1 | Typed dependency edges (`waits-for`, `conditional-blocks`, provenance) | Beads, GC | `tables.py`, `dependency_queries.py`, cascade | High | Med |
| 7.4 | Gates/waits as blocking records; shrink status enum | GC, Beads | state machine, approvals mixin, cascade, Discord views | High | High |
| 8.5 | Zero-LLM orders in the vault; move cycle housekeeping into default orders | GC | new `orders/`, `TimerService`, `PlaybookManager` triggers, `run_one_cycle` | High | Med |
| 9.1 | Deterministic formula layer materializing task graphs | GC, Beads | new compiler, `create_task`, workflows | High | High |
| 10.1 | Task-scoped MCP surface + `aq prime` | Beads | `mcp_registration.py`, `execution.py`, profiles | High | Low–Med |
| 8.1 | Reconcile/adopt on restart; resume sessions; persist restart counters | GC | `_recover_stale_state`, execution, runtimes | High | Med |
| 7.2 | Persisted `is_blocked` + `aq task explain` | Beads, GC | queries, scheduler, CLI | High | Low–Med |
| 8.2 | Heartbeats/leases from stream messages; reaper | Beads, GC | execution `on_message`, cascade | Med–High | Low |
| 10.2 | Provider hooks: prime on SessionStart, PreCompact handoff, inject replies | Beads, GC | runtimes, prompt builder | Med–High | Med |
| 11.1 | Importable vault packs with pinned versions | GC | vault watcher, new `pack` commands | Med–High | Med |
| 12.1 | `aq doctor --fix` | GC, Beads | new command; reuse health checks | Med–High | Low–Med |
| 9.2 | Control nodes: retry class, check loops, fan-out/in, finalize | GC | formula compiler | Med–High | Med |
| 7.5 | Enforce the task state machine | GC | `transition_task` call sites | Med | Med |
| 7.6 | Outcome/failure-class metadata from agents → smarter retries | GC | runtimes, `task_results`, retry policy | Med | Low |
| 8.3 | Stall ladder with nudge before kill | GC | execution, runtimes | Med | Low–Med |
| 8.4 | Per-profile session caps / demand-driven pools | GC | profiles, scheduler, `AgentReconciler` | Med | Med |
| 10.3 | Durable inter-agent messages | Beads, GC | new table, prompt builder | Med | Med |
| 9.3 | Deterministic playbook graph block; LLM as drafting aid | GC | `compiler.py`, schema | Med | Low–Med |
| 12.3 | Written trust boundaries + env scrubbing | GC | runtimes, git, docs | Med | Low |
| 12.2 | Invariant + docs-sync tests | GC, Beads | tests | Med | Low |
| 7.3 | Labels table + metadata-first rule | Beads, GC | `tables.py`, list filters | Med | Low |
| 11.3 | Vault prompt fragments per profile/provider | GC | `prompt_builder.py` | Low–Med | Low |
| 8.6 | Event replay cursor + payload registry test | GC | events API, tests | Low–Med | Low |
| 10.4 | Universal `--json` envelope, `--brief`, `aq schema` | Beads | CLI | Low–Med | Low |
| 12.4 | `aq costs` with pricing | GC | tokens | Low–Med | Low |
| 7.7 | Generic groups with computed progress | GC, Beads | queries | Low–Med | Low |
| 11.2 | `config explain`; definition vs deployment split | GC | config editor | Low–Med | Low |
| 9.4 | Queue playbook runs instead of rejecting | GC, Beads | `PlaybookManager` | Low | Low |
| 12.5 | Release-gate / PR evidence records | GC, Beads | process | Low | Low |

A sensible first slice that compounds: **7.1 + 7.2 + 7.3** (graph richness and
explainability, mostly additive schema), then **8.5** (orders, which also hosts the gate
sweep for 7.4 and the reaper for 8.2), then **10.1/10.2** (context tax and re-injection),
then **9.1** (formulas) once typed edges and gates exist.

---

## Appendix A — Glossary crosswalk

| Agent Queue | Gas City | Beads |
|---|---|---|
| task | bead (type task) | issue / bead |
| project | rig (+ city) | repo `.beads/` |
| agent profile / agent type | agent (config) | — |
| agent (slot) / runtime session | session (a bead) | assignee |
| playbook | formula (+ prompt template for judgment) | formula |
| playbook run | workflow run / molecule / wisp | molecule / wisp |
| workflow (coordination) | convoy + workflow root | epic / swarm |
| task dependency | `needs` / `blocks` edge | dependency (`blocks`, …) |
| DEFINED → READY | invisible → `bd ready` | `is_blocked` → ready frontier |
| AWAITING_APPROVAL / WAITING_INPUT | human gate + mail | `gate` (human) |
| PR merge check | `gh:pr` gate via `gate-sweep` | `bd gate check` |
| TimerService / cron playbooks | orders | — (hooks) |
| EventBus + `events` | events.jsonl + SSE | events / journal |
| Supervisor (chat brain) | mayor (pack agent) + `extmsg` | — |
| vault | packs + city dir | `.beads/` + `PRIME.md` |
| facts.md / memory | prompt template + bead ledger | `bd remember` |
| workspace kind / workspace | rig repo / `work_dir` / worktree | `bd worktree` / merge slot |
| `aq` CLI / MCP / REST | `gc` CLI / HTTP API | `bd` CLI / `bd serve` / MCP |
| `_recover_stale_state` | adoption barrier + reconcile | lease expiry + `bd reclaim` |
| `stuck_timeout_seconds` | idle/progress stall ladders | lease TTL + heartbeat |

## Appendix B — Size and activity (measured 2026-08-19)

| | Agent Queue | Gas City | Beads |
|---|---|---|---|
| Language | Python 3.12 | Go 1.26 | Go 1.26 |
| Non-test source LOC | ~105.7k | ~580.9k | ~341.0k |
| Test LOC | ~136.7k | ~1,004k | ~455.7k |
| Source files | 238 `.py` (src) | 1,465 `.go` | 1,228 `.go` |
| Markdown docs | 69 (`docs/`) | 565 | 375 |
| Commits | 1,767 (to 2026-05-10) | 5,638 | 10,663 |
| CLI commands | 134 handler commands (+ plugin) | ~70 `gc` | ~120 `bd` |
| Largest package | `discord/commands.py` 4.4k, `core.py` 2.1k | `cmd/gc` 183k, `internal/api` 99k | `internal/storage/dolt` 257 files |

## Appendix C — Sources read

Agent Queue: `CLAUDE.md`, `profile.md`, `docs/specs/design/{guiding-design-principles,
self-improvement,agent-coordination,roadmap}.md`, `src/models.py`, `src/database/tables.py`,
`src/orchestrator/{core,execution,workspace_attachments,agent_reconciler}.py`,
`src/scheduler.py`, `src/state_machine.py`, `src/playbooks/*`, `src/runtimes/*`,
`src/prompt_builder.py`, `src/reflection.py`, `src/mcp_registration.py`, `src/plugins/*`.
Gas City: `README.md`, `AGENTS.md`, `docs/getting-started/{how-gas-city-works,
coming-from-gastown}.md`, `docs/guides/understanding-formulas.md`, `docs/reference/{config,
events,trust-boundaries}.md`, `internal/config`, `internal/runtime`, `internal/session`,
`internal/formula`, `internal/dispatch`, `internal/orders`, `internal/events`,
`cmd/gc/{city_runtime,session_reconciler,cmd_sling,cmd_hook}.go`, plus `../city`.
Beads: `README.md`, `AGENTS.md`, `AGENT_INSTRUCTIONS.md`, `engdocs/PROJECT_CHARTER.md`,
`docs/core-concepts/*`, `docs/workflows/*`, `docs/multi-agent/*`, `docs/cli-reference/{prime,
remember,compact}.md`, `internal/types/types.go`, `internal/storage/{storage.go,sqlbuild/
ready.go,issueops/claim.go,issueops/blocked_state.go}`, `internal/idgen`, `internal/compact`,
`cmd/bd/{prime,memory,gate,swarm}.go`, `PROPOSAL-pluggable-storage-backends.md`.
