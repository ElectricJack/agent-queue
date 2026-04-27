---
tags: [design, profiles, agents, markdown]
---

# Agent Profiles as Markdown

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #9 simple interfaces)
**Related:** [[vault]], [[memory-scoping]], [[specs/agent-profiles]], [[agent-coordination]]

---

## 1. Overview

Agent profiles are **markdown files in the vault** that serve as the source of truth
for agent-type configuration. The database stores a synced copy for fast runtime
access. This replaces the current DB-only profile model.

---

## 2. Hybrid Format

Profiles use a **hybrid approach**: freeform English for guidance that gets injected
into the agent's prompt, and JSON code blocks for structured configuration that
requires exact parsing. This avoids the fragility of LLM-parsing tool configurations
while keeping behavioral guidance human-readable.

`vault/agent-types/coding/profile.md`:

````markdown
---
id: coding
name: Coding Agent
tags: [profile, agent-type]
---

# Coding Agent

## Role
You are a software engineering agent. You write, modify, and debug code
within a project workspace. You follow project conventions, write tests,
and commit clean, working code.

## Config
```json
{
  "model": "claude-sonnet-4-6",
  "permission_mode": "auto",
  "max_tokens_per_task": 100000
}
```

## Tools
```json
{
  "allowed": ["shell", "file_read", "file_write", "git", "vibecop_scan", "vibecop_check"],
  "denied": []
}
```

## MCP Servers
```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
  }
}
```

## Rules
- Always run existing tests before committing
- Never commit secrets, .env files, or credentials
- Prefer small, focused commits over large ones
- If tests fail after your changes, fix them before moving on
- Check for and respect any project-specific overrides

## Reflection
After completing a task, consider:
- Did I encounter any surprising behavior worth remembering?
- Did I resolve an error that might recur? If so, save the pattern.
- Is there a convention in this project I should note for next time?
````

**What gets parsed deterministically (JSON blocks):**
- `## Config` → model, permission mode, token limits → DB fields
- `## Tools` → allowed/denied tool lists → DB fields
- `## MCP Servers` → exact server configurations → DB fields

**What gets injected as prompt context (English sections):**
- `## Role` → system prompt prefix
- `## Rules` → behavioral guidance in agent context
- `## Reflection` → post-task reflection instructions

This split means misconfigured MCP servers are caught by JSON parse errors (not
LLM misinterpretation), while behavioral guidance stays natural and editable.

---

## 3. Sync Model

```
Human edits profile.md in Obsidian
       │                              Agent updates profile via chat command
       │                                       │
       ▼                                       ▼
  File watcher detects change          System writes to profile.md
       │                                       │
       ▼                                       ▼
  Parse profile ◄─────────────────────────────┘
       │
       ├── Extract JSON code blocks → validate → structured DB fields
       ├── Extract English sections → store as prompt text
       │
       ▼
  Update DB row (agent_profiles table)
       │
       ▼
  Runtime picks up new config
```

All writes flow through the markdown file. The chat/dashboard interface writes to
the file, not the DB. The file watcher handles sync in one direction only:
**markdown → DB**. No bidirectional sync, no conflicts.

**Validation on sync:**
- JSON blocks must parse successfully; if not, sync fails and the previous DB
  config remains active. An error notification is sent.
- Tool names in `## Tools` are validated against the tool registry. Unknown tools
  produce a warning (not a hard failure — the tool may not be loaded yet).
- MCP server commands are validated for basic structure (command exists, args are
  strings). Server health is not checked at sync time.

### Tool naming in `## Tools`

`allowed` uses **bare tool names** (the form the supervisor's tool registry
exposes — e.g. `get_weather`, `create_task`, `send_message`). The supervisor
validates these directly against the registry; sandboxed playbooks rely on
the same form.

The Claude CLI sees agent-queue tools through the MCP transport as
`mcp__agent-queue__<name>`. The Claude adapter handles the translation
automatically — bare names in `allowed` are mapped to their MCP-prefixed
form when building `--allowed-tools` for the CLI subprocess. **Profile
authors do not write the `mcp__agent-queue__` prefix.**

Exceptions:
- **Claude built-ins** (`Read`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`,
  `WebFetch`, `Agent`, etc.) keep their bare names — they live outside the
  embedded MCP server.
- **Third-party MCP servers** (anything other than the embedded `agent-queue`
  server) use the full `mcp__<server>__<tool>` form in `allowed`. There's
  no unambiguous way to strip a prefix when multiple servers might define
  the same bare tool name.

---

## 4. Starter Knowledge Packs

New agent types start with no memory. To avoid a cold-start problem, the system
ships **starter knowledge packs** in `vault/templates/knowledge/`:

```
vault/templates/knowledge/
  coding/
    common-pitfalls.md           # "Always check for async/sync mismatches..."
    git-conventions.md           # "Prefer small commits, meaningful messages..."
  code-review/
    review-checklist.md          # "Check for: error handling, edge cases..."
    review-process.md            # "Review order, giving feedback, scope..."
  qa/
    testing-patterns.md          # "Prefer integration tests for critical paths..."
```

When a new agent type is created (profile.md saved for the first time), the system
copies matching starter knowledge from `templates/knowledge/{type}/` to the agent
type's `memory/` folder if one exists. These starter files are tagged `#starter`
and can be updated or removed as the agent accumulates real experience.
