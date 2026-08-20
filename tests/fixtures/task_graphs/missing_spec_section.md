---
tags: [spec, project]
project: p1
---

# Partial Spec

## 1. Problem

Only one section exists here.

```aq-graph
version: 1
parent: { title: "Partial" }
nodes:
  - key: a
    title: A node pointing at a heading that does not exist
    acceptance: ["done"]
    context: [{ type: spec_ref, section: "9. Nonexistent" }]
```
