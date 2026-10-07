# Recurring prompt wake-ups

AQ schedules prompts for the authenticated session on Codex, Claude and other
harnesses. The daemon queues messages and wakes idle sessions through the normal
delivery engine. Registration does not run commands or grant permissions.

```bash
aq cron register --every 900 --offset 120 --idempotency-key patrol-v1 \
  --prompt 'Run the authorized patrol and handle findings.'
aq cron list --json
aq cron show SCHEDULE_ID --json
aq cron cancel SCHEDULE_ID
```

An interval is 60–604800 seconds. Offset is seconds from each Unix epoch interval
boundary and must be smaller than the interval. `900/120` fires at :02, :17, :32
and :47 UTC. Registration picks the first future boundary, even when registered
exactly at a boundary. Identical registration with the same key returns the same
ID; changing its definition is refused. Cancellation is permanent for that key;
use a new key to create another schedule. Ten active schedules per session are allowed.

For local wall-clock scheduling:

```bash
aq cron register --cron '15 9 * * *' --timezone America/Los_Angeles \
  --idempotency-key morning-v1 --prompt 'Run the authorized morning check.'
aq cron register --cron '*/15 * * * *' --idempotency-key quarter-hour-v1 \
  --prompt 'Run the authorized check.'
```

Only `MINUTE HOUR * * *` is supported. Minute accepts 0–59, `*`, or `*/N`
(N=1–59); hour accepts 0–23 or `*`. Lists, ranges, names, and day/month constraints
are refused. Timezone defaults to UTC and accepts IANA names. Missing local DST
times are skipped; repeated local times can fire twice. Intervals use elapsed
epoch time regardless of the display timezone. Show/list include the timezone,
next-fire epoch, UTC time and local time with its UTC offset.

Every registration belongs to a live session ID and instance token. Global
supervisors can leave project unset; no schedule can nominate another recipient.
A daemon restart that adopts the same session retains the schedule and timing.
Ending or replacing the owner instance expires its schedules. A new supervisor
session must register its patrol again. Workers also bind to their current task
claim; claim turnover expires their schedules. Recurrence grants no inactivity
exemption: use [one-shot waits](agent-waits.md) to retain a worker claim while idle.

Missed ticks coalesce into one pending prompt per schedule. No backlog replays
every missed tick after restart. Busy owners wait; absent/sleeping owners are
never cold-started by cron. Cron coalescing counts are a lower bound after outages
over seven days; interval counts are exact. Pending delivery expires after one hour. There are
five terminal submission attempts, with 30/60/120/240/480 second backoff, and
attempts/status/errors persist through daemon restart. A later future tick may
try a fresh prompt after failure. Show includes pending message ID/expiry,
delivery receipt (queued versus delivered versus read), coalescing count and
last-delivery diagnostics. Prompt handling should be idempotent because an
ambiguous crash after terminal submission can cause a retry.

The wake pointer is `aq cron show ID --consume --json`. This returns the prompt
and consumes only its pending notification. Plain show/list are reads. Cancelling
archives pending wakes; an already authorized in-flight submission may finish.
Existing `aq wait` subscriptions and reviewed playbook gates keep their semantics.

The supervisor startup patrol uses one stable `supervisor-patrol-v1` key,
`--every 900 --offset 120`, and a prompt to run
`python3 ~/.agent-queue/operator-checks/stall-sweep.py`, poll the default inbox,
`profile:supervisor`, and its own session inbox with `--inject`, then handle
findings with the authorized stall actions. It works with no project selected.
Use `cron_register`, `cron_get`, `cron_list`, `cron_cancel` as equivalent agent tools.

Fresh supervisor and worker templates grant these commands. Supervisor capability
sync merges new grants on reload. Existing worker profiles and skills remain
write-if-absent; operators inspect `aq doctor --check profiles.system_drift` and
`aq doctor --check skills.installed_drift`, merge profile grants using
`aq agent profile-reseed --profile-id ID --grants-only`, and repair installed skill
drift with `aq doctor --check skills.installed_drift --fix`. Code deployment also
requires the operator's normal `aq db upgrade` for revision `a00000000086`;
worker sessions never migrate the operator database.
