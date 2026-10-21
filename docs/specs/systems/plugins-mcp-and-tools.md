# System spec: plugins-mcp-and-tools

Status: describes the system as of 2026-10-20 (base `22cffd1b7`). This is system 18 of the
eighteen in the agent-queue system inventory (vault
`projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`, review
`rev-bright-rapids`, approved at revision 1 when this was written). It explains what the
system is for, how it works today, where it stops, and what is planned. It links to the
existing guide, reference and design documents rather than repeating them.

## 1. Purpose

Agent Queue has one command surface: every state change is a `CommandHandler` command. This
system owns the ways that surface is extended and the ways an agent finds out what is on
it. It answers four questions:

- **How does the daemon gain capabilities without a core change?** Through plugins:
  Python packages that register commands, tools, services, doctor checks and CLI groups
  against a narrow context object.
- **How do outside tools reach AQ, and AQ reach outside tools?** Through MCP, in two
  directions. AQ serves its own commands as an MCP endpoint. Operators register external
  MCP servers in the vault and name them in profiles.
- **How does any caller learn what a command takes?** Through the tool catalogue: one
  table of JSON-schema tool definitions that the CLI, MCP and the API client generator
  all read.
- **How does an agent learn how to use the surface?** Through the eight shipped `aq-*`
  skills, short task-shaped guides installed into each harness's skill directory.

The inventory merged the tool catalogue and the skills into this system because both
describe how an agent discovers the surface.

It has five parts:

- **Plugins.** The `Plugin` contract and `PluginContext`, the registry that discovers,
  loads, isolates and unloads plugins, four internal plugins that are always loaded, and
  the external `aq-memory` plugin.
- **AQ as an MCP server.** The embedded streamable-HTTP server at `/mcp`, which turns
  commands, resources and prompts into MCP capabilities.
- **External MCP servers.** The vault-backed registry, the probe that lists each server's
  tools, the in-memory tool catalog, and the profile `## MCP Servers` block.
- **The tool catalogue.** `src/tools/definitions.py`, the `ToolRegistry` and its tiers,
  the semantic `ToolIndex`, the generated `command_catalogue.json` and the generated CLI
  inventory.
- **Agent skills and harness tool names.** `src/skills/`, their installer and drift
  check, and `src/known_tools.py`, which names the harness tools a profile may allow.

The problems it solves are:

- **Extending without forking.** A capability that is not core (a static analyser, an
  email poller, a memory store) ships as a plugin with its own dependencies and its own
  lifecycle, and a failing plugin is disabled instead of taking the daemon down.
- **One schema per command.** The CLI tree, the MCP tool list and the typed API client
  are generated from the same definitions, so a flag cannot exist on one surface and be
  missing from another without a drift test failing.
- **A CLI that works with the daemon down.** `aq --help` builds its whole tree from a
  committed JSON catalogue, without importing the handler.
- **Teaching agents the current surface.** Skills and prime text tell an agent which
  command to run, and a test checks every `aq …` line in them against the live CLI.

## 2. How it works today

### 2.1 Vocabulary

- **Plugin.** A Python package that subclasses `Plugin` (`src/plugins/base.py`) and
  implements `initialize(ctx)` and `shutdown(ctx)`. It is found through the `aq.plugins`
  entry-point group, or through a deprecated `plugin.yaml` plus `plugin.py`.
- **Internal plugin.** A plugin that ships in `src/plugins/internal/`, subclasses
  `InternalPlugin`, is named `aq-<module>`, has no database row and is always loaded.
- **External plugin.** A plugin installed from git or a local path into
  `~/.agent-queue/plugins/<name>/`, with a `plugins` row and data under
  `~/.agent-queue/plugin-data/<name>/`.
- **Trust level.** `INTERNAL` or `EXTERNAL` (`TrustLevel`, `base.py:100-104`). It decides
  which services and which parts of the context a plugin can reach.
- **Service.** A typed facade (git, db, workspace, config, memory, vault watcher and
  others, `src/plugins/services.py`) handed to a plugin, or registered by one.
- **Tool definition.** A plain dict `{name, description, input_schema}`. A tool's name is
  the command name it runs.
- **Core tool / category.** A tool listed in `_TOOL_CATEGORIES` belongs to that category;
  any tool not listed is core.
- **Registry entry (MCP).** A vault markdown file that describes one external MCP server:
  stdio command or HTTP URL, environment, headers and notes.
- **Tool catalog (MCP).** The in-memory snapshot of the tools each registry entry offers,
  built by probing it. Not the same thing as the command catalogue below.
- **Command catalogue.** `src/tools/command_catalogue.json`, the generated, committed
  projection of the tool definitions that the offline CLI reads.
- **`aq://` reference.** A portable, read-only path reference with five authorities
  (`prompts`, `vault`, `logs`, `tasks`, `attachments`). It is a path convention, not an
  MCP resource scheme; the MCP resources use `agentqueue://`.
- **Skill.** A `SKILL.md` under `src/skills/<name>/` that a harness loads on demand.

The word "plugin" also means two other things in this repository. **Harness plugins**
(winnow, fast-jev, superpowers) are Claude Code or Codex plugins that a harness loads; they
belong to sessions-and-harnesses, and their value by role is assessed in the vault report
`specs/2026-10-02-plugin-value-by-role-winnow-fast-jev-and-superpowers.md` (review
`rev-nimble-torrent`, approved at revision 1). **Dashboard panes** have a pane manifest
contract (`dashboard/src/panes/`, design
[pane plugin interface](../../superpowers/specs/2026-08-22-pane-plugin-interface-design.md));
they belong to the dashboard and do not touch `src/plugins/`. Neither is covered here.

### 2.2 The plugin contract

A plugin sees the daemon only through `PluginContext` (`base.py:185-680`). Through it a
plugin can:

- **Register commands.** `register_command("hello", fn)` stores the command under both
  `hello` and `<plugin>.hello` (`base.py:325-342`). `CommandHandler.execute` tries a
  built-in `_cmd_<name>` first and falls back to the plugin registry
  (`src/commands/handler.py:1126-1175`), so a plugin command goes through the same
  authorization and correlation as a built-in one.
- **Register tools.** `register_tool(schema, category=...)` tags the schema with its plugin
  and category (`base.py:377-399`). The tool registry, the MCP server and the
  orchestrator's MCP catalogue merge plugin tools in. A plugin tool whose name matches a
  built-in command is dropped with a warning (`src/tools/registry.py:427-457`).
- **Register other things.** Doctor checks are prefixed `plugin.<name>.`
  (`base.py:346-373`). Services that core can read with `get_service` are registered with
  `register_service` (`base.py:418-442`). Event types are informational. A Click group is
  returned from `cli_group()` and mounted under `aq` from the entry point
  (`src/cli/app.py:623-686`). Discord command groups are also possible.
- **Call out.** `emit_event` and `subscribe` on the event bus, `execute_command` back into
  the handler, `notify`, and `invoke_llm`, which reaches the providers system's direct
  path (`base.py:444-527`).
- **Keep state.** Config with a schema and defaults, stored as JSON on the `plugins` row
  (`load_config`, `save_config`, `set_config_value`), and a key-value store in
  `plugin_data` (`get_data`, `set_data`, `delete_data`). Prompt templates live under the
  plugin's `prompts/` directory and can be diffed and reset from the CLI.
- **Schedule.** Methods decorated `@cron(expr)` run from the orchestrator cycle
  (`registry.py:1341-1403`, called from `src/orchestrator/core.py:3541`). A run is skipped
  while the previous one is still going. No internal plugin uses `@cron` any more; their
  schedules moved to playbooks, and `tests/test_plugin_cron_to_playbook_migration.py`
  pins that.

**Isolation.** An external plugin can get only the `config` and `vault_watcher` services
(`EXTERNAL_ALLOWED_SERVICES`, `base.py:110-113`); asking for any other raises
`PermissionError`. Reading `ctx.bus` directly needs internal trust. Permissions
(`NETWORK`, `FILESYSTEM`, `DATABASE`, `SHELL`) are declared metadata; nothing enforces
them. A plugin runs in the daemon's process with the daemon's rights.

**Failure handling.** If `initialize` raises, the plugin is set to `ERROR` and the error is
logged; other plugins still load. A command or cron failure counts against a circuit
breaker: after five consecutive failures (`MAX_CONSECUTIVE_FAILURES`, `registry.py:67`)
the plugin is set to `ERROR` and `plugin.auto_disabled` is emitted.

### 2.3 Discovery, loading and lifecycle

`PluginRegistry` (`src/plugins/registry.py`) is built in `Orchestrator` startup
(`core.py:2519-2571`). `load_all` runs in this order:

1. **Internal plugins.** `src/plugins/internal/__init__.py:27-56` scans the direct child
   modules of the package for `InternalPlugin` subclasses. Each one gets an internal
   context and no database row. One that fails is logged and skipped.
2. **External plugins.** `discover_plugins` creates a `plugins` row for any install
   directory that lacks one. Each non-disabled row is then loaded:
   - the legacy-memory guard runs first (§2.4);
   - a missing install directory is re-cloned from the row's `source_url`;
   - the plugin class is found by entry point, then by distribution name, then by
     `plugin.py`;
   - config is loaded, `initialize` runs, `@cron` methods are collected;
   - the row is set `ACTIVE` and `plugin.loaded` is emitted.

Install, update and remove go through `install_from_git`, `install_from_path` (a symlink,
for development), `update_plugin` and `remove_plugin`. Names that collide with core
commands or reserved words are refused before and after cloning
(`registry.py:71-91`). `unload_plugin` removes every command key, alias, tool, cron job
and service the plugin registered. Each step emits a lifecycle event (`plugin.installed`,
`.updated`, `.removed`, `.loaded`, `.unloaded`, `.auto_disabled`;
`src/event_schemas.py:530-554`).

There are two operator paths. Daemon commands (`plugin_list`, `plugin_install`,
`plugin_enable`, `plugin_reload`, `plugin_config_*` and others,
`src/commands/plugin_commands.py`) act on the live registry. The `aq plugin` CLI
(`src/cli/plugins.py`) writes the `plugins` table directly through a database client
instead, and `enable`, `disable` and `update` take effect at the next restart. `aq plugin
logs` reports that per-plugin hook history was removed.

### 2.4 Shipped and external plugins

| Plugin | Provides | Notes |
|---|---|---|
| `aq-files` (`internal/files.py`) | `read_file`, `write_file`, `edit_file`, `glob_files`, `grep`, `search_files`, `list_directory`, project-memory file reads, file-inspection records | Tools in the `files` category. |
| `aq-git` (`internal/git.py`) | Twenty git commands: status, commit, pull, push, branch, merge, `create_pr`, log, diff, checkout, remote, README generation and others | Commits run under the project's resolved identity. A worker session is locked to its own worktree. The module docstring says nineteen. |
| `aq-notes` (`internal/notes.py`) | `list_notes`, `write_note`, `read_note`, `append_note`, `delete_note`, `promote_note`, `compare_specs_notes` | Uses the memory service if one is registered. |
| `aq-vibecop` (`internal/vibecop.py`) | `vibecop_scan`, `vibecop_check`, `vibecop_status` | Subscribes to `task.completed`; its weekly scan is a playbook now. |
| inbox (`internal/inbox/`) | Gmail poller, sender allowlist, `inbox_status` and allowlist commands | **Not loaded.** It is a package, and discovery reads only direct child modules. `tests/test_inbox_plugin.py` tests it in isolation. |
| `aq-memory` (external) | The `memory` service: semantic search, facts, KV, documents | Separate repository and not bundled. **Not loaded today** (below). |

**aq-memory today.** Boot calls `load_all(skip=LEGACY_MEMORY_PLUGINS)`, where that set is
`{"aq-memory", "memory"}` (`src/knowledge/legacy.py:9`, `core.py:2551-2566`). The log line
says the legacy memory writer is excluded until a versioned read-only adapter handshake
exists (work the knowledge layer calls K07), so the plugin is not loaded even when
installed. Its data and its database row are preserved. While the `memory` service is
absent:

- the facts handlers are not wired;
- `memory_*` commands answer "paused" when `memory.enabled` is false, or "unknown command"
  with an install hint otherwise;
- the memory tool schemas stay declared in core, so the `aq memory` group is still
  visible.

The knowledge layer reaches memory only through `MemorySemanticPort`
(`src/knowledge/memory_port.py`), which never imports the plugin. The pause is described in
[feature pauses](../design/feature-pauses.md); the knowledge graph itself belongs to
reviews-and-knowledge.

### 2.5 AQ as an MCP server

`run_mcp_server` (`src/embedded_mcp.py:35`) starts from `src/main.py:536-552` when
`mcp_server.enabled` is set. It runs inside the daemon process and reuses the daemon's
database, event bus, orchestrator and `CommandHandler`. It mounts a `FastMCP` app at `/` on
the FastAPI application, so the endpoint is `http://<api host>:<api port>/mcp`
(default `127.0.0.1:8081`). The transport is streamable HTTP only. A supervised loop
restarts the server with a backoff from one to thirty seconds, replacing the mount rather
than adding a second one. A failure to start does not stop the daemon.

`register_command_tools` (`src/mcp_registration.py`) registers tools in three passes:

1. every explicit definition in `_ALL_TOOL_DEFINITIONS`;
2. every plugin tool;
3. every remaining `_cmd_*` method found by introspection, using its typed contract's
   schema if it has one, then a fallback schema, then an empty object.

Each tool's handler strips `_`-prefixed arguments and calls `CommandHandler.execute`, which
does all argument validation and authorization. Excluded commands are the union of
`DEFAULT_EXCLUDED_COMMANDS` (`shutdown`, `restart_daemon`, `update_and_restart`,
`run_command`, `load_tools`, `session_token`, `subagent_event` and others,
`mcp_registration.py:51-92`), `mcp_server.excluded_commands` and the
`AGENT_QUEUE_MCP_EXCLUDED` environment variable.

It also serves `agentqueue://` resources (tasks, projects, agents, profiles, recent events,
workspaces) and three prompts (`create_task_prompt`, `review_task_prompt`,
`project_overview_prompt`).

**Who an MCP caller is.** The API's `TokenAuthMiddleware` covers `/mcp`. A bad bearer
token is refused with 401; no token is accepted unless `api_auth.require_session_token` is
on, which it is not by default. The MCP tool handler does not forward the resolved scope
to `execute`, so every MCP call runs as a trusted local principal, whatever token was
sent. The three `question_*` commands are the exception: they are given a fixed
`{"kind": "mcp"}` scope so an MCP client cannot claim a human identity. In practice the
endpoint is an operator surface on loopback. A task-scoped endpoint, `/mcp-task`, is
configured (`mcp_server.task_scope`) but not served; `src/config.py:1187-1210` says
nothing reads those fields until the aq-surface Phase 3 flip
([aq-surface design](../design/aq-surface.md),
[implementation](../implementation/aq-surface.md)).

### 2.6 External MCP servers for workers

An operator registers a server with `aq mcp create-server`. The entry is written to
`vault/mcp-servers/<name>.md` (system scope) or
`vault/projects/<id>/mcp-servers/<name>.md` (project scope, which shadows system scope)
(`src/profiles/mcp_registry.py:9-53`). There is no database table. The vault watcher
reloads entries on change and keeps the previous entry if a file fails to parse. The
synthetic `agent-queue` entry, which points at AQ's own endpoint, is added in memory when
`mcp_server.inject_into_tasks` is on; it cannot be edited or removed.

The in-memory tool catalog (`src/profiles/mcp_catalog.py`) is filled at startup by probing
every entry in parallel (`src/profiles/mcp_probe.py`): connect over stdio or HTTP, run
`initialize` and `list_tools`, give up after ten seconds. The builtin `agent-queue` entry
is resolved in process instead of probed. `aq mcp probe-server` refreshes one entry, and
`aq mcp list-tool-catalog` shows the result.

A profile names the servers it wants in its `## MCP Servers` block. `aq mcp delete-server`
refuses to delete a server any profile still names. Old profiles that carried inline
server definitions are converted once by `src/profiles/mcp_inline_migration.py`, which
writes registry files and never overwrites an existing one.

**What happens at launch.** Nothing yet. Sessions start without `--mcp-config`
(`src/sessions/spec.py:945-949`); no launch path resolves `profile.mcp_servers` into a
harness configuration, and `TaskContext.mcp_servers` is never filled. Workers reach AQ
through the `aq` CLI over `Bash`, and reach external tools only if their harness is
configured outside AQ. The registry, probe and catalog work and are tested, but a profile
grant changes nothing about what a session can call. The only code that writes harness MCP
files is the graft installer (`src/install/graft.py`).

### 2.7 The tool catalogue

`src/tools/definitions.py` holds `_ALL_TOOL_DEFINITIONS`, 368 hand-written tool definitions
at this base, as plain dicts with no heavy imports. Alongside it sit:

- `_TOOL_CATEGORIES`, which places 392 tool names in 37 categories;
- `_CLI_CATEGORY_OVERRIDES`, which moves 19 commands to a different CLI group;
- `_FALLBACK_INPUT_SCHEMAS`, for commands deliberately left out of the master list.

The tail of the module also projects a few definitions from typed contracts and Pydantic
models (knowledge, records, decisions, operator, batch and spec ingestion, ask;
`definitions.py:9208-9364`).

Three readers consume it:

- **`ToolRegistry`** (`src/tools/registry.py`) splits tools into core (always loaded) and
  categories (loaded on demand), adds the navigation stubs `load_tools` and
  `reply_to_user`, and can compress schemas for small-context models. Eight explicit tools
  are core: `list_tasks`, `create_task`, `get_task`, `edit_task`, `find_applicable_tool`,
  `task_handoff`, `provider_usage_probe` and `job_submit_integration`.
- **The CLI.** `scripts/generate-command-catalogue.py` renders
  `src/tools/command_catalogue.json`: the explicit definitions, 125 fallback entries for
  discovered commands without one, the categories, the CLI overrides and the four internal
  plugin categories. `src/cli/auto_commands.py` builds the generated part of the `aq` tree
  from that file, so the CLI works with the daemon down
  (`tests/test_cli_startup_offline.py`). The full tree is then written to
  `docs/reference/cli-command-inventory.json`.
- **MCP and the API client.** `mcp_registration.py` registers from the same definitions
  (§2.5). `src/api/codegen.py` reads them to group operations for `openapi.json` and the
  generated clients.

Where a command has both a hand-written definition and a typed contract, the hand-written
definition wins on every transport (`effective_tool_definitions`,
`mcp_registration.py:236-256`). At this base 128 commands have both, and for 14 of them the
property sets differ (for example `create_task` and `message_send`). `execute` does not
validate arguments against the contract; only handlers that call `model_validate`
themselves do.

**Finding a tool.** `find_applicable_tool` searches `ToolIndex`
(`src/tools/tool_index.py`), an in-memory embedding index over `name: description` built
after plugins load. Without an embedding backend it is empty and the command falls back to
keyword overlap. `load_tools` returns the tool names of a category, filtered by what the
caller's capabilities allow (`src/commands/tool_commands.py:60-137`). It is excluded from
both CLI and MCP; the chat loop that used to add loaded schemas to a model's tool set is
gone, so today an agent discovers commands through `aq … --help`, `aq schema`,
`find_applicable_tool` or `/api/tools`.

**Publishing to a caller.** Every published tool list is filtered twice. Dispatchability
drops names with no handler. Capability policy (`command_allowed` in
`src/commands/authorization.py`) drops names the caller's profile does not grant, without
a message. The same policy refuses the command again at dispatch. The
[agent-facing tools reference](../../reference/cli/agent-tools.md) describes both filters.

### 2.8 Generated artifacts

`scripts/regenerate-generated.sh` regenerates every artifact `.gitattributes` marks
`merge=aq-generated`. The command catalogue is built first because the CLI inventory reads
it. This system's artifacts are `src/tools/command_catalogue.json` and
`docs/reference/cli-command-inventory.json`; `openapi.json` and `packages/aq-client/` are
shared with api-and-clients. `--check` regenerates in a scratch copy and fails on any
difference. On a merge conflict in one of them, take either side and regenerate; never
hand-merge ([codegen](../../contributing/codegen.md)).

### 2.9 Skills and harness tool names

Eight skills ship in `src/skills/`: `aq-cli`, `aq-comms`, `aq-knowledge`,
`aq-mini-projects`, `aq-playbooks-and-gates`, `aq-reviews`, `aq-tasks` and
`aq-workspaces-and-git`. Each is one `SKILL.md` with `name`, `description` and
`allowed-tools: [Bash]` frontmatter.

`ensure_default_aq_skills` (`src/vault.py:1686-1780`, called from vault setup at `:1393`)
copies them into `~/.claude/skills`, and also into `~/.gemini/skills`,
`~/snap/gemini-cli/common/.gemini/skills` and `~/.codex/skills` if those home directories
exist. It runs when the vault is set up, not per session. Copies are write-if-absent: an
installed `SKILL.md` is never overwritten. `aq doctor --check skills.installed_drift`
(`src/doctor/skill_checks.py`) names installed copies that differ from the shipped ones,
and `--fix` saves a `SKILL.md.bak` and re-copies.

Prime's tool guidance (`src/prime/templates/tool_guidance.md`) tells agents to find skills
in their harness catalogue. `tests/test_guidance_docs.py` parses every `aq …` invocation in
the skills, prime templates and selected documents and checks it against the live Click
tree, so a renamed subcommand or removed flag fails the suite.

`src/known_tools.py` lists the harness tool names (`Read`, `Write`, `Edit`, `Bash` and the
rest) that a profile's harness-tool allowlist may name. Unknown names are warned about,
not refused. It also parses a profile's `install:` manifest (npm, pip and command checks)
for `aq agent check-profile` and `aq agent install-profile`.

### 2.10 Data model

| Store | Holds | Writer |
|---|---|---|
| `plugins` table | one row per external plugin: version, source URL and revision, install path, status, config JSON, permissions, error message | plugin registry, `aq plugin` CLI |
| `plugin_data` table | per-plugin key-value data, keyed `(plugin_id, key)` | plugins through `PluginContext` |
| `~/.agent-queue/plugins/`, `plugin-data/` | external plugin source and data | registry install and remove |
| `vault/[projects/<id>/]mcp-servers/*.md` | external MCP registry entries | `aq mcp create-server`, `edit-server`, the inline migration, operators |
| profile `## MCP Servers`; `agent_profiles.mcp_servers` | the server names a profile wants | profile sync |
| MCP tool catalog | probed tool lists per server | in memory only |
| `ToolIndex` | tool embeddings | in memory only, rebuilt at start |
| `src/tools/command_catalogue.json`, `docs/reference/cli-command-inventory.json` | generated projections | generators only |
| `~/.claude/skills/` and the other harness skill directories | installed skill copies | `ensure_default_aq_skills`, doctor `--fix` |

The table definitions are in `src/database/tables.py:2448-2473`.

### 2.11 Surfaces

- **CLI:** `aq plugin install|list|info|config|enable|disable|reload|update|remove|prompts|
  diff-prompts|reset-prompts|logs`; `aq mcp list-servers|get-server|create-server|
  edit-server|delete-server|probe-server|list-tool-catalog`; `aq schema`;
  `aq agent list-available-tools|check-profile|install-profile`.
- **Commands:** `src/commands/plugin_commands.py`, `mcp_commands.py`, `tool_commands.py`
  (`load_tools`, `find_applicable_tool`), plus every command a plugin registers.
- **MCP:** `/mcp` on the API port.
- **API:** `/api/tools` publishes the caller's filtered tool list
  (`src/api/execute.py:113-149`).
- **Doctor:** `skills.installed_drift`, and any `plugin.<name>.*` check a plugin registers.
- **Config:** `mcp_server:` (`enabled`, `host`, `port`, `inject_into_tasks`,
  `excluded_commands`, `task_scope`), and per-plugin config on the `plugins` row. See
  [config](../config.md).

The module catalogue is [plugins](../../reference/modules/plugins.md); the operator guide is
[plugins, MCP and extensions](../../guides/plugins-and-mcp.md).

## 3. Boundaries

| System | What crosses the boundary |
|---|---|
| daemon-core | `Orchestrator` builds the plugin registry, MCP registry and tool index at start and ticks plugin cron each cycle. `CommandHandler.execute` falls back to plugin commands and does all authorization for MCP calls. |
| task-graph | Most tool definitions describe task commands; this system only publishes their schemas. |
| scheduling-and-claims | None directly. Plugins may read claims through commands. |
| routing-and-profiles | Profiles carry the `## MCP Servers` names, the harness-tool allowlist checked against `known_tools`, the `install:` manifest and the `## Capabilities` grants that filter published tools. Capability policy is owned there and in daemon-core. |
| sessions-and-harnesses | Harnesses read the installed skills from their skill directories. Session launch is where profile MCP grants would be applied and today are not. Prime renders the tool guidance. Harness plugins (winnow, superpowers) belong there. |
| projects-workspaces-git | `aq-git` and `aq-files` wrap `GitManager` and the workspace service, locked to the session's worktree. |
| integration-and-delivery | `aq-git` exposes PR and merge commands; delivery itself does not call plugins. |
| playbooks | Playbook steps run commands, including plugin commands, through the same handler. The compiler is the intended consumer of `aq://` references. The command descriptor work (§6) is owned by the playbooks refactor. Plugin `@cron` schedules moved to playbooks. |
| messaging-and-human-asks | The inbox plugin would emit email events into messaging; the `question_*` commands get a fixed MCP scope. |
| supervisors-and-policy | The supervisor prompt still describes `load_tools`; supervisors use the same CLI as workers. |
| reviews-and-knowledge | Owns the knowledge graph and memory policy. `aq-memory` is an external plugin behind `MemorySemanticPort`, excluded until the K07 handshake. |
| providers | Plugins reach the direct LLM path through `invoke_llm`. |
| configuration-and-vault | The `mcp_server:` config, the vault-backed MCP registry and its watcher, and the write-if-absent seeding of shipped content. |
| api-and-clients | `/mcp` is mounted on the API app and covered by its middleware. `/api/tools` and `src/api/codegen.py` read the tool definitions; `openapi.json` and the clients are generated from them. |
| dashboard | None. Pane manifests are a separate contract. |
| test-service-and-jobs | Generated-artifact and guidance tests run there. |
| operations-and-recovery | `aq doctor` runs plugin checks and `skills.installed_drift`; the CLI is the operator surface. |

What this system does not own: what any command does (each owning system), who may run it
(capability policy in routing-and-profiles and daemon-core), the API transport
(api-and-clients), harness launch (sessions-and-harnesses) or the knowledge store
(reviews-and-knowledge).

## 4. Principles and invariants

- **Plugins extend through commands.** A plugin adds commands and tools; it does not get a
  private path around `CommandHandler`. Its commands are authorized and correlated like
  built-ins.
- **Built-ins win.** A plugin can never shadow a built-in command or tool name; the clash
  is dropped with a warning, and reserved names are refused at install.
- **Least privilege by trust, not by permission flag.** External plugins get two
  services. Declared permissions are documentation; the trust level is the boundary.
- **A failing plugin never stops the daemon.** Load errors, initialize errors and repeated
  command failures disable that plugin and leave the rest running.
- **Plugins own their dependencies.** A plugin such as `aq-memory` that needs a vector
  store ships it; core declares none of it and works without it.
- **One table of tool schemas, generated everywhere.** The CLI tree, the MCP list and the
  API client come from `definitions.py`. Generated files are regenerated, never
  hand-edited or hand-merged.
- **The CLI is usable offline.** Building `aq --help` must not import the handler.
- **Publication is filtered, dispatch is refused.** A caller only sees commands it may
  run, and the handler checks again, so a filtered list is a convenience, not the guard.
- **MCP excludes daemon control.** Shutdown, restart, update, arbitrary shell and token
  minting are never MCP tools.
- **The MCP registry lives in the vault.** Server definitions are operator-readable
  markdown; scope is project before system; builtins are synthesized and immutable.
- **Shipped skills are write-if-absent.** An operator's edits survive upgrades; drift is
  reported by doctor, not silently overwritten.
- **Guidance is tested against the CLI.** Every `aq …` line in a skill or prime template
  must parse against the live Click tree.

## 5. Known gaps

**Documentation drift.**

- The [plugins and MCP guide](../../guides/plugins-and-mcp.md) says that at task launch the
  harness receives the profile's MCP server configuration (`:50-53`, and the "Select a
  server for a worker" row at `:124`). Nothing does that (§2.6).
- [MCP server spec](../mcp-server.md) is a historical record, but several statements are
  plainly wrong now: stdio transport, a separate process with its own orchestrator,
  definitions in `src/tools/registry.py`, `src/command_handler.py`, the
  `AQ_MCP_EXCLUDED_COMMANDS` variable, hook tools, and per-task injection of the
  `agent-queue` entry.
- [Tiered tools](../tiered-tools.md) describes six categories, a `browse_tools` core tool
  and a `ChatAgent` loop. There are 37 categories, no `browse_tools`, and no chat loop.
- The [agent-facing tools reference](../../reference/cli/agent-tools.md) says six skills ship
  and that they install at session start. Eight ship, `aq-knowledge` and
  `aq-mini-projects` are missing from its table, and installation happens at vault setup.
- Prime's tool guidance names five of the eight skills; `aq-comms`,
  `aq-playbooks-and-gates` and `aq-mini-projects` are not mentioned.
- [Memory plugin v2](../design/memory-plugin.md) is still marked Draft and does not mention
  that the plugin is excluded pending the K07 handshake.
- [Platforms design](../../superpowers/specs/2026-04-25-platforms-implementation-design.md)
  is fully superseded: there is no `src/platforms/`, and every agent is a tmux harness.
- Docstrings are stale: `registry.py` says about ten core tools and eighty definitions,
  `vault.py:1689` says about 215 MCP tools, and `git.py` says nineteen commands.

**Behaviour.**

- **MCP calls are all trusted local.** The bearer token is checked but its scope is not
  passed to `execute`, so a worker token sent to `/mcp` gets operator rights. No test
  covers MCP authentication or principal scoping. Loopback binding is the only guard.
  The planned `/mcp-task` endpoint is not served.
- **Profile MCP grants are not applied.** The registry, probe and catalog work, but no
  session receives a server configuration. `inject_into_tasks` only controls whether the
  builtin registry entry exists.
- **Two schema sources, nothing comparing them.** Hand-written definitions override typed
  contracts on every transport; 14 commands disagree on their fields, and `execute` does
  not validate against either.
- **The MCP tool catalog omits discovered commands.** `build_agent_queue_tools` mirrors only
  passes 1 and 2 of registration, so commands exposed through pass 3 are missing from
  `aq mcp list-tool-catalog`'s `agent-queue` entry.
- **The inbox plugin is never loaded.** It is a package and discovery reads direct
  modules only.
- **`aq-memory` is never loaded,** by design until K07, so semantic memory, L1/L2 prompt
  tiers and reflection that depend on it are off.
- **The `aq plugin` CLI bypasses the daemon.** It writes the `plugins` table directly, so
  its changes do not go through `CommandHandler` and wait for a restart. CLI `reload`
  understands only `plugin.yaml` plugins.
- **Plugin lifecycle defects.** `on_config_changed` is never called after a config change;
  `enable_plugin` overwrites the `ACTIVE` status that loading just wrote; internal plugins
  are not exempt from the circuit breaker, although the docstring says so; and `subscribe`
  skips the trust check that guards `ctx.bus`.
- **Plugin permissions are not enforced** (§2.2).
- **`aq://` rewriting has no caller.** `rewrite_aq_uris` and `is_aq_uri` are unused; only
  the authority roots are used, by `read_prompt`, prime and the graph validator. Tool
  descriptions still say the compiler rewrites references.
- **Tiered loading has no consumer.** `get_core_tools`, `get_tool_index` and
  `search_relevant_categories` are called only from tests. The core tier includes
  `provider_usage_probe`, `job_submit_integration` and `task_handoff` only because they
  are missing from `_TOOL_CATEGORIES`, and no test pins the exact core set.
- **`find_applicable_tool` is not capability-filtered,** unlike `load_tools`.
- **Duplicated lists.** The skill target directories exist in both `vault.py` and
  `skill_checks.py`. The `ask_*` commands group as `ask` in the CLI and `review` in the
  API.
- **Skill fixes need an operator.** Write-if-absent means a corrected skill reaches an
  existing install only through doctor `--fix`. Skill text is checked against the CLI
  tree but not against worker scope or profile grants.
- **Smaller loose ends.** `mcp_server.enabled` defaults to true in the dataclass but false
  in the loader (`src/config.py:5253`). The MCP registry's vault-watcher handlers are
  registered twice. `agent_profiles.mcp_servers` defaults to `"{}"` although the value is a
  list. The `mcp_interfaces` dataclasses and `KNOWN_MCP_NAMES` are unused. Header and
  environment values in registry files are not `${VAR}`-expanded, and are stored in
  plain vault markdown.

## 6. Planned work

Open tasks were re-queried on 2026-10-20. No open epic targets this system, and nothing
open addresses the MCP scope gap, profile MCP grants, the inbox plugin or the plugin
lifecycle defects in §5. What is in flight or open:

- **Command descriptors (`bright-grove-21.1`, A1, with A2 `bright-grove-21.2`).** Part of
  the playbooks reusable-building-blocks epic `bright-grove-21`, from the vault spec
  `specs/2026-10-12-implementation-playbooks-reusable-building-blocks.md` (review
  `rev-smart-flare`, approved at revision 1). The tasks are complete on their branches,
  delivered through `agile-stone-80`, but the work is not on dev at this base:
  `src/commands/descriptors.py` does not exist at `22cffd1b7`. When it lands it adds one
  immutable `CommandDescriptor` snapshot beneath contracts, MCP, the API and the CLI.
  `src/mcp_registration.py`, `src/tools/command_catalogue.py`, `src/api/codegen.py` and
  the generated command documentation consume it. It keeps today's precedence (explicit
  definition over typed contract) but records every disagreement in a descriptor audit,
  which makes the 14-command drift in §5 visible. It adds a CI invariant that every
  discovered command outside an exact deny-list is covered. Snapshots are rebuilt on
  plugin load and unload. Consolidating onto one schema is left to later work with its own
  compatibility evidence.
- **Held ingestion batch tools (`calm-orbit-69.3`, DEFINED).** Under `calm-orbit-69`
  (IN_PROGRESS), from the vault spec
  `specs/2026-10-13-spec-ingestion-with-task-structure-review-before-release.md` (review
  `rev-bright-meadow`, approved at revision 2). It adds tool definitions for `aq batch
  show|validate|release|review-record|update` to `src/tools/definitions.py`, so they reach
  MCP and the CLI. It also adds two doctor checks, `ingestion.held_batches` and
  `ingestion.legacy_proposals`. The recovery mechanisms come from `sharp-cascade-53.2`
  (PAUSED).
- **Skill and guidance text.**
  - `bold-zenith-11.22` (READY), in the waits and hibernation epic, rewrites skills and
    prime text to state only the wait condition and regenerates the inventory and clients.
  - `calm-grove-14` (READY, H09) removes duplicate human publishers. It updates
    `aq-reviews` and `aq-comms` and the generated command, API and catalogue artifacts, and
    documents the write-if-absent reseed.
  - `vivid-rapids-36` (READY, H09-OPS) records the installed `skills.installed_drift` and
    `profiles.system_drift` results on this box.
  - `stark-willow-95.17` (DEFINED, S4) aligns skills, prime and worker templates with the
    shipped policy and reports installed-skill drift without overwriting it.
- **Adjacent.** `clear-impact-72` (IN_PROGRESS) evaluates every shipped playbook for a
  rewrite onto the building blocks; `wise-orbit-24` and `agile-vault-38` (blocked) affect
  playbook activation and scripts. None of them changes plugins or MCP, but they are the
  main callers of plugin commands from playbooks.
- **Designed, not scheduled.** `/mcp-task`, the task-scoped MCP endpoint with an
  allowlist, is Phase 3 of the [aq-surface design](../design/aq-surface.md); its config
  substrate exists and nothing reads it. The K07 adapter handshake that would let
  `aq-memory` load again belongs to the knowledge rollout in reviews-and-knowledge.

The candidates for filing are:

- forwarding the authenticated scope through the MCP handler, with a test;
- either applying profile MCP grants at launch or removing the claim from the guide;
- loading or retiring the inbox plugin;
- the plugin lifecycle fixes;
- the documentation fixes listed in §5.

## 7. Related documents and source

Documents:

- [Plugins, MCP and extensions guide](../../guides/plugins-and-mcp.md)
- [Agent-facing tools reference](../../reference/cli/agent-tools.md)
- [Plugins module catalogue](../../reference/modules/plugins.md)
- [Plugin system design record](../plugin-system.md)
- [MCP server design record](../mcp-server.md)
- [Tiered tools design record](../tiered-tools.md)
- [aq-surface design](../design/aq-surface.md) and
  [implementation](../implementation/aq-surface.md) (`/mcp-task`, scopes)
- [Memory plugin v2](../design/memory-plugin.md),
  [aq-memory extraction design](../../superpowers/specs/2026-04-25-aq-memory-extraction-design.md)
  and [feature pauses](../design/feature-pauses.md)
- [Platforms design](../../superpowers/specs/2026-04-25-platforms-implementation-design.md)
  (historical, superseded)
- [Codegen](../../contributing/codegen.md)
- Vault: `specs/2026-10-12-implementation-playbooks-reusable-building-blocks.md`
  (`rev-smart-flare`, approved r1),
  `specs/2026-10-13-spec-ingestion-with-task-structure-review-before-release.md`
  (`rev-bright-meadow`, approved r2),
  `specs/2026-10-02-plugin-value-by-role-winnow-fast-jev-and-superpowers.md`
  (`rev-nimble-torrent`, approved r1)

Source:

- `src/plugins/`: `base.py`, `loader.py`, `registry.py`, `services.py`, `internal/`
  (`files.py`, `git.py`, `notes.py`, `vibecop.py`, `inbox/`)
- `src/commands/plugin_commands.py`, `src/cli/plugins.py`, `src/knowledge/legacy.py`,
  `src/knowledge/memory_port.py`
- `src/embedded_mcp.py`, `src/mcp_registration.py`, `src/mcp_interfaces.py`,
  `src/aq_uri.py`
- `src/profiles/mcp_registry.py`, `mcp_catalog.py`, `mcp_probe.py`,
  `mcp_inline_migration.py`, `src/commands/mcp_commands.py`
- `src/tools/definitions.py`, `registry.py`, `tool_index.py`, `command_catalogue.py`,
  `command_catalogue.json`, `src/cli/auto_commands.py`,
  `src/commands/tool_commands.py`
- `src/skills/*/SKILL.md`, `src/vault.py` (`ensure_default_aq_skills`),
  `src/doctor/skill_checks.py`, `src/prime/templates/tool_guidance.md`,
  `src/known_tools.py`
- `scripts/generate-command-catalogue.py`, `scripts/generate-cli-command-inventory.py`,
  `scripts/regenerate-generated.sh`

Tests: `tests/test_plugins.py`, `test_plugin_*.py`, `test_cli_plugins.py`,
`test_notes_plugin.py`, `test_vibecop_plugin.py`, `test_inbox_plugin.py`,
`test_embedded_mcp.py`, `test_mcp_server.py`, `test_mcp_registry.py`,
`test_mcp_catalog.py`, `test_mcp_probe.py`, `test_mcp_commands.py`,
`test_mcp_inline_migration.py`, `test_aq_uri.py`, `test_tool_registry.py`,
`test_tool_index.py`, `test_command_surface.py`, `test_cli_inventory.py`,
`test_cli_startup_offline.py`, `test_generated_artifacts.py`,
`test_doctor_skill_checks.py`, `test_guidance_docs.py`, `test_known_tools.py`.
