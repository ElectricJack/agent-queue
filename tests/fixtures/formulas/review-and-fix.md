---
name: review-and-fix
description: Review a branch, then fix any findings
extends: base-review
vars:
  branch: {required: true}
---
# Review And Fix

Review the branch, then fix anything the review flags.

```aq-graph
version: 1
nodes:
  - key: review
    title: Review {{branch}}
  - key: fix
    title: Fix findings
    needs: [review]
```
