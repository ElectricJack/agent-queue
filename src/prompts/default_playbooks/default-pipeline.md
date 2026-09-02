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
path sets `no_code: true` on `task.completed` when the task by construction
left no commits behind — a `read_only: true` profile (the shipped `reviewer`
and `final-reviewer`) or a close with `--work-outcome no-op`. Reviewer tasks
run on a slot checked out on their own `aq/<id>` branch, so they carry a
`branch_name` like any other session task; without this guard every finished
review spawned a review *of the review*, recursively. Emitters that do not set
the key (container settlement, custom pipelines) still fire the review.

Both review rules also require `event.review_task` to be falsy. `no_code` is
only as reliable as the reviewer profile's `read_only` flag: a project that
gives its reviewers Write/Edit tools (`read_only: false`) disarms it and the
recursion returns. `review_task` is structural instead — it is set when the
finishing task carries the dedup key this pipeline stamps on every review it
creates (`review:task:<task_id>` or `branch-review:<branch_name>`, see
`src/review_keys.py`), so a review is recognised as a review whatever its
profile says. A custom pipeline that keys its review tasks differently must
either keep these prefixes or add its own guard.

Unlike `no_code`, `review_task` does not depend on which code path finished the
task. `Orchestrator._emit_task_event` derives it from the task row's own dedup
key for *every* `task.completed`, so container settlement — a review task that
acquired children completes through that path, not through session close —
carries the guard as well. Before that, settling a review container reopened
the recursion the session-close guard had already closed.

The `ensure_task` nodes below pin `profile_id` but no `intelligence_class`, so
the assignment-routing playbook chooses the class for the tasks they create. A
pinned profile is a compatibility constraint, not a route: until that decision
lands the task is held back with `awaiting_intelligence_route`. A project whose
reviewer profiles have a fixed class can skip that wait by adding
`"intelligence_class": "<class id>"` beside `profile_id` in its own copy of
this pipeline — it must match the profile's `default_class`, or no worker will
be compatible with the task.
```json
{
  "rules": [
    {
      "id": "per-task-review",
      "on": "task.completed",
      "when": {
        "all": [
          {"field": "event.task.branch_name", "truthy": true},
          {"field": "event.no_code", "truthy": false},
          {"field": "event.review_task", "truthy": false}
        ]
      },
      "entry": "create-review",
      "nodes": {
        "create-review": {
          "command": "ensure_task",
          "args": {
            "project_id": "{{event.project_id}}",
            "dedup_key": "review:task:{{event.task_id}}",
            "title": "Review: {{event.title}}",
            "description": "Reviewing task: {{event.task_id}}\nBranch: {{event.task.branch_name}}\nPR: {{event.task.pr_url}}\n\nRead the diff and either approve (close this task with a summary) or reject (call reopen_with_feedback on the reviewed task, then close this task).",
            "profile_id": "reviewer"
          },
          "output": {"as": "review"},
          "on_success": "link-discovered-from",
          "on_failure": "done"
        },
        "link-discovered-from": {
          "command": "add_dependency",
          "args": {
            "task_id": "{{outputs.review.task_id}}",
            "depends_on": "{{event.task_id}}",
            "dep_type": "discovered-from"
          },
          "on_success": "fetch-downstream",
          "on_failure": "done"
        },
        "fetch-downstream": {
          "command": "get_downstream_tasks",
          "args": {"task_id": "{{event.task_id}}"},
          "output": {"as": "downstream"},
          "on_success": "gate-downstream",
          "on_failure": "done"
        },
        "gate-downstream": {
          "command": "gate_create",
          "for_each": {"source": "outputs.downstream.tasks", "as": "dep"},
          "args": {
            "project_id": "{{event.project_id}}",
            "gate_type": "task",
            "title": "Awaiting review of {{event.task_id}}",
            "await_id": "{{outputs.review.task_id}}",
            "waiter_task_ids": ["{{outputs.dep.id}}"]
          },
          "on_success": "done",
          "on_failure": "done"
        },
        "done": {"terminal": true}
      }
    },
    {
      "id": "per-branch-final-review",
      "on": "task.completed",
      "when": {
        "all": [
          {"field": "event.task.branch_name", "truthy": true},
          {"field": "event.task.pr_url", "truthy": true},
          {"field": "event.no_code", "truthy": false},
          {"field": "event.review_task", "truthy": false}
        ]
      },
      "entry": "ensure-final",
      "nodes": {
        "ensure-final": {
          "command": "ensure_task",
          "args": {
            "project_id": "{{event.project_id}}",
            "dedup_key": "branch-review:{{event.task.branch_name}}",
            "title": "Final review: {{event.task.branch_name}}",
            "description": "Final review for branch {{event.task.branch_name}}. Runs after every per-task review approves. Merge authority.",
            "profile_id": "final-reviewer"
          },
          "output": {"as": "final"},
          "on_success": "ensure-review",
          "on_failure": "done"
        },
        "ensure-review": {
          "command": "ensure_task",
          "args": {
            "project_id": "{{event.project_id}}",
            "dedup_key": "review:task:{{event.task_id}}",
            "title": "Review: {{event.title}}",
            "description": "Reviewing task: {{event.task_id}}\nBranch: {{event.task.branch_name}}",
            "profile_id": "reviewer"
          },
          "output": {"as": "review"},
          "on_success": "link-blocks",
          "on_failure": "done"
        },
        "link-blocks": {
          "command": "add_dependency",
          "args": {
            "task_id": "{{outputs.final.task_id}}",
            "depends_on": "{{outputs.review.task_id}}",
            "dep_type": "blocks"
          },
          "on_success": "fetch-downstream-branch",
          "on_failure": "fetch-downstream-branch"
        },
        "fetch-downstream-branch": {
          "command": "get_downstream_tasks",
          "args": {"task_id": "{{event.task_id}}"},
          "output": {"as": "downstream"},
          "on_success": "gate-downstream-pr-merged",
          "on_failure": "done"
        },
        "gate-downstream-pr-merged": {
          "command": "gate_create",
          "for_each": {"source": "outputs.downstream.tasks", "as": "dep"},
          "args": {
            "project_id": "{{event.project_id}}",
            "gate_type": "pr-merged",
            "title": "Awaiting merge of {{event.task.branch_name}}",
            "await_id": "{{event.task.pr_url}}",
            "waiter_task_ids": ["{{outputs.dep.id}}"]
          },
          "on_success": "done",
          "on_failure": "done"
        },
        "done": {"terminal": true}
      }
    },
    {
      "id": "spec-ingest-on-approve",
      "on": "spec.approved",
      "entry": "spec_ingest_gate",
      "nodes": {
        "spec_ingest_gate": {
          "command": "ensure_task",
          "args": {
            "project_id": "{{event.project_id}}",
            "dedup_key": "spec-ingest:{{event.spec_path}}",
            "title": "Ingest spec {{event.spec_path}}",
            "description": "Read this spec, list existing tasks in the project, and emit task_batch_propose with the derived task graph. Iterate on validation errors.",
            "profile_id": "spec-ingest"
          },
          "on_success": "done",
          "on_failure": "done"
        },
        "done": {"terminal": true}
      }
    },
    {
      "id": "proposal-ready-gate",
      "on": "proposal.ready",
      "entry": "proposal_ready_gate",
      "nodes": {
        "proposal_ready_gate": {
          "command": "gate_create",
          "args": {
            "project_id": "{{event.project_id}}",
            "gate_type": "human",
            "title": "Approve task batch?",
            "question": "Approve proposal {{event.proposal_id}}?",
            "await_id": "{{event.proposal_id}}"
          },
          "on_success": "done",
          "on_failure": "done"
        },
        "done": {"terminal": true}
      }
    },
    {
      "id": "commit-on-gate-resolve",
      "on": "gate.resolved",
      "entry": "commit_proposal",
      "nodes": {
        "commit_proposal": {
          "command": "task_batch_commit",
          "args": {
            "proposal_id": "{{event.await_id}}"
          },
          "on_success": "done",
          "on_failure": "done"
        },
        "done": {"terminal": true}
      }
    }
  ]
}
```
