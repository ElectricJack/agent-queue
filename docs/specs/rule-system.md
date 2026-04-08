---
tags: [spec, rules, automation, deprecated]
---

# Rule System — Primary Automation Specification

## Purpose

Rules are the **only interface for creating automation** in Agent Queue. They are
natural language intentions stored as structured markdown files in the memory
filesystem. The system translates rules into hooks (internal execution artifacts)
automatically. Users never need to interact with hooks directly.

**Key principle:** Rules are the source of truth. Hooks are derived, disposable
artifacts that the hook engine uses internally for execution.

> **Future evolution:** Rules evolve into [[design/playbooks|playbooks]] and [[design/vault-and-memory|vault memory]]. See [[design/playbooks]] Section 13 for migration path.

---

## Concepts

### Rule Types

**Active rules** have a trigger (periodic, event-driven) and logic. They generate one
or more hooks that execute via the hook engine. The rule is the source of truth;
hooks are derived, disposable artifacts.

**Passive rules** have no triggers. They influence the Supervisor's reasoning by
surfacing through semantic search when relevant to the current action. Example:
"When reviewing PRs, always check for SQL injection."

### Rule Storage

Rules are structured markdown files with YAML frontmatter:
- Project-scoped: `~/.agent-queue/memory/{project_id}/rules/`
- Global: `~/.agent-queue/memory/global/rules/`

### Rule Document Structure

```
---
id: rule-keep-tunnel-open
type: active
project_id: my-game-server
hooks: [hook-abc123]
source_hash: a1b2c3d4e5f6
created: 2026-03-20T10:00:00Z
updated: 2026-03-21T14:30:00Z
---

# Keep Cloudflare tunnel open

## Intent
...

## Trigger
Check every 5 minutes.

## Logic
1. Run `cloudflared tunnel status`
2. ...
```

---

## The Rule → Hook → Execution Pipeline

This is the core automation pipeline. Every automation follows this path:

### 1. Rule Saved

A rule is created or updated via one of:
- `save_rule` command (from Discord chat, MCP, or LLM tool call)
- Direct file editing (detected by the Rule File Watcher)
- Default rule installation on first startup

### 2. Hook Generated (Reconciliation)

The `RuleManager` processes the rule:
1. Parse trigger and logic from the rule's markdown content
2. Compute a `source_hash` of the trigger config + prompt content
3. Compare against existing hooks for this rule
4. If hash is unchanged, **skip regeneration** (idempotent)
5. If changed or no hooks exist: generate new hooks, then delete old ones
6. Update the rule's `hooks` frontmatter field

**Content-hash based reconciliation** prevents unnecessary hook churn. The
`source_hash` is stored on the hook record and compared on each reconciliation pass.
Only rules whose content has actually changed trigger hook regeneration.

### 3. Hook Fires

The hook engine monitors for trigger conditions:
- **Periodic hooks:** checked every `tick()` cycle (~5s), fire when interval elapses
- **Event hooks:** fire when matching events are published on the EventBus
- **File/folder watch hooks:** fire when watched paths change on disk

### 4. Run Recorded

Each execution creates a `HookRun` record capturing the trigger reason, rendered
prompt, LLM response, token usage, and status.

---

## Rule File Watcher — Auto-Reconciliation

The system monitors all rule directories for changes using a `FileWatcher` instance.
This makes direct file editing a first-class workflow.

### How It Works

1. On startup, a `FileWatcher` monitors:
   - `~/.agent-queue/memory/*/rules/` (per-project rules)
   - `~/.agent-queue/memory/global/rules/` (global rules)
2. When a `.md` file is created or modified, **only that rule** is reconciled
   (not a full scan of all rules)
3. When a `.md` file is deleted, its associated hooks are cleaned up
4. Changes are debounced (5s) to batch rapid edits

### Startup Reconciliation

On startup, the `RuleManager` performs one full reconciliation pass:
1. Scans all rule files (project + global)
2. For each active rule, checks that its listed hooks exist in the DB
3. Regenerates missing hooks (using `source_hash` to skip unchanged rules)
4. Removes orphaned hooks (hooks referencing rules that no longer exist)
5. Migrates any legacy direct hooks to rule-backed hooks

After this pass, the file watcher handles ongoing changes.

---

## Migration of Legacy Direct Hooks

Hooks that were created directly (not via rules) are automatically migrated to
rule-backed hooks. This ensures every automation has a rule file as its source of
truth.

### Migration Process

The `migrate_orphan_hooks()` method runs during startup reconciliation:

1. Query all hooks from DB
2. For each hook where `id` does NOT start with `rule-`: it's a direct/orphan hook
3. Generate a rule markdown file from the hook's config:
   - `name` → rule title (`# {name}`)
   - `trigger` JSON → `## Trigger` section (reverse-parsed into natural language)
   - `prompt_template` → `## Logic` section
   - `cooldown_seconds` → mentioned in trigger section
4. Save the rule file, let reconciliation regenerate the hook
5. Delete the original direct hook

The migration is idempotent — re-running it skips hooks that already have rule backing.

---

## Hook Generation Details

### LLM Prompt Expansion

When the supervisor is available (interactive `save_rule` calls),
`_generate_hooks_for_rule` calls `Supervisor.expand_rule_prompt()` to transform the
rule's natural language into a concrete operational prompt. This is a single LLM call
(no tool loop) that produces instructions with:
- Exact shell commands to run for health/status checks
- How to interpret command output (what "healthy" vs "unhealthy" looks like)
- Specific actions for each outcome, including the "do nothing" path
- Edge cases to watch for

This expansion happens once at rule creation/edit, not on every hook execution.

When the supervisor is unavailable (startup reconciliation), hooks fall back to a
static template that includes the raw rule content. These hooks still work — the
supervisor interprets the rule at execution time — but the prompts are less specific.

### Global Rules

**Global rules** (project_id=None) create one hook per active project. This means a
single global rule fans out to N hooks at generation time. New projects added after a
global rule was saved will pick up the hooks on the next startup reconciliation or
when detected by the file watcher.

Generated hooks do not use context steps. Instead, the hook's `prompt_template`
contains a specific, actionable prompt that the supervisor can execute using its
existing tools (shell, file I/O, task creation).

---

## Reconciliation Safety

### Idempotent Reconciliation

Reconciliation is designed to be safe against concurrent runs and impossible to
produce duplicates:

1. **Content-hash comparison:** Before regenerating hooks, compute a hash of the
   rule's trigger config + prompt content. Compare with the `source_hash` stored on
   existing hooks. If unchanged, skip entirely.
2. **Atomic hook replacement:** When regeneration IS needed:
   - Create new hooks first (with new IDs)
   - Verify creation succeeded
   - Delete old hooks
   - Update rule frontmatter atomically
3. **Concurrency protection:** An `asyncio.Lock` prevents concurrent reconciliation
   runs from racing.

### Cooldown Preservation

When hooks are regenerated during reconciliation, the `last_triggered_at` timestamp
from the old hook is carried forward to the new hook. This prevents hooks from
firing immediately after reconciliation just because the hook ID changed.

---

## Degradation Without Memsearch

- Rule files are always readable from disk (plain markdown)
- `browse_rules` and `load_rule` read directly from filesystem
- Without memsearch: `load_relevant_rules()` loads ALL rules for project + globals
- Hook reconciliation is filesystem-based, no search index needed

---

## Interfaces

### RuleManager

Constructor: `RuleManager(storage_root: str, db: Database, hook_engine: HookEngine | None)`

Methods:
- `save_rule(id, project_id, type, content) -> dict` — Write rule file, trigger hook generation for active rules
- `delete_rule(id) -> dict` — Delete rule file and associated hooks
- `browse_rules(project_id) -> list[dict]` — List rules for project + globals
- `load_rule(id) -> dict | None` — Load full rule content
- `reconcile() -> dict` — Full reconciliation (startup)
- `migrate_orphan_hooks() -> dict` — Convert direct hooks to rule-backed hooks
- `start_file_watcher(bus) -> None` — Start monitoring rule directories for changes
- `get_rules_for_prompt(project_id, query) -> str` — Load rules for PromptBuilder Layer 3
- `install_defaults() -> list[str]` — Install default global rules from bundled templates

### Bundled Default Rules

`install_defaults()` copies templates from `src/prompts/default_rules/` as global rules on first run. Each rule uses the standard Intent / Trigger / Logic structure:

| Rule | Trigger | Purpose |
|---|---|---|
| `dependency-update-check` | Every 24 hours | Run `scripts/check-outdated-deps.py` and `pip-audit` to find outdated or vulnerable packages; create tasks for critical updates |
| `error-recovery-monitor` | `event:task.failed` | Inspect failure details, retry transient errors, create fix tasks for code bugs |
| `periodic-project-review` | Every 30 minutes | Detect stuck/orphaned tasks, verify rule-hook sync, surface BLOCKED tasks |
| `post-action-reflection` | `event:task.completed` | Evaluate task results against acceptance criteria, trigger follow-up work, update project memory |
| `spec-drift-detector` | `event:task.completed` | Compare modified source files against corresponding specs, update stale specs or create tasks to reconcile |

### Commands (Primary Automation Interface)

These are the tools available to users and LLM agents for managing automation:

| Command | Description |
|---|---|
| `save_rule` | Create or update a rule (the primary way to create automation) |
| `delete_rule` | Remove a rule and its associated hooks |
| `browse_rules` | List active rules for a project + globals |
| `load_rule` | Load full rule content and metadata |
| `refresh_hooks` | Force reconciliation of all rules (rarely needed — file watcher handles this) |

### Read-Only Hook Commands

These commands provide visibility into the hook execution layer:

| Command | Description |
|---|---|
| `list_hooks` | List generated hooks (for debugging/inspection) |
| `list_hook_runs` | Show recent execution history for a hook |
| `fire_hook` | Manually trigger a hook immediately |
| `hook_schedules` | Show upcoming hook execution times |
| `schedule_hook` | Schedule a one-shot timed action (auto-deletes after execution) |

> **Note:** `create_hook`, `edit_hook`, and `delete_hook` are deprecated. All
> automation should be created via rules. Legacy direct hooks are automatically
> migrated to rule-backed hooks.

---

## Invariants

- Rule files are the source of truth; hooks are derived
- Every hook traces back to a source rule (enforced by migration)
- Deleting a rule always deletes its hooks
- Hook generation failures do not prevent rule save (rule saved, hooks marked as failed)
- Global rules are visible from all projects
- Rule IDs are unique across all scopes (project + global)
- Frontmatter timestamps are always UTC ISO 8601
- Reconciliation is idempotent (content-hash based)
- The file watcher auto-reconciles on rule file changes
