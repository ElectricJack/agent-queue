---
name: phases-invalid
description: A phased formula that also fails validation, for ordering tests
---
# Phases, Invalid

Used to pin that `graph.phases_need_root` is decided **before** validation:
the node names a profile that does not exist, so cooking it at the root
reports `unknown_profile`.

```aq-graph
version: 1
parent:
  title: Epic
phases:
  - key: one
    title: Phase 1
nodes:
  - key: solo
    title: Solo
    acceptance: ["x"]
    phase: one
    profile: ghost-profile
```
