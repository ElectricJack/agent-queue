---
name: aq-playbooks-and-gates
description: Playbook runs and human-in-the-loop gates in aq. Use to find and inspect a paused playbook run, resume it with an approve / reject decision, run a playbook by hand, or check playbook health. Also covers what the shipped default pipeline actually does (spec ingest, proposal gate, batch commit) and where assignment routing lives.
allowed-tools:
  - Bash
---

# aq playbooks + gates

Playbooks are deterministic DAGs that fire on bus events (`task.created`,
`task.completed`, `spec.approved`, `proposal.ready`, `gate.resolved`).
The framework runs them; no LLM is in the compile or dispatch path.
Human-in-the-loop gates pause a run until a human answers.

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
routing is a pinned contract that must also write the task's profile,
intelligence class and workspace. Resolve those with `aq task route`:

```bash
aq task route --task-id <task_id> --profile-id <profile_id>
```

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
the compiler task validates the artifact and activates it through the reviewed
workflow.

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
  profile, deduped on `spec-ingest:<spec_path>`, that turns the approved
  spec into a `task_batch_propose` proposal.
- `proposal.ready` → open a human gate ("Approve task batch?") whose
  `await_id` is pinned to the proposal id.
- `gate.resolved`, filtered to `gate_type: human` → call
  `task_batch_commit` with that `await_id`, writing the approved batch
  into the task graph.

It does **not** create per-task reviewers, final branch reviewers, or
review/PR gates on downstream work — code validation and delivery come
from the project's configured integration owner. The `reviewer` and
`final-reviewer` profiles remain available as explicitly selected
specialists: a review is one explicit task or gate when a change warrants
it, never an automatic stage or a chain of passes (software-factory policy,
`docs/concepts/factory-policy.md`).

Assignment routing is a **separate** playbook,
`default-assignment-routing`. It fires on `task.route_needed` — emitted
for a task that lacks an `intelligence_class`, a `profile_id`, or both —
reads the catalog with `task_route_options`, and writes the chosen class
and profile back with `task_route`, which also resolves the task's
routing gate. A project that wants different routing keeps a
project-scope copy of that file.

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
