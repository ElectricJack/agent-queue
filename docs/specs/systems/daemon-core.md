---
tags: [spec, system, daemon-core]
system: 1
status: current
as_of: 2026-10-20 (dev 22cffd1b7)
---

# System 1: daemon-core

This is a **current** system spec, not a design record. It describes what the
code at `dev` 22cffd1b7 does. Where it and the code disagree, the code is
right and this file should be fixed. The older subsystem specs it links to, such as
[`../orchestrator.md`](../orchestrator.md) and
[`../command-handler.md`](../command-handler.md), carry the `aq:historical`
banner. They are kept for the reasoning behind the design, not as a
description of what runs today.

daemon-core is system 1 of the 18 in the system inventory
(`~/.agent-queue/vault/projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, *in review*, revision 1). The parent vision and
system map (`2026-10-20-agent-queue-the-vision-and-the-system-map.md`, review
`rev-smart-nexus`, *in review*, revision 1) links here. Sibling specs live in
the same directory as `<system-slug>.md`.

## 1. Purpose and problem

Agent Queue runs many coding agents at once against shared git repositories,
and those agents are controlled from several places: the `aq` CLI, MCP
tools, the HTTP API, a Discord channel and playbooks. Without a single owner
of state, each of those surfaces would need its own rules for what may change
and when, and the scheduler would race the people and agents editing tasks.

daemon-core is that single owner. It is the long-running process that:

- **composes** every service into one process listening on port 8081
  (`src/main.py`);
- **owns the one write path**: every state change, from any surface, is a
  named command executed by one `CommandHandler` and checked against a typed
  contract;
- **runs the deterministic cycle**: a scheduler loop that, without any LLM
  call, decides what to start, sweeps what has gone stale and drives the
  periodic work of every other system in a fixed order;
- **publishes what happened** on an in-process, schema-validated event bus
  that playbooks, the dashboard WebSocket and gates subscribe to;
- **owns the PostgreSQL schema** (`src/database/tables.py`), its Alembic
  migrations and the guard that decides who may migrate which database.

Everything else in Agent Queue is built on these five things. The problem
daemon-core solves is keeping them small, ordered and predictable, so that
policy (which agent, which retry, which escalation) can live in playbooks and
profiles rather than in the scheduler. That split is
[the design principle](../design/guiding-design-principles.md) "mechanism in
code, policy in playbooks".

## 2. How it works today

### 2.1 The process and its composition root

`src/main.py` is the composition root. `main()` parses arguments, and
`run()` (`src/main.py:252`) builds the process in this order:

1. It scrubs inherited `AQ_*` variables so that a daemon started from inside
   an agent session does not inherit that session's identity, then calls
   `set_process_scope(DAEMON)` for the migration guard (§2.7).
2. It loads `AppConfig` from `~/.agent-queue/config.yaml` (`src/config.py`).
3. It builds `Orchestrator(config, runtimes=None)`. The `runtimes` argument is
   the empty injection seam described in §2.9.
4. It builds the `CommandHandler(orch, config)` and wires it into the
   orchestrator with `orch.set_command_handler(...)` **before**
   `await orch.initialize()`, because startup recovery already issues
   commands. It then registers the tool registry.
5. It starts the `MetricsSampler` and the messaging adapter (Discord, see
   [messaging-and-human-asks](messaging-and-human-asks.md)).
6. `run_scheduler` waits up to 120 s for the adapter, then binds the report
   outbox (`_bind_report_outbox`, `src/main.py:184`), which covers escalation
   delivery, the digest and the `DeliveryScheduler`. It starts the
   `ReviewNotifier`, optionally freezes the startup heap
   (`_freeze_startup_heap`), and enters `_run_scheduler_cycles` (§2.3).
7. The embedded MCP server and the HTTP API run under a supervised uvicorn
   task that restarts with backoff if it dies.
8. `_health_checks` (`src/main.py:608`) backs `/health` and readiness. It
   reports the orchestrator, required playbooks, database, agents, tasks,
   messaging, Discord cutover, the bot rate guard and the paused flags.

On shutdown `run()` closes the adapter, cancels MCP, stops metrics and calls
`orch.shutdown()` (§2.10). A requested restart re-executes the process with
`os.execv` after a 15 s grace.

`src/daemon_state.py` is a standard-library-only module for two files: the
`daemon.pid` file (with a PID ownership check) and the `daemon.stopped`
marker. The watchdog never starts the daemon while the marker exists, which
is how `aq stop` stays stopped. Operating the daemon (`aq restart`,
`aq update`, the watchdog itself) belongs to
[operations-and-recovery](operations-and-recovery.md).

### 2.2 Startup: `Orchestrator.initialize`

`Orchestrator` (`src/orchestrator/core.py:191`) is one class composed from
mixins: Triage, Workspace, Execution, Monitoring, Lifecycle, GitOps,
PRPolling, Context, Events, SyncWorkflow, Pools, LayoutStep, RouteNeeded and
ProviderFailover. `__init__` (`core.py:230`) creates the shared `EventBus` as
`self.bus` and the service objects. `initialize()` (`core.py:1901`) then runs
the startup sequence. The order matters, and several steps exist because of
an incident:

1. Orphan grace, then `db.initialize()`: schema setup through the migration
   guard (§2.7).
2. The session token store, profile sync and the pool-configuration warning.
3. `recover_pending_completions`: closes that were committed but not fully
   recorded before the last stop.
4. **Session adoption before stale-state recovery.** Live tmux sessions are
   re-adopted first (`adopt_on_start`). Only then does `_recover_stale_state`
   reset BUSY agents, release locks and sentinels, and move non-container
   IN_PROGRESS tasks back to READY. Reversing these two would kill work that
   survived a restart.
5. Container settlement, then seeding and probing provider availability.
6. The vault manager, task directories and vault structure, and one unified,
   tick-driven vault watcher with handlers for profiles, MCP servers,
   harnesses, intelligence classes and formulas.
7. Workspace kinds bootstrap and scan, and the facts watchers.
8. Playbooks V2, then the integration service, which runs its **own**
   durable reconciliation loop outside the cycle (see §5).
9. The override, README and workspace-spec watchers, the plugin registry and
   the tool index, and the config watcher (hot reload).
10. Gate sweep, a vault snapshot, profile and class migrations, derived
    worker rungs and startup scans.
11. Message delivery starts **last**, so that nothing is delivered to a
    session before the state it refers to has been recovered.

### 2.3 The scheduler cycle

#### The wake loop

`_run_scheduler_cycles` (`src/main.py:122`) runs **one cycle at a time**,
never two concurrently. Between cycles it waits for whichever comes first:

- a **wake event**. It subscribes to `SCHEDULER_WAKE_EVENTS`
  (`src/main.py:86`): `task.*` changes, `gate.resolved`/`gate.expired`,
  `provider.state_changed`/`provider.allocation_changed`,
  `pool.bounds_rescoped`, `session.started`/`exited`/`killed`/`drain_acked`
  and `config.reloaded`;
- the **periodic backstop**, `cycle_interval_seconds` (default 5.0,
  `src/config.py:665`).

Consecutive cycles are at least `min_cycle_interval_seconds` apart (default
1.0, `config.py:666`), so a burst of events coalesces into one cycle. A
cycle that raised waits the full interval before the next. A cycle running
longer than 30 s is reported with its await chain
(`_report_long_scheduler_cycle`), which is how a stuck step gets named.

A separate **pool watchdog** (`_run_pool_watchdog`, `src/main.py:106`) calls
`_check_pool_progress` every `POOL_WATCHDOG_INTERVAL` = 30 s
(`src/orchestrator/pools.py:81`). It keeps pool demand observable even when
an unrelated step in the cycle is blocked.

#### `run_one_cycle` ordering

`Orchestrator.run_one_cycle` (`core.py:3339`) is the heartbeat. **daemon-core
owns the order of its steps and their isolation. Each step's logic belongs
to the system named in the right-hand column**, and that system's spec
describes it.

Before the phases, the cycle schedules the tracked deploy check and runs the
`job_reconcile` command.

| Phase | Steps, in order | Owning system |
|---|---|---|
| 1. State | `_resume_paused_tasks`; `_sweep_gates` | [task-graph](task-graph.md), [playbooks](playbooks.md) |
| | `_check_defined_tasks` (promote DEFINED work whose dependencies are met) | [task-graph](task-graph.md) |
| | `_drain_branch_materializations` | [projects-workspaces-git](projects-workspaces-git.md) |
| | `router_readiness.refresh`, `_emit_route_needed_events` | [routing-and-profiles](routing-and-profiles.md) |
| | `_sweep_container_completion`, `_sweep_lifecycle` | [task-graph](task-graph.md) |
| | `_check_stuck_defined_tasks`, `_check_failed_blocked_tasks`, each in its own try/except | [task-graph](task-graph.md) |
| 2. Schedule and launch | `provider_availability.tick` | [providers](providers.md) |
| | hierarchy frontier modes, then `_schedule` (skipped when paused) | [scheduling-and-claims](scheduling-and-claims.md) |
| | reap finished `_running_tasks`; launch `_execute_task_safe` for each action | [sessions-and-harnesses](sessions-and-harnesses.md) |
| | `_reconcile_pools` | [scheduling-and-claims](scheduling-and-claims.md) |
| 3. Periodic services | ticks: timer service, morning report due, plugin cron, `vault_watcher.check`, workspace-spec watcher, escalation delivery, delivery scheduler, digest schedule | [playbooks](playbooks.md), [plugins-mcp-and-tools](plugins-mcp-and-tools.md), [configuration-and-vault](configuration-and-vault.md), [messaging-and-human-asks](messaging-and-human-asks.md) |
| | orphan workflow recovery; hourly LLM log cleanup; retention sweeps for operational events and test selection | [playbooks](playbooks.md), [providers](providers.md), [operations-and-recovery](operations-and-recovery.md), [test-service-and-jobs](test-service-and-jobs.md) |
| | `_maintain_conversations` | [messaging-and-human-asks](messaging-and-human-asks.md) |
| | `spec_ingest_reconcile` command as `ExecutionPrincipal.service("spec-ingest-reconcile")` | [reviews-and-knowledge](reviews-and-knowledge.md) |
| | `_auto_archive_tasks` | [task-graph](task-graph.md) |
| | `schedule_layout_step` (started, not awaited) | [task-graph](task-graph.md) |
| | playbook recovery, child reconciliation, timeouts and retention; `AgentWaitReconciler` tick | [playbooks](playbooks.md), [sessions-and-harnesses](sessions-and-harnesses.md) |
| 4. Housekeeping | `_reap_worktree_slots` (also restores disabled slots), merge-slot expiry, the daily branch sweep | [projects-workspaces-git](projects-workspaces-git.md), [integration-and-delivery](integration-and-delivery.md) |
| | `_reconcile_sessions` | [sessions-and-harnesses](sessions-and-harnesses.md) |
| | `_deliver_messages` | [messaging-and-human-asks](messaging-and-human-asks.md) |
| | `_revoke_expired_tokens` | daemon-core (session tokens, §2.4) |

If any step raises, the outer handler logs `Scheduler cycle error` and still
runs `_reconcile_pools` and `_reconcile_sessions` if the cycle had not
reached them, so that a broken sweep cannot also stop agents starting or
dying sessions being noticed. Counting `Scheduler cycle error` lines in the
daemon log is the first thing to check when a pool stops starting work.

#### `_schedule`

`_schedule` (`core.py:4649`) builds a pure `SchedulerState` snapshot and
hands it to the scheduler (`src/scheduler.py`). Before that it excludes:

- held tasks;
- supervisor-profile tasks;
- tasks refused delivery admission;
- tasks the hierarchy says are not runnable;
- withheld containers;
- unrouted tasks.

The scheduling rules themselves (fair share, budgets, pools) are
[scheduling-and-claims](scheduling-and-claims.md). daemon-core's part is
that this step is a pure function of a snapshot and makes no LLM call.

#### Launching a task

`_execute_task` (`src/orchestrator/execution.py:374`) does the following:

1. Re-checks constraints and hierarchy runnability.
2. Assigns the task, moves it to IN_PROGRESS and emits `task.started`.
3. Intercepts SYNC tasks.
4. Resolves the profile and launches a tmux session through
   `harness_registry.get(...)`. A missing harness is an explicit failure,
   never a fallback to some other runtime.

The launch does not wait for the agent. The agent finishes by running
`aq task close`, which is an ordinary command (§2.4). Prompt assembly
(`context.py`), session mechanics and harnesses are
[sessions-and-harnesses](sessions-and-harnesses.md).
`src/orchestrator/events.py` provides `_emit_task_event` and `_emit_notify`,
the two helpers the cycle uses to put events on the bus.

### 2.4 The command handler

`src/commands/handler.py` defines `CommandHandler` (`handler.py:358`),
assembled from 61 mixins in `src/commands/*_commands.py` that between them
define 491 `_cmd_<name>` methods. The CLI, MCP, the API, Discord, playbooks
and the cycle itself all call `execute(name, args)` (`handler.py:934`), which
runs this pipeline:

1. Strip the server-owned argument keys (`SERVER_OWNED_ARG_KEYS`,
   `src/commands/principal.py:72`) so that a caller cannot claim to be
   someone else.
2. Save the scope and principal context variables, and restore them when the
   command finishes, because a command can dispatch other commands.
3. Bind the operator dispatch (`src/commands/operator_authority.py`). An
   operator session may not act as a human at a gate or delivery decision,
   and setup commands need `operator_execute`. That refusal is returned at
   `handler.py:1027`.
4. Redact secrets from the arguments that will be logged, and resolve
   `project_id`.
5. **Subsystem pause gate** (`handler.py:1041`): when playbooks or memory are
   switched off in config, their commands are refused before any handler
   runs ([`../implementation/feature-pauses.md`](../implementation/feature-pauses.md)).
6. **Capability authorization**: `authorize_command`
   (`src/commands/authorization.py:138`) checks the caller's principal
   against the command's capability. Its mode is
   `security.capability_enforcement`, default `"audit"`
   (`src/config.py:2083`). In audit mode a would-be denial is logged and
   the command still runs.
7. **Routing-choice guard**: for the filing and project commands in
   `GUARDED_COMMANDS` (`src/routing/filing.py:104`), a hand-picked profile,
   provider, model or pin is refused the same way for every principal, and
   inert choices are stripped
   ([routing-and-profiles](routing-and-profiles.md)).
8. **Operator decisions**: a command that a recorded operator decision
   controls is refused through `src/operator_decisions.py`
   ([supervisors-and-policy](supervisors-and-policy.md)).
9. Dispatch to `_cmd_<name>`, falling back to a plugin command
   ([plugins-mcp-and-tools](plugins-mcp-and-tools.md)).
10. Emit `command.invoked` with the outcome, and log the timing, as a
    warning above 1 s.

Every command returns a dict with `success: bool`. Refusals are
`{"success": False, "error": "...", ...}` and often carry a `code`. The
exceptions are the three catch-all paths noted in §5.

Execution principals (`src/commands/principal.py`) say who is acting: an
operator, a worker session holding a scoped token, or a named service such as
`spec-ingest-reconcile`. Worker session tokens are issued by the session token
store at launch and revoked by `_revoke_expired_tokens` in the cycle.

### 2.5 Command contracts

`src/commands/contracts/` describes commands as data. `ContractRegistry`
(`registry.py`; the shared instance is
`CONTRACTS = ContractRegistry(autoload=True)` at `registry.py:160`) loads
`builtin.py` and about 28 topic modules (ask, wait, message_wait, integration,
job and others). It currently holds 214 typed contracts. A contract names the
command's arguments, its outcomes and its result fields. The registry also
computes a **fingerprint** for each contract. Reviewed playbook bundles record
the fingerprints they were checked against, so changing an existing contract's
outcomes or result fields makes those bundles stale. Prefer adding a new
contract to widening an existing one.

The CLI and generated clients read the same contracts.
`src/surface_schema.py` serves the offline enum catalog the CLI uses when the
daemon is unreachable. The design of the surface is
[`../design/aq-surface.md`](../design/aq-surface.md), and the generated
artefacts belong to [api-and-clients](api-and-clients.md).

### 2.6 The event bus

`src/event_bus.py` is a small, in-process, non-durable publish/subscribe
bus. Its event types are declared in `src/event_schemas.py`, which has 224
schemas.

- `emit(event_type, payload)` stamps `event_id` and `_event_type` onto the
  payload dict and validates it against the schema. An invalid payload
  raises `EventValidationError` in development and logs a warning in
  production.
- It then awaits each direct subscriber, followed by each `"*"` wildcard
  subscriber, **sequentially and without catching their exceptions**. A
  handler's exception reaches the code that emitted.
- It records `seen_event_types` for the doctor check `events.registry`, and
  offers `wait_for` / `waiter` for code that needs to await a specific event.

There are 13 subscription sites. The three wildcard subscribers are:

- the playbook runtime (`src/playbooks/runtime.py:263`), which turns each
  event into its own asyncio task under a detached commit context, so a slow
  playbook never blocks the emitter;
- the gate sweep (`core.py:3986`);
- the dashboard WebSocket (`src/api/websocket.py:254`).

Because the bus is not durable, an event emitted while nobody is listening is
gone. That is why the scheduler keeps its periodic backstop, and why durable
work (integration, message delivery, waits) keeps its own tables and
reconcilers rather than trusting the bus.

### 2.7 Database, schema and the migration guard

PostgreSQL is the only backend. The schema is SQLAlchemy Core `Table`s in
`src/database/tables.py`, which has 187 tables. Queries live in 81 modules
under `src/database/queries/`. `migrations/versions/` holds a squashed
baseline plus incremental revisions, with head `a00000000102`. The how-to is
[`../../guides/migrations.md`](../../guides/migrations.md), and the tables
are documented in [`../../reference/database/`](../../reference/database/README.md).
**Each system spec names the tables it owns.** daemon-core owns the schema
mechanism, not the meaning of individual tables.

`src/database/engine.py` creates the engine (`create_postgres_engine`, with
an `ObservedQueuePool` for pool metrics). `run_schema_setup`
(`engine.py:412`) does the following:

1. Pre-flight checks `alembic_version`.
2. Asks the guard for a decision.
3. Either upgrades or verifies the schema is at head.
4. Runs startup data migrations.

`src/database/migration_guard.py` exists because of the 2026-09-02 incident,
in which worker sessions stamped the production database with revisions that
had not been merged. It separates two questions:

- **Who is asking** is the process scope: DAEMON, OPERATOR, WORKER, CLI or
  TEST. The scope comes from the `AQ_DB_SCOPE` environment variable, which
  overrides `set_process_scope`. Worker sessions are launched with
  `AQ_DB_SCOPE=worker` and a refusal sentinel in place of the database URL.
- **What they point at** is answered by `is_production_database`.

`migration_decision` returns `MIGRATE` only for the daemon or an operator
against the production database, or for tests against their own database.
Everyone else gets `VERIFY`. Verifying raises one of these:

- `SchemaBehindCode`: "schema behind code; ask the operator to upgrade";
- `SchemaAheadCode`, subject to `SchemaAheadPolicy`;
- `MigrationRefused`.

That refusal is the guard working, not a bug to route around.
`src/database/additive_migrations.py` is a conservative static check that a
release's revisions are additive. A revision it cannot prove additive (raw
SQL, imported helpers) requires a breaking release. `schema_key.py` gives the test template databases a schema identity
for caching.

### 2.8 Models

`src/models.py` holds the dataclasses and enums shared across systems,
including `TaskStatus` (9 states), task, agent and session records. The
transition rules between task states (`src/state_machine.py`) and what each
state means for readiness belong to [task-graph](task-graph.md). Today the
`TaskStatus` docstring states that transitions are not enforced in
production. The planned hibernation work changes that for suspended work
(§6.2).

### 2.9 The seams that hold nothing

- `src/runtimes/` is an injection seam. `default_registry()` returns no
  runtimes, and the profile keys `runtime` and `agent_name` are rejected.
  Every agent is a CLI in a tmux session, selected by the profile's
  `harness` ([sessions-and-harnesses](sessions-and-harnesses.md)). The
  historical [`../runtimes/`](../runtimes/README.md) specs describe a layer
  that does not ship.
- `src/services/` contains only `__init__.py`. Services are built in
  `Orchestrator.__init__` and `src/main.py` (see §5).

### 2.10 Shutdown and restart

`Orchestrator.shutdown()` (`core.py:3249`) runs in this order:

1. Cancel the deploy check and pending claim preparations.
2. Stop message delivery.
3. Close provider availability.
4. Wait for pool launches, then cancel them.
5. Wait 10 s for running task coroutines.
6. Cancel the branch sweep.
7. Wait 30 s for the layout step.
8. Stop the spec watcher and the integration service.
9. Cancel the promotion check.
10. Stop the record outbox and the knowledge-generation loop.
11. Stop the vault, config and timer services.
12. Shut down the playbook manager.
13. Close the database.

Shutdown does **not** kill agent tmux sessions. `aq restart` relies on this:
the restarted daemon re-adopts them (§2.2, step 4). Only `aq stop` without
`--keep-sessions` kills them.

## 3. Boundaries and interactions

### 3.1 Who owns `src/orchestrator/`

The package is split by file across systems 1, 3 and 6. Every orchestrator
file has one owning spec. Files the inventory left unassigned are assigned
here.

| Files | Owner |
|---|---|
| `core.py` (class, `initialize`, `run_one_cycle` ordering, `shutdown`), `events.py`, `execution.py` (the launch path), `monitoring.py` (per-cycle checks and notifications), `context.py` (prompt context assembly; its content is owned by sessions-and-harnesses) | **1 daemon-core** |
| `pools.py`, `agent_reconciler.py`, `layout_step.py`, `merge_slot.py`, `route_needed.py`, `stranded_work.py`, `task_checkpoint.py`, `triage.py`, `sync_workflow.py`, `pr_polling.py` | [3 scheduling-and-claims](scheduling-and-claims.md) |
| `worktree_manager.py`, `workspace.py`, `workspace_claim_recovery.py`, `workspace_attachments.py`, `git_ops.py`, `base_workspace.py`, and the previously unassigned `retained_workspace.py`, `slot_restore.py`, `workspace_binding_audit.py` | [6 projects-workspaces-git](projects-workspaces-git.md) |
| `lifecycle.py` (re-checks BLOCKED/PAUSED work that has stopped moving; previously unassigned) | [2 task-graph](task-graph.md) |
| `provider_failover.py` | [12 providers](providers.md) |

`core.py` is 4,980 lines and holds methods for most of those systems. A
method's owner is the system whose behaviour it implements, wherever the method
sits. daemon-core owns *where in the cycle it runs* and *how its failure
is isolated*.

### 3.2 Interactions with the other 17 systems

| # | System | How it meets daemon-core |
|---|---|---|
| 2 | [task-graph](task-graph.md) | Supplies the state machine and readiness rules. The cycle runs its sweeps in phase 1. `TaskStatus` lives in `src/models.py`. |
| 3 | [scheduling-and-claims](scheduling-and-claims.md) | `_schedule` hands it a snapshot. The cycle runs `_reconcile_pools`, and the pool watchdog runs beside the cycle. Claims are commands. |
| 4 | [routing-and-profiles](routing-and-profiles.md) | Router readiness and `task.route_needed` in phase 1; the routing-choice guard in `execute`; profiles are resolved at launch. |
| 5 | [sessions-and-harnesses](sessions-and-harnesses.md) | Launch goes through `harness_registry`. Session adoption runs at startup and `_reconcile_sessions` every cycle. Session tokens are principals. |
| 6 | [projects-workspaces-git](projects-workspaces-git.md) | Workspace preparation at launch, slot reaping in phase 4, async git only. |
| 7 | [integration-and-delivery](integration-and-delivery.md) | Started in `initialize`, but runs its own reconciliation loop. The cycle runs the branch sweep and deploy check. |
| 8 | [playbooks](playbooks.md) | The largest event subscriber. Playbooks change state only by calling commands. Gates, timers and agent cron tick in the cycle. |
| 9 | [messaging-and-human-asks](messaging-and-human-asks.md) | The adapter and report outbox are bound in `main.py`; `_deliver_messages` and the delivery ticks run in the cycle. |
| 10 | [supervisors-and-policy](supervisors-and-policy.md) | Operator decisions and the operator-session limits are steps in `execute`. Supervisor tasks are excluded from `_schedule`. |
| 11 | [reviews-and-knowledge](reviews-and-knowledge.md) | `spec_ingest_reconcile` runs in phase 3 as a service principal; the review notifier starts in `main.py`. |
| 12 | [providers](providers.md) | `provider_availability.tick` opens phase 2. Provider state changes wake the cycle. |
| 13 | [configuration-and-vault](configuration-and-vault.md) | `AppConfig` is loaded first. Config and vault watchers hot-reload, and `config.reloaded` wakes the cycle. |
| 14 | [api-and-clients](api-and-clients.md) | The API is served by the daemon's uvicorn and calls `execute`. Contracts feed codegen. |
| 15 | [dashboard](dashboard.md) | Reads the API, and subscribes to every event through the WebSocket wildcard. |
| 16 | [test-service-and-jobs](test-service-and-jobs.md) | `job_reconcile` runs at the top of each cycle; test-selection retention runs in phase 3. |
| 17 | [operations-and-recovery](operations-and-recovery.md) | Owns start, stop, restart, update, the watchdog and doctor. `_health_checks` and `daemon_state.py` are the daemon's side of that. |
| 18 | [plugins-mcp-and-tools](plugins-mcp-and-tools.md) | The embedded MCP server, plugin commands as the `execute` fallback, and plugin cron in the cycle. |

## 4. Design principles and invariants

These hold today, and changes to daemon-core must keep them.

1. **One write path.** Every state change goes through
   `CommandHandler.execute`, including changes made by the cycle and by
   playbooks. No surface writes to the database directly, and no surface
   carries its own copy of a rule. ([`../design/aq-surface.md`](../design/aq-surface.md))
2. **Commands return `{"success": bool, ...}`.** Refusals are data, not
   exceptions, and carry an `error` and usually a `code`.
3. **The caller does not choose its identity.** Server-owned arguments are
   stripped, and the principal comes from the token or the service name.
4. **The cycle is deterministic and makes no LLM calls.** Judgment such as
   reflection, failover choice and escalation is a playbook calling commands.
5. **One cycle at a time.** It is woken by events and backed by a period, and
   events are a latency optimisation, never a correctness requirement. A
   cycle given no events and a fake clock must still converge.
6. **A broken step does not stop the fleet.** Steps that have caused outages
   are individually isolated. In the fair-grove-86 incident, one sweep's
   exception stopped scheduling for 53 minutes. Pool reconciliation
   and session reconciliation run even when an earlier step raised.
7. **Startup order is recovery order.** Adopt live sessions before resetting
   stale state, and deliver messages last.
8. **Async first.** No `subprocess.run()` in production code. Git goes
   through `GitManager`'s `a`-prefixed API. A blocking call in the cycle
   stalls every agent
   ([`../design/event-loop-responsiveness.md`](../design/event-loop-responsiveness.md)).
9. **Events are validated and typed.** Every emitted type has a schema in
   `src/event_schemas.py`, and doctor reports emitted types that lack one.
10. **The schema changes only through a revision.** Never change
    `tables.py` without one. Revisions are idempotent, guarded with
    `sa.inspect(bind)`, and PostgreSQL only. Constraints are named,
    `server_default` takes the bare value, and JSON columns are `JSONB`
    (see the rules in [`AGENTS.md`](../../../AGENTS.md)).
11. **Only the daemon or an operator migrates the operator's database.**
    Workers verify, tests migrate their own databases, and
    `SchemaBehindCode` is reported, never worked around.
12. **No in-process agent runtime.** Agents are external CLIs in tmux; a
    missing harness fails the launch.

## 5. Known gaps

- **Stale docstrings.** The module docstring of `src/orchestrator/core.py`
  describes `adapter.start`/`wait`, `src/adapters/` and `_phase_verify`,
  none of which exist. The `CommandHandler` docstring cites
  `src/tool_registry.py`, `src/supervisor.py` and `src/discord/commands.py`,
  which are also gone. `src/database/__init__.py` lists a `schema.py` and
  SQLite PRAGMA handling. A reader new to the code is misled by all three.
- **Three return paths break the `success` contract.** `execute` returns a
  bare `{"error": ...}` with no `success` key for a failed plugin command
  (`handler.py:1175`), an unknown command (`handler.py:1182`) and an
  unexpected exception (`handler.py:1193`). Callers must treat a missing
  `success` as failure. The hibernation epic maps `InvalidTransition` to a
  proper refusal (§6.2), but not these three.
- **The event bus is fragile.** A subscriber's exception propagates to the
  emitter, so a buggy dashboard or gate handler can fail a command that has
  already committed. `emit` mutates the caller's payload dict. Events are not
  durable, so anything missed while no subscriber is listening is lost.
- **The cycle is long and of mixed ownership.** `run_one_cycle` drives work
  for most of the other systems from one method in a 4,980-line file. Only some steps
  are individually isolated, and a slow step delays everything after it.
  The integration-ownership size budget
  (`tests/integration_ownership.py`, `tests/integration_ownership.json`)
  charges new lines in `Orchestrator.__init__` and `run_one_cycle`. That
  slows growth but does not split the method.
- **Two loops, one daemon.** The integration service runs its own durable
  reconciliation loop, the pool watchdog runs beside the cycle, and the
  waits task will join them (§6.2). Their ordering relative to the cycle is
  not written down anywhere except in code.
- **`src/services/` is empty.** Services are constructed ad hoc in
  `Orchestrator.__init__` and `main.py`, so there is no single list of what
  the daemon runs.
- **Authorization audits rather than enforces.**
  `security.capability_enforcement` defaults to `"audit"`. A worker token
  calling an operator command is logged, not refused, unless the command's
  own scope check refuses it.
- **Task transitions are not enforced.** `TaskStatus` transitions are
  checked by the state machine only where callers use it (§2.8).
- **Historical specs are not marked superseded by this one.**
  [`../orchestrator.md`](../orchestrator.md),
  [`../command-handler.md`](../command-handler.md),
  [`../event-bus.md`](../event-bus.md), [`../main.md`](../main.md),
  [`../database.md`](../database.md) and
  [`../models-and-state-machine.md`](../models-and-state-machine.md) still
  read as the subsystem description. Their banner should point here once the
  system-spec set is accepted.

## 6. Planned work

Tasks and reviews were re-queried on 2026-10-20 for this section. No open
epic is about daemon-core alone. The items below change daemon-core files or
invariants as part of another system's work.

### 6.1 Fast and smooth integration: `stark-willow-95` (IN_PROGRESS)

Design: `~/.agent-queue/vault/projects/agent-queue/specs/2026-10-12-fast-smooth-integration.md`.
Its review `rev-fair-lantern` is *approved* at revision 2, although the
file's frontmatter says revision 3. The decision notes are
`2026-10-13-fast-smooth-integration-rev2-decisions.md`,
`2026-10-13-fast-smooth-integration-rev3-playbook-policy.md` and
`2026-10-20-fast-smooth-integration-rev4-retrospective-decisions.md`. The
rev4 decisions absorb the withdrawn stall retrospective (`rev-quick-pinnacle`,
*withdrawn*). The epic branch is
`aq/epic/fast-smooth-integration-implementation`. Integration behaviour is
[integration-and-delivery](integration-and-delivery.md). What it changes
here:

- **`.7` (READY): replace the timed train window with idle/busy dispatch.**
  The vision spec lists this as daemon-core's open work. The integration
  train dispatches when work is ready and capacity is idle, instead of on a
  timer. Its acceptance includes "a fake clock with no events changes
  nothing", which is invariant 5 applied to integration. It depends on
  `bold-zenith-11.7`, `.8`, `.11` and `.12`.
- **`.19` (COMPLETED on the epic branch, not yet in `dev`): the git-truth
  reconciler.** `initialize` builds a `GitTruthReconciler`. Every cycle runs it
  through the command `integration_reconcile_git_truth` under
  `ExecutionPrincipal.service("git-truth-reconciler")`, replacing the
  earlier owner-recovery sweep entry. It adds the
  `integration.readiness_changed` event schema. This is a new phase-3 step
  that goes through the one write path.
- **`.13` (READY): one admission view.** The frontier, pools, `aq task
  explain` and the delivery guards will share one admission decision. Several
  of `_schedule`'s exclusions (§2.3) become a single call.
- **`.3` (READY): redrive stuck delivery once.**
- `.11` (DEFINED: warm workspaces and compiler cache) and the DEFINED
  `.5`, `.8`, `.9`, `.14`, `.15`, `.17`, `.18` belong to systems 6, 7 and 16.
  `.1`, `.2`, `.6` and `.20` are completed.

### 6.2 Event-driven hibernation and verified waits: `bold-zenith-11` (IN_PROGRESS)

Design: `2026-10-13-event-driven-agent-hibernation-and-verified-waits.md`
(review `rev-bold-pinnacle`, *approved*, revision 2; decisions in
`2026-10-13-hibernation-rev2-decisions.md`). Implementation plan:
`2026-10-13-event-driven-agent-hibernation-and-verified-waits-implementa.md`
(review `rev-fleet-orbit`, *approved*, revision 1). Every child except `.22`
is COMPLETED on the epic branch
`aq/epic/event-driven-hibernation-and-verified-waits-implementation`
(50f65ef43), which has not reached `dev`. The behaviour belongs to
[sessions-and-harnesses](sessions-and-harnesses.md) and
[task-graph](task-graph.md). When the branch lands it changes daemon-core in
these ways:

- **Composition root.** `main.py` sets `orch.wait_service =
  WaitService(orch)` and starts an `aq-waits` task next to the review
  notifier, cancelled in the same `finally`. That is another loop beside
  the cycle (§5).
- **Cycle.** `wait_service.tick()` runs as recovery at the
  `AgentWaitReconciler` site in phase 3. A config reload refreshes the wait
  source commit.
- **Command pipeline.** `CommandHandler` gains `WaitV2CommandsMixin` and a
  `_wait_mutation_fence` step that refuses a mutation conflicting with a
  pending wait. `InvalidTransition` becomes
  `{"success": False, "code": ..., "error": ...}` instead of an exception.
- **Models.** `TaskStatus` gains WAITING, HIBERNATED and RESUME_PENDING, plus
  `SUSPENDED_TASK_STATUSES` and `OPEN_TASK_STATUSES`. Transition validation
  is **enforced** for suspended work, closing part of the §5 gap.
- **Schema.** New packages `src/waits` and `src/wait_runtime`, about 372 new
  lines in `tables.py`, and migrations `a00000000103` to `a00000000106`.
  Sibling branches that also claim `a00000000103` must re-chain after
  rebasing.
- **`.22` (READY): the cutover.** It flips the defaults, refuses v1 wait
  bodies with `wait.upgrade_required`, ships the doctor checks
  `waits.schema` and `waits.orphans`, and adds the runbook.

### 6.3 Distributed worker nodes (in review, not filed)

`2026-10-06-distributed-worker-nodes.md`, review `rev-eager-quest`, *in
review*, revision 2. If accepted, tmux execution moves out of the daemon into
a worker service, even on a single machine. The launch path in §2.3 would
then become a request to a node, session adoption and `_reconcile_sessions`
would read node state, and the daemon would stop being the process that owns
agent sessions. No tasks are filed. The related
`2026-10-06-containerized-sandboxed-and-team-hosted-agent-queue.md`
(`rev-steady-ember`) is *withdrawn*.

### 6.4 Human-ask cutover: `calm-grove-14` (READY), `vivid-pinnacle-60`, `vivid-rapids-36`

This finishes moving human questions onto the ask model
([`../design/human-ask-cutover-status.md`](../design/human-ask-cutover-status.md)).
daemon-core is touched only lightly, through the report and conversation
outbox binding in `main.py` (`_bind_report_outbox`,
`_bind_conversation_outbox`). The rest is
[messaging-and-human-asks](messaging-and-human-asks.md).

### 6.5 The system-spec set: `eager-vault-55` (this epic)

The inventory (`rev-bright-rapids`) and the vision (`rev-smart-nexus`) are in
review. This spec is one of the 18 children. When the set is accepted, the
historical banners in §5 should point to the matching system spec.

Other open work was checked and does not change daemon-core: the held
spec-ingestion phases (`sharp-cascade-53`, `nimble-lantern-37`,
`calm-orbit-69`), the playbook epics (`wise-orbit-24`, `agile-vault-38`,
`clear-impact-72`) and the operational promote task.

## 7. Further reading

- [`../../../profile.md`](../../../profile.md): architecture and design decisions.
- [`../../contributing/repo-map.md`](../../contributing/repo-map.md): package ownership.
- [`../../concepts/architecture.md`](../../concepts/architecture.md),
  [`../../reference/modules/architecture.md`](../../reference/modules/architecture.md),
  [`../../reference/modules/database.md`](../../reference/modules/database.md).
- [`../../reference/database/`](../../reference/database/README.md): tables,
  migrations, queries and data lifecycle.
- [`../design/guiding-design-principles.md`](../design/guiding-design-principles.md),
  [`../design/aq-surface.md`](../design/aq-surface.md),
  [`../design/event-loop-responsiveness.md`](../design/event-loop-responsiveness.md),
  [`../design/trust-and-ops.md`](../design/trust-and-ops.md).
- [`../../guides/migrations.md`](../../guides/migrations.md),
  [`../../guides/resource-gating.md`](../../guides/resource-gating.md).
- Historical: [`../main.md`](../main.md), [`../orchestrator.md`](../orchestrator.md),
  [`../command-handler.md`](../command-handler.md), [`../event-bus.md`](../event-bus.md),
  [`../database.md`](../database.md),
  [`../models-and-state-machine.md`](../models-and-state-machine.md).
