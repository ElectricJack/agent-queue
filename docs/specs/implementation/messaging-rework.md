---
tags: [implementation, messaging, discord, dashboard, api, overhaul]
---

# Messaging Rework — Implementation Plan

**Status:** Draft — approved direction (2026-08-19)
**Design:** [[../design/messaging-rework]]
**Related:** [[../design/session-runtime]] (event producers, transcripts), [[../design/supervisor-agent]] (messages, chat relay), [[../design/work-graph]] (gates, event log, `after_seq`), [[../design/aq-surface]] (command surface), [[../messaging/base]], `dashboard/CLAUDE.md`

---

## 1. Scope and prerequisites

This plan covers: removing Telegram; stripping the in-process Discord surface to the five
kept features; building `packages/aq-discord/` as a standalone process over REST + WS;
deleting `src/discord/` after cutover; and the eight dashboard features with their commands
and response models.

Hard prerequisites from other workstreams (tracked in their specs, gated in the checklist):

| Prerequisite | Owner | Needed by |
|---|---|---|
| Durable event log with `seq` + `GET /api/ws?after_seq=` replay (today: `/ws/events` in `src/api/app.py:91`, no seq, `notify.*` only) | [[../design/work-graph]] | M2 |
| `gates` table, `gate_list` / `gate_resolve` commands, `gate.*` events | [[../design/work-graph]] | M3 |
| Session lifecycle + transcript-streaming events (`session.started/.exited`, streamed output — names per [[../design/session-runtime]], fields per design §5) | [[../design/session-runtime]] | M3 |
| `messages` table, `message_send`, `session_message_send` relay, `message.*` events | [[../design/supervisor-agent]] | M3 |
| `session_list/peek/attach_command/nudge/logs` commands | [[../design/session-runtime]] / [[../design/aq-surface]] | M3, D1 |

Until a prerequisite lands, the interim in-process bot (M0) keeps the old wiring for that
feature.

---

## 2. Module layout — `packages/aq-discord/`

Modeled on `packages/aq-client/` (own pyproject, installable independently). The daemon's
`pyproject.toml` drops `discord.py` at M4; `aq-discord` owns it from M2.

```
packages/aq-discord/
├── pyproject.toml            # deps: discord.py>=2.5.2,<2.6, httpx, websockets, pydantic, pyyaml
├── README.md
├── aq_discord/
│   ├── __init__.py
│   ├── __main__.py           # `python -m aq_discord [--config PATH] [--check]`
│   ├── config.py             # BotConfig: load/validate ~/.agent-queue/aq-discord.yaml + env
│   ├── daemon_client.py      # DaemonClient — REST via httpx, service token, retries
│   ├── event_stream.py       # EventStream — WS consumer, after_seq resume, backoff, catch-up
│   ├── state.py              # StateStore — last_seq + thread registry (JSON under data dir)
│   ├── bot.py                # DiscordBot — gateway lifecycle, channel map, on_message routing
│   ├── threads.py            # ThreadManager — lifecycle table from design §6, registry rebuild
│   ├── streaming.py          # StreamRenderer — per-stream worker, chunking (ported)
│   ├── rate_guard.py         # ported verbatim from src/discord/rate_guard.py
│   ├── render.py             # embed builders (subset of src/discord/embeds.py + notifications.py)
│   ├── gates.py              # GateView + text-answer resolution
│   ├── chat.py               # project-channel → supervisor relay
│   ├── slash.py              # the 6 read-only slash commands
│   └── permissions.py        # authorized_users / role map checks, actor string
└── tests/
    ├── conftest.py           # FakeDaemon (aiohttp/uvicorn test server: /api/execute + /api/ws)
    ├── test_event_stream.py
    ├── test_streaming.py     # chunker/coalescing goldens (ported cases)
    ├── test_threads.py
    ├── test_routing.py       # reply→gate/message rules, loop prevention, permissions
    └── test_catchup.py
```

### 2.1 Key signatures

```python
# daemon_client.py
class DaemonClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0): ...
    async def execute(self, command: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /api/execute {"command", "args"}; injects Authorization: Bearer <token>;
        returns the {"success": bool, ...} envelope; raises DaemonUnavailable on
        connect/5xx errors (callers surface these visibly, never queue)."""
    async def snapshot(self) -> DaemonSnapshot:
        """projects + channel map + open gates + live sessions; used at boot and
        on seq-gap resync (design §8)."""

# event_stream.py
class EventStream:
    def __init__(self, ws_url: str, token: str, state: StateStore,
                 dispatch: Callable[[Event], Awaitable[None]],
                 *, min_backoff: float = 1.0, max_backoff: float = 60.0): ...
    async def run(self) -> None:
        """Connect /api/ws?after_seq=<state.last_seq>; on each event: dedupe by seq,
        await dispatch, persist seq. Reconnect forever with jittered backoff.
        On SeqCompacted (server says after_seq too old): full catch-up via
        Catchup.resync() then continue from head."""

# threads.py
class ThreadManager:
    async def ensure_thread(self, task: TaskRef) -> discord.Thread | None
    async def on_terminal(self, task_id: str, status: str, outcome: str | None) -> None
    async def rebuild_registry(self) -> int   # scan threads for "<task_id> ·" prefix

# streaming.py  (port of DiscordNotificationHandler._stream_worker et al.)
class StreamRenderer:
    def submit(self, stream_id: str, thread: discord.Thread, text: str, done: bool) -> None
    async def drain(self) -> None             # shutdown flush
```

Ruff line-length 100, py312, fully async — same toolchain as the daemon.

---

## 3. Daemon-side changes

### 3.1 Service token (new)

- Config: `api.auth_tokens: list[str]` (empty = auth disabled, preserving localhost dev).
  Verified base: `MCPServerConfig`/API config in `src/config.py:637-755` (host `127.0.0.1`,
  port `8081`) — today there is **no** auth (`src/api/dependencies.py`, `middleware.py`).
- Enforcement: a `BearerTokenMiddleware` beside `RequestContextMiddleware`
  (`src/api/middleware.py`), applied to `/api/*` and the WS handshake
  (`?token=` query param fallback for WS). `/health`, `/ready` stay open.
- Token generation surfaced via `aq doctor` / setup; shared config key consumed by
  `aq-discord`, the dashboard dev proxy, and agent sessions (`AQ_API_TOKEN`,
  [[../design/aq-surface]]).

### 3.2 Messaging port and factory (interim period)

Verified integration points:

- `src/main.py:107` — `adapter = create_messaging_adapter(config, orch)`; ready-wait block
  at `main.py:128-192`; `orch.set_command_handler(adapter.get_command_handler())` and
  `orch.set_supervisor(...)` at `main.py:192-193`.
- `src/messaging/factory.py:45-58` — platform dispatch (`discord` | `telegram`).

Changes:

1. **M0:** delete the `telegram` branch (`factory.py:51-54`) and `src/telegram/`; add
   platform `"none"` → a `NullMessagingAdapter` (implements `MessagingAdapter`, all no-ops,
   `is_connected() -> True`). `main.py` gains a guard so `set_command_handler` /
   `set_supervisor` fall back to constructing them directly when the adapter doesn't provide
   them (today the Discord adapter is their *factory* — `adapter.py:108-114` — which is
   itself coupling to remove).
2. **M0:** strip the in-process bot: remove `register_commands` (all of
   `src/discord/commands.py`), `project_wizard.py`, notes threads, channel summarization
   (`bot.py:1715-1763, 2175-2400`), ad-hoc views. The interim bot keeps: channel routing,
   task threads + streaming (`notification_handler.py`), gate/approval views, project-channel
   chat, and the 6 slash commands (re-pointed to `system_status`, `list_tasks`, etc.).
3. **M4:** default `messaging_platform: "none"`; delete `src/discord/` and the `discord.py`
   dependency (`pyproject.toml:16`). `src/messaging/` (base/port/types/factory) **stays** as
   the in-process transport abstraction per the design.

### 3.3 Commands and events consumed by the bot

All owned elsewhere; this spec only asserts the bot's call list:
`system_status`, `list_tasks`, `task_explain`, `gate_list`, `gate_resolve`, `message_send`,
`session_message_send`, `session_peek`, `session_attach_command`, `set_project_channel`
(exists today), `list_projects`. Channel mapping source of truth remains
`projects.discord_channel_id` (resolved today in `bot.py:813-839`) — the bot fetches it via
`list_projects` at boot and on `project.*` events; `aq-discord.yaml` may add static
overrides but never writes the DB except through `set_project_channel`.

---

## 4. Configuration

### 4.1 Daemon `~/.agent-queue/config.yaml`

| Key | Type / default | Notes |
|---|---|---|
| `messaging_platform` | `"discord" \| "none"`; default `"discord"` until M4, then `"none"` | `"telegram"` removed at M0 |
| `api.auth_tokens` | `list[str]`, `[]` | empty disables auth (dev) |
| `discord.*` | unchanged until M4, then deleted | `bot_token`, `guild_id`, `channels`, `authorized_users`, `per_project_channels`, `rate_guard_*` (verified `src/config.py:73-104`) migrate to `aq-discord.yaml` |

### 4.2 Bot `~/.agent-queue/aq-discord.yaml` (new)

```yaml
daemon:
  url: http://127.0.0.1:8081        # AQ_API_URL
  token: "..."                      # AQ_API_TOKEN; required when daemon auth is on
  reconnect: {min_backoff_s: 1, max_backoff_s: 60}
discord:
  bot_token: "..."                  # AQ_DISCORD_BOT_TOKEN
  guild_id: "..."
  default_channel: agent-queue      # fallback when a project has no channel
  auto_create_channels: true        # port of per_project_channels behavior
  channel_overrides: {}             # project_id -> channel_id (DB wins; this is bootstrap)
permissions:
  authorized_users: []              # required, as today
  roles: {operator: [], approver: []}
chat: {require_mention: true}
threads: {archive_after_s: 86400}
catchup: {max_age_s: 21600}
rate_guard: {warn: 1000, critical: 5000, halt: 8000}
```

`aq doctor` gains a check that both files agree (token pair, daemon reachable).

---

## 5. Migration inventory (verified against the tree)

| Path (lines) | Fate |
|---|---|
| `src/telegram/` — `adapter.py` 108, `bot.py` 742, `commands.py` 170, `notifications.py` 265, `views.py` 309, `__init__.py` 30 | **Delete at M0**, plus `[telegram]` extra (`pyproject.toml:73-74`), `TelegramConfig` + platform validation (`src/config.py:106-125, 966-980`), `docs/specs/messaging/telegram.md` |
| `src/discord/commands.py` (4439) | **Delete at M0** (122 mirrored handlers; 6 survivors reimplemented in `aq_discord/slash.py`) |
| `src/discord/project_wizard.py` (828) | **Delete at M0** |
| `src/discord/views.py` (294) | Delete at M0 except `ExpiredInteractionTolerantView` → `aq_discord/gates.py` |
| `src/discord/rate_guard.py` (264) | **Move verbatim** → `aq_discord/rate_guard.py` at M2 |
| `src/discord/notification_handler.py` (948) | Stream worker + thread-callback logic → `aq_discord/streaming.py` / `threads.py` at M2; event subscription replaced by `EventStream`; playbook handlers dropped (paused) |
| `src/discord/bot.py` (2400) | Channel routing, `_create_task_thread` (1295), `_handle_task_thread_message` (1492), `on_message` routing (1764) → ported into `aq_discord/bot.py`/`threads.py`/`chat.py`; notes/summarization/history deleted; **file deleted at M4** |
| `src/discord/embeds.py` (749) / `notifications.py` (2068) | Formatters for kept features → `aq_discord/render.py`; the rest deleted at M4 |
| `src/discord/adapter.py` (126) | Deleted at M4 (interim it remains the in-process `MessagingAdapter`) |
| `src/messaging/` (base 131, port 249, factory 58, types 48) | **Stays**; factory loses telegram at M0, gains `none`; `MessagingPort.set_supervisor` removed with the Supervisor unwiring ([[../design/supervisor-agent]]) |
| `src/notifications/events.py` (489) | Task/gate/session event models evolve under [[../design/session-runtime]] / [[../design/work-graph]]; playbook events pause; `TaskThreadOpen/Close` and `TaskMessageEvent.stream_id` plumbing retire when the interim bot does |

**Interim compatibility period (M0 → M4):** the stripped in-process bot and `aq-discord`
are mutually exclusive — the daemon refuses to start the in-process adapter when
`messaging_platform: "none"`, and dual-run for validation (M3) uses a **separate guild or
channel set**, never the same channels, to avoid duplicate posting.

---

## 6. Phase checklist

**M0 — Strip (with overhaul Phase 0)**
- [ ] Delete `src/telegram/`, `[telegram]` extra, `TelegramConfig`, telegram factory branch, telegram spec; migration note for `messaging_platform: telegram` users (hard error with pointer).
- [ ] Delete `src/discord/commands.py`, `project_wizard.py`, notes threads + summarization + history buffer from `bot.py`; register the 6 survivor slash commands in-process.
- [ ] Add `NullMessagingAdapter` + `messaging_platform: "none"`; decouple `CommandHandler`/Supervisor construction from the adapter in `src/main.py`.
- [ ] Tests: daemon boots and schedules with `messaging_platform: "none"`; factory rejects `"telegram"` with a clear error.

**M1 — Daemon prerequisites**
- [ ] `api.auth_tokens` + bearer middleware (REST + WS handshake); `aq doctor` token check.
- [ ] Event log + `/api/ws?after_seq=` replay landed ([[../design/work-graph]]); payload-registry test covers every event in design §5.
- [ ] `gate_*`, `message_send`, `session_message_send`, `session_*` commands landed (owning workstreams) with response models registered.

**M2 — Scaffold `packages/aq-discord/`**
- [ ] Package skeleton, `pyproject.toml`, CI job (ruff + pytest).
- [ ] Port `rate_guard.py` (verbatim) and stream worker with its golden tests.
- [ ] `DaemonClient`, `EventStream` (+ `StateStore`), `FakeDaemon` test fixture; reconnect/backoff and seq-dedup tests.

**M3 — Feature parity out of process**
- [ ] `ThreadManager` lifecycle (design §6) incl. registry rebuild; `StreamRenderer` wired to `task.session.output`.
- [ ] Reply routing (gate answer / `message_send`), loop prevention, permissions; gate buttons; project-channel chat relay; 6 slash commands via `DaemonClient`.
- [ ] Catch-up policy (collapse, terminal summaries, open-gates-always, `max_age_s`, snapshot resync).
- [ ] Dual-run against a test guild alongside the in-process bot (separate channels); soak ≥ 1 week; compare rendered output.

**M4 — Cutover and deletion**
- [ ] Default `messaging_platform: "none"`; delete `src/discord/`, drop `discord.py` from daemon deps; update `docs/specs/messaging/*`; `run.sh` gains an `aq-discord` unit (auto-restart, backoff).
- [ ] Config migration: move `discord.*` keys into `aq-discord.yaml` (documented, `aq doctor --fix` assist).

**M5 — Dashboard (order fixed)**
- [ ] D1 Sessions view · D2 Task explain + graph · D3 Gates inbox · D4 Supervisor chat · D5 Worktrees · D6 Harness editor · D7 Doctor · D8 Costs — each: command(s) + response model in `src/api/models/<category>.py` (registered in `RESPONSE_MODELS`), `npm run generate:ts-client`, hooks in `dashboard/src/api/hooks.ts`, page under `dashboard/src/pages/{project,system}/`, WS-driven invalidation.

---

## 7. Dashboard feature details (API-first)

Pages live beside the existing ones (`dashboard/src/pages/project/`: Overview, Tasks,
Workspaces, Profiles, Playbooks, Config; `system/`: Overview, Events, Profiles, Playbooks,
Config — verified listing). New response-model modules: `src/api/models/session.py`,
`gate.py`, `message.py`, `worktree.py`, `harness.py`, `doctor.py`, `costs.py`; `task.py`
gains explain/graph models. Commands surface automatically through the codegen routers
(`src/api/codegen.py` → `/api/<category>/…`) once registered in the tool registry.

| Page (route) | Commands | Model highlights |
|---|---|---|
| `project/Sessions.tsx`, `system/Sessions.tsx` | `session_list`, `session_peek`, `session_attach_command`, `session_nudge`, `session_logs` | `SessionSummary{id, task_id, profile, harness, state, last_activity, restarts}`; peek returns `{lines: list[str], captured_at}`; nudge returns `{submitted: bool}` |
| `TaskDetail.tsx` tabs + `project/TaskGraph.tsx` | `task_explain`, `task_graph` | `ExplainBlocker{kind: dep\|gate\|cap\|budget\|affinity\|lease\|cooldown, detail, ref_id}`; graph nodes carry `status/labels/gates`, edges carry `dep_type`, `cross_project: bool` |
| `system/Gates.tsx` (+ project filter) | `gate_list`, `gate_resolve` | `GateSummary{id, task_id, project_id, kind, prompt, options, created_at, age_s}` |
| `project/Chat.tsx` | `session_message_send`, `session_message_history` | `ChatMessage{id, author, author_kind, body, ts, reply_to}` |
| `project/Worktrees.tsx` | `worktree_list`, `workspace_doctor`, `worktree_reap` | `WorktreeSummary{slot, path, branch, task_id, dirty, last_used}` |
| `system/Harnesses.tsx` | `harness_list/get/update/validate` | round-trips vault markdown; validate returns parse errors with line numbers |
| `system/Doctor.tsx` | `doctor_run`, `doctor_fix` | `DoctorCheck{id, severity, message, fixable}` |
| `system/Costs.tsx` | `costs_summary`, `costs_breakdown` | totals + `by: project\|task\|profile\|day` series |

---

## 8. Process supervision and observability

`aq-discord` is a plain long-running process; the daemon does not manage it.

- **Startup order is irrelevant.** The bot retries the daemon (REST snapshot + WS) with the
  same backoff it uses at runtime, and Discord login independently; either side may come up
  first. `--check` performs one pass of both probes and exits non-zero on failure — usable
  as a systemd `ExecStartPre` or CI smoke test.
- **Supervision.** `run.sh` gains `start|stop|status` for the bot alongside the daemon
  (separate pidfile/log); the reference systemd unit sets `Restart=on-failure` with
  `RestartSec` backoff. A crash loop is contained to the bot by construction.
- **Health signals.** The bot sets its Discord presence to reflect daemon connectivity
  (`online` = WS connected, `idle` = reconnecting) so operators see degradation in Discord
  itself. `aq doctor` gains a `discord-bridge` check: config-pair consistency, token file
  perms, last WS heartbeat age read from the bot's `StateStore`, and last_seq lag versus the
  daemon's head seq.
- **Logging/metrics.** Structured logs (same `structlog` conventions as the daemon,
  `component="aq-discord"`); counters for events consumed, renders shed by the rate guard,
  action failures, and reconnects — logged periodically rather than requiring a metrics
  stack.

---

## 9. Test plan

| Layer | Tests |
|---|---|
| Unit (`packages/aq-discord/tests/`) | Chunker/coalescing goldens (port existing stream cases: overflow chaining, latest-wins under slow I/O, done-flush); seq dedup + persistence; catch-up collapse matrix (live session / terminal task / open gate / stale event); reply routing incl. loop-prevention (own message, other bot, `source: discord` echo) and permission matrix; thread-name registry rebuild; rate-guard threshold transitions (existing tests move with the file). |
| Contract | `FakeDaemon` asserts every REST call is a registered command with schema-valid args; daemon-side payload-registry test (work-graph) pins the design §5 event fields — a producer rename fails CI, not the bot at runtime. |
| Integration | Bot against `FakeDaemon` end-to-end: event in → Discord API calls out (discord.py HTTP layer faked); WS drop/reconnect mid-stream resumes without duplicate posts; daemon-down action failure is surfaced (⚠️ path). |
| Daemon | Boot with `messaging_platform: "none"`; auth middleware (401 without token, WS handshake rejection); `set_project_channel` round-trip. |
| Dashboard | Existing pattern: typecheck + hook-level tests; each new command's response model registered (guard test: command in registry ⇒ model in `RESPONSE_MODELS`). |
| Manual / soak | M3 dual-run checklist: thread create/rename/archive, stream under rate limit, gate button + text answer, chat relay, all 6 slash commands, bot restart mid-stream, daemon restart with bot up. |

---

## 10. Rollout flags and risks

Flags: `messaging_platform` (`"discord"` in-process interim → `"none"`), `api.auth_tokens`
(auth off until set), bot-side `--check` (validate config + connectivity and exit).
Rollback at any point before M4 = stop `aq-discord`, set `messaging_platform: "discord"`.

| Risk | Mitigation |
|---|---|
| Duplicate posting during dual-run | Separate guild/channels enforced by config check; daemon refuses in-process adapter when platform is `"none"` |
| Catch-up storm after long bot outage re-triggers rate limits | Collapse rules + `max_age_s` + rate guard sheds non-critical renders; snapshot resync instead of replay when gap is large |
| Event contract drift between daemon and bot | Payload-registry contract test in daemon CI; bot pins minimum daemon version via `/api/health` schema field |
| Service token leakage (full-trust token in bot config) | File perms check in `aq doctor`; env-var override (`AQ_API_TOKEN`); scoped tokens are follow-up work with [[../design/aq-surface]] |
| Thread registry divergence (manual thread deletion/rename) | Name-prefix rebuild + orphan sweep on boot; unknown threads ignored |
| Losing "reopen on thread reply" ergonomics (old terminal-status path) | Covered by gates/messages: terminal-task replies produce a `message_send` the supervisor can act on; explicit reopen remains dashboard/`aq` |
| WS backpressure on slow bot | Bot-side coalescing; server-side per-client queue already drops oldest (`src/api/websocket.py:56-66` pattern carried into the work-graph WS) |
