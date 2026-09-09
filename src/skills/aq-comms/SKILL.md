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
```

Every session that starts with a bearer token gets an automatic inbox.
Nothing renders it at every prompt boundary: the `UserPromptSubmit` hook
that ran `aq inbox --inject` was removed on 2026-08-27. Pending messages
reach you when `aq prime` runs (start, resume, post-compact) and through
the cascade's nudge, which types them into the session once it goes idle.
`aq inbox --inject` is still a supported command, so run it yourself when
you want the queue rendered inline mid-task; `aq message inbox` is the
same queue as data.

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

## Reply

Replies preserve threading so the receiver can trace the exchange:

```bash
aq message reply <original_msg_id> "Yes, proceed. I've cleared the gate."
```

## Report a blocker

The old ask_human command was retired. For a mid-task
blocking question, report the blocker through the supported message queue:

```bash
aq message send --to user:dashboard --project <pid> --body "Blocked: should I use the v1 or v2 schema for the migration?"
```

Include the task id and the decision needed in the message body so the
supervisor or human can respond through the supported message flow.

## Escalations: when a human decision is required

A message to `user:dashboard` is how you *tell* a human something. It is not how
a blocking decision gets tracked. When work is genuinely stopped until a person
chooses, the durable record is an **escalation**, and creating one is the owning
project supervisor's job, not a worker's:

- A worker reports the blocker (message, task comment, or a `fail` outcome with
  the reason) and stops. The blocked-task playbook routes it to
  `session:supervisor-<pid>`.
- The supervisor investigates, and only if a human must decide does it call
  `escalation_create`.
- The human replies — in the dashboard, or in the Discord thread the escalation
  opened. Either way the reply is persisted as verified human evidence and
  queued back to the supervisor.
- The supervisor applies that evidence with `escalation_apply_reply` and records
  the outcome.

Two consequences for you as a worker: a Discord reply never reaches your
terminal directly, and `question_answer` refuses a *human* caller outright —
a person replies with `escalation_reply` instead. The owning supervisor may
answer a narrow factual question itself; anything needing a person goes up with
`aq question escalate <question-id> --reason "…"`, which creates or reuses the
durable escalation for that question.

```bash
aq escalation list --states needs_human,reply_received   # waiting on a person
aq escalation get --escalation-id <id>                   # conversation and history
```

See `docs/guides/escalations.md` for the full model.

## Message delivery model (why messages are reliable)

- Every message row is persisted before dispatch — daemon restarts don't
  lose them.
- Delivery is per-`to_kind` (session / task / user). The daemon retries
  transient failures.
- If a session is asleep or mid-turn when a message arrives, the message
  parks. It is rendered when the session next primes, or when the
  cascade nudges the idle session; `aq inbox --inject` renders the same
  parked messages on demand. No hook injects them at a prompt boundary.
- Chat messages between the dashboard and a supervisor session use the
  thread id `dashboard:<project_id>` — filter by that thread id when
  you want to see the current live chat.
