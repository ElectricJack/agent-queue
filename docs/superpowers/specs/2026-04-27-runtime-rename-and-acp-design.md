---
title: Runtime Rename + ACP Adoption (Combined Design)
date: 2026-04-27
status: draft
tags: [spec, runtimes, platforms, acp, refactor]
---

# Runtime Rename + ACP Adoption — Combined Design

## Context

Mid-refactor on phase 1 of the platforms work (branch `platforms-impl`, 13 commits unmerged — the implementation is documented in [`2026-04-25-platforms-implementation-design.md`](./2026-04-25-platforms-implementation-design.md)). Two related decisions consolidated into a single direction:

1. **Rename `Platform` → `Runtime`** — the user chose Runtime as the standard term (matches OpenHands, AutoGen v0.4). Also clean up the misnamed `agent_type` DB column (which actually stores runtime values like `"claude"`/`"codex"`, not roles).

2. **Adopt ACP via ACPX** — introduce a new `ACPXRuntime` that fans out to any ACP-compatible coding agent (Claude, Codex, Gemini, OpenCode, Cursor, GitHub Copilot, Factory Droid, iFlow, Kilocode, Kimi, Kiro, Qoder, Qwen, Trae — 16 agents in ACPX's built-in registry). This replaces the hand-written `ClaudeCLIRuntime` and `CodexCLIRuntime` over time.

3. **Keep `ClaudeSDKRuntime`** as an escape hatch for the foreseeable future. The direct SDK code path has features ACP doesn't expose cleanly (e.g., `_resilient_query`'s reach into `claude_agent_sdk._internal` to survive `MessageParseError`, direct subprocess control for cancellation, bespoke error classification driving retry/pause logic).

This is **not either/or** — it's a sequence. The rename lands first (it's mostly mechanical and independent), then ACP gets layered in alongside the kept SDK runtime.

## Final state (after both phases ship)

`src/runtimes/`:
- `base.py` — `Runtime` ABC, `Capability` enum, `MessageCallback`
- `__init__.py` — `RuntimeRegistry`, `default_registry()` listing the active runtimes
- `_subprocess.py` — shared subprocess + NDJSON helpers (unchanged)
- `claude_sdk.py` — **kept** (renamed `ClaudeSDKPlatform` → `ClaudeSDKRuntime`)
- `acpx.py` — **new** (single Runtime impl, fans out to all ACP agents)

**Deleted:**
- `claude_cli.py` (replaced by ACPX with `agent="claude"`)
- `codex_cli.py` (replaced by ACPX with `agent="codex"`)

**Profile shape:**
- `AgentProfile.runtime: str` — `"claude_sdk"` or `"acpx"` (the registry key)
- `AgentProfile.agent_name: str` — only meaningful when runtime is `"acpx"`. Values: `"claude"`, `"codex"`, `"gemini"`, etc. (the ACP agent identifier)
- For `claude_sdk` profiles, `agent_name` is unused / empty

`AgentProfile.runtime` defaults to `"acpx"` for new agents; `claude_sdk` is the opt-in for cases needing SDK-internals access.

## Vocabulary (final)

| Concept | Final Name | Notes |
|---|---|---|
| Execution layer ABC | `Runtime` | Renamed from `Platform` |
| Direct SDK runtime | `ClaudeSDKRuntime` | Kept; renamed from `ClaudeSDKPlatform` |
| ACP-backed runtime | `ACPXRuntime` | Single Runtime that drives `acpx`, fans out to any ACP agent |
| Capability enum | `Capability` | Kept; ACP capabilities are negotiated, mapped to our enum at runtime |
| Profile bundle | `AgentProfile` | Kept |
| Selected runtime field | `AgentProfile.runtime: str` | Default `"acpx"` |
| Selected ACP agent | `AgentProfile.agent_name: str` | Used only with `runtime="acpx"` |
| DB column (formerly `agent_type`) | `runtime: str` | Renamed; values: `"claude_sdk"`, `"acpx"`, etc. |
| Vault dir `agent-types/` | unchanged | Holds profile/role definitions; renaming is separate scope |

## Sequencing

### Phase 1 (current branch — being shelved temporarily)
Already on `platforms-impl` (13 commits). The Platform/Runtime distinction is an internal naming detail — the work landed correctly.

### Phase 1.5 — Rename pass (Plan A)
On top of the existing 13 commits OR as a fresh branch off main:
- `src/platforms/` → `src/runtimes/`
- `Platform` → `Runtime`, `PlatformRegistry` → `RuntimeRegistry`, etc.
- `default_platform` config → `default_runtime`
- DB column `agent_type` → `runtime`; data migration normalizes values (`"claude"` → `"claude_sdk"`, etc.)
- ~30-40 file moves, ~50-60 file edits, 1 Alembic migration
- Estimated: 2-3 hours subagent work

After Phase 1.5: existing two CLI runtimes (`ClaudeCLIRuntime`, `CodexCLIRuntime`) still present but renamed.

### Phase 1.6 — ACP introduction (Plan B)
Layer ACPX in:
- Add `src/runtimes/acpx.py`
- Add `agent_name` field to `AgentProfile`
- Update `RuntimeRegistry.default_registry()` to include `ACPXRuntime`
- Tests: mock-based unit tests for ACPXRuntime mirroring the pattern from CLI runtimes
- Estimated: 1 day implementation + 1 day spike to validate fidelity

**Spike concerns to resolve before deletion of CLI runtimes:**
1. Token usage in ACP final response — does it match what we currently extract from `result.usage`?
2. Error event granularity — can `_classify_error_result()` style logic still drive retry/pause? (Probably need to layer our classifier over ACP's `stopReason` + error text.)
3. `--cd <worktree>` flag respected by ACPX/agent
4. `exec` mode actually exits cleanly after `stopReason`
5. Tool filtering — can we restrict the agent to a subset (Read/Write/Bash) for security profiles?

### Phase 1.7 — Retire CLI runtimes (Plan C)
Once ACPXRuntime is validated:
- Delete `src/runtimes/claude_cli.py` (~250 lines)
- Delete `src/runtimes/codex_cli.py` (~300 lines)
- Delete `tests/test_runtimes_claude_cli.py` (~200 lines)
- Delete `tests/test_runtimes_codex_cli.py` (~250 lines)
- Update default registry to drop them
- Migrate any profiles using `runtime="claude_cli"` or `runtime="codex_cli"` to `runtime="acpx"` with appropriate `agent_name`
- Net: ~1000 lines removed, but ACPX agent surface goes from 2 → 14+ (Claude + Codex via ACP, plus Gemini, OpenCode, Cursor, Copilot, Droid, iFlow, Kilocode, Kimi, Kiro, Qoder, Qwen, Trae)

### Phase 2 (separate spec — already exists)
The profile validation work at [`2026-04-25-profile-validation-design.md`](./2026-04-25-profile-validation-design.md). Independent of the rename + ACP adoption. Can land before or after.

## Architecture for ACPXRuntime

```
Orchestrator
  └─ RuntimeRegistry
       ├─ ClaudeSDKRuntime — direct SDK invocation (escape hatch)
       └─ ACPXRuntime — wraps acpx subprocess
            └─ subprocess: acpx --format json --approve-all <agent_name> exec '<prompt>'
                 └─ ACP server (claude-acp / codex-acp / gemini-acp / etc.)
                      └─ Underlying CLI (claude / codex / gemini / ...)
```

`ACPXRuntime.wait()` shape (sketch):

```python
async def wait(self, on_message=None):
    cmd = [
        "acpx", "--format", "json", "--approve-all",
        self._profile.agent_name,  # claude / codex / gemini / opencode / cursor / ...
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

Event dispatch routes ACP event types to the same `on_message` callback the existing runtimes use (so Discord live-output integration is unchanged).

ACP event schema (from upstream):
- `session/update` — message chunks (agent text, thinking), plan updates, mode changes
- `tool_call` — agent invoking a tool — emit `-# {tool_name}` to Discord
- `tool_result` — tool finished — log only
- Final `session/prompt` response — has `stopReason` (`completed` / `cancelled` / `failed`) + result text + usage

Permission auto-mode is set globally via `--approve-all`. Per-task tool restriction (if needed) handled via the underlying agent's flags or ACP capability set_config.

## ACPX in headless / orchestrator mode

The ACP protocol was designed editor-first, but ACPX exposes a CLI facade explicitly for the headless case:

| Orchestrator need | ACPX mechanism |
|---|---|
| Submit a task | `acpx --format json --approve-all <agent> exec '<prompt>'` |
| Stream output (Discord live updates) | NDJSON `session/update` events on stdout |
| Track tool use | `tool_call` events |
| Capture final result | Last response event with `stopReason` + result text |
| Capture token usage | Should be in the final response (verify in spike) |
| Cancel on timeout/user request | SIGTERM via existing `_subprocess.py` |
| Filter what tools the agent can use | Per-agent flags; or ACP capability set_config |
| Set working directory | `--cd <path>` |

Permission auto-modes:
- `--approve-all` — auto-approve every tool call (default for orchestrator tasks)
- `--approve-reads` — read tools auto-approved, writes need approval (ACPX default)
- `--deny-all` — read-only mode (good for explain-only tasks)
- `--non-interactive-permissions fail` — fail the run instead of asking

Cancellation:
- `acpx <session> cancel` — cooperative `session/cancel`
- `kill -TERM <pid>` — force termination
- ACPX's own behavior: Ctrl+C sends `session/cancel`, waits briefly for `stopReason=cancelled`, then force-kills

This is the SIGTERM-then-SIGKILL pattern we already implement in `_subprocess.py`. The interface maps cleanly onto our existing CLI runtime patterns.

**Validating precedent:** Jockey (open-source multi-agent orchestrator, Tauri + Rust + SolidJS) does what agent-queue is doing — coordinates Claude Code, Gemini CLI, Codex CLI through ACP. Our use case has been built before. Discord/Slack/Telegram bots in the ACP clients listing also use this pattern.

## Tradeoffs accepted

By keeping `ClaudeSDKRuntime` alongside ACPX:
- ✅ No loss of SDK-internals access (the `_resilient_query` workaround stays alive for the SDK path)
- ✅ Bespoke error classification keeps full fidelity for the SDK path
- ✅ Direct subprocess control for SDK runtime
- ❌ Two code paths to maintain (one direct-SDK, one via ACPX)
- ❌ Operators must understand which runtime to pick

By introducing `ACPXRuntime`:
- ✅ Instant 14+ agent surface via ACP registry
- ✅ Future-proof against new coding agents
- ✅ One dispatcher handles all ACP-compatible agents
- ❌ Alpha-status dependency (ACPX) requires version pinning + active maintenance attention
- ❌ Error classification fidelity depends on ACP — may need a thin layer
- ❌ Cooperative-only cancellation through ACP

## Out of scope (future work)

- **Make Supervisor a profile-driven first-class agent** — separate spec; will brainstorm separately. The supervisor's chat loop is structurally different from worker agents (multi-turn conversation vs single-task `start → wait`), so it likely won't share the Runtime dispatch path even after this work, but it CAN share the AgentProfile dataclass.

- **Provider as a first-class concept** — today implicit (provider derived from runtime + agent_name). May become useful for cross-agent rate limiting and quota pools (e.g., "Anthropic-wide token budget" vs "OpenAI-wide"), but not blocking.

- **Vault `agent-types/` directory rename** — currently holds profile definitions named after roles (`supervisor`, `claude-opus`, etc.). Renaming to `profiles/` or `roles/` is bigger blast radius; defer.

- **Splitting `agent_type` into separate `runtime` + `provider` + `role` columns** — the column rename in Phase 1.5 unifies the value into "runtime" semantically; further splits are future work if needed.

- **Phase 2 profile validation** — separate existing spec at [`2026-04-25-profile-validation-design.md`](./2026-04-25-profile-validation-design.md). Independent.

## Critical files (combined across phases)

### Phase 1.5 (rename) — Critical files
- `src/runtimes/__init__.py`, `base.py`, `claude_sdk.py`, `claude_cli.py`, `codex_cli.py`, `_subprocess.py` (moved + renamed)
- `src/orchestrator/core.py`, `execution.py`, `sync_workflow.py`
- `src/main.py`
- `src/config.py`
- `src/models.py` (Agent.runtime, Task.runtime, Project.default_runtime)
- `src/database/tables.py` (column renames)
- `src/orchestrator/context.py` (override file lookup)
- All `tests/test_runtimes_*.py` (renamed)
- All test files referencing `agent_type=` field
- `tests/conftest.py`
- `alembic/versions/<rev>_runtime_column_rename.py` — new revision
- Docs: `CLAUDE.md`, `docs/specs/runtimes/`, `docs/guides/runtime-development*.md`

### Phase 1.6 (ACP intro) — Critical files
- `src/runtimes/acpx.py` — new
- `src/runtimes/__init__.py` — register ACPXRuntime
- `src/runtimes/_acp_events.py` — event classification helpers (likely needed)
- `src/models.py` — add `agent_name: str` to AgentProfile
- `src/database/tables.py` — add `agent_name` column to agent_profiles
- `alembic/versions/<rev>_acpx_runtime.py` — schema migration
- `tests/test_runtimes_acpx.py` — new
- `docs/specs/runtimes/acpx.md` — new

### Phase 1.7 (CLI runtime retirement) — Critical files
- Delete `src/runtimes/claude_cli.py`, `codex_cli.py`
- Delete corresponding test files
- Update `src/runtimes/__init__.py` (drop registrations)
- Migrate any profiles still pointing at the deleted runtimes (data migration)

## Verification

After each phase:
1. `pytest tests/ -n auto --ignore=tests/chat_eval` — failure count matches baseline (currently 11)
2. `ruff check src/ tests/` — clean
3. Smoke test: `python3 -c "from src.runtimes import default_registry; print(sorted(default_registry().names()))"`
   - After 1.5: `['claude_cli', 'claude_sdk', 'codex_cli']`
   - After 1.6: `['acpx', 'claude_cli', 'claude_sdk', 'codex_cli']`
   - After 1.7: `['acpx', 'claude_sdk']`
4. Live integration: route a real Claude task through both `claude_sdk` and `acpx` (with `agent_name="claude"`) and compare behavior
5. Alembic migrations roundtrip cleanly on SQLite + PostgreSQL

## Status

User is shelving this for now to switch branches. This document captures the consolidated direction for resumption later. Phase 1 branch (`platforms-impl`) remains paused with 13 commits unmerged.

## Sources

- [Zed — Agent Client Protocol](https://zed.dev/acp)
- [ACP Protocol Overview](https://agentclientprotocol.com/protocol/overview)
- [ACP clients listing](https://agentclientprotocol.com/get-started/clients)
- [openclaw/acpx GitHub](https://github.com/openclaw/acpx)
- [ACPX Inside Claude Code: Practical Multi-Agent Orchestration](https://casys.ai/blog/acpx-multi-agent-orchestration)
- [ACP Registry — Zed's Blog](https://zed.dev/blog/acp-registry)
- [JetBrains ACP](https://www.jetbrains.com/acp/)
- [Building an AI Agent Mesh with Gemini 3, OpenClaw, and ACPX (Medium)](https://timtech4u.medium.com/building-an-ai-agent-mesh-with-gemini-3-openclaw-and-acpx-7b6ab5f1cbf4)
