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
| Provider availability (state, evidence, operator override) | `provider_availability` table, mirrored in orchestrator memory; `provider_failover:` in `~/.agent-queue/config.yaml` | Whether anything may launch against a provider. Holds are derived from it on every read and never stored. |
| Provider intent and re-routes | `tasks.provider_intent`, `tasks.rerouted_from`, `task_reroutes` | Whether a task may fail over, and the record (and undo) of every move. |
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
| Push task has no worker | `aq task explain` (an unrouted task names `awaiting_route`, `route_held`, `route_no_candidates` or `router_not_ready`), then project status/constraints, agent capacity, and workspace availability | Fix what the router reports or restore capacity; the push scheduler cannot bypass a paused project, budget, unavailable provider, or locked workspace, and it never launches an unrouted task. |
| Task is READY and `aq task explain` reports `provider_hold` | `aq provider status` and `aq provider held-tasks` | Its provider is out of usage, logged out, failing or disabled. The hold names why the task is not moving; see [provider availability and failover](#provider-availability-and-failover) and the [provider outage runbook](../guides/provider-outage.md). |

`aq task explain --task-id <task-id>` is the task-specific read path: use the reason it returns rather than guessing why the item cannot run. For pool operation and doctor-check meanings, use the [worker-pool guide](../guides/worker-pools.md).

## Provider availability and failover

A worker's **provider** is its harness login — `claude`, `codex` or `gemini`. A provider can run out of usage, lose its login, or start failing every launch, and when it does, capacity on it is gone however many pool slots it has. AQ tracks each provider's availability and scheduling treats an unavailable one as having no capacity at all ([src/providers/availability.py](../../src/providers/availability.py), [src/providers/availability_service.py](../../src/providers/availability_service.py)). What an operator does during an outage is in the [provider outage runbook](../guides/provider-outage.md); every threshold is in the [configuration reference](../reference/configuration.md#provider_failover-keys).

A provider is in one of six states, in two halves. `available` and `degraded` are **launchable**; `exhausted`, `unauthenticated`, `failing` and `disabled` (an operator override) are **unavailable**. No single unexplained failure moves a provider across: a trip needs the provider's own statement (a usage reading, a login probe) or two independent signals. Recovery is a probation: the provider becomes `degraded` with reason `recovering`, admits one launch, and is `available` once that launch succeeds.

**Shipped default.** `provider_failover.mode: enforce` — everything below applies. `observe` tracks, reports and notifies but suppresses nothing and moves nothing on its own; `off` records nothing.

### Launch suppression

Nothing starts against an unavailable provider, and the check costs nothing while every provider is launchable:

* The push scheduler leaves the provider's idle workers out of its supply for the tick ([src/scheduler.py](../../src/scheduler.py), `suppressed_agent_ids`).
* The pool sizer gives every pool profile on the provider bounds of zero, exactly like a disabled pool. Busy sessions keep running — one still making turns is evidence the provider is fine — and an idle pool worker's next `aq task claim` answers `drain_requested` naming the provider's state ([src/orchestrator/pools.py](../../src/orchestrator/pools.py), [src/commands/claim_commands.py](../../src/commands/claim_commands.py)). Admission looks only at the claiming session's provider, so the claim frontier's ordered scan is unchanged.
* A pre-launch check on both paths refuses a launch that raced a state change, and admits one canary at a time while the provider is on probation. A refusal is the provider's, not the task's: it spends no retry and quarantines no pool key.

A `degraded` provider is still launched against; it is only excluded as a failover target.

### Held, not failed

A queued task whose provider is unavailable keeps its status — `READY` stays `READY`. Its **hold** is derived on every read from the task and the provider's state, never stored, so it disappears the moment the provider is back and needs no cleanup. `aq task explain` reports it as a `provider_hold` reason; `aq provider held-tasks [--project-id P]` lists every held task with its hold. The hold's `kind` says why the task is not moving:

| Kind | Meaning |
|---|---|
| `awaiting_failover_capacity` | It will move; `ahead` says how many are queued before it. The trickle below is holding it back, or the next sweep moves it. |
| `provider_pinned` | It is pinned to this provider: an emergency override, or a hold lane such as art design. |
| `no_equivalent_rung` | No other provider has an enabled worker for its class (every `astra-*` class), the router recorded no candidate on another provider, it has no class to match, or its profile is a role profile rather than a worker rung. |
| `no_available_target` | Other providers run the class, or other route candidates exist, but each is unavailable or `degraded`. |
| `class_policy_hold` | `provider_failover.classes` (or `default_policy`) says this class holds. |
| `priority_policy_hold` | Its priority number is above `reroute.max_priority_value`. |
| `reroute_limit_reached` | It was moved automatically `reroute.max_auto_per_task` times, or once within `reroute.task_cooldown_seconds`. A human decides now. |
| `all_providers_unavailable` | Every session provider is unavailable; there is nowhere to go. |
| `failover_inactive` | Re-routing is off: `reroute.enabled` is false or the `provider-failover` playbook is not active. |

### Pin semantics

A task's `profile_id` says both "run this class" and "run on this provider". The column `tasks.provider_intent` records whether the second part binds ([src/providers/intent.py](../../src/providers/intent.py)). Only the project's router and the emergency override write it: no filing surface takes a profile, a pin or an intent, and asking for one is refused with `routing.choice_forbidden` ([agents and routing](agents-and-routing.md#hints-re-routing-and-the-emergency-override)).

| Intent | Meaning during an outage | How a task gets it |
|---|---|---|
| `pinned` | Holds until its provider is back; never moved automatically. | A router route on a hold lane — the shipped art-design lane, which waits for Codex — or `aq task route-override --task-id T --profile-id P --reason "..."`, which only the local operator and a live supervisor session may run. |
| `class_only` | Fails over among the candidates the router recorded. | Every other router route. |
| `preferred` | Fails over to the same class elsewhere. | A role task's stage profile (`route_source = role`), or a route written before mandatory routing (`legacy`). No filing asks for it. |

A pin also binds the push path: a pinned task is given only to a worker on that provider. `aq task route` clears a pin along with the rest of the route and sends the task back to the router; `aq task show` prints the route's source, lane, rule and reason next to its intent.

### The fallback rule

The `provider-failover` playbook calls `provider_reroute` on every state change and every five minutes. Moving work is policy, so it lives in that playbook: if it is not active, tasks hold with `failover_inactive` and nothing moves ([src/providers/reroute.py](../../src/providers/reroute.py), [src/prompts/default_playbooks/provider-failover.md](../../src/prompts/default_playbooks/provider-failover.md)). For each eligible queued task — `READY` and unblocked, or paused by a provider failure — on an unavailable provider, in claim order (`priority, created_at`):

1. A pinned task holds.
2. A class whose policy is `hold` holds; the default policy is `same_class`.
3. Otherwise a task the router routed moves to **the next candidate in `tasks.route.candidates`** — the list the router recorded, in its order — whose profile still exists and whose provider is available. A move never leaves the candidates, so it never breaks a lane: an integration repair never moves to OpenCode, and a code-design task moves only between its Claude and Codex rungs. A task with no recorded candidates (a legacy route still queued) moves to **the same intelligence class on the first available provider** in `provider_failover.order` — when that list is empty, `claude`, then `codex`, then any other provider. Either way it never moves automatically to a provider it has already left.
4. If no such target exists, it holds.

The class never changes on an automatic move, except to a candidate's own class (only the narrow OpenCode lanes map one), and no move changes a task's intent or its `route_source`. A class that only one provider runs holds by construction: every `astra-*` class is OpenAI-only, so an Astra task waits for Codex whatever its intent — the class protects it, not a pin. Each move is recorded on the task (a comment with the undo command, `tasks.rerouted_from`, a `task_reroutes` row), and every automatic move of one outage shares a batch id.

Unrouted work fails over without being moved. When the router plans a route it drops candidates whose provider cannot launch; when every candidate is down the plan answers `held`, the task waits unrouted without failing a run, and `aq task explain` reports `route_held` naming the providers. There is no project default to resolve: a task without a route waits for its router.

### Capacity protection

Failover raises no ceiling. A moved task is an ordinary queued task on its new rung, claimed under that rung's `max_active`, `swarm.global_max_active`, `project.max_concurrent_agents` and workspace capacity like native work, and it keeps its priority and creation time — neither boosted nor demoted. The sweep changes no bound, no `enabled` flag and no agent row, and a disabled pool is never a target.

* **Only an `available` provider is a target.** A `degraded` one — for example Claude past `usage.degraded_percent` of its window — receives no failover traffic, so failover stops before it can push a second provider over the edge (`reroute.allow_degraded_target`, default false).
* **Moves trickle.** Per target rung the sweep keeps at most `max(1, ceil(reroute.target_backlog_factor × capacity))` moved-and-not-yet-started tasks queued, where capacity is the rung's `max_active` for a pool profile or its enabled workers otherwise. The rest hold `awaiting_failover_capacity`. A short outage moves one pool-width of work, and the blast radius of a false alarm is bounded by the same number.
* **Per-sweep and per-task limits.** At most `reroute.max_per_sweep` (10) moves per sweep; a task moved automatically is not moved again within `reroute.task_cooldown_seconds` (30 min) and holds for a human after `reroute.max_auto_per_task` (2) moves, so two providers failing in turn cannot ping-pong it. `reroute.max_priority_value`, unset by default, limits automatic moves to urgent work.

### Return path

When a provider leaves the unavailable half, **new and held work returns at once**: holds vanish because they were derived, pools size back up, and the router offers that provider's candidates again, so a task it held is routed on its next `task.route_needed`. The probation canary paces the first launches.

**Moved work stays moved.** A task already re-routed and still queued has a position on a healthy provider; moving it back buys nothing and is half of a ping-pong. The trickle means there is at most one pool-width of it per rung, and `rerouted_from` stays on the task as the record. Running tasks finish where they are. An operator can send work back with `aq provider reroute-undo --batch-id <id>` or `--task-id <id>`; undo is refused for a running or claimed task, while the original provider is still unavailable unless `--force`, and for a routed task whose original profile is not among its route candidates. A manual `aq provider reroute` is limited to the candidates too, even with `--force`: a move outside them is an emergency override (`aq task route-override`), not a reroute.

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
* [A provider ran out of usage](../guides/provider-outage.md) — the operator runbook for a provider outage: reading its state, forcing or undoing moves, recovery.

## Source and tests

The scheduling decisions are implemented by [src/scheduler.py](../../src/scheduler.py) and the orchestration cycle/pool reconciler in [src/orchestrator/core.py](../../src/orchestrator/core.py) and [src/orchestrator/pools.py](../../src/orchestrator/pools.py). Provider failover's scheduling half is [src/providers/availability_service.py](../../src/providers/availability_service.py) (suppression and holds) and [src/providers/reroute.py](../../src/providers/reroute.py) (the sweep). Claim status semantics are in [src/pool_claims.py](../../src/pool_claims.py). Focused coverage is:

```bash
aq test tests/test_pool_sizing.py tests/test_pool_placement.py \
  tests/test_pool_reconciler.py tests/test_pool_lifecycle_integration.py \
  tests/test_schedule.py tests/test_timer_service.py tests/test_resource_limits.py \
  tests/test_resource_semaphore.py tests/test_cli_test_runner.py
aq test tests/test_provider_suppression.py tests/test_provider_intent.py \
  tests/test_provider_reroute.py
```
