# Rule System

## Purpose

Persistent autonomous behaviors for the Supervisor. Rules are natural language intentions stored as structured markdown files in the memory filesystem. Active rules generate hooks for automated execution. Passive rules influence reasoning via semantic search.

## Concepts

### Rule Types

**Active rules** have a trigger (periodic, event-driven) and logic. They generate one or more hooks that execute via the existing hook engine. The rule is the source of truth; hooks are derived, disposable artifacts.

**Passive rules** have no triggers. They influence the Supervisor's reasoning by surfacing through semantic search when relevant to the current action. Example: "When reviewing PRs, always check for SQL injection."

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

### Hook Generation

When an active rule is saved or updated:
1. Parse trigger and logic from the rule content
2. Compare against existing hooks for this rule
3. If trigger/logic changed or no hooks exist: generate new hooks
4. Delete old hooks, create new ones
5. Update the rule's `hooks` frontmatter field

**Global rules** (project_id=None) create one hook per active project. This means a single global rule fans out to N hooks at generation time. New projects added after a global rule was saved will pick up the hooks on the next startup reconciliation.

Generated hooks do not use context steps. Instead, the hook's `prompt_template` contains a specific, actionable prompt that the supervisor can execute using its existing tools (shell, file I/O, task creation).

#### LLM Prompt Expansion

When the supervisor is available (interactive `save_rule` calls), `_generate_hooks_for_rule` calls `Supervisor.expand_rule_prompt()` to transform the rule's natural language into a concrete operational prompt. This is a single LLM call (no tool loop) that produces instructions with:
- Exact shell commands to run for health/status checks
- How to interpret command output (what "healthy" vs "unhealthy" looks like)
- Specific actions for each outcome, including the "do nothing" path
- Edge cases to watch for

This expansion happens once at rule creation/edit, not on every hook execution.

When the supervisor is unavailable (startup reconciliation), hooks fall back to a static template that includes the raw rule content. These hooks still work — the supervisor interprets the rule at execution time — but the prompts are less specific.

### Reconciliation

On startup, the RuleManager:
1. Scans all rule files (project + global)
2. For each active rule, checks that its listed hooks exist in the DB
3. Regenerates missing hooks
4. Removes orphaned hooks (hooks referencing rules that no longer exist)

### Degradation Without Memsearch

- Rule files are always readable from disk (plain markdown)
- `browse_rules` and `load_rule` read directly from filesystem
- Without memsearch: `load_relevant_rules()` loads ALL rules for project + globals
- Hook reconciliation is filesystem-based, no search index needed

## Interfaces

### RuleManager

Constructor: `RuleManager(storage_root: str, db: Database, hook_engine: HookEngine | None)`

Methods:
- `save_rule(id, project_id, type, content) -> dict` -- Write rule file, trigger hook generation for active rules
- `delete_rule(id) -> dict` -- Delete rule file and associated hooks
- `browse_rules(project_id) -> list[dict]` -- List rules for project + globals
- `load_rule(id) -> dict | None` -- Load full rule content
- `reconcile() -> dict` -- Startup reconciliation
- `get_rules_for_prompt(project_id, query) -> str` -- Load rules for PromptBuilder Layer 3
- `install_defaults() -> list[str]` -- Install default global rules from bundled templates

### CommandHandler Commands

- `save_rule` -- delegates to RuleManager.async_save_rule()
- `delete_rule` -- delegates to RuleManager.async_delete_rule()
- `browse_rules` -- delegates to RuleManager.browse_rules()
- `load_rule` -- delegates to RuleManager.load_rule()

## Invariants

- Rule files are the source of truth; hooks are derived
- Deleting a rule always deletes its hooks
- Hook generation failures do not prevent rule save (rule saved, hooks marked as failed)
- Global rules are visible from all projects
- Rule IDs are unique across all scopes (project + global)
- Frontmatter timestamps are always UTC ISO 8601
