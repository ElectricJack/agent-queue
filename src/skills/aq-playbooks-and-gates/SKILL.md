---
name: aq-playbooks-and-gates
description: Playbook runs and human-in-the-loop gates in aq. Use to find and inspect a paused playbook run, resume it with an approve / reject decision, run a playbook by hand, or check playbook health. Also covers how the default pipeline routes task events into review + final-review + spec-ingest flows.
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

Gate rows themselves (`gate_list` / `gate_show` / `gate_resolve`) have no
usable CLI form right now — `aq task gate-list`, `aq task gate-show` and
`aq task gate-resolve` are generated with no parameters at all, so they
cannot name a gate (tracked by `bright-forge-33`). Drive paused runs through
`aq playbook resume` until that lands.

When a gate does resolve, two things happen:
1. The gate row transitions to `resolved` + records who resolved it.
2. Every waiter task attached to the gate re-checks its blocked state;
   if the gate was the only blocker, the task flips to `READY`.

## Compile + install a playbook (compiler-as-agent path)

Ordinary playbook markdown edits enqueue a compile task under the
`playbook-compiler` profile. You don't compile inline any more:

```bash
aq playbook validate vault/system/playbooks/<name>.md   # parse + lint
aq playbook install --playbook-id <playbook_id> \
  --compiled-path <compiled_json_path>                  # atomic swap
```

For the `pipeline` kind (deterministic parse, no LLM), edits go
straight to the parser — no task enqueued.

## Run a playbook by hand

```bash
aq playbook run --playbook-id <playbook_id> \
  --event '{"type": "manual", "project_id": "<pid>", "task_id": "..."}'
```

Useful for testing a rule change locally without waiting for the
trigger event.

## Default pipeline (shipped)

The default pipeline is a `kind: pipeline` playbook that wires:

- `task.created` → route the task via `task_route` (assigns profile +
  intelligence class + workspace kind).
- `task.completed` with `branch_name` → create a per-task review task
  under the `reviewer` profile.
- `task.completed` with `branch_name AND pr_url` → create a per-branch
  final-review task under the `final-reviewer` profile once every
  per-task reviewer approves.
- `spec.approved` → create a spec-ingest task under `spec-ingest` that
  turns the approved spec into a `task_batch_propose` proposal.
- `proposal.ready` → open a human gate; on approve, run
  `task_batch_commit` to materialize the proposed tasks.

To see it in action:

```bash
aq playbook show-graph --playbook-id default-pipeline
aq playbook list-runs --playbook-id default-pipeline --limit 20
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
