# System spec: plugins-mcp-and-tools

Status: audited against `origin/dev` at `52d2efe4c` on 2026-10-23. This is system 18 of the
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
  loads, isolates and unloads plugins, five shipped internal plugins, and
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
- **Shared command inventory.** Command descriptors compose schemas and record
  disagreements. CLI, MCP and API generation share a transport view; playbooks use
  a separate contract view. Generated-artifact checks detect stale projections,
  rather than proving all schemas or handler validation agree.
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
- **Trust level.** `INTERNAL` or `EXTERNAL` (`TrustLevel`, `base.py`). It decides
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

A plugin sees the daemon only through `PluginContext` (`base.py`). Through it a
plugin can:

- **Register commands.** `register_command("hello", fn)` stores the command under both
  `hello` and `<plugin>.hello` (`base.py`). `CommandHandler.execute` tries a
  built-in `_cmd_<name>` first and falls back to the plugin registry
  (`src/commands/handler.py`), so a plugin command goes through the same
  authorization and correlation as a built-in one.
- **Register tools.** `register_tool(schema, category=...)` tags the schema with its plugin
  and category (`base.py`). The tool registry, the MCP server and the
  orchestrator's MCP catalogue merge plugin tools in. A plugin tool whose name matches a
  built-in command is dropped with a warning (`src/tools/registry.py`).
- **Register other things.** Doctor checks are prefixed `plugin.<name>.`
  (`base.py`). Services that core can read with `get_service` are registered with
  `register_service` (`base.py`). Event types are informational. A Click group is
  returned from `cli_group()` and mounted under `aq` from the entry point
  (`src/cli/app.py`). Discord command groups are also possible and are mounted
  by `src/discord/bot.py`; core plugin-management slash commands are retired.
- **Call out.** `emit_event` and `subscribe` on the event bus, `execute_command` back into
  the handler, `notify`, and `invoke_llm`, which reaches the providers system's direct
  path (`base.py`).
- **Keep state.** Config with a schema and defaults, stored as JSON on the `plugins` row
  (`load_config`, `save_config`, `set_config_value`), and a key-value store in
  `plugin_data` (`get_data`, `set_data`, `delete_data`). Prompt templates live under the
  plugin's `prompts/` directory and can be diffed and reset from the CLI.
- **Schedule.** Methods decorated `@cron(expr)` run from the orchestrator cycle
  (`registry.py`, called from `src/orchestrator/core.py`). A run is skipped
  while the previous one is still going. No internal plugin uses `@cron` any more; their
  schedules moved to playbooks, and `tests/test_plugin_cron_to_playbook_migration.py`
  pins that.

**Isolation.** An external plugin can get only the `config` and `vault_watcher` services
(`EXTERNAL_ALLOWED_SERVICES`, `base.py`); asking for any other raises
`PermissionError`. Reading `ctx.bus` directly needs internal trust. Permissions
(`NETWORK`, `FILESYSTEM`, `DATABASE`, `SHELL`) are declared metadata; nothing enforces
them. A plugin runs in the daemon's process with the daemon's rights.

**Failure handling.** If `initialize` raises, the plugin is set to `ERROR` and the error is
logged; other plugins still load. A command or cron failure counts against a circuit
breaker: after five consecutive failures (`MAX_CONSECUTIVE_FAILURES`, `registry.py`)
the plugin is set to `ERROR` and `plugin.auto_disabled` is emitted.

### 2.3 Discovery, loading and lifecycle

`PluginRegistry` (`src/plugins/registry.py`) is built in `Orchestrator` startup
(`core.py`). `load_all` runs in this order:

1. **Internal plugins.** `src/plugins/internal/__init__.py` scans the direct child
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
(`registry.py`). `unload_plugin` removes every command key, alias, tool, cron job
and service the plugin registered. Each step emits a lifecycle event (`plugin.installed`,
`.updated`, `.removed`, `.loaded`, `.unloaded`, `.auto_disabled`;
`src/event_schemas.py`).

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
| `aq-inbox` (`internal/inbox/`) | Gmail poller, sender allowlist, `inbox_status` and allowlist commands | Discovered through the package re-export of `InboxPlugin`. Polling and commands require `inbox.enabled` and configured projects; disabled by default. |
| `aq-memory` (external) | The `memory` service: semantic search, facts, KV, documents | Separate repository and not bundled. **Not loaded today** (below). |

**aq-memory today.** Boot calls `load_all(skip=LEGACY_MEMORY_PLUGINS)`, where that set is
`{"aq-memory", "memory"}` (`src/knowledge/legacy.py`, `core.py`). The log line
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

`run_mcp_server` (`src/embedded_mcp.py`) starts from `src/main.py` when
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
`mcp_registration.py`), `mcp_server.excluded_commands` and the
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
configured (`mcp_server.task_scope`) but not served; `src/config.py` says
nothing reads those fields until the aq-surface Phase 3 flip
([aq-surface design](../design/aq-surface.md),
[implementation](../implementation/aq-surface.md)).

### 2.6 External MCP servers for workers

An operator registers a server with `aq mcp create-server`. The entry is written to
`vault/mcp-servers/<name>.md` (system scope) or
`vault/projects/<id>/mcp-servers/<name>.md` (project scope, which shadows system scope)
(`src/profiles/mcp_registry.py`). There is no database table. The vault watcher
reloads entries on change and keeps the previous entry if a file fails to parse. The
synthetic `agent-queue` entry, which points at AQ's own endpoint, is added in memory when
`mcp_server.inject_into_tasks` and `mcp_server.enabled` are on; it cannot be edited or removed.

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
(`src/sessions/spec.py`); no launch path resolves `profile.mcp_servers` into a
harness configuration, and `TaskContext.mcp_servers` is never filled. Workers reach AQ
through the `aq` CLI over `Bash`, and reach external tools only if their harness is
configured outside AQ. The registry, probe and catalog work and are tested, but a profile
grant changes nothing about what a session can call. The only code that writes harness MCP
files is the graft installer (`src/install/graft.py`).

### 2.7 The tool catalogue

`src/tools/definitions.py` supplies explicit tool definitions, category assignments,
CLI overrides and fallback schemas. Some definitions are projected from typed contracts
and Pydantic models. The number grows as commands ship; use generated inventories rather
than treating a count in prose as an interface.

`src/commands/command_inventory.py` composes these inputs, typed contracts, handler
introspection and plugin registrations into immutable `CommandDescriptor` snapshots
(`src/commands/descriptors.py`). Offline composition declares internal plugin tools without
pretending those plugins are loaded. Live plugin load/unload refreshes the snapshot and
its plugin generation.

Transport consumers project the snapshot; the registry retains its discovery APIs:

- **`ToolRegistry`** (`src/tools/registry.py`) retains core/category views of explicit
  definitions and plugin tools, navigation stubs, schema compression and the tool index.
  These APIs do not inject definitions into a tmux harness automatically.
- **The CLI.** `scripts/generate-command-catalogue.py` renders the snapshot's explicit,
  discovered and internal-plugin definitions into `src/tools/command_catalogue.json`.
  `src/cli/auto_commands.py` reads this packaged artifact without importing the handler,
  so help works offline (`tests/test_cli_startup_offline.py`). The rendered CLI tree is
  recorded in `docs/reference/cli-command-inventory.json`.
- **MCP and API generation.** `mcp_registration.py` uses the descriptor transport view;
  `src/api/codegen.py` uses the same inventory for generated OpenAPI/client operations.
- **Playbooks.** The command documentation and contract registry consume a separate
  playbook view with typed, generated, argumentless or opaque schema quality.

The transport view preserves explicit-definition precedence over typed contracts for
compatibility. Descriptor audits now record schema disagreements; they do not reconcile
them or make `execute` a universal contract validator. Handlers still determine their
runtime argument validation.

**Finding a tool.** `find_applicable_tool` searches `ToolIndex`
(`src/tools/tool_index.py`), an in-memory embedding index over `name: description` built
after plugins load. Without an embedding backend it is empty and the command falls back to
keyword overlap. `load_tools` returns tool names for a category or individual tool,
filtered by what the caller's capabilities allow (`src/commands/tool_commands.py`). It is excluded from
both CLI and MCP; the chat loop that used to add loaded schemas to a model's tool set is
gone, so today an agent discovers commands through `aq … --help`, `aq schema`,
`find_applicable_tool` or `/api/tools`.

**Publishing to a caller.** `load_tools` filters dispatchability and capability;
`/api/tools` applies the capability predicate. MCP registration and
`find_applicable_tool` do not provide the same per-caller filtering. Dispatchability
drops names with no handler. Capability policy (`command_allowed` in
`src/commands/authorization.py`) drops names the caller's profile does not grant, without
a message. The same policy refuses scoped commands again at dispatch. MCP currently
does not forward that scope (§2.5). The
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

`ensure_default_aq_skills` (`src/vault.py`, called from vault setup)
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

The table definitions are in `src/database/tables.py`.

### 2.11 Surfaces

- **CLI:** `aq plugin install|list|info|config|enable|disable|reload|update|remove|prompts|
  diff-prompts|reset-prompts|logs`; `aq mcp list-servers|get-server|create-server|
  edit-server|delete-server|probe-server|list-tool-catalog`; `aq schema`;
  `aq agent list-available-tools|check-profile|install-profile`.
- **Commands:** `src/commands/plugin_commands.py`, `mcp_commands.py`, `tool_commands.py`
  (`load_tools`, `find_applicable_tool`), plus every command a plugin registers.
- **MCP:** `/mcp` on the API port.
- **API:** `/api/tools` publishes the caller's filtered tool list
  (`src/api/execute.py`).
- **Doctor:** `skills.installed_drift`, and any `plugin.<name>.*` check a plugin registers.
- **Config:** `mcp_server:` (`enabled`, `host`, `port`, `inject_into_tasks`,
  `excluded_commands`, `task_scope`), and per-plugin config on the `plugins` row. See
  [config](../config.md).

The module catalogue is [plugins](../../reference/modules/plugins.md); the operator guide is
[plugins, MCP and extensions](../../guides/plugins-and-mcp.md).

## 3. Boundaries

| System | What crosses the boundary |
|---|---|
| daemon-core | `Orchestrator` builds the plugin registry, MCP registry and tool index at start and ticks plugin cron each cycle. `CommandHandler.execute` falls back to plugin commands and authorizes MCP calls using the supplied or default principal (§2.5). |
| task-graph | Most tool definitions describe task commands; this system only publishes their schemas. |
| scheduling-and-claims | None directly. Plugins may read claims through commands. |
| routing-and-profiles | Profiles carry the `## MCP Servers` names, the harness-tool allowlist checked against `known_tools`, the `install:` manifest and the `## Capabilities` grants that filter published tools. Capability policy is owned there and in daemon-core. |
| sessions-and-harnesses | Harnesses read the installed skills from their skill directories. Session launch is where profile MCP grants would be applied and today are not. Prime renders the tool guidance. Harness plugins (winnow, superpowers) belong there. |
| projects-workspaces-git | `aq-git` and `aq-files` wrap `GitManager` and the workspace service, locked to the session's worktree. |
| integration-and-delivery | `aq-git` exposes PR and merge commands; delivery itself does not call plugins. |
| playbooks | Playbook steps run commands, including plugin commands, through the same handler. The compiler is the intended consumer of `aq://` rewriting, but currently does not call it. Shared command descriptors have shipped (§2.7). Plugin `@cron` schedules moved to playbooks. |
| messaging-and-human-asks | The enabled inbox plugin emits allowlisted/unknown email events; the `question_*` commands get a fixed MCP scope. |
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
- **One composed inventory, separate schema views.** The CLI tree, MCP and API
  generation use descriptor transport views; playbooks use contract views. Explicit
  definitions still win on transports, with disagreements recorded in audits. Generated
  files are regenerated, never hand-edited or hand-merged.
- **The CLI is usable offline.** Building `aq --help` must not import the handler.
- **Scoped dispatch is guarded.** CLI/HTTP dispatch uses session identity and
  capability policy. Discovery filtering is surface-specific; MCP currently fails
  to forward the resolved identity (§5). A catalog match is not an authorization grant.
- **MCP excludes daemon control.** Shutdown, restart, update, arbitrary shell and token
  minting are never MCP tools.
- **The MCP registry lives in the vault.** Server definitions are operator-readable
  markdown; scope is project before system; builtins are synthesized and immutable.
- **Shipped skills are write-if-absent.** An operator's edits survive upgrades; drift is
  reported by doctor, not silently overwritten.
- **Guidance is tested against the CLI.** Every `aq …` line in a skill or prime template
  must parse against the live Click tree.

## 5. Known gaps

The 2026-10-23 audit corrected the operator guide and agent-facing tool reference:
profile MCP names do not configure sessions, MCP does not enforce the authenticated
session scope, legacy memory loading is fenced, the retired chat loop does not inject
schemas, tool search has a keyword fallback, and eight skills seed at vault setup.
The plugin/MCP/tiered-tool, memory-extraction and platforms design records now carry
specific supersession notes. The module catalogue now records inbox discovery correctly.

Remaining source/docstring contradictions and code gaps for supervisor triage follow;
this audit does not change production code.

**Behavior and remaining code contradictions.**

- **MCP scope is lost.** `src/mcp_registration.py` strips private arguments but
  forwards no resolved HTTP identity to `execute` except a fixed MCP identity for
  `question_*`. Valid session tokens therefore do not restrict ordinary MCP dispatch.
  Resource callbacks also read shared state directly. `/mcp-task` remains unserved.
- **Profile MCP selection is not applied.** `src/sessions/spec.py` explicitly starts
  sessions without `--mcp-config`; no launch path populates `TaskContext.mcp_servers`
  from profile names. Config and tool descriptions still imply automatic injection.
- **Plugin lifecycle has two paths.** `src/cli/plugins.py` writes the database directly
  rather than using `CommandHandler`. Its changes wait for restart; its `reload` reads
  only `plugin.yaml`. `config` stores string values without context schema validation.
  This contradicts the repository's intended single command mutation boundary.
- **Live config callback is unused.** `on_config_changed` exists on `Plugin` but config
  mutation does not call it. `enable_plugin` loads the plugin as ACTIVE and then
  overwrites its database status with INSTALLED (`src/plugins/registry.py`).
- **URI rewriting is unwired.** `rewrite_aq_uris` has no production caller; its docstring
  and tool descriptions still promise compiler rewriting. Root/path helpers do have
  consumers, and their presence does not prove URI expansion occurs.
- **Discovery surfaces differ.** `find_applicable_tool` lacks the dispatchability and
  capability filtering used by `load_tools`. The builtin MCP catalog includes explicit
  and plugin definitions only, omitting commands found in registration's third pass
  (`src/profiles/mcp_catalog.py`). Descriptor audits now expose schema disagreements,
  but do not reconcile the transport and typed-contract views.
- **MCP enabled default disagrees.** `McpServerConfig.enabled` defaults true; the YAML
  loader uses false for a present section without `enabled` (`src/config.py`). Set it
  explicitly rather than relying on one of these conflicting defaults.

Intentional limits: plugin permissions are metadata, not process isolation; legacy
memory loading is fenced pending K07; tiered registry APIs do not configure harnesses;
installed skills are write-if-absent and require an operator drift repair. These limits
are now documented and should not be reported as missing installations or test failures.

## 6. Shipped changes and unimplemented designs

The previous version was a 2026-10-20 snapshot with task statuses captured at base
`22cffd1b7`. Those task statuses are historical, not a current queue read. This audit
checks repository code and does not claim access to other workers' task state.

Recent changes visible at the audited base include:

- **Shared descriptors.** `195dd5435` adds the immutable inventory, schema-quality audit
  and transport/contract projections described in §2.7. This is shipped, not planned.
- **Offline CLI generation.** `118bdba1f` moves CLI tree construction to packaged schemas.
- **Knowledge and records.** The registered command surface includes scoped knowledge
  and record operations. `544e29492` and `d34760d87` add scoped vault-read guidance and
  effective knowledge-access reporting. Canonical knowledge does not require loading
  the legacy `aq-memory` plugin.
- **Skills.** Eight skills ship, including bounded mini-project ideation. Recent
  wait/job, review, messaging and integration changes update those guides to the
  current commands. Write-if-absent installs still need an operator drift check to
  receive revised text; this audit does not overwrite installed copies.

Still unimplemented at this base: forwarding MCP request scope, applying profile MCP
names to launched sessions, serving `/mcp-task`, wiring compiler URI rewriting, and the
K07 legacy-memory adapter handshake. These are code gaps or deferred designs, not
features enabled by configuration alone. The [aq-surface design](../design/aq-surface.md)
and [implementation record](../implementation/aq-surface.md) describe the proposed narrow
endpoint; the knowledge rollout owns K07. No live scheduling promise is made here.

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
