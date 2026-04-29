---
title: Runtime Rename + ACP Adoption (Combined Design)
date: 2026-04-27
status: partially-landed (foundations on `Platform` shipped; rename to `Runtime` and ACP adoption still pending)
tags: [spec, runtimes, platforms, acp, refactor]
---

# Runtime Rename + ACP Adoption — Combined Design

## Status update (2026-04-28)

This spec consolidated three threads of work. State today:

1. **Platform → Runtime rename — STILL PENDING (target naming).**
   `Runtime` is the chosen term — matches OpenClaw / OpenHands /
   AutoGen v0.4, and reflects the right semantic distinction:
   companies like Anthropic / OpenAI are technically *platforms*; what
   the orchestrator dispatches into is an *agent runtime*. The
   supervisor-as-runtime port landed on the current `Platform` naming
   because that's what was on disk at the time, but it does not change
   the target. Phase 1.5 below now folds the new artefacts (e.g. the
   `AgentProfile.platform` field added by the supervisor port → must
   rename to `AgentProfile.runtime`) into the rename pass.

2. **Supervisor as a first-class runtime — LANDED.** Originally listed
   under "Out of scope (future work)" with the comment that the
   supervisor's chat loop "likely won't share the Runtime dispatch
   path." Reality went the other way: the in-process `Supervisor`
   itself implements the Platform/Runtime contract and is registered as
   a daemon-wide singleton. Profiles with `platform: supervisor`
   (→ `runtime: supervisor` after the rename) execute in-process via
   `Supervisor.start(task) → wait() → stop()` on the shared instance,
   with per-task state isolated via ContextVars. No separate wrapper
   class.

3. **ACPX adoption — STILL PENDING.** Detailed below; nothing built
   yet. Will land after the rename so the new module is `ACPXRuntime`
   in `src/runtimes/`, and the field key is `runtime: "acpx"`.

## Current state (after platforms-impl + supervisor-as-runtime port land — pre-rename)

`src/platforms/`:
- `base.py` — `Platform` ABC, `Capability` enum, `MessageCallback`,
  `requires_workspace: ClassVar[bool] = True`
- `__init__.py` — `PlatformRegistry` (with `singletons` dict for
  daemon-wide instances), `default_registry(supervisor=...)`
- `_subprocess.py` — shared subprocess + NDJSON helpers
- `claude_sdk.py` — `ClaudeSDKPlatform` (direct SDK invocation)
- `claude_cli.py` — `ClaudeCLIPlatform` (`claude -p --output-format
  stream-json`)
- `codex_cli.py` — `CodexCLIPlatform` (`codex exec --json`)
- `supervisor.py` — `Supervisor` (chat brain AND a Platform; tool-call
  only, `requires_workspace = False`, registered as singleton)

**Profile shape (current — pre-rename):**
- `AgentProfile.platform: str = "claude_sdk"` — registry key. Values:
  `"claude_sdk"` (default; matches `config.default_platform`),
  `"claude_cli"`, `"codex_cli"`, `"supervisor"`.
- `AgentProfile.allowed_tools: list[str]` — bounds tool surface for
  every runtime that honours it (Supervisor and the SDK adapter both
  do; CLIs respect their flags).

There is no `agent_name` field yet — that comes with ACPX.

## Final state (after Phase 1.5 rename + Phase 1.6 ACPX + Phase 1.7 retirement)

`src/runtimes/`:
- `base.py` — `Runtime` ABC, `Capability` enum, `MessageCallback`,
  `requires_workspace`
- `__init__.py` — `RuntimeRegistry` (with `singletons` dict),
  `default_registry(supervisor=...)`
- `_subprocess.py` — unchanged shared helpers
- `claude_sdk.py` — `ClaudeSDKRuntime` (escape hatch; kept indefinitely)
- `acpx.py` — `ACPXRuntime` (single Runtime, fans out to any ACP agent)
- `supervisor.py` — `Supervisor` (in-process Runtime; singleton)

**Deleted (Phase 1.7):**
- `claude_cli.py` — replaced by ACPX with `agent_name="claude"`
- `codex_cli.py` — replaced by ACPX with `agent_name="codex"`

**Profile shape (final):**
- `AgentProfile.runtime: str = "claude_sdk"` — registry key. Values:
  `"claude_sdk"`, `"acpx"`, `"supervisor"`.
- `AgentProfile.agent_name: str = ""` — only meaningful when
  `runtime == "acpx"`. Values: `"claude"`, `"codex"`, `"gemini"`, etc.
  (the ACP agent identifier).
- `AgentProfile.allowed_tools: list[str]` — unchanged.

`AgentProfile.runtime` defaults to `"claude_sdk"` for now (matches the
landed default); a future commit may flip the new-profile default to
`"acpx"` once that runtime is validated as feature-equivalent.

## Vocabulary

The target naming is the right column. The middle column is what
actually shipped on `platforms-impl` and will be mechanically renamed
in Phase 1.5.

| Concept | Current (interim) | Final |
|---|---|---|
| Execution layer ABC | `Platform` | `Runtime` |
| Class registry | `PlatformRegistry` | `RuntimeRegistry` |
| Direct SDK runtime | `ClaudeSDKPlatform` | `ClaudeSDKRuntime` |
| Claude CLI runtime | `ClaudeCLIPlatform` | (deleted in 1.7) |
| Codex CLI runtime | `CodexCLIPlatform` | (deleted in 1.7) |
| ACP-backed runtime | (not present yet) | `ACPXRuntime` |
| In-process runtime | `Supervisor` | `Supervisor` (kept; runtime AND chat brain) |
| Capability enum | `Capability` | `Capability` |
| Workspace gate ClassVar | `Platform.requires_workspace` | `Runtime.requires_workspace` |
| Profile bundle | `AgentProfile` | `AgentProfile` |
| Selected runtime field | `AgentProfile.platform: str` | `AgentProfile.runtime: str` |
| Selected ACP agent | (not present yet) | `AgentProfile.agent_name: str` |
| DB column | `agent_profiles.platform` | `agent_profiles.runtime` |
| Config default | `config.default_platform` | `config.default_runtime` |
| Module path | `src/platforms/` | `src/runtimes/` |
| Vault dir `agent-types/` | unchanged | unchanged (renaming is separate scope) |

## Sequencing

### Phase 1 — platforms refactor (LANDED)
15 commits on `platforms-impl`. Renamed `src/adapters/` →
`src/platforms/`, introduced `Platform` ABC, `PlatformRegistry`,
`default_registry()`, `config.default_platform`. Three subprocess
runtimes shipped (`claude_sdk`, `claude_cli`, `codex_cli`). The
chosen-but-not-yet-landed name "Runtime" was deferred to a follow-on
sweep so this phase could ship in isolation.

### Phase 1.4 — supervisor-as-runtime port (LANDED)
5 commits ported the supervisor-as-runtime branch onto platforms-impl
(see [`2026-04-27-supervisor-as-runtime.md`](../plans/2026-04-27-supervisor-as-runtime.md)).
Net effect:
- `src/supervisor.py` → `src/platforms/supervisor.py`
- `Supervisor` inherits `Platform`; registered as singleton in
  `default_registry(supervisor=...)`
- `AgentProfile.platform` field + DB column + idempotent migration
  (will be renamed to `runtime` in Phase 1.5)
- `TaskContext.profile` field (singleton runtimes read it inside
  `start(task)` since they can't carry profile in `__init__`)
- ContextVar concurrency: handler fields, `_last_messages`,
  `_last_tool_actions`, `_reflection_retry_active`,
  `LoggedChatProvider.caller_override`. `_llm_lock` removed entirely.
- Phase 2 playbook runner: `_execute_single_node` dispatches per-node
  based on `profile.platform`.
- Profile cache (mtime-keyed) on supervisor profile reads.
- Test coverage: `tests/test_supervisor_platform.py` (13 tests; will
  be renamed to `test_supervisor_runtime.py` in 1.5),
  `tests/test_playbook_runner.py::TestPlatformAwareNodeDispatch` (3
  tests; will be renamed in 1.5).

### Phase 1.5 — Rename pass (PENDING)
Sweep `Platform` → `Runtime` across the codebase. The pass is
mechanical but touches a lot of files because the previous two phases
both keyed off `Platform`/`platform` naming.

**Module + class renames:**
- `src/platforms/` → `src/runtimes/`
- `Platform` → `Runtime`, `PlatformRegistry` → `RuntimeRegistry`
- `ClaudeSDKPlatform` → `ClaudeSDKRuntime`
- `ClaudeCLIPlatform` → `ClaudeCLIRuntime` (will be deleted in 1.7)
- `CodexCLIPlatform` → `CodexCLIRuntime` (will be deleted in 1.7)
- `Supervisor.name = "supervisor"` is unchanged (registry key is the
  same word in both worlds)

**Field + config renames:**
- `AgentProfile.platform` → `AgentProfile.runtime`
- `agent_profiles.platform` column → `runtime` column (Alembic
  migration with idempotent rename; data migration is a no-op since
  values already match the runtime registry keys).
- `config.default_platform` → `config.default_runtime`
- `## Config` JSON key in profile markdown: `platform` → `runtime`
  (parser accepts both during a deprecation window if needed; otherwise
  one-shot rewrite of vault profile files via the existing sync layer).
- `Platform.requires_workspace` → `Runtime.requires_workspace`
- `parser.py` `VALID_PLATFORMS` → `VALID_RUNTIMES`

**Test renames:**
- `tests/test_platforms_base.py` → `tests/test_runtimes_base.py`
- `tests/test_supervisor_platform.py` → `tests/test_supervisor_runtime.py`
- `TestPlatformAwareNodeDispatch` → `TestRuntimeAwareNodeDispatch`
- `tests/test_platforms_*.py` → `tests/test_runtimes_*.py`
- Mock factory test sites referencing `platform=` kwarg → `runtime=`

**Doc renames:**
- `docs/specs/platforms/` → `docs/specs/runtimes/`
- `docs/guides/platform-development.md` → `docs/guides/runtime-development.md`
- `CLAUDE.md` Platforms quick-reference line + Profiles description
- `docs/specs/design/profiles.md` Config block + value list

**Migration:** one Alembic revision renaming the column. Following the
idempotent pattern established by the `platform` column migration (and
the supervisor-as-runtime fix at `93aef1d9`), the upgrade inspects the
schema first and skips when the rename has already happened — keeps
partial-state DBs from rebases working.

**Estimated scope:** ~30-40 file moves + ~50-60 file edits + 1 Alembic
migration. Fully mechanical. The work is gated on running the existing
test suite green before and after.

### Phase 1.6 — ACPX introduction (PENDING)
Layer ACPX in alongside existing runtimes:
- Add `src/runtimes/acpx.py` — `ACPXRuntime(Runtime)` with
  `name = "acpx"`, `requires_workspace = True`.
- Add `AgentProfile.agent_name: str = ""` + DB column + migration.
- `parser.py` — read `agent_name` from `## Config` (same block as
  `runtime`); validate non-empty when `runtime == "acpx"`.
- Update `default_registry()` to include `ACPXRuntime`.
- Update `VALID_RUNTIMES` to include `"acpx"`.
- Tests: mock-based unit tests mirroring the pattern from CLI runtimes.

**Spike concerns to resolve before deletion of CLI runtimes:**
1. Token usage in ACP final response — does it match what we extract
   from `result.usage` today?
2. Error event granularity — can `_classify_error_result()` style logic
   still drive retry/pause? (Probably need a thin classifier over ACP's
   `stopReason` + error text.)
3. `--cd <worktree>` flag respected by ACPX/agent.
4. `exec` mode actually exits cleanly after `stopReason`.
5. Tool filtering — can we restrict the agent to a subset
   (Read/Write/Bash) for security profiles?

### Phase 1.7 — Retire CLI runtimes (PENDING; gated on 1.6 validation)
Once `ACPXRuntime` is validated:
- Delete `src/runtimes/claude_cli.py` (~250 lines)
- Delete `src/runtimes/codex_cli.py` (~300 lines)
- Delete `tests/test_runtimes_claude_cli.py` (~200 lines)
- Delete `tests/test_runtimes_codex_cli.py` (~250 lines)
- Update `default_registry()` to drop them
- Update `VALID_RUNTIMES` to drop them
- Migrate any profiles using `runtime="claude_cli"` or `"codex_cli"`
  to `runtime="acpx"` with appropriate `agent_name`
- Net: ~1000 lines removed; ACPX agent surface goes from 2 → 14+
  (Claude + Codex via ACP, plus Gemini, OpenCode, Cursor, Copilot,
  Droid, iFlow, Kilocode, Kimi, Kiro, Qoder, Qwen, Trae)

### Phase 2 (separate spec — already exists)
Profile validation work at [`2026-04-25-profile-validation-design.md`](./2026-04-25-profile-validation-design.md).
Independent. Can land before or after ACPX.

## Architecture for ACPXRuntime

```
Orchestrator
  └─ RuntimeRegistry
       ├─ ClaudeSDKRuntime — direct SDK invocation (escape hatch)
       ├─ Supervisor (singleton) — in-process, tool-call-only
       └─ ACPXRuntime — wraps acpx subprocess
            └─ subprocess: acpx --format json --approve-all <agent_name> exec '<prompt>'
                 └─ ACP server (claude-acp / codex-acp / gemini-acp / etc.)
                      └─ Underlying CLI (claude / codex / gemini / ...)
```

`ACPXRuntime.wait()` shape (sketch):

```python
class ACPXRuntime(Runtime):
    name: ClassVar[str] = "acpx"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(Capability)
    requires_workspace: ClassVar[bool] = True

    def __init__(self, profile=None, llm_logger=None):
        self._profile = profile
        self._llm_logger = llm_logger
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()

    async def wait(self, on_message=None):
        cmd = [
            "acpx", "--format", "json", "--approve-all",
            self._profile.agent_name,  # claude / codex / gemini / ...
            "exec", self._build_prompt(),
        ]
        cmd += self._policy_flags()  # --cd worktree, tool filtering, etc.

        await run_streaming_subprocess(
            cmd=cmd,
            env=isolated_env(),
            cwd=self._task.checkout_path,
            on_line=self._on_line,
            cancel_event=self._cancel_event,
        )
        return self._build_output()
```

Event dispatch routes ACP event types to the same `on_message`
callback the existing runtimes use (so Discord live-output is
unchanged).

ACP event schema (from upstream):
- `session/update` — message chunks (agent text, thinking), plan
  updates, mode changes
- `tool_call` — agent invoking a tool — emit `-# {tool_name}` to Discord
- `tool_result` — tool finished — log only
- Final `session/prompt` response — has `stopReason`
  (`completed` / `cancelled` / `failed`) + result text + usage

Permission auto-mode is set globally via `--approve-all`. Per-task
tool restriction (if needed) handled via the underlying agent's flags
or ACP capability `set_config`.

## ACPX in headless / orchestrator mode

The ACP protocol was designed editor-first, but ACPX exposes a CLI
facade explicitly for the headless case:

| Orchestrator need | ACPX mechanism |
|---|---|
| Submit a task | `acpx --format json --approve-all <agent> exec '<prompt>'` |
| Stream output (Discord live updates) | NDJSON `session/update` events on stdout |
| Track tool use | `tool_call` events |
| Capture final result | Last response event with `stopReason` + result text |
| Capture token usage | Should be in the final response (verify in spike) |
| Cancel on timeout/user request | SIGTERM via existing `_subprocess.py` |
| Filter what tools the agent can use | Per-agent flags; or ACP capability `set_config` |
| Set working directory | `--cd <path>` |

Permission auto-modes:
- `--approve-all` — auto-approve every tool call (default for orchestrator tasks)
- `--approve-reads` — read tools auto-approved, writes need approval (ACPX default)
- `--deny-all` — read-only mode (good for explain-only tasks)
- `--non-interactive-permissions fail` — fail the run instead of asking

Cancellation:
- `acpx <session> cancel` — cooperative `session/cancel`
- `kill -TERM <pid>` — force termination
- ACPX's own behavior: Ctrl+C sends `session/cancel`, waits briefly
  for `stopReason=cancelled`, then force-kills

This is the SIGTERM-then-SIGKILL pattern we already implement in
`_subprocess.py`. The interface maps cleanly onto our existing CLI
runtime patterns.

**Validating precedent:** Jockey (open-source multi-agent orchestrator,
Tauri + Rust + SolidJS) does what agent-queue is doing — coordinates
Claude Code, Gemini CLI, Codex CLI through ACP. Our use case has been
built before. Discord/Slack/Telegram bots in the ACP clients listing
also use this pattern.

## Tradeoffs accepted

By keeping `ClaudeSDKRuntime` alongside ACPX (when ACPX lands):
- ✅ No loss of SDK-internals access (the `_resilient_query` workaround
  stays alive for the SDK path)
- ✅ Bespoke error classification keeps full fidelity for the SDK path
- ✅ Direct subprocess control for SDK runtime
- ❌ Two code paths to maintain (one direct-SDK, one via ACPX)
- ❌ Operators must understand which runtime to pick

By introducing `ACPXRuntime`:
- ✅ Instant 14+ agent surface via ACP registry
- ✅ Future-proof against new coding agents
- ✅ One dispatcher handles all ACP-compatible agents
- ❌ Alpha-status dependency (ACPX) requires version pinning + active
  maintenance attention
- ❌ Error classification fidelity depends on ACP — may need a thin
  layer
- ❌ Cooperative-only cancellation through ACP

## Out of scope (future work)

- **Provider as a first-class concept** — today implicit (provider
  derived from runtime + agent_name). May become useful for
  cross-agent rate limiting and quota pools (e.g., "Anthropic-wide
  token budget" vs "OpenAI-wide"), but not blocking. The Runtime/
  Provider distinction matches industry usage: Anthropic / OpenAI /
  Google are *platforms* (companies that publish models), the runtimes
  are how we *invoke* their agents — orthogonal axes worth keeping
  separate if/when this becomes useful.

- **Vault `agent-types/` directory rename** — currently holds profile
  definitions named after roles (`supervisor`, `claude-opus`, etc.).
  Renaming to `profiles/` or `roles/` is bigger blast radius; defer.

- **Splitting `agent_type` column on the tasks/agents tables** — the
  pre-existing `agent_type` columns on `tasks` and `agents` (separate
  from `agent_profiles.platform`/`runtime`) still hold legacy values.
  The supervisor-as-runtime port did not touch these. A future cleanup
  could split them into `runtime` + `role` if needed; not blocking.

- **Phase 2 profile validation** — separate existing spec at
  [`2026-04-25-profile-validation-design.md`](./2026-04-25-profile-validation-design.md).
  Independent.

## Critical files

### Phase 1.5 (rename) — Critical files
**Module moves (full directory):**
- `src/platforms/__init__.py` → `src/runtimes/__init__.py`
- `src/platforms/base.py` → `src/runtimes/base.py`
- `src/platforms/claude_sdk.py` → `src/runtimes/claude_sdk.py`
- `src/platforms/claude_cli.py` → `src/runtimes/claude_cli.py`
- `src/platforms/codex_cli.py` → `src/runtimes/codex_cli.py`
- `src/platforms/supervisor.py` → `src/runtimes/supervisor.py`
- `src/platforms/_subprocess.py` → `src/runtimes/_subprocess.py`

**Edits:**
- `src/orchestrator/core.py`, `execution.py`, `sync_workflow.py`
- `src/main.py` (`from src.platforms import …` → `from src.runtimes …`,
  `default_platform` → `default_runtime`, `_platforms` field →
  `_runtimes`)
- `src/config.py` (`default_platform` field, validation)
- `src/models.py` (`AgentProfile.platform` → `runtime`)
- `src/database/tables.py` (column rename)
- `src/profiles/parser.py` (`VALID_PLATFORMS` → `VALID_RUNTIMES`,
  Config-block key)
- `src/profiles/sync.py`, `migration.py` (field passthrough)
- `src/database/queries/profile_queries.py`
- `src/playbooks/runner.py` (`platforms=` constructor param,
  `_execute_node_via_platform` → `_execute_node_via_runtime`)
- All `tests/test_platforms_*.py` (renamed)
- `tests/test_supervisor_platform.py` → `test_supervisor_runtime.py`
- All test files referencing `platform=` profile field
- `tests/conftest.py`
- `migrations/versions/<rev>_runtime_column_rename.py` — new revision
  (idempotent)
- Vault profile rewrite: existing markdown files using
  `"platform": "supervisor"` → `"runtime": "supervisor"` (the parser
  could accept both during a deprecation window; cleaner is one-shot
  rewrite via sync layer)
- Docs: `CLAUDE.md`, `docs/specs/design/profiles.md`,
  `docs/specs/runtimes/` (rename folder), example template

### Phase 1.6 (ACP intro) — Critical files
- `src/runtimes/acpx.py` — new
- `src/runtimes/__init__.py` — register `ACPXRuntime`
- `src/runtimes/_acp_events.py` — event classification helpers (likely needed)
- `src/models.py` — add `agent_name: str` to `AgentProfile`
- `src/database/tables.py` — add `agent_name` column to `agent_profiles`
- `src/profiles/parser.py` — add `"acpx"` to `VALID_RUNTIMES`,
  validate `agent_name` non-empty when `runtime == "acpx"`
- `migrations/versions/<rev>_add_agent_name_to_agent_profiles.py` —
  schema migration (idempotent pattern matching the runtime column
  rename migration)
- `tests/test_runtimes_acpx.py` — new
- `docs/specs/runtimes/acpx.md` — new

### Phase 1.7 (CLI runtime retirement) — Critical files
- Delete `src/runtimes/claude_cli.py`, `codex_cli.py`
- Delete corresponding test files
- Update `src/runtimes/__init__.py` (drop registrations from
  `default_registry()`)
- Update `VALID_RUNTIMES` in `src/profiles/parser.py`
- Migrate any profiles still pointing at the deleted runtimes (data
  migration)

## Verification

After each phase:
1. `pytest tests/ -n auto --ignore=tests/chat_eval` — failure count
   matches baseline (currently 5 pre-existing failures from the
   `set_playbook_enabled` tool registry list inconsistency — unrelated
   to this work).
2. `ruff check src/ tests/` — clean.
3. Smoke test:
   ```bash
   python3 -c "from src.runtimes import default_registry; \
       print(sorted(default_registry().names()))"
   ```
   - Current state (pre-rename, `from src.platforms`):
     `['claude_cli', 'claude_sdk', 'codex_cli']` (returns `'supervisor'`
     too if `default_registry(supervisor=…)` is called with an instance)
   - After 1.5: same listing, just from `src.runtimes`
   - After 1.6: `['acpx', 'claude_cli', 'claude_sdk', 'codex_cli']`
   - After 1.7: `['acpx', 'claude_sdk']` (plus `'supervisor'` when
     instance is passed)
4. Live integration: route a real Claude task through both
   `claude_sdk` and `acpx` (with `agent_name="claude"`) and compare
   behavior.
5. Alembic migrations roundtrip cleanly on SQLite + PostgreSQL (already
   verified for the `platform` column; the rename and ACPX migrations
   follow the same idempotent pattern).

## Status

Phase 1 + Phase 1.4 (supervisor-as-runtime port) merged onto
`platforms-impl`. The Platform → Runtime rename pass (Phase 1.5), ACPX
adoption (Phase 1.6), and CLI retirement (Phase 1.7) remain pending —
this document is the blueprint for the work whenever it picks up.

## Sources

- [Zed — Agent Client Protocol](https://zed.dev/acp)
- [ACP Protocol Overview](https://agentclientprotocol.com/protocol/overview)
- [ACP clients listing](https://agentclientprotocol.com/get-started/clients)
- [openclaw/acpx GitHub](https://github.com/openclaw/acpx)
- [ACPX Inside Claude Code: Practical Multi-Agent Orchestration](https://casys.ai/blog/acpx-multi-agent-orchestration)
- [ACP Registry — Zed's Blog](https://zed.dev/blog/acp-registry)
- [JetBrains ACP](https://www.jetbrains.com/acp/)
- [Building an AI Agent Mesh with Gemini 3, OpenClaw, and ACPX (Medium)](https://timtech4u.medium.com/building-an-ai-agent-mesh-with-gemini-3-openclaw-and-acpx-7b6ab5f1cbf4)
