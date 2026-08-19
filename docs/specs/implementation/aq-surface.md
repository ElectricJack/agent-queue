---
tags: [implementation, cli, mcp, api, auth, prime, surface]
---

# `aq` Surface — Implementation Spec

**Status:** Draft — approved direction (2026-08-19)
**Related:** [[../design/aq-surface]] (design), [[../design/session-runtime]], [[../design/work-graph]], [[../design/supervisor-agent]], [[../design/feature-pauses]], [[../design/trust-and-ops]], [[../../analysis/framework-overhaul-todo]]

Design decisions live in [[../design/aq-surface]]; this document is the build plan. All code
is async-first, ruff line-length 100, py312. Every new state-changing operation is a
`CommandHandler` command returning `{"success": bool, ...}`.

---

## 1. Module Layout

```
src/prime/                          # NEW — prime renderer (design §5)
  __init__.py                       # exports PrimeRenderer, PrimeDocument
  models.py                         # PrimeSection, PrimeDocument dataclasses
  renderer.py                       # PrimeRenderer — assembly only, no I/O side effects
  sections.py                       # per-section builders (role, task, workspaces, ...)
  overrides.py                      # .aq/PRIME.md loading + mustache substitution
  hook_envelopes.py                 # per-harness hook output wrapping + suppression
  templates/
    tool_guidance.md                # section 9 static template
    completion_protocol.md          # section 10 static template
    hooks/claude.json               # hook file templates (design §5.5)
    hooks/<harness>.json|toml       # added with each harness

src/api/
  auth.py                           # NEW — SessionTokenStore, RequestScope, mint/validate/revoke
  scope.py                          # NEW — AGENT_COMMAND_SET, check_command_scope()
  middleware.py                     # MOD — add TokenAuthMiddleware
  app.py                            # MOD — wire token store + middleware
  dependencies.py                   # MOD — _token_store + get_token_store()
  execute.py                        # MOD — scope check in api_execute

src/commands/
  surface_commands.py               # NEW mixin — _cmd_prime, _cmd_get_schema, _cmd_task_show,
                                    #   _cmd_task_set, _cmd_task_close, _cmd_task_heartbeat,
                                    #   _cmd_ask_human, _cmd_task_handoff
  handler.py                        # MOD — mix in SurfaceCommandsMixin

src/cli/
  envelope.py                       # NEW — SCHEMA_VERSION, envelope(), emit(), BRIEF_PROJECTIONS
  agent_surface.py                  # NEW — aq prime|handoff|schema, aq inbox --inject,
                                    #   aq session drain-ack
  sessions.py                       # NEW — aq session list|peek|attach|nudge|logs|kill
  gates.py                          # NEW — aq gate list|resolve
  workspaces.py                     # NEW — aq workspace list|doctor|reap
  chat.py                           # NEW — aq chat <project>
  tasks.py                          # MOD — add show|set|close|heartbeat|ask|explain|graph|list;
                                    #   alias details → show
  client.py                         # MOD — AQ_API_URL/AQ_API_TOKEN, bearer header
  app.py                            # MOD — import new modules before register_auto_commands

src/mcp_registration.py             # MOD — DEFAULT_TASK_ALLOWLIST, get_effective_allowlist(),
                                    #   register_task_scope_tools()
src/embedded_mcp.py                 # MOD — second FastMCP mounted at /mcp-task
src/llm_logger.py                   # MOD — log_context_cost()
src/config.py                       # MOD — ApiAuthConfig, McpServerConfig.task_scope
migrations/versions/<rev>_api_session_tokens.py   # NEW
```

The renderer lives in `src/prime/`, not `src/context/` — rationale in design §5.1.

---

## 2. Prime Renderer (`src/prime/`)

```python
# models.py
@dataclass(frozen=True)
class PrimeSection:
    key: str            # "role" | "project_role" | "task" | "task_context" | "workspaces"
                        # | "messages" | "l1_facts" | "l2_context" | "tool_guidance"
                        # | "completion_protocol"
    title: str
    body: str           # empty string ⇒ omitted from markdown, still a template variable

@dataclass(frozen=True)
class PrimeDocument:
    task_id: str
    session_id: str | None
    sections: tuple[PrimeSection, ...]     # canonical order (design §5.2)
    source: str                            # "default" | "override:.aq/PRIME.md"
    rendered_at: datetime

    def to_markdown(self) -> str: ...
    def section_vars(self) -> dict[str, str]: ...     # for overrides.apply_override
    def tokens_est(self) -> int: ...                  # len(markdown) // 4

# renderer.py
class PrimeRenderer:
    def __init__(self, db: Database, config: AppConfig) -> None: ...

    async def render_for_task(
        self, task_id: str, *, session_id: str | None = None, work_dir: str | None = None
    ) -> PrimeDocument:
        """Assemble sections 1–10. L1/L2 builders return empty sections while
        config.memory.enabled is False (src/config.py:296)."""

# overrides.py
def load_override(work_dir: Path) -> str | None          # <work_dir>/.aq/PRIME.md
def apply_override(template: str, doc: PrimeDocument) -> str   # mustache {{vars}}

# hook_envelopes.py
def wrap(body: str, harness: str) -> str                  # "claude" → hookSpecificOutput JSON
def suppressed(env: Mapping[str, str], hook_mode: bool) -> bool
    # env.get("AQ_STARTUP_PROMPT_DELIVERED") == "1" and hook_mode
```

Section builders in `sections.py` reuse `extract_section()` from `src/prompt_builder.py:47`
for pulling `## Role` from profile markdown — do not duplicate it. Spec refs (context type
`spec_ref`) are resolved to the referenced file + heading and inlined.

**Consumers.** (1) `_cmd_prime` (§3). (2) [[../design/session-runtime]]'s prompt-file writer
imports `PrimeRenderer` directly (same process) and writes
`doc.to_markdown()` → `<work_dir>/.aq/prompt.md`, then sets `AQ_STARTUP_PROMPT_DELIVERED=1`
in the session env.

---

## 3. CommandHandler Additions (`src/commands/surface_commands.py`)

New mixin, mixed into `CommandHandler` in `src/commands/handler.py` alongside the existing
`*_commands.py` mixins (same pattern as `task_commands.py`). Commands (names match the MCP
allowlist exactly):

| Command | Args | Behavior |
|---|---|---|
| `prime` | `task_id?`, `session_id?`, `work_dir?` | resolves task from request scope when omitted; returns `{"success": True, "body", "sections", "source", "tokens_est"}` |
| `get_schema` | — | `{"success": True, "schema_version": 1, "enums": {...}}` — introspects enums from `src/models.py` + work-graph/session enums |
| `task_show` | `task_id` | task + work-state + deps + gates + context refs (single round trip; composes existing queries) |
| `task_set` | `task_id`, `branch?`, `pr_url?`, `work_dir?`, `note?`, `labels_add?`, `labels_remove?`, `meta?` | work-state contract writes; **no status transitions** (state machine owned by [[../design/work-graph]]) |
| `task_close` | `task_id`, `outcome`, `failure_class?`, `work_outcome?`, `commit?`, `notes?` | validates enums via `get_schema` source, delegates transition to work-graph's `transition_task`; emits `task.closed` |
| `task_heartbeat` | `task_id?` | refreshes `agents.last_heartbeat` / lease; returns `lease_expires_at` |
| `ask_human` | `question`, `task_id?` | creates a human gate ([[../design/work-graph]]) + message; returns ids |
| `task_handoff` | `subject?`, `detail?`, `auto: bool` | writes `task_context(type=handoff)`; non-auto also emits `session.restart_requested` on the EventBus |

`message_send|message_inbox|message_reply` are implemented by [[../design/supervisor-agent]];
`memory_save|memory_search` pause behavior by [[../design/feature-pauses]];
`session_*` by [[../design/session-runtime]]. This spec only requires their names to match
the inventory. All new commands are auto-exposed via MCP pass 3
(`src/mcp_registration.py:142` `_discover_all_commands`) — add explicit rich schemas to
`src/tools/definitions.py` for the nine allowlist commands so task-scope schemas stay tight
and intentional.

---

## 4. API Auth

### 4.1 Token store (`src/api/auth.py`)

```python
TOKEN_PREFIX = "aqs_"

@dataclass(frozen=True)
class RequestScope:
    kind: Literal["local", "session"]
    session_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None

LOCAL_SCOPE = RequestScope(kind="local")

class SessionTokenStore:
    def __init__(self, db: Database, *, ttl_hours: int = 72) -> None: ...
    async def mint(self, *, session_id: str, task_id: str | None, project_id: str) -> str
        # returns plaintext once; stores sha256 hex in api_session_tokens
    async def validate(self, token: str) -> RequestScope | None
        # in-memory dict cache keyed by hash (invalidated on revoke); checks expiry/revoked
    async def revoke_session(self, session_id: str) -> int
    async def revoke_expired(self) -> int          # cascade housekeeping step
```

Mint/revoke are called by [[../design/session-runtime]] at session start/end (in-process —
no HTTP hop). A `revoke_expired()` sweep joins the existing 5s cascade housekeeping.

### 4.2 Migration

`api_session_tokens` (works on SQLite + PostgreSQL; `alembic revision --autogenerate`):

| Column | Type | Notes |
|---|---|---|
| `token_hash` | `Text` PK | sha256 hex |
| `session_id` | `Text` NOT NULL, indexed | |
| `task_id` | `Text` nullable | |
| `project_id` | `Text` nullable | soft ref, matches `agents.profile_id` pattern |
| `created_at` / `expires_at` / `revoked_at` | `DateTime` | `revoked_at` nullable |

### 4.3 Middleware and enforcement — exact integration points

- `src/api/middleware.py` — add `TokenAuthMiddleware(BaseHTTPMiddleware)`: reads
  `Authorization: Bearer aqs_…`; on valid token sets `request.state.scope = RequestScope(...)`;
  on invalid/expired token returns 401 JSON; on no token sets `LOCAL_SCOPE`. Keep
  `RequestContextMiddleware` (src/api/middleware.py:13) untouched but bind `session_id` into
  its structlog context when a scope is present.
- `src/api/app.py:76` — after `app.add_middleware(RequestContextMiddleware)`, add
  `app.add_middleware(TokenAuthMiddleware)`. Starlette runs last-added first, so token
  resolution happens before request-context binding reads it.
- `src/api/app.py:57–68` — alongside `deps._command_handler` wiring, add
  `deps._token_store = SessionTokenStore(orchestrator.db, ttl_hours=config.api_auth.token_ttl_hours)`.
- `src/api/dependencies.py` — module-level `_token_store` + `get_token_store()` accessor,
  mirroring `get_command_handler()` (src/api/dependencies.py:25).
- `src/api/execute.py:31–51` — `api_execute` gains a `request: Request` parameter; before
  `ch.execute(...)` call `check_command_scope(body.command, body.args, request.state.scope)`
  from `src/api/scope.py`; violations return
  `JSONResponse({"ok": False, "error": "out of scope: …"}, status_code=403)`. The
  `{"ok", "result"|"error"}` wire shape is unchanged.
- `src/api/scope.py` — `AGENT_COMMAND_SET: frozenset[str]` (design §3.1 command names +
  `prime`, `get_schema`) and pure function
  `check_command_scope(command, args, scope) -> str | None` (error message or None). Rules:
  `local` scope → allow all; `session` scope → command must be in `AGENT_COMMAND_SET`, any
  `task_id` arg must equal `scope.task_id`, any `project_id` arg must equal
  `scope.project_id`. Pure function ⇒ trivially table-tested.

The typed auto-generated routers (`src/api/app.py:83–85`) sit behind the same middleware;
scope checks for those routes reuse `check_command_scope` via a FastAPI dependency added in
`register_all_routers` (follow-up within Phase 2 — `/api/execute` is the path agents use).

---

## 5. CLI

### 5.1 Client (`src/cli/client.py`)

- `_resolve_api_url()` (src/cli/client.py:32): check `AQ_API_URL` first, then the existing
  `AGENT_QUEUE_API_URL`, then config, then default.
- `CLIClient.__init__(base_url=None, token=None)`: `token = token or os.environ.get("AQ_API_TOKEN")`;
  when set, add `Authorization: Bearer <token>` to the `httpx.AsyncClient` default headers in
  `connect()` (src/cli/client.py:151–152).
- `execute()` continues to use `/api/execute` only (src/cli/client.py:184–195); the disabled
  typed dispatch stays disabled — the generated `packages/aq-client` remains the dashboard's
  client, not the CLI's. On HTTP 403 raise `ScopeError` (new, in `src/cli/exceptions.py`) →
  exit 4.

### 5.2 Envelope (`src/cli/envelope.py`)

```python
SCHEMA_VERSION = 1

def envelope(data: Any, *, total: int | None = None) -> dict
    # list data → pagination {returned, total or len(data), truncated}
def error_envelope(code: str, message: str) -> dict
def emit(ctx: click.Context, data: Any, *, entity: str | None = None, total: int | None = None)
    # honors ctx.obj["json"], ctx.obj["brief"], AQ_JSON_LEGACY=1

BRIEF_PROJECTIONS: dict[str, tuple[str, ...]] = {"task": (...), "session": (...), ...}
```

Add a global `--brief` flag next to `--json` in the `cli` group (src/cli/app.py:145–167).
All new commands and (incrementally) existing ones route output through `emit()`.

### 5.3 New command modules

Registered by importing them in `src/cli/app.py` **before** `register_auto_commands(cli,
console)` (src/cli/app.py:261–263), joining the existing import block at lines 249–254 —
hand-written groups must exist first so auto-generation skips those names instead of
claiming `session`, `gate`, `workspace`, `schema`, or `chat`. Behavior notes:

- `aq prime`: calls `execute("prime", {})`; task resolved server-side from the bearer scope.
  `--hook-json` / `--hook-format` wrap via `src/prime/hook_envelopes.py` (imported directly —
  wrapping is presentation, no daemon needed); suppression check before the API call.
- `aq inbox --inject`: `asyncio.wait_for(..., timeout=14.0)` inside the 15s budget; every
  exception path prints nothing and exits 0.
- `aq session attach`: prints the provider attach command from `session_show`; with a TTY,
  offers to exec it.
- `aq session logs -f`: follows `GET /api/sessions/{id}/stream` (SSE, owned by
  session-runtime) via the shared `httpx` client.
- `aq chat <project>`: REPL over `POST /api/sessions/{name}/message` + SSE reply stream.
- Paused memory results (`error == "memory paused"`) map to exit 0 + `{paused: true}` data.

---

## 6. MCP Task Scope

### 6.1 `src/mcp_registration.py`

```python
DEFAULT_TASK_ALLOWLIST = frozenset({
    "task_show", "task_set", "task_close", "task_heartbeat", "ask_human",
    "message_send", "message_inbox", "memory_save", "memory_search",
})

def get_effective_allowlist(config: Any | None = None,
                            profile_widenings: Iterable[str] = ()) -> set[str]
    # DEFAULT_TASK_ALLOWLIST ∪ config.mcp_server.task_scope.allowlist_extra ∪ widenings,
    # minus get_effective_exclusions() — exclusions always win (defense in depth)

def register_task_scope_tools(
    mcp_server: FastMCP,
    registered_union: set[str],
    resolve_session_allowlist: Callable[[str | None], Awaitable[set[str]]],
) -> list[str]
```

`register_task_scope_tools` mirrors `register_command_tools`
(src/mcp_registration.py:186–326) but registers only `registered_union` (default allowlist ∪
all profiles' `## Config.mcp_tools`, recomputed on profile sync) and wraps each handler:
extract the bearer token from the streamable-http request
(`ctx.request_context.request.headers`), resolve the session's effective allowlist via
`resolve_session_allowlist`, reject with `{"success": False, "error": "out of scope"}` if the
tool or its task/project args fall outside the scope (reusing `src/api/scope.py`
`check_command_scope`), else delegate to `CommandHandler.execute` exactly like the trusted
handlers. Call-time enforcement is the boundary; `tools/list` shows the union (design §8.3).

`DEFAULT_EXCLUDED_COMMANDS` and `get_effective_exclusions` (src/mcp_registration.py:51–110)
are unchanged and keep governing the trusted scope.

### 6.2 `src/embedded_mcp.py`

In `run_mcp_server`, after the trusted `mcp` instance (src/embedded_mcp.py:86–106):

1. Create `mcp_task = FastMCP(name="agent-queue-task", lifespan=embedded_lifespan, ...)` with
   `streamable_http_path="/mcp-task"`; call `register_task_scope_tools(...)`. No resources or
   prompts on the task server.
2. Mount both sub-apps (src/embedded_mcp.py:137): append `Mount("/", app=mcp_task_app)`
   before the trusted mount — the two streamable apps route distinct paths (`/mcp-task` vs
   `/mcp`), and specific-first ordering keeps `/mcp-task` from being swallowed.
3. Extend `_combined_lifespan` (src/embedded_mcp.py:144–149) to
   `async with mcp.session_manager.run(), mcp_task.session_manager.run():`.
4. Injection retarget: wherever `mcp_server.inject_into_tasks` builds the task's
   `mcp_servers` entry, point it at `{base_url}/mcp-task` with header
   `Authorization: Bearer ${AQ_API_TOKEN}` (env value injected by session-runtime).

---

## 7. Config Keys

| Key | Default | Purpose |
|---|---|---|
| `api_auth.token_ttl_hours` | `72` | backstop expiry for session tokens |
| `api_auth.require_session_token` | `false` | reserved enforcement hook for [[../design/trust-and-ops]]; when true, agent-surface commands without a token are rejected for non-loopback clients |
| `mcp_server.task_scope.enabled` | `false` → `true` at Phase-3 flip | serve `/mcp-task` |
| `mcp_server.task_scope.allowlist_extra` | `[]` | install-wide widening |
| `mcp_server.inject_into_tasks` | existing | retargeted to `/mcp-task` when task scope enabled |
| `surface.context_cost_ceiling_tokens` | `8000` | `aq doctor` warning threshold (design §10) |

New `ApiAuthConfig` dataclass in `src/config.py` + `AppConfig.api_auth` field
(src/config.py:804–847) + parse block near the `mcp_server` parse (src/config.py:1672–1680);
`task_scope` becomes a nested dataclass on `McpServerConfig` (src/config.py:636–640).
Config edits flow through `src/config_editor.py` untouched (schema picks up new fields).

---

## 8. Analytics Instrumentation

`LLMLogger.log_context_cost(**fields)` appends the design §10 record to
`prompt_analytics.jsonl` via the existing `_append` (src/llm_logger.py:314). Called from the
session-start path: prime tokens from `PrimeDocument.tokens_est()`; MCP schema size by
serializing the session's effective tool definitions (reuse the registered `Tool.parameters`
dicts). No new log stream, no new retention rules.

---

## 9. Phase Checklist

**Phase S0 — output contract (no behavior change for agents)**
- [ ] `src/cli/envelope.py` + global `--brief`; `AQ_JSON_LEGACY` escape hatch
- [ ] `get_schema` command + `aq schema`
- [ ] `CLIClient`: `AQ_API_URL`/`AQ_API_TOKEN` support (token unused until S2)
- [ ] `aq task show|set|list` (+ `details` alias); route through `emit()`
- [ ] Fix `src/cli/CLAUDE.md` stale "direct SQLite" claim

**Phase S1 — prime and hooks**
- [ ] `src/prime/` package: models, renderer, sections, overrides, hook_envelopes, templates
- [ ] `prime` + `task_handoff` commands; `aq prime|handoff`, `aq inbox --inject` (inbox
      delivery pending supervisor-agent's `messages` table — stub prints nothing, exit 0)
- [ ] Hook file templates (`templates/hooks/claude.json`); handshake with session-runtime's
      prompt-file writer and `AQ_STARTUP_PROMPT_DELIVERED`
- [ ] Rich tool definitions for the nine allowlist commands in `src/tools/definitions.py`

**Phase S2 — auth**
- [ ] `api_session_tokens` migration (SQLite + PostgreSQL); autogenerate + review
- [ ] `src/api/auth.py`, `src/api/scope.py`; middleware + `create_app` wiring; `/api/execute`
      scope check; `revoke_expired` cascade step
- [ ] Mint/revoke integration points published for session-runtime

**Phase S3 — MCP task scope**
- [ ] `DEFAULT_TASK_ALLOWLIST`, `get_effective_allowlist`, `register_task_scope_tools`
- [ ] Second FastMCP + `/mcp-task` mount + combined lifespan; injection retarget
- [ ] Profile `## Config.mcp_tools` parsing (with `src/profiles/parser.py`)

**Phase S4 — human surface + measurement**
- [ ] `aq session|gate|workspace|doctor|chat` projections (as owning subsystems land)
- [ ] `log_context_cost` + baseline capture + flag flip + before/after report

---

## 10. Test Plan

- **Unit:** renderer golden tests (fixture vault + task → expected markdown; override
  template; memory-paused slots empty); `envelope()`/`emit()` shapes incl. pagination and
  legacy mode; `BRIEF_PROJECTIONS` completeness vs models; `check_command_scope` truth table;
  `SessionTokenStore` mint/validate/expiry/revoke (freeze time); `get_effective_allowlist`
  union/exclusion precedence; hook envelope wrap + suppression matrix.
- **API integration (FastAPI TestClient):** `/api/execute` untouched for local callers;
  401 invalid token; 403 out-of-scope command and cross-task `task_close`; scoped happy path;
  middleware ordering (scope visible to request context).
- **MCP integration:** streamable-http client against `/mcp-task` — `tools/list` = union;
  allowed call delegates; widened-but-not-my-profile call → out-of-scope error; `/mcp`
  behavior byte-identical to today (regression snapshot of registered tool names).
- **CLI (CliRunner + mocked CLIClient):** exit-code contract (0/1/3/4; paused → 0);
  `aq inbox --inject` exits 0 on daemon-down and on timeout; `aq prime --hook-json` envelope;
  alias `task details` → `task show`.
- **Migration:** `pytest tests/test_database.py -v` pattern on both engines.
- **Invariant (G.4):** command inventory in this spec ⇄ `_cmd_*` methods ⇄ CLI groups ⇄
  allowlist names — a docs-sync test that fails when they drift.
- **Cross-spec conformance:** session-runtime `fake` provider e2e — mint token → prime →
  heartbeat → close → drain-ack → revoke.

---

## 11. Rollout Flags

| Flag | Ship default | Flip condition |
|---|---|---|
| `AQ_JSON_LEGACY=1` (env) | envelope on | removed one release after S0 |
| `mcp_server.task_scope.enabled` | `false` | after S3 tests + session-runtime injects tokens |
| `mcp_server.inject_into_tasks` → `/mcp-task` | tied to task_scope flag | same flip |
| `api_auth.require_session_token` | `false` | [[../design/trust-and-ops]] decision; not this spec's flip |

Rollback for each is config-only; no migration is destructive (`api_session_tokens` is
additive).

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| `--json` envelope breaks existing scripts | `AQ_JSON_LEGACY` escape + stderr deprecation warning; one-release window |
| `tools/list` shows widened tools a session can't call | call-time guard is the boundary; error message names the CLI fallback; revisit per-session list filtering if agents thrash |
| Bearer token readable in session env / process table | accepted for v1 (loopback-only daemon); scrubbing + exposure rules in [[../design/trust-and-ops]]; tokens narrow, never widen |
| Unauthenticated local path remains fully privileged | by design (today's model); `require_session_token` reserved for hardening |
| Two FastMCP session managers in one lifespan | combined `async with`; supervised-restart loop (src/embedded_mcp.py:128) resets both `_session_manager`s |
| `register_auto_commands` name collisions with new groups | import order enforced + a unit test asserting hand-written groups win |
| chars/4 token estimates are crude | fine for before/after deltas; the measurement compares like with like |
| Cross-spec sequencing (messages, gates, sessions not landed) | every dependent command degrades: inbox prints nothing/exit 0, gate/session groups hidden until their commands exist (probed via `get_schema` capabilities list) |
