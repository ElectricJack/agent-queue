# Project-owned one-shot schedules

Project schedules queue one durable supervisor notification at an absolute UTC
instant, including weeks or months in the future. They are independent of task
status, worker claims, slots, sessions, continuations and the verified-wait feature
flags. Existing `timer` waits retain their one-second to one-day bounds.

`schedule_register`, `schedule_get`, `schedule_list` and `schedule_cancel` go
through CommandHandler. An operator or elevated supervisor selects an explicit
project; a project supervisor cannot access another project's schedules. Ordinary
workers cannot create schedules. Registration requires a nonblank body, a future
offset-aware ISO instant and a project-scoped idempotency key. Equivalent instants
normalise to UTC. Reusing a key returns the existing schedule, including after
firing/cancellation; changing its instant or body is a conflict. There is no
upper horizon. A new key is required to replace a cancelled schedule.

PostgreSQL retains pending, fired and cancelled rows across deployments and
daemon restarts. A separate daemon service scans indexed due rows every 30 seconds
and catches up overdue rows after downtime. Row locks serialize firing and
cancellation. In one transaction firing inserts a supervisor inbox message using
the existing message/source-event path and marks the schedule fired with its
message id. Rollback retains a pending schedule and no message; commit retains
exactly one message and a terminal receipt. Concurrent scans skip locked rows.
Cancellation is idempotent; if it wins the lock before firing, no message can be
queued. A fired notification cannot be recalled by cancelling its schedule.

The notification addresses the project's logical supervisor mailbox, following
the existing global-supervisor fallback for projects without an enabled
supervisor. The durable inbox owns delivery and retries. Exactly once means one
queued message, not guaranteed human action or exactly one terminal nudge.
Schedules never automatically reopen tasks or override task gates; the supervisor
can inspect current evidence and reopen using the normal commands when notified.

Get/list expose due time, state, registration time, cancellation/firing time and
message id. List supports bounded pagination and optional state filtering.
The internal reconciliation command is daemon-service-only and excluded from
HTTP, MCP and CLI surfaces. It runs independently of scheduler/Git work.

Acceptance checks cover a 30-day schedule surviving engine/service replacement,
catch-up and repeated/concurrent firing, transaction rollback, cancellation and
the firing race, project/worker/service authority, registration retries and key
conflicts, CLI/contracts discovery, and unchanged short-timer bounds.
