---
title: Platforms Refactor + Profile Validation
date: 2026-04-25
status: draft
tags: [spec, platforms, profiles, adapters, refactor]
---

# Platforms Refactor + Profile Validation

## Problem

Three intertwined gaps in the current adapter/profile system make tasks fail invisibly:

1. **Only Claude Code (via the Agent SDK) is supported.** The `AdapterFactory` accepts an `agent_type` string but every call site hardcodes `"claude"`. Adding ClaudeCLI or CodexCLI requires structural changes, not just a new module.
2. **Profile validation is silent.** Bad profiles can sit in the DB with no visible signal. Profile parse failures emit only to the event bus, and only when one is configured. No Discord notification.
3. **Tasks can be created with no profile, or with a broken one.** `tasks.profile_id` is nullable. The orchestrator falls back to platform defaults silently. Tasks then sit in DEFINED forever, or run with the wrong configuration. The user-visible symptom is "I made a task and it never started," with no error trail.

This spec describes a refactor that:

- Renames the adapter layer to "platforms" and adds two new in-tree platforms (ClaudeCLI, CodexCLI) alongside the renamed ClaudeSDK.
- Introduces a typed `Capability` enum so platform features are checkable at sync-time and filterable at runtime.
- Makes `tasks.profile_id` non-nullable. Bad profile names rejected at creation with a Discord error to the project channel. NULL-profile rows deleted in migration.
- Surfaces all profile load issues to the system Discord channel with green/yellow/red status transitions.

Out of scope (deliberate, deferred):

- Platforms-as-plugins. The `PlatformRegistry` is a single dict; swap for a plugin hook later. Designed-for, not built-now.
- Soft fallback / capability shimming (e.g., translating MCP server configs into a non-MCP platform's tool format).
- Per-task capability declarations. Tasks inherit entirely from their profile.

## Architecture overview

```
┌──────────────┐    profile_id    ┌──────────────────┐    platform name    ┌──────────────┐
│  Task row    │ ───────────────► │  AgentProfile    │ ──────────────────► │ Platform ABC │
│  (NOT NULL)  │     FK           │  (status, caps)  │                     │ (capabilities)│
└──────────────┘                  └──────────────────┘                     └──────────────┘
                                          │                                        │
                                          ▼                                        ▼
                              ┌──────────────────────┐               ┌──────────────────────┐
                              │ filter_for_platform()│               │ ClaudeSDKPlatform    │
                              │  strips fields the   │               │ ClaudeCLIPlatform    │
                              │  platform can't run  │               │ CodexCLIPlatform     │
                              └──────────────────────┘               └──────────────────────┘
```

Profile sync (vault → DB) runs a capability audit and tags status. Task creation requires a non-NULL, non-error profile. Runtime hands the platform a filtered profile so it never sees incompatible config.

## §1 — Platform abstraction

### Rename and relocate

Rename `src/adapters/` → `src/platforms/`. The `AgentAdapter` ABC becomes `Platform`. The `AdapterFactory` becomes `PlatformRegistry`. All references updated, including:

- `src/orchestrator/execution.py:411` — `self._adapter_factory.create("claude", profile=profile)` becomes `self._platforms.create(profile.platform, profile=profile)`
- `src/orchestrator/sync_workflow.py:273` — same change
- All imports of `src.adapters.*`
- `docs/specs/adapters/*` → `docs/specs/platforms/*`
- `AdapterFactory` references in tests, supervisor wiring, and config

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

The enum is closed — adding a capability requires editing this file. Each enum member maps to one or more profile config fields via the **rule table** (see §3).

### `Platform` ABC

```python
class Platform(ABC):
    name: ClassVar[str]                          # e.g. "claude_sdk"
    capabilities: ClassVar[frozenset[Capability]]

    @abstractmethod
    async def start(self, task: TaskContext, profile: AgentProfile) -> None: ...

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def is_alive(self) -> bool: ...
```

The contract from `src/adapters/base.py` is preserved — only the names change and `start()` now takes the profile alongside the task (today the profile is passed at construction; this surfaces it to the platform impl directly so capability-aware behavior is local).

### Three implementations

- **`src/platforms/claude_sdk.py`** — current `src/adapters/claude.py`, renamed. `name = "claude_sdk"`. `capabilities` = the full enum (the SDK supports everything).

- **`src/platforms/claude_cli.py`** — new. Wraps `claude -p --output-format stream-json --verbose [--permission-mode {auto|plan|...}]` via `asyncio.create_subprocess_exec`. NDJSON parsing. `name = "claude_cli"`. `capabilities` = full set minus any genuinely-interactive caps (none currently in the enum, so identical to ClaudeSDK in v1; the distinction exists so we can later add a `LIVE_TAKEOVER` cap that only ClaudeCLI gets when paired with a TUI observer).

- **`src/platforms/codex_cli.py`** — new. Wraps the `codex` CLI's streaming JSON mode via subprocess. `name = "codex_cli"`. `capabilities` = `{STREAMING_JSON, RESUME, THINKING}` (provisional — final set determined when implementing, declared at module top).

Subprocess and NDJSON management is shared in `src/platforms/_subprocess.py` so both CLI platforms aren't duplicating pipe/cancel/timeout/env-isolation logic.

### `PlatformRegistry`

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

Platform-as-plugin support is a future swap of this dict for a plugin-discovery hook (`PluginContext.register_platform(name, cls)`) — out of scope here, but explicitly designed for.

## §2 — Profile schema changes

### New columns on `agent_profiles`

| Column | Type | Default | Notes |
|---|---|---|---|
| `platform` | TEXT NOT NULL | `'claude_sdk'` (in migration backfill only) | Required — must match a `PlatformRegistry` name |
| `role` | TEXT NOT NULL | `''` | First-class — populated from markdown `## Role` section. Injected into agent prompt. |
| `rules` | TEXT NOT NULL | `''` | First-class — populated from markdown `## Rules` section. Injected into agent prompt. |
| `reflection` | TEXT NOT NULL | `''` | First-class — populated from markdown `## Reflection` section. **Consumed by the post-task reflection engine, not the executing agent.** Tells the reviewer LLM what to look for when scoring this agent's work. |
| `status` | TEXT NOT NULL | `'ok'` | One of `ok` / `warning` / `error` |
| `status_messages` | TEXT NOT NULL | `'[]'` | JSON list of human-readable warnings/errors |
| `last_validated_at` | REAL NULLABLE | NULL | Epoch — used to dedup "no change" Discord notifications |

`role`/`rules`/`reflection` get split out so they're queryable, individually editable, and surfaceable in `list_profiles` / `get_profile`. The existing `system_prompt_suffix` column is **kept** as a derived/composed field (built from `role + rules` at sync time, omitting `reflection` since it's not for the agent prompt). Existing consumers of `system_prompt_suffix` continue to work without change.

### `tasks.profile_id` becomes NOT NULL

Schema constraint enforces what the system requires: every task points at a valid profile.

### `## Platform` markdown section

The profile parser gains a new structured section, treated like the existing `## Config` JSON block:

```markdown
## Platform
\`\`\`json
{ "name": "claude_cli" }
\`\`\`
```

A profile without a `## Platform` section is a parse error → `status='error'`, sync aborts, last good DB row stays active, Discord error fires.

The `agent_profiles.platform` column is the resolved name from this section.

### Status semantics

- **ok** — loaded clean, capabilities all satisfied.
- **warning** — loaded fine, but the profile configures features the platform doesn't support. The profile is **still usable for new tasks**. At runtime, incompatible fields are filtered out before being passed to the platform (see §3.5). The user fixes the markdown; on the next vault sync, status transitions back to `ok` and a green Discord notification fires.
- **error** — failed to parse, or invalid platform name, or some other fatal validation failure. Sync aborts; last known good DB row stays in place for any in-flight tasks; **new task creation refuses to use this profile**.

## §3 — Loading and validation pipeline

When a profile markdown changes, `sync_profile_to_db()` runs:

1. **Parse** (existing) — `parser.py` produces a `ParsedProfile` with `errors` and `warnings`.
2. **Resolve platform** — look up `parsed.platform` in `PlatformRegistry`. If absent or unknown → `status='error'`, abort sync, emit Discord error embed (system channel), last good row stays.
3. **Capability audit** — walk the parsed config against the **rule table**:

   ```python
   # src/platforms/compat.py
   CAPABILITY_RULES: dict[Capability, Callable[[ParsedProfile], bool]] = {
       Capability.MCP: lambda p: bool(p.mcp_servers),
       Capability.PLAN_MODE: lambda p: p.permission_mode == "plan",
       # ... one rule per capability that gates a profile field
   }
   ```

   For each rule, if the rule fires (profile uses the feature) but the platform's `capabilities` set doesn't include it → append a warning to `status_messages`. Status is `warning` if any warnings, `ok` otherwise.

4. **Sync** — upsert row with `status` + `status_messages` + `last_validated_at`.
5. **Surface to Discord** — only on **status transitions** (compare previous DB row's `status` + `status_messages` content hash against new):
   - **error** (any → error) → system channel, **red** embed: *"Profile `X` failed to load: `<reason>`. Edit `<vault_path>` to fix."*
   - **warning** (ok → warning, or status_messages content changed) → system channel, **yellow** embed: *"Profile `X` loaded with warnings: …"*
   - **recovered** (warning/error → ok) → system channel, **green** embed: *"Profile `X` recovered."*
   - **First-time sync of a profile** (no previous DB row): notify if status is `warning` or `error`, silent if `ok`. Treats "no previous row" as equivalent to a synthetic previous `ok`.
   - No transition → no notification.

Reload is automatic — fix the markdown, the vault watcher re-syncs, new status is computed, the appropriate notification fires. No daemon restart.

### §3.5 — Compatibility filtering at platform handoff

When the orchestrator hands a profile to a platform, it passes a **filtered view** that strips fields the platform can't run:

```python
# src/platforms/compat.py
def filter_for_platform(profile: AgentProfile, platform_cls: type[Platform]) -> AgentProfile:
    """Return a copy of profile with incompatible fields cleared.

    For each capability the platform lacks, the corresponding profile fields
    are reset to their no-op default (empty dict, empty string, etc.)."""
    out = replace(profile)
    if Capability.MCP not in platform_cls.capabilities:
        out.mcp_servers = {}
    if Capability.PLAN_MODE not in platform_cls.capabilities and out.permission_mode == "plan":
        out.permission_mode = ""
    # ... one filter per capability that gates a profile field
    return out
```

Called once before each `platform.start(task, profile)`. The platform never sees fields it can't handle.

The same rule table drives both the sync-time audit (warns) and the runtime filter (silently neutralizes), so they cannot drift. New capabilities require updating one rule entry in `compat.py`.

## §4 — Task creation gate

### Strict requirement

`profile_id` is **required** at task creation across all entry points:

- Discord slash commands
- MCP tools (auto-exposed via CommandHandler)
- CLI (`aq task create`)
- Internal API / `CommandHandler.create_task`

`profile_name` accepted as a convenience input. Resolved via a new `db.get_profile_by_name()` query helper. The schema's `tasks.profile_id` is NOT NULL; any path that produces a row must resolve a valid FK.

### Validation flow

1. Resolve `profile_name` → `profile_id` (or accept `profile_id` directly).
2. If unresolvable → reject task creation. Post **red embed** to **project channel** (`get_notification_channel(project_id)` falls back to system if no project channel): *"Cannot create task: profile `<name>` not found. Available: …"* Return `{"success": false, "error": "profile_not_found", ...}` to the caller.
3. If resolved to a profile with `status='error'` → reject. Post **red embed** to project channel: *"Cannot create task: profile `<name>` is in error state. See system channel for details."*
4. If resolved to a profile with `status='warning'` → **accept**. Include warnings in the response. Post **yellow embed** to project channel: *"Task `<id>` created with profile `<name>` (warning state — some features disabled): …"* so the human knows the task isn't running with the full feature set.
5. If `status='ok'` → accept silently.

### Removed fallback

The orchestrator's silent fallback path (`if profile is None: use platform defaults`) is **deleted**. Tasks always have a profile; the platform is always determined by the profile.

## §5 — Migration

A single Alembic revision:

1. **Add new columns** to `agent_profiles`: `platform`, `role`, `rules`, `reflection`, `status`, `status_messages`, `last_validated_at`.
2. **Backfill `agent_profiles.platform = 'claude_sdk'`** for every existing row (correct — that's the only platform that exists pre-migration).
3. **Delete `tasks` rows where `profile_id IS NULL`.** Strict requirement. Logs every deleted task ID in the migration's `op.execute()` output for review. (`archived_tasks` left untouched — it's history, retains nullable `profile_id`.)
4. **Alter `tasks.profile_id` to NOT NULL** (FK preserved). On SQLite this requires `op.batch_alter_table()` (recreates the table); on PostgreSQL it's a direct `ALTER COLUMN`. Alembic's batch mode handles both — the migration uses batch operations for cross-DB compatibility per the project's SQLite + PostgreSQL support requirement.
5. **Trigger one-shot vault re-sync** of all profiles on the first daemon startup after migration. This populates the new `role`/`rules`/`reflection` fields from the markdown, runs the first capability audit, and fires Discord notifications for any profiles transitioning to `warning`/`error`. The daemon's existing vault sync code path is reused — no new code.

The migration is destructive (step 3). Standard pre-migration backup advice applies. The migration message logs:

```
Deleted N tasks with NULL profile_id: [task-id-1, task-id-2, ...]
```

so the operator has a record.

## §6 — Discord surfacing

Two destinations using the existing `get_notification_channel()` routing:

- **Profile load issues** → no `project_id` provided → falls back to **system channel**. Format: structured embed with profile name, platform, status, status_messages, vault path. Notifies only on status transitions.
- **Task creation issues** → `project_id` provided → routes to **project channel** (falls back to system if not configured). Format: structured embed with task description, profile name/status, error reason.

No new infrastructure — the existing `_emit_text_notify()` + EventBus + Discord transport handler already does the routing. New code just emits the events at the right places.

## Components and files

### New files
- `src/platforms/__init__.py` — `PlatformRegistry`
- `src/platforms/base.py` — `Platform` ABC, `Capability` enum, `MessageCallback`
- `src/platforms/claude_sdk.py` — moved from `src/adapters/claude.py`
- `src/platforms/claude_cli.py` — new
- `src/platforms/codex_cli.py` — new
- `src/platforms/_subprocess.py` — shared subprocess + NDJSON helpers
- `src/platforms/compat.py` — `CAPABILITY_RULES`, `filter_for_platform()`
- `migrations/versions/<rev>_platforms_refactor.py` — Alembic revision
- `tests/test_platforms_*.py` — one per platform impl + compat tests
- `tests/test_profile_validation.py` — covers status transitions, capability audit, Discord surfacing

### Modified files
- `src/database/tables.py` — add columns; alter `tasks.profile_id` to NOT NULL
- `src/database/queries/profile_queries.py` — add `get_profile_by_name()`, surface `status` / `status_messages`
- `src/profiles/parser.py` — parse `## Platform` section; populate `role`/`rules`/`reflection` as separate result fields (already partially split — finish the wiring)
- `src/profiles/sync.py` — invoke capability audit; emit Discord notifications on status transitions
- `src/commands/profile_commands.py` — surface `status` in `get_profile`/`list_profiles`
- `src/commands/task_commands.py` — strict profile validation in `create_task`; project-channel error surfacing
- `src/orchestrator/execution.py:411` — change `create("claude", ...)` → `create(profile.platform, ...)` + `filter_for_platform()`
- `src/orchestrator/sync_workflow.py:273` — same change
- `src/orchestrator/core.py` — remove silent `profile is None → defaults` fallback in `_resolve_profile`
- `src/models.py` — add fields to `AgentProfile` dataclass
- `src/prompt_builder.py` — read `role` field directly instead of (or alongside) `system_prompt_suffix`
- `docs/specs/adapters/*` → `docs/specs/platforms/*` — relocate spec docs and update content

### Deleted
- `src/adapters/` directory (replaced by `src/platforms/`)
- `tasks.profile_id IS NULL` rows (during migration)

## Testing strategy

- **Unit tests per platform impl** (`tests/test_platforms_claude_sdk.py`, etc.) — start/wait/stop/is_alive, cancellation, error classification. Existing `tests/test_claude_adapter.py` content moved + adapted.
- **Compat tests** (`tests/test_compat.py`) — for each capability rule, profile-with-feature + platform-without-cap → `filter_for_platform()` clears the field; sync-time audit produces the right warning.
- **Profile validation E2E** (`tests/test_profile_validation.py`):
  - vault → bad markdown → status='error' + system Discord notification
  - vault → markdown with MCP for codex_cli → status='warning' + system notification
  - vault → fix the bad markdown → status='ok' + green recovery notification
  - vault → no change → no duplicate notification
- **Task creation gate** (`tests/test_task_creation_strict.py`):
  - `profile_id=None` → rejected
  - `profile_name` resolves → success
  - `profile_name` doesn't resolve → rejected + project-channel embed posted
  - `profile_id` resolves to error-state profile → rejected
  - `profile_id` resolves to warning-state profile → accepted with warnings echoed
- **Migration** (`tests/test_migration_platforms.py`) — fixture DB with NULL profile_id tasks, run migration, assert deletion + NOT NULL constraint.
- **Integration** — full vault → DB → task → orchestrator → platform handoff with filtered profile, asserting the platform sees no incompatible fields.

## Risks and mitigations

- **CodexCLI capability declaration is provisional.** Risk: declaring `THINKING` when Codex doesn't actually expose it leads to wrong filtering. *Mitigation:* the capability set is finalized during the implementation plan for that platform, not at this design's commit. Initial set in this spec is illustrative.
- **`role`/`rules`/`reflection` split duplicates `system_prompt_suffix`.** Risk: drift between split fields and the composed suffix. *Mitigation:* the parser is the single writer — `system_prompt_suffix` is rebuilt from `role` + `rules` on every sync, never edited independently. Tests assert the invariant.
- **Migration deletes user data.** Risk: someone with NULL-profile tasks loses them. *Mitigation:* the deletion is logged with full task IDs; pre-migration backup is the standard advice. Considered alternative (backfill to a synthetic "default" profile) was rejected because the user explicitly chose strict deletion.
- **Discord notification spam during initial post-migration vault re-sync.** Risk: dozens of profiles all transition simultaneously and flood the system channel. *Mitigation:* the post-migration re-sync is a one-shot at startup; all transitions to `ok` are silent (status was previously `ok`-default, so no transition); only `warning`/`error` profiles produce notifications, which is exactly what the human needs to see.

## Future work (explicitly out of scope)

- **Platforms-as-plugins.** Replace the dict in `PlatformRegistry` with a plugin-registration hook. Add `PluginContext.register_platform(name, cls)` to the plugin contract. External platforms install via `aq plugin install`. The current spec preserves the seam.
- **Capability shimming.** e.g., translating MCP server configs into a non-MCP platform's tool format. Today we just filter and warn.
- **`ClaudeSDK` vs `ClaudeCLI` long-term.** They overlap (the SDK wraps the CLI). Once ClaudeCLI is proven, we may sunset the SDK platform — or keep both as "supported library path" vs "escape hatch." Decision deferred.
- **`LIVE_TAKEOVER` capability.** Pairs ClaudeCLI with a TUI observer (libtmux) so a human can attach to a running task. Whole separate design.
- **Per-platform config in `config.yaml`.** Today only `claude` config exists. ClaudeCLI needs a binary path; CodexCLI needs an API key and binary path. Added when each platform is implemented.
