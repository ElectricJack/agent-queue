# Long-horizon supervisor notifications

Use project schedules for an observation deadline days, weeks or months away.
They queue one durable message for the project's supervisor and remain registered
when the author finishes a task, a supervisor session is replaced, or AQ restarts.
They retain no worker, claim, workspace or continuation. Operators and elevated
supervisors manage schedules; a project supervisor stays within its project.

```bash
aq schedule register --project-id agent-queue \
  --due-at 2026-11-23T16:20:12Z \
  --idempotency-key discord-ask-compatibility-exit-2026-11-23 \
  --body 'Observe the 30-day Discord ask compatibility exit for vivid-pinnacle-60. Inspect cutover evidence and reopen the task through the normal task commands if follow-up is required.'
aq schedule list --project-id agent-queue --state pending --json
aq schedule get --project-id agent-queue --schedule-id sched-ID --json
aq schedule cancel --project-id agent-queue --schedule-id sched-ID
```

The example is a post-deployment registration command; documentation does not
register it. Workers should report the deadline to their supervisor and finish
normally. Scheduling a notification does not reopen a task automatically.

`due-at` accepts an ISO instant with `Z` or an explicit UTC offset and normalises
to UTC. New schedules must be in the future, with no upper horizon. Retrying the
same project/key/body/instant returns the same schedule, including after firing
or cancellation. Reusing a key with different content is rejected. Use a new key
to replace a cancelled schedule.

Get/list return `pending`, `fired`, or `cancelled`, the UTC due time and terminal
receipt. A fired schedule includes its durable `message_id`; use `aq message
status` to inspect delivery. List accepts `--limit` (up to 100), `--offset`, and
`--state`. Terminal receipts remain available and prevent repeat firing.

The daemon scans every 30 seconds independently of scheduler and Git operations.
Overdue schedules catch up after restart. A transaction queues exactly one inbox
message and records its firing; the existing inbox delivery service retries
notification delivery. If the project's supervisor is disabled, AQ follows its
normal global-supervisor routing. With message delivery disabled, the queued
message remains durable until delivery is enabled or the inbox is read.

Cancellation prevents firing if it wins the schedule row lock first. Cancelling
an already fired schedule returns its firing receipt and does not recall its
message. No periodic registration or worker polling is needed.

This requires the `project_schedules` migration (operator deployment/upgrade).
Shipped supervisor capabilities are merged additively on daemon start/reload
unless the installed profile opts out with `capability_sync: false`. Operators
with a custom profile can grant `schedule_register`, `schedule_get`,
`schedule_list`, and `schedule_cancel`.

Short `aq wait register --kind timer` waits keep their one-day limit and their
existing claim/continuation lifecycle. Session-owned `aq cron` prompts retain
their session lifetime; see [agent cron](agent-cron.md).
