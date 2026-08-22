---
id: default-pipeline
kind: pipeline
role: default-pipeline
scope: system
triggers:
  - task.created
---

# Default Pipeline

Every task is born unrouted. On `task.created` this pipeline attaches a routing
gate to the new task (blocking it from READY) and coalesces work by ensuring
an open triage task per project. The triage agent resolves the routing gate
via `task_route`.

```json
{
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
        "description": "Route every unrouted task in this project via `task_route`. Close this task when the queue is empty."
      },
      "on_success": "done",
      "on_failure": "done"
    },
    "done": {"terminal": true}
  }
}
```
