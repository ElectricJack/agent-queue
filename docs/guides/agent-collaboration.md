---
tags: [guide, agents, collaboration, threads]
---

# Agent collaboration threads

A collaboration thread is a bounded, ordered conversation between 2–4 held
tasks that are **each on their own branch**. It exists for the three cases
where a single-agent turn is not enough:

- **M3 — two writers, one shared goal.** Two agents each own a task (e.g.
  `task-a` implementing `--watch` in `src/api/watch.py` and `task-b`
  implementing `--follow` in `src/api/follow.py`) and must coordinate
  ordering, naming, or error-path semantics before committing.
- **M4 — reviewer tailing a writer.** A writer pushes work; the reviewer
  agent watches the same thread for the push SHA and posts a verdict.
- **M5 — shared scratch / channel.** Two or three workers exchange
  intermediate results (a generated type, a diff hash, a test name) without
  one of them needing to close and re-open its task.

**Never a shared checkout.** Every member keeps its own worktree and its own
claim. The thread carries small, ordered messages — not files. To share a
large artifact, commit it, push the branch, and post the commit SHA in the
message body.

## What the thread does not do

- It does not pause or hold other tasks.
- It does not change pool scheduling or grant a stall exemption.
- It does not give a member access to the daemon or to another task's files.
- It does not survive a member task's `MISSING`, `ARCHIVED`, or terminal state
  — the daemon closes it.

## Creating a thread (operator / supervisor)

Only an operator or a named supervisor may create a thread. The command is
idempotent; replaying the same `--idempotency-key` returns the existing
thread.

```bash
aq collaboration create \
  --project "$AQ_PROJECT_ID" \
  --task task-a --task task-b \
  --goal "Coordinate --watch --follow ordering before both branches commit" \
  --deadline-minutes 90 \
  --budget 25 \
  --idempotency-key "plan-9906-2026-09-26-coordination"
```

Key options:

| option                | meaning                                                                 |
|-----------------------|-------------------------------------------------------------------------|
| `--task`              | Member task id; repeat 2–4 times. Every member must be a held task in the same project. |
| `--goal`              | What the members should settle. ≤ 1000 characters. Appears on every message. |
| `--deadline-minutes`  | Thread lifetime from creation. Default and maximum: 120.                |
| `--budget`            | Maximum number of messages the thread may carry. 1–40.                 |
| `--idempotency-key`   | Required. Replays return the same thread.                              |

Every member is invited exactly once in the creation transaction. No
separate per-member accept is required for the thread to exist; see
"Joining" below.

## Joining (worker)

A worker joins a thread for its held task by accepting it under the live
claim:

```bash
# Inside your worker session; the task id and claim epoch come from prime
# and .aq/claim.json — you do not need to pass them explicitly.
aq collaboration accept <THREAD_ID>
# For a pool session that overrides the epoch:
aq collaboration accept <THREAD_ID> --claim-epoch 7
```

`accept` is a no-op if you have already joined. After accepting, you can
send, wait, and read the thread.

## Reading the thread

Read at most 20 most-recent messages, optionally after a sequence number
cursor:

```bash
aq collaboration show <THREAD_ID>
# Resume after seq 42:
aq collaboration show <THREAD_ID> --after 42
# Structured:
aq collaboration show <THREAD_ID> --after 42 --json
```

The response carries:

- `state`: `"active"` | `"closed"` | `"expired"`
- `close_reason`: one of `"closed"`, `"budget_exhausted"`, `"expired"`, `"members_below_two"` (only when state ≠ active)
- `deadline_at`: UTC epoch seconds
- `message_budget`: thread budget
- `message_count`: messages consumed so far
- `capacity_hold`: `true` until the thread has a live, running partner pair
- `messages`: ordered array of up to 20 records with `seq`, `thread_id`,
  `message_id`, `sender_task_id`, `body`, `client_key`, `created_at`
- `next_step`: one of the following strings — follow its instructions exactly

| `next_step` string                                                                 | you should do                                                                 |
|-----------------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| `Thread <id> has ended (<reason>). Continue your own task.`                       | Thread is over. Resume your task.                                             |
| `Run \`aq collaboration accept <id>\` before sending or waiting.`                 | Join, then try again.                                                         |
| `No partner is running. Do not wait on this thread: your claim holds a seat a partner may need. Prefer finishing your task or handing off.` | **Do not issue `aq message wait`.** Finish or hand off your task. Waiting with no running partner wastes a pool seat. |
| `Send with \`aq message send --thread-id <id>\`; wait with \`aq message wait --thread <id> --after <cursor>\`.` | Normal case. Proceed below. |

## Sending a message

Send to a specific member or broadcast to all other members in the thread:

```bash
# Send to a specific member:
aq message send \
  --thread-id <THREAD_ID> \
  --to-kind task --to-id task-b \
  -b "I'll implement --watch, you do --follow. I'm pushing a branch with SHA abc1234 for your review." \
  --client-key "turn-1-proposal" \
  --claim-epoch 7

# Broadcast to all other members (omit --to-kind/--to-id):
aq message send \
  --thread-id <THREAD_ID> \
  -b "All: --follow wins ordering. Please confirm." \
  --client-key "turn-2-broadcast"
```

Constraints on each message:

- `body` must be text, ≤ **4096 bytes**.
- At most **5 sends per minute** per sender in the thread.
- `client_key` is required for retries; a replay with the same key and same
  body returns the original message (no double-counting against the budget).

## Waiting for a reply

`aq message wait` is a **60-second long-poll**. It does not give back a
durable wait you can check later — the turn ends when it times out and the
thread remains available. That is sufficient for most collaboration turns:
the worker ends its turn, the daemon delivers a nudge when the next message
arrives, and the session resumes to read `--after <last-seq>`.

```bash
# Poll for up to 60 s for the next message after seq 42:
aq message wait \
  --thread <THREAD_ID> \
  --after 42 \
  --idempotency-key "wait-42-retry" \
  --claim-epoch 7
```

If you need a **durable** wait that survives turn-end and the session
restarts, use the generic wait registration:

```bash
aq wait register \
  --kind message \
  --ref <THREAD_ID> \
  --after-seq 42 \
  --timeout 3600 \
  --idempotency-key "durable-wait-42" \
  --claim-epoch 7
```

This keeps the task `IN_PROGRESS`, retains the claim and pool seat, and the
daemon scans for the condition. See [agent-waits.md](agent-waits.md) for the
full semantics of durable waits.

### Collaboration wait terminal reasons

When the daemon resolves a collaboration message wait (durable or long-poll),
the result carries a terminal state. The four collaboration-specific
reasons (listed in the [waits guide](agent-waits.md)) resolve the wait
**without success** and the thread is no longer available for nudges:

| reason                | meaning                                                                  |
|-----------------------|--------------------------------------------------------------------------|
| `peer_failed`         | At least one peer task transitioned to **FAILED** or **BLOCKED**.        |
| `peer_gone`           | All peer tasks are now terminal (`COMPLETED`), `ARCHIVED`, or `MISSING`. |
| `thread_closed`       | Thread state is `closed` (manual close, `budget_exhausted`, `members_below_two`) or `expired`, or the deadline passed. |
| `partner_not_running` | No peer task is running after the 120-second grace period.               |

In every case **do not re-register** the wait and do not try to send
messages on the thread. Continue with your own task.

## Closing the thread

Any member (or the operator) may close a thread:

```bash
# A worker closes its own view (member task must still be held):
aq collaboration close <THREAD_ID> --note "Goal settled: --follow wins ordering." --claim-epoch 7

# An operator removes a specific member task:
aq collaboration close <THREAD_ID> --remove-task task-b

# An operator closes every active thread in the project (rollback lever):
aq collaboration close --all-active --project "$AQ_PROJECT_ID"
```

Closing a thread does not change any member task's status. It sets the
thread state to `closed` and records the close reason (default `"closed"`).
A thread with `message_count == message_budget` is automatically closed with
`close_reason: "budget_exhausted"`.

## Error codes

All collaboration errors are returned in the standard command envelope with
`success: false`, an `error` field, and (where applicable) an `error.code`
field. The full set:

| code                        | when it occurs                                                              |
|-----------------------------|-----------------------------------------------------------------------------|
| `collaboration.invalid`     | Argument out of range (message body > 4096 B, goal > 1000 chars, etc.)      |
| `collaboration.not_found`   | Thread id not in this project.                                              |
| `collaboration.out_of_scope`| Thread involves a task outside the caller's project.                       |
| `collaboration.idempotency_conflict` | Replayed with a different body or args.                      |
| `collaboration.stale_claim` | `claim_epoch` does not match the pool session's live epoch.               |
| `collaboration.not_member`  | Caller's task is not a member of the thread, or recipient is not a member. |
| `collaboration.not_accepted`| Caller has not run `aq collaboration accept` for the current claim.       |
| `collaboration.closed`      | Thread state is `closed` or `expired`.                                      |
| `collaboration.message_too_large` | Body exceeds 4096 bytes.                                            |
| `collaboration.rate_limited`  | Sender already sent 5 messages in the last 60 seconds.                    |
| `collaboration.budget_exhausted`| Thread budget exhausted; further sends rejected.                         |

## Working loop — example

Two workers, `task-a` (writer A) and `task-b` (reviewer), sharing thread
`collab-9906-2026-09-26`:

1. **Supervisor:**

   ```bash
   aq collaboration create \
     --project "$AQ_PROJECT_ID" \
     --task task-a --task task-b \
     --goal "Writer A posts push SHA; reviewer B confirms or requests changes" \
     --deadline-minutes 60 \
     --budget 20 \
     --idempotency-key "review-9906"
   ```

2. **Worker A** (task-a):

   ```bash
   aq collaboration accept collab-9906-2026-09-26
   aq collaboration show collab-9906-2026-09-26   # read goal
   # ... implement, commit, push ...
   aq message send \
     --thread-id collab-9906-2026-09-26 \
     --to-kind task --to-id task-b \
     -b "Pushed branch aq/task-a. SHA: abc1234def. Please review." \
     --client-key "a-push-notice"
   # Wait for the reviewer verdict:
   aq message wait --thread collab-9906-2026-09-26 --after 1 --timeout 60
   # (turn ends; daemon nudges on reply)
   aq collaboration show collab-9906-2026-09-26 --after 1   # resume: read reply
   ```

3. **Worker B** (task-b):

   ```bash
   aq collaboration accept collab-9906-2026-09-26
   # ... read, check out SHA, run tests ...
   aq message send \
     --thread-id collab-9906-2026-09-26 \
     -b "LGTI. Minor: rename --watch to --watch-only in the docstring at src/api/watch.py:42." \
     --client-key "b-verdict"
   aq collaboration close collab-9906-2026-09-26 --note "Review complete: LGTM with one comment."
   ```

## Sharing large artifacts

The thread carries small messages (≤ 4096 bytes each). To pass code, diffs,
or files:

1. Commit to your branch and `aq git push`.
2. Post the branch name and commit SHA in a thread message.
3. The recipient `git fetch`es the branch and checks out the SHA.

Do not encode large text in message bodies — the body cap is 4 KiB and
messages are retained for 30 days after thread closure (tombstone 90 days).

## Capacity hold

`aq collaboration show` reports `capacity_hold: true` until the daemon
confirms at least two members are running and their sessions are idle.
While the hold is active, the `next_step` field tells workers
"do not wait on this thread." This protects pool seats: if no partner
is running, the thread cannot advance anyway, and waiting would burn a
worker's lease time without progress.

If you see `capacity_hold: true`, the correct action is:

- continue working on your task (do not poll), or
- hand off the task to another worker, or
- close the thread and create a fresh one when both sides are ready.

## Limits and retention

| limit                       | value                          |
|-----------------------------|--------------------------------|
| Members per thread          | 2–4                            |
| Message budget (per thread) | 1–40                           |
| Thread deadline             | 1–120 min (max and default)    |
| Body size (per message)     | 4096 bytes                     |
| Send rate (per sender)      | 5 messages / 60 s              |
| Read window (per show)      | 20 messages, 32 768 bytes total |
| Goal length                 | 1000 characters                |
| Content retention           | 30 days after thread closure   |
| Tombstone retention         | 90 days after thread closure   |
| Partner grace               | 120 s (before `partner_not_running`) |

## Post-deploy (operator)

After merging a branch that ships the collaboration feature, the operator
must:

```bash
# 1. Apply migration a00000000034 (collaboration threads table)
aq db upgrade

# 2. Restart the daemon
aq restart --no-dashboard

# 3. Re-seed worker profiles with the new collaboration grants
aq agent list-profiles
aq agent profile-reseed --profile-id worker-claude --grants-only
aq agent profile-reseed --profile-id worker-codex  --grants-only

# 4. Check aq-comms skill drift and refresh if needed
aq doctor --check skills.installed_drift
```

## Live smoke test

To verify end-to-end before relying on the guide:

```bash
# Operator creates a 2-member thread with both workers idle.
# Each worker:
#  - accepts
#  - shows (goal must be non-empty)
#  - sends one message (body ≤ 512 bytes)
#  - waits 60 s for the other's reply
#  - shows again and reads the partner's message
# Measure wall time between partner's send and acceptor's read:
#   target: p95 < 10 seconds

# Verify no pool-seat leak:
aq wait list --json | jq '.waits | map(select(.status=="waiting" and .kind=="message" and (.thread_id|startswith("collab-")))) | length'
# expect: 0 active waits still holding seats after thread closure

# Cleanup:
aq collaboration close --all-active --project "$AQ_PROJECT_ID"
```

## See also

- [agent-waits.md](agent-waits.md) — durable wait semantics and doctor checks
- [Agent coordination](../specs/design/agent-coordination.md) — scheduling
  architecture and how threads relate to it
- `src/skills/aq-comms/SKILL.md` — CLI reference for collaboration commands
- Migration `a00000000034_collaboration_threads.py` — table schema
