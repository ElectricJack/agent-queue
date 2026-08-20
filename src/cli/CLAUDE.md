# CLI Module (`src/cli/`)

The `aq` command-line interface — the primary surface for both agents and humans
(`docs/specs/design/aq-surface.md`). Mirrors Discord slash commands with Rich formatting.

## Architecture

```
app.py          Entry point, shared helpers (_run, _get_client, console), status command,
                global --json / --brief flags
envelope.py     Versioned JSON envelope: envelope(), error_envelope(), emit(),
                BRIEF_PROJECTIONS — see docs/specs/design/aq-surface.md §4
agent_surface.py  aq schema (aq prime|handoff join here in Phase S1)
tasks.py        aq task {list,show,set,details,create,approve,stop,restart,search,select}
agents.py       aq agent {list,details}
hooks.py        aq hook {list,runs,details}
logs.py         aq logs — tail/filter JSONL log file directly (no daemon needed)
projects.py     aq project {list,details,set}
plugins.py      aq plugin {list,info,install,remove,enable,disable,update,config,logs,prompts,...}
client.py       CLIClient — async REST client for CLI operations (talks to the running daemon)
formatters.py   Rich table/panel formatters for all entity types
menus.py        Interactive prompts (task wizard, fuzzy select, confirm)
styles.py       Theme, status icons, color maps
```

## How It Works

- **Daemon required**: the CLI is REST-first — every command delegates to the daemon's
  `CommandHandler` via `POST /api/execute` (`CLIClient` in `client.py`; the direct-DB
  `PluginClient` is a narrow exception for filesystem-heavy plugin management ops).
- **Base URL**: `AQ_API_URL` (canonical) → `AGENT_QUEUE_API_URL` (legacy alias, kept
  indefinitely) → `mcp_server` host/port from `~/.agent-queue/config.yaml` →
  `http://127.0.0.1:8081`.
- **Auth**: `AQ_API_TOKEN`, when set, is sent as `Authorization: Bearer <token>` on every
  request (per-session bearer token injected by session-runtime, design §7). The daemon
  does not yet enforce it (lands in aq-surface Phase S2) — today it is accepted and ignored.
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
  the `aq.plugins` entry point group. These are loaded dynamically at startup in `app.py`.

## Conventions

- Commands that modify state should use `_get_client()` context manager for daemon access.
- Heavy imports (formatters, models, loader functions) are deferred to inside command
  functions to keep CLI startup fast.
- Error handling: catch `FileNotFoundError` (missing DB, for `PluginClient` paths) and
  `Exception`, print with Rich markup, exit with `SystemExit(1)`.
- Plugin install/update logic lives in `src/plugins/loader.py` (`install_plugin_from_url`) —
  CLI and registry both call it. Don't duplicate that logic here.
