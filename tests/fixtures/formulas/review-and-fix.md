---
name: review-and-fix
description: Review a branch, then fix any findings
extends: base-review
vars:
  branch: {required: true}
  fixer: {default: coding, enum: [reviewer, coding]}
---
# Review And Fix

Review the branch, then fix anything the review flags.

```aq-graph
version: 1
parent:
  title: Review and fix {branch}
nodes:
  - key: review
    title: Review branch {branch} (strict)
  - key: fix
    title: Fix findings on {branch}
    needs: [review]
    profile: "{fixer}"
    intelligence_class: standard-medium
```
