# Questions from live agents

> **Where this fits.** Question detection is one of the three lanes described in
> [Messaging, digests and escalations](concepts/messaging.md); read that page
> first for the vocabulary (message, escalation, supervisor). This page is the
> detail of how a question is *noticed* in a live transcript and who it goes to.

AQ watches native Codex and Claude transcripts for completed assistant turns
that ask for input while a worker holds an active task, and OpenCode's own
question dialogs (below). It records the question against the exact task claim
and session instance. Named supervisor sessions and unassigned interactive
terminals do not recursively create questions.

Every question goes first to the project supervisor (`supervisor-<project>`).
For a transcript question only routine factual questions are the supervisor's
to answer; approval requests, scope/design decisions, risky actions and
ambiguous questions are human-required. The supervisor can escalate a routine
question and cannot supply a human-only approval: it investigates and runs
`aq question escalate`, and the human answers through that escalation.

A question the supervisor has neither answered nor escalated within
`discord.escalation.supervisor_delivery_timeout_minutes` (15 by default) raises
one operational notice (`source_kind` `question_unanswered`) so a human knows
the worker is stuck. The question stays the supervisor's: the notice cannot
answer, approve or re-route it.

## OpenCode question dialogs

OpenCode's `question` tool opens a dialog that blocks the whole session until
someone answers or dismisses it, and while it is open no nudge can reach the
composer. OpenCode keeps no transcript file, but it records every tool call in
its own SQLite store (`$XDG_DATA_HOME/opencode/opencode.db`, by default under
`~/.local/share`). AQ reads that store, read-only, every 10 seconds for each
live OpenCode session holding a task
([`src/sessions/native_questions.py`](../src/sessions/native_questions.py)):
a `question` call still `running` started after the session's claim is a
pending dialog, including one a subagent opened. It becomes one durable
question keyed by session instance, claim and the dialog's call id, so a
re-read or a daemon restart finds the same row.

The same store is also AQ's only *liveness* clock for a harness that keeps no
transcript file: which OpenCode sessions an AQ session owns is scoped here and
shared, and
[`src/sessions/opencode_store.py`](../src/sessions/opencode_store.py) reads the
rows' own timestamps so the stall ladder can tell a working turn from a wedged
one that still repaints its spinner — see
[session-troubleshooting](guides/session-troubleshooting.md#a-worker-that-looks-busy-forever-and-is-not).

- **Who may answer.** A native dialog is the model choosing between options
  inside its approved task, so scope, design and "should I continue" choices
  are routed to the supervisor as answerable from that task's context. A
  request to authorize risk — access, credentials or secrets; destructive,
  external or delivery actions (push, merge, deploy, publish); rewriting
  history; the daemon, its database or migrations; skipping checks; widening
  the task — is human-required, judged on every option rather than the
  displayed excerpt, and the supervisor's answer to it is refused.
- **Delivery.** `aq question answer` accepts the answer, then AQ closes *that*
  dialog: one `Escape` into the exact pane the claim owns. The TUI shows one
  prompt for the whole session tree (a subagent's permission request before
  any question), so AQ presses only while this dialog is the tree's sole
  pending prompt and no tool anywhere in the tree is running, re-reading the
  store under the claim fence immediately before the key. Presses are counted
  before they are sent, at most two per question and 30 seconds apart (a quick
  second `Escape` would read as "interrupt"), and the store must record the
  dialog as closed before AQ types the usual one-line answer pointer. A dialog
  someone already dismissed, or undid, gets the pointer alone; one answered in
  the terminal resolves the question, and an answer it overtook is reported as
  not delivered.
- **Human gates.** A human-required dialog is never closed for the supervisor,
  and it stays open even if someone dismisses it and the worker carries on:
  only an answer in the terminal, an undo, or the human's answer through the
  escalation resolves it.
- **Resume.** A delivered native answer counts once the worker's model starts
  a new turn. Until then `aq question list` still shows it as `delivered`; with
  no new turn within the session lease (at least a minute), AQ raises one
  `question_resume` notice and stops waiting.
- **Bounded.** An accepted answer that cannot be delivered within the same
  supervisor timeout — the dialog will not close, the store is unreadable, a
  draft sits in the composer — raises one `question_delivery` notice. AQ never
  retries a key past its bound, never restarts the worker to deliver, and never
  answers or approves an OpenCode permission prompt.

A plain message cannot reach a worker whose dialog is open; answer through the
question instead. OpenCode workers are also told in `aq prime` not to open
optional questionnaires or "should I continue" confirmations, and that
OpenCode's post-compaction "stop and ask for clarification" prompt means
"continue" under AQ
([`src/prime/templates/tool_guidance_opencode.md`](../src/prime/templates/tool_guidance_opencode.md)).
The answer pointer is read with `aq message status`, so an OpenCode worker
profile must grant `message_status`, as the shipped Claude and Codex worker
templates do.

There is no separate `aq task ask-human` command. That never-implemented
gate-plus-message surface was retired to avoid creating a second question
identity and state flow. End a completed assistant turn with the question so
AQ records it here. If the worker cannot continue and only needs to notify the
operator, use `aq message send --to user:dashboard --project PROJECT_ID --body
"Blocked: ..."`.

## Answer a question

- In Discord, use **Reply** on the agent-question card. The button remains usable
  after an AQ restart. Only configured authorized Discord users can submit it.
- In the dashboard, **Waiting for input** appears in the Agent flock. Open the
  agent's terminal and answer directly to continue its existing conversation.
- From the local operator CLI:

  ```sh
  aq question list
  aq question answer QUESTION_ID --body "Your answer"
  aq question escalate QUESTION_ID --reason "This needs a human decision"
  ```

Supervisor sessions use their existing scoped CLI credentials. Worker tokens
cannot impersonate human callers. General MCP/LLM command calls without an
operator identity cannot approve questions.

An answer is queued durably and submitted into the same live terminal. AQ never
clears a terminal draft or reassigns/restarts the task to deliver an answer. If
there is a draft, the answer waits until the input is available. Late answers
cannot follow a reused tmux name into a replacement worker or a different claim.

## Waiting and recovery

Waiting retains the worker and its workspace. Stall recovery is suspended for
the matching pending question; saved stall counters are not reset. A direct user
reply resolves the question. AQ's own stall reminder does not count as a reply.
After a question resumes a task, the stuck-session backstop uses activity so time
spent waiting for the user does not immediately kill the resumed worker.

Questions and accepted answers survive daemon restarts. Discord records actual
successful delivery separately from queue delivery and retries enrolled failures;
it does not replay old message history when the feature is first enabled. If no
Discord destination is available, the pending question remains visible in AQ.
As with any external transport, a process crash after Discord accepts a message
but before the receipt commits can leave a duplicate notification on retry.
Terminal input has the same small crash window: if the process dies after input
is submitted but before its receipt commits, delivery may repeat after recovery.
Normal retries and competing replies are deduplicated.

This feature handles assistant questions in native conversation transcripts and
OpenCode question dialogs. It does not answer or approve harness permission
dialogs or grant additional
filesystem, network, API, or project access. Ended sessions are not rebound to a
new worker to deliver historical answers.

## Context between tasks

Task workers start new conversations for new tasks. Pool workers now do the same
by default: after a task closes, AQ retires its session and starts a fresh one
for the next task while retaining the global worker identity. This resets model
context without injecting provider-specific slash commands into the terminal.
Task summaries, files, and project instructions remain available.

Pending questions, active tasks, same-task recovery, and named supervisor chats
retain their context. Operators who deliberately want the older multi-task pool
conversation can set `swarm.fresh_context_per_task: false`; profile
`max_claims_per_session` then applies as before.
