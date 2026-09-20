---
name: with-phases
description: A formula whose nodes are filed into ordered phases
vars:
  branch: {required: true}
---
# With Phases

The `phases:` grammar reaches formulas unchanged — a formula is just an
`aq-graph` document with an `extends` chain and vars resolved.

```aq-graph
version: 1
parent:
  title: Ship {branch}
phases:
  - key: build
    title: "Phase 1 — build {branch}"
    label: build
  - key: verify
    title: "Phase 2 — verify"
nodes:
  - key: compile
    title: Compile {branch}
    acceptance: ["it builds"]
    phase: build
  - key: check
    title: Check {branch}
    acceptance: ["it passes"]
    phase: verify
```
