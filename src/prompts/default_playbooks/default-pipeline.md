---
id: default-pipeline
kind: pipeline
role: default-pipeline
scope: system
triggers:
  - task.completed
  - spec.approved
  - proposal.ready
  - event_type: gate.resolved
    filter:
      gate_type: human
---

# Default Pipeline

The system default pipeline. Reacts to task lifecycle events.

Ships five rules:

- **Per-task review** (`task.completed`) — on every `task.completed` whose task
  has a `branch_name` and produced code, spawns one reviewer task with a
  `discovered-from` edge to the reviewed task and attaches a `task` gate to
  each downstream dependent so nothing downstream runs until the review
  completes.
- **Per-branch final review** (`task.completed`, `scope: branch`) — maintains
  exactly one `final-reviewer` task per `(project, branch)` pair keyed by
  `branch-review:<branch_name>` via `ensure_task`. Every per-task review on
  the same branch is wired `blocks` → the branch's final review so the final
  reviewer only runs once all per-task reviews have completed. Downstream
  dependents on the reviewed task additionally get a `pr-merged` gate awaiting
  the branch's PR URL.
- **Spec ingest** (`spec.approved`) — ensures exactly one `spec-ingest` task
  per approved spec file, keyed `spec-ingest:<spec_path>` via `ensure_task`.
  The spec-ingest agent reads the spec and calls `task_batch_propose` with
  the derived task graph.
- **Proposal gate** (`proposal.ready`) — raises a `human` gate asking a person
  to approve the proposed task batch, `await_id` pinned to the proposal id.
- **Proposal commit** (`gate.resolved`, filtered to `gate_type: human`) —
  once the gate is resolved, calls `task_batch_commit` for the awaited
  proposal so the approved batch is written into the task graph.

Both review rules also require `event.no_code` to be falsy. The session close
path sets `no_code: true` only when the central Git predicate proved every
relevant delivery ref clean and exactly zero commits ahead of its base. Raw
intent such as `read_only: true` or `--work-outcome no-op` never sets the
event flag. The proof is recorded before any direct merge or worktree
integration can hide the branch's commits, so genuine no-work tasks in both
direct and pull-request mode are suppressed while delivered work is reviewed.

Reviewer tasks run on a slot checked out on their own `aq/<id>` branch, so
they carry a `branch_name` like any other session task; without this guard
every finished review spawned a review *of the review*, recursively. Emitters
that do not set the key (container settlement, custom pipelines) still fire
the review.

Both review rules also require `event.review_task` to be falsy. `review_task`
is structural, and the close path sets it from either of two signals a profile
edit cannot reach (`src/review_keys.py` owns both):

- the **dedup key** this pipeline stamps on every review it creates —
  `review:task:<task_id>` or `branch-review:<branch_name>`;
- the **reviewer role** — a `reviewer` or `final-reviewer` `profile_id`.

The second exists because the first only marks rows the *shipped* pipeline
created: a project that routes reviews through its own pipeline keys them
however it likes, and with a non-read-only reviewer that left every guard blind
and the `Review: Review: ...` chain grew again (task solid-beacon-50). A custom
pipeline that both keys its review tasks differently *and* runs them under its
own profile ids must add its own guard.

The common event emitter derives both signals from the task row for every
`task.completed`, so container settlement and future emitters cannot forget
them (tasks prime-quest-67 and crisp-summit-88). The dispatch path
(`Orchestrator._on_playbook_trigger`) derives it again from the hydrated task
row, because `truthy: false` passes on a *missing* key: an older daemon or a
hand-written event used to fire the review anyway (task prime-cascade-64).

Neither event flag reaches a daemon still running code older than the flag, nor
a vault copy of this file whose rules an operator edited (`ensure_default_playbooks`
never refreshes a copy it does not recognise); both were true at once on the
live box and the chains grew ten deep anyway (task solid-harbor-68). The last
line of defence is therefore in the command every version of these rules must
call: `ensure_task` refuses a `review:task:<X>` key when X itself carries a
`review:task:` or `branch-review:` key, and the refusal follows the node's
`on_failure` edge to `done`. Rules and event flags stop a review early; the
command guarantees it.

The `ensure_task` nodes below pin `profile_id` but no `intelligence_class`, so
the assignment-routing playbook chooses the class for the tasks they create. A
pinned profile is a compatibility constraint, not a route: until that decision
lands the task is held back with `awaiting_intelligence_route`. A project whose
reviewer profiles have a fixed class can skip that wait by adding
`"intelligence_class": "<class id>"` beside `profile_id` in its own copy of
this pipeline — it must match the profile's `default_class`, or no worker will
be compatible with the task.

## Rule: per-task-review

Trigger: `task.completed`.

Guard: all three of — the completed task has a non-empty `branch_name`;
`event.no_code` is falsy; `event.review_task` is falsy.

1. Call `ensure_task` in the event's project (`project_id`) with `dedup_key`
   `review:task:{event.task_id}`, `title` `Review: {event.title}`, and
   `profile_id` `reviewer`. The `description` gives the reviewer the reviewed
   `task_id`, the branch name, the PR URL, and this instruction: read the diff
   and either approve by closing this task with a summary, or reject by calling
   `reopen_with_feedback` on the reviewed task and then closing this task. Bind
   the result as `review`. A failure ends the rule.
2. Call `add_dependency` with `task_id` `review.task_id`, `depends_on` the
   completed `event.task_id`, and `dep_type` `discovered-from`, so the review
   is recorded as discovered from the work it reviews. A failure ends the rule.
3. Call `get_downstream_tasks` for the completed `task_id` and bind the result
   as `downstream`. A failure ends the rule.
4. For each `dep` in `downstream.tasks`, call `gate_create` in the event's
   `project_id` with `gate_type` `task`, `title`
   `Awaiting review of {event.task_id}`, `await_id` `review.task_id`, and
   `dep.id` as the sole entry in `waiter_task_ids`, so no dependent starts
   before the review completes. A failure on one dependent ends the rule.

Any failure ends this rule without failing the other four.

## Rule: per-branch-final-review

Trigger: `task.completed`.

Guard: all four of — the completed task has a non-empty `branch_name`; it has a
non-empty `pr_url`; `event.no_code` is falsy; `event.review_task` is falsy.

1. Call `ensure_task` in the event's `project_id` with `dedup_key`
   `branch-review:{event.task.branch_name}`, `title`
   `Final review: {event.task.branch_name}`, and `profile_id` `final-reviewer`.
   The `description` says this is the final review for that branch, that it runs
   after every per-task review approves, and that it holds merge authority. Bind
   the result as `final`. This is the `ensure_task` that makes the rule maintain
   exactly one final review per `(project, branch)` pair. A failure ends the rule.
2. Call `ensure_task` again, with the same `dedup_key` `review:task:{event.task_id}`
   and `profile_id` `reviewer` that `per-task-review` uses, so the two rules
   converge on one review task per completed task rather than two. `title` is
   `Review: {event.title}` and the `description` names the reviewed `task_id`
   and its branch. Bind the result as `review`. A failure ends the rule.
3. Call `add_dependency` with `task_id` `final.task_id`, `depends_on`
   `review.task_id`, and `dep_type` `blocks`, wiring every per-task review on
   the branch ahead of the branch's final review. **A failure here does not end
   the rule** — step 4 runs either way, because the downstream `pr-merged` gates
   protect dependents independently of the review ordering edge.
4. Call `get_downstream_tasks` for the completed `task_id` and bind the result
   as `downstream`. A failure ends the rule.
5. For each `dep` in `downstream.tasks`, call `gate_create` in the event's
   `project_id` with `gate_type` `pr-merged`, `title`
   `Awaiting merge of {event.task.branch_name}`, `await_id` the task's
   `pr_url`, and `dep.id` as the sole entry in `waiter_task_ids`.

## Rule: spec-ingest-on-approve

Trigger: `spec.approved`. No guard.

1. Call `ensure_task` in the event's `project_id` with `dedup_key`
   `spec-ingest:{event.spec_path}` (the `spec_path` the event carries), `title`
   `Ingest spec {event.spec_path}`,
   and `profile_id` `spec-ingest`. The `description` instructs the agent to read
   the spec, list the project's existing tasks, emit `task_batch_propose` with
   the derived task graph, and iterate on validation errors. The dedup key is
   what makes this exactly one ingest task per approved spec file.

## Rule: proposal-ready-gate

Trigger: `proposal.ready`. No guard.

1. Call `gate_create` in the event's `project_id` with `gate_type` `human`,
   `title` `Approve task batch?`, `question`
   `Approve proposal {event.proposal_id}?`, and `await_id` pinned to
   `event.proposal_id` so the resolution can be matched back to the proposal it
   approves.

## Rule: commit-on-gate-resolve

Trigger: `gate.resolved`, filtered by the playbook's trigger to
`gate_type: human`.

1. Call `task_batch_commit` with `proposal_id` taken from the resolved gate's
   `await_id`, writing the approved batch into the task graph. The `await_id`
   pinned by `proposal-ready-gate` is the only thing that connects the two
   rules; nothing else carries the proposal identity across the human decision.

## Failure handling, uniformly

Every step above routes both success and failure onward as described and then
to the rule's terminal step. No step retries, and no failure fails the playbook:
a rule that cannot finish leaves the work it already did in place and stops. The
review pipeline is a best-effort attachment to task completion, never a
precondition for it.
