# Agent Queue

**A local control plane for running a software factory of coding agents.**

Agent Queue is a long-running daemon that turns durable work into isolated agent
sessions, routes that work across a shared worker fleet, and carries it through review.
Tasks, dependencies, gates, sessions, and outcomes live outside any one model context,
so a crashed or exhausted session does not have to be the end of the job.

The closest category reference is [Gas City](https://github.com/gastownhall/gascity):
in both cases, the interesting unit is not a chat with one agent but an operational
system that expresses work, assigns it to a fleet, observes it, and keeps it moving.
Agent Queue is an independent implementation, not a Gas City distribution or compatible
SDK. It uses its own daemon, database, task graph, markdown vault, pipeline policy, and
API. The comparison is about the kind of system this is—a local software factory—not a
claim that the internals or scale are the same.

Agent Queue is under active development. It is currently best suited to operators who
are comfortable running coding-agent CLIs and inspecting the work they produce.

## How work moves through the system

1. **Work enters as durable records.** Create a task from the dashboard, `aq` CLI,
   Discord, REST API, or MCP. A task can stand alone or sit in a typed dependency graph.
   Reusable formulas can materialize graph templates. Approved specs can be handed to a
   spec-ingest agent, which proposes a task batch for human approval before anything is
   committed to the graph.
2. **Routing chooses capabilities, not a hard-coded model.** An unrouted task receives a
   routing gate. The default pipeline coalesces open gates into a reusable triage task,
   and triage pins a profile, intelligence class, and workspace requirement. Profiles
   describe the role and allowed tools; intelligence classes map a level such as
   `standard-medium` to provider-specific model and reasoning settings.
3. **A shared worker runs the task.** Durable, global worker identities are reused across
   projects. For each assignment, the daemon launches the profile's harness in an
   observable tmux session. The shipped harness definitions cover Claude Code, OpenAI
   Codex, and Gemini CLI.
4. **Git work is isolated.** Repository tasks run in reusable worktree slots, normally on
   an `aq/<task-id>` branch. The worktree is disposable execution space; the branch,
   task history, comments, and session attempts are the durable artifacts. Project caps,
   workspace locks, leases, heartbeats, and recovery logic bound concurrency and make
   stalled work visible.
5. **Review is part of the graph.** A workspace task must close explicitly with a
   summary. The default pipeline creates a read-only reviewer for each completed task
   with a branch, gates downstream work on that verdict, and coalesces the branch into a
   final review. The final-reviewer profile is the only shipped profile with merge
   authority. Rejection reopens the original task with actionable feedback.
6. **Humans steer the running factory.** The Command Center shows the live task graph,
   task list, gates, files, diffs, playbook runs, and session history. The Agent Flock
   view exposes shared workers and attachable live terminals. The same command layer is
   available through the dashboard, CLI, Discord, REST, and MCP rather than being
   reimplemented per interface.

## The pieces

### Daemon and API

The Python daemon owns scheduling, task state, workspace acquisition, session
reconciliation, event delivery, and pipeline dispatch. A FastAPI application exposes
the command surface, health endpoints, generated resource routes, and WebSocket streams
used by the dashboard. SQLite is available for a local install; PostgreSQL is supported
for the more concurrent path.

The control path is deliberately mostly deterministic. Scheduling, dependency and gate
resolution, task assignment, pipeline actions, and recovery do not need an LLM call.
Models do the work that requires judgment: triage, implementation, review, spec
decomposition, and user-defined reasoning steps.

### Work graph and gates

Tasks form a graph with typed relationships such as `blocks`, `parent-child`,
`waits-for`, `discovered-from`, and `related`. Gates represent conditions outside the
ordinary task-status enum: routing decisions, human approval, another task, a timer, a
merged PR, CI, or an event. This keeps “why is this not running?” queryable instead of
hiding it in an agent transcript.

### Markdown vault

Operator-editable policy and configuration live under `~/.agent-queue/vault/` and are
watched by the daemon. Important paths include:

```text
vault/
├── agent-types/<profile>/profile.md
├── harnesses/{claude,codex,gemini}.md
├── intelligence-classes/<class>.md
├── workspace-kinds/<kind>.md
├── system/playbooks/*.md
├── formulas/*.md
└── projects/<project>/
    ├── agent-types/      # project overrides
    ├── playbooks/
    ├── formulas/
    ├── specs/
    ├── notes/
    └── memory/
```

Markdown is the editable source; database rows and compiled artifacts are runtime
projections. The vault is compatible with ordinary editors and Obsidian, but neither is
required.

### Pipelines and playbooks

The shipped `default-pipeline.md` reacts to task, spec, proposal, and gate events. Its
routing, review, and spec-ingest actions are declared in markdown and dispatched through
a strict command allowlist. Project-scoped policy can shadow system policy by role.

General playbooks are also markdown workflow graphs. They can be compiled, validated,
dry-run, inspected, triggered by events, and paused at human gates. Playbooks remain an
actively evolving subsystem; use `aq playbook health` and `aq doctor` to inspect the
capabilities enabled in a particular installation.

### Optional memory and plugins

Memory is not the core scheduling model and Agent Queue does not promise that every
completed task automatically makes the system smarter. The Milvus-backed `aq-memory`
integration is an optional plugin, installed separately. Other plugins can contribute
commands, event handlers, services, and health checks. Inspect a live installation with
`aq plugin list`; the README intentionally does not promise a fixed plugin or tool count.

## Getting started

The source setup currently targets Linux and macOS. You need Python 3.12+, Git, tmux,
and at least one authenticated agent CLI (`claude`, `codex`, or `gemini`). Node.js/npm
is needed for the dashboard, and the GitHub CLI (`gh`) is needed for the automated PR
and merge path. The setup wizard is currently Claude-first even though the session
runtime supports all three shipped harnesses.

```bash
git clone https://github.com/ElectricJack/agent-queue.git
cd agent-queue
./setup.sh
aq start
```

`setup.sh` creates the virtual environment, installs the Python and dashboard packages,
links the CLI entry points, creates `~/.agent-queue/config.yaml`, and runs the interactive
setup wizard. The daemon seeds the vault on first startup. `aq start` starts the daemon
and can start the local dashboard. Discord is the supported chat transport; set
`messaging_platform: none` if you only want the dashboard, CLI, API, and MCP surfaces.

Create a project and put work on the queue:

```bash
aq project create --name my-app --repo-url https://github.com/you/my-app.git
aq agent create --name worker-1 --profile-id worker-standard
aq task create --project my-app --title "Add rate limiting to the API"

aq status
aq task list --project my-app
aq task explain <task-id>
```

`aq task create --graph <file>` creates a dependency graph from JSON or YAML.
`aq task create --from-spec <path>` creates one from a fenced `aq-graph` block. Use
`--dry-run` with either form to validate without writing. Run `aq --help-all` for the
complete CLI surface and `aq doctor` when an installation is not behaving as expected.

To run the dashboard separately during development:

```bash
npm run dev
```

Vite serves the dashboard on `http://127.0.0.1:5173` and proxies the daemon API at
`http://127.0.0.1:8081` by default.

## Development

```bash
pip install -e packages/aq-client
pip install -e ".[dev,cli]"
npm install

pytest tests/
npm test --workspace dashboard
npm run typecheck
npm run build
```

Useful starting points:

- [AQ surface](docs/specs/design/aq-surface.md) — CLI, API auth, task-scoped commands,
  and prime documents
- [Work graph](docs/specs/design/work-graph.md) — typed dependencies, gates, claims,
  pools, and task hierarchy
- [Worktree execution](docs/specs/design/worktree-execution.md) — reusable slots,
  branches, recovery, and integration
- [Profiles](docs/specs/design/profiles.md) — markdown roles and capabilities
- [Playbooks](docs/specs/design/playbooks.md) — authored workflow graphs
- [Agent flock plan](docs/superpowers/plans/2026-08-30-agent-flock.md) and
  [Command Center plan](docs/superpowers/plans/2026-08-30-command-center-unification.md)
  — the recent direction of the worker and operator surfaces

## License

MIT
