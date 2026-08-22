---
tags: [implementation, supervisor, messages, sessions, task-graph, alembic]
---

# Supervisor Agent — Implementation

**Status:** Draft — approved direction (2026-08-19)
**Related:** [[design/supervisor-agent]] (design), [[design/session-runtime]] (named sessions, activity signal, nudge), [[design/work-graph]] (ids, gates, dep types), [[design/aq-surface]] (`aq prime`), `docs/analysis/framework-overhaul-todo.md` §4/§3b

---

## 1. Scope and prerequisites

Implements the design spec: the `messages` table and delivery engine, the chat relay API
and `aq chat`, `aq task create --graph|--from-spec`, new profile fields, shipped
`supervisor`/`planner`/`reviewer` profiles, and the unwiring of `Supervisor.chat()` and
plan discovery.

Dependency ordering: Phases 0–1 (schema, commands, API, CLI, graph creation) have **no
dependency on the session runtime** and land first. Phase 2 (delivery engine wake/nudge/
inject) consumes session-runtime interfaces (`SessionManager.activity()`, `nudge()`,
`ensure_started()`); until those exist the engine runs in queue-and-prime degraded mode.
Phase 4 graph ids assume the work-graph spec's hierarchical ids; if it lands later,
`--graph` initially assigns flat ids behind the same interface (`assign_child_ids()`),
swapped without format changes.

---

## 2. Module layout

| Path | Contents |
|---|---|
| `src/messages/__init__.py` | Public exports |
| `src/messages/models.py` | `Message` dataclass, `FromKind`/`ToKind` enums, envelope rendering |
| `src/messages/delivery.py` | `MessageDeliveryEngine` (cascade step + `message.sent` subscriber) |
| `src/messages/routing.py` | Target resolution (`to_kind/to_id` → session name), supervisor session naming (`supervisor-<project_id>`) |
| `src/database/queries/message_queries.py` | `MessageQueriesMixin` (pattern of the other `queries/*.py` mixins) |
| `src/commands/message_commands.py` | `MessageCommandsMixin` — `_cmd_message_send/reply/inbox/list` |
| `src/task_graph/__init__.py` | Public exports |
| `src/task_graph/models.py` | `TaskGraph`, `GraphNode`, `GraphNeed`, `GraphError` |
| `src/task_graph/parser.py` | `parse_graph()`, `extract_graph_from_spec()` (fenced `aq-graph` block, YAML/JSON) |
| `src/task_graph/validator.py` | `validate_graph()` — vars, cycles, profiles, dep types, spec_ref paths |
| `src/task_graph/creator.py` | `create_graph()` — single-transaction insert via CommandHandler/db |
| `src/api/messages.py` | `POST /api/sessions/{name}/message`, `GET /api/sessions/{name}/messages` |
| `src/cli/messages.py` | `aq message send|inbox|reply|list`, `aq reply` alias, `aq chat` REPL |
| `src/profiles/defaults/{supervisor,planner,reviewer}/profile.md` | Shipped profiles, seeded to `vault/agent-types/` on startup (same write-if-absent path as `src/profiles/migration.py::migrate_db_profiles_to_vault`) |

Changed files: `src/database/tables.py`, `src/models.py`, `src/profiles/parser.py`,
`src/profiles/sync.py`, `src/commands/handler.py` (mixin list, `handler.py:56–69/108+`),
`src/commands/task_commands.py` (`_cmd_create_task_graph`), `src/cli/tasks.py`
(`--graph/--from-spec/--dry-run`), `src/api/app.py`, `src/event_schemas.py`,
`src/config.py`, `src/main.py`, `src/discord/bot.py`, `src/orchestrator/execution.py`.

---

## 3. Schema and migrations

### 3.1 `messages` table (`src/database/tables.py`)

House conventions observed in `tables.py`: `Text` ids, `Float` epoch timestamps
(`Column("created_at", Float, ...)` throughout), integer 0/1 booleans with string
`server_default` (cf. `tasks.requires_approval`).

```python
messages = Table(
    "messages",
    metadata,
    Column("id", Text, primary_key=True),                      # "msg-<uuid7>"
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("from_kind", Text, nullable=False),                 # session|user|system
    Column("from_id", Text, nullable=False),
    Column("to_kind", Text, nullable=False),                   # session|task|profile|user
    Column("to_id", Text, nullable=False),
    Column("thread_id", Text, nullable=True),
    Column("subject", Text, nullable=True),
    Column("body", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default="100"),
    Column("created_at", Float, nullable=False),
    Column("delivered_at", Float, nullable=True),
    Column("read_at", Float, nullable=True),
    Column("archive_after_inject", Integer, nullable=False, server_default="0"),
    Column("archived_at", Float, nullable=True),
    Column("reply_to_id", Text, ForeignKey("messages.id"), nullable=True),
    Column("via", Text, nullable=True),                        # null|"transcript_tail"
    CheckConstraint("from_kind IN ('session','user','system')",
                    name="ck_messages_from_kind"),
    CheckConstraint("to_kind IN ('session','task','profile','user')",
                    name="ck_messages_to_kind"),
    Index("idx_messages_pending", "to_kind", "to_id", "delivered_at"),
    Index("idx_messages_project_created", "project_id", "created_at"),
    Index("idx_messages_thread", "thread_id"),
)
```

`archived_at`, `reply_to_id`, `via` are implementation additions to the design's column
list — needed for `archive_after_inject` semantics, reply linking, and the transcript-tail
marker.

### 3.2 `agent_profiles` columns

New nullable columns (values validated at parse time, §7): `harness Text`,
`lifecycle Text server_default "'task'" NOT NULL`, `mode Text`, `wake_mode Text`,
`idle_timeout Integer`, `max_session_age Integer`. The harness *schema* (what `claude`
means) is owned by session-runtime; these are pass-through storage.

### 3.3 Alembic notes — SQLite and PostgreSQL

```bash
alembic revision --autogenerate -m "add messages table and named-session profile fields"
```

Review checklist for the generated file:

- **Name every constraint** (done above via `name=`): unnamed check constraints cannot be
  dropped on SQLite in later batch migrations and autogenerate emits them inconsistently.
- **SQLite**: `messages` is a new table — plain `op.create_table`, no batch mode needed.
  The `agent_profiles` additions are plain `op.add_column`; adding `lifecycle` as
  `NOT NULL` requires `server_default="'task'"` in the same statement (SQLite cannot
  backfill separately without table rebuild). Keep it in the `Column` definition so
  autogenerate carries it.
- **PostgreSQL**: string `server_default="100"` / `"0"` work for `Integer` on both
  backends (existing pattern). Do **not** use `sa.text("now()")`-style defaults —
  timestamps are `Float` epoch set by application code, matching every existing table.
- **Self-referential FK** `reply_to_id → messages.id`: fine on both; ensure
  `use_alter` is *not* needed (single table, nullable).
- Downgrade must drop the table and the six columns; on SQLite the column drops require
  `batch_alter_table` — write it by hand, autogenerate gets this wrong.
- Verify with `pytest tests/test_database.py -v` plus the PG matrix job.

---

## 4. Models and queries

`src/models.py` gains:

```python
@dataclass
class Message:
    id: str; project_id: str
    from_kind: str; from_id: str; to_kind: str; to_id: str
    body: str; subject: str | None = None; thread_id: str | None = None
    priority: int = 100
    created_at: float = 0.0
    delivered_at: float | None = None; read_at: float | None = None
    archive_after_inject: bool = False; archived_at: float | None = None
    reply_to_id: str | None = None; via: str | None = None
```

`src/database/queries/message_queries.py` (async, mixed into `Database` like
`task_queries.py`):

```python
class MessageQueriesMixin:
    async def create_message(self, *, project_id, from_kind, from_id, to_kind, to_id,
                             body, subject=None, thread_id=None, priority=100,
                             archive_after_inject=False, reply_to_id=None) -> Message
    async def get_message(self, message_id: str) -> Message | None
    async def get_pending_messages(self, to_kind: str, to_id: str, *, limit=50) -> list[Message]
        # WHERE delivered_at IS NULL AND archived_at IS NULL ORDER BY priority, created_at
    async def mark_delivered(self, message_id: str, *, via: str | None = None) -> bool
        # UPDATE ... WHERE delivered_at IS NULL — compare-and-set; False if already delivered
    async def mark_read(self, message_id: str) -> bool
    async def archive_messages(self, message_ids: list[str]) -> int
    async def list_messages(self, *, project_id=None, thread_id=None, to_kind=None,
                            to_id=None, include_archived=False, since: float | None = None,
                            limit=100) -> list[Message]
```

`mark_delivered` as compare-and-set is the idempotency guard against double delivery when
a nudge and a prompt-boundary inject race.

---

## 5. Delivery engine

`src/messages/delivery.py`:

```python
class MessageDeliveryEngine:
    def __init__(self, db, bus, sessions: "SessionManagerProto", config: MessagesConfig): ...
    def start(self) -> None            # subscribe to message.sent
    async def run_delivery_pass(self) -> int   # cascade step, each 5s cycle; returns delivered count
    async def _deliver(self, msg: Message) -> bool
    async def _resolve_target(self, msg: Message) -> "SessionRef | None"   # routing.py
    async def check_reply_timeouts(self) -> int  # transcript-tail fallback sweep
```

`SessionManagerProto` is the narrow interface this spec consumes from session-runtime:

```python
class SessionManagerProto(Protocol):
    async def activity(self, name: str) -> Literal["idle", "busy", "sleeping", "absent"]
    async def ensure_started(self, name: str) -> None      # wake on_demand (resume)
    async def nudge(self, name: str, text: str) -> None    # raises NotSubmitted
    async def tail_assistant_turn(self, name: str, since: float) -> str | None
```

`_deliver` policy (design §6.1): `sleeping` → `ensure_started` then nudge; `idle` →
nudge with envelope + `aq reply` instruction, `mark_delivered`; `busy` → skip (the
`UserPromptSubmit` hook `aq inbox --inject` calls `_cmd_message_inbox(inject=True)` which
marks delivered and archives `archive_after_inject` rows); `absent` for `to_kind=task` →
leave pending (rides into `aq prime` at next session start); `to_kind=user` → emit
`message.sent` only, adapters render it. `NotSubmitted` → row stays pending, retry next
pass with backoff (`delivery_retry_backoff`).

Event registration in `src/event_schemas.py` (extend `_CHAT_SCHEMAS`, `event_schemas.py:340`):

```python
"message.sent":      {"required": ["message_id", "project_id", "from_kind", "from_id",
                                   "to_kind", "to_id"], "optional": ["thread_id", "subject"]},
"message.delivered": {"required": ["message_id", "project_id", "method"], "optional": []},
     # method: nudge|inject|prime
"message.replied":   {"required": ["message_id", "reply_id", "project_id", "body"],
                      "optional": ["via", "thread_id"]},
```

---

## 6. Commands, API, CLI

### 6.1 CommandHandler (`src/commands/message_commands.py`)

Mixin added to `CommandHandler` bases (`src/commands/handler.py:108`). All return
`{"success": bool, ...}`:

```python
async def _cmd_message_send(self, args) -> dict    # project_id, to_kind, to_id, body, ...
async def _cmd_message_reply(self, args) -> dict   # message_id, body → reply row + message.replied
async def _cmd_message_inbox(self, args) -> dict   # to_kind, to_id, inject: bool
async def _cmd_message_list(self, args) -> dict    # filters per list_messages
```

`src/commands/task_commands.py` gains:

```python
async def _cmd_create_task_graph(self, args) -> dict
    # args: {project_id, graph: dict | None, spec_path: str | None, dry_run: bool}
    # → {"success": True, "parent_id": ..., "task_ids": [...], "warnings": [...]}
    #   or {"success": False, "errors": [{"rule": ..., "node": ..., "detail": ...}]}
```

These auto-expose through the existing MCP registration and
`POST /api/{category}/{command}` codegen (`src/api/routers/__init__.py::register_all_routers`,
mounted at `src/api/app.py:83–85`) — no bespoke plumbing. `message_send`,
`message_reply`, `message_inbox` join the slim task-scoped MCP allowlist (G.2).

### 6.2 Relay API (`src/api/messages.py`)

Explicit router (path parameters don't fit the codegen pattern), registered in
`create_app()` alongside `execute_router`/`health_router` (`src/api/app.py:79–80`):

```python
@router.post("/api/sessions/{name}/message")        # → _cmd_message_send, to_kind=session
@router.get("/api/sessions/{name}/messages")        # ?thread_id=&since= → list_messages
```

`name` of form `supervisor-<project_id>` resolves the project (404 if unknown). Response:
`{"success": true, "message_id": str, "state": "queued"|"delivered"}`. `message.*` events
already reach clients through the existing `/ws/events` WebSocket
(`src/api/app.py:88–93`) once added to the notify forwarding set.

### 6.3 CLI (`src/cli/messages.py`, `src/cli/tasks.py`)

`aq reply <msg-id> "…"` (top-level alias for `aq message reply`, agent-facing);
`aq message send|inbox|list`; `aq chat <project> [--once TEXT]` — REPL per design §7,
Click command using the REST client (`src/cli/client.py`) plus a `/ws/events`
subscription; falls back to polling `GET .../messages?since=` when WS is unavailable.
`aq task create` gains `--graph <file|->`, `--from-spec <path>`, `--dry-run`.

---

## 7. Profile parser and sync (`src/profiles/parser.py`)

At `parser.py:47–49` extend:

```python
CONFIG_KNOWN_KEYS |= {"harness", "lifecycle", "mode", "wake_mode",
                      "idle_timeout", "max_session_age", "workspaces"}
VALID_LIFECYCLES = frozenset({"task", "named"})
VALID_MODES = frozenset({"always", "on_demand"})       # named lifecycle only
VALID_WAKE_MODES = frozenset({"resume", "fresh"})
```

Validation: `mode`/`wake_mode`/`idle_timeout` only valid with `lifecycle: named` (parse
error otherwise); `harness` is any string at parse time — existence is checked at sync
against `vault/harnesses/` (session-runtime's registry) with a warning, not an error, so
profiles can land before harnesses. `src/profiles/sync.py` maps the new keys to the new
`agent_profiles` columns. Shipped defaults seed via the existing write-if-absent vault
path; project overrides shadow by id as today.

---

## 8. Task graph module (`src/task_graph/`)

```python
def parse_graph(source: str | dict, *, fmt="auto") -> TaskGraph        # raises GraphParseError
def extract_graph_from_spec(markdown: str, spec_path: str) -> TaskGraph
async def validate_graph(graph: TaskGraph, *, project_id: str, db) -> list[GraphError]
async def create_graph(handler, graph: TaskGraph, *, project_id: str,
                       dry_run: bool = False) -> dict
```

`validate_graph` implements the design §8.3 table: var substitution then unknown-`{var}`
scan; duplicate keys; `needs.on` resolution (graph key, else `db.get_task`, else error;
foreign-project task requires `cross_project: true`); Kahn's algorithm over blocking
`dep_type`s for cycles; profile existence via `db.get_profile(project override → system)`;
dep_type against the work-graph registry; spec_ref path + section-heading existence in the
vault. `create_graph` runs one transaction: parent task, nodes with hierarchical child ids
(work-graph's `assign_child_ids()`), `task_dependencies` rows with `dep_type`,
`task_context` rows (`type='spec_ref'` content = JSON `{path, section}`; `type='file'`
as today's attachments). Rollback on any failure — no partial graphs.

---

## 9. Integration points (verified against source)

**Status:** Phase 4 cutover is **complete** as of 2026-08-21. `supervisor_agent.legacy_chat`
has been deleted; Discord chat routes exclusively to supervisor sessions via `message_send`.

| Point | File:line | Status |
|---|---|---|
| Shared Supervisor construction + `default_registry(supervisor=...)` + `initialize()` | `src/main.py` | `initialize()` is always called; hardened to return False (not raise) on provider errors. `supervisor_agent.legacy_chat` gate removed. |
| `orch.set_supervisor(adapter.get_supervisor())` | `src/main.py` | Removed at cutover — no longer wired |
| Discord chat → `message_send` to supervisor session | `src/discord/bot.py` (project routing via `_channel_to_project`) | **Done** — `on_message` enqueues `_cmd_message_send(to_kind="session", to_id=f"supervisor-{pid}")`; ThinkingView deleted; `message.delivered`/`message.replied` renderers in the notification handler |
| Telegram `self._supervisor.chat(history)` | `src/telegram/bot.py:327` | Removed with `src/telegram/` (Workstream F) — no migration needed |
| Plugin `invoke_llm` fallback → `supervisor.chat` | `src/orchestrator/core.py:412` | Still routes through `supervisor.chat` — unchanged in this phase |
| Plan-approval region: `AWAITING_PLAN_APPROVAL` transition, `break_plan_into_tasks` call, auto-approve | `src/orchestrator/execution.py:977–1160` (call at `:1038`) | Behind `planner.legacy_plan_discovery` (default true until Phase 5, then false) |
| Pipeline phase `_phase_plan_discover` / `_phase_plan_generate` | `src/orchestrator/approval.py:224`, `:282`; invoked from `src/orchestrator/git_ops.py:487–508`; scanner `_discover_and_store_plan` at `git_ops.py:336` | Same flag: phase returns `CONTINUE` immediately when disabled |
| `Supervisor.chat()` / `break_plan_into_tasks()` / `on_task_completed()` | `src/runtimes/supervisor.py:755`, `:1484`, `:1678` | Frozen dormant (decided) — no edits beyond the callers above |
| Cascade | orchestrator cycle (`run_one_cycle`) | `MessageDeliveryEngine.run_delivery_pass()` + `check_reply_timeouts()` run each cycle, after approvals/promotions |
| Event registry | `src/event_schemas.py` `_CHAT_SCHEMAS` | `message.*` schemas added |

---

## 10. Rollout flags (`src/config.py`)

**Note:** `supervisor_agent.legacy_chat` was removed at Phase 4 cutover (2026-08-21).
The `chat_provider` config key remains as the utility-LLM config used by playbook
compilation, vault summaries, and `runtime: supervisor` tasks — it is no longer the
Discord chat model.

```python
@dataclass
class MessagesConfig:
    enabled: bool = False              # table+commands usable; delivery engine runs
    delivery_interval: float = 5.0     # piggybacks the cascade cycle
    reply_timeout: float = 120.0       # transcript-tail fallback trigger
    transcript_tail_fallback: bool = True
    max_inject_per_prompt: int = 10

@dataclass
class SupervisorAgentConfig:
    enabled: bool = False              # route project chat to supervisor sessions
    idle_timeout: int = 900            # default for the shipped profile
    # legacy_chat removed 2026-08-21: Discord chat routes only via message_send

@dataclass
class PlannerConfig:
    legacy_plan_discovery: bool = True # plan.md pipeline; flips false at Phase 5
```

Mounted on `AppConfig` (`config.py:804`) as `messages`, `supervisor_agent`, `planner`.
Editable via the existing `config_editor` round-trip. Valid states: `messages.enabled`
alone = queue/inbox/prime mode (no sessions needed); `supervisor_agent.enabled` requires
`messages.enabled` and session-runtime named sessions (validated in `AppConfig.validate()`).
Old YAML carrying `legacy_chat` is silently ignored by the config loader.

---

## 11. Phase checklist

- [ ] **Phase 0 — schema & parsing** (no runtime deps): `messages` table +
      `agent_profiles` columns in `tables.py`; Alembic revision, hand-check SQLite
      downgrade batch ops; `Message` model; `message_queries.py`; parser/sync fields;
      `event_schemas.py` additions; config dataclasses. Tests green on SQLite + PG.
- [ ] **Phase 1 — commands, API, CLI**: `MessageCommandsMixin` wired into
      `CommandHandler`; `src/api/messages.py` router in `create_app()`; MCP/CLI
      auto-exposure verified; `aq message *`, `aq reply`, `aq chat --once` (poll mode);
      ~~slim-MCP allowlist entries~~ — **not part of Phase 1.**
      `mcp_server.task_scope` is an aq-surface Phase-3 placeholder with no consumer
      (see [[implementation/aq-surface]] §S3); there is nothing to allowlist until
      `register_task_scope_tools` exists.
- [ ] **Phase 2 — graphs**: `src/task_graph/` parser/validator/creator with goldens;
      `_cmd_create_task_graph`; `aq task create --graph|--from-spec|--dry-run`;
      `spec_ref` context rows (prime rendering lands with aq-surface); vault
      `specs/` directory convention documented in [[design/vault]].
- [x] **Phase 3 — delivery engine** (needs session-runtime named sessions + activity):
      `MessageDeliveryEngine` cascade step; wake-on-message; nudge envelope +
      `NotSubmitted` retry; `aq inbox --inject` hook path; prime injection +
      `archive_after_inject`; transcript-tail fallback; `aq chat` live via `/ws/events`.
- [x] **Phase 4 — routing cutover** (complete 2026-08-21): `supervisor_agent.legacy_chat`
      flag deleted; Discord `on_message` routes all project-channel chat exclusively to
      supervisor sessions via `message_send` (no in-process `Supervisor.chat()` path for
      Discord); `ThinkingView` deleted; `main.py` supervisor init hardened (catches provider
      errors, returns False). `supervisor_agent.enabled` defaults `False` — enable via ops
      after live end-to-end test. Dashboard chat page (F.2) deferred to post-live-test.
- [x] **Phase 5 — planner cutover** (partial): `legacy_plan_discovery` flag gates the
      `AWAITING_PLAN_APPROVAL` region and `_phase_plan_discover`/`_phase_plan_generate`;
      drain via task-state-aware `_should_run_legacy_plan_region`; skip-and-log when
      disabled (no concrete replacement path yet). **Default flip deferred**:
      `legacy_plan_discovery` remains `True`; drain of in-flight `AWAITING_PLAN_APPROVAL`
      tasks and marking `plan-parser.md` superseded are post-live-test ops steps.

### 11.1 Open questions the delivery engine must settle (Phase 3)

Recorded during the adversarial review of the Phase 0–2 merge. None is a bug
*today* — Phase 1 only queues rows, nothing reads them — but each becomes one
the moment `MessageDeliveryEngine` starts consuming the queue. Decide them
before writing the engine, not after.

**Message scoping — the big one.** `messages.project_id` is written on every row
and read by almost nothing:

- `get_pending_messages` has **no project filter at all**, and
  `idx_messages_pending` (`to_kind, to_id, delivered_at, priority, created_at`)
  bakes that in — adding a filter later means a new index, not just a `WHERE`.
  `to_kind=profile` and `to_kind=user` recipients are inherently project-agnostic
  ids, so two projects' messages to the same `to_id` land in one inbox. Verified.
- `_cmd_message_list` with no `project_id` argument and no active project returns
  **every project's** messages.
- `message.*` events fan out to **all** `/ws/events` clients regardless of project.
  Chat bodies are more sensitive than the task notifications that channel was
  designed for; scope the fan-out, or route chat over a separate channel.

Pick one model deliberately — recipient ids are globally unique, or delivery is
always project-scoped — and make queries, index, and event fan-out agree.

**Resolution (Phase 3):** `get_pending_messages` stays project-unscoped — recipient
ids are globally unique so cross-project collisions cannot occur. The delivery engine
derives `project_id` per row from the message record itself for any calls that require
it (e.g. `SessionLens` lookups). No index change needed.

**`mark_delivered` and archived rows.** The compare-and-set is genuinely correct
(three concurrent callers → `[True, False, False]`), but it only checks
`delivered_at IS NULL`, so it succeeds on an already-**archived** row. Decide
whether archiving should close the row to delivery.

**Resolution (Phase 3):** Archiving closes the row to delivery. `mark_delivered`
now includes `archived_at IS NULL` in its CAS predicate — an archived row returns
`False` (already closed) rather than marking it delivered.

**`task_criteria` is write-only.** `src/task_graph/creator.py` is the only writer
and there is no getter, so `_compose_description`'s double-write of acceptance
criteria into the task description is invisible. The moment anyone adds a reader,
prime section 3 renders the criteria twice. See the comment on `_compose_description`.

**Transaction boundary.** `creator.py` reaches through to `db._engine` to get one
transaction across five tables. The atomicity is *verified correct* (injected
failure at insert 4 and during `task_criteria` both left zero rows) — but the
right shape is a `db.transaction()` context manager on the query layer, so the
next multi-table writer doesn't copy the reach-through as folk wisdom.

**Resolution (Phase 3):** The delivery engine (`MessageDeliveryEngine`) uses only
public `MessageQueriesMixin` methods — no `db._engine` access, no new transaction
contexts. The `creator.py` reach-through is noted as tech debt but was not replicated.

**Cosmetic, no action required now.**

- `messages are disabled` surfaces from the relay as HTTP 422; 503 is more honest.
- `extract_graph_block` matches a nested ` ```aq-graph ` fence *inside* an outer
  fence, so a spec that merely **documents** a graph gets it extracted; and an
  unclosed fence silently swallows the rest of the file.

---

## 12. Test plan

| Layer | Tests |
|---|---|
| Migrations | upgrade/downgrade on SQLite and PostgreSQL (CI matrix); `lifecycle` default backfill; constraint names present |
| Queries | create/pending ordering (priority then created_at); `mark_delivered` compare-and-set returns False on second call; archive semantics; `since` filtering |
| Graph parser/validator | golden files: valid JSON, valid YAML-in-spec, each §8.3 error (unknown var, dup key, cycle, unknown profile, bad dep_type, missing spec section, cross-project without flag); `--dry-run` output stability |
| Graph creator | single transaction: injected failure on node 3 leaves zero rows; dep rows carry `dep_type`; `spec_ref` context content shape |
| Delivery engine | on `fake` session provider: sleeping→wake→nudge; idle→nudge; busy→skip then inject; `NotSubmitted`→retry; double-delivery race (nudge vs inject) delivers once; reply-timeout → transcript-tail marked `via=transcript_tail`; `archive_after_inject` archived exactly once |
| Commands/API | `_cmd_message_*` success/error envelopes; relay 404 on unknown session name; event payloads validate against the registry (existing invariant test) |
| CLI | `aq chat --once` against a stubbed API; `aq task create --from-spec` happy path + validation-error exit code |
| Routing cutover | Discord `on_message` with flag on creates a row and does not call `agent.chat` (mock); with flag off behavior unchanged |
| E2E (fake provider) | user message → wake → agent `aq reply` → `message.replied` → adapter render; planner task creates a graph from a spec fixture |

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Session-runtime slips; delivery engine blocked | Phases 0–2 are independent; queue+prime degraded mode is functional (inbox consumed at session start) |
| Transcript-tail fallback posts partial/echoed text | Fallback only after `reply_timeout` **and** a completed turn since delivery; marked `via=transcript_tail`; disable via config |
| Double delivery (nudge/inject race) | `mark_delivered` compare-and-set; inject path re-checks pending atomically |
| Prompt injection via message bodies | Bodies are data: envelope clearly frames origin; never interpolated into shell (G.3); slim tool surface bounds blast radius |
| Undelivered messages to dead/retired sessions accumulate | `check_reply_timeouts` sweep parks stale `to_kind=session` rows to `to_kind=user` after `park_after` (default 24 h) with a `system` note |
| On-demand cold start feels broken in chat | REPL/dashboard render `queued → delivered` states; Discord posts a lightweight "waking supervisor…" on first delivery |
| Graph validator too strict for hand-written specs | `--dry-run` everywhere; warnings vs errors split per design §8.3 |
| Legacy flags linger | Each flag names its removal phase in config comments; Phase 5 checklist flips defaults |
