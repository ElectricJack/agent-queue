---
name: aq-comms
description: Covers aq messaging — reading the session inbox, sending to the human operator, a supervisor, a session or a task's agent, replying on a thread, reporting a blocking decision to the project supervisor and waiting for the answer, and how delivery and sender attribution work. Use when checking for messages, answering a delivery nudge, asking a supervisor or human for a decision, or sending supervisor guidance to a live worker. Collaboration threads, recurring prompts (aq cron) and human asks are in its reference file.
allowed-tools:
  - Bash
---

# aq comms — messaging and asking for decisions

The daemon's message bus carries three kinds of frames: agent → user
("I need X"), user → agent (chat and directives), and agent → agent
(coordination). All flow through `aq message`.

**Reference file:** [reference/coordination.md](reference/coordination.md) —
collaboration threads between held tasks, recurring prompts with `aq cron`, and
the operator/supervisor human-ask and escalation surfaces.

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

Every session that starts with a bearer token gets an automatic inbox. No hook
renders it at a prompt boundary. Pending messages reach you when `aq prime`
runs (start, resume, post-compact) and through the independent message-delivery
service, which nudges the session once it goes idle:
``Handle `aq message status <id> --json` (running it marks the message read).``
Run that command to read the body.

- The nudge records a notification attempt and consumes nothing; the message
  stays in `aq message inbox` until its recipient reads it or inbox/prime
  injects the body. Unread notifications retry with bounded backoff, then alert
  the global supervisor once. Operator status inspections do not consume mail.
- A plain session may read the status only of messages addressed to its own
  session, held task or profile; anything else answers "not found".
- `aq inbox --inject` renders the queue inline mid-task; `aq message inbox` is
  the same queue as data. Injection stamps delivery but leaves `via` and
  `read_at` unset.

## Send

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

Recipients use `KIND:ID` syntax. `dashboard` is the canonical ID for the human
operator; other recipient IDs identify the specific session or task.

**Sender attribution.** Omit sender flags for ordinary authenticated session
sends. The server derives `session:<your session UUID>` from the current
authenticated launch and live claim, so a supervisor's `aq message reply`
returns to your mailbox. Pool sends carry the claim epoch automatically from
`.aq/claim.json`. Local human/operator or script calls without an authenticated
session default to `user:cli` and may provide explicit senders. A session may
explicitly name only its own UUID; a live named supervisor may also use its own
canonical supervisor addresses. Sessions cannot impersonate users or other
sessions. Collaboration and scoped global reports keep their dedicated
server-derived attribution.

### Supervisor guidance to a live worker

Supervisors use the native live-worker command for operational guidance. It
resolves a task, agent or session to its current live worker, queues the
message durably, mirrors it in the task comments, and reports delivery status:

```bash
aq agent message <task-id|agent-id|session-id> "Stop the full suite; run the focused file."
aq agent message <task-id> "Please use xdist" --wait 60
aq agent message --all-running "Never run a bare pytest" --profile worker
aq message status <message-id>
```

When the guidance answers a worker's message, pass that message's id with
`--reply-to <message-id>`. The guidance then joins the message's thread, which
is what a worker's verified message condition waits for; guidance sent without
it never satisfies that wait:

```bash
aq agent message <task-id> "Filed: fresh-ember and .1-.9." --reply-to <message-id>
```

Use `aq session nudge` only for low-level diagnostics; it is not a reliable
supervisor-to-worker delivery surface.

## Reply

Replies preserve threading so the receiver can trace the exchange:

```bash
aq message reply <original_msg_id> "Yes, proceed. I've cleared the gate."
```

A worker can reply only to messages addressed to its own mailboxes, and the
reply's sender is always that mailbox. Guidance from a project supervisor's own
session is signed `session:supervisor-<project>`; answer it with
`aq message reply <guidance-id>` and the reply lands back in that mailbox on the
same thread. Guidance from the operator CLI, automation or the global supervisor
is signed `system:supervisor` and cannot be replied to; answer those with
`aq message send --to session:supervisor-<project>`.

## Report a blocker

The old ask_human command was retired. A worker reports a blocking decision to
its owning project supervisor with the held task ID and evidence:

```
- [ ] Record the finding as a task comment (aq-tasks)
- [ ] Send one report on a unique thread and keep the returned created_seq
- [ ] If prime says waits are enabled: register ONE message wait on that thread
- [ ] Preserve held work and end the turn; do not poll or repeat the report
```

```bash
aq message send --to session:supervisor-<pid> --project <pid> \
  --thread-id BLOCKER_THREAD_ID --json \
  --body "Blocked on task <task-id>: <evidence and decision needed>."
aq wait register --kind message --ref BLOCKER_THREAD_ID \
  --after-seq <sent-message-created_seq> --idempotency-key <stable-key>
```

Choose a unique `BLOCKER_THREAD_ID`. Register once and end the turn; consume the
returned wait ID when notified (`aq wait show <wait-id> --consume --json`). If
waits are disabled or registration returns `wait.disabled`, preserve the claim
and end the turn awaiting inbox delivery. An old `user:cli` report does not
confer ownership of its thread. Do not retry an unauthorized wait or change its
authorization; send an owned report instead. A message to `user:dashboard`
informs the operator but does not create a tracked decision.

## Message delivery model (why messages are reliable)

- Every message row is persisted before dispatch — daemon restarts don't lose
  them.
- Delivery is per-`to_kind` (session / task / profile / user). Task mail nudges
  the active holder; absent task mail waits without spawning a worker. Profile
  mail is pull-only. The daemon retries transient failures.
- If a session is asleep or mid-turn, mail stays pending. A supervisor can be
  woken; busy sessions wait for inbox/prime or an idle nudge from the
  independent delivery service. Absent session mail is archived after 24 hours,
  with a return notice only for human senders.
- Chat messages between the dashboard and a supervisor session use the thread
  id `dashboard:<project_id>` — filter by that thread id to see the live chat.

## Old patterns

<details>
<summary>Explicit sender on a daemon that predates server-derived attribution</summary>

Before the server derived the sender from the authenticated launch, a session
send could arrive as `user:cli`, so a supervisor's reply never reached the
worker. The supported workaround on such a daemon names the held session UUID
(from `.aq/claim.json` or prime) explicitly; it supplies an address, never extra
authority:

```bash
aq message send --to session:supervisor-<pid> --project <pid> \
  --from-kind session --from-id <held-session-UUID> \
  --thread-id <unique-thread> --body "Blocked: <task and decision needed>" --json
```

Source validation does not mean the fix is loaded in the operator daemon.
</details>
