---
name: aq-comms
description: Messages, inbox, and human questions in the aq daemon. Use to check for queued messages (`aq message inbox`), send a message to the user or another agent (`aq message send`), reply to a specific message (`aq message reply`), or report a blocking question with `aq message send`. Also covers the message-delivery model (session vs user vs task recipients).
allowed-tools:
  - Bash
---

# aq comms — Messaging and asking humans

The daemon's message bus carries three kinds of frames: agent → user
("I need X"), user → agent (chat + directives), and agent → agent
(coordination). All flow through `aq message`.

## Read

```bash
aq message inbox                              # your session's pending messages
aq message inbox --json                       # structured (--json is
                                              # global: any position works)
aq message list --project <pid>               # everything sent in a project
aq message list --thread-id dashboard:<pid>   # filter to a chat thread
aq message status <message-id> --json         # one message you were handed,
                                              # body included
```

Every session that starts with a bearer token gets an automatic inbox.
Nothing renders it at every prompt boundary: the `UserPromptSubmit` hook
that ran `aq inbox --inject` was removed on 2026-08-27. Pending messages
reach you when `aq prime` runs (start, resume, post-compact) and through
the independent message-delivery service, which nudges the session once it
goes idle: ``Handle `aq message status <id> --json` (running it marks the message read).``
Run that command to read the body. The nudge records a notification attempt; the message stays
in `aq message inbox` until its recipient reads it or inbox/prime injects
the body. Unread notifications retry with bounded backoff, then alert the
global supervisor once. Operator status inspections do not consume mail.
A plain session may read the status
only of messages addressed to its own session, held task or profile;
anything else answers "not found". `aq inbox --inject` is still a supported command,
so run it yourself when you want the queue rendered inline mid-task;
`aq message inbox` is the same queue as data. Injection stamps delivery but
leaves `via` and `read_at` unset. A nudge alone consumes nothing.

## Send

Supervisors should use the native live-worker command for operational
guidance. It resolves a task, agent, or session to its current live worker,
queues the message durably, mirrors it in the task comments, and reports its
delivery status:

```bash
aq agent message <task-id|agent-id|session-id> "Stop the full suite; run the focused file."
aq agent message <task-id> "Please use xdist" --wait 60
aq agent message --all-running "Never run a bare pytest" --profile worker
aq message status <message-id>
```

When the guidance answers a worker's message, pass that message's id with
`--reply-to <message-id>`. The guidance then joins the message's thread, which
is what a worker's verified message condition waits for; guidance sent without it
never satisfies that wait:

```bash
aq agent message <task-id> "Filed: fresh-ember and .1-.9." --reply-to <message-id>
```

Guidance from a project supervisor's own session is signed
`session:supervisor-<project>`, so the worker answers it with
`aq message reply <guidance-id>` and the reply lands back in that mailbox on
the same thread. Guidance from the operator CLI, automation or the global
supervisor is signed `system:supervisor` and cannot be replied to; a worker
answers those with `aq message send --to session:supervisor-<project>`.
A worker can reply only to messages addressed to its own mailboxes, and the
reply's sender is always that mailbox.

Use `aq session nudge` only for low-level diagnostics; it is not a reliable
supervisor-to-worker delivery surface.

```bash
# To the human operator.  `user:dashboard` is the canonical recipient — the
# dashboard is the human's surface; Discord only mirrors decisions that were
# escalated, and it is never addressed directly:
aq message send --to user:dashboard --project <pid> \
  --body "Blocked on the API key rotation — details in task xyz."

# To another session (e.g. the supervisor):
aq message send --to session:supervisor-<pid> \
  --body "Please clear the merge conflict on branch aq/foo."

# To a specific task's assigned agent:
aq message send --to task:<task_id> \
  --body "Rebase your branch on main before continuing."
```

Recipients use `KIND:ID` syntax. `dashboard` is the canonical ID for the
human operator; other recipient IDs identify the specific session or task.

Omit sender flags for ordinary authenticated session sends. The server derives
`session:<your session UUID>` from the current authenticated launch and live
claim, so a supervisor's `aq message reply` returns to your mailbox. Pool sends
carry the claim epoch automatically from `.aq/claim.json`. Local human/operator
or script calls without an authenticated session default to `user:cli` and may
provide explicit senders. A session may explicitly name only its own UUID; a
live named supervisor may also use its own canonical supervisor addresses.
Sessions cannot impersonate users or other sessions. Collaboration and scoped
global reports keep their dedicated server-derived attribution.

If the running daemon/CLI predates this fix, use the supported explicit sender
workaround until delivery. Read the held session UUID from `.aq/claim.json` or
prime; it supplies an address, never extra authority:

```bash
aq message send --to session:supervisor-<pid> --project <pid> \
  --from-kind session --from-id <held-session-UUID> \
  --thread-id <unique-thread> --body "Blocked: <task and decision needed>" --json
aq wait register --kind message --ref <unique-thread> \
  --after-seq <sent-message-created_seq> --idempotency-key <stable-key>
```

Register once and end the turn; consume the returned wait ID when notified.
An old `user:cli` report does not confer ownership of its thread. Do not retry
an unauthorized wait or change its authorization; send an owned report instead.
Source validation does not mean this fix is loaded in the operator daemon.

## Reply

Replies preserve threading so the receiver can trace the exchange:

```bash
aq message reply <original_msg_id> "Yes, proceed. I've cleared the gate."
```

## Report a blocker

The old ask_human command was retired. A worker reports a blocking decision to
its owning project supervisor with the held task ID and evidence:

```bash
aq message send --to session:supervisor-<pid> --project <pid> \
  --thread-id BLOCKER_THREAD_ID --json \
  --body "Blocked on task <task-id>: <evidence and decision needed>."
```

Choose a unique `BLOCKER_THREAD_ID`. Record the finding in a task comment too.
Preserve held work; when prime enables
waits, register one durable message wait on the report's unique thread and returned
`created_seq`, then end the turn awaiting delivery. Do not poll. A message to
`user:dashboard` informs the operator but does not create a tracked decision.

## Human asks and legacy escalations

The canonical decision record is a **human ask**. The supervisor investigates and
publishes/escalates the source only when a human must decide. The dashboard's
Needs-you view and scoped ask commands expose current context:

```bash
aq ask list --project-id <pid> --view needs_you --json
aq ask show --ask-id <ask-id> --json
aq escalation list --states '["needs_human","reply_received"]'
aq escalation get --escalation-id <id>
```

These are operator/supervisor surfaces; workers have no ask-decision authority.
Human text is verified evidence, never a supervisor-authored assertion of approval.
Prepare/decide binds the selected choice to a current source version, preview and
idempotency key. Compatibility `escalation_apply_reply` requires this context too
and only applies an exact stored declared choice. Unknown operations reconcile
rather than blindly retrying.

Legacy escalation threads and choice buttons accept replies only while that
destination retains legacy ownership. Ask channel mode owns inbound text and
buttons exclusively, posts without creating threads, and never falls back to
conversation intake. Inspect `aq ask cutover-status` before diagnosing missing
thread replies; workers must not change modes or reset production history.

The shipped `supervisor-failure-triage` playbook handles task failures; the old
`blocked-task-escalation` recording is retired. A narrow factual worker question
can be answered by its supervisor; when human authority is required,
`aq question escalate <question-id> --reason "…"` links it to the decision path.
A human cannot bypass that path with `question_answer`.

See `docs/guides/escalations.md` and `docs/guides/human-asks-rollout.md`.

## Message delivery model (why messages are reliable)

### Recurring prompts on every harness

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

Registration never executes immediately. Intervals align to epoch multiples plus
offset; 900/120 fires at :02/:17/:32/:47 UTC. Simple cron accepts minute 0–59,
`*` or `*/N`, hour 0–23 or `*`, and requires `* * *` for the remaining fields.
The IANA timezone defaults to UTC. Show/list expose next-fire and delivery diagnostics.
Missed ticks coalesce into one pending message; busy owners defer delivery, which
has five attempts with backoff and a one-hour expiry. A daemon restart retains
the same session instance; ending/replacing that instance expires its schedules.
A worker's claim ending also expires its schedules; cron grants no lease exemption.
Global supervisors leave project unset and can register/list their own patrol.
`--consume` consumes only the schedule's notification. Scheduling grants no extra
authority to execute its prompt. See `docs/guides/agent-cron.md` for the lifecycle
and installed grant/skill drift workflow.

- Every message row is persisted before dispatch — daemon restarts don't
  lose them.
- Delivery is per-`to_kind` (session / task / profile / user). Task mail nudges
  the active holder; absent task mail waits without spawning a worker. Profile
  mail is pull-only. The daemon retries transient failures.
- If a session is asleep or mid-turn, mail stays pending. A supervisor can be
  woken; busy sessions wait for inbox/prime or an idle nudge from the independent
  delivery service. Absent session mail is archived after 24 hours, with a return
  notice only for human senders. `aq inbox --inject` renders pending bodies.
  No hook injects them at a prompt boundary.
- Chat messages between the dashboard and a supervisor session use the
  thread id `dashboard:<project_id>` — filter by that thread id when
  you want to see the current live chat.

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
The `--client-key` is the only send-side flag collaboration-specific; everything
else is the ordinary message send you already know.

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
reference before continuing. An unavailable peer or source is an explicit outcome,
not proof the requested work succeeded. Operational deadlines escalate to the
supervisor without resetting wait age or waking the worker. Human waits have no
automatic deadline; the supervisor decides or lifts with explicit evidence.
See [agent-waits.md](/docs/guides/agent-waits.md) for the checkpoint and
acknowledgement protocol.

Full worker guide: [agent-collaboration.md](/docs/guides/agent-collaboration.md).
