---
title: Platforms Implementation (Phase 1 — rename + new platforms)
date: 2026-04-25
status: draft
tags: [spec, platforms, adapters, refactor]
---

# Platforms Implementation (Phase 1)

## Scope

This spec covers **only** the platform abstraction and three platform implementations. Profile schema changes, validation, Discord surfacing, and the strict task-creation gate are deferred to a separate spec: [`2026-04-25-profile-validation-design.md`](./2026-04-25-profile-validation-design.md).

After this phase ships, the three platforms exist and are testable, but platform selection is driven by a single config option (`default_platform`) rather than by the profile. Phase 2 replaces that config option with profile-driven dispatch and adds the validation/UX layer.

## Problem

The current adapter layer (`src/adapters/`) supports only Claude Code via the Agent SDK. The `AdapterFactory` accepts an `agent_type` string but every call site hardcodes `"claude"`. The `AgentAdapter` ABC is structurally fine — it's the rename + multi-impl that's missing.

This phase:

- Renames `src/adapters/` to `src/platforms/`. The ABC becomes `Platform`.
- Adds a typed `Capability` enum (used at runtime by phase 2; declared here so platform impls can populate `capabilities` correctly from the start).
- Adds two new in-tree platform implementations: `ClaudeCLIPlatform` (subprocess wrapper around `claude -p --output-format stream-json`) and `CodexCLIPlatform` (subprocess wrapper around the Codex CLI).
- Renames the existing Claude SDK adapter to `ClaudeSDKPlatform`.
- Replaces hardcoded `"claude"` at orchestrator call sites with a config-driven default (`config.default_platform`, defaults to `"claude_sdk"`).

## Out of scope (deferred to phase 2)

- Profile schema changes (`platform`/`role`/`rules`/`reflection`/`status` columns).
- The `## Platform` markdown section.
- Capability audit at profile sync time.
- `filter_for_platform()` runtime field stripping.
- Discord surfacing of profile load issues.
- Task creation gate (NOT NULL `profile_id`, name resolution, status checks).
- Migration deleting NULL `profile_id` tasks.
- Removing the `config.default_platform` stub introduced here.

## §1 — Platform abstraction

### Rename and relocate

`src/adapters/` → `src/platforms/`. The `AgentAdapter` ABC becomes `Platform`. The `AdapterFactory` becomes `PlatformRegistry`. All references updated, including:

- Imports of `src.adapters.*` → `src.platforms.*`
- `docs/specs/adapters/*` → `docs/specs/platforms/*`
- `AdapterFactory` references in tests, supervisor wiring, config

### `Capability` enum

```python
# src/platforms/base.py
from enum import StrEnum

class Capability(StrEnum):
    MCP = "mcp"
    PLAN_MODE = "plan_mode"
    RESUME = "resume"
    STREAMING_JSON = "streaming_json"
    HOOKS = "hooks"
    SKILLS = "skills"
    MEMORY_MD = "memory_md"
    THINKING = "thinking"
    PERMISSION_CALLBACKS = "permission_callbacks"
```

The enum is closed — adding a capability requires editing this file. In phase 1 the enum is **declared and populated on each platform**, but it's not yet consumed (no audit, no filtering). Phase 2 wires up the consumers.

Declaring it here ensures each platform impl gets its capability set right from the start, so phase 2 doesn't have to retrofit.

### `Platform` ABC

```python
# src/platforms/base.py
class Platform(ABC):
    name: ClassVar[str]                          # e.g. "claude_sdk"
    capabilities: ClassVar[frozenset[Capability]]

    @abstractmethod
    async def start(self, task: TaskContext) -> None: ...

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def is_alive(self) -> bool: ...
```

Contract from `src/adapters/base.py` is preserved verbatim — `start(task)` is unchanged. The only structural additions are the two `ClassVar`s (`name`, `capabilities`). Profile binding stays at construction (each platform's `__init__` takes `profile: AgentProfile`), exactly like today's `ClaudeAdapter`.

## §2 — Three implementations

### `ClaudeSDKPlatform` (`src/platforms/claude_sdk.py`)

The current `src/adapters/claude.py`, renamed:

```python
class ClaudeSDKPlatform(Platform):
    name = "claude_sdk"
    capabilities = frozenset(Capability)  # SDK supports everything
```

No behavioral change — same SDK invocation, same output mapping, same error classification. The class name and `name`/`capabilities` attributes are added; the rest is moved verbatim.

### `ClaudeCLIPlatform` (`src/platforms/claude_cli.py`) — new

Wraps `claude -p --output-format stream-json --verbose [--permission-mode {auto|plan|acceptEdits|...}]` via `asyncio.create_subprocess_exec`. NDJSON parsing of stdout. Stderr captured for error attribution.

```python
class ClaudeCLIPlatform(Platform):
    name = "claude_cli"
    capabilities = frozenset(Capability)  # Same surface as the SDK in v1
```

The CLI and SDK share a runtime; capabilities are identical in v1. The distinction exists so we can later add interactive-only caps (e.g., `LIVE_TAKEOVER` paired with a tmux observer) that only `ClaudeCLI` will gain.

Key behaviors:

- **Prompt delivery** — the assembled prompt (description + acceptance criteria + test commands + attached context) is passed via `--print "<prompt>"`. For very long prompts, fall back to stdin (the CLI accepts `-p -` to read from stdin).
- **Streaming output** — read NDJSON lines from stdout. Map each line type (`assistant`, `tool_use`, `tool_result`, `result`) to the existing `on_message` callback as one-line markdown summaries, mirroring `ClaudeSDKPlatform`'s formatting.
- **Token accounting** — sum `input_tokens` + `output_tokens` from the final `result` event, populate `AgentOutput.tokens_used`.
- **Cancellation** — `stop()` sets `_cancel_event`; `wait()` checks on every line read; on cancel, send SIGTERM, wait briefly, then SIGKILL.
- **Environment isolation** — strip `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` from the subprocess env (mirrors `ClaudeSDKPlatform`).
- **Working directory** — `task.checkout_path`.
- **Error classification** — exit code + stderr scan for known patterns: rate-limit messages → `PAUSED_RATE_LIMIT`, quota → `PAUSED_TOKENS`, otherwise `FAILED`.

### `CodexCLIPlatform` (`src/platforms/codex_cli.py`) — new

Wraps the Codex CLI's streaming JSON mode via subprocess. Final command form determined during implementation (Codex's CLI flags evolve); the platform is structured so the invocation is one helper, the streaming loop is shared with `ClaudeCLIPlatform`.

```python
class CodexCLIPlatform(Platform):
    name = "codex_cli"
    capabilities = frozenset({
        Capability.STREAMING_JSON,
        Capability.RESUME,
        Capability.THINKING,
        # Final set determined when implementing — declared at module top
    })
```

Capability set is provisional. Final values determined when implementing this platform; the implementation plan for that work captures them precisely.

Same behaviors as `ClaudeCLIPlatform` (subprocess, NDJSON, cancellation, env isolation, working directory) — implementation reuses the shared helpers.

### `_subprocess.py` (`src/platforms/_subprocess.py`) — new

Shared helpers used by both CLI platforms:

- `async def run_streaming_subprocess(cmd, env, cwd, on_line, cancel_event)` — runs the subprocess, streams stdout lines through `on_line`, handles SIGTERM/SIGKILL on cancel.
- `def parse_ndjson_line(line: bytes) -> dict | None` — robust JSON parsing with logging for malformed lines.
- `def isolated_env(extra: dict[str, str] | None = None) -> dict[str, str]` — builds an env dict stripped of `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, etc.

This module is the first place to look when a third CLI platform (Aider, Gemini CLI, Cline) gets added — most of the work is configuration of the helper, not new infrastructure.

## §3 — `PlatformRegistry`

```python
# src/platforms/__init__.py
class PlatformRegistry:
    """Looks up Platform classes by name. Single source of truth for which
    platforms are available to the running daemon."""

    def __init__(self):
        self._platforms: dict[str, type[Platform]] = {
            "claude_sdk": ClaudeSDKPlatform,
            "claude_cli": ClaudeCLIPlatform,
            "codex_cli": CodexCLIPlatform,
        }

    def get(self, name: str) -> type[Platform] | None:
        return self._platforms.get(name)

    def names(self) -> list[str]:
        return list(self._platforms.keys())

    def create(self, name: str, profile: AgentProfile, llm_logger=None) -> Platform:
        cls = self._platforms.get(name)
        if cls is None:
            raise ValueError(f"Unknown platform: {name}. Available: {self.names()}")
        return cls(profile=profile, llm_logger=llm_logger)
```

The current `AdapterFactory._config_for_profile()` Claude-specific merging is moved into each platform's `__init__` — each platform takes the full `AgentProfile` and pulls the fields it cares about (model, permission_mode, allowed_tools).

Plugin-as-platform support is a future swap of this dict for a plugin-discovery hook (`PluginContext.register_platform(name, cls)`) — out of scope here.

## §4 — Configuration

Add to `AppConfig` (`src/config.py`):

```python
@dataclass
class AppConfig:
    ...
    default_platform: str = "claude_sdk"  # phase-1 stub; replaced by profile.platform in phase 2
```

Validation: `default_platform` must be a valid name in `PlatformRegistry.names()`. Validated at daemon startup; bad value → fail-fast with a clear error. Loaded from `~/.agent-queue/config.yaml` like other config.

This is **explicitly a temporary mechanism**. Phase 2 deletes this field and switches call sites to `profile.platform`. The phase-2 spec includes the deletion as a migration step.

Why introduce it at all: without it, the new platforms are unreachable code in phase 1 (every task would still run on `claude_sdk`). The config option lets operators flip an entire deployment to `claude_cli` or `codex_cli` for testing. Phase 1 is verifiable.

## §5 — Call site updates

Two call sites change from hardcoded `"claude"` to `config.default_platform`:

- `src/orchestrator/execution.py:411` — `self._platforms.create(self._config.default_platform, profile=profile)` (was `self._adapter_factory.create("claude", profile=profile)`)
- `src/orchestrator/sync_workflow.py:273` — same change

The orchestrator's silent fallback path (`if profile is None: use platform defaults`) is **not** removed in this phase — that's a phase-2 change that pairs with the strict task-creation gate. Phase 1 keeps the existing fallback so behavior changes are minimized.

## §6 — Components and files

### New files
- `src/platforms/__init__.py` — `PlatformRegistry`
- `src/platforms/base.py` — `Platform` ABC, `Capability` enum, `MessageCallback` (re-exported)
- `src/platforms/claude_sdk.py` — moved from `src/adapters/claude.py`
- `src/platforms/claude_cli.py` — new
- `src/platforms/codex_cli.py` — new
- `src/platforms/_subprocess.py` — shared subprocess + NDJSON helpers
- `tests/test_platforms_claude_sdk.py` — moved/adapted from `tests/test_claude_adapter.py`
- `tests/test_platforms_claude_cli.py` — new
- `tests/test_platforms_codex_cli.py` — new
- `tests/test_platforms_subprocess.py` — new

### Modified files
- `src/orchestrator/execution.py:411` — call site change
- `src/orchestrator/sync_workflow.py:273` — call site change
- `src/supervisor.py` — wires `PlatformRegistry` instead of `AdapterFactory`
- `src/main.py` — same
- `src/config.py` — add `default_platform` field with validation
- `docs/specs/adapters/*` → `docs/specs/platforms/*` — relocate adapter spec docs
- Any file importing `src.adapters.*` — update import paths

### Deleted
- `src/adapters/` directory (replaced by `src/platforms/`)

### No DB changes in phase 1.

## §7 — Testing strategy

### Unit tests per platform impl

- `tests/test_platforms_claude_sdk.py` — content moved from `tests/test_claude_adapter.py`. Asserts contract preservation (start/wait/stop/is_alive, cancellation, error classification).
- `tests/test_platforms_claude_cli.py` — same matrix as ClaudeSDK, but mocks the subprocess (use `pytest`'s `monkeypatch` on `asyncio.create_subprocess_exec`). Tests:
  - happy path: NDJSON stream → `AgentOutput.COMPLETED` with summary + token count
  - rate-limit stderr → `PAUSED_RATE_LIMIT`
  - quota stderr → `PAUSED_TOKENS`
  - cancellation mid-stream → `FAILED` with "Cancelled" summary
  - subprocess crash (non-zero exit, no NDJSON) → `FAILED`
  - malformed NDJSON line → logged, stream continues
- `tests/test_platforms_codex_cli.py` — same matrix.

### Subprocess helper tests

`tests/test_platforms_subprocess.py` — unit tests for `run_streaming_subprocess`, `parse_ndjson_line`, `isolated_env`. No Claude/Codex specifics.

### Integration

A live integration test for `ClaudeCLIPlatform` is **not** required in this phase (depends on the local Claude CLI being installed and authenticated). Document the manual test flow: `default_platform: claude_cli` in config, create a task, observe behavior.

### Regression

Existing `tests/test_orchestrator*.py` and `tests/test_supervisor*.py` continue to pass — they don't care about platform internals, just that the registry returns something that satisfies the ABC.

## §8 — Risks

- **Subprocess fragility on the CLI platforms.** Pipe handling, signal forwarding, env-variable leakage are all classic sources of flake. *Mitigation:* the shared `_subprocess.py` is unit-tested in isolation; both CLI platforms use it; bugs are fixed in one place.
- **CodexCLI's CLI surface may change underneath us.** *Mitigation:* the platform is one module, the invocation is one function; if Codex changes its flags, we change the helper. Capability declarations are the contract with the rest of the system, not the CLI flags.
- **`default_platform` config drift.** Operators set `claude_cli`, hit a CLI bug, want to fall back to SDK. *Mitigation:* the config is hot-reload-friendly (validated at startup; restart picks up changes). Phase 2 removes this field entirely, replaced by profile-driven selection.
- **Test coverage gap on live CLI invocations.** Without integration tests, we may ship CLI platforms that "pass unit tests but never actually work." *Mitigation:* the manual test flow is documented and run before merge; integration test infrastructure can be added later when Claude CLI / Codex CLI auth is automated in CI.

## §9 — Future work (handed to phase 2)

- Profile-driven platform selection (`profile.platform` replaces `config.default_platform`).
- Capability audit at profile sync time.
- `filter_for_platform()` runtime field stripping.
- Discord surfacing of profile load issues.
- Strict task-creation gate (NOT NULL `profile_id`, name resolution, status checks).
- Removal of `config.default_platform` field.
- Migration deleting NULL `profile_id` tasks.
