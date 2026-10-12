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
Recording session sleep intent or transitioning to sleeping cancels the current
instance's active registrations and archives undelivered scheduled prompts atomically
with the session update. Waking does not restore cancelled registrations; cancelled idempotency keys remain cancelled.
