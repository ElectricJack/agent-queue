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
aq message inbox --json                       # structured
aq message list --project <pid>               # everything sent in a project
aq message list --thread-id dashboard:<pid>   # filter to a chat thread
```

Every session that starts with a bearer token gets an automatic inbox.
The `aq inbox --inject` hook the Claude harness runs at every prompt
boundary rendering pending messages inline — you don't usually need to
poll `aq message inbox` manually.

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
# To the human via the project's Discord / dashboard channel.  The canonical
# human-operator recipient is user:dashboard:
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
aq message reply --message-id <original_msg_id> \
  --body "Yes, proceed. I've cleared the gate."
```

## Report a blocker

The ask_human command is not available in the current build. For a mid-task
blocking question, report the blocker through the supported message queue:

```bash
aq message send --to user:dashboard --project <pid> --body "Blocked: should I use the v1 or v2 schema for the migration?"
```

Include the task id and the decision needed in the message body so the
supervisor or human can respond through the supported message flow.

## Message delivery model (why messages are reliable)

- Every message row is persisted before dispatch — daemon restarts don't
  lose them.
- Delivery is per-`to_kind` (session / task / user). The daemon retries
  transient failures.
- If a session is asleep when a message arrives, the message parks. When
  the session wakes, `aq inbox --inject` (hook) or `aq prime` (manual)
  renders parked messages inline.
- Chat messages between the dashboard and a supervisor session use the
  thread id `dashboard:<project_id>` — filter by that thread id when
  you want to see the current live chat.
