---
tags: [analysis, beads, swarm, parity, requirements]
date: 2026-08-28
status: analysis — companion to 2026-08-28-beads-swarm-migration-evaluation
---

# What makes beads work for agent swarms — and can Agent Queue match it?

**Question.** Strip beads down to the *properties* that make it succeed as the work ledger
for a swarm (as opposed to its features or its storage choice), then check each property
against what this codebase has, what it lacks, and what it would take to close the gap on
our own store.

**Sources.** beads `engdocs/PROJECT_CHARTER.md`, `docs/core-concepts/*`, `AGENTS.md`
("landing the plane"), `issueops/{claim,blocked_state}.go` as read in
[[comparison-gascity-beads]] §3; Gas Town polecat lifecycle and Gas City `work_query`
routing; current code on `main@aff5547c`. Property numbering is used in the scorecard (§3).

---

## 1. The fourteen properties

Grouped by what they are *for*. The first two groups are load-bearing — a swarm without
them fails in a specific, observed way (Gas Town's "clown show" period is the record).
The third group is what makes a swarm *operable* rather than merely possible.

### A. Correctness under many concurrent agents

**P1 — The graph decides; there is no dispatcher.** Readiness is a persisted projection
(`is_blocked`) over typed *blocking* edges (`blocks`, `parent-child`, `waits-for`,
`conditional-blocks`) plus gates plus `defer_until`, recomputed to a fixpoint in the same
transaction as every mutation. `bd ready` *is* the scheduler. Why it matters: N agents can
pull simultaneously with no coordinator on the hot path and no coordinator to crash;
ordering is data, so it survives every process death.

**P2 — Atomic, fenced claims with leases.** `bd ready --claim` is a compare-and-set
(`UPDATE … WHERE row_lock=? AND status IN (claimable)`); a losing racer gets a typed
conflict, not a silent double-assignment; re-claim by the same actor is idempotent; the
claim grants a lease (5 min) refreshed by `bd heartbeat` and reverted by `bd reclaim`.
Why: two agents never work one item; a crashed agent's item returns to the pool without a
human noticing. Gas City goes further and fences the claim to the live session token so a
stale session cannot claim.

**P3 — Work state lives in the ledger, not in the agent's head.** Description, design,
acceptance criteria, notes, comments, metadata (`work_dir`, `branch`, `pr_url`,
`rejection_reason`, "recorded early for crash recovery"). Beads' own problem statement:
"a crashed agent takes its context with it." Why: any agent — or a fresh session of the
same agent after compaction — can resume mid-flight; handoff is a read, not a conversation.

**P4 — Explicit completion with a typed reason; exit is not success.** `bd close --reason`,
Gas City's `gc.outcome / failure_class / work_outcome`, and the "landing the plane"
protocol (file remaining work → close → push → verify). Why: retry policy, merge queues and
downstream unblocking need a *signal*, and a process exiting is not one.

### B. A self-feeding, self-ordering graph

**P5 — Any agent can file work, cheaply, with provenance.** `bd create --deps
discovered-from:<id>` from inside a worker; the graph grows as work is done. Why: this is
the difference between a queue that drains and a swarm that runs — discovered bugs, follow-ups
and split-out subtasks enter the frontier without a human relay.

**P6 — Ordering primitives rich enough that no orchestrator has to hold state.** Fan-in
(`waits-for` over dynamic children), contingency (`conditional-blocks`), external waits as
gate records (`human`, `timer`, `gh:pr`, `gh:run`), epics with computed progress, `swarm`
wave analysis "computed from beads, not stored separately". Why: multi-step pipelines become
data; a dead orchestrator loses nothing.

**P7 — Multi-step methods as data (formulas → molecules).** A TOML formula with `[[steps]]`,
`needs`, `[vars]`, `extends` materialises into real beads, so the *run* outlives the file,
the session, and the daemon. Why: the swarm executes workflows without an LLM interpreting
a workflow engine, and each step routes to whichever agent/pool it names.

**P8 — Schema boundary: metadata first, columns last.** Orchestrators layer on via
`metadata` (`gc.routed_to`, execution hints, holds as labels) and never require a beads
migration. Why: the ledger stays small and stable; several orchestrators can share it; routing
and holds are filtered "when deciding what to *do*, never when deciding what must *exist*."

### C. Operability for humans and agents

**P9 — Context frugality.** `bd prime` ≈ 1–2k tokens (vs 10–50k for MCP schemas), CLI over
MCP, `--json` everywhere with a versioned envelope, `bd schema` publishes enums, hooks
re-inject after compaction, `.beads/PRIME.md` override, "do not use TodoWrite". Why: the
ledger's cost per turn is what lets you run many agents at once.

**P10 — Explainability.** `bd ready --explain` → `{Ready, Blocked, Cycles}`, `bd blocked`,
`bd dep tree|cycles`, `bd stale`, `bd orphans`, `bd lint`, `bd doctor`. Why: "why isn't X
moving" is the question operators ask hourly; it must be a query, not a log grep.

**P11 — Identity and provenance.** `BD_ACTOR`, `created_by/updated_by`, git author trailers,
an append-only event journal with a cursor. Why: attribution of who did what is the audit
trail for a fleet you are not watching.

**P12 — One surface for humans and agents.** Same CLI, `bd human` subset, fuzzy `bd show`.
Why: humans steer with the same verbs agents use; nothing has to be mirrored.

**P13 — Durable, offline-first, travels with the repo.** Dolt history, sync over
`refs/dolt/data`, works with no daemon. Why (for a *single-machine* swarm): very little —
the daemon is always up. Why (for multi-machine / federated): everything.

**P14 — Ledger hygiene over time.** Ephemeral wisps for control beads, semantic compaction
of old closed issues, `bd purge`. Why: a ledger that grows forever eventually costs context
on every `prime` and every list.

**A property beads deliberately does *not* have:** a scheduler, an agent registry, or any
orchestration ("Beads should not know about orchestration layers built on top of it"). That
clean data-plane boundary is why several orchestrators exist on it. Agent Queue conflates
data plane and control plane on purpose (`ecosystem-positioning.md` §2: "the join is the
product"). That is a trade, not a defect — noted in §3 as P15.

---

## 2. Property-by-property: what Agent Queue has, lacks, and needs

### P1 — graph decides
- **Have:** `tasks.is_blocked` recomputed in-transaction (`blocked_state.py:220`) over the
  same four blocking types, plus gates and `hold:*` labels in the predicate (`:192`);
  `get_ready_frontier(project, labels)` (`:487`) ordered `(priority, created_at)`; cycle
  rejection at write time (`state_machine.py:201`); `waits-for` deadlock rule (`:231`).
- **Lack:** the projection is in **shadow mode** (`work_graph.blocked_state_authoritative:
  false`); the legacy scan still decides (`monitoring.py:52-100`). And nothing *pulls* from
  the frontier — `Scheduler.schedule()` pushes `AssignAction`s.
- **Close:** flip the flag after reading the divergence log; expose the frontier as the
  claim source (P2). *Effort: a day plus P2.* **Parity: yes; the predicate is a superset.**

### P2 — atomic fenced claim + leases
- **Have:** leases (`sessions.lease_ttl_seconds: 480`, `last_activity` from transcripts,
  `aq task heartbeat`), reclaim as the stall ladder (`reconciler.py:544`: nudge → backoff →
  restart → quarantine) and orphan recovery (`:651`); per-session bearer tokens scoped to
  `task_id`/`project_id` (`api/auth.py`, `api/scope.py:34-80`); `_cmd_task_close` refuses a
  session closing someone else's task (`session_commands.py:435`).
- **Lack:** a claim. No `UPDATE … WHERE status='READY' AND is_blocked=0 AND
  assigned_agent_id IS NULL` anywhere; assignment is `assign_task_to_agent` inside
  `_execute_task` (`execution.py:295`).
- **Close:** `_cmd_task_claim` (single CAS on `(id, status, assigned_agent_id)`, typed
  conflict, idempotent re-claim by the same session, `--next` walks the frontier past
  racing claims); fence to the session token (Gas City-style, stronger than beads'
  actor string); admission checks (`_check_constraints_before_assignment`,
  `execution.py:187`) run inside the claim. Add `task_claim` to `AGENT_COMMAND_SET`.
  *Effort: ~300 LOC + tests.* **Parity: yes, and fenced harder.**

### P3 — work state in the ledger
- **Have:** `task_context` rows typed `note | spec_ref | handoff | conversation_context |
  salvage-patch` (`prime/sections.py:117-166`); `task_metadata` work-state contract
  (`work_dir`, `branch`, `pr_url`, `rejection_reason`, `close_notes`, stall counters);
  `aq task set --note/--branch/--pr-url/--meta` (`surface_commands.py:92`); `task_results`;
  crashed-predecessor work archived as a patch on the task rather than `git stash`
  (`worktree_manager.py:19-22`); `aq handoff --auto` on `PreCompact`; **plus** something
  beads lacks: full session transcripts readable by id and streamable.
- **Lack:** `design` / `acceptance` are not editable after creation via the agent surface
  (`task_criteria` is create-time); there is no threaded comment model, only append-only
  context rows (adequate).
- **Close:** `aq task set --design/--acceptance/--append-notes` → `task_context`/`task_criteria`.
  *Effort: ~80 LOC.* **Parity: yes; richer on transcripts.**

### P4 — explicit typed completion
- **Have:** `aq task close --outcome pass|fail --failure-class transient|hard
  --work-outcome shipped|no-op|blocked|abandoned --commit --notes` then `aq session
  drain-ack`; exit-with-open-task classified as failure (`exit_classifier.py`); a
  drain-ack with the task still open is a *premature drain* and earns a nudge, not a
  kill (`reconciler.py:388`); no `Stop` hook by design (`claude.md:125-128`).
- **Lack:** `--claim-next` / `--continue` (needed for a worker loop, P2/P5); the
  "file remaining work" half of landing-the-plane, blocked by P5. Vocabulary drift:
  `aq-tasks/SKILL.md` says `--outcome success|needs_context|failure`, code says
  `pass|fail` (`session_commands.py:42`).
- **Close:** `--claim-next` on close; fix the skill text. *Effort: ~100 LOC.*
  **Parity: yes — ours is typed, beads' `--reason` is free text.**

### P5 — any agent can file work  ← the biggest divergence
- **Have:** `create_task` accepts `depends_on: [{task_id, dep_type: discovered-from}]`
  (`task_commands.py:984-1013`), `dedup_key` for idempotent filing, `ensure_task`; the
  default pipeline already puts a **routing gate** on every `task.created` and spawns a
  `triage` task (`default-pipeline.md` rule 1), so newly filed work is triaged before it
  can run.
- **Lack:** **workers are forbidden to create tasks.** `AGENT_COMMAND_SET`
  (`api/scope.py:14-31`) omits `create_task`; `aq-tasks/SKILL.md:74-76,113` says "Don't
  create tasks from a worker session. That's the supervisor's job." This was decision D2
  (supervisor owns the graph). It is the one place the codebase's philosophy is anti-swarm:
  discovered work has to go through a message to the supervisor.
- **Close:** add `create_task` to the worker scope **with constraints enforced server-side
  by the token scope**: `project_id` pinned to the token; `discovered-from` (or `related`)
  edge to the calling task mandatory; `hold:triage` label (or rely on the existing routing
  gate) so the Mayor/triage decides priority and profile before it enters the frontier.
  This keeps D2's intent — the supervisor still decides what *runs* — while letting the
  graph grow from work. *Effort: ~150 LOC + a skill rewrite.* **Parity: yes, with a
  triage gate beads does not have.**

### P6 — ordering primitives
- **Have:** identical blocking types plus 4 non-blocking; gates `human | timer | pr-merged |
  ci-run | event | task | routing` (a superset of beads' `human|timer|gh:pr|gh:run|bead`)
  swept every 30 s; `resume_after` (≈ `defer_until`); any task is a container;
  `get_group_progress` computes done/ready/blocked, Kahn waves and max parallelism on
  demand (`task_queries.py:613`) — the `bd swarm` rule, adopted verbatim.
- **Have (children):** two layers kept in sync by `create_task --parent-id` — the
  blocking `parent-child` edge (released when the container leaves DEFINED /
  AWAITING_PLAN_APPROVAL; `waits-for` fans in over it dynamically) and the denormalised
  `tasks.parent_task_id` pointer that `get_subtasks` / `get_task_tree` /
  `_check_plan_parent_completion` read. Hierarchical ids `<parent>.<n>` with a depth cap
  that falls back to a root id + `discovered-from` (`task_names.py:140`,
  `task_commands.py:1027-1033`).
- **Lack:** `create_task_graph` still assigns flat ids (`creator.py:57-63`); an
  `add_dependency(dep_type='parent-child')` added after creation sets the edge without the
  pointer, so that child is invisible to tree/auto-completion (column↔edge drift);
  `get_group_progress` has no CLI verb; provenance edges are writable but have no read
  path (`implementation/work-graph.md:264`); no generic `defer_until` on an *open* task
  (today only via PAUSED).
- **Close:** derive tree/completion from the edge (or sync the pointer in
  `add_dependency`), dotted ids in the graph creator, `aq task children`, read path for
  non-blocking edges, `--defer-until` on `edit_task`. *Effort: ~300 LOC.*
  **Parity: yes; superset on gates.**

### P7 — workflows as data
- **Have:** `aq-graph` blocks in specs → `parse_graph` → `validate_graph` (with
  `substitute_vars`, `validator.py:66`) → `create_task_graph` in one transaction
  (`task_graph/creator.py:310`); `aq task create --from-spec --dry-run`. That is a formula
  compiler without a formula *library*. Playbooks (LLM-per-node) exist for judgment steps.
- **Lack:** a registry (`vault/[projects/<pid>/]formulas/<name>.md`), `vars` declared with
  `required/enum/default`, `extends`, `aq formula list|show|cook`, per-step `profile`
  routing already exists on nodes. No control steps (retry-with-class, check loop) —
  comparison §9.2, later.
- **Close:** registry + verbs over the existing compiler. *Effort: ~600 LOC.*
  **Parity: yes for formulas → molecules; wisps not needed until control steps exist.**

### P8 — metadata first
- **Have:** `task_metadata` KV, `task_labels`, `hold:*` convention, and the rule written
  into `work-graph.md` §6; routing (`profile_id`, `intelligence_class`) is *columns* here,
  which is fine — it is one store, not an ecosystem.
- **Lack:** nothing load-bearing. 33 columns where beads has ~15 + metadata is a
  maintenance cost, not a swarm blocker.
- **Close:** nothing now; the status collapse (work-graph §12) is the eventual cleanup.
  **Parity: functionally yes.**

### P9 — context frugality
- **Have:** `aq prime` (ten sections; shipped templates total ~310 words; role/task/context
  on top — the shape of `bd prime`), `.aq/PRIME.md` override with `{{section}}` vars,
  `SessionStart` re-prime on `resume|compact`, `PreCompact` handoff, CLI-first
  (`tool_guidance.md`: "prefer `aq <command>` over tool schemas"), sessions launched with
  **no** `--mcp-config` (`spec.py:449`), `--json` envelope with `schema_version` and
  `--brief` projections (`cli/envelope.py`), `aq schema` enum catalog.
- **Lack:** `aq schema` omits `outcome/failure_class/work_outcome` and session states (its
  docstring says so); prime's token cost has never been measured (`prompt_analytics.jsonl`
  exists in `llm_logger.py` but nothing reports prime size); the per-prompt inbox hook was
  correctly removed, so mid-turn messages arrive by nudge only.
- **Close:** extend `aq schema`; log prime size per session. *Effort: ~60 LOC.*
  **Parity: yes.**

### P10 — explainability
- **Have:** `aq task explain` with typed reason codes across **both** graph reasons
  (blocking deps naming cross-project owners, open gates, holds) **and** capacity reasons
  (no idle agent, workspace locked, budget, rate limit, project paused) — beads only has
  the first half; `aq task project-ready` (frontier + withheld); `get_chain_health`,
  `get_downstream`, `get_task_tree`; write-time cycle rejection; `aq doctor` with 11
  built-in checks incl. `tasks.stuck` and `events.registry`; `/api/projects/{id}/graph`
  feeding the dashboard DAG.
- **Lack:** `stale` (open, untouched > N days), `orphans` (no parent, no edges),
  `lint` (empty description, no acceptance) as doctor checks; `dep cycles` as a query
  (cycles are only prevented, never reported after the fact).
- **Close:** three doctor checks. *Effort: ~150 LOC.* **Parity: yes; superset on
  capacity reasons.**

### P11 — identity and provenance
- **Have:** monotonic `events.id` with `after_seq` replay on the websocket and
  `get_recent_events` (`event_queries.py:48`); `command.invoked` events carrying
  `session_id/task_id/project_id`; per-session tokens identify the caller; `sessions`
  rows with `instance_token`/`epoch`.
- **Lack:** **`tasks` has no `created_by` / `updated_by`** (the only `created_by` column is
  on `project_constraints`, `tables.py:699`); sessions are launched without a git author
  identity (no `GIT_AUTHOR_NAME` in `env.py`), so commits from a worktree carry the
  operator's name; no `Agent-Signature` trailer.
- **Close:** two columns + a migration, stamped from the request scope in
  `CommandHandler.execute`; `GIT_AUTHOR_NAME=aq/<profile>/<session>` in the session env.
  *Effort: ~120 LOC + migration.* **Parity: yes after a small change.**

### P12 — one surface for humans and agents
- **Have:** the same `aq` CLI (agents see 13 commands by token scope, humans see all
  ~180), dashboard, Discord read-only + gate buttons, all generated from one `_cmd_*` map.
  `aq task search` is a client-side filter over `list_tasks` (`cli/tasks.py:342`) —
  fine at this scale.
- **Lack:** nothing.  **Parity: yes; richer (dashboard, live panes).**

### P13 — offline-first, travels with the repo
- **Have:** a central SQLite/Postgres DB behind an always-on daemon; `aq` is an HTTP
  client; `_recover_stale_state` + adoption make daemon restarts safe for running agents.
  The vault is git-friendly markdown; task records under `~/.agent-queue/tasks/` are
  markdown.
- **Lack:** no work-state in the repo; no daemon → no `aq task close`; no multi-machine
  story.
- **Close (cheap, partial):** a beads-compatible **JSONL export** (`aq task export --beads`
  writing `.aq/tasks.jsonl`, `bd import`-able) as a passive artifact, and an import for
  interchange. **Close (real):** not on this store. **Parity: no — by design.** It only
  matters if the swarm spans machines, which nothing in the current goals requires
  (`framework-overhaul-todo.md` D9 keeps one daemon, N projects).

### P14 — ledger hygiene
- **Have:** auto-archive of terminal tasks (`archived_tasks`, `_auto_archive_tasks`),
  LLM-log retention, `events` table growth unbounded but cursor-paged.
- **Lack:** ephemeral tier; semantic compaction (memory-consolidation is paused with
  memory). Neither matters until formulas emit control tasks at volume.
- **Close:** later; `ephemeral` label + purge in the archive sweep is ~50 LOC when needed.
  **Parity: adequate.**

### P15 — data-plane / control-plane boundary (the property beads has by *not* having things)
- **Have:** one store with the readiness join and admission in it — deliberate.
- **Lack:** any way for another orchestrator or tool to consume the ledger.
- **Close:** the `WorkItemStore` protocol (seven `SessionReconciler` calls + three
  `SessionSpecBuilder` fields, per the migration evaluation §4.4) plus the P13 JSONL
  export gives the interop surface without splitting the store. **Parity: partial, and
  the right partial.**

---

## 3. Scorecard

| # | Property | Load-bearing for a swarm? | AQ today | Gap to close | Effort |
|---|---|---|---|---|---|
| P1 | Graph decides | **yes** | built, shadow mode | flip flag; pull from frontier | 1 day + P2 |
| P2 | Fenced claim + leases | **yes** | leases yes, claim no | `aq task claim` CAS, token-fenced | ~300 LOC |
| P3 | Work state in ledger | **yes** | yes (+ transcripts) | `set --design/--acceptance` | ~80 LOC |
| P4 | Typed explicit close | **yes** | yes, stronger | `--claim-next`; skill text | ~100 LOC |
| P5 | Agents file work | **yes** | **forbidden by policy** | scoped `create_task` for workers + triage gate | ~150 LOC + skill |
| P6 | Ordering primitives | **yes** | superset | provenance read path, `defer_until` | ~200 LOC |
| P7 | Workflows as data | yes | compiler, no library | formula registry + verbs | ~600 LOC |
| P8 | Metadata first | supporting | yes | — | 0 |
| P9 | Context frugality | **yes** | yes | `aq schema` completeness; measure prime | ~60 LOC |
| P10 | Explainability | supporting | superset | stale/orphans/lint doctor checks | ~150 LOC |
| P11 | Identity/provenance | supporting | partial | `created_by/updated_by`, git author per session | ~120 LOC + migration |
| P12 | One surface | supporting | yes, richer | — | 0 |
| P13 | Offline / travels with repo | only multi-machine | no, by design | JSONL export/import for interchange | ~250 LOC |
| P14 | Ledger hygiene | later | adequate | ephemeral tier when formulas need it | later |
| P15 | Data-plane boundary | ecosystem only | no | `WorkItemStore` protocol | ~200 LOC |

Total to reach parity on every load-bearing property: **~1.5–2k LOC**, one small
migration, two config flips, and one policy reversal (P5). The three properties where
parity is *not* reached — P13, P14, P15 — are the ones that do not affect a swarm on one
machine.

What the existing store gives that beads cannot, which the scorecard should not hide:
the capacity half of `explain`; gates of type `event`, `task`, `routing`; the merge slot
and all-or-nothing multi-kind workspace acquisition in the same transaction domain as
readiness; typed close outcomes driving retry; session transcripts and live panes keyed
by task; a dashboard.

---

## 4. Requirements restated for *our* swarm

Translating the properties into acceptance criteria for the pull-mode work
(P1–P7 of the migration evaluation), so parity is testable rather than asserted:

1. **No coordinator on the hot path.** With the daemon's `_schedule` step disabled, N
   pool sessions must still drain a project's frontier to empty via `aq task claim --next`
   alone. (P1, P2)
2. **Exactly-once assignment.** 20 concurrent `claim --next` calls against a 10-task
   frontier yield 10 successes and 10 typed `no_ready_work`/`claim_conflict` results, on
   both SQLite and Postgres. (P2)
3. **Crash return.** Kill a claimed session mid-task; within `lease_ttl + one tick` the
   task is back in the frontier, its worktree salvaged as a patch, and the next claimant
   sees the salvage in `aq prime`. (P2, P3)
4. **Self-feeding.** A worker can `aq task create --discovered-from <self>`; the new task is
   routing-gated, triage resolves it, and it appears in the frontier — without any
   supervisor message. (P5, P6)
5. **Resumable mid-flight.** After `PreCompact` handoff or a `--resume` restart, `aq prime`
   returns work_dir, branch, notes, handoff note and acceptance criteria — the session can
   continue without re-reading the transcript. (P3, P9)
6. **Typed close drives policy.** `--failure-class transient` retries with backoff,
   `hard` blocks with the reason surfaced in `explain`. (P4, P10)
7. **Workflows outlive the daemon.** `aq formula cook <name>` materialises a graph; restart
   the daemon mid-run; nothing is lost and the next step is claimable. (P7)
8. **Prime budget.** `aq prime` for a typical task ≤ 2k tokens, logged per session. (P9)
9. **Explain is complete.** For every task not running, `aq task explain` returns at least
   one reason code; `aq doctor` flags stale/orphan/lint conditions. (P10)
10. **Attribution.** Every task mutation and every commit from a worker carries the
    session/profile identity. (P11)

---

## 5. Conclusion

Beads succeeds for swarms because of a small set of properties — graph-decided readiness,
fenced claims with leases, work state in the ledger, typed explicit completion, agents that
can file work, ordering primitives rich enough to hold workflow state, and a context-cheap
surface — none of which depend on Dolt or on the `bd` binary. Agent Queue already has
eleven of fifteen at parity or better, mostly because they were copied on purpose in
August. The remaining load-bearing gaps are one primitive (claim), one policy (workers may
file work), and one library (formulas). The properties it will not match — repo-portable,
offline-first storage and a clean data-plane boundary — are the ones that only matter
across machines or across orchestrators, and a JSONL export plus a store protocol covers
the interop cases cheaply.

Build the swarm on our store; measure it against §4; revisit the store question with data.
