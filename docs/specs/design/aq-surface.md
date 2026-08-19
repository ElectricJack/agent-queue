---
tags: [design, cli, mcp, api, auth, prime, surface]
---

# `aq` Surface — CLI-First Interface for Agents and Humans

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [[guiding-design-principles]] (#2 visible and editable, #7 events not coupling, #9 simple interfaces, #10 fewer moving parts)
**Related:** [[session-runtime]], [[supervisor-agent]], [[work-graph]], [[feature-pauses]], [[trust-and-ops]], [[workspaces-v2]], [[../analysis/framework-overhaul-todo]] (§5 Workstream C, §2 A.4, §7 Workstream E, §10 G.2)

---

## 1. Problem Statement

Agents today reach agent-queue through an auto-generated MCP registry of ~150 tools
(`src/mcp_registration.py` exposes every `CommandHandler` command). Every task session pays
for those tool schemas in its context window before doing any work. The overhaul analysis
([[../analysis/framework-overhaul-todo]] §10 G.2, drawing on the Beads comparison) measured
full MCP tool schemas at **10–50k tokens per session versus 1–2k tokens for an equivalent
CLI + hooks surface**. With the move to interactive CLI sessions ([[session-runtime]], D1/D6),
the daemon no longer sits inside the agent's process — the natural, harness-agnostic way for
any agent (Claude, Codex, Gemini, opencode, …) to act on the system is to shell out to a
binary that already exists on `PATH`.

Humans have the inverse problem: the Discord mirrored-command surface is being removed (D7),
the dashboard is not yet complete, and the current `aq` CLI covers only a slice of operations
with inconsistent JSON output.

**Decision D6: the `aq` CLI is the primary surface for both agents and humans.** MCP shrinks
to a small task-scoped allowlist; REST stays as the transport underneath; every surface
remains a projection of `CommandHandler`.

---

## 2. Shape of the Surface

`CommandHandler` remains the single entry point for all state changes. The surfaces stack on
top of it:

| Layer | Who uses it | Transport | Notes |
|---|---|---|---|
| `aq` CLI | agents (in sessions), humans (terminal) | REST `POST /api/execute` | Primary surface. Versioned JSON envelope, `--brief`, `aq schema`. |
| REST API | dashboard, Discord adapter, `aq` itself | HTTP + WS | Typed routes + `/api/execute`; bearer-token auth for agent sessions. |
| MCP (trusted scope) | human-side MCP clients (Claude Code on the operator's machine, IDEs) | streamable-http `/mcp` | Full registry minus exclusions — unchanged behavior. |
| MCP (task scope) | task sessions that prefer native tools over shelling out | streamable-http `/mcp-task` | ~9-tool allowlist; profiles may widen. |

Agents inside sessions receive two environment variables from [[session-runtime]]:
`AQ_API_URL` (daemon base URL) and `AQ_API_TOKEN` (per-session bearer token, §7). The `aq`
binary reads both automatically; no per-command flags are needed inside a session. Humans on
the daemon host use `aq` unauthenticated exactly as today.

Everything below the CLI is shared: the same command produces the same
`{"success": bool, ...}` dict whether it arrived via CLI, MCP, or a typed REST route.

---

## 3. Command Inventory

Legend: **A** = agent-facing (taught by `aq prime`), **H** = human-facing, **A/H** = both.
"Output" describes the `data` payload of the JSON envelope (§4.1); human mode renders the
same data as Rich tables/panels.

### 3.1 Agent surface

| Command | Args | Aud. | Output (`data`) |
|---|---|---|---|
| `aq prime` | `[--hook-json] [--hook-format <harness>]` | A | markdown body (plain), or hook envelope JSON (§5.4) |
| `aq handoff` | `[--auto] [subject] [detail]` | A | `{handoff_id, restart_requested}` |
| `aq inbox --inject` | — | A (hook) | plain text for prompt injection; always exit 0 (§6.2) |
| `aq task show` | `<task_id>` | A/H | full task object: fields, work-state, deps, gates, context refs |
| `aq task set` | `<task_id> [--branch] [--pr-url] [--work-dir] [--note] [--label +l/-l] [--meta k=v]` | A/H | updated task summary |
| `aq task close` | `<task_id> --outcome pass\|fail [--failure-class transient\|hard] [--work-outcome shipped\|no-op\|blocked\|abandoned] [--commit <sha>] [--notes <text>]` | A | `{task_id, status}` |
| `aq task heartbeat` | `[<task_id>]` (defaults to token scope) | A | `{ok: true, lease_expires_at}` |
| `aq task ask` | `"<question>" [--task <id>]` | A | `{question_id, gate_id}` — human gate via [[work-graph]] |
| `aq message send` | `<recipient> <body> [--task <id>]` | A/H | `{message_id}` |
| `aq message inbox` | `[--unread]` | A/H | list of messages |
| `aq message reply` | `<message_id> <body>` | A/H | `{message_id}` |
| `aq memory save` | `<content> [--scope]` | A | paused: `{paused: true}` per [[feature-pauses]] (§9.3) |
| `aq memory search` | `<query> [--scope]` | A | paused: `{paused: true, results: []}` |
| `aq session drain-ack` | — | A | `{acknowledged: true}` — session may now be reaped |

### 3.2 Human surface

| Command | Args | Aud. | Output (`data`) |
|---|---|---|---|
| `aq session list` | `[--project] [--state]` | H | list of sessions (id, task, state, activity) |
| `aq session peek` | `<session_id> [-n <lines>]` | H | captured pane text |
| `aq session attach` | `<session_id>` | H | prints/execs the provider attach command |
| `aq session nudge` | `<session_id> <text>` | H | `{submitted: bool}` |
| `aq session logs` | `<session_id> [-f]` | H | transcript entries (follow via SSE) |
| `aq session kill` | `<session_id> [-y]` | H | `{killed: true}` |
| `aq task list` | `[--project] [--status] [--label] [--brief]` | A/H | list of tasks |
| `aq task explain` | `<task_id>` | H | blocker analysis per [[work-graph]] |
| `aq task graph` | `[--project] [--format ascii\|dot\|json]` | H | dependency graph |
| `aq project ready` | `[<project_id>]` | H | ready tasks + not-ready reasons |
| `aq gate list` | `[--project] [--pending]` | H | list of gates |
| `aq gate resolve` | `<gate_id> [--approve\|--reject] [--note]` | H | `{gate_id, resolution}` |
| `aq workspace list` | `[--project]` | H | workspaces + worktree slots ([[workspaces-v2]]) |
| `aq workspace doctor` | `[--fix]` | H | orphan/stale findings |
| `aq workspace reap` | `[--project] [-y]` | H | reaped slots/branches |
| `aq doctor` | `[--fix]` | H | system health findings (G.1) |
| `aq chat` | `<project>` | H | interactive REPL to the project supervisor session ([[supervisor-agent]]) |
| `aq schema` | — | A/H | enum catalog (§4.3) |

### 3.3 Preserved existing commands

`aq status` (default), `aq task create|approve|stop|restart|search|select`, `aq task details`
(kept as an alias of `aq task show`), `aq project list|details|set`, `aq plugin *`, `aq vault *`,
`aq logs`, daemon lifecycle (`aq start|stop|restart`), and the auto-generated category groups
from `register_auto_commands` all continue to work (§9).

---

## 4. Output Contract

### 4.1 Versioned JSON envelope

Every command run with `--json` emits exactly one JSON object on stdout:

```json
{
  "schema_version": 1,
  "data": { ... },
  "pagination": {"returned": 20, "total": 143, "truncated": true}
}
```

- `schema_version` is an integer, incremented only on breaking envelope changes. Command
  payload evolution is additive and does not bump it.
- `data` is the command's payload — an object for singular commands, an array for lists.
- `pagination` is present **only when `data` is a list**: `returned` = items in this
  response, `total` = matching rows server-side, `truncated` = `returned < total`.
- Errors: `{"schema_version": 1, "error": {"code": "...", "message": "..."}, "data": null}`
  on stdout, non-zero exit. Error codes: `command_error`, `not_found`, `out_of_scope`,
  `daemon_unreachable`, `paused`.

The envelope is applied by the CLI presentation layer on top of the unchanged
`CommandHandler` `{"success": bool, ...}` dicts and the `/api/execute`
`{"ok": bool, "result"|"error"}` wire format — neither changes.

Exit codes: `0` success (and all `paused` no-ops, so agent loops don't spuriously fail),
`1` command error, `2` usage error (Click), `3` daemon unreachable, `4` auth/scope denied.
`aq inbox --inject` always exits `0` regardless (§6.2).

### 4.2 `--brief` lite projections

`--brief` trims each entity to a fixed projection so agents can list cheaply:

| Entity | Brief fields |
|---|---|
| task | `id, title, status, priority, project_id, assigned_agent` |
| session | `id, task_id, state, harness, last_activity` |
| gate | `id, gate_type, status, task_id` |
| message | `id, from, subject, created_at, read` |
| workspace | `id, kind_id, path, locked_by` |

`--brief` composes with `--json` (trimmed `data` items, envelope unchanged) and with table
output (fewer columns). Projections are defined centrally, not per command.

### 4.3 `aq schema`

Prints the system's enums so agents never guess magic strings and MCP schemas don't have to
carry them: task statuses, task types, dependency types (`blocks`, `parent-child`,
`waits-for`, `conditional-blocks`, `discovered-from`, `related`, `duplicates`,
`supersedes`), gate types (`human`, `timer`, `pr-merged`, `ci-run`, `event`, `task`),
lifecycle values (`task`, `named`), outcome enums (`outcome`, `failure_class`,
`work_outcome`), and session states. Backed by a `get_schema` command (so REST and MCP get
it too); the enum values themselves are owned by [[work-graph]] and [[session-runtime]] —
`aq schema` is a projection with its own `schema_version`.

---

## 5. `aq prime` — Context Delivery

### 5.1 One renderer, two consumers

The full startup context is produced by a single renderer module, **`src/prime/`**, with two
consumers:

1. **`aq prime`** — the CLI command, run by the agent (bootstrap instruction) or by the
   `SessionStart` hook; fetches the rendered document from the daemon.
2. **The prompt-file writer** — [[session-runtime]] calls the same renderer at session start
   to write `<work_dir>/.aq/prompt.md` before the harness launches.

`src/prime/` is chosen over `src/context/` because: (a) "prime" is the domain term across
the overhaul (Gas City `gc prime`, Beads `bd prime`, hook wiring in A.4) — the module renders
*the prime document*, nothing else; (b) `src/context/` would collide conceptually with the
existing `task_context` rows and `prompt_builder.py` context blocks, inviting misplaced code;
(c) the module is deliberately narrow — when memory returns (Phase 4), L1/L2 render *into*
prime sections rather than prime growing into a general context framework.

The renderer is pure assembly: it reads profile markdown, task rows, `task_context` rows,
attachments, and workspace state, and produces an ordered list of sections. It performs no
LLM calls and no writes.

### 5.2 Section order (canonical)

| # | Section | Source | Notes |
|---|---|---|---|
| 1 | L0 profile role | `vault/agent-types/<id>/profile.md` `## Role` | who you are |
| 2 | Project override role | `vault/projects/<pid>/agent-types/<id>/profile.md` | specificity wins (principle #6) |
| 3 | Task pointer | `aq task show` summary | id, title, status, acceptance criteria |
| 4 | Task context | `task_context` rows incl. `spec_ref` sections + attachments | spec sections are inlined, not linked |
| 5 | Workspaces block | task work-state + attachments | `work_dir`, `branch`, other attached kinds |
| 6 | Pending messages + handoff note | `messages` table, latest `task_context(type=handoff)` | unread first; handoff verbatim |
| 7 | *(slot)* L1 facts | — | **skipped while memory is paused** ([[feature-pauses]]) |
| 8 | *(slot)* L2 topic context | — | skipped while memory is paused |
| 9 | Tool guidance | static template | CLI-first: "use `aq …`"; one line noting the minimal MCP set exists |
| 10 | Completion protocol | static template | "When done: `aq task close <id> --outcome … && aq session drain-ack`" |

Slots 7–8 exist in the section model from day one so the memory comeback is a renderer
change, not a protocol change.

### 5.3 Per-project override: `.aq/PRIME.md`

If `<work_dir>/.aq/PRIME.md` exists (committed to the project repo, so it rides into every
worktree), its body **replaces the default body entirely**. The override is a Mustache-style
template with the rendered sections available as variables (`{{role}}`, `{{task}}`,
`{{task_context}}`, `{{workspaces}}`, `{{messages}}`, `{{tool_guidance}}`,
`{{completion_protocol}}`, plus `{{task.id}}`, `{{work_dir}}`, `{{branch}}`), so a project
can reorder, drop, or wrap sections without losing them. This is the "files are the source
of truth" escape hatch (principle #1): a project that wants a different startup contract
edits a markdown file, not Python.

### 5.4 Hook modes and suppression

- `aq prime` (no flags): prints the markdown body — for humans, debugging, and harnesses
  whose bootstrap says "run `aq prime` and follow it".
- `aq prime --hook-json`: wraps the body in the Claude Code `SessionStart` hook envelope:
  `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<body>"}}`.
- `aq prime --hook-format <harness>`: per-harness envelope (Claude → the JSON above; harnesses
  without structured hook output → plain text). The format table ships with the hook
  templates (§5.5).
- **Suppression:** when `AQ_STARTUP_PROMPT_DELIVERED=1` is set **and** a hook mode is
  requested, the body is suppressed (empty `additionalContext`) — the bootstrap argv prompt
  already pointed the agent at `.aq/prompt.md`, and double delivery would waste the exact
  tokens this design saves. Post-compaction `SessionStart` events do deliver the body:
  [[session-runtime]] clears the variable's effect by design there (compaction is precisely
  when re-priming pays for itself).

### 5.5 Hook file templates

This spec owns the **contents** of the per-harness hook files; installation timing and the
`--settings` merge mechanics belong to [[session-runtime]]. Canonical Claude template
(merged, never overwritten, into the session's settings):

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "aq prime --hook-json", "timeout": 30}]}],
    "PreCompact":   [{"hooks": [{"type": "command", "command": "aq handoff --auto", "timeout": 30}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "aq inbox --inject", "timeout": 15}]}]
  }
}
```

No `Stop` hook — completion is explicit (A.5). Other harnesses get equivalent templates in
their native config formats, referenced from `vault/harnesses/<name>.md`.

---

## 6. Handoff and Inbox

### 6.1 `aq handoff [--auto] [subject] [detail]`

Writes a `task_context(type=handoff)` row on the current task (subject + detail + timestamp +
session id). The next `aq prime` for that task renders it in section 6 — this is how work
state survives compaction and session recycling while memory is paused (Workstream E).

- `--auto` (wired to `PreCompact`): **note only, never a restart** — Gas City's `gc-flp1`
  lesson: restarting on every compaction loops forever.
- Non-auto: note **plus** a restart request (`session.restart_requested` event). Restart
  mechanics — whether to recycle now, with what `wake_mode` — are owned by
  [[session-runtime]]; this command only records intent.

### 6.2 `aq inbox --inject`

The `UserPromptSubmit` hook body. Prints pending messages for this session's task/recipient,
formatted as a plain-text injection block; marks them delivered; honors the
`archive_after_inject` message policy (owned by [[supervisor-agent]]). Hard budget of
**15 seconds** and **always exit 0** — a broken daemon must never block the human's prompt
from reaching the agent. On any failure it prints nothing and exits 0.

---

## 7. Authentication — Per-Session Bearer Tokens

### 7.1 Mechanics (owned here)

- **Mint at session start:** [[session-runtime]] asks the token store for a token bound to
  `(session_id, task_id, project_id)`; the plaintext (`aqs_<random>`) is returned once and
  injected as `AQ_API_TOKEN` (env injection owned by session-runtime; scrubbing rules by
  [[trust-and-ops]]). Only a SHA-256 hash is stored.
- **Validate per request:** middleware reads `Authorization: Bearer …`, resolves the hash to
  a scope, and attaches it to the request. Unknown/expired/revoked → 401.
- **Revoke at session end:** drain-ack or the exit classifier triggers revocation. A TTL
  backstop (default 72 h) plus a cascade sweep that revokes tokens of dead sessions covers
  crashes. Tokens are persisted in the DB because sessions survive daemon restarts (A.5
  adoption) — an in-memory store would orphan live sessions on restart.

### 7.2 Scope model

A token scope is `(session_id, task_id, project_id)`. Requests carrying a session token may
only execute the **agent surface** command set (§3.1 plus `prime`, `get_schema`), and
task-addressed arguments must match the scope: `task_close` on someone else's task is
`out_of_scope` (403 / exit 4). Project-scoped commands (`message_*`, `memory_*`) bind to the
scope's project. Requests **without** a token keep today's behavior: full local trust on the
loopback interface (the daemon already binds `127.0.0.1` by default). Tokens therefore only
ever *narrow* — they never grant anything a local caller lacks. Hardening the unauthenticated
path for network exposure is [[trust-and-ops]] scope, with a `require_session_token` flag
reserved here as the enforcement hook.

---

## 8. MCP — Two Scopes

### 8.1 Trusted scope (unchanged)

The existing registry at `/mcp` stays for trusted MCP clients: all commands minus
`get_effective_exclusions()` (defaults `DEFAULT_EXCLUDED_COMMANDS` ∪ config
`mcp_server.excluded_commands` ∪ `AGENT_QUEUE_MCP_EXCLUDED`). Nothing about this scope
changes; it is the operator's surface.

### 8.2 Task scope (new): inversion to an allowlist

A second MCP mount at `/mcp-task` exposes only the default task allowlist:

`task_show, task_set, task_close, task_heartbeat, ask_human, message_send, message_inbox,
memory_save, memory_search`

Rationale: these are the calls an agent makes *mid-turn* where a native tool call beats
shelling out; everything else goes through the CLI, which costs ~1–2k tokens of learned
usage instead of 10–50k tokens of schemas ([[../analysis/framework-overhaul-todo]] G.2).
The exclusion model (`get_effective_exclusions`) is **not** reused here — task scope is an
include-list (`DEFAULT_TASK_ALLOWLIST` ∪ widenings), because the failure mode of a
forgotten exclusion is exposure, while the failure mode of a forgotten inclusion is an agent
falling back to the CLI, which is the designed path anyway.

### 8.3 Profile widening

A profile may declare `## Config.mcp_tools: [name, ...]` to widen its sessions' allowlist
(e.g. the supervisor profile adds `task_create`, `gate_resolve`). The task-scope server
registers the **union** of the default allowlist and all profiles' widenings; per-session
enforcement happens at call time against the token's resolved profile (the bearer token from
§7 identifies the session). Call-time enforcement is the security boundary; `tools/list`
filtering per session is best-effort (an agent may see a widened tool it cannot call — the
call returns a clear `out_of_scope` error).

### 8.4 Injection into tasks

`mcp_server.inject_into_tasks` currently injects the full server into every task. It is
retargeted: task sessions get `/mcp-task` plus their bearer token as a header; the full
`/mcp` URL is never handed to task sessions. Trusted clients configure `/mcp` manually as
today.

---

## 9. Backward Compatibility

1. **Existing commands** (§3.3) keep their names and behavior. New verbs join existing Click
   groups; `aq task details` becomes an alias of `aq task show`.
2. **`--json` output changes shape**: today it prints the raw result; it becomes the
   envelope. This is the one breaking change. Mitigation: `AQ_JSON_LEGACY=1` restores raw
   output for one release, with a deprecation warning on stderr.
3. **Env vars:** `AQ_API_URL` becomes canonical; `AGENT_QUEUE_API_URL` remains a fallback
   alias indefinitely (it is baked into existing shells and docs).
4. **Wire format:** `/api/execute` (`{"ok", "result"|"error"}`) and `CommandHandler`
   (`{"success": bool, ...}`) are untouched; the envelope lives purely in the CLI
   presentation layer. The generated `packages/aq-client` continues to serve the dashboard.
5. **MCP trusted scope** is bit-for-bit today's behavior; only task sessions move.
6. **Paused memory commands** return `{success: false, error: "memory paused"}` from the
   handler ([[feature-pauses]]); the CLI maps this specific error to exit 0 with
   `{paused: true}` data so agent scripts and hooks don't fail loops on a deliberate pause.

Known documentation drift to fix alongside: `src/cli/CLAUDE.md` still claims the CLI talks
directly to SQLite; the code (`src/cli/client.py`) has been REST-first for some time.

---

## 10. Measurement Plan — `prompt_analytics.jsonl`

The claim behind this design (context cost drops by an order of magnitude) must be measured,
not asserted. `LLMLogger` already maintains `logs/llm/prompt_analytics.jsonl`
(`src/llm_logger.py`); a new `context_cost` record is appended at session start:

```json
{"kind": "context_cost", "ts": "...", "session_id": "...", "task_id": "...",
 "profile": "...", "harness": "claude", "surface_mode": "full-mcp|task-scope",
 "prime_chars": 0, "prime_tokens_est": 0,
 "mcp_tools_count": 0, "mcp_schema_chars": 0, "mcp_schema_tokens_est": 0}
```

- `prime_*` from the rendered `PrimeDocument` (chars/4 estimate, same convention as
  `prompt_builder.py`).
- `mcp_schema_*` from serializing the effective `tools/list` payload the session would
  receive.
- **Before:** run ≥20 task sessions with `mcp_server.task_scope.enabled=false`
  (`surface_mode=full-mcp`) and record. **After:** flip the flag, run the same task mix.
- **Success criteria:** median `mcp_schema_tokens_est` drops from the measured baseline
  (expected 10–50k) to ≤2k, and median (`prime_tokens_est` + `mcp_schema_tokens_est`) drops
  ≥80%. Regression guard: `aq doctor` warns when a session's combined startup context
  estimate exceeds a configurable ceiling.

---

## 11. Ownership and Cross-References

| Concern | Owner | This spec's boundary |
|---|---|---|
| `messages` table, reply protocol, `archive_after_inject` | [[supervisor-agent]] | `aq message *`, inbox formatting/delivery marking |
| Gates, dep types, `explain`, state machine | [[work-graph]] | `aq gate *`, `aq task explain/graph/ask` projections |
| Session lifecycle, env injection, hook installation, restarts, drain | [[session-runtime]] | token mint/revoke API, hook file *contents*, `aq session *` projections, prime renderer called by its prompt-file writer |
| Pause semantics for memory/playbooks | [[feature-pauses]] | CLI presentation of paused results; L1/L2 prime slots |
| Env scrubbing, trust boundaries, network exposure | [[trust-and-ops]] | token *mechanics*; `require_session_token` enforcement hook |
| Worktrees, reaper, merge slot | [[workspaces-v2]] | `aq workspace *` projections |

Everything else in this document — CLI inventory, envelope, `--brief`, `aq schema`,
`src/prime/`, token store and scope middleware, MCP task scope, injection retargeting,
backward compatibility, measurement — is owned here. Implementation details:
[[../implementation/aq-surface]].
