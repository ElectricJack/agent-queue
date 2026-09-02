---
name: base-review
description: Review a branch
vars:
  branch: {required: true}
  reviewer: {default: reviewer, enum: [reviewer, coding]}
---
# Base Review

Review the branch for correctness.

```aq-graph
version: 1
defaults:
  profile: "{reviewer}"
  intelligence_class: standard-low
nodes:
  - key: review
    title: Review {branch}
    acceptance: ["findings written"]
```
