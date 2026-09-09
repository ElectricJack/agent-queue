# Escalations and the hourly digest

Setting up the one Discord channel AQ talks to, reading the hourly activity
digest, and answering the escalation threads that ask you to decide something.

If you have not met these ideas yet, read
[Messaging, digests and escalations](../concepts/messaging.md) first: it
explains what an escalation *is*. This page is the operator's procedure.

## What you are setting up

AQ posts two things and nothing else:

* an **activity digest**, once an interval, summarising what actually
  happened. It is silent when nothing did;
* an **escalation**: one post plus one thread, each time a project supervisor
  decides it needs a human to choose something. You answer by typing in the
  thread.

Both are optional. Turning either off, or running with no Discord bot at all,
changes nothing about how AQ works — escalations are still recorded, the
supervisor loop still runs, and the dashboard still shows everything. Only the
channel goes quiet.

> **Discord cannot control AQ.** The slash commands, task controls, approval
> buttons and per-project channels were removed in the single-channel cutover.
> A reply inside an escalation thread is the only thing Discord can send into
> AQ. See [what Discord no longer does](../concepts/messaging.md#what-discord-no-longer-does).

## Configure the channel

Everything lives under `discord:` in `~/.agent-queue/config.yaml`. The digest
and the escalation surface are enabled independently:

```yaml
discord:
  bot_token: "…"
  guild_id: "123456789012345678"
  channel_id: "123456789012345679"
  authorized_users:
    - "123456789012345680"
  digest:
    enabled: true
    interval_minutes: 60
    project_ids: []
    categories: ["work", "vcs", "budget", "system"]
    catchup_hours: 24
  escalation:
    enabled: true
    mention_user_ids: ["123456789012345680"]
    mention_role_ids: []
    reminder_minutes: 0
    supervisor_delivery_timeout_minutes: 15
```

| Setting | Ships as | What it does |
|---|---|---|
| `channel_id` | empty | The one destination, addressed by ID so a rename cannot break delivery. Nothing is posted until it is set. |
| `authorized_users` | empty | Accounts allowed to reply in an escalation thread. Empty means every account the gateway sees. |
| `digest.interval_minutes` | `60` | Window length, 15–1440. Changing it starts a new schedule generation rather than re-evaluating old windows. |
| `digest.project_ids` | empty (all) | The destination's visibility. Applied before any activity row is read. |
| `digest.categories` | all four | `work`, `vcs`, `budget`, `system`. `budget` and `system` have no producer yet — they exist so the filter has a stable vocabulary. |
| `digest.catchup_hours` | `24` | How far back one labelled catch-up window may reach after an outage, 1–168. A window older than this is dropped rather than posted as stale news. |
| `escalation.mention_user_ids` / `mention_role_ids` | empty | The **only** source of a ping. Applied to the first post of an incident and nowhere else. |
| `escalation.reminder_minutes` | `0` (off) | Optional nag interval for an unanswered incident, 5–1440 when set. |
| `escalation.supervisor_delivery_timeout_minutes` | `15` | How long an internal supervisor notice may stay undelivered before the watchdog raises an operational incident about it. |

Settings → Messaging in the dashboard edits the same values and shows a live
preview; the config file is the source of truth either way.

> **Note.** Enabling the digest does not enable escalations, and disabling
> escalations does not stop incidents being created — it stops them being
> *posted*. The dashboard's escalation inbox is unaffected by both switches.

## Check that it is working

`aq digest status` is the one health command. It answers "what is configured,
when does it next speak, and is anything stuck?".

```bash
aq digest status
```

It reports the destination and configuration generation, `next_evaluation_at`,
recent windows with their `suppression_reason`, `settings_errors` and
`warnings` (including project IDs that do not exist), the `cutover` status,
and `delivery_health` — how many digest windows and
`pending_escalation_deliveries` are pending, retrying or `unknown`.

To see what the next digest *would* say without sending it:

```bash
aq digest preview
```

The preview runs the same code path delivery uses, so it cannot disagree with
the real message. It reserves no window, sends nothing and advances no cursor,
so you may run it as often as you like. It returns either `would_send` with
the exact text, or `suppression_reason`.

> **Both commands are operator-scoped.** A worker session's token is refused
> with `out of scope: digest_status`. Run them from an operator shell or the
> dashboard.

## Reading a digest

One message, at most 1,200 characters, never split across posts:

```text
**Agent Queue — last hour**
2 completed · 1 progressed · 3 active
• agent-queue: completed — document the escalation guide (solid-grove.19)
• agent-queue: progress — pull request ready — fix the claim race (solid-grove.7)
• demo: started — add a health endpoint (demo.4)
+2 more · 1 open escalation · http://localhost:8081
```

Counts first, then at most three highlights, then the overflow count, the
number of open escalations and the dashboard link. If the highlights would
push the message over the limit, the least informative one is folded into the
`+N more` count rather than starting a second message.

### Why the channel is quiet

Silence is a feature, and it is recorded as durably as a message. Every window
that says nothing stores a reason, which `digest_status` and `digest_preview`
both surface:

| `suppression_reason` | Meaning | Is that right? |
|---|---|---|
| `no_activity` | Nothing durable happened and nothing is running | Yes |
| `idle_only` | Tasks exist but are waiting — queued, paused, dependency-blocked, waiting on a human — or the only news is an open escalation | Yes; waiting is not progress, and an escalation speaks on its own path |
| `all_filtered` | Work happened, but not in a project or category this destination may see | Check `digest.project_ids` and `digest.categories` |
| `already_reported` | Everything in the window was already said in an earlier one | Yes; a re-evaluated window never repeats itself |

Four things count as evidence, and nothing else does: a task completion, an
attempt that started, a task comment explicitly recorded as *progress*, and a
`pr_url` becoming available. Heartbeats, `updated_at` churn, layout rebuilds,
periodic metrics and repeated notifications are not progress. "Active" means a
task with a live, non-stale attempt right now — not a status column, so an
`IN_PROGRESS` container with no running session, a stalled session and a
worker parked on a human answer are all correctly counted as *not* active
([`src/database/queries/digest_queries.py`](../../src/database/queries/digest_queries.py)).

## Answering an escalation

When a supervisor escalates, the channel gets a post naming the project, the
task, the blocker, what was already tried, the exact decision needed, the
options if there are any, a dashboard link and the escalation ID. The
configured mention is attached to that first post and nowhere else.

**Reply in the thread underneath it.** That is the whole interface. What
happens next:

1. The gateway checks the message: not a bot, in a thread whose parent is the
   configured channel, from an account on `authorized_users`, and bound to a
   known incident by the receipt stored on its root post.
2. Your words are persisted as immutable evidence, and a message to
   `session:supervisor-<project>` is queued in the *same* transaction.
3. The thread gets one acknowledgement.
4. The supervisor reads the evidence and decides. Applying that decision —
   answering a question, resolving a gate, retrying or holding a task — is a
   separate, explicitly bound call it makes with `escalation_apply_reply`.

A reply never resolves a gate, edits a task or types into a worker's terminal
by itself. If you reply to an incident that already closed, the words are
still recorded, no supervisor work is queued, and the thread answers with
closed-state guidance saying nothing has reopened.

Messages that do not correlate are dropped silently — the channel is shared
with people, and answering every unrelated line would make it a chatbot.

### From the CLI or the dashboard instead

The same six commands back the CLI, the HTTP API and the MCP surface. All are
operator- or supervisor-scoped; a worker session's token is refused.

| CLI | HTTP operation | Who may call it |
|---|---|---|
| `aq escalation create` | `escalation_create` | The owning supervisor or a trusted core source; a replayed source/incident returns the same row |
| `aq escalation list` | `escalation_list` | Anyone in scope; shows current external-delivery status |
| `aq escalation get` | `escalation_get` | Anyone in scope; the authoritative incident, its immutable message history, deliveries and action receipts |
| `aq escalation reply` | `escalation_reply` | A dashboard human or a trusted external adapter only — never a supervisor, so supervisor text can never become human evidence |
| `aq escalation update` | `escalation_update` | The owning supervisor, with `--expected-revision` as the compare-and-set fence |
| `aq escalation apply-reply` | `escalation_apply_reply` | The owning supervisor, applying one verified reply through its bound question, gate or recovery target |

```bash
aq escalation list --states '["needs_human"]'
aq escalation get --escalation-id escalation-abc123
```

Refusals return `success: false`, a stable `error_code` and an operator-facing
message. The codes are `invalid_request`, `not_found`, `out_of_scope`,
`spoofed_identity`, `identity_conflict`, `stale_revision`, `invalid_state`,
`invalid_binding`, `human_evidence_required` and `action_failed`.

Caller identity, human status, verified actor, supervisor ownership, transport
authority and thread binding are never request fields — they come from the
server request principal. A trusted external adapter calls `escalation_reply`
under a service principal named `<transport>:<verified actor>`, and the core
derives the stored actor and transport from it.

`escalation_apply_reply` binds `--escalation-id`, `--reply-id`,
`--expected-revision`, `--action-kind`, `--target-id` and
`--idempotency-key`; `--decision retry|hold` is additionally required for
`task_recover`. The core verifies that the inbound reply belongs to the
escalation, that it arrived through the human boundary, and that the target is
the original question, gate or recovery incident. It reserves an action
receipt before invoking the guarded service, so reusing an idempotency key
returns the receipt and never repeats the action.

## Where escalations come from

Three sources, all of them a supervisor's judgement rather than an automatic
reflex:

* **A blocked task.** The shipped system playbook
  [`blocked-task-escalation`](../../src/prompts/default_playbooks/blocked-task-escalation.md)
  turns every `task.failed` event whose status is `BLOCKED` into exactly one
  message to `session:supervisor-<project>`, telling it to read the session
  log tail with `aq session logs` and decide: retry, hold, follow up, or ask
  the human. The playbook itself repairs nothing.
* **A worker question.** A question a supervisor cannot answer is escalated
  with `aq question escalate <question-id> --reason "…"`, which creates or
  reuses a durable escalation with the question as its source. A human
  answering directly with `question_answer` is refused: the human replies to
  the escalation, and the supervisor applies that evidence.
* **The supervisor delivery watchdog.** If an internal notice to a supervisor
  stays undelivered past `supervisor_delivery_timeout_minutes`, at most one
  operational incident per notice is raised
  ([`src/escalations/supervisor.py`](../../src/escalations/supervisor.py)). It
  reports unavailability only — its source kind is deliberately not applicable
  as approval for anything.

Dependency waits, in-flight retries and unchanged queue state never create an
incident.

## For contributors: how delivery stays exactly-once-ish

External delivery is driven from the `escalation_deliveries` outbox by
[`EscalationDeliveryService`](../../src/escalations/dispatch.py), ticked once
per orchestrator cycle when a bot is present and `discord.escalation.enabled`
is true.

| Delivery kind | When | Where |
|---|---|---|
| `root` | The incident exists and is not closed | One channel post plus the one thread it opens |
| `ack` | A human reply was persisted | In the thread, once per reply |
| `relay` | A correlated supervisor message | In the thread |
| `resolution` | The incident reached a terminal state | In the thread, then the root is edited and the thread archived |

Identity is durable, never in memory:

* one row per `(escalation, kind, generation)` through the outbox's unique
  `dedup_key`, so a replayed event, a gateway reconnect and a second daemon
  all converge on the same row;
* `claim_escalation_deliveries` leases a row, so one sender owns it at a time
  and an expired lease is reclaimed rather than duplicated;
* a restart rebinds from the stored channel, message and thread IDs — never
  from a task-thread naming heuristic.

An external send cannot be transactionally exactly-once with the database, and
the code does not pretend otherwise. Every message embeds an
`aq-esc:<dedup key>` marker (the digest uses `aq-dig:<hash>`); after an
ambiguous timeout the next attempt searches recent history for it. Found, the
delivery is recorded `sent` with that receipt and nothing is reposted. Not
found, the row becomes `unknown` — attention-needed, visible on
`escalation_get` and counted in `digest_status` — rather than being posted
twice.

A deleted root or thread earns one explicit replacement generation: at most
one may be pending at a time, and a closed incident never gets one, so a
resolution can never reopen work. A missing channel or a missing permission is
recorded as an actionable delivery fault; the daemon never creates a channel.
Retries are bounded (six attempts, 15 s → 30 min backoff), and the Discord
invalid-request rate guard holds sends rather than dropping them.

Escalations outrank digests. The digest pump asks how many escalation
deliveries are owed a send and, if any are, declines to claim at all — it does
not merely lose a race for the rate-limit budget
([`src/digest/dispatch.py`](../../src/digest/dispatch.py)).

The event bus publishes these versioned state hints after commit:
`escalation.created.v1`, `escalation.reply_received.v1`,
`escalation.updated.v1` and `escalation.delivery_status.v1`. Every payload
carries `version: 1`, `escalation_id` and `project_id` plus its own identity
and status fields. Consumers must reload authoritative state; an event replay
is never permission to repeat an external send or a recovery.

## For contributors: the inbound gate

[`DiscordEscalationIntake`](../../src/discord/escalation_intake.py) is the
only path a Discord message takes into an escalation, and `on_message` does
nothing else — there is no general channel chat, mention handling or
task-thread routing left to fall through to. The gateway also ignores every
message until the startup cutover pass reports `complete`, so a pre-cutover
conversation can never be half-migrated and half-live (see the
[migration runbook](discord-migration.md)).

Every one of these must hold, and each refusal is silent:

* the author is not this bot and not any bot — an acknowledgement must never
  acknowledge itself;
* the message is in a thread whose parent is the configured `channel_id`, so
  moving a thread out of that channel stops it correlating on its own;
* the author is on the `authorized_users` allowlist;
* the thread is bound to an incident by the confirmed receipt on its root
  delivery;
* the reply has text, is at most 16,000 characters, and carries a transport
  message ID.

The cheap checks run first against no binding at all, so ordinary channel
chatter costs no database query
([`src/escalations/intake.py`](../../src/escalations/intake.py) is the pure
decision table; the Discord half only observes the message).

Identity comes from the gateway, never the body: the adapter hands
`escalation_reply` an `ExecutionPrincipal.service("discord:<user id>")`, which
the core turns into `human:discord:<user id>`. A body carrying `actor`,
`human`, `verified_actor` or a project ID is refused outright, so a service
principal cannot impersonate a human or a supervisor through this path.

## For contributors: why nothing can ping you by accident

Two rules, both enforced in the renderer rather than trusted to callers
([`src/escalations/render.py`](../../src/escalations/render.py),
[`src/digest/render.py`](../../src/digest/render.py)):

* every interpolated string — a task title, a supervisor note, a human reply —
  is sanitised on the way in: mention tokens are removed, a bare `@` gains a
  zero-width space, backticks are dropped and a pasted traceback is cut to its
  first line;
* the configured mention is rendered by the mention policy alone, and only on
  an incident's *initial* root post. A reposted root and a resolution edit
  deliberately drop it, and a digest never mentions anybody at all.

## Troubleshooting

| Symptom | Diagnose | What to do |
|---|---|---|
| Nothing has ever been posted | `aq digest status` → `settings_errors`, `cutover` | `channel_id` is unset, or the cutover pass has not reported `complete`. Both are named in the output |
| `out of scope: digest_status` | Expected inside a worker session | Run it from an operator shell or the dashboard |
| The digest is silent while work is happening | `aq digest preview` → `suppression_reason` | `idle_only` and `already_reported` are correct. `all_filtered` means `digest.project_ids` or `categories` excludes the work |
| A digest window is `unknown` | `aq digest status` → `delivery_health` | The send was ambiguous and could not be reconciled, or the window aged past `catchup_hours`. AQ deliberately does not repost stale news |
| An escalation shows a delivery as `unknown` | `aq escalation get --escalation-id <id>` | Check the channel for the post; if it is genuinely missing, resolve or re-raise the incident deliberately rather than expecting a retry |
| A reply in the thread does nothing | Check `authorized_users`, and that the thread's parent is `channel_id` | Refusals are silent by design; the reason is in the daemon log |
| The channel is posting but the supervisor never acts | `aq message list --to-kind session --to-id supervisor-<project>` | The reply was persisted but its supervisor message is undelivered; the watchdog raises an incident about that after `supervisor_delivery_timeout_minutes` |
| An unanswered incident got a second post | Compare `generation` on the deliveries in `escalation_get` | Someone deleted the original post; a replacement generation is the intended response, and only one may be pending |

## Related pages

* [Messaging, digests and escalations](../concepts/messaging.md) — the
  concepts and vocabulary this page assumes.
* [Discord migration runbook](discord-migration.md) — moving an installation
  that predates the single-channel model.
* [Discord commands](discord-commands.md) and
  [replacement checklist](discord-replacement-checklist.md) — the ledger of
  what each retired Discord surface became.
* [Module catalog: messaging, digests and escalations](../reference/modules/communications.md)
  — every module behind this page, one row each.

## Source and tests

[`src/digest/`](../../src/digest/), [`src/escalations/`](../../src/escalations/)
and [`src/discord/`](../../src/discord/), configured by the `discord:` section
of [`src/config.py`](../../src/config.py). The spec is
[the Discord simplification implementation spec](../superpowers/specs/2026-09-08-discord-simplification-implementation.md).

```bash
aq test tests/test_digest.py tests/test_digest_dispatch.py tests/test_digest_commands.py \
  tests/test_escalation_delivery.py tests/test_escalation_intake.py \
  tests/test_discord_escalation_transport.py tests/test_discord_docs.py
```
