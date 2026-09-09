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
aq agent        — profile CRUD, list agents, live-worker messaging
aq chat         — talk to a project's supervisor session
aq costs        — token spend rolled up into USD
aq db           — inspect the daemon's schema (`current`) / upgrade it (operator only)
aq discord      — Discord channel and thread housekeeping
aq doctor       — health checks for this install, with fixes
aq file         — read / write / edit / glob / grep on files
aq formula      — reusable task-graph templates (list / show / cook)
aq git          — branch / commit / push / PR / merge
aq graph        — server-side spatial task-graph layout
aq handoff      — record a handoff note, request a session restart
aq inbox        — pending messages (alias for `aq message inbox`)
aq integration  — inspect and control hierarchical integration trains
aq logs         — tail and filter daemon logs (reads JSONL directly, no daemon needed)
aq mcp          — MCP server registry + tool catalog
aq memory       — semantic memory search, project profiles, compaction
aq message      — inter-agent and user message queue
aq note         — project notes (list / read / write / append / delete)
aq playbook     — playbook compilation, runs, HITL, health
aq plugin       — plugin management
aq pool         — worker pool sizing (status / scale)
aq prime        — print this task's startup prime document
aq project      — project CRUD, workspaces, channels, budgets
aq question     — pending worker questions (list / answer / escalate)
aq reply        — reply to a message (alias for `aq message reply`)
aq schema       — the system's enum catalog (statuses, outcomes, error codes)
aq session      — inspect and steer agent sessions
aq status       — one-shot system-status overview
aq stream       — streamable-command registry (console-stream pane view)
aq subagent     — native sub-agent telemetry reported by harness hooks
aq system       — diagnostics, config, prompt management
aq task         — task lifecycle, gates, dependencies, results, archives
aq test         — run pytest under the box-wide test semaphore
aq vault        — vault migration and inspection
aq start / stop / restart — daemon lifecycle (operator only)
```

`aq --help` lists the full set, and this list will drift before the CLI
does — treat it as a map, and confirm a shape with `--help` before you
run something new.

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

Never guess an enum value. `aq schema` prints the catalog the daemon
actually validates against — task statuses, `outcome` (`pass` / `fail`),
`claim_result`, `hierarchy_error`, dependency types.

## Output formats

`aq` prints human-friendly tables by default. Two flags change that:

- `--json` — the versioned envelope on stdout:
  `{"schema_version": …, "data": …, "pagination"?: …}` on success and
  `{"schema_version": …, "error": …, "data": null}` on failure. Read
  `.data`, not the top level. (`AQ_JSON_LEGACY=1` restores the
  pre-envelope raw payload for one release; don't rely on it.)
- `--brief` — trims each entity to a compact projection (still readable).
  Composes with `--json`.

Both are options on the top-level `aq` group, so they go **before** the
subcommand: `aq --json task list` works, while putting `--json` after
`task list` fails with `No such option: '--json'`. The same goes for
`--api-url` and `--help-all`.

Example:

```bash
aq --json task list | jq '.data[] | select(.status=="READY") | .id'
```

## Scope + authentication

The CLI talks to the daemon on `127.0.0.1:8081` (or `AQ_API_URL` /
`--api-url`). `AQ_API_TOKEN`, injected into every daemon-started session,
is sent as `Authorization: Bearer <token>` and **is enforced**:

- **No bearer, on loopback** — the trusted local operator path. Every
  command is allowed.
- **A session token** — restricted to the agent command set (task reads
  and writes for your own task, comments, close, claim, heartbeat,
  handoff, messages, memory, `prime`, `schema`, `session drain-ack`), and
  to your own `task_id` / `project_id`. Anything else answers
  `out of scope: <command>` or `out of scope: <field> mismatch`.
- **A supervisor token** — *elevated*: any command, still pinned to that
  supervisor's project.

Don't work around scope errors and don't retry them — they are a property
of your token, not a transient failure. If you legitimately need a broader
command, message the human (`aq message send --to user:dashboard`).

## Profiles and intelligence classes

A task runs under an **agent profile** (which worker: tools, prompt,
`harness`, lifecycle) and an **intelligence class** (which model and how
much reasoning). Both are markdown in the vault, so the current set is
whatever this install has — read it, never hard-code it:

```bash
aq agent list-profiles                    # the profiles that exist here
aq system list-intelligence-classes       # the classes that exist here
```

Shipped worker defaults are named `worker-<tier>-<level>-<provider>`, e.g.
`worker-standard-medium-claude`. Shipped intelligence classes are the
`{fast,standard,deep}-{off,low,medium,high}` ladder. Anything you see in
an example is an example, not a guarantee that the id exists here.

## When to reach for a specialized aq-* skill

For common workflows there are focused skills that document the exact
command shapes and gotchas. Use them instead of re-deriving:

- **aq-tasks** — creating / closing / reopening / editing tasks, working
  with results, dependencies, the pool claim loop.
- **aq-comms** — messages, inbox handling, reporting a blocker.
- **aq-workspaces-and-git** — workspace ops + git via CLI.
- **aq-playbooks-and-gates** — playbook runs and human-in-the-loop
  gates.

If a workflow needs a command none of those cover, come back here and
use the discovery workflow above.

## Keeping these skills current (operators)

These skills ship in the repo at `src/skills/<name>/SKILL.md` and are
copied into every harness skill directory (`~/.claude/skills/`,
`~/.gemini/skills/`, `~/snap/gemini-cli/common/.gemini/skills/`,
`~/.codex/skills/`) by `src.vault.ensure_default_aq_skills` at daemon
start. That copy is **write-if-absent**: an installed `SKILL.md` is never
overwritten, so operator edits survive upgrades — and so does a stale copy
after the in-tree source is fixed.

`aq doctor --check skills.installed_drift` reports installed copies that
differ from the shipped source; `--fix` backs each one up next to itself
as `SKILL.md.bak` and re-copies the shipped version. To do it by hand,
delete the installed copy and restart the daemon:

```bash
aq doctor --check skills.installed_drift          # what has drifted
aq doctor --check skills.installed_drift --fix    # back up + re-copy
```

An agent inside a task worktree cannot do this: the installed copies live
outside the workspace and re-seeding is a daemon/operator action. Ship the
fix in `src/skills/` and tell the operator to run the check.
