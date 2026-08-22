---
id: default-pipeline
kind: pipeline
role: default-pipeline
scope: system
triggers:
  - task.created
  - task.completed
---

# Default Pipeline

The system default pipeline. Reacts to task lifecycle events.

Ships two rules:

- **Routing** (`task.created`) — attaches a routing gate to every new task and
  coalesces work by ensuring an open triage task per project.
- **Per-task review** (`task.completed`) — on every `task.completed` whose task
  has a `branch_name`, spawns one reviewer task with a `discovered-from` edge
  to the reviewed task and attaches a `task` gate to each downstream dependent
  so nothing downstream runs until the review completes.

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
            "profile_id": "triage"
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
    }
  ]
}
```
