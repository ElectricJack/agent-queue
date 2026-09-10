---
tags: [concept, scheduling, pools, resources]
---

# Scheduling, worker pools and resource limits

AQ turns work that is ready into bounded, fair agent capacity: it chooses work without using an LLM, starts either task workers or pull-based pool workers, and prevents one busy machine from being overwhelmed.

## Why it exists

A task becoming `READY` means its graph and policy blockers have cleared; it does **not** promise that a process is already running. AQ must still respect a project's workspace and concurrency limits, the machine-wide worker budget, and the selected profile's lifecycle. This separation lets one daemon share a box fairly across projects without starting an unbounded number of agent or test processes.

## Vocabulary

* A [READY task](../guides/task-state-machine.md) is eligible for scheduling, subject to capacity and routing.
* A **push worker** is a `lifecycle: task` session that AQ assigns to one task. A **pool worker** is a long-lived `lifecycle: pool` session that asks for work with `aq task claim`.
* **Desired capacity** is how many pool sessions the sizing calculation wants; **running capacity** is the observed idle, busy, starting, and draining sessions. Desired capacity is a target, not a claim that processes have started.
* A **fleet** is all pool workers for one profile across the daemon. A **project** is one repository/work queue inside that fleet.
* A **worktree slot** is an isolated checkout available to one worker. Workspace selection and slot recovery are owned by the `workspaces` documentation shard; until its concept page lands, see [project onboarding](../guides/project-onboarding.md).

## A realistic example

Assume the daemon is running, projects `website` and `api` both use the same pool profile, and the operator has enabled pools. The operator can inspect the current, fleet-wide view without changing it:

```bash
aq pool status --help
```

```text
Usage: aq pool status [OPTIONS]

  Supply/demand/bounds snapshot for every worker pool (one row per profile,
  fleet-wide, with the per-project placement detail nested in `projects`).
```

Suppose that profile has `min_active: 1`, `max_active: 3`, one busy session, and two READY tasks total. The pool sizer wants `busy + ready = 3` sessions (within those bounds). It authorizes starts only up to `swarm.max_starts_per_tick` and the fleet's global cap. The placement step then puts each authorized start in the project with an eligible workspace and the greatest unserved need; it does not give each project its own maximum of three.

When a pool worker starts in `website`, AQ mints its project-scoped credentials and gives it a workspace there. The worker loops on `aq task claim --next --wait 60`; a successful claim changes the task from READY to in progress under an epoch fence. If no admissible task is available, it waits or backs off rather than taking work from another project.

```mermaid
flowchart LR
    R[READY tasks] --> M[Measure demand and live supply]
    M --> S[Size each profile fleet-wide]
    S --> P[Place starts or drains per project]
    P --> W[Pool worker starts in one project]
    W --> C[aq task claim]
    C --> I[In-progress claimed task]
```

## Inputs and outputs

The deterministic [push scheduler](../../src/scheduler.py) receives a snapshot of active projects, READY tasks, idle agents, routing, budgets, project constraints, and workspace availability. It returns `AssignAction` values; the [orchestrator](../../src/orchestrator/core.py) performs the database and session work afterwards.

The same module's pool functions take measured per-profile demand and session supply, profile `min_active`/`max_active` bounds, a global cap, and per-project placement candidates. `size_pools()` returns start/drain actions; `place_pool_actions()` turns those into project-specific starts or idle drains. Project candidates are rejected when they are quarantined, lack a workspace, or have reached `project.max_concurrent_agents`.

Timer and schedule inputs are different from worker capacity. [src/timer_service.py](../../src/timer_service.py) emits system-scoped `timer.Nm`, `timer.Nh`, and `cron.HH:MM` playbook events. [src/schedule.py](../../src/schedule.py) matches the older structured/cron schedule objects used by periodic hooks. Neither starts a worker by itself.

## State ownership

| State | Owner and location | What it controls |
|---|---|---|
| Project status, READY tasks, sessions, claims, constraints and workspace locks | PostgreSQL, written through the daemon | Whether work is eligible and what is actually running. |
| Profile lifecycle and pool bounds | Global profile markdown in `~/.agent-queue/vault/agent-types/` | Push versus pull lifecycle; `min_active`, `max_active`, and optional `min_per_project`. |
| `swarm` and `resources` policy | `~/.agent-queue/config.yaml` | Pool enablement, tick start/drain budgets, claim/preparation timeouts, fleet cap, and machine resource policy. |
| Pool surplus, launch quarantine, and placement-starvation observations | Orchestrator process memory | Short-lived backoff and grace-period decisions; they reset safely when the daemon restarts. |
| Test-slot locks | `${data_dir}/locks/test-slots/slot-N.lock` | A crash-safe machine-wide limit on concurrent `aq test` runs. |

**Shipped defaults.** `swarm.enabled` defaults to false, so ordinary `lifecycle: task` profiles keep using push scheduling. When pools are enabled, `swarm.global_max_active` inherits `resources.max_concurrent_agents` unless explicitly set; resource gating defaults to a bounded per-session share and two global test slots. See the [configuration reference](../specs/config.md#411-swarm-section) for current defaults.

**Configured local policy.** The actual fleet cap, profile bounds, project caps, session availability, and workspace inventory are installation-specific. Inspect the configuration and `aq pool status`; do not infer them from an example.

**Optional compatibility.** An operator can pin a harness environment value such as `PYTEST_XDIST_AUTO_NUM_WORKERS`; that explicit harness value wins over AQ's derived per-session cap. Optional cgroup v2 scopes add kernel-enforced CPU and memory limits when user-scope delegation is available.

## Why READY can have no worker

Use the symptom, not the task state alone, to choose recovery:

| Symptom | Diagnose | Recovery |
|---|---|---|
| Profile is `lifecycle: pool`, but pools are disabled | `aq doctor --check pools.disabled` | Enable `swarm.enabled` or change the profile back to `lifecycle: task`; do not expect push scheduling to rescue a pool profile. |
| Pool has demand but no start is placed | `aq pool status` and `aq doctor --check pools.placement_starved` | Free or add a workspace, raise the relevant project/fleet limit, or resolve the reported quarantine. |
| A launch failed repeatedly | `aq pool status` shows the quarantined project/profile and reason | Fix the harness, project checkout, or workspace cause; the short launch backoff prevents a failed launch every tick. |
| A pool session is present but cannot claim | `aq task claim --next --wait 60` reports `not_admissible` or waits | Check swarm enablement, the profile route, task eligibility, and the command's stated backoff/retry result. |
| Push task has no worker | Inspect routing, project status/constraints, agent capacity, and workspace availability | Restore the missing route or capacity; the push scheduler cannot bypass a paused project, budget, provider cooldown, or locked workspace. |

`aq task explain <task-id>` is the task-specific read path: use the reason it returns rather than guessing why the item cannot run. For pool operation and doctor-check meanings, use the [worker-pool guide](../guides/worker-pools.md).

## Resource limits and throughput

Resource gating protects a shared machine in three layers:

1. At launch, [src/resources/limits.py](../../src/resources/limits.py) derives a per-session CPU share, xdist worker cap, thread caps, and lower process priority. This is cooperative: a process can ignore environment variables.
2. `aq test` takes a global `flock` slot before pytest, implemented by [src/resources/semaphore.py](../../src/resources/semaphore.py). A full slot does not mean a failed test: it means wait, inspect `aq test --aq-status`, or retry exit code 75 later.
3. Optional cgroup v2 scopes enforce CPU and memory at the kernel level when systemd delegation works. Missing delegation falls back to the first two layers rather than blocking a worker launch.

These controls bound contention; they are not a throughput benchmark or a promise about tasks completed per day. Run focused suites with `aq test`, never raise its worker count above the session cap, and reserve wall-clock performance tests for an intentionally quiet machine. The [resource-gating guide](../guides/resource-gating.md) has the operational configuration and recovery steps.

## Related pages

* [Worker pools](../guides/worker-pools.md) — enable, size, inspect, and roll back a pull-based fleet.
* [Resource gating](../guides/resource-gating.md) — tune test slots and optional cgroups safely.
* [Agents and routing](agents-and-routing.md) — how a task obtains its profile and execution class before capacity is considered.
* [Project onboarding](../guides/project-onboarding.md) — the current entry point for repository and workspace setup; the workspaces concept page is being written by its own shard.
* [Task state machine](../guides/task-state-machine.md) — what READY means before scheduling begins.

## Source and tests

The scheduling decisions are implemented by [src/scheduler.py](../../src/scheduler.py) and the orchestration cycle/pool reconciler in [src/orchestrator/core.py](../../src/orchestrator/core.py) and [src/orchestrator/pools.py](../../src/orchestrator/pools.py). Claim status semantics are in [src/pool_claims.py](../../src/pool_claims.py). Focused coverage is:

```bash
aq test tests/test_pool_sizing.py tests/test_pool_placement.py \
  tests/test_pool_reconciler.py tests/test_pool_lifecycle_integration.py \
  tests/test_schedule.py tests/test_timer_service.py tests/test_resource_limits.py \
  tests/test_resource_semaphore.py tests/test_cli_test_runner.py
```
