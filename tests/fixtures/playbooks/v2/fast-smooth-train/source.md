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

## Bounded selection contract

Packing is disabled by default. An absent `packing` block retains legacy selection;
missing or mixed signature versions retain safe selection and full candidate checks.
`signature_version: 1` and `policy_version: 1` identify the shared contract. The
immutable activated artifact SHA is `policy_revision`: changing weights changes
that revision and invalidates signature/cache evidence. Versions alone are not
proof of identical policy. Graph signatures remain observational until suite cutover.

When explicitly enabled, packing defaults to four members, with a hard maximum of
eight including dependencies. Rank only the bounded eligible window. Exact-head
green earns 100, merge-clean 40, small signatures and nonhot paths each 0..10,
age 1 per hour capped at 80, and rolling worker/profile pass rate 0..10 (neutral
5 until 20 observed checks). Hotness uses the last 50 landed batches. Reserve one
admissible slot for the oldest candidate older than two hours, then score the
remainder; ties use oldest ready then task ID. Admission, dependency closure,
cumulative merge-tree probes, identity rechecks and exact candidate CI remain
mandatory. Oversized closures use ordinary delivery. No LLM or online learning.

Tuning is bounded: green/clean weights and age cap 0..1000; size/nonhot/pass-rate
weights and neutral score 0..100; age rate 0..100 per hour; minimum observations
20..10000; hotness window 1..1000 landed batches; oldest-slot threshold 1..7200
seconds. Neutral score cannot exceed the pass-rate weight. Unknown fields,
coerced strings/booleans, nonfinite values and unsupported versions are rejected.

Bisection is disabled by default and permits at most three probe candidates per
incident. Its future consumer must retain baseline comparison, exact-candidate
evidence, dependency-closed groups and ordinary repair on ambiguity or exhaustion.
`parallel_disjoint` is disabled by default. It is mutually exclusive with the
project's legacy `train.speculative_ci`; the loaded policy refuses that combination.
Enabling it never bypasses capacity admission, the sole owning batch or full checks.
These flags declare policy for the later mechanisms; they do not implement them.

Shipping this template does not activate it. Preserve installed project source and
activation until an operator explicitly imports and activates an approved artifact.
The daemon-owned reviewed bundle may refresh on upgrade; that is not project
activation and does not authorize replacing an installed policy.

```integration-policy
{
  "version": 1,
  "max_wait_seconds": 3600,
  "tables": {},
  "train": {
    "idle": "merge",
    "red": "repair",
    "conflict": "repair",
    "wait_seconds": 30,
    "required_checks": null,
    "packing": {
      "signature_version": 1,
      "policy_version": 1,
      "enabled": false,
      "max_members": 4,
      "green_weight": 100,
      "merge_clean_weight": 40,
      "signature_size_weight": 10,
      "nonhot_weight": 10,
      "age_per_hour": 1.0,
      "max_age_score": 80,
      "pass_rate_weight": 10,
      "pass_rate_neutral": 5,
      "pass_rate_min_checks": 20,
      "hotness_window_batches": 50,
      "oldest_slot_after_seconds": 7200
    },
    "bisection": {
      "enabled": false,
      "max_probes": 3
    },
    "parallel_disjoint": false
  }
}
```

## Rule: observe-policy
On `integration.readiness_changed`, finish with `completed`. The level-triggered
Git train reads this policy on every visit, so a lost event cannot lose progress.
