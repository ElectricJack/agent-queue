---
id: default-pipeline
kind: pipeline
role: default-pipeline
scope: system
triggers:
  - task.created
  - task.completed
  - spec.approved
  - proposal.ready
  - event_type: gate.resolved
    filter:
      gate_type: human
---

# Default Pipeline

The system default pipeline. Reacts to task lifecycle events.

Ships three rules:

- **Routing** (`task.created`) — attaches a routing gate to every new task and
  coalesces work by ensuring an open triage task per project.
- **Per-task review** (`task.completed`) — on every `task.completed` whose task
  has a `branch_name`, spawns one reviewer task with a `discovered-from` edge
  to the reviewed task and attaches a `task` gate to each downstream dependent
  so nothing downstream runs until the review completes.
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
- **Worker-filed triage** (`task.created`, session-filed root tasks only) —
  routes a task a pool worker filed for itself (no `parent_id`) straight to
  the filer's own profile via `task_route`, resolving the routing gate the
  worker-filing constraint attached. Projects that want different triage for
  worker-filed work override this rule.

```json
{
  "rules": [
    {
      "id": "task-created-routing",
      "on": "task.created",
      "entry": "attach_routing_gate",
      "nodes": {
        "attach_routing_gate": {
          "command": "gate_create",
          "args": {
            "project_id": "{{event.project_id}}",
            "gate_type": "routing",
            "title": "Route task",
            "question": "Assign profile + intelligence class (+ workspace if profile needs one).",
            "waiter_task_ids": ["{{event.task_id}}"]
          },
          "on_success": "ensure_triage_task",
          "on_failure": "done"
        },
        "ensure_triage_task": {
          "command": "ensure_task",
          "args": {
            "project_id": "{{event.project_id}}",
            "dedup_key": "triage-open",
            "title": "Triage unrouted tasks",
            "description": "Route every unrouted task in this project via `task_route`. Close this task when the queue is empty.",
            "profile_id": "triage",
            "priority": 1
          },
          "on_success": "done",
          "on_failure": "done"
        },
        "done": {"terminal": true}
      }
    },
    {
      "id": "per-task-review",
      "on": "task.completed",
      "when": {"field": "event.task.branch_name", "truthy": true},
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
          {"field": "event.task.pr_url", "truthy": true}
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
    },
    {
      "id": "worker-filed-triage",
      "on": "task.created",
      "when": {
        "all": [
          {"field": "event.created_by_kind", "equals": "session"},
          {"field": "event.parent_task_id", "is_null": true}
        ]
      },
      "entry": "route",
      "nodes": {
        "route": {
          "command": "task_route",
          "args": {
            "task_id": "{{event.task_id}}",
            "profile_id": "{{event.filed_by_profile_id}}"
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
