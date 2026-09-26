# CLI Module (`src/cli/`)

The `aq` command line — the primary surface for agents and humans
(`docs/specs/design/aq-surface.md`). Hand-crafted modules are listed below; every other
`aq <group> <cmd>` is generated from the tool definitions by `auto_commands.py`. Keep the
map in sync with the directory: `tests/test_cli_module_map.py` fails when it names a
module that does not exist or misses one that does.

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
daemon.py          `aq start` / `stop` / `restart` — the daemon, plus the dashboard server when a
                   bundle is installed (`--no-dashboard` only skips the Vite prompt;
                   `--no-dashboard-server` leaves the server alone)
dashboard.py       Hand-written `aq dashboard` group (the generated `state-*` commands merge
                   into it): `serve` (foreground) and `start|stop|restart|status` (background,
                   via src/dashboard_server/process.py), plus the read-only `link` (the origin
                   Discord links name, via src/remote_links.py); also the helpers `aq start|
                   stop|restart|status` call, so the PID/log/config paths come from daemon.py
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
                   runs src/install's engine in-process, maps outcomes to exit codes;
                   `--repair` / `--upgrade` reconcile an existing installation
inventory.py       Reproducible CLI command inventory and ownership classification
jobs.py            `aq job {submit,show,list,cancel,result,logs,attach}` and `aq run` preset alias
logs.py            `aq logs` — tail/filter JSONL log file directly (no daemon needed)
menus.py           Interactive prompts (task wizard, fuzzy select, confirm)
messages.py        `aq message *`, `aq inbox`, `aq reply`, `aq chat`
playbook.py        `aq playbook` — compile, run, HITL, health
plugins.py         `aq plugin {list,info,install,remove,enable,disable,update,config,logs,...}`
projects.py        Hand-crafted `aq project` commands needing composite logic or UX sugar
questions.py       `aq question {list,answer,escalate}` — identity-based worker questions
reports.py         `aq report {morning,request,brief,submit}` — evidence preview, durable reads and file submission
reviews.py         `aq review` — document-review queue, decisions, revisions, and comments
sessions.py        `aq session` — the session-runtime CLI group
streams.py         `aq stream start|tail|kill`
styles.py          Theme, status icons, color maps
system_config.py   `aq system config` — YAML config editing
tasks.py           Hand-crafted `aq task` commands needing interactive features
                   (create wizard, select, close, claim, comment)
test_runner.py     `aq test` — pytest behind the box-wide test semaphore
uninstall.py       `aq uninstall` — plans and removes installer-owned resources from the
                   resume record; destructive scopes are opt-in and confirmed one by one
update.py          `aq update` — stop the daemon, fast-forward the source checkout, then hand
                   reinstall / rebuild / restart to a fresh process on the new code
                   (src/install/update_finish.py); rolls back on any failure (src/install/update.py)
waits.py           `aq wait {register,show,list,cancel}` — typed, claim-fenced durable waits
vault.py           `aq vault {migrate,reset-harness}`
```

There is no `agents.py` and no `hooks.py`: `aq agent <cmd>` other than `message` is
generated, and hooks were replaced by playbooks (`aq playbook`).

## How it works

- **Daemon required, REST-first:** commands delegate to the daemon's `CommandHandler`
  via `POST /api/execute` (`CLIClient.execute`). A few shapes use bespoke routers —
  `POST /api/messages/send`, `POST|GET /api/sessions/{name}/message[s]`,
  `/api/streams…`, `GET /api/tools`, `GET /api/health`. The typed-dispatch machinery
  (`_build_typed_dispatch` / `_execute_typed`) is dormant compatibility code kept alive by
  `tests/test_cli_client_generated.py`; the generated `agent_queue_api_client` package is
  the dashboard client, not the CLI transport. The direct-DB `PluginClient` is a narrow
  exception for filesystem-heavy plugin management.
- **Base URL:** `AQ_API_URL` → `AGENT_QUEUE_API_URL` (legacy alias, kept) → `mcp_server`
  host/port from `~/.agent-queue/config.yaml` → `http://127.0.0.1:8081`; `--api-url`
  overrides all of them.
- **Auth:** `AQ_API_TOKEN` is sent as `Authorization: Bearer` and **the daemon enforces
  it** (`src/api/auth.py`, `middleware.py:TokenAuthMiddleware`, `scope.py`): no bearer on
  loopback is `LOCAL_SCOPE` (every command); a session token is restricted to
  `AGENT_COMMAND_SET` and its own `task_id` / `project_id` (`out of scope: …` →
  `ScopeDeniedError`); a per-project supervisor token is `elevated` but project-pinned.
- **Global options** `--json`, `--brief`, `--api-url` parse at any position because
  `install_global_options()` copies them onto every command after registration — so it
  must stay the last thing `app.py` does. Passthrough commands (`aq test`,
  `aq stream start`) keep their argv, and a command that already declares `--json`
  (`aq doctor`, `aq logs`, `aq system config get`) keeps its local meaning. Design §4.0.
- **Output contract:** `--json` prints the versioned envelope from `envelope.py`;
  `--brief` trims to `BRIEF_PROJECTIONS`; `AQ_JSON_LEGACY=1` restores the raw payload for
  one release. Route new output through `emit()`, never print JSON directly.
- **Registration:** a module imports `cli` from `app.py` and decorates with
  `@cli.group()` / `@group.command()`; importing registers. Hand-crafted modules are
  imported *before* `register_auto_commands()` so their names win over generated ones.
  `_run()` in `app.py` bridges sync Click commands to the async client.
- **Plugin CLI extensions** register `aq <plugin-name> …` via the `aq.plugins` entry
  point group; saved plugin config is fetched only when that group is invoked, read-only
  and bounded. Import, help, version, schema and `aq test` discovery must never initialize
  or migrate a database.

## Conventions

- State-modifying commands use the `_get_client()` context manager.
- Defer heavy imports (formatters, models, loaders) into the command function to keep
  startup fast.
- Errors: catch `FileNotFoundError` (missing DB on `PluginClient` paths) and
  `Exception`, print with Rich markup, `SystemExit(1)`.
- Plugin install/update logic lives in `src/plugins/loader.py`
  (`install_plugin_from_url`); don't duplicate it here.
- **Never hard-code a profile id in help text or examples.** Worker rungs are derived per
  (class × harness) from the `worker-<harness>` templates (`src/profiles/catalog.py`);
  point the reader at `aq agent list-profiles` and `aq system list-intelligence-classes`.
- `src/skills/*/SKILL.md` are shipped into the harness skill dirs write-if-absent
  (`src.vault.ensure_default_aq_skills`): editing one here does not update an installed
  copy — see the `skills.installed_drift` doctor check.
