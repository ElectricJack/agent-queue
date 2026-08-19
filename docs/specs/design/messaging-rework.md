---
tags: [design, messaging, discord, dashboard, api, overhaul]
---

# Messaging Rework — Out-of-Process Discord, Dashboard as Primary UI

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #2 visible and editable, #5 reduce effort not judgment, #7 events not coupling, #10 fewer moving parts)
**Related:** [[../analysis/framework-overhaul-todo]] (D7, Workstream F §9), [[session-runtime]] (notify events, transcripts, SSE), [[supervisor-agent]] (messages table, chat relay), [[work-graph]] (gates, event log, `after_seq`), [[aq-surface]] (commands, `--json` envelope), [[../messaging/base]] (superseded in part), [[../messaging/discord]] (superseded), [[../messaging/telegram]] (removed)

---

## 1. Problem

Messaging is today the daemon's largest single liability. `src/discord/` (12.1k lines) and
`src/telegram/` (1.6k lines) run **inside** the daemon process: `main.py` builds a
`MessagingAdapter` before the orchestrator starts scheduling, waits up to 120 s for the
gateway, and wires the Supervisor chat loop, notification handler, and 122 mirrored slash
commands directly into daemon internals (`bot.agent.handler`, `orchestrator._adapters`,
`orchestrator.db`). The consequences:

- **Blast radius.** A discord.py regression, a gateway outage, or an invalid-request ban
  affects the process that schedules tasks and owns the database. The `rate_guard` module
  exists precisely because Discord can ban the daemon's IP.
- **Deploy coupling.** Restarting the bot means restarting the orchestrator; the daemon's
  dependency set carries a pinned `discord.py>=2.5.2,<2.6` and (optionally)
  `python-telegram-bot`.
- **Surface duplication.** The 122 slash commands in `src/discord/commands.py` (4.4k lines)
  mirror `CommandHandler` one-to-one and each carries bespoke embed formatting. Every new
  command grows three surfaces (MCP, CLI, Discord). The project wizard and ad-hoc views
  duplicate flows the dashboard already does better.
- **Two transports, one user.** Telegram is a parallel 1.6k-line implementation of the same
  port that nobody exercises.
- **Wrong primary UI.** The dashboard (`dashboard/`, React 19, generated TS client, live WS
  events) is architecturally the right operator surface — API-first, typed, no rate limits —
  but it lags Discord in features because Discord got them first.

The framework overhaul (D1/D2) also removes what today's Discord streaming depends on: the
SDK message callback dies with `claude_sdk`/`acpx`. Streaming must be rebuilt on
transcript-reader events regardless — which makes this the right moment to move the whole
adapter out of process.

---

## 2. Decisions (canonical)

| # | Decision |
|---|---|
| M1 | **Telegram is removed entirely** — `src/telegram/` and the `python-telegram-bot` dependency are deleted, not paused. |
| M2 | **Discord becomes a separate process** in `packages/aq-discord/` with its own `pyproject.toml` and its own `discord.py` dependency. After migration the daemon never imports discord.py. |
| M3 | The bot talks to the daemon **only via REST + WebSocket** — the same `/api/execute` + `/api/ws` surface the dashboard uses — authenticated with a **service token**, with reconnect/backoff and `after_seq` resume. |
| M4 | Discord keeps five features: per-task threads (streamed from transcript events, in-place edits), thread replies → agent messages / gate answers, gate & approval buttons, project-channel chat → the project's supervisor session, and a minimal (≤6) set of read-only slash commands. |
| M5 | Removed: the 122 mirrored command handlers, the project wizard, and the ad-hoc views (`src/discord/commands.py`, `project_wizard.py`, most of `views.py`). |
| M6 | `src/messaging/` stays in-tree as the transport-port abstraction (used by the interim in-process adapter and any future in-process transport). Its Telegram branch goes now. |
| M7 | **The dashboard is the primary UI.** Every dashboard feature follows the API-first rule: a named `CommandHandler` command + a registered Pydantic response model + the generated TS client — no dashboard-private endpoints. |

Rationale for M2 follows Gas City's `extmsg` precedent: chat adapters are external
processes that can crash, restart, be replaced, or be absent without the orchestration core
noticing. This is a deliberate exception to principle #10 (fewer moving parts): the second
process buys crash isolation, independent deploys, and a hard guarantee that the daemon's
event loop can never stall on a chat platform. Principle #7 is what makes it cheap — the
daemon already communicates through events; the bot becomes just another subscriber.

---

## 3. Architecture

```
┌────────────────────────── daemon (Linux/WSL2) ──────────────────────────┐
│ orchestrator ── EventBus ──► event log (seq, durable)  [work-graph]     │
│      │                            │                                     │
│ CommandHandler ◄── /api/execute   ├──► /api/ws?after_seq=N  (WS fanout) │
│      ▲                REST        └──► /api/sessions/{id}/stream (SSE)  │
└──────┼────────────────────────────────────┬─────────────────────────────┘
       │ service token                      │ service token
┌──────┴──────────┐                 ┌───────┴─────────┐
│  dashboard SPA  │                 │  aq-discord     │  packages/aq-discord/
│  (primary UI)   │                 │  (own process)  │──► Discord gateway
└─────────────────┘                 └─────────────────┘
```

Three properties define the boundary:

1. **The daemon is authoritative and bot-agnostic.** It emits events into the durable event
   log ([[work-graph]]) and serves commands. It has no knowledge that a Discord process
   exists; if `aq-discord` is down, nothing in the daemon degrades.
2. **The bot is a projection.** Discord threads, embeds, and buttons are a *view* of daemon
   state, rendered from events at-least-once and made idempotent by event `seq`. The bot
   holds only presentation state (thread ↔ task registry, last-rendered seq) and can rebuild
   it.
3. **All writes go through commands.** A button click, a thread reply, a channel chat
   message — each becomes exactly one `CommandHandler` command over REST, returning the
   standard `{"success": bool, ...}` envelope. The bot contains zero business logic.

---

## 4. What Discord keeps

### 4.1 Per-task threads (observe surface)

A thread is created in the project's channel when a task's session starts
(`task.session.started`), not when the task is created — DEFINED/READY tasks are dashboard
material. The thread streams the agent's activity from `task.session.output` events, which
[[session-runtime]] now feeds from **transcript readers** (the harness's own JSONL
transcripts) instead of the dead SDK callback. Rendering ports the two patterns that already
work in `src/discord/notification_handler.py` and `src/discord/rate_guard.py`:

- **Per-stream worker with latest-wins coalescing** (`_stream_worker` /
  `_handle_streamed_task_message`): one long-lived worker per stream owns Discord I/O;
  the event consumer only overwrites `latest_text` and pings. A rate-limited Discord can
  never back up the WS consumer; intermediate frames are dropped, never queued.
- **In-place message edits** with ~1990-char chunk chaining under one stream id.
- **Invalid-request rate guard** (sliding-window 401/403/429 circuit breaker with
  warn/critical/halt tiers) — ported verbatim; non-critical renders are shed first.

### 4.2 Thread replies → the agent

A human message in a task thread becomes structured input to the task, replacing today's
ad-hoc description-append (`_handle_task_thread_message` in `src/discord/bot.py`):

- If the task has an **open human gate** (an `aq ask` question or approval —
  [[work-graph]]), the reply resolves it via `gate_resolve`.
- Otherwise it becomes a **`messages` row addressed to the task** ([[supervisor-agent]])
  via `message_send`; the daemon delivers it by nudge (or `UserPromptSubmit` inject) per
  the session-runtime delivery rules. The bot never touches sessions directly.

Routing rules, loop prevention, and permissions are specified in §7.

### 4.3 Gate & approval buttons

`gate.created` events render as an embed with Approve / Reject / Answer buttons in the task
thread (plus a brief in the channel). Buttons call `gate_resolve` with the actor's identity.
This replaces the five bespoke view classes (`TaskApprovalView`, `PlanApprovalView`,
`AgentQuestionView`, `TaskFailedView`, `TaskBlockedView`) with one gate-shaped view, because
plan approval, task approval, and agent questions all collapse into gates ([[work-graph]]).

### 4.4 Project channel chat → the project supervisor

A message in a mapped project channel relays to that project's supervisor session via the
relay API owned by [[supervisor-agent]] (`session_message_send` →
`POST /api/sessions/{name}/message`). The supervisor's reply (a `message.replied` event or
tailed assistant turn) posts back to the channel. This replaces the in-process
`Supervisor.chat()` loop, the channel history buffer, and channel summarization — the
supervisor session owns its own conversation memory (`--resume`).

### 4.5 Minimal slash commands (6)

| Command | Backs onto | Why it survives |
|---|---|---|
| `/status` | `system_status` | The single highest-frequency glance; answerable in one embed. |
| `/tasks [project] [status]` | `list_tasks` | Orientation before replying in a thread. |
| `/explain <task>` | `task_explain` | "Why isn't X running" is *the* support question; explain is built for it ([[work-graph]]). |
| `/peek <task>` | `session_peek` | See the live pane without leaving Discord; complements thread streaming. |
| `/gates [project]` | `gate_list` | What is waiting on a human right now. |
| `/attach <task>` | `session_attach_command` | Prints the `tmux attach` command — the bridge to the real terminal. |

Selection rule: **read-only or navigation only**. Every mutation flows through gate buttons,
thread replies, supervisor chat, or the dashboard — mutating slash commands are exactly the
122-command surface we are deleting. Six is the cap; additions require removing one.

### 4.6 Removed

Mirrored command handlers (`src/discord/commands.py`), the project wizard
(`project_wizard.py`), notes threads/notes views, channel summarization and the local
message-history buffer, the chat-observer wiring (paused with its subsystem), per-command
embed formatters for playbooks/workspaces/profiles, and Telegram in its entirety. Their
replacements are the dashboard (§8) and the supervisor chat.

---

## 5. Event consumption contract

`aq-discord` consumes the daemon's event stream over `GET /api/ws?after_seq=<n>` (envelope,
sequencing, and replay are owned by [[work-graph]]; event *names and payloads* below are
owned by [[session-runtime]], [[work-graph]], and [[supervisor-agent]] — this spec pins the
fields the bot needs, and the payload-registry test keeps them honest). Delivery is
at-least-once; the bot deduplicates on `seq` and persists the last-rendered seq.

| Event | Fields consumed | Rendered as |
|---|---|---|
| `task.session.started` | `task_id, project_id, session_id, title, profile, harness, work_dir, branch, attach_command` | Create/reuse thread; root message "Agent working: …" |
| `task.session.output` | `task_id, session_id, stream_id, text` (cumulative), `done`, `source` (`transcript\|peek`) | In-place edit via stream worker |
| `task.session.closed` | `task_id, session_id, reason, outcome` | Final flush; thread status line |
| `task.status_changed` | `task_id, project_id, old, new, outcome, failure_class, detail` | Thread + brief on terminal states; rename/archive (§6) |
| `task.needs_attention` | `task_id, project_id, reason, rejection_reason` | Ping in thread + channel brief |
| `gate.created` | `gate_id, task_id, project_id, kind` (`human\|ask\|pr-merged\|…`), `prompt, options` | Gate embed + buttons (human kinds only) |
| `gate.resolved` | `gate_id, task_id, resolution, resolved_by, source` | Disable buttons, post outcome (regardless of which surface resolved it) |
| `message.created` / `message.replied` | `message_id, task_id\|session_name, project_id, author, author_kind, body` | Agent/supervisor replies into thread or channel; used for reply receipts |
| `pr.created`, `merge.conflict`, `budget.warning` | ids, `pr_url`, `branch`, usage figures | Channel briefs (no buttons; PR review happens on the forge/dashboard) |

**Name mapping.** [[session-runtime]] currently names its lifecycle events
`session.started` / `session.exited` (etc.) and streams transcript output as
`notify.task_message` (the existing `stream_id` contract). The rows above use the
messaging-view names; whichever names the owning spec finalizes, the **fields** listed here
are the bot's requirement and the shared payload-registry test enforces both name and shape
— a rename on the producer side fails CI, not the bot at runtime.

Non-goals: the bot does not consume playbook events (paused), does not tail SSE session
streams (the session-output events on the WS are its feed; SSE is for dashboard/humans), and
does not receive `chat.message` back (it is the producer of chat input, §7.3).

---

## 6. Thread lifecycle

| Task/session moment | Thread action |
|---|---|
| `task.session.started`, no thread known | Post root message to project channel, create thread named `⏳ <task_id> · <title≤80>`; register `task_id ↔ thread_id` in local durable state |
| `task.session.started`, thread exists (retry/reopen) | Unarchive if needed, post "resumed" marker, reuse |
| Streaming | `task.session.output` → stream worker edits in place |
| `task.status_changed` → `COMPLETED` | Rename `✅ …`, edit root message with outcome summary, post brief reply on root; archive after `archive_after_s` (default 24 h) |
| → `FAILED` / `BLOCKED` / `needs_attention` | Rename `⚠️ …` / `🚫 …`; **never** auto-archive — these wait for a human |
| → task deleted / project deleted | Rename `🗑 …`, archive immediately |
| Bot restart | Rebuild registry from local state; verify threads exist; missing entries are recovered by scanning channel threads for the `<task_id> ·` name prefix |

The thread name carries the task id precisely so the registry is reconstructible from
Discord itself — the bot's local state is a cache, not a source of truth (principle #1
applied to presentation state).

---

## 7. Reply routing, loop prevention, permissions

### 7.1 Routing (task threads)

1. Drop: the bot's own messages, any `author.bot`, empty messages, unauthorized authors.
2. If the task has an open **human-kind gate** → the reply resolves it: for `ask` gates the
   text is the answer (`gate_resolve {gate_id, resolution:"answer", answer, actor}`);
   approval gates are **not** resolvable by plain text — they need an explicit button click
   (or `approve` / `reject` as the entire message), so a casual comment can never approve.
3. Otherwise → `message_send {task_id, body, author, source:"discord"}`. The daemon owns
   what happens next (store row; nudge live session; queue for next run — per
   [[supervisor-agent]] and [[session-runtime]]). The bot acknowledges with a 📨 reaction on
   success, ⚠️ + error reply on failure. **No client-side queueing of actions**: if the
   daemon is down, the human sees the failure immediately instead of a silent maybe-later.

### 7.2 Loop prevention

- Own-message guard (`author == bot.user`) and blanket `author.bot` guard — agent output
  rendered by the bot can never re-enter as a message.
- `message.created` events whose `source` is `discord` are not re-posted to the thread they
  came from (echo suppression by source + message id).
- Per-message dedup by Discord message id (restart-safe via the processed-id window).

### 7.3 Project channel chat

Messages in a mapped project channel from authorized users relay to the supervisor
(`session_message_send`). In shared channels an @mention is required (config
`chat.require_mention`, default true — preserves today's "collaborators can talk without
triggering the bot" rule from `on_message`); in a dedicated per-project channel every
authorized message relays. Attachments are uploaded via the daemon's file API and referenced
by path, replacing the bot-local download dir.

### 7.4 Permissions (Discord → daemon identity)

| Config | Grants | Default |
|---|---|---|
| `authorized_users` (user ids) | everything below | required, as today |
| `roles.operator` (role ids) | reply-to-agent, supervisor chat, slash commands | falls back to `authorized_users` |
| `roles.approver` (role ids) | gate resolve (buttons + text answers) | falls back to `authorized_users` |

Every command call carries `actor: "discord:<user_id>:<display_name>"`; the daemon records
it on the gate/message row (audit lives daemon-side, enforcement bot-side — acceptable
because the service token already implies full trust; role mapping is a UX guard, not a
security boundary). Unauthorized interactions are silently ignored (messages) or answered
ephemerally (buttons/slash).

---

## 8. Failure model

| Failure | Behavior |
|---|---|
| **Daemon down / unreachable** | Bot stays up. WS reconnect loop with exponential backoff + jitter (1 s → 60 s cap). Actions fail fast and visibly (§7.1). Slash commands answer with a degraded notice. Nothing is queued client-side. |
| **Bot down / crashed** | Daemon unaffected — events accumulate in the durable event log. Supervision (systemd / `run.sh`) restarts the bot; it resumes from its persisted `last_seq`. |
| **Catch-up after gap** | Replay from `after_seq` with collapse rules: `task.session.output` collapses to the latest text per stream (never replay intermediate edits); sessions still live get threads created late; tasks that went terminal while offline get a single summary post; open gates are always rendered regardless of age; other events older than `catchup.max_age_s` (default 6 h) are dropped. If the requested seq has been compacted away ([[work-graph]] retention), the bot falls back to a state resync: `list_tasks` + `gate_list` + `session_list` snapshot, then streams from the current head. |
| **Discord rate limits / bans** | Ported rate guard: warn → critical (shed non-critical edits) → halt (stop all I/O until the window clears). Stream coalescing means shedding loses only intermediate frames. |
| **Registry loss** | Thread registry rebuilt from thread-name prefixes (§6). |

Eventual consistency is explicit: Discord may lag the dashboard; it converges. The daemon
never waits on, retries for, or is aware of the bot.

---

## 9. Dashboard — primary UI

The dashboard already has tasks, playbooks, profiles, workspaces, config, and events pages
(`dashboard/src/pages/{project,system}/`), a generated TS client, and the WS event stream.
The rework adds, **in this order** (each is a `CommandHandler` command + Pydantic response
model in `src/api/models/` + generated client — never a dashboard-private endpoint):

| # | Feature | Commands | Response models |
|---|---|---|---|
| 1 | **Sessions view** — list with state, auto-refreshing peek, copyable attach command, nudge box, logs | `session_list`, `session_peek`, `session_attach_command`, `session_nudge`, `session_logs` | `SessionSummary`, `SessionListResponse`, `SessionPeekResponse`, `SessionAttachResponse`, `SessionNudgeResponse`, `SessionLogsResponse` |
| 2 | **Task explain + graph** — typed edges, gates, blockers with reasons | `task_explain`, `task_graph` | `TaskExplainResponse` (blocker list: dep/gate/cap/budget/affinity/lease…), `TaskGraphResponse` (typed nodes/edges, gate nodes, cross-project edges flagged) |
| 3 | **Gates / approvals inbox** — all pending human gates across projects, one-click resolve | `gate_list`, `gate_resolve` | `GateSummary`, `GateListResponse`, `GateResolveResponse` |
| 4 | **Supervisor chat panel** — per-project chat with the supervisor session | `session_message_send`, `session_message_history` (command wrappers over [[supervisor-agent]]'s relay: `POST /api/sessions/{name}/message`, `GET /api/sessions/{name}/messages`) | `ChatMessage`, `ChatHistoryResponse`, `MessageSendResponse` |
| 5 | **Worktrees view** — slots, branches, tasks, doctor findings, reap | `worktree_list`, `workspace_doctor`, `worktree_reap` | `WorktreeSummary`, `WorktreeListResponse`, `WorkspaceDoctorResponse` |
| 6 | **Harness editor** — vault `harnesses/*.md` round-trip editing with validation | `harness_list`, `harness_get`, `harness_update`, `harness_validate` | `HarnessSummary`, `HarnessDetail`, `HarnessValidateResponse` |
| 7 | **Doctor page** — run checks, apply fixes | `doctor_run`, `doctor_fix` | `DoctorCheck`, `DoctorReport` |
| 8 | **Costs** — token ledger × pricing, per project/task/profile over time | `costs_summary`, `costs_breakdown` | `CostsSummaryResponse`, `CostsBreakdownResponse` |

Command names above that belong to other workstreams (`session_*`, `task_explain`,
`gate_*`, `worktree_*`, `doctor_*`) are *owned* by [[session-runtime]], [[work-graph]],
[[supervisor-agent]], and [[aq-surface]] respectively; this spec claims only the dashboard
obligation: the command must exist with a registered response model before the page ships.
Live updates come from the same WS contract as §5 — one event stream serves both consumers.

---

## 10. Non-goals

- No Telegram replacement, no Slack/Matrix adapter, no generic multi-platform framework —
  `src/messaging/` remains a thin port used by the interim adapter, nothing more.
- No mutating slash-command surface, ever again.
- No Discord-side persistence beyond presentation state (registry, last seq).
- No pixel-level dashboard design in this spec — pages, commands, and models only.
- No bot-initiated writes that don't map to a named command.

---

## 11. Open questions

1. Should approval gates accept bare `approve`/`reject` text replies at all, or buttons
   only? (Current lean: allow the exact keywords, nothing fuzzier.)
2. Thread archive timing for `COMPLETED` — fixed 24 h vs. archive-on-merge once the merge
   slot ([[workspaces-v2]]) emits `task.merged`.
3. Whether `aq-discord` ships a read-only "no token" spectator mode. Deferred.
