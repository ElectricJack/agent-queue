---
id: default-assignment-routing
kind: assignment-routing
role: assignment-routing
profile_id: playbook-compiler
scope: system
triggers:
  - assignment.route.requested
max_tokens: 4096
llm_config:
  intelligence_class: fast-low
---

# Default assignment routing

> This file is already a **prose authoring source**: it carries no embedded
> JSON action graph, so Package 6's rewrite left its instructions untouched.
> Its reviewed V2 artifact lives in `tests/fixtures/playbooks/v2/default-assignment-routing/`.

Choose the least expensive intelligence class that can reliably complete each
task. Use the title, description, task type, constraints, and available options.
The supplied options are binding: a task pinned to a profile may have its
catalog narrowed to that profile's fixed intelligence class, so never choose a
class that is absent from this batch's options.

Prefer a fast, low-reasoning class for routine, localized, well-specified work.
Use a standard class when the task needs several coordinated edits, debugging,
or judgment across modules. Reserve deep classes for ambiguous architecture,
high-risk changes, hard investigations, and work whose failure would be costly.

Leave provider null unless the supplied AQ availability data gives a concrete
reason to pin it. Temporary worker occupancy is not a reason to change the
required intelligence class.

Return one decision for every supplied task and no others. Copy each task's
`task_id` and `input_hash` exactly. Choose `intelligence_class` and optional
`provider` only from the supplied options. Give a concise, non-empty `reason`.
Return exactly one JSON object with this shape and no extra fields:

```json
{"decisions":[{"task_id":"<task_id>","input_hash":"<input_hash>","intelligence_class":"<available class>","provider":null,"reason":"<why this class fits>"}]}
```
