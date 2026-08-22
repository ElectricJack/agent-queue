---
name: aq-comms
description: Messages, inbox, and human questions in the aq daemon. Use to check for queued messages (`aq message inbox`), send a message to the user or another agent (`aq message send`), reply to a specific message (`aq message reply`), or escalate a blocking question with `ask_human`. Also covers the message-delivery model (session vs user vs task recipients).
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

```bash
# To the human via the project's Discord / dashboard channel:
aq message send --to user --project <pid> \
  --body "Blocked on the API key rotation — details in task xyz."

# To another session (e.g. the supervisor):
aq message send --to session --to-id supervisor-<pid> \
  --body "Please clear the merge conflict on branch aq/foo."

# To a specific task's assigned agent:
aq message send --to task --to-id <task_id> \
  --body "Rebase your branch on main before continuing."
```

Recipients are `user`, `session`, or `task`. `to-id` disambiguates:
- `user` → project id or Discord channel id.
- `session` → session name (e.g. `supervisor-demo`, `s-my-task`).
- `task` → task id.

## Reply

Replies preserve threading so the receiver can trace the exchange:

```bash
aq message reply --message-id <original_msg_id> \
  --body "Yes, proceed. I've cleared the gate."
```

## Ask the human a question

For a mid-task blocking question, don't message-and-guess — use
`ask_human`:

```bash
aq ask-human --project <pid> --task-id <task_id> \
  --question "Should I use the v1 or v2 schema for the migration?"
```

`ask_human` puts the task into `WAITING_INPUT`, notifies the human, and
holds until they respond. Your response comes back via
`aq task input-response` (or the human's inline reply in the chat
thread — the daemon routes it into a `provide_input` command
automatically).

Use `ask_human` for:
- Design decisions with multiple valid answers.
- Missing spec context you can't infer from the code.
- Permission asks ("delete this stale branch?").

Don't use it for:
- Anything you can derive from `aq task explain` (that's not blocked-
  on-human, it's blocked-on-thinking).
- Routine progress reports — those are `aq message send`.

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
