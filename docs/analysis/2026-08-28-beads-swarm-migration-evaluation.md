---
tags: [analysis, beads, gastown, gascity, swarm, migration, orchestrator]
date: 2026-08-28
status: evaluation — no decision taken
---

# Moving to beads + a Gas Town–style pull swarm — what it would actually take

**Question asked.** What would it take to retire Agent Queue's task system and push
orchestrator, adopt **beads** as the work store, and run **agent swarms** that *pull*
work Gas Town–style — while keeping projects, the dashboard, and the `aq` CLI?

**Method.** Five parallel code/web surveys on `main@e1d99354` (task model + cascade;
sessions/workspaces/messaging; CLI/API/dashboard/MCP; playbooks/reflection/vault/specs;
current state of `gastownhall/beads` and `gastownhall/gastown` as of 2026-08-29), read
against the three prior analyses this repo already holds:
[[comparison-gascity-beads]] (2026-08-19), [[framework-overhaul-todo]] (decisions D1–D9),
[[ecosystem-positioning]] §2, and [[2026-08-26-session-runtime-vs-gascity]]. File:line
references are to this repo unless noted.

---

## 0. TL;DR

The request bundles three separable changes. They have very different costs, and only
one of them is the thing that actually delivers "agent swarms":

| Change | What it buys | Cost | Verdict |
|---|---|---|---|
| **P — Pull model.** Agents claim work themselves; the daemon sizes worker pools instead of assigning tasks. Roles (Mayor / Polecat / Witness / Refinery) as profiles + existing subsystems. | The swarm. This is the behavioural change you are asking for. | **Weeks.** ~3–6k LOC, mostly additive. Every substrate piece already exists and most are switched on in your live config. | **Do this first.** |
| **S — beads as the store.** Replace `tasks` + 12 side tables with a Dolt-backed beads DB; the daemon and agents both talk to `bd`. | Git-synced, versioned work ledger; `bd`-ecosystem tooling; agents that already know `bd`. | **Months.** Rewrite of ~15k LOC across the orchestrator, command surface, and query layer; ~40–60k test LOC invalidated; a Dolt SQL server becomes a hard runtime dependency; the ready-frontier join (deps + gates + workspaces + admission) has to be split across two datastores. | **Don't do this now.** Build P on the existing store behind a `WorkItemStore` seam so S can be tried later as an adapter rather than a rewrite. |
| **R — Gas Town roles literally.** Mayor, Polecats, Witness, Refinery, Deacon, Boot, Dogs, convoys, hooks, GUPP. | A vocabulary. | Mostly free — each role maps onto something already in the tree (§5). | Adopt the *shapes*, not the names or the 10-role taxonomy. Gas Town itself is in maintenance; Gas City (its successor) has **zero** hardcoded roles. |

**The one-paragraph reason.** Every beads *idea* is already in this codebase's schema by
deliberate decision on 2026-08-19: typed edges, persisted `is_blocked`, gates as blocking
records, labels, metadata-first, `explain`, leases/heartbeats, adopt-on-restart, stall
ladder, `aq prime`, explicit `task close`. What is *not* built is the pull side: nothing
lets an agent select its own work (`Scheduler.schedule()` is push-only, `SessionLens`
refuses to spawn task sessions — `src/messages/session_lens.py:11-15`), and there are no
worker pools. That gap is the swarm, and it is independent of where the rows live.
Meanwhile beads itself has changed shape: since ~v0.58 it is **Dolt-only** — SQLite,
JSONL sync, `bd sync` and `bd daemon` are deleted; concurrent writers require a running
`dolt sql-server`; there is no Python SDK. "Move to beads" in 2026 means "run a Dolt
server per machine and drive it from Python via a Go CLI." That is a large ops and
latency commitment to make for a store whose semantics you already have.

---

## 1. What "beads" and "Gas Town" mean today (verified 2026-08-29)

Both repos moved to the `gastownhall` org. Details and sources are in the research
appendix (§A); the facts that change the evaluation:

**beads v1.2.2 (2026-08-15, MIT, Go).**
- **Dolt is the only backend.** Embedded mode (`.beads/embeddeddolt/`) is *single-writer,
  file-locked*. Multi-writer — which a daemon plus N agents is — requires **server mode**
  (`bd init --server`, a `dolt sql-server` on 3307, or `--shared-server` for per-project DBs
  under one server). Beads pins Dolt 2.2.0 and documents a regression in 2.3.x.
- **JSONL is an export, not sync.** `.beads/issues.jsonl` is "not the source of truth or a
  backup"; the 3-way JSONL merge engine was deleted. Sync is `bd dolt push/pull` over
  `refs/dolt/data` on the git remote. Third-party tools that read `issues.jsonl` broke.
- **Programmatic access:** `--json` everywhere (`BD_JSON_ENVELOPE=1` for the envelope), a
  Go library, `bd serve` (loopback HTTP `/v0/beads/*` + SSE watch — surface only partially
  documented), an opt-in events journal (`bd events tail --since N --follow`). **No Python
  SDK**; `beads-mcp` (PyPI) shells out to the CLI. Dolt speaks the MySQL wire protocol, so
  Python *can* read the tables directly — but every write that needs beads' semantics
  (`is_blocked` recompute, CAS claims, gate resolution) lives in Go `issueops`.
- **Model:** `open|in_progress|blocked|deferred|closed` (+ custom statuses with a category);
  dep types `blocks|parent-child|conditional-blocks|waits-for` (blocking) plus 8 non-blocking;
  gates `human|timer|gh:pr|gh:run|bead`; `bd ready --claim` (CAS on `row_lock`), 5-min
  leases + `bd heartbeat` + `bd reclaim`; formulas → protos → molecules/wisps; `bd swarm`
  computes ready-front waves; `bd remember` KV memory; ~108 commands.
- **Multi-repo:** one `.beads/` per repo; routing by prefix; `bd repo add` hydration for a
  merged view; cross-repo deps via `external:<project>:<capability>`. Cross-rig `bead`
  gates "must be resolved manually now" — the multi-rig routing was removed.

**Gas Town v1.2.1 (2026-06-06) — maintenance mode.** Yegge shipped **Gas City** (`gc`) on
2026-04-24 as the successor and answers "should you switch?" with "Yes". Gas City's thesis
is *zero hardcoded roles* (`AGENTS.md`: "If a line of Go references a specific role name,
it's a bug"); Mayor/Polecat/Witness/Refinery survive only as a *pack*. So "Gas Town–style"
is best read as the mechanics — hook/claim, worktree-per-worker, merge queue, watchdog
chain, mail/nudge/handoff, formulas — not the role list. Gas Town's own operating envelope
is worth keeping in view: one Dolt server per town, tmux always, `--dangerously-skip-permissions`
always, ~$4k/day at 50 agents, and a documented history of the watchdog killing workers
mid-job before the Dolt migration stabilised it.

---

## 2. Where this codebase already is

### 2.1 The substrate is beads-shaped by design

`docs/specs/design/work-graph.md:28-36` credits every piece to Beads or Gas City and the
schema carries it (`src/database/tables.py`):

| Beads / Gas City | Agent Queue today | State |
|---|---|---|
| typed deps `blocks/parent-child/waits-for/conditional-blocks` + provenance | `task_dependencies.dep_type` (`tables.py:152`, `TASK_DEP_TYPES` `:125`) | **built**; provenance edges writable, read path incomplete (`docs/specs/implementation/work-graph.md:264`) |
| persisted `is_blocked`, in-transaction recompute | `tasks.is_blocked` (`tables.py:99`), `blocked_state.py:220` | **built, shadow mode** — `work_graph.blocked_state_authoritative: false`; legacy scan still decides (`monitoring.py:52-100`) |
| gates `human/timer/gh:pr/gh:run/bead` | `gates` + `task_gates` (`tables.py:189-236`), types `human/timer/pr-merged/ci-run/event/task/routing`, `_sweep_gates` at cascade 2b (`core.py:2428`) | **built, on** (`gate_sweep_interval_seconds: 30`) — a superset of beads' gate types |
| labels, `hold:*` | `task_labels` (`tables.py:238`), `apply_label_filters(exclude_hold=…)` | **built** |
| metadata as the extension point; `gc.outcome` / `work_outcome` | `task_metadata`; `aq task close --outcome --failure-class --work-outcome` (`session_commands.py:435`) | **built** |
| `bd ready --explain` | `aq task explain`, `aq task project-ready` (`task_commands.py:3177, 3254`, `src/explain.py`) | **built** |
| leases + heartbeat + reclaim | `sessions.lease_ttl_seconds: 480`, `aq task heartbeat`, stall ladder nudge→backoff→restart→quarantine (`reconciler.py:544`) | **built, on** |
| adopt-on-restart, "unknown is not dead", kill fencing | `SessionReconciler.adopt_on_start` (`reconciler.py:194`), `PartialListError`, `instance_token` (`provider.py:154`) | **built, on** |
| `bd prime --hook-json`, handoff | `aq prime`, `aq handoff --auto` on `PreCompact`, `.aq/PRIME.md` override (`src/prime/`) | **built** |
| mail + nudge | `messages` table, `MessageDeliveryEngine`, `SessionLens.nudge` | **built, on** |
| epics / convoys, swarm waves computed not stored | any task is a container; `get_group_progress` Kahn waves (`task_queries.py:613`) | **built** |
| formulas (deterministic graph materialisation) | `src/task_graph/` (`aq-graph` blocks with `vars`, validated, one-transaction create) | **built**; not parameterised/reusable like formulas |
| enforced state machine | `state_machine.enforce` flag | **off** |
| merge slot | `merge_slots` + `_phase_integrate` renew-before-push (`git_ops.py:1432`) | **built, on** |
| desired-state reconciliation | `sessions.desired_state` (2026-08-27) | **built** for named sessions; **no pools** |

Your live `~/.agent-queue/config.yaml` has `sessions.enabled: true` (tmux), `messages`,
`supervisor_agent`, `worktrees`, `playbooks` all `true`. Two things remain dark:
`blocked_state_authoritative` and `state_machine.enforce`.

### 2.2 Decisions already on record

`framework-overhaul-todo.md` §0 (Jack, 2026-08-19): D1 tmux-first sessions ✅, D2
algorithmic orchestrator with **no hardcoded roles** and the supervisor as a *profile* ✅,
D5 worktree-per-task ✅, D6 `aq` CLI primary ✅, **D9 multi-project stays first-class on the
single-DB `project_id` model** ✅. `comparison-gascity-beads.md` §13 explicitly lists
"Dolt as the store" and "'everything is a bead' taken literally" under *what not to copy*,
and `ecosystem-positioning.md` §2 argues the ready frontier "is a join across four domains
that only exist together here … a sidecar tracker knows edges and nothing else; the join is
the product." This evaluation re-opens those decisions on purpose; §4 says where they hold
and where they don't.

### 2.3 What is genuinely missing for a swarm

1. **A claim primitive.** No `aq task claim` / `aq ready --claim`. Assignment is
   `Scheduler.schedule()` → `AssignAction` → `_execute_task` (`execution.py:250`).
2. **Worker pools.** Profiles have `lifecycle: task|named`; there is no `pool` lifecycle,
   no `min/max_active_sessions`, no demand query. `AgentReconciler` sizes *agent rows*, not
   sessions (`agent_reconciler.py:40`); `_step_named` converges single named sessions.
3. **A worker loop.** Task sessions are one-shot: bootstrap prompt → work → `aq task close`
   → `drain-ack` → killed. A polecat-style worker that closes one item and claims the next
   in the same session has no protocol.
4. **Reusable formulas.** `aq-graph` blocks are per-spec; there is no library of
   parameterised graphs (`vars` exists in `task_graph/validator.py:66`, a registry doesn't).
5. **Agentic merge conflict handling.** `_phase_integrate` aborts on conflict → BLOCKED
   (`git_ops.py`); nothing re-dispatches a rebase task the way Gas Town's `REWORK_REQUEST` does.

---

## 3. Concept mapping

| Gas Town | beads | Agent Queue equivalent | Gap |
|---|---|---|---|
| Town | — | the daemon | none |
| Rig | `.beads/` per repo, prefix | `projects` row + `repos` + per-project vault/kinds/profiles/merge slot | none (richer) |
| Bead | issue | `tasks` row + side tables | none |
| Hook (agent's pinned work) | `assignee` + `in_progress` | `tasks.assigned_agent_id` + `sessions.task_id` | need claim |
| `gt sling` / `gc hook --claim` | `bd ready --claim` | `Scheduler` push | **need claim** |
| Polecat | — | `worker-*` profile, `lifecycle: task`, worktree slot `aq/<task_id>` | **need pool lifecycle + loop** |
| Crew | — | named session (`lifecycle: named`) | none |
| Mayor | — | `supervisor` profile; `supervisor-global` (`session_lens.py:80-88`) | prompt rewrite only |
| Witness | `bd reclaim` | `SessionReconciler` (observe/exits/orphans/stall ladder/backstop) | none |
| Refinery (Bors merge queue) | merge-slot bead | `merge_slots` + `_phase_integrate` | **conflict → agent rework** |
| Deacon / Boot / Dogs | — | `run_one_cycle` cascade + `TimerService` + plugin cron | none (zero-LLM "orders" still a good idea, comparison §8.5) |
| Convoy | epic / `tracks` | container task + `get_group_progress` | none |
| Formula → molecule / wisp | same | `aq-graph` → `create_task_graph`; playbooks (LLM) | **reusable formula registry** |
| Mail / nudge / handoff / seance | message beads | `messages` + delivery engine + `aq handoff`; transcript readers | seance-equivalent = `aq session logs` |
| GUPP | — | `BOOTSTRAP_PROMPT` (`spec.py:69-80`) | reword for pull |
| `gt prime`, `.claude/settings.json` hooks | `bd prime --hook-json` | `aq prime`, `hook_files` + `--settings` (`spec.py:406`) | none |
| `gt done` | `bd close --reason` | `aq task close --outcome … && aq session drain-ack` | none (ours is typed — keep) |
| `gt dashboard`, `gt feed` | — | React dashboard, `/ws/events`, pane SSE | none (richer) |
| Dolt server per town | Dolt | SQLite/Postgres | n/a |

---

## 4. Option S in detail — beads as the sole work store

This is the literal request. Here is what it entails, so the cost is concrete.

### 4.1 Deployment shape

- A **`dolt sql-server` per machine**, supervised by the daemon (Gas Town's `gt daemon`
  does exactly this: 30 s health checks, backoff restart, port 3307). New binaries: `dolt`
  (pinned 2.2.0), `bd`. Beads server mode is required because the daemon and every agent
  session are concurrent writers.
- Per-project beads DBs (`--shared-server`, one database per project) to keep the
  git-sync story per repo — or one DB with a `project:<id>` label, which is simpler but
  forfeits the git-backed-per-repo property that is the main reason to want beads.
  Cross-project deps (allowed today, `work-graph.md:65-67`) become `external:` edges,
  which beads treats loosely and cross-rig gates are currently manual-resolve.
- Sync: `bd dolt push/pull` on the repo remote's `refs/dolt/data`. Agents in worktrees
  need `.beads/redirect` files (Gas Town writes them per worktree).

### 4.2 Python ↔ beads access

Three options, none free:

| Path | Reads | Writes | Cost |
|---|---|---|---|
| `bd --json` subprocess | ok | ok, with beads semantics | Go binary + Dolt connection per call. The cascade issues dozens of queries per 5 s tick (`core.py:2353-2558`, `_schedule` snapshot `core.py:3038-3104`); `list_active_tasks` alone was optimised because an unfiltered scan cost 1.7 s at 100k rows. Must be measured before committing. |
| `bd serve` HTTP + SSE | ok | limited, partially documented | loopback only by default; surface unverified |
| MySQL wire to Dolt (SQLAlchemy `mysql+aiomysql`) | fast, native SQL, joins | **bypasses `issueops`** — no `is_blocked` recompute, no `row_lock` CAS, no gate semantics | reads via SQL + writes via `bd` is the plausible hybrid; two access paths to keep coherent |

### 4.3 Schema mapping — `tasks` (33 columns) → beads issue

| AQ column | beads | Note |
|---|---|---|
| `id` slug | hash id `bd-a1b2`, hierarchical `.1` | `task_names.py`, `assign_child_ids` (`creator.py:57`) replaced; every URL/thread/branch (`aq/<task_id>`) changes shape |
| `project_id` (NOT NULL FK) | per-DB or label | see 4.1 |
| `status` (11) | `open/in_progress/blocked/deferred/closed` + custom | DEFINED→open; READY→open∧ready; ASSIGNED/IN_PROGRESS→in_progress; PAUSED→deferred+`defer_until`; WAITING_INPUT/AWAITING_PLAN_APPROVAL→human gate; AWAITING_APPROVAL→`gh:pr` gate; COMPLETED→closed; FAILED→custom `failed:frozen`; BLOCKED→blocked. This is the status collapse work-graph §12 already plans, done all at once. |
| `priority` int (lower=higher, default 100) | 0–4 | lossy remap |
| `profile_id`, `intelligence_class`, `affinity_*`, `workspace_mode`, `preferred_workspace_id`, `verification_type`, `requires_approval`, `skip_verification`, `auto_approve_plan`, `retry_count/max_retries`, `branch_name`, `pr_url`, `discord_thread_id` | `metadata` JSON | beads' own convention (`execution_agent_type`, etc.). Fine, but every query that filters on them becomes client-side or JSON-path |
| `resume_after` | `defer_until` | native |
| `dedup_key` (+ `ensure_task`) | `external_ref` or metadata | no unique index → `ensure_task` needs a search-then-create race guard; the default pipeline depends on it |
| `parent_task_id`, `is_plan_subtask`, `workflow_id` | `parent`, epic/convoy | native |
| `is_blocked` | native | native |
| `assigned_agent_id` | `assignee` string | agents table becomes a soft registry |

Side tables: `task_criteria` → `acceptance`; `task_context` → `design/notes/comments`
(spec_ref inlining in `prime/sections.py:117-166` must re-parse from comments);
`task_labels` → labels; `task_gates`/`gates` → `bd gate` (**loses `event`, `task`,
`routing` gate types** — the default pipeline creates `routing` and `task` gates
(`default-pipeline.md`), and `routing` gates are only resolvable via `task_route`
(`gate_commands.py:163-172`)); `task_results` → comments + close reason (typed outcomes
survive as metadata); `archived_tasks` → closed + `bd admin compact`; `task_proposals` →
**no analog** (stays local); `task_workspace_requirements`, `task_tools` → metadata.

### 4.4 Code that changes

| Layer | Files | LOC | What happens |
|---|---|---|---|
| Query layer | `task_queries.py`, `blocked_state.py`, `dependency_queries.py`, `gate_queries.py`, `archive_queries.py`, `result_queries.py`, `task_requirements_queries.py` | ~3,000 | Deleted; replaced by a `BeadsStore` adapter that shells to `bd` / reads Dolt |
| Command surface | `task_commands.py` (36 `_cmd_*`), `gate_commands.py`, `workflow_commands.py`, `session_commands.py` (`task_close`) | ~4,200 | Rewritten against the adapter. **Good news:** CLI, REST, OpenAPI, TS/Python clients and MCP tools all generate from `_cmd_*` + `_ALL_TOOL_DEFINITIONS` (`auto_commands.py:283-310`, `codegen.py:419-450`, `mcp_registration.py:291-311`), so keeping command names and result shapes keeps ~49 `aq task` subcommands, 42 `/api/task/*` routes and the dashboard's ~100 hooks working |
| Orchestrator | `core.py` (cascade, `_schedule`, `_recover_stale_state`), `monitoring.py`, `approval.py`, `execution.py`, `git_ops.py`, `workspace.py`, `sync_workflow.py`, `events.py` | ~9,500 | The status-machine-shaped halves rewritten; `_launch_session_for_task` (`execution.py:1766-1957`) and `_phase_integrate` are the salvageable algorithms |
| Models / state machine | `models.py` `Task`/`TaskStatus`/`TaskEvent`, `state_machine.py` | ~1,500 | 38 files import `TaskStatus`, 72 import `src.models` — a `Task`-shaped adapter object is mandatory or ~70 files change |
| Sessions (thin seam) | `reconciler.py` (7 db methods: `get_task`, `transition_task`, `list_tasks`, `get_session_for_task`, `get/set_task_meta`), `spec.py:203-225` (3 fields), `exit_classifier.py` (duck-typed `.status`) | ~100 | Extract a `WorkItemStore` protocol; **this is the cheap part** |
| Workspaces / merge | `workspace_attachments.py` (3 fields), `worktree_manager.py` (task id → branch), `merge_slot.py` (soft ref already) | ~50 | trivial |
| Prime / explain / graph API | `prime/sections.py`, `explain.py` graph half, `api/graph.py` | ~400 | re-sourced |
| Playbooks | `core.py:869-881` hydrates `event.task` via `asdict(get_task())`; default pipeline templates `{{event.task.branch_name}}`, `ensure_task(dedup_key)`, `gate_create(routing|task)`, `add_dependency(dep_type)` | — | survive **only if** the adapter returns a `Task`-shaped dict and `dedup_key`/`routing` gates are re-implemented |
| Events | 12 `task.*` schemas require `(task_id, project_id, title)`; dashboard `ws/types.ts` 38-event union; Discord `_task_proxy` 18 fields | — | keep firing from the daemon. **Catch:** if agents write to beads directly with `bd`, the daemon does not see the mutation. Needs a journal tail (`bd events tail --follow`, opt-in) or `bd serve` SSE bridged into `EventBus`, with idempotency |
| Migrations | 33 tables, Alembic | — | one-way data migration + a long dual-run |
| Tests | tasks 4.5k, orchestrator 2.2k, database 2.2k, gates 0.9k, events 7.4k, playbooks **29k** (enabled in your live config), merge 3.3k, sessions 5.5k (partly) | **~40–60k of 166k test LOC** | most rewritten, not adapted |

**Order of magnitude: 15–20k source LOC rewritten, 40–60k test LOC invalidated, one
new always-on server process, two access paths to keep coherent.** Several months of
focused work before the system is back to today's behaviour, with the swarm still to build
on top.

### 4.5 The structural problem, independent of effort

Readiness today is one SQL predicate over deps ∧ gates ∧ (`hold:*` labels) evaluated
in the same transaction as the mutation (`blocked_state.py:192`), and *admission* is a
join of that with workspace locks, merge slot, project caps, budget, provider cooldown and
constraints (`_schedule`, `explain.py:48-139`). Beads owns the first half natively and
knows nothing of the second. After a move you have:

- either **two readiness authorities** (beads `ready` + AQ admission) that can disagree —
  the exact "sidecar tracker knows edges and nothing else" failure `ecosystem-positioning.md`
  §2 names; or
- AQ admission state pushed *into* beads as labels/metadata (`hold:workspace`,
  `hold:budget`) so `bd ready` is the single truth — which means the cascade writes to
  beads every tick, through the CLI, and gate types beads lacks (`event`, `task`,
  `routing`) are re-expressed as `hold:` labels swept by AQ.

The second is coherent and is roughly what Gas City does (`gc.routed_to` metadata, hold
labels). It is also a full rewrite of the scheduler's contract, not a swap of storage.

### 4.6 What you would gain that you don't have

Honest list: git-synced work state across machines (`refs/dolt/data`) and Dolt history
of every mutation; the `bd` ecosystem (BeadBoard, Foolery, vscode-beads, `bd swarm`
wave analysis); agents that have `bd` conventions in their training data; federation /
Wasteland. None of these are prerequisites for a swarm on one machine. If multi-machine
work state is the real driver, comparison §13's advice stands: a beads-compatible
**JSONL export/import** (`bd import` is upsert-only and still supported) gets you
interchange without adopting Dolt.

---

## 5. Option P in detail — the pull swarm on the existing store

This is the path that produces swarms in weeks. Each item names the existing piece it
extends.

### P1. Claim primitive (new, ~300 LOC + tests)

`_cmd_task_claim(task_id | --next, profile_id, project_id)` → single CAS:

```sql
UPDATE tasks SET status='IN_PROGRESS', assigned_agent_id=?, updated_at=?
WHERE id=? AND status='READY' AND is_blocked=0 AND assigned_agent_id IS NULL
```

(`rowcount==1` or typed `ClaimConflict`, beads' `claim.go` shape). `--next` walks
`get_ready_frontier(project, profile filter)` (`blocked_state.py:487`) past rows racing
agents took. Fenced to the session's bearer token (`api_session_tokens` already scope
`task_id`/`project_id`, `api/scope.py:34-80`) so a stale session cannot claim. Admission
checks that today run in `_schedule` (caps, budget, cooldown, constraints) move into the
claim path via `_check_constraints_before_assignment` (`execution.py:187`) — this is also
`ecosystem-positioning.md` §5.1's "atomic admission control" item. Expose as `aq task claim`,
`aq task ready --claim`; add `task_claim` to `AGENT_COMMAND_SET` (`api/scope.py:14`).
Requires flipping `work_graph.blocked_state_authoritative: true` first (shadow mode has
been logging divergence; check the log, flip).

### P2. Pool lifecycle + desired-state sizing (~800 LOC)

Profile `## Config`: `lifecycle: pool`, `min_active`, `max_active`, `work_query`
(default: ready frontier for this project filtered by `profile_id`/`intelligence_class`),
`idle_timeout`, `wake_mode`. A new cascade step `_reconcile_pools()` (Gas City's
"build desired state → reconcile"): per (project, pool-profile) compute
`desired = clamp(ready_count_for_query, min, max)` bounded by `project.max_concurrent_agents`
and the usage-aware headroom from `2026-08-24-usage-aware-concurrency`, then start/drain
sessions through `SessionLens.ensure_started` (`session_lens.py:215`) and
`sessions.desired_state`. `Scheduler.schedule()` stops emitting `AssignAction` for pool
profiles and becomes the pure *admission* function (`scheduler.py` is already side-effect
free, so this is a narrowing). `AgentReconciler` keeps sizing agent rows to match.

### P3. Worker loop protocol (~200 LOC + prompt/template changes)

A `POOL_BOOTSTRAP_PROMPT` beside `BOOTSTRAP_PROMPT` (`spec.py:69-80`):
`aq task claim --next` → `aq prime` (task-scoped; works because `AQ_TASK_ID` is written
via `set_meta` after claim, or prime accepts `--task`) → work → `aq task close … --claim-next`
(beads' `bd close --claim-next` shape) → loop; `aq session drain-ack` only when
`claim --next` returns empty. `_cmd_task_close` today verifies the calling session owns
the task (`session_commands.py:435`) — keep; it becomes the GUPP enforcement. Worktree
slot reset per claim via `reset_slot_for_task` (`worktree_manager.py:359`) — already
branch-per-task and never stashes. `sessions.task_id` becomes mutable across the session's
life; reconciler's orphan step (`reconciler.py:651`) already handles "live session, task
closed" as *drain*, which needs a `lifecycle: pool` carve-out ("live session, task closed,
pool" = fine).

### P4. Roles as profiles, not code (prompt work, ~0 LOC)

- **Mayor** = `supervisor-global` + per-project `supervisor` (exist). Rewrite
  `profile.md:75-94` from "stay in your project / create graphs" to: turn specs into
  graphs, sling (= set `profile_id`/labels/priority), watch `aq task project-ready`,
  read `explain`, escalate via gates. It already acts only through `aq`.
- **Polecats** = `worker-fast|standard|deep` with `lifecycle: pool`.
- **Witness** = `SessionReconciler` — deterministic, no LLM, already does nudge/recycle/
  quarantine/orphan recovery. Keep it out of a prompt.
- **Refinery** = `merge_slots` + `_phase_integrate`. Add P5.
- **Deacon/Boot/Dogs** = cascade + `TimerService` + plugin cron. Comparison §8.5
  ("zero-LLM orders in the vault") is the tidy version; not required for the swarm.
- **Convoys** = container tasks; `get_group_progress` already gives waves and max
  parallelism. Dashboard has no group view — D-side work (see §6).

### P5. Agentic rework on merge conflict (~300 LOC)

On `merge.conflict`, `_phase_integrate` records `rejection_reason` + files and BLOCKs.
Add: create a `rebase:<task>` task (profile `worker-standard`, `conditional-blocks` or
`discovered-from` edge, affinity to the original session) carrying the rejection reason —
Gas Town's `REWORK_REQUEST` and the polecat "rejection-aware resume" contract that
`framework-overhaul-todo.md` §1 already documents as our work-state contract.

### P6. Formula registry (~600 LOC)

`vault/[projects/<pid>/]formulas/<name>.md` with an `aq-graph` block + `vars`
(`task_graph/validator.py:66` `substitute_vars` exists). `aq formula list|show|cook`
(materialise = `create_task_graph`). This is comparison §9.1, and it is how a Mayor
"slings a formula" instead of authoring graphs by hand every time.

### P7. Dashboard for swarms (D-side, ~1–2k LOC TS)

`command-center/Agents.tsx` already tiles live sessions. Add: pool gauges per
(project, profile) (`desired/active/min/max`), a ready-frontier column, group/convoy
progress from `get_group_progress`, claim events in the ActivityDrawer. All reads go
through existing or new `_cmd_*` → auto-generated routes/hooks; the only manual pieces
are `src/api/models/*.py` response models and `ws/types.ts`.

**Total: roughly 3–6k Python LOC, 1–2k TS, prompt/profile edits, and two config flips.**
Nothing in `src/sessions/`, `src/git/`, `merge_slot.py`, `messages/`, `prime/`,
`tokens/`, `event_bus.py` changes materially. All 166k test LOC stay valid; new tests are
additive.

---

## 6. Surfaces — what each option does to them

| Surface | Contract it needs (from the survey) | P (pull, own store) | S (beads store) |
|---|---|---|---|
| `aq task …` (~49 subcommands, auto-generated from `_cmd_*`) | list/show/deps/explain/create/edit/close/set/delete/archive/gates | +`claim`, +`ready --claim`, +`formula` | all re-implemented over the adapter; names can be preserved |
| `aq project …` (23) | list/get/create/edit/pause/constraints/workspaces | unchanged | unchanged **iff** projects stay AQ-side (they must — beads has no project entity) |
| REST `/api/task/*` (42), OpenAPI, `packages/aq-client`, `aq-ts-client` | codegen from tool defs | regenerate | regenerate; response models hand-edited |
| MCP (~174 tools; agents see 13 via token scope) | `AGENT_COMMAND_SET` | +`task_claim` | same |
| Dashboard (16 task-centric, 8 session, ~20 config components; `hooks.ts` ~100 hooks) | `TaskDetail` 18-field shape, `/api/projects/{id}/graph`, `/ws/events` with `task.*`, `gate.*`, `session.*`, `message.*` | additive views (P7) | survives only with a `Task`-shaped adapter + daemon-side event re-emission; `pages/project/Tasks.tsx:24` hardcoded status list must read `get_schema` |
| Discord (6 read-only slash + gate buttons + `_task_proxy` 18 fields) | `notify.*` events | unchanged | as dashboard |
| Playbooks (default pipeline on `task.created/completed`, `ensure_task`, `gate_create routing/task`) | `event.task.*` hydration | unchanged | at risk: dedup, gate types |
| Reflection / memory | stable work-item id + results; scopes are `project_id`/`agent_type` only | unchanged | low risk |

---

## 7. Risks and unknowns

**Option P**
- Claim races under xdist-style concurrency on SQLite: the CAS is a single `UPDATE`, fine;
  `--next` walking the frontier needs a retry loop, not a transaction.
- Pool sizing oscillation: use hysteresis + `max_wakes_per_tick` (Gas City does; the
  session-runtime comparison §3 lists it as unbuilt).
- `blocked_state_authoritative` has never been flipped; check divergence logs first.
- Cost: a pool of N pulling workers spends like Gas Town — the usage-aware concurrency
  spec is the throttle and should land with P2, not after.

**Option S**
- Dolt server as a single point of failure (a recurring Gas Town/beads complaint); Dolt
  2.3.x regression; background I/O; `bd` CLI call latency in a 5 s cascade (unmeasured).
- No Python SDK; the Go library is the only complete API.
- Cross-project deps and cross-rig gates are weaker in beads than in AQ today.
- `task_proposals`, `routing`/`event`/`task` gates, `dedup_key` uniqueness, project
  entity, workspace requirements — all AQ-only; they stay in SQLite either way, so S is
  really "two stores", not "one".
- Every `task_id` changes format; branch names `aq/<id>`, Discord threads, LLM log paths
  (`logs/llm/tasks/{task_id}.jsonl`), vault task records all key on it.
- Gas Town is in maintenance; its successor rejects the role model you'd be copying.

---

## 8. Recommendation and sequencing

1. **Flip the two dark flags** (`work_graph.blocked_state_authoritative`,
   `state_machine.enforce`) after reading the divergence log — a day, and a prerequisite
   for a correct claim.
2. **Build P1–P3** behind `swarm.enabled` (claim, pools, worker loop) with a
   `WorkItemStore` protocol extracted from `SessionReconciler`'s seven task calls and
   `SessionSpecBuilder`'s three fields. Run one project with a 2–3 worker pool. This is the
   first point at which you have "agent swarms" and it is 2–4 weeks.
3. **P4–P7** (Mayor prompt, merge rework, formulas, dashboard pools) — another 2–4 weeks,
   parallelisable.
4. **beads interop, not migration:** `aq task export --beads-jsonl` / `import` so a repo's
   graph can be viewed with `bd`/BeadBoard/vscode-beads and so agents can be given `bd`
   vocabulary via a thin `bd`-compatible shim if that proves valuable. Cheap, reversible.
5. **Revisit S in Q4** with data: if multi-machine work state or the `bd` ecosystem turns
   out to matter, implement `BeadsStore` against the protocol from step 2 and run it as an
   adapter on one project. The decision is then an experiment, not a rewrite.

The prior decision that *does* need revisiting is D2's "supervisor as a profile" scope:
a Mayor needs cross-project authority, which `supervisor-global` has and per-project
supervisors do not. The prior decisions that hold: D9 (projects first-class, single DB),
§13 (no Dolt), and "everything is a bead" not taken literally — sessions, messages,
gates, workspaces and the merge slot are better as their own tables.

---

## A. Research appendix — sources for §1

beads: `gastownhall/beads` README, `AGENTS.md`, `CHANGELOG.md`, `docs/architecture/dolt.md`,
`docs/core-concepts/{issues,dependencies,hash-ids,sync-concepts}.md`,
`docs/multi-agent/{routing,federation,coordination}.md`, `docs/cli-reference/{init,ready,
close,prime,mol,formula,statuses,swarm}.md`, `docs/workflows/{molecules,wisps,formulas,gates}.md`,
`docs/reference/{json-schema,events-journal}.md`, `docs/community-tools.md`,
pkg.go.dev `github.com/steveyegge/beads`, PyPI `beads-mcp`.
Gas Town: `gastownhall/gastown` README, `CHANGELOG.md`, `docs/{glossary,overview,HOOKS,
agent-provider-integration}.md`, `docs/concepts/{molecules,identity,polecat-lifecycle}.md`,
`docs/design/{architecture,mail-protocol,dolt-storage}.md`; Gas City README;
yegge.ai "Welcome to Gas City" (2026-04-24); Medium posts (via mirrors); third-party
write-ups (Wangdhen, de hÓra, Appleton, Klabnik, Simons); HN threads 47770124, 46601439.
Unverified: full `bd serve` route list; whether `gt dashboard` is maintained post-Gas City.
