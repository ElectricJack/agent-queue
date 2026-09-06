# Assignment routing as an authored playbook

**Status:** in progress · **Date:** 2026-09-06 · **Supersedes:** the coordinator half of
`2026-08-31-playbook-intelligence-routing-design.md`

## 1. Why

The orchestration layer should be mechanism, not policy. Routing today is the
opposite: `kind: assignment-routing` is not an authored graph at all. The lowering
step throws the prose away and emits one fixed LLM node, and every decision about
*when* a task is routed, *what* it may be routed to, and *what gets written* lives
in `src/orchestrator/assignment_routing.py` (~800 lines: candidate selection,
batching, option catalog, input and catalog hashes, freshness checks, gate
resolution, re-stamping, retries, and the profile backfill added on 2026-09-06).

The concrete failure that exposed it: a task with an explicit `intelligence_class`
of `deep-low` sat READY for hours beside four idle `standard-medium` pool workers,
because no policy anywhere turned a class into a claimable profile. The right
place for that policy is the playbook, and the playbook could not express it.

## 2. Principle

Every policy is a **read command**, a **write command**, and a **playbook between
them**. The orchestrator owns the cycle, the transactions, the locks, and the
delivery of events. Commands are the only boundary a playbook touches.

For routing:

| Layer | Owns |
|---|---|
| Orchestrator (mechanism) | Notice a task that lacks the fields it needs to be picked up and emit `task.route_needed`. Nothing else. |
| `task_route_options` (read command) | Report the task, whether its class is explicit, and the compatible class/provider/profile catalog. |
| `default-assignment-routing` (policy) | Decide the class (LLM, or explicit) and the profile that serves it. |
| `task_route` (write command) | Write class + profile onto the task, record the reason, resolve the routing gate. |
| Scheduler / pool claim (mechanism) | Match a task's **own** class and profile to a worker. No route table, no hashes. |

The task row is the route. A task is *routed* when `intelligence_class` and
`profile_id` are both set. There is no separate decision record to keep fresh.

## 3. Event: `task.route_needed`

Emitted by the cascade (step 3a, where `assignment_routing.reconcile()` ran) for
every task that is unassigned, not a plan subtask, in READY / BLOCKED / DEFINED
(DEFINED only when unblocked or when its sole blocker is an open `routing` gate),
and missing `intelligence_class` or `profile_id`. Re-emitted for the same task at
most every 120 s so a playbook run has time to land. Payload: `task_id`,
`project_id`, `title`, `description`, `priority`, `task_type`,
`intelligence_class`, `profile_id`.

This is the only orchestrator-side routing code that remains.

## 4. Commands

### `task_route_options` (READ)

Args: `task_id`. Outcomes:

- `already_routed` — class and profile are set and compatible. Nothing to do.
- `explicit` — class is set; `explicit_profile_id` names the profile that serves it.
- `undecided` — no class; `options` is non-empty and the playbook must decide.
- `no_options` — no profile in the project can execute any class (or the
  explicit class). Terminal failure the run overlay shows.
- `rejected` — unknown task.

Value: `task_id`, `project_id`, `title`, `description`, `priority`, `task_type`,
`intelligence_class`, `profile_id`, `default_profile_id`, `explicit_profile_id`,
`options: [{intelligence_class, provider, profile_id, lifecycle, configured_capacity,
idle_count, busy_count}]`.

The option catalog is the former `build_assignment_options`, now one row per
(class, provider, **profile**) so the playbook can name the profile it wants.
`explicit_profile_id` is the deterministic tie-break for a class the operator
already chose: the task's pinned profile if it serves the class, else the pool
or task profile whose `default_class` is that class, preferring the project
default profile's provider, then the lowest id.

### `task_route` (UPDATE, existing)

Gains an optional `reason` (stored as task metadata `route_reason`, shown by
`aq task explain`). Already writes class + profile under the "no worker holds
the task" predicate and resolves open `routing` gates.

## 5. The playbook

`default-assignment-routing.md` becomes `kind: pipeline`, system scope, one rule
`route-task` on `task.route_needed`:

1. `task_route_options` → bind `routing`. `already_routed` ends the rule;
   `explicit` goes to step 3; `undecided` goes to step 2; `no_options`,
   `rejected`, `runtime_error` fail the rule.
2. LLM step (profile `playbook-compiler`, class `fast-low`): choose
   `intelligence_class`, `provider`, `profile_id` **from the supplied options**,
   with a `reason`. Bind `decision`. Continue to step 4.
3. `task_route` with `routing.explicit_profile_id`, `routing.intelligence_class`,
   reason "explicit intelligence class". `routed` ends the rule.
4. `task_route` with `decision.profile_id`, `decision.intelligence_class`,
   `decision.reason`. `routed` ends the rule; `rejected` fails it.

Anything an operator wants different — a project that always routes to codex, a
task type that is always deep — is a project-scope copy of this file. No code.

## 6. What is removed

- `src/orchestrator/assignment_routing.py` (coordinator) and `lower_assignment`.
- `task_assignment_routes` reads and writes. The table stays until a follow-up
  migration drops it; nothing reads it.
- `EffectiveAssignmentRoute` freshness: `resolve_effective_route(task)` is now
  "explicit class or nothing". The scheduler's `assignment_routes` mapping, the
  launch check, and the pool claim query all read `tasks.intelligence_class`
  directly. The claim query loses its LEFT JOIN.
- `purpose: assignment_routing` on artifacts.

## 7. Acceptance

1. A task created with an explicit class and no profile is claimable by the
   pool that runs that class within one playbook run, with no operator action.
2. A task created with neither is routed by the LLM step and pinned to a profile
   it named from the options, in one run.
3. `aq task explain` on an unrouted task says the playbook is pending, failed
   (with the run id), or that no profile serves the class — never that it is
   waiting on a pool that cannot claim it.
4. A project-scope copy of the playbook overrides routing with no code change.
5. No orchestrator module contains a routing decision.

## 8. Rollout on an existing install

The activation table still points at the old fixed-node artifact, and the vault
holds the old `kind: assignment-routing` source. On the first boot of this code:

1. `ensure_default_playbooks` replaces a vault copy of the retired kind with the
   shipped pipeline (old bytes kept in `default-assignment-routing.md.bak`).
2. An operator imports and activates the reviewed bundle, exactly as for the
   CI main sentinel:

   ```bash
   aq playbook v2-import --path tests/fixtures/playbooks/v2/default-assignment-routing
   aq playbook activate --playbook-id default-assignment-routing --artifact-sha256 <hash from the import>
   ```

   The import validates the artifact against the live command registry, so it
   only succeeds on a daemon running this code.
3. Nothing reads `task_assignment_routes` any more. Rows left in it are inert;
   a later migration drops the table.

Until step 2 is done, `task.route_needed` events are held as pending (no
artifact can run them) and `aq task explain` reports `awaiting_intelligence_route`.
