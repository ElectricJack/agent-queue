---
id: fast-smooth-train
name: Fast smooth train
version: 1
scope: system
enabled: false
triggers:
  - integration.readiness_changed
description: Git-first train dispatch and repair policy
---
# Fast smooth train

Copy this policy into the project's playbooks and activate its compiled artifact.
Disabling this project activation stops admission and fenced train writes. Workers
continue publishing source branches. Re-enable to resume from Git. Existing batch
members and candidates survive either operation.

When idle, merge all ready inputs. While busy, accumulate the next batch. Repair
red checks or conflicts in the existing batch with its retained agent. Unknown
facts and an explicit policy wait revisit after `wait_seconds`; they never create
a human gate. Exact green publishes subject to human holds and live writer fences.
The parent has no extra CI gate by default. Set `required_checks` to the project's
trusted names, producer and version to override the repository check settings;
null inherits those settings. Changes apply to pending candidates immediately.

```integration-policy
{"version":1,"max_wait_seconds":3600,"tables":{},"train":{"idle":"merge","red":"repair","conflict":"repair","wait_seconds":30,"required_checks":null}}
```

## Rule: observe-policy
On `integration.readiness_changed`, finish with `completed`. The level-triggered
Git train reads this policy on every visit, so a lost event cannot lose progress.
