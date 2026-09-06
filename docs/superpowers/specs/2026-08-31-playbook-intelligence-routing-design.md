> **Superseded 2026-09-06.** The coordinator, the route table and the fixed
> `assignment-routing` lowering described here were replaced by an authored
> pipeline playbook over `task_route_options` / `task_route`; see
> `2026-09-06-assignment-routing-as-playbook.md`. Item 6 below no longer holds:
> the playbook now names the profile too, from the option rows it is given.

# Playbook-owned intelligence routing

Date: 2026-08-31

Status: Approved for implementation. This document is the governing specification
for the narrow assignment-routing change.

Source baseline: `agent-queue2`, commit `0436555e`.

Supersedes:

- `docs/superpowers/specs/2026-08-31-mandatory-triage-playbook-design.md`
- `docs/superpowers/plans/2026-08-31-mandatory-triage-playbook.md`

## 1. Outcome

AQ must stop choosing a task's `intelligence_class` algorithmically. When a task
does not already have an explicit `intelligence_class`, a fast LLM-backed assignment
playbook chooses one from the classes that the current agent flock can execute. The
playbook may also choose a provider when it has a reason to pin the task to that
provider, such as currently known usage availability.

After that decision, the existing orchestrator continues doing the work it already
does well: it chooses a compatible capability profile and concrete agent while
honoring availability, lifecycle, affinity, workspace locks, fair share, pool
capacity, and other scheduling constraints. The playbook does not select an agent.

AQ ships a system assignment-routing playbook that works without configuration. A
project may select a custom playbook to replace the system default for that project.
The selected playbook remains a normal, inspectable PlaybookRunner definition so its
instructions can evolve without moving policy back into scheduler code.

## 2. Ownership boundary

The terms in this design have deliberately separate meanings:

| Concern | Owner | Meaning |
| --- | --- | --- |
| `intelligence_class` | Explicit task setting or assignment playbook | The model and reasoning tier required for the task. |
| Provider | Assignment playbook when returned; otherwise scheduler | An optional execution-provider constraint such as OpenAI or Anthropic. |
| `profile_id` | Existing orchestrator | The worker's capabilities, tools, role, and lifecycle configuration. |
| Concrete agent/session | Existing orchestrator | The available process that receives or claims the task. |

Precedence is simple:

1. A task's explicit `intelligence_class` is authoritative and skips the LLM route.
2. Otherwise, the current successful assignment-playbook decision supplies the
   `intelligence_class` and optional provider.
3. No profile default, agent default, or scheduler heuristic may silently invent the
   task's class when neither of the first two sources exists.

An agent or profile may still have fixed execution settings. Those settings describe
what the worker can execute; they are compatibility constraints, not a substitute
for the task route. A worker with a fixed class can receive the task only when it
matches the effective task class.

## 3. Scope limits

This change is intentionally confined to assignment routing. It does not:

- add a universal task-creation or task-edit admission gate;
- create a long-running triage task, control agent, session, workspace, or flock
  execution type;
- ask the LLM to choose `profile_id`, workspace, lifecycle, pool, or exact agent;
- retire pool claims or converge the system onto push scheduling;
- replace dependency, approval, routing-gate, blocked-state, or task-status logic;
- add external provider quota or billing integrations;
- redesign the dashboard or general playbook engine;
- reroute work that is already assigned or running; or
- preserve the abandoned broad implementation as a partially enabled alternate
  path.

## 4. Effective route

The scheduler and claim paths consume one internal `EffectiveAssignmentRoute`:

```text
task_id
intelligence_class
provider?          # null means any compatible provider
source             # explicit | playbook
input_hash?        # present for playbook decisions
decision_id?       # present for playbook decisions
```

For an explicit class, the route is derived directly from the task and has no
playbook decision. For an unspecified class, the route is valid only when the saved
decision's task-input hash still matches the current task and its class/provider is
still present in the current compatible option catalog.

Freshness is exactly those two hashes. A route must not be revoked by a write that
changes no routed input: the reservation that moves a task from READY to ASSIGNED
bumps `tasks.updated_at`, and treating that revision as part of the freshness test
made every playbook-sourced route die at the moment of assignment, failing the
launch check with "awaiting intelligence route", pausing the task on a backoff, and
returning its worker to IDLE on every cycle.

The effective route is an input to existing matching. It does not mutate
`profile_id`, `affinity_agent_id`, or `assigned_agent_id`. Launch configuration must
receive the effective class so an unpinned compatible worker actually launches at
the selected tier. A non-null provider filters candidates before the existing
selection algorithm runs. A null provider leaves provider selection to existing
behavior.

## 5. Decision persistence

Add one current-decision table, `task_assignment_routes`, with:

| Column | Purpose |
| --- | --- |
| `task_id` | Primary key and foreign key to the routed task. |
| `project_id` | Project-scoped lookup and integrity check. |
| `input_hash` | Hash of the material task snapshot used by the LLM. |
| `task_updated_at` | Redundant task revision used for an inexpensive freshness join in the pool-claim SQL, which cannot hash a task row. It is *not* part of the effective-route test — see §4 — and the coordinator re-stamps it when a write moves the revision without changing a routed input. |
| `options_hash` | Hash of the compatible class/provider catalog used by the LLM. |
| `intelligence_class` | Required class selected by the playbook. |
| `provider` | Nullable hard provider constraint. |
| `playbook_id` / `playbook_version` | Definition that made the decision. |
| `playbook_run_id` | Inspectable PlaybookRunner run. |
| `reason` | Short LLM rationale for operators. |
| `decided_at` | Decision timestamp. |

The row stores the current successful decision and is replaced atomically after a
new valid decision. Playbook run records retain the detailed execution history, so
this feature does not introduce a second append-only audit system.

`tasks.intelligence_class` remains the explicit task requirement. The playbook does
not write into it. Keeping explicit intent separate from derived policy makes
precedence unambiguous and lets task edits invalidate derived decisions without
erasing a user's setting.

Add nullable `projects.assignment_playbook_id`. Null means the bundled system
default. A non-null value must resolve to an enabled playbook whose role is
`assignment-routing` and whose output obeys the assignment contract.
If an explicitly selected project playbook is missing, invalid, or disabled, routing
for that project reports the configuration error and waits; it never silently falls
back to the system definition.

All schema changes are additive, migrated on SQLite and PostgreSQL, and included in
SQLite-to-PostgreSQL copying. No active task row is rewritten during migration.

## 6. What the playbook sees

The routing coordinator builds a bounded batch of otherwise assignable tasks from a
single project. Each task entry contains only material assignment context:

- task ID, title, description, type, priority, and relevant specification/context
  references already available to the scheduler;
- explicit execution constraints that the route must not weaken; and
- the input hash that the response must echo.

The batch also contains a normalized option catalog:

- configured intelligence classes;
- providers on enabled ordinary execution routes that can run each class;
- configured capacity and current idle/busy counts for those routes; and
- recent usage or budget availability already known to AQ, marked `unknown` when AQ
  has no observation.

The implementation does not call provider billing or quota APIs. Usage is an
advisory snapshot for the LLM. Rapidly changing busy counts do not invalidate an
otherwise sound route. Administrative changes to classes, providers, enabled
workers, fixed worker settings, or hard budget availability change `options_hash`.

Disabled agents, control-only profiles, and playbook execution profiles do not make
an option eligible for ordinary work. Busy agents still establish configured
compatibility; their busy state is reported separately so temporary load does not
make a valid class disappear.

## 7. Playbook contract

AQ bundles and installs one immutable-source system definition named
`default-assignment-routing`, plus its compiled graph. Installation follows the
existing default-playbook rules and never overwrites a user-owned project playbook.

The graph uses the existing in-process, direct-LLM PlaybookRunner path. It does not
launch a CLI session or give the model general AQ command tools. One structured node
returns:

```json
{
  "decisions": [
    {
      "task_id": "task-id",
      "input_hash": "hash-from-input",
      "intelligence_class": "configured-class",
      "provider": null,
      "reason": "Short assignment rationale"
    }
  ]
}
```

The response must contain exactly one decision for each task supplied in the batch,
with no unknown tasks or duplicate IDs. `provider` is optional. When present, it is
a hard constraint and must be compatible with the chosen class. Class and provider
are the only assignment choices; task ID and input hash are integrity fields, and
the short reason is operator-facing audit metadata with a conservative length
limit.

The core, not the prompt, validates all output. It rejects unknown classes,
unsupported providers, missing or extra tasks, duplicate decisions, malformed
values, and choices that weaken explicit constraints. A structural or catalog error
rejects the whole response. After a structurally valid response, the commit
transaction may skip a task that independently became explicit, assigned, terminal,
or stale while the LLM was running; valid decisions for the other tasks commit
together and skipped tasks are reconciled separately. One frequently edited task
therefore cannot force the rest of a batch through another LLM call.

## 8. Routing coordinator and speed

Assignment routing runs as a small coordinator inside the orchestrator cycle:

1. Existing status, dependency, approval, blocked-state, and timing checks identify
   tasks that are close enough to assignment to need a route. That includes an
   unblocked DEFINED task — the shape every worker filing starts in — so filed work
   is routed in the cycle it is created rather than after the promotion cascade,
   while a DEFINED task still waiting on the graph produces no router traffic.
2. Tasks with an explicit class or a fresh saved route are skipped.
3. Remaining tasks are grouped by project and current playbook selection.
4. Up to 25 tasks are sent in one PlaybookRunner call per project.
5. The LLM response is validated and committed without holding a database lock
   during the network call.
6. A scheduler wake follows a successful commit so work can be assigned immediately.

The coordinator is event-driven with reconciliation on normal orchestrator cycles.
A short debounce coalesces bursts, but a single task is not held waiting for a full
batch. Empty queues produce no LLM call. At most one assignment-routing run is
in-flight per project in one process.

Each normalized batch has a deterministic batch key derived from project, selected
playbook/version, ordered task input hashes, and `options_hash`. The run event ID
combines that batch key with a persisted attempt ordinal. The existing unique
`(playbook_id, event_id)` guard deduplicates the same attempt across orchestrators.
After an existing run wins that race, a contender observes its result instead of
starting another model call. A failed or invalid terminal attempt advances the
ordinal, allowing a later retry without deleting its audit record.

Failed calls and invalid responses use exponential retry backoff with a five-minute
cap. A material task edit, playbook version change, or option-catalog change resets
that task's wait. Retry timing may be held in coordinator state and reconstructed
from recent PlaybookRun records after restart; an immediate retry on restart is also
safe because the deterministic event ID prevents a duplicate identical run. There
is no fallback that guesses a class.

## 9. Task changes and concurrency

No task-edit hook is required. The coordinator computes `input_hash` from a
canonical serialization of the material fields it gave the LLM. Before committing a
decision, the transaction reloads each task and recomputes that hash. A task whose
hash changed is skipped as stale while decisions for other unchanged tasks can
commit. The hash is the whole staleness test; a bare revision bump is not.

The scheduler and claim paths perform the same freshness check when reading a saved
decision. This closes the race where a task changes immediately after a successful
route commit. The next coordinator pass routes the new snapshot.

The transaction also verifies:

- every task still belongs to the project and is not assigned or terminal;
- no task gained an explicit class while the LLM was running;
- the selected playbook/version is still the project's effective definition; and
- `options_hash` still represents the current administrative compatibility catalog.

Transient worker occupancy does not fail this validation. It only makes the routed
task wait for a compatible agent.

## 10. Scheduler and claim integration

Both ordinary assignment paths must enforce the same rule:

- Push scheduling does not reserve an unspecified task until it has a fresh
  playbook route.
- Pool claim selection does not return an unspecified task until it has a fresh
  playbook route.
- Both filter candidates by the effective class and optional provider, then reuse
  their existing profile/agent selection and reservation logic.

The old fallback from a missing task class to `task_profile.default_class` may still
describe a worker's launch default, but it cannot satisfy assignment eligibility.
Every active/default path must prove the task class came from explicit task intent
or a fresh playbook decision before matching.

When a route is valid but every compatible worker is busy, at capacity, locked, or
temporarily budget-limited, the task waits with the route intact. When an
administrative change removes all configured compatibility for the selected
class/provider, the route becomes stale through `options_hash` and the coordinator
asks the playbook to choose again from the new catalog.

## 11. Existing routing gates and triage path

AQ already attaches routing gates to some tasks and maintains a reusable
`triage-open` task whose agent calls `task_route`. That path currently mixes profile,
class, workspace, and agent-facing assignment policy. It must not remain active in
parallel with assignment routing.

The cutover is narrow:

- remove the default pipeline's `task-created-routing` and
  `worker-filed-triage` assignment rules, including creation or reopening of
  `triage-open`, while keeping its review, spec, proposal, and other unrelated
  rules;
- let the assignment coordinator consider a task whose only unresolved blocker is
  its existing routing gate;
- after a successful decision, resolve only that task's open routing gates using a
  core-owned resolution that records the playbook run;
- resolve those legacy routing gates without an LLM call when the task already has
  an explicit class; and
- preserve all unrelated default-pipeline rules, gate types, historical triage
  tasks, and historical playbook runs.

Tasks created through paths that do not attach routing gates still receive the same
scheduler-boundary protection. Therefore this feature does not need to edit every
task-creation path. `task_route` may remain as a compatibility wrapper for an
explicit administrative class override, but automatic routing no longer uses it to
choose a profile or workspace. There is one effective-route resolver used by push
scheduling, pool claims, diagnostics, and launch preparation.

## 12. Recovery and diagnostics

On startup and during reconciliation, the coordinator finds assignment candidates
with no explicit class and no fresh route. This recovers work after crashes without
special task states. An interrupted PlaybookRun remains inspectable; a new run is
eligible after normal timeout/recovery handling.

Operators must be able to distinguish at least:

- awaiting intelligence route;
- assignment playbook running;
- assignment playbook unavailable or invalid;
- invalid or stale LLM response, waiting to retry;
- route selected, waiting for a compatible agent; and
- selected class/provider no longer configured, awaiting reroute.

These reasons belong in existing task explanation/detail and logs. The saved route
shows its class, optional provider, reason, source playbook/version, run ID, and
freshness. A full dashboard redesign is outside this change.

## 13. Upgrade behavior

The migration is additive and does not interrupt assigned or running work.

- Tasks with an explicit `intelligence_class` proceed without an LLM call.
- Unassigned tasks without one are routed on their next eligible coordinator pass.
- Existing open routing gates are resolved only after a successful route.
- Existing `triage-open` tasks and runs remain historical; the default system stops
  creating or waking them for assignment.
- Existing project behavior uses the bundled system assignment playbook until an
  operator selects a project override.
- Removing a project override returns the project to the system default. Selecting
  a broken override blocks visibly until it is fixed or cleared.

## 14. Acceptance criteria

The implementation is complete only when tests demonstrate all of the following:

1. An explicit task `intelligence_class` bypasses the assignment LLM and remains
   authoritative.
2. A task without an explicit class cannot be pushed, claimed, or launched until a
   fresh playbook decision exists.
3. The bundled default works for every project without per-project copies; a custom
   override affects only its project.
4. Multiple ready tasks in one project are routed in a single bounded LLM call.
5. Omitting provider leaves existing provider choice available; returning provider
   hard-filters scheduling and claiming.
6. The playbook never chooses `profile_id` or a concrete agent. Existing scheduling
   rules make that selection after routing.
7. A task edit during the LLM call makes the response stale without relying on edit
   hooks; the updated task is routed on a later pass.
8. Invalid output, unavailable playbooks, and LLM failures wait and retry visibly;
   no algorithmic class fallback occurs.
9. Busy compatible workers preserve the route and the task waits; administrative
   removal of compatibility causes rerouting.
10. Concurrent orchestrators do not run or commit duplicate identical batches on
    SQLite or PostgreSQL.
11. Push scheduling and pool claims use the same effective-route semantics; pools,
    claims, sessions, task creation, and unrelated gates otherwise retain their
    existing behavior.
12. Restart reconciliation finds unrouted eligible work and neither loses nor
    duplicates a successful decision.
13. No ordinary-task assignment or launch path infers a missing task class from a
    profile, agent, or model default.

## 15. Delivery boundary

The implementation plan should be organized around these limited units:

1. persistence and effective-route resolution;
2. bundled/default and project-selected assignment playbooks;
3. batched coordinator, validation, deduplication, and recovery;
4. push scheduler and pool-claim consumption;
5. cutover from the current `triage-open` assignment path; and
6. diagnostics, upgrade coverage, and end-to-end verification.

No implementation task may reintroduce the superseded universal admission system,
triage worker identity, execution-type catalog, or pool retirement. Any newly
discovered need outside this boundary requires a design review before code is added.
