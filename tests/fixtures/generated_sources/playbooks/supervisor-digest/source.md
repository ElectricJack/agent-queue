---
id: supervisor-digest
name: Supervisor digest
version: 1
scope: system
enabled: false
triggers:
  - timer.1m
---

# Supervisor digest

An optional, operator-activated system policy. Every minute it reconciles the
digest windows the daemon is holding for the supervisor and hands each one to the
supervisor's inbox through `digest_request`. The command owns reservation,
dedup, cadence, quiet hours, the "nothing to report" rule, the once-a-day quiet
line, the deadline fallback and delivery. The playbook holds no state and makes
no model call. It creates no worker task.

The schedule is opt-in through `discord.digest.supervisor_authored`; with that
flag off the deterministic digest posts exactly as it does today and this policy
has nothing to reconcile. Each held window carries a frozen brief, a deadline and
a deterministic fallback. A window posts once: either the supervisor's
`aq digest post` or the fallback at the deadline, never both and never an edit.
The full report stays readable on the dashboard; delivery is daemon-owned and a
transport failure never creates another author turn.

## Rule: reconcile-digest

1. Call `digest_request` with no arguments and bind its result as `windows`. The
   `completed` outcome ends successfully: no held window, a quiet fleet, before
   the cadence, quiet hours, or a disabled flag is a normal result. A `rejected`
   or `runtime_error` outcome fails the rule.

## Failure handling, uniformly

A failed command reaches a failed terminal. There is no in-run retry: the next
minute recovers durable windows. A lost event, restart, duplicate tick or time
edit cannot create another same-window author turn. No automatic activation or
readiness requirement is added by shipping this policy.