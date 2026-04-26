---
title: Profile Validation + Platform-Driven Dispatch (Phase 2)
date: 2026-04-25
status: draft
tags: [spec, platforms, profiles, validation, discord]
---

# Profile Validation + Platform-Driven Dispatch (Phase 2)

## Scope

This spec depends on phase 1 ([`2026-04-25-platforms-implementation-design.md`](./2026-04-25-platforms-implementation-design.md)) being merged. Phase 1 establishes the `Platform` ABC, `Capability` enum, three platform implementations (`ClaudeSDKPlatform`, `ClaudeCLIPlatform`, `CodexCLIPlatform`), the `PlatformRegistry`, and a temporary `config.default_platform` field used at orchestrator call sites.

This phase replaces the `config.default_platform` stub with profile-driven dispatch and adds the validation/UX layer that makes the system fail loudly when profiles or tasks are misconfigured.

## Problem (the user pain this addresses)

After phase 1, the daemon can run any of three platforms — but selection is global, not per-profile. And the original problems remain:

1. **Profile validation is silent.** Bad profiles can sit in the DB with no visible signal. Profile parse failures emit only to the event bus, and only when one is configured. No Discord notification.
2. **Tasks can be created with no profile, or with a broken one.** `tasks.profile_id` is nullable. The orchestrator falls back to platform defaults silently. Tasks then sit in DEFINED forever, or run with the wrong configuration. The user-visible symptom is "I made a task and it never started," with no error trail.
3. **There's no way to express which platform a profile targets.** All profiles end up running on `config.default_platform` regardless of intent.

This phase fixes all three.

## Out of scope

- The platform abstraction and platform implementations themselves (covered in phase 1).
- Platforms-as-plugins (the `PlatformRegistry` dict is preserved; plugin-discovery hook deferred).
- Capability shimming (translating MCP server configs to a non-MCP platform's tool format). v1 just filters and warns.
- Per-task capability declarations. Tasks inherit entirely from their profile.

## §1 — Profile schema changes

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
- **warning** — loaded fine, but the profile configures features the platform doesn't support. The profile is **still usable for new tasks**. At runtime, incompatible fields are filtered out before being passed to the platform (see §3). The user fixes the markdown; on the next vault sync, status transitions back to `ok` and a green Discord notification fires.
- **error** — failed to parse, or invalid platform name, or some other fatal validation failure. Sync aborts; last known good DB row stays in place for any in-flight tasks; **new task creation refuses to use this profile**.

## §2 — Loading and validation pipeline

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

## §3 — Compatibility filtering at platform handoff

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

## §5 — Removing the phase-1 `default_platform` stub

Phase 1 introduced `config.default_platform` as a temporary mechanism for selecting which platform the orchestrator dispatches to. Phase 2 replaces it with profile-driven dispatch:

- `src/orchestrator/execution.py:~411` — change `self._platforms.create(self._config.default_platform, profile=profile)` to `self._platforms.create(profile.platform, profile=profile)`. Wrap the `profile` argument with `filter_for_platform(profile, platform_cls)` first.
- `src/orchestrator/sync_workflow.py:~273` — same change.
- `src/config.py` — delete the `default_platform` field.
- `~/.agent-queue/config.yaml` — config loader logs a one-line warning at daemon startup if `default_platform` is still present in the user's config file ("config.default_platform is no longer supported — platform is now determined per-task by the profile. Remove this key from config.yaml."). The unknown key is otherwise ignored, not a hard error.

After this change, `config.default_platform` is fully retired and platform selection is always per-task via `profile.platform`.

## §6 — Discord surfacing

Two destinations using the existing `get_notification_channel()` routing:

- **Profile load issues** → no `project_id` provided → falls back to **system channel**. Format: structured embed with profile name, platform, status, status_messages, vault path. Notifies only on status transitions.
- **Task creation issues** → `project_id` provided → routes to **project channel** (falls back to system if not configured). Format: structured embed with task description, profile name/status, error reason.

No new infrastructure — the existing `_emit_text_notify()` + EventBus + Discord transport handler already does the routing. New code just emits the events at the right places.

## §7 — Migration

A single Alembic revision:

1. **Add new columns** to `agent_profiles`: `platform`, `role`, `rules`, `reflection`, `status`, `status_messages`, `last_validated_at`.
2. **Backfill `agent_profiles.platform = 'claude_sdk'`** for every existing row (correct — that's the only platform in operational use pre-migration).
3. **Delete `tasks` rows where `profile_id IS NULL`.** Strict requirement. Logs every deleted task ID in the migration's `op.execute()` output for review. (`archived_tasks` left untouched — it's history, retains nullable `profile_id`.) Before deletion, NULL out `agents.current_task_id` for any agent pointing at a task being deleted (`UPDATE agents SET current_task_id = NULL, state = 'IDLE' WHERE current_task_id IN (<deleted-ids>)`), so the FK is consistent post-delete.
4. **Alter `tasks.profile_id` to NOT NULL** (FK preserved). On SQLite this requires `op.batch_alter_table()` (recreates the table); on PostgreSQL it's a direct `ALTER COLUMN`. Alembic's batch mode handles both — the migration uses batch operations for cross-DB compatibility per the project's SQLite + PostgreSQL support requirement.
5. **Trigger one-shot vault re-sync** of all profiles on the first daemon startup after migration. This populates the new `role`/`rules`/`reflection` fields from the markdown, runs the first capability audit, and fires Discord notifications for any profiles transitioning to `warning`/`error`. The daemon's existing vault sync code path is reused — no new code.

The migration is destructive (step 3). Standard pre-migration backup advice applies. The migration message logs:

```
Deleted N tasks with NULL profile_id: [task-id-1, task-id-2, ...]
```

so the operator has a record.

## §8 — Components and files

### New files
- `src/platforms/compat.py` — `CAPABILITY_RULES`, `filter_for_platform()`, capability audit helper consumed by `sync_profile_to_db()`
- `migrations/versions/<rev>_profile_validation.py` — Alembic revision
- `tests/test_profile_validation.py` — covers status transitions, capability audit, Discord surfacing
- `tests/test_compat.py` — capability rules + `filter_for_platform`
- `tests/test_task_creation_strict.py` — task creation gate behaviors
- `tests/test_migration_profile_validation.py` — migration fixture

### Modified files
- `src/database/tables.py` — add columns; alter `tasks.profile_id` to NOT NULL
- `src/database/queries/profile_queries.py` — add `get_profile_by_name()`, surface `status` / `status_messages`
- `src/profiles/parser.py` — parse `## Platform` section; populate `role`/`rules`/`reflection` as separate result fields
- `src/profiles/sync.py` — invoke capability audit; emit Discord notifications on status transitions
- `src/commands/profile_commands.py` — surface `status` in `get_profile`/`list_profiles`
- `src/commands/task_commands.py` — strict profile validation in `create_task`; project-channel error surfacing
- `src/orchestrator/execution.py:~411` — switch to `profile.platform` + `filter_for_platform`
- `src/orchestrator/sync_workflow.py:~273` — same
- `src/orchestrator/core.py` — remove silent `profile is None → defaults` fallback in `_resolve_profile`
- `src/config.py` — delete `default_platform` field; emit deprecation warning if present in config.yaml
- `src/models.py` — add fields to `AgentProfile` dataclass
- `src/prompt_builder.py` — read `role` field directly instead of (or alongside) `system_prompt_suffix`

### Deleted
- `tasks.profile_id IS NULL` rows (during migration)
- `AppConfig.default_platform` field (replaced by per-profile dispatch)

## §9 — Testing strategy

### Compat tests

`tests/test_compat.py` — for each capability rule, profile-with-feature + platform-without-cap → `filter_for_platform()` clears the field; sync-time audit produces the right warning. Covers all entries in `CAPABILITY_RULES`.

### Profile validation E2E

`tests/test_profile_validation.py`:
- vault → bad markdown → status='error' + system Discord notification
- vault → markdown with MCP for codex_cli → status='warning' + system notification
- vault → fix the bad markdown → status='ok' + green recovery notification
- vault → no change → no duplicate notification

### Task creation gate

`tests/test_task_creation_strict.py`:
- `profile_id=None` → rejected
- `profile_name` resolves → success
- `profile_name` doesn't resolve → rejected + project-channel embed posted
- `profile_id` resolves to error-state profile → rejected
- `profile_id` resolves to warning-state profile → accepted with warnings echoed

### Migration

`tests/test_migration_profile_validation.py` — fixture DB with NULL profile_id tasks, run migration, assert deletion + NOT NULL constraint + populated `platform` column.

### Integration

Full vault → DB → task → orchestrator → platform handoff with filtered profile, asserting the platform sees no incompatible fields. One end-to-end test using mock platforms.

### Regression

Existing `tests/test_orchestrator*.py`, `tests/test_supervisor*.py`, `tests/test_profiles_*.py` continue to pass. Phase 1's platform tests are unchanged.

## §10 — Risks

- **`role`/`rules`/`reflection` split duplicates `system_prompt_suffix`.** *Mitigation:* the parser is the single writer — `system_prompt_suffix` is rebuilt from `role` + `rules` on every sync, never edited independently. Tests assert the invariant.
- **Migration deletes user data.** *Mitigation:* the deletion is logged with full task IDs; pre-migration backup is the standard advice. Considered alternative (backfill to a synthetic "default" profile) was rejected because the user explicitly chose strict deletion.
- **Discord notification spam during initial post-migration vault re-sync.** *Mitigation:* the post-migration re-sync is a one-shot at startup; profiles backfilled with `status='ok'` and identical re-sync result produce no transition; only `warning`/`error` profiles produce notifications, which is exactly what the human needs to see.
- **Operator misses the `default_platform` deprecation.** *Mitigation:* startup warning logs the deprecation; one release of compatibility (warning + ignore); removed in the release after.

## §11 — Future work

- **Platforms-as-plugins.** Replace the dict in `PlatformRegistry` with a plugin-registration hook. Add `PluginContext.register_platform(name, cls)` to the plugin contract. External platforms install via `aq plugin install`.
- **Capability shimming.** e.g., translating MCP server configs into a non-MCP platform's tool format. Today we just filter and warn.
- **`ClaudeSDK` vs `ClaudeCLI` long-term.** They overlap (the SDK wraps the CLI). Once ClaudeCLI is proven, we may sunset the SDK platform — or keep both as "supported library path" vs "escape hatch." Decision deferred.
- **`LIVE_TAKEOVER` capability.** Pairs ClaudeCLI with a TUI observer (libtmux) so a human can attach to a running task. Whole separate design.
- **Per-platform config in `config.yaml`.** Today only `claude` config exists. ClaudeCLI needs a binary path; CodexCLI needs an API key and binary path. Added when each platform is implemented.
