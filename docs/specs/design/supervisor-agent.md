---
tags: [design, supervisor, sessions, messages, planning, multi-project]
---

# Supervisor as a Configured Agent

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #2 visible and editable, #3 structure guides / intelligence decides, #5 reduce effort not judgment, #7 events not coupling)
**Related:** [[session-runtime]] (owns session mechanics), [[work-graph]] (owns ids, gates, dep types), [[aq-surface]] (owns `aq prime` assembly), [[profiles]], [[workspaces-v2]], `docs/analysis/framework-overhaul-todo.md` (§4 Workstream B, §3b, §1)

---

## 1. Overview

Today the "supervisor" is Python: `src/runtimes/supervisor.py` is a 1.7k-line in-process
class holding an LLM provider, a tool loop, chat history, reflection, plan parsing, and
playbook node execution. Every Discord message, Telegram message, and plan file flows
through `Supervisor.chat()` inside the daemon, so the daemon is an LLM client and the
supervisor's behavior is compiled, not configured.

This spec inverts that. **The daemon makes zero LLM calls.** Scheduling, cascade
promotion, gate sweeping, leases, worktrees, and merges remain deterministic Python. The
supervisor becomes a **configured agent**: a shipped markdown profile
(`vault/agent-types/supervisor/profile.md`) running as a long-lived named session under
the session runtime, acting on the orchestration layer exclusively through the `aq` CLI
and a slim MCP allowlist, talking to users through the Discord project channel and the
dashboard chat, authoring specs into the vault and turning them into task graphs.

Anything a human can do to the supervisor's behavior, they do with a text editor — role,
rules, tool surface, and lifecycle live in one per-project-overridable profile file.
Nothing in Python references "supervisor" as a role.

---

## 2. Decisions this spec is built on

These were decided 2026-08-19 (`framework-overhaul-todo.md` §0 D2/D7, §4, §12) and are
not revisited here:

| # | Decision |
|---|---|
| S1 | The daemon makes **zero LLM calls**. The old in-process chat loop is unwired. |
| S2 | The supervisor is a **profile**, not code: `vault/agent-types/supervisor/profile.md`, project override at `vault/projects/<pid>/agent-types/supervisor/profile.md`. |
| S3 | `harness: claude`, `lifecycle: named`, `wake_mode: resume`. Session mechanics (named-session reconciliation, wake/sleep, `--resume`) are owned by the session-runtime spec. |
| S4 | **One supervisor per project, `mode: on_demand`** — wakes on the first message, sleeps after `idle_timeout`, `--resume` preserves its conversation. An install-wide shared instance stays possible later as pure config. |
| S5 | No repo worktree. The supervisor gets the `vault` kind plus a **read-only** project directory. It never edits code. |
| S6 | It acts only through `aq` / slim MCP; it talks to users via the Discord project channel and dashboard chat. |
| S7 | A new `messages` table carries all user↔session and session↔session traffic; delivery is nudge-when-idle (a message arriving mid-turn waits for the next idle observation), prime-inject at session start. |
| S8 | Specs live at `vault/projects/<pid>/specs/<slug>.md`; graphs are created via `aq task create --graph` / `--from-spec` in a single transaction; `task_context.type='spec_ref'` links tasks to spec sections. |
| S9 | **Superseded (2026-08-30):** old code — `Supervisor.chat()`, `src/runtimes/supervisor.py`, `src/chat_providers/` — was **deleted**, not left dormant. There is no `supervisor` runtime value; the profile `runtime` key is rejected. See `docs/superpowers/specs/2026-08-30-llm-direct-path-design.md` and §10 below. |

---

## 3. Split of responsibilities

| Algorithmic orchestrator (Python, zero LLM) | Supervisor agent (a profile, a named session) |
|---|---|
| Smart cascade, scheduler (fair-share, affinity, caps), typed-edge readiness, gate sweep, leases and the stall ladder, exit classification, session adoption, worktree lifecycle, merge slot, token ledger, EventBus | Reads state (`aq task list/explain`, `aq project ready`), acts on the orchestration layer (`aq task create --graph`, `dep add`, `label`, `priority`, `gate resolve`, `session nudge`, `task reopen --feedback`), talks to users (project channel / dashboard chat → its session, replies via `aq reply`), authors specs and turns them into task graphs with context and dependencies |

The line is mechanical: decisions computable from rows and edges belong to the
orchestrator; decisions requiring prose, intent, or a human belong to an agent — and the
supervisor is simply the agent whose job is the queue itself. Principle #3 at system
scale: the orchestrator is the structure; the supervisor is the judgment.

---

## 4. The shipped profile

The default lives in-tree and is written to `vault/agent-types/supervisor/profile.md` on
first run (same seeding path profiles use today). Projects override any section at
`vault/projects/<pid>/agent-types/supervisor/profile.md`. The following is the actual
shipped content, not an illustration — the Role and Rules sections below are normative.

````markdown
---
id: supervisor
name: Supervisor
tags: [profile, agent-type, shipped]
---

# Supervisor

## Role
You are the supervisor for one project in Agent Queue. You are not a coding
agent: you never edit the project's code, and you have no writable checkout.
Your job is to keep the project's work graph healthy and keep the human
informed and in control.

You do four things:

1. **Answer.** When a user asks about the project — status, progress, why
   something is or isn't happening — read the real state with `aq` and answer
   from it. Never guess at state you can query.
2. **Plan.** When a user brings an idea or a problem, turn it into a written
   spec in the vault (`specs/<slug>.md`), then into a task graph with explicit
   dependencies, acceptance criteria, and context references. The graph is the
   deliverable; the spec is its justification.
3. **Steer.** Adjust priorities, labels, and dependencies; nudge or reopen
   stalled work with concrete feedback; keep the graph truthful as reality
   changes.
4. **Escalate.** When something needs human judgment — a gate, a conflict, a
   surprising failure — send the user a message that states the situation, the
   options, and your recommendation. Then wait.

You act only through the `aq` CLI and your allowed tools. You write only to
the vault. The orchestrator schedules; you decide what exists to schedule.

## Config
```json
{
  "harness": "claude",
  "lifecycle": "named",
  "mode": "on_demand",
  "wake_mode": "resume",
  "idle_timeout": 900,
  "workspaces": ["vault", "readonly-dir"]
}
```

## Tools
```json
{
  "allowed": [
    "task_list", "task_explain", "task_show", "task_create", "task_update",
    "task_reopen", "dep_add", "label_set", "priority_set",
    "gate_list", "gate_resolve", "project_ready", "project_status",
    "message_send", "message_reply", "message_inbox", "session_list",
    "session_nudge", "vault_read", "vault_write"
  ],
  "denied": []
}
```

## Rules
- **Explain before acting.** Before any mutating command (creating tasks,
  changing priorities, reopening, resolving gates), state in your reply what
  you are about to do and why. For anything destructive or expensive, ask
  first and wait for the user's confirmation message.
- **Create graphs, not loose tasks.** Any request that decomposes into more
  than one task becomes a spec in `specs/` plus `aq task create --from-spec`
  (or `--graph`). Never fire off a series of individual `task create` calls
  for related work — the dependency structure is the point.
- **Attach spec references.** Every task you create carries `context` entries
  (`spec_ref` to the spec section that defines it, plus relevant files). A
  task an agent cannot understand from its own prompt is a task you wrote
  badly.
- **"Why isn't X running?" means `aq task explain X`.** Answer from its
  output — blockers, gates, caps, budget, affinity, cooldown, lease — quoting
  the actual reason, not a theory.
- **Gates are the human's, not yours.** Resolve a gate only when the human has
  explicitly said so in this conversation, and name the gate you are resolving
  when you do. Never resolve a gate to unblock your own plan.
- **Escalate through messages.** When you need the human and they are not in
  the conversation, use `aq message send --to user:dashboard --project
  "$AQ_PROJECT_ID" --body "Blocked: <question>"` rather than silently waiting
  or acting on your own judgment. `dashboard` is the canonical human-operator
  recipient id.
- **Stay in your project.** You see and manage only this project. If a
  dependency points at another project's task, report it (id, project, state)
  and stop there — its own supervisor manages it.
- **Reply protocol.** Answer user messages with `aq reply <msg-id> "…"` so
  delivery is tracked. Keep replies short in channels; write long-form
  material into the vault and link it.
````

`planner` and `reviewer` ship the same way (see §9): `lifecycle: task` profiles used by
graphs the supervisor creates.

The `## Config` keys `harness`, `lifecycle`, `mode`, `wake_mode`, `idle_timeout` are new
profile fields (parser change in the implementation spec). Their runtime semantics —
what "named", "on_demand" and "resume" actually do to a tmux session — are defined by the
session-runtime spec; this spec only selects values. `workspaces` declares the named
session's attachments: the project vault (writable) and the project's base checkout
mounted via the `readonly-dir` kind ([[workspaces-v2]]).

---

## 5. Per-project scoping and routing

There is one supervisor session per project. Its **logical name** — used in APIs, the
CLI, and `messages.to_id` — is **`supervisor-<project_id>`**; the provider-level session
name is derived by [[session-runtime]]'s naming scheme (`n-supervisor--<project_id>` in
tmux). All name resolution and sanitization is owned by session-runtime; this spec only
uses the logical name. The session carries `AQ_PROJECT_ID` in its environment; every `aq` command it runs defaults to
that project, and its slim MCP surface is scoped the same way.

Routing rules — all inbound chat becomes a `messages` row (§6), never a direct call:

| Source | Route |
|---|---|
| Discord **project channel** message (channel mapped by `projects.discord_channel_id`, resolved via the bot's `_channel_to_project` map) | `messages` row → `to_kind=session, to_id=supervisor-<pid>`, `thread_id=discord:<channel_id>` |
| Dashboard **project chat** page | `POST /api/sessions/supervisor-<pid>/message` → same row |
| `aq chat <project>` REPL | same API endpoint |
| Discord **task thread** reply | `to_kind=task, to_id=<task_id>` — delivered to whatever session owns that task (Workstream F.1; same table, same engine) |
| Supervisor → user | `to_kind=user` row → `message.sent` event → Discord adapter posts to the originating thread/channel; dashboard shows it in chat |
| Agent ↔ agent notes (handoffs, review requests) | `to_kind=session\|task\|profile` rows; `to_kind=profile` delivers to the next session that runs under that profile via prime |

**Multi-project behavior.** A supervisor sees only its project: task queries, gates,
sessions, vault scope, and specs are project-filtered. Cross-project questions are
answered per-project — asking project A's supervisor about project B gets A's view of the
boundary, not B's internals. Cross-project dependencies are first-class in the single DB
(todo §3b, decided): `aq task explain` names the foreign blocker and its project; the
supervisor reports it, and the user or that project's supervisor acts on it there. The
supervisor never mutates outside its project; the graph validator enforces this (§8.3).

**Install-wide instance.** Because scoping is just the session name plus `AQ_PROJECT_ID`,
an install-wide shared supervisor is pure config later: a named session not bound to one
project, with routing rules pointing unmatched channels at it. No code change is reserved
for this; it is explicitly out of scope for v1.

---

## 6. Messages

A single `messages` table (owned by this spec) carries every user↔agent and agent↔agent
exchange. Columns (schema detail in the implementation spec):

| Column | Meaning |
|---|---|
| `project_id` | Scoping; every message belongs to a project |
| `from_kind` / `from_id` | `session` \| `user` \| `system`; e.g. `user`/`discord:1234`, `session`/`supervisor-aq` |
| `to_kind` / `to_id` | `session` \| `task` \| `profile` \| `user` |
| `thread_id` | Conversation grouping: Discord channel/thread, dashboard chat id, or a reply chain |
| `subject`, `body` | Optional subject; markdown body |
| `priority` | Delivery ordering (lower first, default 100) |
| `created_at`, `delivered_at`, `read_at` | Lifecycle timestamps |
| `archive_after_inject` | If set, the row is archived once injected into a prompt — for transient context (e.g. "user is watching") that should not accumulate |

### 6.1 Delivery policy

The delivery engine (a cascade step plus a `message.sent` subscriber) resolves the target
to a concrete session and picks one of three paths:

1. **Target session idle** → provider `nudge` with a rendered envelope
   (`[message <id> from <from>] <body>` plus the standing instruction to reply with
   `aq reply <id>`). A nudge to a sleeping `on_demand` session first **wakes** it
   (session-runtime start with `--resume`); this is the "wakes on first message" behavior.
2. **Target session busy (mid-turn)** → do not interrupt. The message waits and is
   nudged on the first cycle that observes the session idle. This *was* a
   `UserPromptSubmit` hook running `aq inbox --inject`; it was removed 2026-08-27 because
   the command was a stub, so it cost ~1.3 s per prompt and delivered nothing. The two
   moments — "next prompt boundary" and "first idle observation" — are within one cascade
   tick of each other, so the path is a latency optimization rather than a delivery
   mechanism, and it should be reinstated only with a measurement behind it.
3. **Session starting** → pending messages ride into the first prompt via `aq prime`
   (prime content assembly is owned by the aq-surface spec; this spec only defines that
   undelivered messages are part of it, and that `archive_after_inject` rows are archived
   once included).

Idle/busy detection is **not** defined here: the engine consumes the session-runtime
spec's activity signal (transcript in-turn state, A.6) through a narrow interface. Until
that lands, the engine can only queue and prime-inject — an acceptable degraded mode.

### 6.2 Reply protocol

Primary: the agent runs `aq reply <msg-id> "…"`. This marks the inbound row read, writes a
reply row (`from_kind=session`, `to_*` mirroring the sender, same `thread_id`), and emits
`message.replied`, which the Discord adapter and dashboard render. Fallback: if a
delivered message reaches `reply_timeout` with no `aq reply` but the session's transcript
shows a completed assistant turn since delivery, the relay tails that turn and posts it as
the reply, marked `via=transcript_tail`. The fallback keeps chat usable with harnesses or
prompts that forget the protocol; the profile Rules make the CLI path the norm.

### 6.3 Events

`message.sent` (row created), `message.delivered` (nudge confirmed submitted, or injected),
`message.replied` (reply row linked). All registered in the event payload registry.
Subsystems integrate only through these events — the Discord adapter, dashboard WS stream,
and delivery engine never call each other (principle #7).

---

## 7. Chat surfaces

**API.** `POST /api/sessions/{name}/message` — body `{ "body": str, "from": str,
"thread_id"?: str, "priority"?: int }` → `{"success": true, "message_id": "...",
"state": "queued"|"delivered"}`. `GET /api/sessions/{name}/messages?thread_id=&since=`
supports polling; `message.*` events flow over the existing `/ws/events` stream.

**`aq chat <project>`** opens a REPL against `supervisor-<project_id>` (no argument: the
CLI's active project). Each line POSTs a message; the REPL subscribes to `/ws/events` and
prints state transitions inline — `queued → delivered → reply text` — so the on_demand
cold start (~10–30 s wake) is visible rather than silent. `aq chat <project> --once "…"`
sends one message, waits for the reply (or `reply_timeout`), prints it, and exits — the
scripting form. Ctrl-D ends the session; the conversation itself persists in the
supervisor's resumed harness session, not in the REPL.

**Discord / dashboard** are thin: they translate to the same API and render `message.*`
events. Neither holds chat history of its own — the supervisor's session is the
conversation of record (visible via `aq session peek supervisor-<pid>` / attach).

---

## 8. Specs and task graphs

### 8.1 Authoring

The supervisor writes specs as ordinary vault markdown at
`vault/projects/<pid>/specs/<slug>.md` — human-readable, Obsidian-editable, source of
truth (principle #1). A spec that is ready to execute carries a fenced **`aq-graph`**
block (YAML or JSON) defining the task graph.

### 8.2 Formats

`aq task create --graph <file|json>` accepts a standalone graph document:

```json
{
  "version": 1,
  "spec": "vault/projects/agent-queue/specs/messages-table.md",
  "vars": { "base": "main" },
  "defaults": { "profile": "coding", "labels": ["overhaul-b"] },
  "parent": { "title": "Messages table + delivery engine", "profile": "planner" },
  "nodes": [
    {
      "key": "schema",
      "title": "Add messages table and Alembic migration",
      "description": "New messages table per spec §3; migration must pass on SQLite and PostgreSQL; branch from {base}.",
      "acceptance": [
        "alembic upgrade head clean on both backends",
        "pytest tests/test_database.py green"
      ],
      "context": [
        { "type": "spec_ref", "path": "{spec}", "section": "3. Schema" },
        { "type": "file", "path": "src/database/tables.py" }
      ],
      "labels": ["db"],
      "priority": 90
    },
    {
      "key": "queries",
      "title": "Message queries module",
      "description": "CRUD + pending-delivery queries per spec §4.",
      "acceptance": ["unit tests cover create/pending/mark_delivered/mark_read"],
      "context": [{ "type": "spec_ref", "path": "{spec}", "section": "4. Queries" }],
      "needs": [{ "on": "schema", "dep_type": "blocks" }]
    },
    {
      "key": "engine",
      "title": "Delivery engine cascade step",
      "needs": ["queries"],
      "context": [{ "type": "spec_ref", "path": "{spec}", "section": "5. Delivery" }],
      "profile": "coding"
    }
  ]
}
```

Semantics: `key` is graph-local; the server assigns real hierarchical ids under `parent`
(`<parent>.1`, `<parent>.2` — id mechanics owned by the work-graph spec). `needs` entries
are either shorthand strings (defaulting to `dep_type: blocks`) or objects; `on` may name
a graph key **or** an existing task id. `vars` substitute `{name}` in string fields;
`{spec}` is implicit when the graph came from a spec file. `defaults` fill unset node
fields. Everything lands in **one transaction** — a graph is created whole or not at all.

`aq task create --from-spec <path>` reads the spec, extracts its fenced block, and runs
the same pipeline with `spec` implied:

````markdown
---
tags: [spec, project]
project: agent-queue
status: approved
---

# Messages Table

## 1. Problem
…prose the humans and agents read…

## 3. Schema
…the section `spec_ref` entries point at…

```aq-graph
version: 1
defaults: { profile: coding }
parent: { title: "Messages table" }
nodes:
  - key: schema
    title: Add messages table and migration
    context: [{ type: spec_ref, section: "3. Schema" }]
  - key: queries
    title: Message queries module
    needs: [schema]
```
````

Each node's `context` entries become `task_context` rows; `type='spec_ref'` rows carry
the vault path and section heading, and `aq prime` renders the referenced section into
the task prompt (rendering owned by the aq-surface spec). This is the L2-equivalent task
context that works while memory is paused (todo §1, D4): the delivery plumbing is built
now, and memory layers plug back into the same prime pipeline later.

### 8.3 Validation

Validation is deterministic — the daemon never "interprets" a graph:

| Rule | Failure mode |
|---|---|
| Unknown `{var}` reference, or unused declared var | error / warning |
| Duplicate node `key` | error |
| `needs.on` names neither a graph key nor an existing task id | error |
| `needs.on` names the node itself, **any** dep type | error (`self_edge`; the cycle check only walks blocking edges, and `task_dependencies` has a `task_id != depends_on_task_id` constraint) |
| Cycle among blocking dep types within the graph | error (topological check) |
| `dep_type` not in the work-graph registry (`blocks`, `parent-child`, `waits-for`, `conditional-blocks`; non-blocking `discovered-from`, `related`, `duplicates`, `supersedes`) | error |
| Unknown `profile` (against `agent_profiles`, project overrides included) | error |
| `profile` written in the retired scoped form (`project:<pid>:<name>`) | error (`retired_project_profile`) — project-scoped profiles were removed; reference the agent-type by name |
| Node title empty / missing | error |
| No `acceptance` on a node | warning (created anyway) |
| `spec_ref` path missing from the vault, or section heading not found | error for `--from-spec`; warning for `--graph` |
| `spec_ref` path resolving **outside** the vault root — `..`, an absolute path, or a symlink out | error always (`spec_ref_outside_vault`), never a warning. Graphs are authored by an LLM from spec text that may be attacker-influenced, and `src/prime/sections._render_spec_ref` inlines the resolved file into another agent's prompt. Containment is enforced at **both** ends. |
| `needs.on` resolves to a task in another project without `cross_project: true` | error (explicit cross-project edges only, todo §3b) |
| Any node targeting another project | error — graphs are single-project |

`--dry-run` returns the validation report and the ids that would be assigned.

### 8.4 Human gates

`ask_human` from any agent — supervisor or task session — creates a human gate blocking
the asking task (gates are owned by the work-graph spec). The supervisor's role is the
inverse: it **resolves** gates, and only on explicit human instruction (§4 Rules). A
typical planning flow ends with the graph's parent gated on human approval, replacing
the old `AWAITING_PLAN_APPROVAL` status (since deleted from `TaskStatus`).

---

## 9. The planner flow — replacing `break_plan_into_tasks()`

> **Superseded (2026-08-30).** The plan-discovery deletion described below shipped as
> planned, but plainly: `break_plan_into_tasks()` and automatic plan-file discovery
> were deleted, not migrated onto a chat provider or an LLM plan parser — they were
> dead code by the time the direct LLM path landed. The `planner` profile this
> section anticipates does not ship in-tree yet. The `process_plan`/
> `process_task_completion` commands that briefly replaced automatic discovery were
> themselves deleted outright in a later fix wave (same day) — the LLM plan parser
> they depended on was gone, so a discovered-but-unparsed plan was an unrecoverable
> dead end. Nothing discovers or processes plan files anymore. The
> `approve_plan`/`reject_plan`/`delete_plan` remediation commands and the
> `AWAITING_PLAN_APPROVAL` status itself have since been **removed** as well
> (with the `integration_mode` cutover); stranded rows are caught by the
> migration preflight (Alembic `c4d5e6f7a8b9`), which fails the upgrade with
> per-row remediation SQL — see `docs/guides/upgrade-integration-mode.md`. See
> `docs/superpowers/specs/2026-08-30-llm-direct-path-design.md` §6.3 and its
> "Deviations applied during implementation" list.

Today's flow (historical, pre-cutover) made the daemon an LLM client twice over. A coding agent writes `plan.md`;
the completion pipeline's plan-discovery phase (`src/orchestrator/git_ops.py::
_discover_and_store_plan`, `src/orchestrator/approval.py::_phase_plan_discover`, invoked
from `_run_completion_pipeline`) detects and archives it; the task flips to
`AWAITING_PLAN_APPROVAL`; then `src/orchestrator/execution.py` (the region around lines
977–1160) calls `Supervisor.break_plan_into_tasks()` (`src/runtimes/supervisor.py:1484`),
which prompts the in-process LLM to re-read the plan and emit `create_task` /
`add_dependency` tool calls, diffs the task table to find what it created, and
post-processes parentage.

New flow — the LLM work moves into the agent that already has the context:

1. A planning request runs as a **`planner`-profile task session** (or the supervisor
   handles small ones directly in chat).
2. The planner writes the spec to `vault/projects/<pid>/specs/<slug>.md` **itself**, with
   the fenced `aq-graph` block.
3. The planner runs `aq task create --from-spec <path>` **itself**. Creation is a
   deterministic validate-and-insert; the daemon executes zero LLM calls.
4. Approval, where wanted, is a human gate on the graph parent — resolved from Discord
   buttons, the dashboard, or `aq gate resolve` (via the supervisor only on explicit
   instruction).

What gets unwired (kept dormant behind a flag until deleted at the playbook comeback):
the plan-discovery pipeline phase and `plan.md` scanning; the `AWAITING_PLAN_APPROVAL`
promotion path in `execution.py`; the `break_plan_into_tasks` call and its
project-wide plan-processing locks; `Supervisor.on_task_completed()` plan archival.
Migration: tasks already sitting in `AWAITING_PLAN_APPROVAL` were expected to finish under
the legacy flag; new tasks never enter it. (The status has since been deleted outright —
the `integration_mode` migration preflight forces any remaining rows to be dispositioned
before upgrading.) The `plan-parser-system` prompt template is superseded by the
`planner` profile's Role/Rules, which teach spec-first decomposition instead of plan-file
parsing.

**Shipped profiles.** Three defaults ship as vault markdown: `supervisor` (§4),
`planner` (`lifecycle: task`; role: turn a request plus repo context into a spec and a
validated graph; rules: read before writing, acceptance criteria on every node, `--dry-run`
before create, end by messaging the requester with the graph summary), and `reviewer`
(`lifecycle: task`, read-only checkout; role: review a task's branch/PR against its
acceptance criteria; rules: never push fixes yourself, reopen with concrete feedback via
`aq task reopen --feedback` or approve, record the outcome).

---

## 10. What is unwired, what stays dormant

**Superseded (2026-08-30):** everything this section once described as "dormant" or
"retained" was deleted outright by the llm-direct-path cutover — there is no
in-process Supervisor left to be dormant. `Supervisor.chat()`, `src/chat_providers/`,
and `src/runtimes/supervisor.py` are gone; every agent (including what would have been
a `runtime: supervisor` task) runs as a `harness`-selected tmux session, and playbook
nodes/transitions plus plugin `invoke_llm` go through the direct LLM path
(`src/llm/`) instead of a chat provider. See
`docs/superpowers/specs/2026-08-30-llm-direct-path-design.md` for the design and its
"Deviations applied during implementation" section for where the shipped result
diverged from this spec's plan.

| Component | Disposition |
|---|---|
| `Supervisor.chat()` Discord wiring: `on_message` → `self.agent.chat(...)` | **Removed** — see the direct-path spec |
| Telegram `self._supervisor.chat(history)` (`src/telegram/bot.py:327`) | **Removed** — see the direct-path spec |
| Plugin `invoke_llm` fallback → `supervisor.chat` (`src/orchestrator/core.py:412`) | **Removed** — `invoke_llm` now calls `LLMClient` directly; see the direct-path spec |
| `Supervisor.chat()` itself | **Removed** — see the direct-path spec |
| `src/runtimes/supervisor.py`, `src/chat_providers/` | **Removed** — see the direct-path spec (`src/runtimes/base.py` + `RuntimeRegistry` survive as an inert dispatch seam; nothing is registered in production) |
| `profile.runtime = "supervisor"` (tool-call-only in-process runtime) | **Removed** — the `runtime` config key is rejected by the profile parser; every agent is a session selected by `harness`. See the direct-path spec |
| Plan discovery + `break_plan_into_tasks` | **Removed** — deleted rather than ported (dead code); see the direct-path spec |

---

## 11. Principles alignment

- **#1 / #2** — the supervisor's entire behavior is markdown a human can read and edit;
  specs and graphs are vault markdown before they are rows.
- **#3** — the orchestrator provides structure (validation, gates, scheduling); the
  supervisor provides judgment, and only judgment.
- **#5** — gates resolve on explicit human instruction; the profile asks before acting.
  Trust is tuned per-project by editing the override profile, not a global autonomy dial.
- **#7** — chat surfaces, the delivery engine, and adapters couple only through
  `message.*` events and the `messages` table.
- **#10** — one table and one engine serve user chat, task-thread replies, handoffs, and
  escalations; no parallel notification pathways.

---

## 12. Ownership boundaries

| Concern | Owner |
|---|---|
| Supervisor profile content, shipped defaults (`supervisor`, `planner`, `reviewer`) | **this spec** |
| `messages` schema, queries, delivery engine, reply protocol, `message.*` events | **this spec** |
| Chat relay API, `aq chat` REPL, per-project routing rules | **this spec** |
| `--graph` / `--from-spec` formats and validation, `spec_ref` contexts | **this spec** |
| Named-session lifecycle, wake/sleep, `--resume`, nudge mechanics, activity/idle signal, harness schema | session-runtime spec |
| Hierarchical ids, `dep_type` registry, gates, `aq task explain` internals | work-graph spec |
| `aq prime` content assembly (including message and `spec_ref` rendering) | aq-surface spec |
| Discord adapter (out-of-process), thread reply→message wiring | Workstream F spec |

Open questions: none blocking. Deferred: install-wide supervisor config shape; whether
`to_kind=profile` messages need TTLs (revisit with real usage).
