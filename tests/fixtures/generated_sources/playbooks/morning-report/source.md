---
id: morning-report
name: Morning report
version: 1
scope: system
enabled: false
triggers:
  - report.morning_due
---

# Morning report

An optional, operator-activated system policy. Once per local day, when the
zoned morning time has passed, the daemon emits `report.morning_due`. Its event
id names the schedule and the local date, so a restart or a repeated emission
deduplicates to the day's one run. That run reconciles the zoned daily schedule,
frozen overnight evidence, one supervisor author request and the deadline
fallback through `morning_report_tick`, and keeps calling it a minute apart
until the day's report settles. The command owns reservation, dedup, visibility,
suppression, recovery and immutable finalization. The playbook holds no state
and makes no model call. It creates no worker task.

The schedule is opt-in through `reports.morning.enabled`. Authored reports also
require `reports.morning.full_fleet_visibility: true` and an empty project
selection, explicitly declaring that the destination can see the full fleet.
Restricted destinations receive deterministic scoped content with no author wake.
The full report is readable at `/reports/:id` before any delivery link is emitted.
Discord delivery is daemon-owned and transport retries never create another
author request. Config disable retains readable content and cancels unsent work.

## Rule: reconcile-morning

1. Call `morning_report_tick` with no arguments and bind its result as `report`.
   The `completed` outcome continues at step 2. A `rejected` or `runtime_error`
   outcome waits at step 4 for one retry.
2. If `report.report_id` is the event's `report_id` and `report.state` is still
   pending (`building`, `failed`, `ready` or `authoring`), wait at step 3.
   Otherwise end successfully: a final or suppressed report, a late-start skip,
   a disabled schedule, a timezone guard or another day's report is a normal
   result, and the next day's run owns the next report.
3. Wait 60 seconds on a durable timer, then call step 1 again.
4. Wait 60 seconds on a durable timer before retrying a failed call.
5. Call `morning_report_tick` again and bind its result as `retried`. The
   `completed` outcome continues at step 6. A second `rejected` or
   `runtime_error` outcome fails the rule.
6. If `retried` is still pending for the event's `report_id`, as in step 2,
   wait at step 3. Otherwise end successfully.

## Failure handling, uniformly

A failed call is retried once after a minute; a second failure reaches a failed
terminal, and the next day's run recovers durable reservations, requests and
final reports. A timer that cannot be armed also fails the rule. A lost event,
restart, duplicate emission or time edit cannot create another same-day run or
author turn. No automatic activation or readiness requirement is added by
shipping this policy.
