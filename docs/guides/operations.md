# Operating and recovering Agent Queue

This guide shows an operator how to tell a normal queue delay from a fault, collect evidence, and recover safely. Start with the smallest read-only check that can answer the question; use an automated repair only after its finding identifies an owned, safe repair.

## Why it exists

Agent Queue (AQ) has several independent kinds of health. A task can be ready while its project has no compatible worker, a pool can be healthy while one workspace is blocked, and a healthy daemon can be disconnected from a dashboard browser. Treating every warning as a daemon outage risks interrupting work that is actually progressing.

This page is for the local operator who runs the daemon and has authority to inspect or repair its project state. A pool worker has a narrower session-scoped token: it should report evidence through its assigned task rather than try operator commands.

## Vocabulary

* A **check** is one named, independently timed diagnostic reported by `aq doctor`; its result has an `ok`, `info`, `warn`, or `error` severity.
* A **pool** is the global fleet of durable workers for one `lifecycle: pool` profile. It is not a per-project process group; its status includes a per-project breakdown.
* A **claim** is a time-bounded task ownership record. Its epoch fences an old worker from writing after ownership changes.
* An **integration train** is the guarded process that delivers task branches to a project target. It is separate from a task reaching a terminal status.

For task states and the meaning of `READY`, see the [task state machine](task-state-machine.md). For pool sizing and worker lifecycle, see [worker pools](worker-pools.md).

## A realistic first investigation

Assume `demo.17` is `READY` but no work begins. Do not restart the daemon first: a dependency, profile mismatch, disabled pool, workspace shortage, or integration fence can all produce an idle-looking task with different remedies.

```bash
aq task explain demo.17
aq pool status
aq doctor --check pools.disabled --check pools.placement_starved \
  --check pools.preparing_stuck
```

The current CLI exposes focused doctor checks (this help output was produced locally):

```text
Usage: aq doctor [OPTIONS]

  Check whether this install is healthy, and what to do about it.

Options:
  --fix           Apply fixes for failing fixable checks, then re-run.
  --check TEXT    Run only this check id (repeatable, e.g. --check db.migrations).
```

Interpret the three commands together:

* `aq task explain` names the actual readiness blocker for that task. A dependency or gate is task-local, not a pool incident.
* `aq pool status` shows global supply, demand, bounds, and its nested project placement data. A `READY` count alone does not prove that the pool should launch another worker.
* The focused checks distinguish a disabled pull-pool (`pools.disabled`), an unplaceable start (`pools.placement_starved`), and a claim/prepare operation that exceeded its timeout (`pools.preparing_stuck`).

Expected outcome: either the explanation identifies a task-level blocker, or the pool/doctor output narrows the problem to one component. Preserve the command output in the incident or task note before changing anything. A warning is a prompt to inspect its detail and data; it is not automatically permission to repair.

## Inputs and outputs

| Surface | Input | Output | Best use |
|---|---|---|---|
| `aq task explain TASK_ID` | one task id | readiness blockers, dependencies and gates | A task is not starting. |
| `aq status` | none | system overview | First high-level orientation. |
| `aq pool status` | optional project view | fleet-wide supply/demand/bounds plus project placement | Workers are idle, missing, or cannot start. |
| `aq doctor [--check ID] [--fix]` | zero or more check ids | per-check severity, detail, duration and summary | Evidence-first diagnosis and narrowly advertised repair. |
| `aq integration status PROJECT_ID` | project id | rollout, readiness, active work and cleanup | A delivered branch is not reaching its target. |
| `GET /health`, `GET /ready` | HTTP probe | `healthy`/`degraded`, or readiness checks | Load balancers and dashboard connectivity diagnosis. |
| `GET /api/metrics/series` and Metrics dashboard | range and resolution | stored history plus live `metrics.tick` frames | Trend analysis; not a task-control command. |

`aq doctor` exits `0` for only `ok`/`info`, `1` when the worst result is `warn`, `2` when any result is `error`, and `3` when the doctor command itself cannot run. This makes a focused check usable in an operator script, but it does not turn every non-zero exit into an automatic restart request.

## State ownership

| State | Owner and location | What an operator may infer |
|---|---|---|
| Task status, dependencies, claims, sessions and integration ownership | AQ database | The database is authoritative; dashboard labels and command output are views. |
| Pool limits and whether pool scheduling is enabled | configured local policy in the `swarm:` configuration and global profile definitions | A local installation can deliberately disable pools or cap them below demand. |
| Metrics history | `metrics_samples` database rows | One-second rows are buffered briefly; history can lose only the newest buffered seconds on a hard stop. |
| Live metrics | the daemon event bus (`metrics.tick`) | A browser must reconnect after a daemon or WebSocket interruption; a live gap does not rewrite history. |
| Logs | configured console and optional rotating JSONL file | Correlation fields such as task, project, session/agent, command and component connect an event to its owner. |
| Worktrees and branches | project repository plus AQ workspace/ownership records | A checkout or remote ref can outlive a task; do not delete it merely because a dashboard row changed. |

**Shipped defaults.** Fleet metrics are enabled by default and collect at a one-second cadence. AQ retains one-second detail for one hour, minute roll-ups for 30 days, and hour roll-ups for one year. The console logging default is `dev`; JSON and plain modes are available. These are defaults, not promises about a locally edited configuration.

**Configured local policy.** Pool enablement, pool bounds, logging level/file retention, metrics retention, integration mode, project concurrency and provider access come from the daemon's configuration and project/profile records. Inspect the active configuration before concluding that an expected worker or metric is absent.

**Optional compatibility.** `logging.format: text` is accepted as a compatibility alias for `dev`. A metrics sampler can be disabled locally; then a missing Metrics stream is expected rather than a dashboard fault.

## Health layers: diagnose the right thing

```mermaid
flowchart TD
  S[Symptom] --> T{One task?}
  T -->|yes| TE[aq task explain]
  T -->|no| D{Daemon reachable?}
  D -->|no| H[/health and daemon logs]
  D -->|yes| P{Worker supply or placement?}
  P -->|yes| PS[aq pool status + focused doctor]
  P -->|no| I{Delivery to target?}
  I -->|yes| IS[aq integration status]
  I -->|no| M[Metrics/dashboard connection]
```

### Task health

Use `aq task show` for the state and `aq task explain` for why it cannot run. `READY` means it is eligible in the task state machine, not that a compatible worker, workspace, provider credential, or integration branch is already available. A task blocked by a dependency, approval, human input, or its declared workspace requirement is not repaired by increasing pool size.

### Project health

Use `aq status` for the broad overview, then `aq integration status PROJECT_ID` when the question is about delivery. Integration status is the authority for rollout/readiness/cleanup; a terminal task is not by itself proof that its branch has reached the target ref.

### Pool health

Use `aq pool status` for fleet-wide profile bounds and its project placements, then doctor checks for an exception. A globally healthy profile can still have a quarantined, capacity-limited, or workspace-starved project. Conversely, a task profile with no compatible installed/available worker is a routing or profile problem, not a generic scheduler failure.

### Daemon health

Use `/health` for all registered health-provider checks and `/ready` for the database, messaging, and required-playbook readiness subset. `/health` returns HTTP 200 only when all checks report okay; `/ready` returns HTTP 200 only when its required dependencies are ready. `503` is evidence to investigate the named check, not a diagnosis by itself.

## Symptom-to-command troubleshooting

| Symptom | Read-only evidence first | Likely distinction | Safe next step and verification |
|---|---|---|---|
| A `READY` task is idle | `aq task explain TASK`; `aq pool status`; `aq doctor --check pools.disabled --check pools.placement_starved` | dependency/gate vs disabled pool vs no place to launch | Correct the named task or local pool policy. Re-run `task explain` and `pool status`; do not restart merely to clear `READY`. |
| No compatible agent appears | `aq task explain TASK`; inspect the task profile and `aq pool status` | routing/profile availability vs a running pool shortage | Use the [agents and routing concept](../concepts/agents-and-routing.md) to correct the profile/provider policy. Confirm the task's actual compatibility reason before changing bounds. |
| A worker never finishes preparation | `aq doctor --check pools.preparing_stuck`; inspect the listed sessions and workspace capacity | a recent launch is normal; only `claiming`/`preparing` beyond twice `prepare_timeout` is stale | Record session ids. `--fix` releases a timed-out claim back to `READY` with a prepare backoff, or clears a taskless phase. Re-run the same check and `aq task explain`. |
| A stale owner holds work | `aq doctor --check pools.stuck --check pools.orphan_agents --check pools.stale_worktree_checkouts` | stale claim/session, leaked lock, or an operator-owned busy orphan | `--fix` can release invalid pool claims, release leaked locks, and retire only unusable unowned agents; it explicitly leaves busy/task-holding orphan agents alone. Verify the check and preserve the listed identifiers. |
| A worker received a nudge but does nothing | `aq doctor --check sessions.stuck_composer` | a confirmed daemon marker is still in a harness composer | `--fix` only resubmits a matching daemon-injected marker, never a human draft. Re-run the check; inspect the task before any broader restart. |
| Work is completed but integration is stuck | `aq integration status PROJECT`; `aq doctor --check integration.operational --check integration.stranded_fences` | normal queued cleanup, a parked repair, or a writer-less branch fence | Preserve branch/ref, task and operation ids. `integration.stranded_fences` is deliberately report-only because recovery needs proof about the stopped writer, clean checkout and published work. Follow the guarded integration recovery path; do not delete refs. |
| A branch discard or cleanup is parked | `aq doctor --check integration.branch_discards`; `aq integration status PROJECT` | remote deletion failure vs active ownership | The check's `--fix` only re-arms its durable pending discard; the next drain re-derives head and ownership. Verify status; manually delete a remote ref only when the evidence says it is no longer owned. |
| Dashboard says disconnected or charts stop | browser network/WebSocket diagnostics; `/health`; `/ready`; inspect the Metrics tab and `GET /api/metrics/series` | browser/WebSocket break, disabled sampler, daemon readiness failure, or absent retained history | Reconnect the browser after checking endpoint status. If history exists but live updates do not, investigate the connection. If the sampler is disabled by configuration, enable it through the approved configuration workflow and verify a new `metrics.tick`; do not invent missing historical points. |
| Metrics have a gap after restart | Metrics history and daemon uptime/restart series | newest buffered seconds lost vs broader database failure | A short newest-history gap is an intentional batching tradeoff. Check `/health`, database diagnostics and subsequent samples; preserve the range when escalating. |

## Evidence-first recovery runbooks

### 1. Restore work dispatch without losing a task

1. Capture `aq task explain TASK_ID`, `aq pool status`, and the relevant focused doctor check.
2. If the check says `pools.disabled`, decide whether the local `swarm.enabled` policy is intentional. Enabling a pool is an operator policy decision, not a doctor fix.
3. If `pools.preparing_stuck` lists a session beyond its threshold, run only:

   ```bash
   aq doctor --check pools.preparing_stuck --fix
   ```

4. Verify with the same check and `aq task explain TASK_ID`.

The repair returns task-backed stale preparations to `READY` and records a prepare backoff; it does not delete the task. If it repeats, preserve the session ids, profile, workspace information, and logs for a routing/workspace investigation instead of looping repairs.

### 2. Reconcile stale pool ownership safely

1. Run `aq doctor --check pools.stuck --check pools.orphan_agents --check pools.stale_worktree_checkouts` without `--fix`.
2. Classify every row. A worktree checkout on a non-live branch is different from a workspace lock; a busy orphan is intentionally not auto-retired.
3. Run the smallest applicable fix, for example:

   ```bash
   aq doctor --check pools.stuck --fix
   ```

4. Re-run the same check and inspect task/workspace status.

`pools.stuck` releases a pool claim only when its task is no longer `IN_PROGRESS` or `ASSIGNED`. Orphan-agent repair audits durable `pool.agent_repaired` events and leaves busy or task-holding records for a human. For stale worktree checkouts, the automated action detaches the checkout; it does not delete a directory or branch. Preserve branch names until task and integration ownership agree.

### 3. Recover a stalled integration without rewriting delivery history

1. Read `aq integration status PROJECT_ID` and record the project, operation/batch ids, task ids, target ref and heads it reports.
2. Run `aq doctor --check integration.operational --check integration.stranded_fences --check integration.branch_discards`.
3. For a safe human-required operation, use `aq integration resume OPERATION_ID`; use `aq integration retry-cleanup BATCH_ID` only for the exact safe cleanup reported by status.
4. If an already-delivered task must be recorded without replaying old repair checkpoints, use `aq integration adopt` with the observed target ref/head, every affected task, an explicit reason, and `--accept-equivalent` only when accepting operator-edited or evidence-only delivery is intentional.
5. Re-run `aq integration status PROJECT_ID`; verify the target ref independently in the repository before retiring evidence.

> **Warning.** `integration.stranded_fences` has no doctor fix. It names a branch that looks held by a writer that disappeared, but only the guarded recovery workflow can establish that the checkout is clean and work is published. Keep refs and workspaces intact until that proof exists.

### 4. Restore visibility, not just the page

1. From the same network path as the dashboard, request `/health` and `/ready`; save HTTP status and response body.
2. If readiness is degraded, recover the named database, messaging, or required-playbook dependency first. A browser reconnect cannot cure a 503 readiness failure.
3. If endpoints are healthy, reconnect the dashboard and inspect a historical Metrics range. History comes from `/api/metrics/series`; live points arrive on `metrics.tick` and must resume after a WebSocket reconnect.
4. If no metrics arrive, inspect the configured `metrics.enabled` policy and daemon logs. After a configuration correction/reload, verify a new live point and a persisted historical point.

The rollback is straightforward: restore the previous configuration through the supported configuration workflow. Do not backfill invented samples; the sampler exposes daemon uptime specifically so restart-reset event rates remain visible.

## Metrics reference

The [Metrics dashboard API](../../src/api/metrics.py) uses history for its initial range and live `metrics.tick` events for ongoing display. It asks for `1s`, `1m`, or `1h` data; `auto` selects the finest tier that fits the response point limit and reports when a requested step was coarsened.

| Group | Measurements | Interpretation caveat |
|---|---|---|
| `agents` | total running sessions; state, harness, profile and lifecycle breakdowns | Counts describe current sessions, not task completion. |
| `tasks` | `READY`, `IN_PROGRESS`, `ASSIGNED`, `PAUSED`, `BLOCKED`, `WAITING_INPUT`, and `other` | A rise in `READY` needs task/pool evidence before it means a fault. |
| `machine` | 1/5/15-minute load, CPU count and available/free/total memory where the platform supplies them | Unsupported measurements are `null`, not zero. |
| `daemon` | uptime and restart count | Bus-only rates reset at restart; read them with uptime. |
| `stall` and merge rate | nudges/hour, killed sessions/hour, merges/hour | These are in-memory rolling windows and restart at zero by design. |
| throughput and slow tier | completion/PR throughput, token, subagent, delegation and worktree-slot data | Expensive data is refreshed on the slower configured interval and carried between samples. |
| `sampler` | collection duration | Lets an operator see whether monitoring itself is expensive. |

One-second samples are published before they are buffered to the database. The sampler batches disk flushes (default five seconds), then rolls completed seconds into minute rows and minutes into hour rows, and prunes each tier by its own retention. A brief gap in the newest durable history after an abrupt stop can therefore be expected; a live dashboard should not wait for that flush.

## Logs and correlation

AQ routes standard-library logging through structured logging. Console formats are `dev`, `json`, and `plain`; an optional rotating JSONL file is always JSON regardless of console presentation. Configure retention (`log_file_max_bytes` and `log_file_backup_count`) deliberately, especially when retaining task- or provider-rich logs.

When collecting an incident, filter or preserve correlation fields where present: `task_id`, `project_id`, `cycle_id`, `component`, `hook_id`, `agent_id`, and `command`. They are bound through async work and make it possible to join a doctor finding, a task, a session and a daemon event without guessing from timestamps.

## What doctor fixes — and what it will not do

`aq doctor --fix` selects only checks that declared a fix and returned `warn` or `error`, then reruns them. It is not a general "repair the system" button. Prefer `--check ID --fix` during an incident so the intended mutation is obvious.

Automated examples include releasing invalid stale pool claims, returning timed-out prepare claims to the frontier with backoff, detaching stale slot checkouts, re-arming parked branch discards, refreshing missing/stale shipped harness copies while leaving edited copies alone, and resubmitting a daemon-marked stuck composer. Some checks are report-only by design: a stranded integration fence, a busy orphan agent, an operator-edited configuration/profile problem, or a deletion that would require choosing a branch/ref owner needs human evidence and authority.

Before and after every repair, save the finding and rerun its exact check. If the same failure recurs, stop repeating repair and escalate with the ids, branch/ref, task state, configuration generation and correlation-rich logs.

## Related pages

* [Worker pools](worker-pools.md) explains the pull-worker lifecycle, global bounds and placement data used in pool incidents.
* [Resource gating](resource-gating.md) distinguishes intentional test/CPU limits from a failing scheduler.
* [Development integration](development-integration.md) explains the normal delivery path that integration recovery protects.
* [Task state machine](task-state-machine.md) defines why a task is `READY`, blocked or waiting for input.
* [CLI reference](../reference/cli/README.md) is the authoritative flag-by-flag command reference.

## Source and tests

The check registry and its timeout/exit-code contract live in [src/doctor/__init__.py](../../src/doctor/__init__.py), [src/doctor/runner.py](../../src/doctor/runner.py), and [src/doctor/models.py](../../src/doctor/models.py). Pool and integration recovery checks are in [src/doctor/pool_checks.py](../../src/doctor/pool_checks.py) and [src/doctor/integration_checks.py](../../src/doctor/integration_checks.py). Metrics and logging are implemented by [src/metrics/sampler.py](../../src/metrics/sampler.py) and [src/logging_config.py](../../src/logging_config.py).

Focused implementation tests (for changes to those modules) are:

```bash
aq test tests/test_doctor.py tests/test_pool_doctor.py tests/test_metrics_sampler.py \
  tests/test_logging_config.py tests/test_api_metrics.py tests/test_health_platform.py
```

This documentation-only change is checked with the documentation inventory and link/whitespace checks described below; it does not change runtime behavior.
