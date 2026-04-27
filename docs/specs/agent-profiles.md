---
tags: [spec, agents, profiles]
---

# Agent Profiles Specification

## Overview

See [[design/profiles]] for the hybrid markdown profile format and vault storage.

Agent Profiles are capability bundles that configure agents with specific tools, MCP servers, model overrides, and system prompt additions at task execution time. They allow task-level specialization (e.g., a code reviewer vs. a web developer) without changing the scheduler or agent pool.

**Key invariant:** The scheduler is completely profile-unaware. Profile resolution happens downstream in `_execute_task()`, after the scheduler has already assigned a task to an agent. Zero LLM calls for orchestration are added.

## Data Model

### AgentProfile

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | str | (required) | Slug identifier: "reviewer", "web-developer" |
| `name` | str | (required) | Human-readable display name |
| `description` | str | "" | What this profile is for |
| `model` | str | "" | Model override (empty = use adapter default) |
| `permission_mode` | str | "" | Permission mode override (empty = use adapter default) |
| `allowed_tools` | list[str] | [] | Tool whitelist (empty = use adapter default). Stored as **bare** tool names — see [Tool Naming](#tool-naming) below. |
| `mcp_servers` | list[str] | [] | Names of MCP servers from the [registry](#mcp-server-registry). Resolved at task launch (project scope first, system fallback). |
| `system_prompt_suffix` | str | "" | Appended to agent's context as "Agent Role Instructions" |
| `install` | dict | {} | Auto-install manifest (reserved for future use) |

### Task Extension

Tasks gain an optional `profile_id` field (nullable foreign key to `agent_profiles.id`). When set, the agent executing this task is configured with the referenced profile's capabilities.

### Project Extension

Projects gain an optional `default_profile_id` field (nullable foreign key to `agent_profiles.id`). This serves as the fallback when a task has no explicit `profile_id`.

## Profile Resolution Cascade

When `_execute_task()` runs, it resolves the profile in this order:

1. **Task-level:** `task.profile_id` — if set, use this profile
2. **Project-level:** `project.default_profile_id` — if set, use this profile
3. **System default:** Neither set → use `ClaudeAdapterConfig` defaults (current behavior)

This is a pure dict lookup. No LLM calls. No scheduler changes.

## Adapter Integration

When a profile is resolved:

1. **Config merging:** `AdapterFactory._config_for_profile()` creates a `ClaudeAdapterConfig` by overlaying profile fields onto the base config. Empty profile fields (model="", permission_mode="", allowed_tools=[]) fall through to base config defaults.
2. **TaskContext population:** `TaskContext.mcp_servers` is populated from the profile. (Tools flow through `ClaudeAdapterConfig.allowed_tools`.)
3. **System prompt injection:** If `profile.system_prompt_suffix` is non-empty, it's injected as an "Agent Role Instructions" section in the task context.
4. **Runtime logging:** After profile resolution, the orchestrator logs which profile/tools/MCP servers were applied to the task.

## Database

### Table: `agent_profiles`

```sql
CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    permission_mode TEXT NOT NULL DEFAULT '',
    allowed_tools TEXT NOT NULL DEFAULT '[]',       -- JSON array of bare tool names
    mcp_servers TEXT NOT NULL DEFAULT '{}',          -- JSON; written as a list[str] of registry names. The literal default is '{}' for legacy reasons; the application coerces on read.
    system_prompt_suffix TEXT NOT NULL DEFAULT '',
    install TEXT NOT NULL DEFAULT '{}',              -- JSON dict
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
```

### Migrations

- `ALTER TABLE tasks ADD COLUMN profile_id TEXT REFERENCES agent_profiles(id)`
- `ALTER TABLE projects ADD COLUMN default_profile_id TEXT REFERENCES agent_profiles(id)`
- `ALTER TABLE archived_tasks ADD COLUMN profile_id TEXT`

### CRUD Methods

- `create_profile(profile)` — insert a new profile
- `get_profile(profile_id)` — fetch by ID, returns None if not found
- `list_profiles()` — all profiles sorted by name
- `update_profile(profile_id, **kwargs)` — partial update; JSON fields auto-serialized
- `delete_profile(profile_id)` — cascades: clears profile_id from tasks and default_profile_id from projects before deleting

## Source of Truth: Vault Markdown

The runtime source of truth for profiles is **vault markdown** at
`vault/agent-types/<id>/profile.md` (system) and
`vault/projects/<pid>/agent-types/<id>/profile.md` (project override). See
[[design/profiles]] for the markdown format. The vault watcher syncs changes
into the `agent_profiles` table at startup and on file change.

`config.yaml` profiles are still accepted for backward compatibility — a
startup migration extracts any `agent_profiles:` section into vault markdown
files (idempotent; never clobbers a hand-authored entry). New work should
edit the markdown directly.

### Tool Naming

`allowed_tools` is stored as **bare** tool names (`get_weather`,
`create_task`, `send_message`). The supervisor matches the LLM's
`tool_use.name` against this list directly. The Claude adapter rewrites
embedded `agent-queue` tools to their MCP-prefixed form
(`mcp__agent-queue__<name>`) when building `--allowed-tools` for the CLI
subprocess. Profile authors must **not** write the `mcp__agent-queue__`
prefix.

Exceptions:
- Claude built-ins (`Read`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`,
  `WebFetch`, `Agent`, etc.) keep their bare names.
- Third-party MCP servers use the full `mcp__<server>__<tool>` form (no
  unambiguous strip rule across servers).

The profile parser strips the `mcp__agent-queue__` prefix on read, so
legacy entries auto-canonicalize on the next vault → DB sync.

## MCP Server Registry

Profiles do **not** store inline MCP server configs anymore. `mcp_servers`
is a `list[str]` of names from the registry sourced at:

- `vault/mcp-servers/*.md` — system-scope server definitions
- `vault/projects/<pid>/mcp-servers/*.md` — project-scope (shadows system
  by name)

The orchestrator resolves names at task launch (project scope first,
system fallback). A startup migration moves legacy inline configs from
config.yaml profiles and old `profile.md` `## MCP Servers` blocks into
registry files. The embedded `agent-queue` server is a builtin registry
entry computed in-process from CommandHandler tool definitions plus
plugin tools. See [[mcp-server]] for the registry API and CRUD commands.


## Discovery & Validation

### Known Tools Registry (`src/known_tools.py`)

A registry of Claude Code built-in tools (`CLAUDE_CODE_TOOLS`) and well-known MCP servers (`KNOWN_MCP_SERVERS`). Provides:

- `validate_tool_names(tools)` — returns unrecognized tool names (soft warning, not error)
- `KNOWN_TOOL_NAMES` / `KNOWN_MCP_NAMES` — frozensets for fast lookup

### Tool Validation on Create/Edit

When creating or editing a profile with `allowed_tools`, unrecognized tool names produce a `warnings` field in the response. The profile is still created/edited — this catches typos without blocking custom MCP-provided tools.

### Discovery Command

`list_available_tools` returns all known tools (name + description) and MCP servers (name + description + npm_package) so users can discover what's available before configuring profiles.

## Install Manifest

### InstallManifest Dataclass

Parsed from `AgentProfile.install` dict:

```yaml
install:
  npm: ["@anthropic/mcp-playwright"]
  pip: ["black"]
  commands: ["docker", "node"]
```

`InstallManifest.from_dict(d)` / `.to_dict()` for serialization. `.is_empty` property.

### check_profile Command

Validates a profile's install manifest:
- Commands: checked via `shutil.which()`
- npm packages: checked via `npm list -g <pkg> --depth=0`
- pip packages: checked via `importlib.metadata.version()`

Returns `{profile_id, valid: bool, issues: [...], manifest: {...}}`.

### install_profile Command

Installs missing dependencies from the manifest:
- npm: `npm install -g <pkg>`
- pip: `pip install <pkg>`
- Commands: reported as manual-install items (can't auto-install system binaries)

Returns `{profile_id, installed, already_present, manual, ready}`.

**No restart required.** Each task spawns a fresh Claude Code subprocess, so newly-installed packages are immediately available.

### install Field in Create/Edit

`create_profile` and `edit_profile` accept an optional `install` dict. The `install` field is persisted as JSON in the `agent_profiles` table.

## Export / Import & Sharing

### YAML Export Format

Self-contained, shareable format under `agent_profile:` key:

```yaml
# Agent Profile: Code Reviewer
agent_profile:
  id: reviewer
  name: "Code Reviewer"
  description: "Read-only code review agent"
  model: "claude-sonnet-4-5-20250514"
  allowed_tools: [Read, Glob, Grep, Bash]
  mcp_servers: ["linter"]   # name of an entry in vault/mcp-servers/
  system_prompt_suffix: "You are a code reviewer."
  install:
    npm: ["eslint-mcp"]
    commands: ["npx"]
```

### export_profile Command

Serializes a profile to YAML. Optionally creates a public GitHub gist via `gh gist create`.

### import_profile Command

Imports from YAML text or gist URL:
- URL: extracts gist ID, fetches via `gh gist view <id> --raw`
- YAML: parses `agent_profile` key
- Optional `id` override and `overwrite` flag
- **Auto-installs dependencies:** if profile has non-empty install manifest, runs check → install for missing npm/pip packages
- Returns `{imported, name, installed, already_present, manual, ready}`

## Commands (Complete)

### Profile CRUD (system scope)
- `list_profiles` — show all profiles with summary info
- `create_profile` — create new profile (id, name required; tools/MCP/prompt/install optional)
- `get_profile` — full details of one profile (includes install manifest)
- `edit_profile` — partial update of profile fields (including install)
- `delete_profile` — remove profile, clear references

### Project-Scoped Profile CRUD
- `list_project_profiles` — list per-agent-type rows for a project (override / inherit / no-default)
- `create_project_profile` — create a project override (optionally seeded from the global default)
- `edit_project_profile` — partial update of a project override
- `delete_project_profile` — remove the override (resets the agent-type to global). Cleans up nested **and** flat vault paths so a stray scoped file can't resurrect during the next watcher pass.
- `show_effective_profile` — resolve `project + agent_type` to the effective profile (override → inherit → no-default)

### Discovery & Validation
- `list_available_tools` — discover tools and MCP servers for profile configuration

### Install Manifest
- `check_profile` — validate install dependencies (commands, npm, pip)
- `install_profile` — install missing npm/pip dependencies

### Export / Import
- `export_profile` — serialize to YAML, optionally create public gist
- `import_profile` — import from gist URL or YAML text, auto-install dependencies

### MCP Server Registry
See [[mcp-server]] for full details. Commands: `list_mcp_servers`,
`get_mcp_server`, `create_mcp_server`, `edit_mcp_server`,
`delete_mcp_server`, `probe_mcp_server`, `list_mcp_tool_catalog`.
`delete_mcp_server` refuses if any profile still references the name.

### Modified Commands
- `create_task` — accepts optional `profile_id`
- `edit_task` — accepts optional `profile_id` (null to clear)
- `edit_project` — accepts optional `default_profile_id` (null to clear)
- `get_task` — returns `profile_id` in output

## Backward Compatibility

- Tasks without `profile_id` → cascade to project default or system default. Identical to current behavior.
- Projects without `default_profile_id` → tasks use system defaults. Identical to current behavior.
- No YAML `agent_profiles` section → no profiles synced, everything works as before.
- Database migrations are additive (`ALTER TABLE ADD COLUMN`).
- Agents are unchanged — no migration needed.
- Scheduler is unchanged — same algorithm, same inputs, same outputs.
