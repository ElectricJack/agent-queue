---
name: with-subtasks
description: A formula whose node declares a checklist
vars:
  branch: {required: true}
---
# With Subtasks

The `subtasks:` grammar reaches formulas unchanged — a formula is just an
`aq-graph` document with an `extends` chain and vars resolved.

```aq-graph
version: 1
parent:
  title: Ship {branch}
nodes:
  - key: ship
    title: Ship {branch}
    acceptance: ["merged"]
    subtasks:
      - "Rebase {branch}"
      - title: "Run the focused tests"
        context: "Only the files {branch} touches."
```
