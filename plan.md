---
auto_tasks: true
---

# Refactor: Unify Rules & Hooks — Rules as First-Class, Hooks as Implementation Detail

## Background & Problem Statement

The current system has **two parallel interfaces** for automation:

1. **Rules** — Markdown files with YAML frontmatter in `~/.agent-queue/memory/`. User-friendly, natural language. Active rules generate hooks via `RuleManager`.
2. **Hooks** — SQLite DB records. Lower-level, JSON config for triggers, prompt templates, cooldowns. Directly created/edited via `create_hook`, `edit_hook`, etc.

### Current Problems

1. **Duplicate hooks** — Discord reconnects fire `on_ready` → `_reconcile_rules()`, which deletes old hooks and creates new ones. Despite guards (TOCTOU race protection, `_reconciliation_task` check), rapid reconnects or concurrent reconciliations can create orphan hooks that persist in the DB and fire alongside legitimate ones.

2. **Two command surfaces** — Users can create automation via `create_hook` (direct DB) OR `save_rule` (markdown → hooks). Direct hooks bypass rule tracking entirely, making them invisible to `browse_rules` and unmanaged by reconciliation. This causes confusion about what automations exist and where they came from.

3. **Hook editing breaks rule linkage** — If a user edits a rule-generated hook directly via `edit_hook`, the next reconciliation overwrites those changes because the rule file is the source of truth. But the user has no indication this will happen.

4. **LLM agents create hooks directly** — The tool registry exposes `create_hook`, `edit_hook`, etc. as first-class tools. LLM agents in chat/hook execution create hooks directly, bypassing rules entirely. These "orphan" hooks have no rule backing and are invisible to rule management.

5. **No automatic hook regeneration on rule file changes** — If a rule markdown file is edited directly on disk (not via `save_rule` command), the hooks are not regenerated until the next manual `refresh_hooks` or Discord reconnect.

### Design Goal

**Rules are the ONLY way to create automation.** Hooks become a pure implementation detail — the DB-backed execution artifacts that the hook engine uses internally. Users never interact with hooks directly.

This eliminates:
- Duplicate hooks (single source of truth: rule files)
- Orphan hooks (every hook traces back to a rule)
- Confused state from mixed hook/rule editing
- Need for manual `refresh_hooks`

---

## Phase 1: Add Rule File Watcher — Auto-Reconcile on Rule Changes

**Goal:** When any rule markdown file is created, modified, or deleted on disk, automatically trigger reconciliation for that rule. This makes direct file editing a first-class workflow and eliminates the need for `refresh_hooks`.

**Files to modify:**
- `src/rule_manager.py` — Add a `FileWatcher` instance monitoring all rule directories (`~/.agent-queue/memory/*/rules/` and `~/.agent-queue/memory/global/rules/`). On file change, reconcile just the affected rule (not all rules). On file deletion, clean up associated hooks.
- `src/hooks.py` or `src/main.py` — Wire rule file watcher startup/shutdown into the orchestrator lifecycle.
- `src/discord/bot.py` — Remove `_reconcile_rules()` from `on_ready` (or keep it only for first-ever startup). The file watcher handles ongoing changes.

**Key details:**
- Use the existing `FileWatcher` from `src/file_watcher.py` (already used for hook file/folder triggers)
- Debounce changes (5s) to batch rapid edits
- Per-rule reconciliation instead of full scan (parse changed file → regenerate its hooks only)
- On startup, still do a full reconciliation pass once, then hand off to file watcher

---

## Phase 2: Redirect Hook Commands to Rule Commands

**Goal:** Replace all direct hook creation/editing commands with rule-based equivalents. Users always work with rules; hooks are generated automatically.

**Files to modify:**

### `src/tool_registry.py`
- **Remove** from tool registry: `create_hook`, `edit_hook`, `delete_hook` (the direct hook CRUD tools)
- **Keep** read-only hook tools: `list_hooks` (renamed to `list_automations` or kept for debugging), `list_hook_runs`, `fire_hook`, `hook_schedules`
- **Keep** scheduling tools: `schedule_hook` (one-shot scheduled hooks are a special case — they auto-delete and don't need rule backing)
- **Rename/update** rule tools to be the primary automation interface: `save_rule` → `create_automation` or keep `save_rule`, `browse_rules` → `list_rules`, etc.

### `src/command_handler.py`
- **Deprecate** `_cmd_create_hook`: Make it create a rule instead (generate a rule markdown file from the hook parameters, then let reconciliation create the hook)
- **Deprecate** `_cmd_edit_hook`: For rule-backed hooks (`hook.id` starts with `rule-`), redirect to editing the source rule. For legacy direct hooks, allow edit but warn.
- **Remove** `_cmd_delete_hook` for rule-backed hooks (must delete via rule). Keep for orphan cleanup.
- **Keep** `_cmd_fire_hook`, `_cmd_list_hooks`, `_cmd_list_hook_runs` as read-only/execution commands

### `src/discord/commands.py`
- **Remove or hide** the `/create-hook` and `/add-hook` slash commands
- **Remove** the `_HookWizardStartView` and all hook creation wizard UI (lines 5004-5577)
- **Keep** `/hooks` as a read-only view (shows generated hooks, links back to source rules)
- **Update** `HooksListView` — remove edit buttons for rule-backed hooks, add "View Source Rule" button instead
- **Keep** `/rules`, `/rule`, `/delete-rule`, `/refresh-hooks` as the primary management commands
- **Add** `/create-rule` slash command with a modal for quick rule creation

---

## Phase 3: Migrate Existing Direct Hooks to Rules

**Goal:** Convert all existing hooks that were created directly (not via rules) into rule-backed hooks, so every automation has a rule file as its source of truth.

**Files to modify:**

### `src/rule_manager.py`
- Add `migrate_orphan_hooks()` method:
  1. Query all hooks from DB
  2. For each hook where `id` does NOT start with `rule-`: it's a direct/orphan hook
  3. Generate a rule markdown file from the hook's config:
     - `name` → rule title (`# {name}`)
     - `trigger` JSON → `## Trigger` section (reverse-parse periodic/event into natural language)
     - `prompt_template` → `## Logic` section
     - `cooldown_seconds` → mentioned in trigger section
  4. Save the rule file, let reconciliation regenerate the hook
  5. Delete the original direct hook
- Add `migrate_orphan_hooks()` call to startup reconciliation (run once, idempotent)

### `src/database.py`
- No schema changes needed — hooks table stays as-is

---

## Phase 4: Simplify Reconciliation & Eliminate Duplicates

**Goal:** Make reconciliation idempotent, safe against concurrent runs, and impossible to produce duplicates.

**Files to modify:**

### `src/rule_manager.py`
- **Add reconciliation lock** — Use `asyncio.Lock` to prevent concurrent reconciliation runs. The current `_reconciliation_task.done()` check in `bot.py` is insufficient for rapid reconnects.
- **Content-hash based reconciliation** — Instead of always deleting and recreating hooks:
  1. Compute a hash of the rule's trigger config + prompt content
  2. Store this hash in the hook (add `source_hash` field or encode in hook ID)
  3. On reconciliation, compare hashes. If unchanged, skip regeneration entirely.
  4. This eliminates unnecessary hook churn and the associated timing/duplicate risks.
- **Atomic hook replacement** — When regeneration IS needed:
  1. Create new hooks first (with new IDs)
  2. Verify creation succeeded
  3. Only then delete old hooks
  4. Update rule frontmatter atomically
  This prevents the window where no hooks exist for a rule.

### `src/models.py`
- Add `source_hash: str | None = None` to Hook dataclass (or store in `llm_config` JSON to avoid schema migration)

### `src/database.py`
- Add `source_hash` column to hooks table (with migration) OR encode in existing JSON field

### `src/discord/bot.py`
- Simplify `on_ready` — reconciliation is now handled by the file watcher (Phase 1) + startup-once pass. Remove the `_reconciliation_task` pattern.

---

## Phase 5: Update Specs & Clean Up

**Goal:** Update all specs and documentation to reflect the unified model.

**Files to modify:**

### `specs/hooks.md`
- Add section: "Hook Provenance — all hooks are rule-generated"
- Remove/deprecate sections about direct hook creation
- Document the `source_hash` field
- Update lifecycle to show: Rule saved → Hook generated → Hook fires → Run recorded

### `specs/rule-system.md`
- Expand to be the primary automation spec
- Document the file watcher auto-reconciliation
- Document the migration of orphan hooks
- Add section on the rule → hook → execution pipeline

### `src/tool_registry.py`
- Final cleanup: remove any remaining direct hook CRUD tool stubs
- Update tool descriptions to reference rules as the primary interface

### `src/prompts/default_rules/`
- Review and potentially add more default rules now that rules are the only interface
- Ensure default rules cover common automation patterns

---

## Migration Strategy

1. **Phase 1** can be deployed independently — it only adds capability (file watcher)
2. **Phase 2** should be deployed with **Phase 3** — removing hook commands without migrating existing hooks would break automations
3. **Phase 4** can be deployed anytime after Phase 1 — it's a pure improvement to reconciliation
4. **Phase 5** is documentation cleanup, deploy last

## Risk Mitigation

- **Backward compatibility:** Phase 2 should log deprecation warnings for 1-2 releases before removing direct hook commands entirely. `_cmd_create_hook` can internally create a rule + trigger reconciliation.
- **Data safety:** Phase 3 migration is idempotent and creates rule files before deleting hooks. If migration fails mid-way, re-running it picks up where it left off.
- **Concurrent safety:** Phase 4's `asyncio.Lock` prevents all race conditions. Content hashing prevents unnecessary regeneration.
