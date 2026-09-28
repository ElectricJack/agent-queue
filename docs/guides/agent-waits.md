---
tags: [guide, agents, waits]
---

# Durable agent waits

A wait records one bounded condition for a task or a named supervisor. It
keeps the task `IN_PROGRESS` and retains its claim, workspace and pool seat.
Registration returns immediately. Job, task, message and timer adapters are available.
Managed job admission remains opt-in with `resources.jobs.enabled: true`.

```bash
aq job submit --preset test --wait --idempotency-key validation -- tests/test_agent_waits.py
aq test --aq-detach --aq-wait --aq-idempotency-key validation-tests tests/test_agent_waits.py
aq wait register --kind job --ref JOB_ID --idempotency-key existing-job
aq wait register --kind task --ref other-task --timeout 7200 --idempotency-key review-result
aq wait register --kind message --ref thread-id --after-seq 42 --idempotency-key reply
aq wait register --kind timer --due-at 1800000060 --timeout 120 --idempotency-key reminder
aq wait show WAIT_ID --json
aq wait list --json
aq wait cancel WAIT_ID
```

To wait for a reply to a message you send, choose a thread ID and send as your
session. The returned message ID identifies the message; it is not the thread ID.
Use the sent message's `created_seq` as the exclusive cursor:

```bash
aq message send --to user:dashboard --project "$AQ_PROJECT_ID" \
  --from-kind session --from-id "$AQ_SESSION_ID" \
  --thread-id "request-$AQ_SESSION_ID" --body "Please reply here." --json
# Read data.message.thread_id and data.message.created_seq from the response.
aq wait register --kind message --ref "request-$AQ_SESSION_ID" \
  --after-seq <created_seq> --timeout 3600 --idempotency-key reply
```

The reply must stay on that thread, for example through
`aq message reply <message-id>` or
`aq agent message <task-id> BODY --reply-to <message-id>`.
For a collaboration thread, use its `collab-*` ID and the last seen thread
sequence instead. Omitting `--after-seq` is a usage error for message waits.

Job submission with `--wait` commits the job, workspace pin and blocking wait
in one transaction. An existing active wait rejects the submission without
leaving a job or reservation behind. Repeating the same submission key returns
the same job and wait; changing arguments or the wait option is refused. The CLI
prints its key before contacting the daemon, so an ambiguous response can be
retried with that key. It never starts a local fallback. `aq run --preset ...`
is an alias for the same submit command. Synchronous `aq test` keeps its existing
exit-code behavior; only `--aq-detach` submits to the queue.

`--due-at` is UTC epoch seconds. A deadline is mandatory internally: omitted
`--timeout` defaults to two hours for task/message/timer waits. A job wait defaults
to its remaining queue budget plus run budget plus 300 seconds, or the remaining
run budget plus grace after it starts. All deadlines are capped at 24 hours. Timer due times
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
task status and close outcome. Due timers receive the same scan priority as
waits past their hard deadline, without waiting for the timer's later timeout.
Completion at or before the deadline wins even if
observed later. Job waits return the actual terminal state, outcome, exit code, infrastructure
reason and a bounded failure-first excerpt with a `job:ID` result reference.
Read the full immutable result with `aq job result ID`; read retained output with
`aq job logs ID`. Workers may wait only on their own task's jobs, while named
supervisors may subscribe to jobs within their project. An unrelated job grants
no inactivity exemption. Missing sources yield `source_unavailable`; deadlines yield an
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

An active current-claim wait suspends the stall ladder and stuck-timeout
backstop. Preparation expiry, orphan recovery and timeout-driven pool cleanup
consult the same fenced decision. Waiting keeps a pool worker busy, so sizing
does not release its seat or workspace. Process death, claim turnover and
operator stops still follow normal recovery.

Completion, expiry and cancellation reset the stall counters and grant one
normal lease interval to consume the result. The durable `wait_resumed_at`
timestamp also advances the task-lifecycle age backstop; terminal output does
not extend that age baseline. Result nudges and the next prime point to
`aq wait show WAIT_ID --json`. Busy sessions queue the result; absent task
sessions receive it on their next legitimate launch. Named supervisors use
their existing wake path. A manual pause never automatically resumes.

Task-addressed results route to the session currently holding the task,
including pool workers whose session names do not contain the task id.
Plain messages to `task:<id>` or `session:<id>` use the same idle delivery:
an idle holder has ``Handle `aq message status <id> --json`.`` typed into its
terminal.

A message wait (`--kind message --ref <thread> --after-seq <n>`) resolves on
the first later message on that thread addressed to the waiting session or its
task. An answer only satisfies it when it lands on the thread: answer with
`aq message reply <message-id>`, or as a supervisor with
`aq agent message <task-id> BODY --reply-to <message-id>`. Guidance sent
without `--reply-to` has no thread; it still wakes an idle worker through the
ordinary message nudge, but the wait stays active until its deadline.

The same cursor rule applies to collaboration threads. A message wait whose
`--ref` is a `collab-*` thread id resolves on the first later collaboration
message. Unlike an ordinary task thread, a collaboration wait can also end in
one of four typed reasons (first match wins; the result is final):

| reason                | meaning                                                             |
|-----------------------|---------------------------------------------------------------------|
| `peer_failed`         | at least one peer task is **FAILED** or **BLOCKED**                  |
| `peer_gone`           | all peer tasks are terminal, archived or missing                     |
| `thread_closed`       | thread state is `closed` (e.g. `budget_exhausted`, `members_below_two`, manual close) or `expired`, or the deadline has passed |
| `partner_not_running` | no peer is running after a 120-second grace period                  |

In each case the daemon records the terminal result; the thread is not a live
conversation to nudge. Continue with your own task rather than re-registering.

For mail that has waited more than five minutes for an idle live worker, run
`aq doctor --check messages.idle_worker_backlog`. It uses the delivery engine's
session lens and reports each message with the last refused-nudge reason for
its session.
For unresolved task waits whose targets have already settled, operators can run
`aq doctor --check waits.pending_terminal_tasks`. The read-only check includes
live and archived COMPLETED, FAILED and BLOCKED targets and reports the wait,
owner and session ids. The daemon's normal reconciliation resolves these waits
and queues their result pointers; doctor does not change claims or task state.

For timers that missed their due instant, run
`aq doctor --check waits.pending_timers`. This read-only check flags active
timers as soon as `due_at` passes, including timers whose hard timeout remains
in the future, and also flags timers past that timeout. It reports due and
deadline times, the last reconciliation check, and wait, owner and session ids.
It also flags resolved timers whose result remains undelivered for at least
30 seconds (or two delivery intervals, whichever is longer), including the
resolution time and result message id. An idle agent receives its result pointer
even when its terminal keeps repainting: completed transcript turns determine
idle status, and the next prompt makes the session busy again. Missing transcript
evidence falls back to recent terminal activity.

`agents.stuck_timeout_seconds` defaults to disabled (`0`) both with and without
an `agents:` configuration section. Explicit configured limits still apply.

## Opt-in live harness check

The regular reconciler and delivery tests use a fake terminal and disposable
PostgreSQL. A real tmux pool-terminal test uses a small local input stub and a
five-second timer and continuous idle terminal redraws, with no model credentials
or operator daemon:

```bash
aq test -m tmux -p no:xdist \
  tests/test_session_reconciler.py::test_short_timer_wakes_idle_pool_through_real_tmux
```

It checks normal reconciliation, result submission exactly once, and retained
claim and workspace. To measure idle behavior with installed harness credentials, run
the paid probe on an isolated tmux socket:

```bash
AQ_WAIT_REAL_HARNESS=codex aq test -m tmux -p no:xdist \
  tests/test_session_reconciler.py::test_opt_in_real_harness_wait_idle
```

Use `claude` instead of `codex` to check that harness. Set `POSTGRES_TEST_DSN`
to the disposable test service first. The probe observes actual transcripts
over two shortened lease intervals, checks for no new turns, tools or usage,
then confirms a result pointer reaches the harness. It cleans up its session
and socket and never contacts the operator daemon. Without the environment
opt-in it skips; a skip is not evidence of real-harness idle behavior.

## Installation and rollback

The operator upgrades through `aq db upgrade`. Workers must never migrate the
operator database. Revision `a00000000029` conditionally adds the wait table and
message sequence, including existing messages. The downgrade deliberately retains
wait/result history. Before rolling back code, stop new registrations, cancel
active waits through the command boundary, and restore ordinary lease baselines.
Never discard active exemptions without handling their results.

Worker template grants include `wait_register`, `wait_get`, `wait_list`, and
`wait_cancel`, plus `job_submit`, `job_get`, `job_list`, `job_cancel`,
`job_result`, and `job_logs`. Installed templates are write-if-absent; operators check drift with
`aq doctor --check profiles.system_drift` and merge grants without replacing their
edits:

```bash
aq agent list-profiles
aq agent profile-reseed --profile-id PROFILE_ID --grants-only
```
