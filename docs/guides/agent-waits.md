---
tags: [guide, agents, waits]
---

# Durable agent waits

A wait records one bounded condition for a task or a named supervisor. It
keeps the task `IN_PROGRESS` and retains its claim, workspace and pool seat.
Registration returns immediately. Task, message and timer adapters are available;
job registration returns `wait.adapter_unavailable` until the job adapter lands.

```bash
aq wait register --kind task --ref other-task --timeout 7200 --idempotency-key review-result
aq wait register --kind message --ref thread-id --after-seq 42 --idempotency-key reply
aq wait register --kind timer --due-at 1800000060 --timeout 120 --idempotency-key reminder
aq wait show WAIT_ID --json
aq wait list --json
aq wait cancel WAIT_ID
```

`--due-at` is UTC epoch seconds. A deadline is mandatory internally: omitted
`--timeout` defaults to two hours; the maximum is 24 hours. Timer due times
must fall at or before the deadline. A message wait requires an existing thread
the owner participates in, and matches only incoming messages addressed to that
owner with a server `created_seq` greater than `--after-seq`. Reading, delivering
or archiving a message cannot consume the condition. Message send/status/inbox
responses expose `created_seq` for choosing the cursor.

Owners come from the authenticated session. Pool mutations use the epoch read
from `.aq/claim.json`, with an explicit `--claim-epoch` override available. Workers
cannot choose another owner or a target in another project. Each task claim has
at most one active blocking wait. Named supervisors can register non-blocking
subscriptions within their project, with at most 100 active subscriptions per
project. Results remain readable when a task acquires a new claim; mutations and
lease exemptions never follow an old epoch into a new claim.

The daemon scans at most 100 rows per cycle, rotates past unresolved conditions,
and reads durable producer state, including archived tasks. It preserves actual
task status and close outcome. Completion at or before the deadline wins even if
observed later. Missing sources yield `source_unavailable`; deadlines yield an
explicit `expired` result. Neither expiry nor cancellation changes the task or
cancels its producer. Claim turnover or manual pause cancels the old exemption,
retains its result, and never unpauses the task.

Resolution uses a version CAS and queues one result message with identity
`wait:<wait_id>:result`. A failed message insertion leaves the resolved row as
durable outbox intent for retry. Result digests are bounded to 4 KiB, with large
results read via their reference. `resolved_at` and `wait_resumed_at` record the
daemon's observation time for the lease consumers' fresh consumption baseline.
The shared query `blocking_wait_for(session, claim_epoch, now)` verifies the live
claim, bounded deadline and producer, without writing fabricated activity.

This first implementation supplies the persistence, commands, result messages
and shared lease decision. Wiring all inactivity/claim consumers and rendering
the `wait_result` nudge as `aq wait show` is the second implementation task. Until
that wiring lands, registration alone does not suspend existing stall handling.
Do not rely on live-idle dormancy before the second task is delivered.

## Installation and rollback

The operator upgrades through `aq db upgrade`. Workers must never migrate the
operator database. Revision `a00000000029` conditionally adds the wait table and
message sequence, including existing messages. The downgrade deliberately retains
wait/result history. Before rolling back code, stop new registrations, cancel
active waits through the command boundary, and restore ordinary lease baselines.
Never discard active exemptions without handling their results.

Worker template grants include `wait_register`, `wait_get`, `wait_list`, and
`wait_cancel`. Installed templates are write-if-absent; operators check drift with
`aq doctor --check profiles.system_drift` and merge grants without replacing their
edits:

```bash
aq agent list-profiles
aq agent profile-reseed --profile-id PROFILE_ID --grants-only
```
