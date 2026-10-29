---
tags: [spec, system, daemon-core]
system: 1
status: current
as_of: 2026-10-22 (dev 52d2efe4c)
---

# System 1: daemon-core

This is a **current** system spec, not a design record. It describes what the
code at the assigned `origin/dev` snapshot 52d2efe4c does. Where it and the code
disagree, the code is right and this file should be fixed. The older subsystem specs it links to, such as
[`../orchestrator.md`](../orchestrator.md) and
[`../command-handler.md`](../command-handler.md), carry the `aq:historical`
banner. They are kept for the reasoning behind the design, not as a
description of what runs today.

daemon-core is system 1 of the 18 in the system inventory
(`~/.agent-queue/vault/projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`).
Sibling specs live in the same directory as `<system-slug>.md`. This page
records implemented behaviour; historical proposals and task statuses are not
statements about the running daemon.

## 1. Purpose and problem

Agent Queue runs many coding agents at once against shared git repositories,
and those agents are controlled from several places: the `aq` CLI, MCP
tools, the HTTP API, a Discord channel and playbooks. Without a single owner
of state, each of those surfaces would need its own rules for what may change
and when, and the scheduler would race the people and agents editing tasks.

daemon-core is that single owner. It is the long-running process that:

- **composes** every service into one process listening on port 8081
  (`src/main.py`);
- **owns the command boundary**: CLI, MCP, API, Discord and playbooks dispatch
  operational mutations through one `CommandHandler`; domain services and
  database queries implement the underlying state changes;
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
`run()` (`src/main.py`) builds the process in this order:

1. It removes inherited harness-session markers and calls
   `set_process_scope(DAEMON)` for the migration guard (§2.7). It warns about
   inherited `AQ_*` variables; it does not remove them. In particular,
   `AQ_DB_SCOPE=worker` still overrides the process scope.
2. It loads `AppConfig` from `~/.agent-queue/config.yaml` (`src/config.py`),
   or the positional config path. `--profile` selects a configuration overlay;
   `--validate-config` validates and exits without starting the daemon.
3. It builds `Orchestrator(config, runtimes=None)`. The `runtimes` argument is
   the empty injection seam described in §2.9.
4. It builds the `CommandHandler(orch, config)` and wires it into the
   orchestrator with `orch.set_command_handler(...)` **before**
   `await orch.initialize()`, because startup recovery already issues
   commands. It then registers the tool registry.
5. It starts the `MetricsSampler` and the messaging adapter (Discord, see
   [messaging-and-human-asks](messaging-and-human-asks.md)).
6. `run_scheduler` waits up to 120 s for the adapter, then binds the report
   outbox (`_bind_report_outbox`, `src/main.py`), which covers escalation
   delivery, the digest and the `DeliveryScheduler`. It starts the
   `ReviewNotifier` and the independent `WaitService.run()` task (`aq-waits`),
   optionally freezes the startup heap (`_freeze_startup_heap`), and enters
   `_run_scheduler_cycles` (§2.3). The report outbox is bound only when a
   usable Discord transport/channel is available.
7. The embedded MCP server and the HTTP API run under a supervised uvicorn
   task that restarts with backoff if it dies.
8. `_health_checks` (`src/main.py`) backs `/health` and readiness. It
   reports the orchestrator, required playbooks, database, agents, tasks,
   messaging, Discord cutover, the bot rate guard and the paused flags.
   The HTTP health path uses a cached snapshot so probes do not queue database
   reads behind a busy scheduler; see
   [event-loop responsiveness](../design/event-loop-responsiveness.md).

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

`Orchestrator` (`src/orchestrator/core.py`) is one class composed from
mixins: Triage, Workspace, Execution, Monitoring, Lifecycle, GitOps,
PRPolling, Context, Events, SyncWorkflow, Pools, LayoutStep, RouteNeeded and
ProviderFailover. `__init__` (`core.py`) creates the shared `EventBus` as
`self.bus` and the service objects. `initialize()` (`core.py`) then runs
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
   survived a restart. Stale-job recovery runs before workspace cleanup:
   interrupted job snapshots and live workspace pins are preserved while their
   owners are reconciled.
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

`_run_scheduler_cycles` (`src/main.py`) runs **one cycle at a time**,
never two concurrently. Between cycles it waits for whichever comes first:

- a **wake event**. It subscribes to `SCHEDULER_WAKE_EVENTS`
  (`src/main.py`): the listed task changes (not a wildcard subscription), `gate.resolved`/`gate.expired`,
  `provider.state_changed`/`provider.allocation_changed`,
  `pool.bounds_rescoped`, `session.started`/`exited`/`killed`/`drain_acked`
  and `config.reloaded`;
- the **periodic backstop**, `cycle_interval_seconds` (default 5.0,
  `src/config.py`).

Consecutive cycles are at least `min_cycle_interval_seconds` apart (default
1.0, `config.py`), so a burst of events coalesces into one cycle. A
cycle that raised waits the full interval before the next. A cycle running
longer than 30 s is reported with its await chain
(`_report_long_scheduler_cycle`), which is how a stuck step gets named.

A separate **pool watchdog** (`_run_pool_watchdog`, `src/main.py`) calls
`_check_pool_progress` every `POOL_WATCHDOG_INTERVAL` = 30 s
(`src/orchestrator/pools.py`). It keeps pool demand observable even when
an unrelated step in the cycle is blocked.

#### `run_one_cycle` ordering

`Orchestrator.run_one_cycle` (`core.py`) is the heartbeat. **daemon-core
owns the order of its steps and their isolation. Each step's logic belongs
to the system named in the right-hand column**, and that system's spec
describes it.

Before the phases, the cycle schedules the tracked deploy check and reconciles
jobs. Currently this calls `_cmd_job_reconcile({})` directly rather than
`execute`; see the command-boundary gap in §5.

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
| | orphan workflow recovery; hourly LLM log cleanup; retention sweeps for operational events, observation snapshots and test selection | [playbooks](playbooks.md), [providers](providers.md), [operations-and-recovery](operations-and-recovery.md), [test-service-and-jobs](test-service-and-jobs.md) |
| | `_maintain_conversations` | [messaging-and-human-asks](messaging-and-human-asks.md) |
| | `spec_ingest_reconcile` command as `ExecutionPrincipal.service("spec-ingest-reconcile")` | [reviews-and-knowledge](reviews-and-knowledge.md) |
| | `_auto_archive_tasks` | [task-graph](task-graph.md) |
| | `schedule_layout_step` (started, not awaited) | [task-graph](task-graph.md) |
| | playbook recovery, child reconciliation, timeouts and retention | [playbooks](playbooks.md), [sessions-and-harnesses](sessions-and-harnesses.md) |
| 4. Housekeeping | `_reap_worktree_slots` (also restores disabled slots), the daily background branch sweep | [projects-workspaces-git](projects-workspaces-git.md), [integration-and-delivery](integration-and-delivery.md) |
| | `_reconcile_sessions` | [sessions-and-harnesses](sessions-and-harnesses.md) |
| | `_deliver_messages` | [messaging-and-human-asks](messaging-and-human-asks.md) |
| | `_revoke_expired_tokens` | daemon-core (session tokens, §2.4) |

Several stages catch and log their own exceptions. If an exception reaches
the outer cycle handler, it logs `Scheduler cycle error` and attempts pool and
session reconciliation if that cycle has not already attempted them. This
recovers from exceptions, not from an await that never returns: there is no
whole-cycle deadline.

`_reconcile_sessions` bounds each cascade stage with
`SESSION_STAGE_TIMEOUT` (60 seconds): transcript watching,
agent questions, collaboration, agent waits, verified-wait recovery, agent cron
and the session reconciler. A timed-out stage is cancelled and retried next
cycle; subsequent stages can still run. Owner recovery is scheduled separately.

#### Work beside the cycle

| Work | Lifetime and recovery |
|---|---|
| Integration service | Starts in `initialize()` and runs its own durable maintenance loop, including Git truth reconciliation through `integration_reconcile_git_truth` under a service principal. |
| Message delivery | Starts last in `initialize()` and delivers independently of a slow cycle. `_deliver_messages` remains a maintenance/recovery hook. |
| Verified waits | `main.py` constructs `WaitService` and starts `aq-waits` beside the review notifier; `_reconcile_sessions` also ticks it for recovery. |
| Pool watchdog | Runs beside the scheduler loop, checking pool progress every 30 seconds. |
| Layout, branch sweep, deploy check | Scheduled background tasks with their own lifecycle; the cycle does not await their full work. |

These tasks share the asyncio event loop. Their independence does not protect
against synchronous blocking code. None has a guaranteed order relative to an
unrelated cycle stage.

#### `_schedule`

`_schedule` (`core.py`) builds a pure `SchedulerState` snapshot and
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

`_execute_task` (`src/orchestrator/execution.py`) does the following:

1. Re-checks constraints and hierarchy runnability.
2. Assigns the task, moves it to IN_PROGRESS and emits `task.started`.
3. Intercepts SYNC tasks.
4. Resolves the profile and launches a tmux session through
   `harness_registry.get(...)`. A missing harness is an explicit failure,
   never a fallback to some other runtime.

This is the task-launch path. A pool worker's harness is already running
before it calls `aq task claim`; claiming does not launch another harness.
The launch does not wait for the agent. The agent finishes by running
`aq task close`, which is an ordinary command (§2.4). Prompt assembly
(`context.py`), session mechanics and harnesses are
[sessions-and-harnesses](sessions-and-harnesses.md).
`src/orchestrator/events.py` provides `_emit_task_event` and `_emit_notify`,
the two helpers the cycle uses to put events on the bus.

### 2.4 The command handler

`src/commands/handler.py` defines `CommandHandler`, composed from domain
command mixins. Supported command surfaces call `execute(name, args)`. Its
pipeline is:

1. Establish correlation context; strip `SERVER_OWNED_ARG_KEYS`. An inherited
   principal takes precedence over a nested caller's proposed identity. Save
   scope/principal context variables and restore them in `finally`.
2. Bind operator dispatch authority, redact logged arguments, apply any
   operator-session refusal and resolve `project_id`.
3. Apply subsystem pause gates and `_wait_mutation_fence`, which refuses
   mutations that conflict with a pending verified wait.
4. Run capability authorization, then resource/scope authorization. These
   are separate gates: capability audit mode does not bypass resource scope.
5. Apply the routing-choice guard and recorded operator decisions.
6. Dispatch to `_cmd_<name>` or a plugin command, then classify the result.
   Unknown commands and unexpected exceptions return `success: false` with
   `unknown_command` or `command_runtime_error` codes. `InvalidTransition`
   becomes a structured refusal; enforced principals receive redacted errors.
7. Restore caller context; emit `command.invoked` when command-event emission
   is enabled, except for `session_input`. Event-emission errors are isolated.
   Log slow commands (at least one second), including dispatch/event timings.

Commands return `{"success": bool, ...}`. Refusals contain `error` and often
`code`; typed contracts describe specific outcomes. `execute` is not a global
Pydantic argument validator for every handler.

Execution principals (`src/commands/principal.py`) distinguish local callers,
operator sessions, scoped worker sessions and named services. Session tokens
are issued at launch and expired tokens are revoked in the cycle.

`security.capability_enforcement` defaults to `audit`, but that is not a
blanket permission to run denied commands. Explicitly authored capability
grants are enforced in audit mode too; audit grace applies to legacy-adapted
or unresolved capability definitions. Operator sessions are enforced.
`off` disables capability checks; independent scope, resource, wait and
operator-decision gates still apply. See `src/commands/authorization.py` and
`src/commands/resource_authorization.py` for the checks.

### 2.5 Command contracts and discovery

`CONTRACTS = ContractRegistry(autoload=True)` in
`src/commands/contracts/registry.py` lazily loads typed registrations from
`builtin.py` and topic modules. The registry also generates registrations for
admitted handler/plugin descriptors that lack a typed registration; typed
adapters take precedence. An offline descriptor snapshot supports discovery
without a running daemon; a configured live snapshot includes the handler's
actual commands and plugins.

Contracts describe arguments, outcomes, result fields and execution effects.
Their fingerprints are recorded in reviewed playbook bundles; changing a
contract can require rebuilding and reviewing those bundles. Generated
registrations broaden discovery without claiming that every handler has an
individually authored typed adapter. Generated invocation requires an actual
playbook-engine invocation with ready private-value storage; discovery alone
does not authorize direct contract execution.

`src/surface_schema.py` supplies offline enum discovery. The current shared
inventory is `src/commands/command_inventory.py`; surface design history is
[aq-surface](../design/aq-surface.md). API/client generation belongs to
[api-and-clients](api-and-clients.md). Generated references and clients must
be regenerated from their sources, never edited by hand.

### 2.6 The event bus

`src/event_bus.py` is an in-process, non-durable publish/subscribe bus;
`src/event_schemas.py` declares event payload schemas.

- `subscribe(event_type, handler, filter=None)` returns an idempotent
  unsubscribe function. Filters compare top-level payload keys for equality.
- `emit` adds an `event_id` if absent and records the attempted type in
  `seen_event_types`, then validates the payload when validation is enabled.
  Invalid registered payloads raise `EventValidationError` only when
  `env == "dev"`; other environments log and continue. Unknown types are
  observed for doctor checks but do not acquire a schema automatically.
- After validation, `emit` stamps `_event_type`. A non-empty payload dict
  supplied by the caller is mutated. Empty/absent payloads use a new dict.
- Specific subscribers run before wildcard (`"*"`) subscribers, sequentially.
  Async handlers are awaited; synchronous handlers run inline. Subscriber
  exceptions propagate to the emitter and stop the remaining delivery.
- `waiter` subscribes before a caller checks its condition, avoiding the
  subscribe-after-read race. `wait_for` cleans up its subscription and returns
  `None` on timeout. Neither persists the wait across a process restart.

Subscribers include playbooks, gate handling, the dashboard WebSocket and
scheduler wakeups. The playbook runtime schedules detached work rather than
awaiting an entire playbook in the emitter. Durable waits, integration and
message delivery use database records and reconcilers; they do not depend on
retaining bus events. Operational event storage is a separate mechanism.

### 2.7 Database, schema and the migration guard

PostgreSQL is the only backend. The schema is SQLAlchemy Core `Table`s in
`src/database/tables.py`. Queries live under `src/database/queries/`.
`migrations/versions/` holds a squashed baseline plus incremental revisions.
Use read-only `alembic heads` to identify this checkout's head; a hard-coded
revision here would become stale with the next schema change. The how-to is
[`../../guides/migrations.md`](../../guides/migrations.md), and the tables
are documented in [`../../reference/database/`](../../reference/database/README.md).
**Each system spec names the tables it owns.** daemon-core owns the schema
mechanism, not the meaning of individual tables.

`src/database/engine.py` creates the engine (`create_postgres_engine`, with
an `ObservedQueuePool` for pool metrics). `run_schema_setup` asks the migration
guard whether to verify or migrate, then follows the corresponding schema
path, including Alembic preflight where migration is permitted. The adapter's
`initialize()` separately runs startup data normalisations after schema setup.
Verification never grants permission to upgrade production.

`src/database/migration_guard.py` exists because of the 2026-09-02 incident,
in which worker sessions stamped the production database with revisions that
had not been merged. It separates two questions:

- **Who is asking** is the process scope: DAEMON, OPERATOR, WORKER, GLOBAL_SUPERVISOR, CLI or
  TEST. The scope comes from the `AQ_DB_SCOPE` environment variable, which
  overrides `set_process_scope`. Worker sessions are launched with
  `AQ_DB_SCOPE=worker` and a refusal sentinel in place of the database URL.
- **What they point at** is answered by `is_production_database`.

`migration_decision` returns `MIGRATE` for any non-production database.
For production it returns `MIGRATE` only for daemon/operator scope; all other
scopes get `VERIFY`. Verification can raise:

- `SchemaBehindCode`: "schema behind code; ask the operator to upgrade";
- `SchemaAheadCode`, subject to `SchemaAheadPolicy`;
- `MigrationRefused`.

That refusal is the guard working. The optional schema-ahead policy can
accept a newer additive descendant only with retained migration-source proof
and the configured distance policy. It does not stamp, downgrade or upgrade
that database; see [migrations](../../reference/database/migrations.md).
`src/database/additive_migrations.py` is a conservative static check that a
release's revisions are additive. A revision it cannot prove additive (raw
SQL, imported helpers) requires a breaking release. `schema_key.py` gives the test template databases a schema identity
for caching.

### 2.8 Models

`src/models.py` holds shared dataclasses and enums. `TaskStatus` includes
DEFINED, READY, ASSIGNED, IN_PROGRESS, WAITING, HIBERNATED, RESUME_PENDING,
WAITING_INPUT, PAUSED, COMPLETED, FAILED and BLOCKED. The suspended set is
WAITING/HIBERNATED/RESUME_PENDING; the open set excludes terminal
COMPLETED/FAILED/BLOCKED states.

`transition_task` in `src/state_machine.py` validates transitions. Invalid
moves raise when `state_machine.enforce` is enabled or either state is
suspended, subject to explicit guarded overrides. Legacy transitions outside
that set warn by default (`enforce` defaults to false). State meanings and
readiness belong to [task-graph](task-graph.md).

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

`Orchestrator.shutdown()` (`core.py`) runs in this order:

1. Cancel the deploy check and pending claim preparations; stop ready-owner
   recovery.
2. Stop message delivery.
3. Close provider availability.
4. Wait for pool launches, then cancel them.
5. Wait 10 s for running task coroutines.
6. Wait up to 30 s for the branch sweep, then cancel it if necessary.
7. Wait up to 30 s for the layout step, then cancel it if necessary.
8. Stop the spec watcher and the integration service.
9. Cancel the promotion check and bound its cancellation wait to 5 s.
10. Stop the record outbox and the knowledge-generation loop.
11. Unsubscribe delivery callbacks and stop the vault, config and timer services.
12. Shut down the playbook manager.
13. Close the database.

Shutdown does **not** kill agent tmux sessions. `aq restart` relies on this:
the restarted daemon re-adopts them (§2.2, step 4). Only `aq stop` without
`--keep-sessions` kills them.

## 3. Boundaries and interactions

### 3.1 Who owns `src/orchestrator/`

The package is split by responsibility across several systems. Every orchestrator
file has one owning spec. Files the inventory left unassigned are assigned
here.

| Files | Owner |
|---|---|
| `core.py` (class, `initialize`, `run_one_cycle` ordering, `shutdown`), `events.py`, `execution.py` (the launch path), `monitoring.py` (per-cycle checks and notifications), `context.py` (prompt context assembly; its content is owned by sessions-and-harnesses) | **1 daemon-core** |
| `pools.py`, `agent_reconciler.py`, `layout_step.py`, `merge_slot.py`, `route_needed.py`, `stranded_work.py`, `task_checkpoint.py`, `triage.py`, `sync_workflow.py`, `pr_polling.py` | [3 scheduling-and-claims](scheduling-and-claims.md) |
| `worktree_manager.py`, `workspace.py`, `workspace_claim_recovery.py`, `workspace_attachments.py`, `git_ops.py`, `base_workspace.py`, and the previously unassigned `retained_workspace.py`, `slot_restore.py`, `workspace_binding_audit.py` | [6 projects-workspaces-git](projects-workspaces-git.md) |
| `lifecycle.py` (re-checks BLOCKED/PAUSED work that has stopped moving; previously unassigned) | [2 task-graph](task-graph.md) |
| `provider_failover.py` | [12 providers](providers.md) |

`core.py` holds methods for most of those systems. A
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
| 9 | [messaging-and-human-asks](messaging-and-human-asks.md) | The adapter/report outbox are bound in `main.py`; session delivery runs independently, with maintenance and other delivery ticks in the cycle. |
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

These are the architectural rules. The implementation gaps in §5 qualify
claims about complete enforcement today.

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

## 5. Known gaps and limits

- **Command-boundary bypass:** the cycle invokes `_cmd_job_reconcile` directly.
  It therefore skips `execute`'s authorization, result classification and
  command-event boundary. This is an implementation discrepancy to review,
  not a second public write surface.
- **Subscriber failure after a commit:** bus exceptions propagate. An emitter
  may have committed its database mutation before a subscriber fails, so a
  failed response does not necessarily mean nothing changed. Consumers must
  use durable evidence for recovery. The bus also mutates non-empty payloads.
- **Partial timeout coverage:** session-cascade stages are bounded, while an
  unbounded await elsewhere can still delay the cycle. The pool watchdog and
  independent services do not pre-empt synchronous blocking code.
- **Legacy transition grace:** suspended-state moves are enforced, but other
  invalid legacy transitions only warn unless state-machine enforcement is on.
- **Stale source docstrings:** `src/orchestrator/core.py`,
  `src/commands/handler.py` and `src/database/__init__.py` still contain obsolete
  module/runtime or SQLite descriptions. This audit changes documentation
  files only; source-docstring cleanup needs a separate code change.
- **Composition is distributed:** `src/services/` remains empty apart from its
  initializer. `Orchestrator.__init__` and `main.py` compose the actual services.

## 6. Recent changes verified in this snapshot

These commits explain material differences from the 2026-10-20 version of this
page. They are history, not a live task queue or a promise about future work.

| Commit | Implemented behaviour |
|---|---|
| `000433935` | Event-driven scheduler wakeups with minimum spacing and a periodic backstop. |
| `195dd5435` | Shared command descriptors and generated contract registrations. |
| `88ec58400`, `6c6b88df2` | Structured command fallback failures, capability enforcement distinctions and independent resource authorization. |
| `094b61ab4` | Git truth reconciliation through the integration service's maintenance loop. |
| `5c96f5b52` | Verified waits and suspended task states landed in `dev`; the waits loop and mutation fence are current behaviour. |
| `64c3472b9`, `fe8619598` | Integration idle/busy dispatch and unified admission landed; their policy belongs to the integration and scheduling specs. |
| `083a96593` | Recover stale jobs before workspace cleanup and bound session-cascade stages. |

Distributed worker-node designs remain proposals: this checkout still launches
CLI agents through tmux-backed harness/session machinery. Historical design
specs do not establish that a proposal has shipped.

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
