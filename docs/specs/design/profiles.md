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
  "max_tokens_per_task": 100000,
  "runtime": "claude_sdk"
}
```

The ``runtime`` field selects which Runtime implementation executes
tasks for this profile.  Supported values:

- ``"claude_sdk"`` (default; matches ``config.default_runtime``) —
  spawns a Claude Code subprocess via the Agent SDK.  Owns a per-task
  workspace.  The escape hatch when ACP doesn't expose a needed feature
  (SDK-internals reach for ``_resilient_query``, bespoke error
  classification, etc.).
- ``"acpx"`` — fans out to any ACP-compatible coding agent via the
  ``acpx`` CLI.  ``profile.agent_name`` selects the underlying agent
  (``"claude"`` / ``"codex"`` / ``"gemini"`` / ``"opencode"`` /
  ``"cursor"`` / etc. — see [[runtimes/acpx]]).  Workspace-based.
- ``"supervisor"`` — runs in-process via the daemon-wide
  :class:`Supervisor` singleton.  Tool-call-only, no workspace.
  Suitable for triage / classify / summarise / route / send-message
  work where the profile's ``allowed_tools`` defines a bounded surface.
  See ``vault/templates/example-supervisor-runtime-profile.md``.

## Tools
```json
{
  "allowed": ["shell", "file_read", "file_write", "git", "vibecop_scan", "vibecop_check"],
  "denied": []
}
```

## MCP Servers
```json
["github", "playwright"]
```

The values are **registry names**, not inline configs. Server definitions
live in `vault/mcp-servers/<name>.md` (system) or
`vault/projects/<pid>/mcp-servers/<name>.md` (project scope shadows system
by name). See [[specs/mcp-server]] for the registry format and CRUD.

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
- `## MCP Servers` → list of registry names → DB field (`mcp_servers: list[str]`)

**What gets injected as prompt context (English sections):**
- `## Role` → system prompt prefix
- `## Rules` → behavioral guidance in agent context
- `## Reflection` → post-task reflection instructions

This split means misconfigured MCP servers are caught by JSON parse errors (not
LLM misinterpretation), while behavioral guidance stays natural and editable.

### Which headings reach the agent

Every agent runs as a session (a CLI in tmux) and receives its prompt from
`aq prime`, not from the DB `system_prompt_suffix` (that field is read only by
the legacy adapter path, which session-routed tasks skip). Prime therefore
defines the delivery contract:

| Heading | Reaches the agent? | Consumed by |
| --- | --- | --- |
| `## Role` | **yes** — prime section 1/2, rendered bare | prime |
| `## Rules` | **yes** — prime section 1/2, under a `### Rules` sub-heading | prime |
| `## Config` | no | harness/session launcher |
| `## Tools` | no | tool allow-list |
| `## MCP Servers` | no | MCP registry |
| `## Reflection` | no | post-task reflection playbook |
| anything else | no | nothing — machine-only or documentation |

The prime-visible set is the single tuple
`src/prime/sections.PRIME_VISIBLE_PROFILE_HEADINGS`; widening what agents see
means adding a heading there (and, for a project override profile, the same
headings apply — section 2 supplements section 1, it does not replace it).

Author `## Rules` as tight, imperative bullets: they are paid for on every
prime render. Across the shipped agent types the Rules block costs ~290 tokens
on average and ~700 at the largest, against a whole prime document of a few
thousand — cheap, but not free.

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

### 3.1 System profiles: seeding, drift, and reseeding

The profiles under `src/profiles/defaults/` are *shipped system profiles*.
`vault.ensure_default_profiles()` copies each to
`vault/agent-types/<id>/profile.md` on startup and is deliberately
**write-if-absent**: an existing file is never overwritten, so operator edits
survive upgrades and the vault copy is the source of truth once it exists.

The cost of that rule is drift. A vault copy seeded by an older release keeps
that release's schema and semantics forever, and some `## Config` fields are
load-bearing rather than cosmetic:

| field | why a stale value matters |
|---|---|
| `read_only` | `GitOpsMixin._task_produces_no_code()` (`src/orchestrator/git_ops.py`) reads it to decide whether a task must push a branch and open a PR. A vault `reviewer` still saying `read_only: false` re-arms the require-a-PR close gate for a session that is told never to push. |
| `harness` | selects which CLI actually runs the agent. |
| `lifecycle` | push (`task`) vs pull (`pool`) vs `named`. |
| `needs_workspace` | whether the orchestrator acquires a worktree. |

Section renames drift the same way — a copy predating the `## Tools` →
`## Capabilities` rename routes through the legacy `allowed_tools` adapter
forever.

**Detection** is `src/profiles/drift.py`, surfaced two ways:

- `profiles.system_drift` — a report-only doctor check (trust-and-ops §5.2).
- `aq agent profile-drift [--profile-id <id>] [--drifted-only]` — the same
  comparison as a command, one row per system profile with its status
  (`ok` / `not_seeded` / `drifted` / `unreadable`), the diverging semantic
  fields, and missing/extra section headings.

Only the four fields above and *missing* sections count as drift. A changed
`description`, `model` or `default_class`, and sections the operator added,
are the operator's business and are reported for context but never flagged.

**Repair** is explicit and per-profile:

```bash
aq agent profile-drift --drifted-only     # what diverged, and how
aq agent profile-reseed reviewer          # restore the shipped version
```

`profile-reseed` copies the old file to `profile.md.bak-<epoch>` (pass
`--backup false` to skip) before writing the shipped default, then syncs the
new text straight to the DB. There is deliberately no `aq doctor --fix` for
this: overwriting an operator-owned vault file automatically would violate the
`--fix` safety rules (trust-and-ops §5.4).

---

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
