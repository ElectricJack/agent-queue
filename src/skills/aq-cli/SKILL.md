---
name: aq-cli
description: Discovery and orientation for the aq (agent-queue) command-line interface. Use when you need to find or invoke any daemon command, or when another aq-* skill directs you to run `aq <group> <cmd>`. Answers "what can I do with aq" and "how do I get more detail on a specific command".
allowed-tools:
  - Bash
---

# aq CLI — Orientation & Discovery

`aq` is the command-line surface for the agent-queue daemon. Everything the
daemon can do — task management, workspaces, sessions, playbooks, gates,
messages, projects, plugins, memory — is reachable through it. Prefer
`aq` over any MCP tool that duplicates its function; the CLI is the
source of truth.

## Command groups

```
aq agent      — profile CRUD, list agents, live-worker messaging
aq doctor     — health checks for this install, with fixes
aq file       — read / write / edit / glob / grep on files
aq formula    — reusable task-graph templates (list / show / cook)
aq git        — branch / commit / push / PR / merge
aq mcp        — MCP server registry + tool catalog
aq memory     — semantic memory search, project profiles
aq message    — inter-agent and user message queue
aq note       — project notes (list / read / write / append / delete)
aq playbook   — playbook compilation, runs, HITL, health
aq pool       — worker pool sizing (status / scale)
aq project    — project CRUD, workspaces, channels, budgets
aq session    — inspect and steer agent sessions
aq status     — one-shot system-status overview
aq system     — diagnostics, config, prompt management
aq task       — task lifecycle, gates, dependencies, results, archives
aq vault      — vault migration and inspection
```

`aq --help` lists the full set; the above is the part you reach for most.

## Discovery workflow

Two commands cover every need:

1. **`aq --help-all`** — prints the full command reference for the entire
   CLI (every group, every subcommand, every flag). Emit this to a temp
   file (`aq --help-all > /tmp/aq-help.txt`) and read/grep it when you
   need a broad view.
2. **`aq <group> <cmd> --help`** — prints detail on one specific command.
   Use this before invoking anything you haven't run before.

Example:

```bash
aq task --help                    # subcommands of `aq task`
aq task close --help              # arguments and flags for `aq task close`
aq --help-all | grep -A5 gate     # every gate-related command
```

## Output formats

`aq` prints human-friendly tables by default. Two flags change that:

- `--json` — raw JSON on stdout. Use in scripts and when you need
  structured data.
- `--brief` — trims each entity to a compact projection (still readable).
  Composes with `--json`.

Both are options on the top-level `aq` group, so they go **before** the
subcommand: `aq --json task list` works, while putting `--json` after
`task list` fails with `No such option: '--json'`.

Example:

```bash
aq --json task list | jq '.[] | select(.status=="READY") | .id'
```

## Scope + authentication

The CLI talks to the daemon on `127.0.0.1:8081` (or whatever `--api-url`
overrides). When running inside a session started by the daemon, the
session's bearer token is injected automatically and every command is
scope-checked at the daemon: a session-scoped token can only touch its
own project (or, for supervisor sessions, run any command within that
project). Cross-project mutations from a non-elevated session return
`out of scope: <field> mismatch`.

Don't work around scope errors — they are the access-control layer. If
you legitimately need to run a broader command, message the human
(`aq message send`) instead.

## When to reach for a specialized aq-* skill

For common workflows there are focused skills that document the exact
command shapes and gotchas. Use them instead of re-deriving:

- **aq-tasks** — creating / closing / reopening / editing tasks, working
  with results, dependencies.
- **aq-comms** — messages, inbox handling, ask_human.
- **aq-workspaces-and-git** — workspace ops + git via CLI.
- **aq-playbooks-and-gates** — playbook runs and human-in-the-loop
  gates.

If a workflow needs a command none of those cover, come back here and
use the discovery workflow above.
