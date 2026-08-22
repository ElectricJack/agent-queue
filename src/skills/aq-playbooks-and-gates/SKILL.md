---
name: aq-playbooks-and-gates
description: Playbook runs and human-in-the-loop gates in aq. Use to inspect a paused playbook run, resolve a human gate (approve / reject), see which gates are currently open, run a playbook by hand, or check playbook health. Also covers how the default pipeline routes task events into review + final-review + spec-ingest flows.
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
aq playbook show <playbook_id>                   # metadata + rules
aq playbook health                               # active runs, stuck, failure rate
aq playbook show-graph <playbook_id>             # DAG rendered as ASCII
```

## Inspect runs

```bash
aq playbook runs                                 # recent runs across all playbooks
aq playbook runs --playbook <id> --status running
aq playbook inspect-run <run_id>                 # nodes, statuses, outputs
```

## Resolve a paused run

If a run hit a HITL gate and is waiting:

```bash
aq gate list --status open --project <pid>
aq gate show <gate_id>                           # full detail incl. waiter tasks
aq gate resolve --gate-id <gate_id> \
  --resolved-by <your_session_id> \
  --resolution approve                           # or 'reject'
```

Two things happen when you resolve:
1. The gate row transitions to `resolved` + records who resolved it.
2. Every waiter task attached to the gate re-checks its blocked state;
   if the gate was the only blocker, the task flips to `READY`.

## Compile + install a playbook (compiler-as-agent path)

Ordinary playbook markdown edits enqueue a compile task under the
`playbook-compiler` profile. You don't compile inline any more:

```bash
aq playbook validate vault/system/playbooks/<name>.md   # parse + lint
aq playbook install <playbook_id> <compiled_json_path>  # atomic swap
```

For the `pipeline` kind (deterministic parse, no LLM), edits go
straight to the parser — no task enqueued.

## Run a playbook by hand

```bash
aq playbook run <playbook_id> --project <pid> \
  --context '{"task_id": "..."}'                # pass a JSON context
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
aq playbook show default-pipeline
aq playbook runs --playbook default-pipeline --limit 20
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
