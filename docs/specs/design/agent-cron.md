# Recurring agent prompts

AQ owns recurring prompts independently of the harness. `aq cron register`,
`show`, `list` and `cancel` dispatch through CommandHandler and expose the same
`cron_register`, `cron_get`, `cron_list`, `cron_cancel` agent tools.

A registration belongs to the authenticated live session instance, never a
caller-selected recipient. Named supervisors may have no project; their record
and message retain null project scope. Workers additionally bind to their held
task claim. These schedules do not exempt workers from inactivity/claim rules,
execute commands, change capabilities, or approve playbook gates.

Registration requires an idempotency key and prompt (at most 8,000 characters).
The same key and definition in the same session instance returns the existing
stable ID, including after reconnect or cancellation. A different definition
with that key is refused. Use a new key to replace a cancelled schedule. At most
10 active registrations per session instance are allowed.

Choose exactly one recurrence:

* `--every SECONDS` (60–604800), with `--offset SECONDS` in `[0, every)`.
  Ticks align to Unix epoch multiples: every 900, offset 120 fires at :02,
  :17, :32 and :47 UTC. Registration chooses the first strictly future tick.
* `--cron 'MINUTE HOUR * * *'`: minute is an integer 0–59, `*`, or `*/N`
  (1–59); hour is an integer 0–23 or `*`. No lists, ranges, names, seconds,
  month/day constraints or arbitrary cron expressions. `--timezone` is an
  IANA zone, default UTC. Search real UTC minutes, so nonexistent local times
  are skipped and repeated local times can fire twice during DST changes.

The daemon scans a bounded batch on its existing cycle. Persisted next-fire
times survive daemon restarts that adopt the same owner instance; replacement
or termination expires the record and archives its pending wake. New owner
instances must register again. No schedule cold-starts a replacement owner.

Missed ticks coalesce into one prompt. A schedule retains at most one pending
message, with a stable message ID per emitted tick. Cron coalescing counts are a
diagnostic lower bound after outages exceeding seven days; recurrence advances
directly past every missed tick. Pending prompts expire after
one hour and have at most five terminal submission attempts, at least 30 seconds
apart with exponential backoff. Busy/absent owners consume no attempt. The
normal message engine and session lens perform idle detection and terminal
submission. A one-line wake points to `aq cron show ID --consume --json`, which
returns the prompt and consumes only that schedule's pending notification.
Reading/listing without consume has no delivery side effect. New ticks never
replay every missed interval. Submission is at least once under an ambiguous
terminal crash; consumers should make patrol actions idempotent.

Show/list expose timezone, epoch and local next-fire, coalesced count, pending
message/expiry, attempt count, and last submission time/status/error. Show also
returns the latest message delivery/read receipt. Exhausted delivery produces
a daemon warning alongside the persisted failure. Cancellation archives pending delivery. An already authorized
in-flight terminal submission may finish; cancellation cannot undo execution.
Delivered prompts may be handled while the session remains busy; later ticks
wait through the ordinary activity gate. Schedule expiry retains history.

Fresh profiles grant the four public commands on supported harnesses. Supervisor
capability sync adds shipped grants on reload; worker templates and skills remain
write-if-absent and operators inspect installed drift. Existing `aq wait`
conditions and reviewed playbook artifacts are unchanged.
