---
name: aq-playbooks-and-gates
description: Covers aq playbook runs and human-in-the-loop gates — listing and inspecting playbooks and runs, deciding a paused run's human wait through the ask preview flow, driving gate rows (`aq task gate-list`, `gate-show`, `gate-resolve`), why routing and review gates refuse a direct resolve, running or dry-running a playbook by hand, and what the shipped default pipeline and assignment-routing playbooks do. Use when a run is paused or failed, a task is blocked on a gate, a task is stuck unrouted, or a playbook needs a manual or dry run. Authoring, compilation, state and script contracts are in its reference file.
allowed-tools:
  - Bash
---

# aq playbooks + gates

Playbooks are typed, bounded graphs that fire on bus events (`task.created`,
`task.completed`, `spec.approved`, `proposal.ready`, `gate.resolved`).
The framework dispatches them deterministically. Ordinary Markdown compilation
uses the compiler agent; pipeline compilation is deterministic.
Human-in-the-loop gates pause a run until a human answers.

**Reference file:** [reference/authoring.md](reference/authoring.md) —
compilation, the command catalogue, snapshots, state, scripts and frontmatter
declarations, for authors and reviewers of a playbook policy.

## Worker verification

Workers can run `aq playbook dry-run --playbook-id <id> --event '{}'` for their
held task. The daemon pins the event to that task and project. Steps and AI
calls are simulated; this does not execute a routing change, approve a human
gate, or activate a playbook. The API option `invoke_ai` requires supervisor authority.

## Inspect playbooks

```bash
aq playbook list                                 # every playbook + enabled state
aq playbook get-source --playbook-id <id>        # the playbook's markdown source
aq playbook activation-health                    # enabled state and activation health
aq playbook v2-graph --playbook-id <id>          # structured semantic graph
```

## Inspect runs

```bash
aq playbook list-runs                            # recent runs across all playbooks
aq playbook list-runs --playbook-id <id> --status running
aq playbook inspect-run --run-id <run_id>        # nodes, statuses, outputs
```

## Resolve a paused run

For a run waiting on a human decision, inspect its wait and declared options
before sending a decision. A paused run can also need technical recovery;
`resume` accepts only human waits. Event, child-task and timer waits consume
their registered cause rather than manual prose.

A human decision is an **ask**: it is previewed, then accepted, so work
through it in order:

```
- [ ] Confirm you are the human (or delegated decider) the wait asks
- [ ] inspect-run: which step paused, its declared option keys, the pinned artifact
- [ ] Get the ask id and expected version (aq ask show, or the
      ask.context_missing reply from resume)
- [ ] aq ask prepare with the option key; review the preview (ten-minute lifetime)
- [ ] aq ask decide with the same intent, the preview id and a stable idempotency key
- [ ] Confirm application_state: applied before treating the run as resumed
```

```bash
aq playbook list-runs --status paused            # paused runs and their wait kind
aq playbook inspect-run --run-id <run_id>        # which node paused, and on what
aq playbook resume --run-id <run_id> --human-input approve
#   -> ask.context_missing with ask_id and expected; then:
aq ask show --ask-id <ask-id> --json
aq ask prepare --ask-id <ask-id> --expected '<expected JSON>' --option-key approve
aq ask decide --ask-id <ask-id> --expected '<expected JSON>' --option-key approve \
  --preview-id <preview-id> --idempotency-key <unique-key>
```

`--human-input` / `--option-key` is the exact declared option key, such as
`approve`; it is not conversation text. Put an optional explanation in
`--overall-note`. An undeclared option is refused. `aq playbook resume` also
accepts `--expected`, `--preview-id` and `--idempotency-key` and then decides
directly, with the same preview requirement.

Gate rows can also be driven directly, when you have the gate id rather than
a run id:

```bash
aq task gate-list --status open                     # everything awaiting an answer
aq task gate-list --project-id <pid> --gate-type human
aq task gate-show --gate-id <gate_id>               # the gate + its waiter task ids
aq task gate-resolve --gate-id <gate_id> --resolved-by <who> --resolution "<answer>"  # technical gate
```

`--status` takes `open`, `resolved`, `expired` or `cancelled`; `--gate-type`
takes `human`, `timer`, `pr-merged`, `ci-run`, `event`, `task`, `routing` or
`review`. What `gate-resolve` does depends on the gate type:

- **Technical gates** (timer, pr-merged, ci-run, event, task): `--gate-id`
  and `--resolved-by` are required — `--resolved-by` is recorded on the gate
  row — and `--resolution` is the free text stored as the answer. Resolving
  is idempotent.
- **Human gates** go through the same ask preview flow as a paused run: pass
  `--option-key` (or `--resolution`) with `--expected` and `--preview-id`, or
  get `ask.context_missing` with the ask id. The decider's identity is
  authenticated; `--resolved-by` with `--expected` is refused
  (`ask.not_decider`).
- **Review gates** are refused (`review_gate`): decide them in the review (the
  aq-reviews skill).
- **Routing gates** are refused, because a routing gate resolves only when the
  task is routed.

The project's router (`task_route_apply`) resolves a routing gate as it writes
the route; nobody else picks a profile. When a routing gate stays open, `aq task explain --task-id <task_id>` says
why the router has not routed the task (`route_held`, `route_no_candidates`,
`route_failed`, `router_not_ready`, `router_unbound`). To send the task back
to its router with new hints:

```bash
aq task route --task-id <task_id> [--intelligence-class <class>] [--task-type <kind>] \
  --reason "why the hints change"
```

Routing gates: the only way to name a profile is the audited emergency override,
`aq task route-override`, which the local operator and the supervisor use on
a human's instruction (it needs `--reason`, and `aq doctor --check
routing.bypassed` lists it until `aq task route` clears it).

Use `aq playbook resume` for a declared option on a human wait; use
`gate-resolve` when the gate row itself is what you are authorized to answer.

When a gate does resolve, two things happen:
1. The gate row transitions to `resolved` + records who resolved it.
2. Every waiter task attached to the gate re-checks its blocked state;
   if the gate was the only blocker, the task flips to `READY`.

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
  role, deduped on `spec-ingest:decision:<id>` with its approved packet receipt.
  Its server-stamped task batch commits directly after validation, without a
  second human gate.
- Ordinary `proposal.ready` → open a human gate ("Approve task batch?") whose
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
aq playbook v2-graph --playbook-id default-pipeline
aq playbook list-runs --playbook-id default-pipeline --limit 20
aq playbook v2-graph --playbook-id default-assignment-routing
```

## Rules of thumb

- **Never resolve a gate someone else is waiting on unless you are the
  human they are asking.** Gates are the coordination point between
  agents and humans; resolving one without authority breaks the trust
  model.
- **A stuck run wants attention, not a re-run.** `aq playbook
  inspect-run` shows why it stalled; fix the underlying cause (missing
  context, failing sub-command, expired gate) before restarting.
- **Compile failures need an explicit revision** — see
  [reference/authoring.md](reference/authoring.md).
