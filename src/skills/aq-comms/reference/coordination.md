# aq coordination — collaboration threads, recurring prompts, human asks

Reference for [the aq-comms skill](../SKILL.md).

## Contents

- Collaboration threads (agent-to-agent, same goal)
- Recurring prompts on every harness (`aq cron`)
- Human asks and legacy escalations (operator and supervisor)

## Collaboration threads (agent-to-agent, same goal)

A collaboration thread is a bounded ordered conversation between 2-4 held
tasks, each on its own branch and worktree. It exists to let agents that are
working on the same goal coordinate without one of them needing to hand off
the task. Threads are operator- or supervisor-created and invite a fixed set
of member tasks when created.

Workers use:

```bash
aq collaboration show <thread-id>                       # goal, state, cursor,
                                                        # up to 20 messages
aq collaboration show <thread-id> --after 42            # resume after seq 42
aq collaboration accept <thread-id>                     # join under your live claim
aq collaboration list --task-id <your-task-id>          # threads you belong to
aq collaboration close <thread-id> --note "Goal met."   # record final result
```

To send a message on a thread, use the ordinary send with the thread's id as
the `--thread-id` and the target peer as `--to-kind task --to-id <peer-task>`:

```bash
aq message send --thread-id <thread-id> --to-kind task --to-id <peer> \
  --body "I'm about to commit X; will need a rebase from you after that." \
  --client-key turn-1
```

Re-send the same `--client-key` with the same body if you need to retry: the
daemon de-duplicates it and does not double-count against the thread's budget.
The `--client-key` is the only collaboration-specific send flag; everything
else is the ordinary message send.

When prime reports waits enabled for the project, wait on a peer reply using the
ordinary durable message wait with the thread id as the referral. If waits are
disabled or registration returns `wait.disabled`, preserve the claim and end the
turn awaiting inbox delivery; do not retry registration or poll:

```bash
aq wait register --kind message --ref <thread-id> --after-seq <cursor> \
  --idempotency-key turn-1-retry
```

The verified wait resolves on a source outcome after your cursor. Register once
and end the turn. AQ chooses sleep and delivers the outcome; inspect its result
reference before continuing. An unavailable peer or source is an explicit
outcome, not proof the requested work succeeded. Operational deadlines escalate
to the supervisor without resetting wait age or waking the worker. Human waits
have no automatic deadline; the supervisor decides or lifts with explicit
evidence.

In the agent-queue repository, `docs/guides/agent-waits.md` covers the
checkpoint and acknowledgement protocol and `docs/guides/agent-collaboration.md`
is the full worker guide.

## Recurring prompts on every harness

Use AQ's supported `aq cron` commands for recurring prompt wake-ups on Codex,
Claude and other harnesses. No native scheduling tool or shell loop is needed.
Register with a stable key; identical startup retries recover the same ID:

```bash
aq cron register --every 900 --offset 120 --idempotency-key patrol-v1 \
  --prompt 'Run the authorized patrol and handle its findings.'
aq cron register --cron '15 9 * * *' --timezone America/Los_Angeles \
  --idempotency-key daily-v1 --prompt 'Run the authorized morning check.'
aq cron list --json
aq cron show SCHEDULE_ID --consume --json
aq cron cancel SCHEDULE_ID
```

- Registration never executes immediately. Intervals align to epoch multiples
  plus offset; 900/120 fires at :02/:17/:32/:47 UTC.
- Simple cron accepts minute 0–59, `*` or `*/N`, hour 0–23 or `*`, and requires
  `* * *` for the remaining fields. The IANA timezone defaults to UTC.
- Show/list expose next-fire and delivery diagnostics. Missed ticks coalesce
  into one pending message; busy owners defer delivery, which has five attempts
  with backoff and a one-hour expiry.
- A daemon restart retains the same session instance; ending or replacing that
  instance expires its schedules. A worker's claim ending also expires its
  schedules; cron grants no lease exemption.
- Global supervisors leave project unset and can register/list their own
  patrol. `--consume` consumes only the schedule's notification. Scheduling
  grants no extra authority to execute its prompt.

The repository's `docs/guides/agent-cron.md` covers the lifecycle and the
installed grant/skill drift workflow.

## Human asks and legacy escalations

The canonical decision record is a **human ask**. The supervisor investigates
and publishes/escalates the source only when a human must decide. The
dashboard's Needs-you view and scoped ask commands expose current context:

```bash
aq ask list --project-id <pid> --view needs_you --json
aq ask show --ask-id <ask-id> --json
aq escalation list --states '["needs_human","reply_received"]'
aq escalation get --escalation-id <id>
```

These are operator/supervisor surfaces; workers have no ask-decision authority.
Human text is verified evidence, never a supervisor-authored assertion of
approval. Prepare/decide binds the selected choice to a current source version,
preview and idempotency key. Compatibility `escalation_apply_reply` requires
this context too and only applies an exact stored declared choice. Unknown
operations reconcile rather than blindly retrying.

Legacy escalation threads and choice buttons accept replies only while that
destination retains legacy ownership. Ask channel mode owns inbound text and
buttons exclusively, posts without creating threads, and never falls back to
conversation intake. Inspect `aq ask cutover-status` before diagnosing missing
thread replies; workers must not change modes or reset production history.

The shipped `supervisor-failure-triage` playbook handles task failures; the old
`blocked-task-escalation` recording is retired. A narrow factual worker
question can be answered by its supervisor; when human authority is required,
`aq question escalate <question-id> --reason "…"` links it to the decision path.
A human cannot bypass that path with `question_answer`.

The repository's `docs/guides/escalations.md` and
`docs/guides/human-asks-rollout.md` have the full model.
