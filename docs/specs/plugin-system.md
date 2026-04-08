---
tags: [spec, plugins, extensibility]
---

# Plugin System Specification

**Source files:** `src/plugins/` , modifications to `src/command_handler.py`, `src/tool_registry.py`, `src/orchestrator.py`, `src/cli/app.py`, `src/discord/bot.py`
**Related specs:** [[specs/hooks]], [[specs/rule-system]], [[specs/command-handler]], [[specs/tiered-tools]]

---

## 1. Overview

> **Future evolution:** See [[design/vault-and-memory]] Section 3 for how the memory plugin v2 extends the plugin architecture.

The plugin system enables extending AgentQueue with installable, self-contained Python packages that can register commands, tools, cron-scheduled functions, CLI commands, and Discord slash commands. Plugins are installed from git repositories, managed via Discord slash commands and the `aq` CLI, and can be developed/updated by AgentQueue agents themselves.

The core design principles:

- **Plugins are proper Python packages** — metadata (name, version, description, author) lives in `pyproject.toml`, not a custom manifest. Entry points use the `aq.plugins` group.
- **Plugins are first-class participants** in the command, tool, CLI, and Discord systems — not a parallel execution path.
- **Cron-scheduled functions are plain async Python** — not LLM prompt invocations. Plugins can call `ctx.invoke_llm()` when they need AI reasoning.
- **Legacy `plugin.yaml` is still supported** but deprecated. New plugins should use `pyproject.toml`.

### Use Cases

- Periodic email review: plugin runs every 4 hours, checks email via IMAP/API, surfaces important items as notifications or tasks
- Custom notification channels: Slack, Teams, webhooks
- External service integrations: Jira, Linear, GitHub Issues sync
- Custom agent adapters: local LLMs, OpenAI, etc.
- Domain-specific tools: deployment pipelines, monitoring dashboards

---

## 2. Plugin Structure

A plugin has two locations: the **source** (git-managed code) and the **instance data** (install-specific configuration, prompts, and state that lives outside the git repo).

### Source Directory (git-managed)

```
my-plugin/                         # Cloned to ~/.agent-queue/plugins/{name}/src/
├── pyproject.toml       # Required: Python packaging metadata + aq.plugins entry point
├── my_plugin/
│   ├── __init__.py      # Exports the Plugin subclass
│   └── ...
├── CLAUDE.md            # Required: plugin dev guide for agents (see §12)
├── prompts/             # Optional: default prompt templates
├── README.md            # Optional: documentation
└── ...
```

**Legacy format** (deprecated, still supported):
```
my-plugin/
├── plugin.yaml          # Metadata, capabilities, config schema
├── plugin.py            # Python entry point
├── requirements.txt     # pip dependencies
└── ...
```

### Instance Directory (install-specific, not in git)

Each installed plugin gets a separate instance directory for prompts, configuration overrides, and persistent state that should not be committed back to the plugin's git repo. This allows users to customize plugin behavior per-installation.

```
~/.agent-queue/plugins/{name}/
├── src/                 # Git clone (source directory above)
├── config.yaml          # Install-specific configuration (overrides plugin.yaml defaults)
├── prompts/             # Customizable prompt templates
│   ├── main.md          # Primary prompt (seeded from source defaults on install)
│   └── ...              # Any additional prompts the plugin defines
├── data/                # Plugin-scoped persistent storage (key/value, caches, etc.)
└── logs/                # Plugin-specific logs
```

**Prompt lifecycle:**

1. On install, default prompts from `src/prompts/` (if any) are copied to `prompts/`
2. Users or agents can edit `prompts/` freely — these are the active prompts
3. On plugin update, source prompts may change but instance prompts are **never overwritten**
4. `aq plugin reset-prompts <name>` re-copies defaults (destructive, requires confirmation)
5. `aq plugin diff-prompts <name>` shows differences between instance and source defaults

**Why separate?** The plugin source is a git repo that may be shared across installs or contributed back upstream. Install-specific prompts, API keys, custom filters, and cached state should never leak into the source repo. This also means AgentQueue agents can freely edit prompts for a plugin without creating dirty git state in the plugin source.

**PluginContext prompt API:**

```python
# Read the active (instance) prompt
prompt = ctx.get_prompt("main")

# Read with variable substitution
prompt = ctx.get_prompt("main", variables={"project": "myproj"})

# Update the active prompt (writes to instance dir, not source)
await ctx.save_prompt("main", new_content)

# List available prompts
prompts = ctx.list_prompts()  # ["main", "summary", "classification"]
```

### pyproject.toml

```toml
[project]
name = "aq-email-reviewer"
version = "1.0.0"
description = "Periodic email review and important item notifications"
authors = [{name = "david"}]
dependencies = ["httpx"]

[project.entry-points."aq.plugins"]
email-reviewer = "email_reviewer:EmailReviewerPlugin"

[build-system]
requires = ["setuptools>=78.1.1"]
build-backend = "setuptools.build_meta"
```

### Plugin class

```python
import click
from discord import app_commands
from src.plugins.base import Plugin, PluginContext, PluginPermission, cron

class EmailReviewerPlugin(Plugin):
    """Periodic email review and notification plugin."""

    # Class attributes replace plugin.yaml capability declarations
    plugin_permissions = [PluginPermission.NETWORK]
    config_schema = {
        "imap_server": {"type": "string", "required": True},
        "email_address": {"type": "string", "required": True},
    }
    default_config = {"imap_server": "", "email_address": ""}

    async def initialize(self, ctx: PluginContext) -> None:
        self.config = ctx.get_config()
        ctx.register_command("check_email", self.cmd_check_email)
        ctx.register_command("email_summary", self.cmd_email_summary)
        ctx.register_event_type("email.checked")
        ctx.register_event_type("email.important_found")

    async def shutdown(self, ctx: PluginContext) -> None:
        pass

    # --- Cron-scheduled functions (plain async Python) ---

    @cron("0 8,12,16,20 * * *")
    async def periodic_check(self, ctx: PluginContext) -> None:
        """Check email 4x daily. Can invoke LLM when needed."""
        emails = await self._fetch_emails()
        if emails:
            summary = await ctx.invoke_llm(
                f"Summarize these emails and identify action items: {emails}"
            )
            await ctx.notify(summary)
            await ctx.emit_event("email.checked", {"count": len(emails)})

    # --- CLI extension (Click group) ---

    def cli_group(self) -> click.Group | None:
        @click.group("email")
        def email():
            """Email reviewer plugin commands."""
        @email.command()
        def status():
            click.echo("Email plugin: OK")
        @email.command()
        @click.argument("query")
        def search(query):
            click.echo(f"Searching: {query}")
        return email

    # --- Discord extension (app_commands.Group) ---

    def discord_commands(self) -> app_commands.Group | None:
        grp = app_commands.Group(
            name="email", description="Email plugin commands"
        )
        @grp.command(name="check", description="Check email now")
        async def check(interaction):
            await interaction.response.send_message("Checking...")
        @grp.command(name="status", description="Email status")
        async def status(interaction):
            await interaction.response.send_message("OK")
        return grp

    # --- Command handlers ---

    async def cmd_check_email(self, args: dict) -> dict:
        emails = await self._fetch_emails()
        return {"emails": emails, "important_count": len(emails)}

    async def cmd_email_summary(self, args: dict) -> dict:
        return {"summary": "...", "action_items": []}
```

---

## 3. Plugin Lifecycle

### Installation

Plugins are installed from git repositories:

```
# Via aq CLI
aq plugin install https://github.com/user/aq-email-reviewer.git
aq plugin install git@github.com:user/aq-email-reviewer.git
aq plugin install https://github.com/user/aq-email-reviewer.git --branch dev
aq plugin install /path/to/local/plugin  # local directory (symlinked)

# Via Discord
/plugin-install url:https://github.com/user/aq-email-reviewer.git
```

Installation steps:

1. Clone the repository into `~/.agent-queue/plugins/{plugin-name}/`
2. Validate `plugin.yaml` — check required fields, schema, version compatibility
3. Install `requirements.txt` into the AgentQueue venv (if present)
4. Load and validate `plugin.py` — ensure it exports a `Plugin` subclass
5. Call `plugin.initialize(ctx)` — register commands, tools, hooks
6. Store plugin metadata in the database (`plugins` table)
7. Create hooks declared in `plugin.yaml` (owned by the plugin, tagged with `plugin_id`)

### Update

```
# Via aq CLI
aq plugin update email-reviewer          # git pull + reinstall
aq plugin update email-reviewer --rev abc123  # specific revision
aq plugin update --all                   # update all plugins

# Via Discord
/plugin-update name:email-reviewer
```

Update steps:

1. Call `plugin.shutdown()` on the running instance
2. Remove plugin-owned hooks from the database
3. `git pull` (or `git checkout <rev>`) in the plugin directory
4. Re-install `requirements.txt` if changed
5. Re-load `plugin.py` and call `plugin.initialize(ctx)`
6. Re-create hooks from updated `plugin.yaml`
7. Update metadata in database

### Self-Update via AgentQueue

A key feature: AgentQueue agents can develop and update plugins. When a task targets a plugin repository:

1. Agent works on the plugin code in a workspace (normal task flow)
2. On completion, the orchestrator detects the workspace is a plugin directory
3. Triggers `plugin.updated` event
4. Plugin is automatically reloaded (shutdown → load → initialize)

This enables the workflow: "create a task to add email filtering to the email-reviewer plugin" → agent modifies plugin code → plugin auto-reloads with new functionality.

### Removal

```
aq plugin remove email-reviewer
/plugin-remove name:email-reviewer
```

1. Call `plugin.shutdown()`
2. Remove plugin-owned hooks and hook runs from database
3. Remove plugin metadata from database
4. Delete plugin directory (or unlink if symlinked)
5. Optionally uninstall plugin-specific pip packages

### Enable / Disable

```
aq plugin disable email-reviewer
aq plugin enable email-reviewer
/plugin-toggle name:email-reviewer
```

Disabling a plugin:
- Calls `plugin.shutdown()`
- Disables all plugin-owned hooks
- Unregisters commands and tools
- Retains plugin directory and database record (status = `disabled`)

---

## 4. Plugin Registry

The `PluginRegistry` is the central coordinator. It lives in `src/plugins/registry.py`.

```python
class PluginRegistry:
    """Central registry for all loaded plugins and their capabilities."""

    # Discovery & lifecycle
    async def discover_plugins(self) -> list[PluginInfo]
    async def load_plugin(self, plugin_id: str) -> None
    async def unload_plugin(self, plugin_id: str) -> None
    async def reload_plugin(self, plugin_id: str) -> None

    # Installation
    async def install_from_git(self, url: str, branch: str | None = None) -> PluginInfo
    async def install_from_path(self, path: str) -> PluginInfo
    async def update_plugin(self, plugin_id: str, rev: str | None = None) -> PluginInfo
    async def remove_plugin(self, plugin_id: str) -> None

    # Capability queries
    def get_command(self, name: str) -> Callable | None
    def get_tools(self, category: str | None = None) -> list[dict]
    def get_all_tool_definitions(self) -> list[dict]
    def list_plugins(self) -> list[PluginInfo]
    def get_plugin(self, plugin_id: str) -> PluginInfo | None
```

### Integration Points

**CommandHandler** — after checking `_cmd_{name}`, falls back to plugin registry:

```python
async def execute(self, name: str, args: dict) -> dict:
    handler = getattr(self, f"_cmd_{name}", None)
    if handler:
        return await handler(args)

    # Check plugin commands
    if self.plugin_registry:
        plugin_handler = self.plugin_registry.get_command(name)
        if plugin_handler:
            return await plugin_handler(args)

    return {"error": f"Unknown command: {name}"}
```

**ToolRegistry** — merges plugin tools into the tool list:

```python
def get_all_tools(self) -> list[dict]:
    tools = list(self._builtin_tools.values())
    if self._plugin_registry:
        tools.extend(self._plugin_registry.get_all_tool_definitions())
    return tools
```

**HookEngine** — plugin-owned hooks are regular hooks with a `plugin_id` tag. No special handling needed — the hook engine already processes all hooks generically.

**EventBus** — plugins emit and subscribe to events freely. Plugin-declared event types are documented but not enforced (the EventBus is already freeform).

**Orchestrator initialization** — plugins load after database, before hooks:

```python
async def initialize(self) -> None:
    await self.db.initialize()
    await self.plugin_registry.discover_plugins()  # NEW
    await self.plugin_registry.load_all()           # NEW
    await self.hooks.initialize()
    # ... rest of init
```

---

## 5. Plugin Context

The `PluginContext` is the API surface available to plugins. It provides controlled access to AgentQueue capabilities without exposing internals.

```python
class PluginContext:
    """Stable API for plugins to interact with AgentQueue."""

    # Registration
    def register_command(self, name: str, handler: Callable) -> None
    def register_tool(self, definition: dict) -> None
    def register_event_type(self, event_type: str) -> None

    # Runtime
    async def emit_event(self, event_type: str, data: dict) -> None
    async def execute_command(self, name: str, args: dict) -> dict
    async def notify(self, message: str, project_id: str | None = None) -> None

    # Configuration
    def get_config(self) -> dict
    async def save_config(self, config: dict) -> None

    # Storage (plugin-scoped key/value)
    async def get_data(self, key: str) -> Any
    async def set_data(self, key: str, value: Any) -> None
    async def delete_data(self, key: str) -> None

    # Prompts (reads from instance dir, not source)
    def get_prompt(self, name: str, variables: dict | None = None) -> str
    async def save_prompt(self, name: str, content: str) -> None
    def list_prompts(self) -> list[str]

    # LLM invocation (ChatProvider, not Claude Code agent)
    async def invoke_llm(
        self,
        prompt: str,
        *,
        model: str | None = None,       # override model
        provider: str | None = None,     # "anthropic" or "ollama"
        tools: list[dict] | None = None, # tool definitions for tool-use loop
    ) -> str
```

**LLM access:** `invoke_llm()` uses the system's ChatProvider (Anthropic API or Ollama) for lightweight message-in/message-out calls. This is suitable for summarization, classification, extraction, etc. For heavy autonomous work (file editing, shell commands), plugins should create a task via `execute_command("create_task", {...})`.

Plugins do NOT get direct access to the database, orchestrator, or file system (unless granted via permissions). All interaction goes through `PluginContext`.

---

## 6. Database Schema

### plugins table

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Plugin name (from plugin.yaml) |
| `version` | `str` | Installed version |
| `source_url` | `str` | Git URL or local path |
| `source_rev` | `str` | Git commit hash of installed version |
| `source_branch` | `str` | Git branch |
| `install_path` | `str` | Path to plugin directory |
| `status` | `str` | `active`, `disabled`, `error` |
| `config` | `str` | JSON — plugin-specific configuration |
| `permissions` | `str` | JSON — granted permissions |
| `error_message` | `str` | Last error (if status = error) |
| `installed_at` | `float` | Epoch |
| `updated_at` | `float` | Epoch |

### plugin_data table

| Field | Type | Description |
|---|---|---|
| `plugin_id` | `str` | FK to plugins.id |
| `key` | `str` | Data key |
| `value` | `str` | JSON-serialized value |
| `updated_at` | `float` | Epoch |

### hooks table modification

Add `plugin_id` column (nullable) to the existing hooks table. Plugin-owned hooks are created/deleted with the plugin lifecycle.

---

## 7. CLI Commands

### aq plugin

```
aq plugin list                              # List installed plugins
aq plugin info <name>                       # Show plugin details, config, hooks
aq plugin install <url> [--branch <branch>] # Install from git
aq plugin update <name> [--rev <hash>]      # Update plugin
aq plugin update --all                      # Update all plugins
aq plugin remove <name>                     # Uninstall plugin
aq plugin enable <name>                     # Enable disabled plugin
aq plugin disable <name>                    # Disable plugin (keep installed)
aq plugin config <name> [key=value ...]     # View or set plugin config
aq plugin logs <name> [--limit N]           # View plugin hook run history
aq plugin prompts <name>                    # List plugin prompts
aq plugin edit-prompt <name> <prompt-name>  # Edit an instance prompt
aq plugin diff-prompts <name>               # Diff instance vs source defaults
aq plugin reset-prompts <name>              # Reset prompts to source defaults
```

### Discord Commands

```
/plugin-list                                # List installed plugins
/plugin-install url:<git-url>               # Install from git
/plugin-update name:<name>                  # Update plugin
/plugin-remove name:<name>                  # Uninstall plugin
/plugin-toggle name:<name>                  # Enable/disable
/plugin-config name:<name>                  # Interactive config modal
/plugin-info name:<name>                    # Show details
/plugin-prompts name:<name>                 # View/edit plugin prompts
```

---

## 8. Security & Permissions

Plugins declare required permissions in `plugin.yaml`. On install, permissions are reviewed and stored. The `PluginContext` enforces these at runtime.

| Permission | Grants |
|---|---|
| `network` | HTTP requests from context steps and plugin code |
| `filesystem` | Read/write files (scoped to plugin directory + workspace paths) |
| `database` | Direct database queries (read-only by default) |
| `shell` | Execute shell commands |

By default, all plugins get:
- Command registration
- Tool registration
- Event emission/subscription
- Plugin-scoped key/value storage
- Notifications via messaging adapter

Plugins do NOT get:
- Access to other plugins' data
- Direct database writes (use commands instead)
- Access to credentials/secrets of other plugins
- Ability to modify core behavior or other plugins

---

## 9. Error Handling

- If a plugin fails to load, it enters `status = error` with the error message stored. Other plugins and core functionality are unaffected.
- If a plugin command raises an exception, it is caught by CommandHandler and returned as `{"error": "Plugin error: ..."}`. The plugin remains loaded.
- If a plugin hook fails, it follows the normal hook failure flow (recorded in HookRun, does not block other hooks).
- If a plugin's `requirements.txt` install fails, the plugin is not loaded and enters error state.
- Circuit breaker: if a plugin's hooks fail 5 times consecutively, the plugin is auto-disabled and a notification is sent.

---

## 10. Future Work: Extracting Existing Features to Plugins

Several existing AgentQueue subsystems are good candidates for extraction into plugins. This would slim down the core and validate the plugin architecture. Listed by extraction difficulty:

### Easy Extractions

| Component | Current Location | Notes |
|---|---|---|
| **File Watcher** | `src/file_watcher.py` (419 LOC) | Self-contained, event-driven. Only integration is EventBus emit. |
| **Chat Providers (Ollama)** | `src/chat_providers/ollama.py` | Already behind factory. Extract non-default providers as plugins. |
| **Agent Adapters (future)** | `src/adapters/` | Factory pattern already in place. New LLM backends (OpenAI, local) would be plugins. |
| **Token Budget Manager** | `src/tokens/` (104 LOC) | Pure math, minimal coupling. Alternative scheduling strategies could be plugins. |

### Medium Extractions

| Component | Current Location | Notes |
|---|---|---|
| **Memory / Semantic Search** | `src/memory.py` (400+ LOC) | Good isolation, lazy loading. Needs dependency injection for task context assembly. |
| **Telegram Adapter** | `src/telegram/` (1,379 LOC) | Self-contained platform adapter. Needs formalized callback API. |

### Hard Extractions (Long-term)

| Component | Current Location | Notes |
|---|---|---|
| **Discord Adapter** | `src/discord/` (11,959 LOC) | Tightly coupled, huge. Would need significant refactoring. Core platform for now. |
| **Reflection Engine** | `src/reflection.py` | Deeply integrated with Supervisor decision loop. |

### Recommended Extraction Order

1. Start with **File Watcher** — simplest, validates the plugin loading pipeline end-to-end
2. Then **Ollama Chat Provider** — validates plugin tool/command registration
3. Then **Telegram Adapter** — validates messaging plugin pattern for future platforms (Slack, Teams)
4. Then **Memory Manager** — validates complex plugin with external dependencies

Each extraction serves as a proving ground for the plugin API surface. If the API can't cleanly support the extraction, the API needs to change before publishing it for external plugin authors.

---

## 11. Plugin CLAUDE.md (Agent Development Guide)

Every plugin **must** include a `CLAUDE.md` at its root. This file is injected into the agent's context when AgentQueue assigns a task targeting the plugin's workspace. It teaches the agent how to develop for and extend the plugin.

### Required Sections

```markdown
# {Plugin Name}

{One-paragraph description of what the plugin does.}

## Structure

{File layout with descriptions of each file's purpose.}

## Development

{How to run, test, and iterate on the plugin locally.}

### Commands Provided
{List of commands this plugin registers, their args, and return format.}

### Tools Provided
{List of tools, their schemas, and behavior.}

### Hooks / Schedules
{What hooks this plugin creates, their triggers, and what they do.}

### Events Emitted
{Event types this plugin emits and their payloads.}

## Prompt Authoring

{How prompts work in this plugin. Where defaults live (src/prompts/),
where active prompts live (~/.agent-queue/plugins/{name}/prompts/),
and how variable substitution works.}

## Configuration

{config_schema from plugin.yaml explained in plain language.
Where instance config lives. How to validate changes.}

## Testing

{How to run plugin tests. What fixtures are available.
How to test hooks and commands in isolation.}
```

### Why This Matters

AgentQueue agents are the primary developers of plugins. When a task says "add email filtering to the email-reviewer plugin," the agent needs to:
- Understand the plugin's file layout and conventions
- Know which files to edit and which to create
- Know how to register new commands/tools
- Know how to write and test hooks
- Understand the prompt system so it can add/modify prompts
- Know not to modify instance-specific files (those live outside the source)

Without a CLAUDE.md, the agent has to reverse-engineer all of this from code, which wastes tokens and produces inconsistent results.

---

## 12. Implementation Phases

### Phase 1: Core Plugin Infrastructure ✅
- `src/plugins/base.py` — Plugin ABC, PluginContext, PluginInfo
- `src/plugins/registry.py` — PluginRegistry with discover/load/unload
- `src/plugins/loader.py` — Git clone, requirements install, module import
- Database schema: `plugins` and `plugin_data` tables
- Integration: CommandHandler fallback, ToolRegistry merge, Orchestrator init

### Phase 2: Management Commands ✅
- `aq plugin` CLI subcommands (list, install, update, remove, enable, disable, config)
- Discord slash commands (/plugin-list, /plugin-install, etc.)
- Supervisor tools (list_plugins, install_plugin, etc.)

### Phase 3: Self-Update Flow ✅
- Detect when an agent task targets a plugin workspace
- Auto-reload plugin on task completion
- `plugin.updated` event emission

### Phase 4: pyproject.toml + Cron + CLI/Discord Extension ✅
- Plugin metadata from `pyproject.toml` (via `importlib.metadata` + entry points)
- `pip install -e .` instead of `requirements.txt`
- `@cron` decorator for scheduled plain async Python functions
- `PluginContext.invoke_llm()` for LLM access from cron jobs and handlers
- `Plugin.cli_group()` for extending the `aq` CLI with Click groups
- `Plugin.discord_commands()` for extending Discord with `app_commands.Group`
- Reserved plugin name validation to prevent CLI/Discord collisions
- Class attributes (`plugin_permissions`, `config_schema`, `default_config`) replace
  plugin.yaml capability declarations
- Legacy `plugin.yaml` still supported with deprecation warning

### Phase 5: First Plugin Extraction
- Extract File Watcher as a plugin (proves the architecture)
- Document plugin authoring guide
- Publish plugin template repository
