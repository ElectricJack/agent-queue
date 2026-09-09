# Glossary

Every term the rest of the documentation uses as jargon, in one place. Terms
are grouped by what you meet first, not alphabetically; use your browser's find
if you arrived here looking for one word.

Each entry says what the thing *is*, and where it lives — configuration file,
vault markdown, database row, or running process — because "where does this
live" is the question that most often makes AQ confusing.

## The system

**Agent Queue (AQ)** — the whole system: a background service plus the
surfaces you drive it from.

**Daemon** — the long-running `aq` process. It hosts the orchestrator, the
HTTP/WebSocket API, the embedded MCP server and the Discord gateway in one
Python process. Entry point: [`src/main.py`](../../src/main.py).

**Orchestrator** — the deterministic loop inside the daemon that advances work
every few seconds: promoting tasks whose dependencies are met, reconciling
worker pools, acquiring workspaces, reaping finished sessions. It spends no LLM
tokens; every decision it makes is code.

**Supervisor** — an LLM-backed agent, one per project, that reads state and
takes recovery actions on your behalf. It is a *consumer* of the same commands
you use, not a privileged path.

**Command handler** — the single entry point through which every state change
passes, whatever surface asked for it. `aq`, the REST API and MCP all call the
same commands, which return `{"success": bool, ...}`.
[`src/commands/handler.py`](../../src/commands/handler.py).

**Surface** — a way of talking to AQ: the CLI (`aq`), the dashboard, the REST
API, MCP, or a worker's in-session tools. Surfaces differ in authority, not in
capability: an agent's session token is scoped to its own task.

## Work

**Task** — one unit of work, with a title, a description, a status and an
owning project. Tasks carry the routing fields a worker needs (an intelligence
class and a profile) and, optionally, declared deliverables.

**Task status** — one of `DEFINED`, `READY`, `ASSIGNED`, `IN_PROGRESS`,
`WAITING_INPUT`, `PAUSED`, `COMPLETED`, `FAILED`, `BLOCKED`
([`src/models.py`](../../src/models.py)). `READY` means "nothing blocks this",
not "somebody is working on it".

**Dependency** — an edge saying one task may not start until another finishes.
Distinct from hierarchy: a parent is not automatically a dependency of its
children.

**Parent / epic / container** — a task that groups children. A container
organises work; it does not have to be executed by a worker itself.

**Deliverable** — a declared file, symbol, test or command that a task promises
to produce. Closing a task successfully reconciles the promise against the
worktree, so a pass with an unlisted gap is refused.
[`src/deliverables.py`](../../src/deliverables.py).

**Formula** — a reusable, parameterised task-graph template stored as vault
markdown and expanded into real tasks.
[`src/task_graph/formulas.py`](../../src/task_graph/formulas.py).

**Task graph / layout** — the dependency graph of a project and its computed
spatial arrangement for the dashboard's graph view.
[`src/task_graph/layout/`](../../src/task_graph/layout/).

## Agents and how they are chosen

**Agent** — a running coding assistant working on a task. In AQ every agent is
an external CLI running inside a terminal session; there is no in-process agent
implementation.

**Harness** — *which* CLI runs the agent. Shipped harnesses are `claude`,
`codex` and `gemini` ([`src/sessions/default_harnesses/`](../../src/sessions/default_harnesses/)).
A profile's `harness` field is the only thing that selects one.

**Agent profile (agent type)** — the durable definition of a kind of worker:
its harness, its role prompt, its tools, its lifecycle. Profiles are global
markdown under `vault/agent-types/<id>/profile.md` — one definition shared by
every project.

**Intelligence class** — how much thinking a task gets, independent of which
CLI runs it. Shipped classes are the cross product of `fast` / `standard` /
`deep` with `off` / `low` / `medium` / `high`
([`src/prompts/default_intelligence_classes/`](../../src/prompts/default_intelligence_classes/)).
A class resolves to a concrete provider model at launch.

**Provider / model** — the LLM vendor and the specific model a class resolves
to. A class is a policy; a model is what actually answers.

**Routing** — deciding which profile and intelligence class a task needs.
On current `main` this is a playbook, not orchestrator code: the orchestrator
emits `task.route_needed` for a task missing those fields and decides nothing
else ([`src/prompts/default_playbooks/default-assignment-routing.md`](../../src/prompts/default_playbooks/default-assignment-routing.md)).

**Profile pin** — an explicit `profile_id` on a task, which overrides routing.
Clearing the pin lets routing choose again.

**Lifecycle** — how sessions for a profile are created:
`task` (one session per assigned task, work is pushed), `pool` (a standing
worker that pulls work by claiming it) or `named` (a long-lived session
addressed by name, such as a supervisor).
[`src/profiles/parser.py`](../../src/profiles/parser.py).

**Pool** — the set of standing workers for one profile. Sizing is global per
profile; a separate placement step decides which project each start lands in.
[`src/orchestrator/pools.py`](../../src/orchestrator/pools.py).

**Claim** — a pool worker taking a `READY` task for itself. The claim is
recorded durably and mirrored into `.aq/claim.json` in the worker's workspace,
which is the worker's proof of what it holds.

**Claim epoch** — a fence number that increases each time a task is claimed.
An older epoch's writes are rejected, so a worker that was superseded cannot
overwrite the new owner's state.

## Sessions

**Session** — one terminal-hosted run of an agent CLI, managed as a tmux
session. Sessions have their own identity, transcript and lifetime, separate
from the task they are working on. [`src/sessions/`](../../src/sessions/).

**Attempt** — one recorded try at a task by a session, including the model that
served it. Attempts are how AQ attributes work to models; a profile is not
evidence of which model ran.

**Transcript** — the harness's own conversation log, read back by AQ for
progress and token accounting.
[`src/sessions/transcripts/`](../../src/sessions/transcripts/).

**Prime** — the opening context handed to a starting worker: its role, its
task, its workspace and project knowledge. Produced by `aq prime`
([`src/prime/`](../../src/prime/)).

**Drain** — asking a pool session to stop after its current task instead of
claiming another.

## Repositories and workspaces

**Project** — a registered Git repository plus its settings: default branch,
concurrency limits, integration mode. [`src/projects/`](../../src/projects/).

**Workspace** — a directory a task is allowed to work in. Workspaces are typed
and normalised: a *workspace kind* defines the type, and a task declares the
kinds it requires.

**Workspace kind** — the type of a workspace, defined as vault markdown. Shipped
kinds are `project-repo` (writable, exclusive lock), `vault` (auto-attached, no
lock) and `readonly-dir`.

**Worktree slot** — a numbered Git worktree under `.aq/worktrees/slot-N` that a
task borrows for the duration of its work. Slots are reset between tasks.

**Base repository** — the shared checkout the slots are worktrees of.

**Task branch** — the `aq/<task-id>` branch a task's commits land on. A task
never commits to your default branch.

## Delivery

**Integration** — everything between "a worker finished" and "the change is on
the default branch": validation, collection, publication and recovery.
[`src/integration/`](../../src/integration/).

**Source completion vs delivery** — a task closing successfully means its
branch is finished, not that it has been delivered. Delivery is a separate,
batched step the daemon performs.

**Development mode** — the integration mode where a worker pushes its own
branch and the daemon collects completed branches into validated batches. This
is the configured mode on this repository; hierarchy/train and verifier/repair
flows are optional compatibility modes, not defaults.

**Integration train / hierarchy mode** — an optional stricter mode in which
work is delivered through parent-owned integration branches.

## Automation

**Event** — something that happened, published on the in-process bus:
`task.completed`, `task.route_needed`, `gate.resolved`, `timer.15m`, and so on.
[`src/event_schemas.py`](../../src/event_schemas.py).

**Playbook (V2)** — automation authored as markdown and compiled into an
executable graph. Playbooks subscribe to events, run rules, call commands and
can pause for a human. [`src/playbooks/`](../../src/playbooks/). V1 playbooks
were removed; anything describing a compiler/runner/manager/store is historical.

**Rule** — one trigger-plus-steps unit inside a playbook.

**Playbook source / artifact / activation / run** — four different things: the
markdown you write, the compiled JSON it becomes, the decision to enable that
artifact in a scope, and one execution of it.

**Gate** — a pause that waits for a decision. A *human* gate waits for a person;
resolving it emits `gate.resolved`. A gate is not a task dependency.

**Default pipeline** — the shipped system-scope playbook. On current `main` it
handles approved specs and task-batch proposals only: it creates **no** per-task
reviewers, no final branch reviewers and no review or PR gates
([`src/prompts/default_playbooks/default-pipeline.md`](../../src/prompts/default_playbooks/default-pipeline.md)).

## Knowledge and configuration

**Configuration** — `~/.agent-queue/config.yaml`: connection strings, limits,
feature flags. Read by the daemon; some keys are live-reloadable and some are
startup-only. [`src/config.py`](../../src/config.py).

**Vault** — `~/.agent-queue/vault/`: Obsidian-compatible markdown that is
*content*, not settings — profiles, intelligence classes, playbooks, workspace
kinds, formulas, facts and knowledge bases. Edited by hand or by agents, watched
for changes.

**Facts** — small, timestamped statements stored as vault markdown and injected
into agent prompts.

**Memory** — the external `aq-memory` plugin's 4-tier knowledge store (identity,
facts, topic context, deep search). It is optional; in-tree facts and profile
parsing work without it.

**Prompt builder** — the layered assembly that turns role, overrides, facts,
context and tools into an agent's opening prompt.
[`src/prompt_builder.py`](../../src/prompt_builder.py).

## Extension and interfaces

**Plugin** — a package that adds commands, tools, events or CLI surface.
Internal plugins ship with the repository and are always loaded; external ones
are installed with `aq plugin install`. [`src/plugins/`](../../src/plugins/).

**MCP (Model Context Protocol)** — used in two directions that are easy to
confuse: AQ *exposes* its commands as MCP tools through an embedded server, and
a worker *connects to* external MCP servers declared in the vault.

**Dashboard** — the React web UI served by the daemon.
[`dashboard/src/`](../../dashboard/src/).

## Communication

**Message** — text delivered to a recipient: a session, a user, or a task.
Workers read theirs with `aq inbox`. [`src/messages/`](../../src/messages/).

**Digest** — the periodic Discord activity summary posted to one configured
channel. It is silent when nothing durable happened.
[`src/digest/`](../../src/digest/).

**Escalation** — a durable incident needing a human decision. It gets one
Discord thread; a reply in that thread reaches the project's supervisor.
[`src/escalations/`](../../src/escalations/). Discord is notification-only —
there are no AQ slash commands or task controls in Discord.

## Operations

**Doctor** — `aq doctor`, a registry of named checks that diagnose and
sometimes repair specific conditions. [`src/doctor/`](../../src/doctor/).

**Metrics** — one sample a second of the running fleet, rolled up per minute
and per hour. [`src/metrics/sampler.py`](../../src/metrics/sampler.py).

**Test slot** — a box-wide semaphore that `aq test` takes before running, so
many concurrent agents cannot each spawn a full test run.
[`src/resources/semaphore.py`](../../src/resources/semaphore.py).

**Scope** (database) — who is asking: `daemon`, `operator`, `worker`, `test` or
`cli`. Only `daemon` and `operator` may migrate the production database.
[`src/database/migration_guard.py`](../../src/database/migration_guard.py).
