---
id: default-assignment-routing
kind: assignment-routing
role: assignment-routing
scope: system
triggers:
  - assignment.route.requested
max_tokens: 1024
---

# Default assignment routing

Choose the least expensive intelligence class that can reliably complete each
task. Use the title, description, task type, constraints, and available options.

Prefer a fast, low-reasoning class for routine, localized, well-specified work.
Use a standard class when the task needs several coordinated edits, debugging,
or judgment across modules. Reserve deep classes for ambiguous architecture,
high-risk changes, hard investigations, and work whose failure would be costly.

Leave provider null unless the supplied AQ availability data gives a concrete
reason to pin it. Temporary worker occupancy is not a reason to change the
required intelligence class.
