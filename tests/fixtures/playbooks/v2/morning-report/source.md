---
id: morning-report
name: Morning report
version: 1
scope: system
enabled: false
triggers:
  - timer.1m
---

# Morning report

An optional, operator-activated system policy. Every minute it reconciles the
zoned daily schedule, frozen overnight evidence, one supervisor author request
and the deadline fallback through `morning_report_tick`. The command owns
reservation, dedup, visibility, suppression, recovery and immutable finalization.
The playbook holds no state and makes no model call. It creates no worker task.

The schedule is opt-in through `reports.morning.enabled`. Authored reports also
require `reports.morning.full_fleet_visibility: true` and an empty project
selection, explicitly declaring that the destination can see the full fleet.
Restricted destinations receive deterministic scoped content with no author wake.
The full report is readable at `/reports/:id` before any delivery link is emitted.
Discord delivery is daemon-owned and transport retries never create another
author request. Config disable retains readable content and cancels unsent work.

## Rule: reconcile-morning

1. Call `morning_report_tick` with no arguments and bind its result as `report`.
   The `completed` outcome ends successfully: a quiet day, before schedule,
   authoring, deadline fallback, late-start skip or disabled schedule is a
   normal result. A `rejected` or `runtime_error` outcome fails the rule.

## Failure handling, uniformly

A failed command reaches a failed terminal. There is no in-run retry: the next
minute recovers durable reservations, requests and final reports. A lost event,
restart, duplicate tick or time edit cannot create another same-day author turn.
No automatic activation or readiness requirement is added by shipping this policy.
