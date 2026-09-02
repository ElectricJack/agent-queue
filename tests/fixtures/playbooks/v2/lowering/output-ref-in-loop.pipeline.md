---
id: output-ref-in-loop
kind: pipeline
role: default-pipeline
scope: system
triggers: [task.completed]
---
```json
{"rules":[{"id":"route","on":"task.completed","entry":"fetch","nodes":{"fetch":{"command":"get_downstream_tasks","args":{"task_id":"{{event.task_id}}"},"output":{"as":"downstream"},"on_success":"gate","on_failure":"done"},"gate":{"command":"gate_create","for_each":{"source":"{{outputs.downstream.tasks}}","as":"dep"},"args":{"project_id":"{{event.project_id}}","gate_type":"task","title":"{{outputs.review.task_id}}","waiter_task_ids":["{{outputs.dep.id}}"]},"on_success":"done","on_failure":"done"},"done":{"terminal":true}}}]}
```
