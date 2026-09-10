# CLI Module (`src/cli/`)

The `aq` command-line interface — the primary surface for both agents and humans
(`docs/specs/design/aq-surface.md`). Mirrors Discord slash commands with Rich formatting.

## Architecture

Hand-crafted modules only; everything else is generated from the tool definitions by
`auto_commands.py`. Keep this map in sync with the directory — `tests/test_cli_module_map.py`
fails when it names a module that does not exist.

```
app.py             Entry point, shared helpers (_run, _get_client, console), global
                   --api-url / --help-all / --json / --brief flags, command registration
auto_commands.py   Auto-generates a Click command per tool definition (src/tools/definitions.py),
                   grouped by category — this is where most `aq <group> <cmd>` come from
adapters.py        Normalises API responses for the CLI formatters
agent_messages.py  `aq agent message` — live-worker guidance with delivery status
agent_surface.py   Agent-facing surface: `aq schema`, `aq prime`, `aq handoff`
claim_epoch.py     Shared --claim-epoch resolution for pool-session mutators
                   (reads <work_dir>/.aq/claim.json, falls back to $AQ_CLAIM_EPOCH)
client.py          CLIClient — async REST client for CLI operations (see Transport below)
daemon.py          `aq start` / `stop` / `restart`
db.py              `aq db` — the operator's migration door (`current`, `upgrade`)
doctor.py          `aq doctor` and `aq costs`
envelope.py        Versioned JSON envelope: envelope(), error_envelope(), emit(),
                   BRIEF_PROJECTIONS — see docs/specs/design/aq-surface.md §4
exceptions.py      CommandError, DaemonNotRunningError, ScopeDeniedError
formatter_registry.py  Maps CommandHandler commands to Rich formatters
formatters.py      Rich table/panel formatters for all entity types
formulas.py        `aq formula show` / `aq formula cook`
global_options.py  Copies --json / --brief / --api-url onto commands so they parse anywhere
integration.py     `aq integration` — hierarchical integration trains
install.py         `aq install` — the daemon-free installer: builds the step registry,
                   runs src/install's engine in-process, maps outcomes to exit codes
inventory.py       Reproducible CLI command inventory and ownership classification
logs.py            `aq logs` — tail/filter JSONL log file directly (no daemon needed)
menus.py           Interactive prompts (task wizard, fuzzy select, confirm)
messages.py        `aq message *`, `aq inbox`, `aq reply`, `aq chat`
playbook.py        `aq playbook` — compile, run, HITL, health
plugins.py         `aq plugin {list,info,install,remove,enable,disable,update,config,logs,...}`
projects.py        Hand-crafted `aq project` commands needing composite logic or UX sugar
questions.py       `aq question {list,answer,escalate}` — identity-based worker questions
sessions.py        `aq session` — the session-runtime CLI group
streams.py         `aq stream start|tail|kill`
styles.py          Theme, status icons, color maps
system_config.py   `aq system config` — YAML config editing
tasks.py           Hand-crafted `aq task` commands needing interactive features
                   (create wizard, select, close, claim, comment)
test_runner.py     `aq test` — pytest behind the box-wide test semaphore
vault.py           `aq vault {migrate,reset-harness}`
```

There is no `agents.py` and no `hooks.py`: `aq agent <cmd>` other than `message` is generated
by `auto_commands.py`, and hooks were replaced by playbooks (`aq playbook`).

## How It Works

- **Daemon required**: the CLI is REST-first — commands delegate to the daemon's
  `CommandHandler` via `POST /api/execute` (`CLIClient.execute` in `client.py`).
- **Transport**: `/api/execute` is the default path, not a universal one. `CLIClient` also
  calls bespoke routers where the shape demands it:
  `POST /api/messages/send` (message send), `POST /api/sessions/{name}/message` and
  `GET /api/sessions/{name}/messages` (the session name lives in the path),
  `POST|GET /api/streams…` (`aq stream`), `GET /api/tools`, `GET /api/health`.
  The typed-dispatch machinery (`_build_typed_dispatch` / `_execute_typed`) is dormant
  compatibility code with no live caller, kept working by
  `tests/test_cli_client_generated.py`; the generated `agent_queue_api_client` package is
  the dashboard client, not the CLI transport. The direct-DB `PluginClient` is a narrow
  exception for filesystem-heavy plugin management ops.
- **Base URL**: `AQ_API_URL` (canonical) → `AGENT_QUEUE_API_URL` (legacy alias, kept
  indefinitely) → `mcp_server` host/port from `~/.agent-queue/config.yaml` →
  `http://127.0.0.1:8081`. `--api-url` overrides all of them.
- **Auth**: `AQ_API_TOKEN`, when set, is sent as `Authorization: Bearer <token>` on every
  request (per-session bearer token injected by session-runtime, design §7). **The daemon
  enforces it** (`src/api/auth.py`, `src/api/middleware.py:TokenAuthMiddleware`,
  `src/api/scope.py`): a request with no bearer on loopback gets `LOCAL_SCOPE` and may run
  every command; a valid session token is restricted to `AGENT_COMMAND_SET` and to its own
  `task_id` / `project_id`. Violations come back as `out of scope: <command>` or
  `out of scope: <field> mismatch` and surface as `ScopeDeniedError`. Per-project supervisor
  tokens are `elevated`: any command, still pinned to their project.
- **Global options**: `--json`, `--brief` and `--api-url` parse at *any* position
  (`aq task list --json` == `aq --json task list`), because `install_global_options()` in
  `global_options.py` copies them onto every command after registration — so it must stay
  the last thing `app.py` does. Two exclusions: passthrough commands
  (`ignore_unknown_options`: `aq test`, `aq stream start`) keep their argv for the child
  program, and a command that already declares `--json` (`aq doctor`, `aq logs`,
  `aq system config get`) keeps its local meaning. Design §4.0.
- **Output contract**: `--json` prints the versioned envelope from `envelope.py`
  (`{"schema_version", "data", "pagination"?}` / `{"schema_version", "error", "data": null}`);
  `--brief` trims list/detail entities to `BRIEF_PROJECTIONS`; `AQ_JSON_LEGACY=1` restores
  the pre-envelope raw-payload shape for one release. New commands should route their output
  through `emit()` rather than printing JSON directly.
- **Async bridge**: All DB/REST calls are async. `_run()` in `app.py` bridges sync Click
  commands to async operations.
- **Command registration**: Each module imports `cli` from `app.py` and decorates functions
  with `@cli.group()` / `@group.command()`. Importing the module registers the commands.
  Hand-crafted modules are imported in `app.py` *before* `register_auto_commands()` so their
  command names win over any auto-generated command of the same name.
- **Plugin CLI extensions**: Plugins can add their own `aq <plugin-name> ...` subcommands via
  the `aq.plugins` entry point group. Command metadata is registered at startup in `app.py`,
  but saved plugin configuration is fetched only when that plugin group is invoked. The
  config reader is read-only and bounded; import, help, version, schema, and `aq test`
  discovery must never initialize or migrate a database.

## Conventions

- Commands that modify state should use `_get_client()` context manager for daemon access.
- Heavy imports (formatters, models, loader functions) are deferred to inside command
  functions to keep CLI startup fast.
- Error handling: catch `FileNotFoundError` (missing DB, for `PluginClient` paths) and
  `Exception`, print with Rich markup, exit with `SystemExit(1)`.
- Plugin install/update logic lives in `src/plugins/loader.py` (`install_plugin_from_url`) —
  CLI and registry both call it. Don't duplicate that logic here.
- **Profile ids in help text and examples**: never hard-code a historical id. Shipped worker
  defaults are `worker-standard-medium-claude`, `worker-deep-high-claude` and
  `worker-fast-medium-claude` (`src/profiles/default_selection.py`); point the reader at
  `aq agent list-profiles` and `aq system list-intelligence-classes` instead of a literal
  list that will rot.
- **Agent-facing docs**: `src/skills/*/SKILL.md` are shipped into the harness skill dirs by
  `src.vault.ensure_default_aq_skills`, which is **write-if-absent**. Editing a skill here
  does not update an already-installed copy — see that function's docstring and the
  `skills.installed_drift` doctor check.
