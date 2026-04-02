# CLI Module (`src/cli/`)

The `aq` command-line interface. Mirrors Discord slash commands with Rich formatting.

## Architecture

```
app.py        Entry point, shared helpers (_run, _get_client, console), status command
tasks.py      aq task {list,details,create,approve,stop,restart,search,select}
agents.py     aq agent {list,details}
hooks.py      aq hook {list,runs,details}
projects.py   aq project {list,details,set}
plugins.py    aq plugin {list,info,install,remove,enable,disable,update,config,logs,prompts,...}
client.py     CLIClient — async DB wrapper for CLI operations (no running daemon needed)
formatters.py Rich table/panel formatters for all entity types
menus.py      Interactive prompts (task wizard, fuzzy select, confirm)
styles.py     Theme, status icons, color maps
```

## How It Works

- **No daemon required**: CLI talks directly to the SQLite database via `CLIClient`.
- **Async bridge**: All DB calls are async. `_run()` in `app.py` bridges sync Click commands to async DB operations.
- **Command registration**: Each module imports `cli` from `app.py` and decorates functions with `@cli.group()` / `@group.command()`. Importing the module registers the commands.
- **Plugin CLI extensions**: Plugins can add their own `aq <plugin-name> ...` subcommands via the `aq.plugins` entry point group. These are loaded dynamically at startup in `app.py`.

## Conventions

- Commands that modify state should use `_get_client()` context manager for DB access.
- Heavy imports (formatters, models, loader functions) are deferred to inside command functions to keep CLI startup fast.
- Error handling: catch `FileNotFoundError` (missing DB) and `Exception`, print with Rich markup, exit with `SystemExit(1)`.
- Plugin install/update logic lives in `src/plugins/loader.py` (`install_plugin_from_url`) — CLI and registry both call it. Don't duplicate that logic here.
