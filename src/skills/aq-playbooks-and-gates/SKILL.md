---
name: aq-playbooks-and-gates
description: Playbook runs and human-in-the-loop gates in aq. Use to find and inspect a paused playbook run, resume it with an approve / reject decision, run a playbook by hand, or check playbook health. Also covers what the shipped default pipeline actually does (spec ingest, proposal gate, batch commit) and where assignment routing lives.
allowed-tools:
  - Bash
---

# aq playbooks + gates

Playbooks are deterministic DAGs that fire on bus events (`task.created`,
`task.completed`, `spec.approved`, `proposal.ready`, `gate.resolved`).
The framework dispatches them deterministically. Ordinary Markdown compilation
uses the compiler agent; pipeline compilation is deterministic.
Human-in-the-loop gates pause a run until a human answers.

## Worker verification

Workers can run `aq playbook dry-run --playbook-id <id> --event '{}'` for their
held task. The daemon pins the event to that task and project. Steps and AI
calls are simulated; this does not execute a routing change, approve a human
gate, or activate a playbook. The API option `invoke_ai` requires supervisor authority.

## Inspect playbooks

```bash
aq playbook list                                 # every playbook + enabled state
aq playbook get-source --playbook-id <id>        # the playbook's markdown source
aq playbook health                               # active runs, stuck, failure rate
aq playbook show-graph --playbook-id <id>        # DAG rendered as ASCII
```

## Inspect runs

```bash
aq playbook list-runs                            # recent runs across all playbooks
aq playbook list-runs --playbook-id <id> --status running
aq playbook inspect-run --run-id <run_id>        # nodes, statuses, outputs
```

## Resolve a paused run

A run that hit a HITL node parks in status `paused`. Find it, read why it
stopped, then hand it the decision:

```bash
aq playbook list-runs --status paused            # everything awaiting a human
aq playbook inspect-run --run-id <run_id>        # which node paused, and on what
aq playbook resume --run-id <run_id> --human-input "approve"
```

`--human-input` is free text: it is appended to the run's conversation and
is what the next transition is evaluated against, so say *approve* / *reject*
plus the reason rather than a bare token.

Gate rows can also be driven directly, when you have the gate id rather than
a run id:

```bash
aq task gate-list --status open                     # everything awaiting an answer
aq task gate-list --project-id <pid> --gate-type human
aq task gate-show --gate-id <gate_id>               # the gate + its waiter task ids
aq task gate-resolve --gate-id <gate_id> --resolved-by <who> --resolution "approve: ..."
```

`--status` takes `open` / `resolved` / `expired`; `--gate-type` takes `human`,
`timer`, `pr-merged`, `ci-run`, `event`, `task` or `routing`. On
`gate-resolve`, `--gate-id` and `--resolved-by` are both required —
`--resolved-by` is recorded on the gate row — and `--resolution` is the free
text stored as the answer. Resolving is idempotent.

`routing` gates are the one exception: `gate-resolve` refuses them, because
a routing gate resolves only when the task is routed. The project's router
(`task_route_apply`) resolves it as it writes the route; nobody else picks a
profile. When a gate stays open, `aq task explain --task-id <task_id>` says
why the router has not routed the task (`route_held`, `route_no_candidates`,
`route_failed`, `router_not_ready`, `router_unbound`). To send the task back
to its router with new hints:

```bash
aq task route --task-id <task_id> [--intelligence-class <class>] [--task-type <kind>] \
  --reason "why the hints change"
```

The only way to name a profile is the audited emergency override,
`aq task route-override`, which the local operator and the supervisor use on
a human's instruction (it needs `--reason`, and `aq doctor --check
routing.bypassed` lists it until `aq task route` clears it).

Prefer `aq playbook resume` when the paused run's HITL node wants free text
back in the run conversation; use `gate-resolve` when the gate row itself is
what you are answering.

When a gate does resolve, two things happen:
1. The gate row transitions to `resolved` + records who resolved it.
2. Every waiter task attached to the gate re-checks its blocked state;
   if the gate was the only blocker, the task flips to `READY`.

## Playbook compilation (compiler-as-agent path)

Ordinary playbook markdown edits enqueue a compile task under the
`playbook-compiler` profile. Do not validate or install the source inline:
the compiler task validates a proposal for the reviewed activation workflow.

`playbook-compiler` agents iterate with `aq playbook v2-propose` and never
activate; activation is `aq playbook activate` against a reviewed hash.

For the `pipeline` kind (deterministic parse, no LLM), edits go straight to
the parser — no compiler task is enqueued.

## Run a playbook by hand

```bash
aq playbook run --playbook-id <playbook_id> \
  --event '{"type": "manual", "project_id": "<pid>", "task_id": "..."}'
```

Useful for testing a rule change locally without waiting for the
trigger event.

## Default pipeline (shipped)

The system default pipeline is a `kind: pipeline` playbook with exactly
three rules — approved specs in, an approved task batch out:

- `spec.approved` → create a spec-ingest task under the `spec-ingest`
  profile with an explicit `standard-high` class, deduped on
  `spec-ingest:<spec_path>`, that turns the approved spec into a
  `task_batch_propose` proposal.
- `proposal.ready` → open a human gate ("Approve task batch?") whose
  `await_id` is pinned to the proposal id. The proposal's payload is
  frozen from then on (`task_batch_update` refuses), so the decision
  covers exactly one revision.
- `gate.resolved`, filtered to `gate_type: human` and a `resolution` of
  `approve` or `approved` → call `task_batch_commit` with that
  `await_id`, the `gate_id` and the `project_id`. The command re-reads
  the gate and answers `not_approved` unless it is that proposal's
  resolved approval; a replay answers `already_committed` with the
  original task ids. A rejected, expired or free-text resolution commits
  nothing — resolve with `approve`/`approved` to approve.

Every refused step (`not_approved`, `rejected`, `runtime_error`) ends its
run `failed`, never `completed`.

It does **not** create per-task reviewers, final branch reviewers, or
review/PR gates on downstream work — code validation and delivery come
from the project's configured integration owner. A review is one explicit
task or gate when a change warrants it (`aq review dispatch`), never an
automatic stage or a chain of passes (software-factory policy,
`docs/concepts/factory-policy.md`).

Assignment routing is a **separate** playbook,
`default-assignment-routing`. It fires on `task.route_needed` — emitted
for a queued task its router still owes a route — plans the route with
`task_route_plan`, and writes it with `task_route_apply`, which also
resolves the task's routing gate. Every project is bound to a router
(`aq project set <project> router <playbook-id>`, local operator only);
a project that wants different routing runs a project-scope copy of that
file under its own id and is bound to it.

To see them in action:

```bash
aq playbook show-graph --playbook-id default-pipeline
aq playbook list-runs --playbook-id default-pipeline --limit 20
aq playbook show-graph --playbook-id default-assignment-routing
```

## Rules of thumb

- **Never resolve a gate someone else is waiting on unless you are the
  human they are asking.** Gates are the coordination point between
  agents and humans; resolving one without authority breaks the trust
  model.
- **A stuck run wants attention, not a re-run.** `aq playbook
  inspect-run` shows why it stalled; fix the underlying cause (missing
  context, failing sub-command, expired gate) before restarting.
- **Compile errors go to the playbook-compiler agent, not to you.** If
  a `.md` edit produced a FAILED compile task, that task will show the
  error — read it, fix the markdown, re-save, the watcher enqueues a
  fresh compile.

## Catalogue, state and scripts

Discover commands with `aq playbook commands` (`playbook_commands`) and diagnostic
scope/coverage with `doctor_catalog`. Generated coverage includes review verbs;
it does not grant authority, human decisions, session ownership or cross-project
access. `run_command`, `shutdown`, `restart_daemon`, `update_and_restart` are denied.
Generic results preserve their raw object paths and require a boolean `success`;
malformed results are `contract_violation`, denied callers `unauthorized`.

Use finite snapshot `page_tokens` (30-minute TTL, up to 500 rows/192 KiB per page,
262,144-byte response). Producer completeness and successful traversal are separate
requirements. `task_observations_v1` starts system-only; a project cannot scan the
whole system then filter. Unknown/unported checks and unsupported scope are explicit
failures, not healthy empty scans. Never clear on partial/expired coverage.

State survives recompilation, restart, disable/re-enable and source deletion.
CAS clears use revisions saved before scans, never refreshed revisions with old
absence evidence. Notification episodes suppress unchanged findings; null reminders
mean no repeat. Explicit operator reset/version migration can re-alert and retains
replay history. See `docs/guides/playbook-state.md` for quotas and reset semantics.

Scripts run as ordinary processes; `resources.scripts.enabled` defaults true.
Jobs admission, runtime/input configuration and protected-output keys still apply;
no cgroup/namespace/network isolation, launcher or host-prerequisite gate exists.
Timeout and output caps bound execution; ambiguous launch requires operator action,
never automatic replay. Dry-run/shadow launches nothing and leaves values symbolic.
Public receipts contain handles; `job_logs` checks run ownership before returning
bounded protected stdout/stderr. See `docs/guides/playbook-script-sandbox.md`.

Source frontmatter declares `state_schema_version`, `required_capabilities`, and
`scripts` program/dependency/limit policy. These are not compiler-supplied identity
or grants. Review real predicates, program text, settings and protected/unresolved
values. Follow `docs/guides/playbook-building-blocks.md` for deliberate pin rebuilds,
installation drift/reseed and rollback; never refresh installed policy implicitly.
