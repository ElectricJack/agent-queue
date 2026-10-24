---
tags: [spec, system, messaging-and-human-asks]
system: 9
status: current
as_of: 2026-10-23 (dev 52d2efe4cb)
---

# System 9: messaging-and-human-asks

This is a **current** system spec, not a design record. It describes what the
code at `dev` 52d2efe4cb does. Where it and the code disagree, the code is
right and this file should be fixed. Some older specs it links to carry the
`aq:historical` banner: [`../messaging/`](../messaging/README.md),
[`../design/messaging-rework.md`](../design/messaging-rework.md),
[`../implementation/messaging-rework.md`](../implementation/messaging-rework.md)
and
[`../../superpowers/specs/2026-09-08-discord-simplification-implementation.md`](../../superpowers/specs/2026-09-08-discord-simplification-implementation.md).
They are kept for the reasoning behind the design, not as a description of
what runs today.

messaging-and-human-asks is system 9 of the 18 in the system inventory
(`~/.agent-queue/vault/projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, *approved*, revision 1). The inventory merged
human asks, escalations, the digest and the reports into this one system
because they share one delivery engine and one set of recipients. The parent
vision and system map (`2026-10-20-agent-queue-the-vision-and-the-system-map.md`,
review `rev-smart-nexus`) was *withdrawn*. Sibling specs live in the same
directory as `<system-slug>.md`.

## 1. Purpose and problem

Agent Queue has three kinds of correspondent: agents (workers and
supervisors, each a tmux session), the daemon, and Jack. They need to reach
each other reliably, and every one of them is often not listening. A worker
is busy mid-turn, a supervisor has gone to sleep, and Jack is away from the
keyboard. Without a system that owns this, each feature would invent its own
way to say something: a review notifier here, a gate prompt there, a
free-text escalation somewhere else, each with its own idea of who may
answer and what an answer does.

This system owns every message between agents, the daemon and Jack:

- **Internal mail.** A durable message store and inbox, and a delivery
  engine that wakes an idle agent session with a one-line pointer.
  Supervisor-to-worker mail rides the same rails as everything else.
- **Conversation.** Message threads, collaboration threads, and Jack talking
  to a supervisor from the Discord channel.
- **Asking Jack.** Escalations (durable incidents that need a human) and
  human asks (one durable, authority-checked question per thing that needs
  Jack, with buttons, answer ingestion and batch scope).
- **Telling Jack.** One Discord channel, a deterministic hourly digest, an
  optional supervisor-authored hourly report and a daily morning report,
  each carrying at most one dashboard link.

The hard problems it solves are durability (nothing is lost when the reader
is away), safety (a message is never typed into a human's half-written
prompt, and a reply never acts on its own), idempotency (a send whose result
is unknown is never posted twice) and authority (what Jack typed, not what
an agent says Jack meant, is what gets applied).

## 2. How it works today

### 2.1 The packages

The system spans many packages, and some of their names overlap in ways that
mislead a newcomer. In particular, `src/messages/` and `src/messaging/` are
different things, and `MessageDelivery` (external) is not
`MessageDeliveryEngine` (internal).

| Package | What it owns |
|---|---|
| `src/messages/` | The **internal** mailbox delivery engine: `service.py` (`MessageDeliveryService`), `delivery.py` (`MessageDeliveryEngine`), `session_lens.py` (`SessionLens`). Rows in `messages` become tmux nudges. |
| `src/messaging/` | The chat-platform adapter seam. `factory.py` returns the Discord adapter or a null adapter (telegram raises `ValueError`); it is called from `src/main.py`. `port.py` (`MessagingPort`, `RichNotification`) is dead code used only by its test. |
| `src/delivery/` | The **external** outbound pump: `DeliveryScheduler` (`scheduler.py`), `dispatch_batch` (`dispatch.py`) and `MessageDelivery` (`message.py`). |
| `src/conversations/` | Discord-to-supervisor conversation: intake, preconditions, limits, backfill, hourly maintenance and `DurableConversationOutbox`. |
| `src/discord/` | The bot, inbound routing, buttons, the cutover, the outbound transport, the rate guard and the render budgets. |
| `src/escalations/` | Presentation and transport for escalation incidents: facts, plan, render, state, dispatch, intake, auto-resolve, sweep and the supervisor watchdog. |
| `src/asks/` | Human asks: source adapters, the decision pipeline, intake, delivery, cutover controls, reconciliation and the legacy reset. |
| `src/digest/` | The deterministic digest and the supervisor-authored digest. |
| `src/reports/` | The hourly and morning reports: briefs, snapshots, validation, the morning-due emitter. |
| `src/notifications/` | About 35 typed `notify.*` events. Their only consumer is the dashboard WebSocket. |
| `src/remote_links.py`, `src/dashboard_paths.py` | Deep links from any post to the dashboard. |

The command surface is `src/commands/message_commands.py`,
`escalation_commands.py`, `digest_commands.py`, `discord_commands.py` (only
`discord_purge_channel`), `report_commands.py`, `conversation_commands.py`,
`ask_commands.py` and `ask_communication_commands.py`. As with every system,
state changes go through `CommandHandler`.

### 2.2 Internal mail: the message store

Every internal message is one row in `messages` (`src/database/tables.py`).
Its id is `msg-<uuid>`, and a global `created_seq` orders it. The main
columns are:

- **Addressing.** `from_kind`/`from_id` (session, user or system) and
  `to_kind`/`to_id` (session, task, profile or user), plus `project_id`.
- **Content.** `subject`, `body`, `priority`, `thread_id`, `reply_to_id` and
  `body_kind`, which marks special rows such as `conversation_input`.
- **Delivery state.** `delivered_at`, `read_at`, `last_notified_at`,
  `notification_attempts`, `pane_open`, `archive_after_inject` and
  `archived_at`.
- **`via`.** How the row was consumed: `platform`, `read`, `prime`,
  `expired-task-notice`, `expired-source-notice` or `transcript_tail`. The
  column comment in `tables.py` lists an older set. Nudges record attempts
  without consuming mail; injection leaves `via` and `read_at` unset.

**Sending.** `_cmd_message_send` (`message_commands.py`) binds the sender to
the caller's principal, so a worker cannot send as another session (a stale
claim is refused with `stale_claim`). It then calls `create_message`
(`src/database/queries/message_queries.py`), emits `message.sent` and returns
`queued`. "Queued" means the row is durable, not that anyone has seen it.
Mail for a project supervisor that is disabled is rerouted to
`supervisor-global` (`_supervisor_recipient`).

**Recipients mean different things:**

- `session:<id>` is one live agent session, by immutable session id or by
  runtime name. `supervisor-<project>` is a per-project mailbox, mapped to
  the tmux session `n-supervisor--<project>`. It is not an alias for the
  global supervisor.
- `task:<id>` reaches whichever session currently holds the task's claim.
- `profile:<id>` is a broadcast inbox read by `aq message inbox` or
  `aq prime`. Nobody is woken for it. `profile:supervisor` is the common
  case.
- `user:<id>` is a human. These rows go out through the chat platform and
  are marked `via=platform`.

### 2.3 Waking a recipient: the delivery service

The database is the queue. The event bus only hints that there is work.

`MessageDeliveryService` (`src/messages/service.py`) is built in
`Orchestrator.__init__` and started at the end of `initialize`. It runs
beside the scheduler cycle, not inside it. It coalesces `message.sent`
events into one bit and runs a pass at least every
`messages.delivery_interval` seconds (default 5.0). A pass on startup and the
periodic pass recover lost events and restarts. A throttled fallback call in
the cycle remains for callers that never ran `initialize`. These are the
fixes from the 2026-10-07 missed-wake RCA (vault spec
`2026-10-07-missed-supervisor-message-wake-up-rca-and-bounded-delivery-f.md`,
review `rev-keen-current`, *approved*).

`MessageDeliveryEngine.run_delivery_pass` (`src/messages/delivery.py`) holds a
lock so passes never overlap. It first runs three sweeps (expiry, redirect
and a backlog alert above 50 pending rows). It then visits each recipient
mailbox once, with a 120-second budget per recipient that includes a cold
start. A failure or timeout leaves that mailbox's rows pending and lets the
other recipients progress.

For one recipient, `_deliver_recipient` takes up to 10 pending rows (skipping
rows parked as `conversation-queued`) and asks `SessionLens` what the
recipient is doing:

| Activity | What happens |
|---|---|
| **busy** (activity in the last 30 s) | Skipped. The rows stay durable. |
| **idle** | `claim_message_notification` applies a 60 s → 300 s backoff, then the provider nudges. |
| **sleeping** | Supervisors only: `ensure_started` cold-starts the session. Task sessions are never spawned by messaging. |
| **absent** | After 24 hours the rows are parked. They are re-addressed to the sender only when the sender is a user. |

After three or more failed notification attempts the engine raises an alert
(uuid5-keyed, so it is raised once) to `supervisor-global` or to
`user:dashboard`. Retired or stopped session ids are refused, so a reused
runtime name cannot receive stale mail.

**The nudge is a pointer, not the message.** The engine types one line
asking the agent to run `aq message status <id> --json`. `TmuxProvider.nudge`
(`src/sessions/tmux.py`) takes a lock and defers if a human typed recently.
It checks twice that the composer is empty (refusing on an attached client,
copy mode, an unknown prompt prefix or invalid cursor coordinates), types,
waits for the text to land, and submits. Each refusal is a named
`NudgeDeferred` reason (`src/sessions/provider.py`). A confirmed nudge records
a notification attempt and leaves the row pending with no consumption stamp.
A refused nudge also leaves it pending for the next pass. It
is never typed into a human's draft.

Delivery is **at least once**. A crash after the keystrokes but before the
delivered compare-and-set, or a manual inbox read racing the engine, can
repeat a pointer. Handlers check durable task state before repeating
anything consequential.

### 2.4 Reading, replying and waiting

- **`aq message inbox`** lists pending rows and changes nothing.
  **`--inject`** returns the bodies and marks them delivered with no `via`
  and no `read_at` (`message_commands.py`). An injected message is consumed
  but not read.
- **`aq message status <id>`** returns the durable body even after it has
  left the inbox, and stamps `via=read` and `read_at`.
- **`aq prime`** consumes pending mail with `via=prime`.
- **`aq message reply`** threads a reply. A reply to a Discord conversation
  input goes to `_cmd_supervisor_inbox_reply` (§2.7). A reply in a
  collaboration thread takes its own path. System senders cannot be replied
  to; to answer the system supervisor, send to `session:supervisor-<project>`.
- **Transcript-tail fallback.** `check_reply_timeouts` (`delivery.py`) recovers
  only human-origin messages, never agent/system messages or explicit decision
  kinds. After the default 120-second timeout it selects prose within the
  correlated window ending at `delivered_at + reply_timeout × 3.0`, refusing
  overlapping conversations. An atomic insert locks the original so an explicit
  reply and recovery cannot both win. The receipt uses `via=transcript_tail`.
- **Collaboration threads** (`collaboration_threads`, `collaboration_members`,
  `collaboration_messages`) carry multi-party discussion. `aq message wait`
  waits on these threads only; it is distinct from durable verified waits.
- **Durable message waits.** Version-2 registration binds a thread to an exclusive
  `created_seq` cursor and source authorization (`src/wait_runtime/`). Routine
  mail is held while waiting; outcome/decision notifications can reach the owner.
  Acknowledgement delivers held mail atomically. A supervisor answer must reply
  on the original thread; unrelated guidance cannot satisfy the condition.
  Human waits require human evidence. See [agent waits](../../guides/agent-waits.md).
- **Close guard.** `messages.pending_before_close` is a returned error code,
  not a configuration setting. Closing a session with unread mail is refused
  (`src/commands/session_commands.py`).

### 2.5 The external outbox

Anything that leaves the daemon for Discord goes through a durable outbox
row. A transport call never runs inside a command's transaction.

**`outbound_deliveries`** is the shared outbox for asks, conversation replies
and routine reports. Each row has a unique `dedup_key` and marker, a status
(pending, sending, sent, retry, unknown, cancelled), a lease and attempt
counter, and a receipt. A `CHECK` constraint makes `sent` impossible without
a receipt.

**`DeliveryScheduler`** (`src/delivery/scheduler.py`) is the single bounded
pump. Each cycle calls its `tick()` (bound in `main.py` by
`_bind_report_outbox`). It claims due `digest_windows` rows and
`outbound_deliveries` rows together and hands them to `dispatch_batch` (a
120 s lease, at most 20 per batch). Escalation rows have priority:
the scheduler declines to claim anything while escalation deliveries are due
or the rate guard is hot. `ScopedOutboundAdapter` routes each outbound row by
owner: `ask`, `conversation` or `morning`.

**Idempotency by marker.** `MessageDelivery.deliver` (`src/delivery/message.py`)
appends a zero-width marker to every post. When a send's outcome is
ambiguous (a timeout after the request left), the worker searches recent
channel history for that marker. If it finds it, the row becomes `sent` with
that receipt. If it cannot decide, the row becomes `unknown` and is **never
reposted**. An operator resolves it.

**Escalations still have their own outbox and pump.** `escalation_deliveries`
and `EscalationDeliveryService` (§2.9) predate the shared outbox, with the
same lease, receipt and marker rules (`aq-esc:` markers, six attempts with
15 s to 30 min backoff). Moving them onto the shared outbox is §6.1.

### 2.6 The Discord channel

There is **one channel**. `DiscordConfig` (`src/config.py`) names it by
snowflake in `discord.channel_id`, together with `bot_token`, `guild_id`,
`authorized_users`, the default `project_id` and the rate-guard thresholds.
The older per-purpose channel names survive only as private input to the
cutover.

**Startup.** `setup_hook` unregisters the six retired slash commands,
registers `/delete-task` (preview, then confirm, with
`deletion_actor=human:discord:<id>`) and wraps the allowlist check.
`on_ready` runs `run_discord_cutover` (`src/discord/cutover.py`), which is
idempotent and never deletes anything. It inventories legacy agent
questions and gate cards into `escalation-<qid>` incidents. `main.py` wires
the `DiscordEscalationTransport` only after the cutover reports complete and
a channel id is set. Without them, escalations and the dashboard still
work, but nothing is posted.

**Inbound.** `on_message` drops bot messages, messages from users outside the
allowlist and messages from before the cutover. It then calls
`DiscordInboundRouter.route` (`src/discord/inbound.py`) under the service
principal `discord-gateway`:

1. If the destination is in ask **channel mode** (§2.10), every message goes
   to `ask_intake`. There is no fallthrough.
2. Otherwise, with conversations off, only escalation thread intake runs.
3. With conversations on, escalation intake runs first, then
   `classify_conversation`, then `supervisor_inbox_post` (§2.7).

Ignored messages are counted by reason (`IgnoreCounter`), and `digest_status`
reports the counts. Button presses arrive through `on_interaction`: custom
ids prefixed `aqask:` go to the `ask_button` command, and escalation buttons
(`aqesc`) go to `EscalationButtonPress`. A button carries an index or token,
never a label. The option text is re-read from the database.

**Outbound.** Every post goes through `DiscordEscalationTransport`, which
classifies errors as retryable, ambiguous, unavailable, missing or rejected.
An `OutboundTokenBucket` allows 20 writes per minute, edits included.
`InvalidRequestTracker` (`src/discord/rate_guard.py`) counts invalid requests
over a 10-minute window, warns at 1000 and halts writes at 8000. Escalation
edits are coalesced to one per 30 s.

**Size budgets.** `src/discord/render_budget.py` gives every post kind a line
and character budget. Examples: an open escalation root is 2 lines and 400
characters, a review-ready post 2 lines and 300, the digest 3 lines and 600,
a chat reply 1900 characters, and the transport ceiling 2000. `link_line` is
the single place a link is built, and `compose` never cuts the marker or the
link. `sanitise()` strips `@everyone` and user mentions from every
interpolated field. Only an initial escalation root mentions the configured
users and roles.

### 2.7 Talking to a supervisor from Discord

With `discord.conversation.enabled` (default off; it also needs an
allowlist, a guild and a channel), a plain message in the channel becomes a
conversation with the supervisor. The route fails closed on an empty
allowlist.

1. `src/conversations/` checks intake and preconditions. Limits
   (`limits.py`): 4000 characters per input, 10 inputs per author and 60 per
   channel per 600 s.
2. `supervisor_inbox_post` (`src/commands/conversation_commands.py`) records
   the input in `conversation_inputs`, deduplicated on
   `(transport, external_message_id)`. Text is nulled after 30 days.
3. `src/asks/intake.py` inserts a message `msg-<input_id>` with
   `body_kind=conversation_input`, addressed to the supervisor, or to
   `conversation-queued` if no supervisor can take it yet. A cycle step
   (`route_queued_conversation_inputs`) retries the routing.
4. The supervisor answers with `aq message reply`. The reply (at most 1900
   characters) goes through `DurableConversationOutbox` into
   `outbound_deliveries`.

`supervisor_conversations` tracks each conversation. On bot `READY`, a
backfill reads up to 24 hours or 1000 missed messages
(`conversation_backfill_cursors`), and `conversation_intake_gaps` records
what it could not cover. `ConversationMaintenance` runs hourly.
[`../../guides/discord-conversations.md`](../../guides/discord-conversations.md)
is the operator guide.

### 2.8 Links to the dashboard

Every post carries at most one link, to the object it is about.
`src/dashboard_paths.py` gives every object a `/focus/...` path. Its `SCHEME`
table marks each kind live (task, session, report) or planned (review,
escalation, batch, inbox, conversation). `/settings/` is never a link
target.

`remote_links.dashboard_link_base` turns a path into a URL. It uses
`dashboard.server.public_url`, or else a tailnet bind that Tailscale
confirms. Otherwise it returns a reason code (`local_bind`,
`bind_not_tailnet`, `tailscale_unavailable`, `tailnet_address_mismatch`) and
no link. `DashboardLinkResolver` never raises. It caches a link for 300 s and
an absence for 30 s.

### 2.9 Escalations

An escalation is a durable incident raised when a human must decide
something. The core owns the incident: its state, a revision for
compare-and-set, an immutable message history and action receipts.
`src/escalations/` owns only presentation and transport. The operator guide
is [`../../guides/escalations.md`](../../guides/escalations.md), and the
creation and delivery contract is
[`../escalation-delivery.md`](../escalation-delivery.md).

**Sources.** Supervisors and playbooks raise incidents through
`escalation_create` (the `ci-main-sentinel` bundle and
`supervisor-failure-triage`, for example), and so does the fidelity review.
Several producers call `db.create_escalation` directly and bypass the
command:

| Caller | `source_kind` |
|---|---|
| `src/sessions/questions.py` | `question`, `question_unanswered`, `question_delivery`, `question_resume` |
| `src/integration/repair.py` | `integration_repair` |
| `src/integration/repository_incidents.py` | `retained_repository_recovery` |
| `src/integration/stall_alerts.py` | `integration_train_stall` |
| `src/providers/availability_service.py` | `provider_availability` |
| `src/escalations/supervisor.py` | `supervisor_delivery` (the watchdog) |
| `src/discord/cutover.py` | `question` (legacy gate cards) |

**Data model.**

- `escalations` holds one row per incident, unique on
  `(project, incident_key)` and on `(project, source_kind, source_identity)`.
  It records the decision requested, up to five choices, a severity label,
  the state, the revision and a terminal outcome. States move from
  `needs_human` to `reply_received` (only through `accept_escalation_reply`),
  to `resolving`, to `resolved`. Any open state can move to `cancelled` or
  `stale`.
- `escalation_messages` is immutable, with direction inbound, outbound or
  system.
- `escalation_actions` is the leased operation ledger with receipts. Asks
  reuse it with `action_kind=ask_operation`.
- `escalation_deliveries` is the escalation outbox.

**Raising.** `create_escalation` inserts with on-conflict-do-nothing, so a
replay returns the same incident, and a mismatched identity raises
`EscalationConflict`. The root outbox row `<id>:root:0` is written in the
same transaction. `supervisor_delivery` notices are coalesced under an
advisory lock and only reach the supervisor inbox. For a free-text source,
`escalation_create` also publishes a `free_text` ask (§2.10).

**Delivering.** Each cycle ticks `EscalationDeliveryService`
(`src/escalations/dispatch.py`). It does nothing unless
`discord.escalation.enabled`. Otherwise it re-plans open incidents and pumps
due rows. `plan_deliveries` (`plan.py`) is a pure function of the incident,
its deliveries and messages. It plans the root, one ack per inbound message,
one relay per outbound message, and a resolution edit once the incident is
terminal. With `discord.escalations.stateful` on, it also plans state edits
(answered or stale). Claims skip paused projects and projects in ask
cutover. A deleted root is reposted as a new generation, unless the
incident is already answered or terminal.

**Replying.** A thread reply or a button press goes through intake
(`classify_inbound` in `src/escalations/intake.py` is pure and compares
gateway-observed author and thread facts with the allowlist). It reaches
`escalation_reply`, which stores an immutable inbound message and a
supervisor inbox message in one transaction. **A reply never acts on its
own.** The owning supervisor applies it with `escalation_apply_reply`, which
now decides through the ask service. Only `question`, `gate` and
`task_recovery` incidents can be applied. Every other kind is free text.
Then `escalation_resolve` closes the incident with an outcome sentence, and
the planner collapses the post.

**Cleaning up.**

- **Auto-resolve** (`autoresolve.py`) closes an incident when its gate
  resolves or its task reaches `COMPLETED`. It cancels after 7 idle days,
  except for gates. The whole pass needs `discord.escalations.stateful`,
  which defaults to off.
- **Sweep** (`sweep.py`, `aq escalation sweep`) is a one-shot backlog pass,
  dry run by default. It reuses the auto-resolve rules, so the two cannot
  disagree, and adds others (withdrawn reviews, archived tasks, delivered
  notices, inactive projects, retired sources, triage). It deliberately does
  not expire stale incidents: Jack chose supervisor triage over "mark all
  obsolete".
- **The watchdog** (`supervisor.py`) queues reminders for answered
  incidents. When supervisor inbox notices go undelivered for
  `escalation.supervisor_delivery_timeout_minutes` (default 15), it opens a
  `supervisor_delivery` incident and resolves it when the backlog drains.
  `supervisor_delivery` cannot be applied, so the watchdog can never approve
  work.

**Severity is a label.** Nothing routes, throttles or mutes by severity, and
escalations have no quiet hours.

### 2.10 Human asks

A **human ask** is the one durable record of a thing that needs Jack: a
review verdict, a gate, a proposal, an integration decision, a wait, an
agent question, a recovery or a free-text escalation. It replaced several
separate paths, each with its own notifier and its own idea of authority.
The design is the vault spec
`2026-10-11-one-place-to-ask-jack-cohesive-human-communication-in-aq.md`
(review `rev-fair-ridge`, *approved*, revision 3), and the implementation
spec is `2026-10-11-one-place-to-ask-jack-implementation-specification.md`
(review `rev-sharp-zenith`, *approved*, revision 1, baseline 2962314f1).
Epic `stark-forge-10` (H01–H08, PR #1555) is COMPLETED. The rollout runbook
is [`../../guides/human-asks-rollout.md`](../../guides/human-asks-rollout.md),
and the acceptance map is
[`../../reference/human-asks-implementation.md`](../../reference/human-asks-implementation.md).

**Data model** (migrations `a00000000096`–`a00000000098`):

- `human_asks` holds one row per source, unique on
  `(project, source_kind, source_key)`. It carries a source fingerprint and
  a generation fingerprint, the counters `decision_generation`,
  `display_version` and `feedback_version`, the decision and application
  states, and who acts next.
- `ask_source_aliases` redirects old source keys.
- `ask_decision_previews` holds previews with a 600 s time-to-live.
- `ask_decisions` holds the immutable decision packet. A database trigger,
  `human_ask_immutable_v1`, enforces that.
- `ask_reconciliation_cases` holds conflicts for an operator.
- `ask_external_bindings` holds the one Discord root post per ask, with a
  write fence and the mode generation it was posted under.
- `ask_action_tokens` holds button tokens, valid for 7 days.
- Asks reuse `conversation_inputs` (inbound), `escalation_actions` (the
  operation ledger) and `outbound_deliveries` (outbound).

**Sources.** `AskCommandsMixin._ask_service()` (`src/commands/ask_commands.py`)
builds an adapter registry: reviews, standalone specs, gates, proposals,
integration, waits, questions, recoveries, free text and batch scope. The
legacy commands for gates, playbook approvals and escalations now decide
through asks (`_legacy_ask_decision`).

**Deciding.** A decision is prepared (`ask_prepare_decision`), then decided
(`ask_decide`). Deciding requires an idempotency key, passes the adapter's
validation, writes the immutable packet and reserves an operation, all in
one transaction. Locks are taken source first, then ask, then binding.
Applying (`_apply_ask_decision`) depends on the source:

- **Most sources** go through `source_operations.apply`. An operation that
  already succeeded replays its result. One whose state is unknown is
  reconciled, never retried blindly. Otherwise it re-authorizes, checks the
  generation and runs under a 120 s lease. In a paused project the decision
  is saved and the result says "decision saved without resuming work".
- **Reviews** go through `ReviewOperations.apply`: filing follow-up tasks for
  feedback, running the review playbook and finalizing.
- **Standalone specs** go through `ingestion.dispatch`, which re-emits
  `spec.approved` every 30 s until spec ingest records its task.

**Answers from Discord.** In channel mode, `ask_intake` admits a message
under the gateway principal. The supervisor then interprets it with
`ask_apply_input` (`src/asks/intake.py`). That command requires the human's
stored words to parse, through `clear_intent`, to the option being applied.
Otherwise it refuses with `ask.clarification_required`. **A supervisor
interprets, but it cannot turn free text into a decision.** A button press
goes through `ask_button` with a verified token.

**Delivering asks.** `src/asks/delivery.py` enqueues ask posts into the
shared outbox. It refuses when any of these hold:

- the project is paused;
- the destination is not in channel mode;
- the Discord config fingerprint (guild, channel, allowlist and project) has
  changed;
- the allowlist is empty;
- the ask is not public.

With no public dashboard URL it opens a repair case and sends nothing. The
first post is a root without buttons. The buttons arrive on the following
root edit. Recovery is read-only: an unknown send is reported, not
reposted.

**Cutover modes.** Each destination has a mode row with a generation:
`legacy`, `shadow`, `dashboard`, `transferring`, `channel` or
`delivery_disabled`. `src/asks/transport_controls.py` changes it by
compare-and-set under an advisory lock. Only these transitions are allowed:

| From | To |
|---|---|
| `legacy` | `shadow`, `delivery_disabled` |
| `shadow` | `dashboard`, `delivery_disabled` |
| `dashboard` | `transferring`, `delivery_disabled` |
| `transferring` | `transferring`, `channel`, `delivery_disabled` |
| `channel` | `transferring`, `delivery_disabled` |
| `delivery_disabled` | `transferring`, `delivery_disabled` |

Switching to `channel` requires reconciliation diagnostics with no blockers,
a matching inventory hash, evidence, and legacy controls disabled. The first
switch sets a 30-day compatibility deadline that is never extended. Once a
destination is transferring, in channel mode or disabled, the legacy
escalation pump and the review notifier stop posting there
(`legacy_human_delivery_blocked`), and old escalation buttons answer "This
request moved…". `aq ask cutover-status` reports the mode
([`../design/human-ask-cutover-status.md`](../design/human-ask-cutover-status.md)).

**Reconciliation and reset.** `src/asks/reconcile.py` scans the old sources
(reviews, gates, escalations, agent questions, playbook runs) and projects
aliases for incidents the direct producers created. Its diagnostics list the
blockers a channel switch must clear. On 2026-10-19 Jack chose to discard
the history instead of reconciling it ("Blow it all away and start
fresh"). `ask_legacy_reset` (`src/asks/legacy_reset.py`) cancels the legacy
incidents, actions, inputs and outbox rows with
`terminal_outcome='legacy_discarded'` and re-asks only reviews still in
review ([`../design/human-ask-legacy-reset.md`](../design/human-ask-legacy-reset.md)).

**Authority.** Gateway commands are callable only over the local transport.
`ask_reply` is local only. Workers have no human-ask authority
(`src/api/scope.py`). The principals that may decide are the local
operator, a live supervisor (for verified input only), a verified button and
the Discord allowlist.

**What is live.** The code is complete and tested on fixtures. With no mode
row, a destination reports `legacy` at generation 0. Production enablement
(H08-OPS) and cleanup (H09) are open (§6.1).

### 2.11 The deterministic digest

The digest is an installation-wide summary of each hour, posted to the
channel. It makes no LLM calls. Its pure core does no I/O and reads no
clock, and it always yields to escalations.

- **Schedule** (`src/digest/schedule.py`). Windows are snapped to a UTC grid
  of `discord.digest.interval_minutes` (default 60). `config_generation`
  hashes the interval, projects, categories and catch-up horizon, so a
  config change starts a fresh series and never replays the old one. A gap
  becomes one catch-up window bounded by `catchup_hours` (default 24). Two
  daemons collide on the unique key of `digest_windows` instead of posting
  twice.
- **Eligibility** (`eligibility.py`). A window speaks only if it has a
  completion, a start, a progress note or a live attempt in a selected
  category. Otherwise it is stored as silent with a reason (`no_activity`,
  `idle_only`, `all_filtered`, `already_reported`). Facts already reported
  are dropped. Provider and reroute facts rank first.
- **Rendering** (`render.py`). One message of at most 3 lines and 600
  characters: counts, then highlights, then the needs-you link.
  `build_digest` (`aggregate.py`) is the single entry point shared by
  `aq digest preview` and real delivery.
- **Delivery.** `DigestScheduleService.tick` (`src/digest/dispatch.py`)
  reserves the window row and stores either a payload or a suppression
  reason. In production its own pump is off, and `DeliveryScheduler` (§2.5)
  sends it with an `aq-dig` marker.

**Supervisor-authored digest (phase P3).** With
`discord.digest.supervisor_authored` (default off), windows follow
`cadence_minutes` (default 120) and quiet hours (22:00–07:00) suppress the
post but not fact collection. An unchanged facts hash skips the window, with
a quiet line after three skips. A window that speaks waits
`author_fallback_minutes` (default 10) for `aq digest post`. If nothing
arrives, the deterministic text goes out.

### 2.12 Hourly and morning reports

Both reports follow one pattern: the daemon reserves the slot, freezes a
brief of facts and a deterministic **fallback text**, and only then wakes a
supervisor to author it. The fallback goes out if the supervisor misses the
deadline; for full-fleet hourly reporting it is only a one-line unavailable
notice, never a concatenation of the evidence. Requests are rows in
`supervisor_report_requests` (kind `hourly`,
`morning` or `digest`), with the frozen brief, its hash and the fallback.
Supervisor policy (the playbooks, what to write) is
[supervisors-and-policy](supervisors-and-policy.md). The mechanism is here.

**Hourly** (`reports.hourly`, default off;
[`../../guides/supervisor-hourly-reports.md`](../../guides/supervisor-hourly-reports.md)).
When a digest window would speak, the digest dispatcher creates an hourly
request with a 24 KiB brief instead of posting directly (`src/reports/hourly.py`).
It emits `digest.window_ready`. The `supervisor-hourly-report` playbook calls
`report_reconcile`, and the supervisor runs `aq report brief` and
`aq report submit`. The brief is input only: bounded facts, counts, links and
active work, never delivered prose. The durable wake instructs the supervisor
to write a short first-person developer update in plain English: what changed
since the last update, highlights, anything blocked or needing Jack, and what
comes next. It prioritizes meaningful changes and human decisions, combines
related evidence and omits repeated noise; it does not invent progress or plans
when evidence is missing. Only the submitted narrative becomes the report body,
with server-owned links and marker. Submit rejects URLs, validates references against the frozen
brief and requires a `delivery:` reference for any claim that something shipped.
The enforced 600-character limit includes the server footer and marker. A grace
period of 5 minutes and a limit of 12 per day apply.
If a full-fleet hourly author is unavailable, late, capped or busy, the pump
sends only “No report this hour: the supervisor update is unavailable.” This
also applies when an authoring activation is missing or disabled. Quiet hours
still suppress author wakes; they do not enable a fact dump. Feature-off and
restricted-destination deterministic digests retain their existing behavior.
Reservations, deadlines, visibility checks and one-post idempotency are unchanged.

**Morning** (`reports.morning`, default off, at 07:00 in `reports.timezone`).
`MorningDueEmitter` (`src/reports/morning_due.py`) emits `report.morning_due`
once a day. The morning-report playbook drives `morning_report_tick`
(`src/commands/report_commands.py`). That command reserves the day in a
DST-safe way, collects a read-only snapshot (13 tables and frozen-SHA git
reads), builds a 24 KiB brief and a fallback, and asks `supervisor-global`
to author it when the destination is the full fleet.
`validate_morning_report` (`src/reports/authoring.py`) checks the
submission. A 1500-character summary linking to the report page goes out
through an outbox row with owner `morning`. The rows live in
`morning_reports`, `morning_report_coverage` and `morning_report_facts`.

### 2.13 Notification events

`src/notifications/` defines about 35 typed `notify.*` events, emitted by
`_emit_notify` (`src/orchestrator/events.py`). Their only consumer is the
dashboard WebSocket (`src/api/websocket.py`). Discord subscribes to none of
them. Everything Discord shows comes from a durable row (an incident, an
ask, a digest window or an outbox row), never from a bus event.

### 2.14 Where this system runs

daemon-core owns the ordering of `run_one_cycle`
([daemon-core](daemon-core.md) §2.3). This system's steps in it are the
escalation delivery tick, `DeliveryScheduler.tick` and the digest schedule
tick, the escalation watchdog, `ConversationMaintenance`,
`route_queued_conversation_inputs` and the throttled message-delivery
fallback. `MessageDeliveryService` and the Discord bot run as their own
tasks beside the cycle.

## 3. Boundaries and interactions

### 3.1 What this system owns and what it does not

It owns the message, conversation, escalation, ask, digest and report
tables, the delivery engines and both outboxes, the Discord bot, and the
link builders. It does not own:

- **What a supervisor decides or writes.** Triage, applying a reply, and
  authoring a report are supervisor policy in playbooks (system 10). This
  system makes sure the supervisor is told and that what it submits is
  checked.
- **What an applied decision does.** Approving a review runs the review
  playbook (system 11). Resolving a gate resumes a playbook (system 8).
  Recovering a task changes the task graph (system 2). The ask system
  records the decision and calls the owning system's command.
- **The terminal.** Whether a composer is safe to type into is the tmux
  provider's decision (system 5).

### 3.2 Interactions with the other 17 systems

| # | System | How it meets messaging-and-human-asks |
|---|---|---|
| 1 | [daemon-core](daemon-core.md) | Owns the cycle and its ordering, the composition root (`_bind_report_outbox`, `_bind_conversation_outbox`) and the one write path every message command goes through. |
| 2 | [task-graph](task-graph.md) | `task:` recipients, task-recovery incidents, auto-resolve on `COMPLETED`, the close guard on unread mail. |
| 3 | [scheduling-and-claims](scheduling-and-claims.md) | Task mail reaches the claim holder. Send binds the sender to its claim (`stale_claim`). |
| 4 | [routing-and-profiles](routing-and-profiles.md) | `profile:` broadcast inboxes. Profile grants decide who may call message and ask commands. |
| 5 | [sessions-and-harnesses](sessions-and-harnesses.md) | `SessionLens` reads session activity. `TmuxProvider.nudge` and its composer guard type the pointer. Agent questions become escalations. |
| 6 | [projects-workspaces-git](projects-workspaces-git.md) | Paused projects hold delivery and save decisions without resuming work. The morning report reads git at frozen SHAs. |
| 7 | [integration-and-delivery](integration-and-delivery.md) | Raises repair, retained-repository and train-stall incidents. Integration decisions are an ask source. Ship claims in reports must cite a `delivery:` reference. |
| 8 | [playbooks](playbooks.md) | Gates and playbook approvals are ask sources. Escalation and report playbooks call this system's commands. |
| 10 | [supervisors-and-policy](supervisors-and-policy.md) | The main reader of mail, the owner of incidents and the interpreter of human input. Owns the triage, hourly and morning-report playbooks as policy. |
| 11 | [reviews-and-knowledge](reviews-and-knowledge.md) | Reviews and standalone specs are ask sources. `ReviewNotifier` is the legacy publisher that §6.1 removes. |
| 12 | [providers](providers.md) | Raises `provider_availability` incidents and feeds the digest's provider and reroute facts. |
| 13 | [configuration-and-vault](configuration-and-vault.md) | `discord`, `messages`, `escalation`, `reports` and `digest` config. A change to the Discord config fingerprint disables ask delivery. |
| 14 | [api-and-clients](api-and-clients.md) | Message, ask and escalation endpoints. `src/api/scope.py` enforces the ask authority groups. |
| 15 | [dashboard](dashboard.md) | The Needs-you view, the ask pages and every `/focus/...` link target. The only consumer of `notify.*` events. |
| 16 | [test-service-and-jobs](test-service-and-jobs.md) | No direct coupling. |
| 17 | [operations-and-recovery](operations-and-recovery.md) | Doctor checks (`asks.cleanup_ready`, the conversation stall check), repair of `unknown` deliveries, cutover and reset as operator commands. |
| 18 | [plugins-mcp-and-tools](plugins-mcp-and-tools.md) | The internal inbox and notes plugins. MCP exposes the message and ask commands to agents. |

## 4. Design principles and invariants

These hold today, and changes to this system must keep them.

1. **The database is the queue.** Every message, incident, ask, window and
   post is a row before anything is sent. Bus events only hint, and a pass
   that sees no events must still deliver everything that is due.
2. **A pointer, never the message, and never into a draft.** A nudge is one
   line naming `aq message status <id>`. The composer guard refuses rather
   than type into a human's input, and a refused nudge stays pending.
3. **Internal delivery is at least once.** Compare-and-set stamps on
   delivered, read and notification prevent ordinary duplicates, but a
   handler must check durable state before repeating consequential work.
4. **External delivery never reposts an unknown.** Every post has a unique
   dedup key and a marker. `sent` requires a receipt. An ambiguous send is
   reconciled from channel history or left `unknown` for an operator.
5. **One channel, one link per post, budgets enforced.** No post exceeds its
   kind's budget, the link and marker are never cut, and mentions are
   stripped from interpolated text.
6. **A reply never acts by itself.** Inbound human text is stored as
   evidence and handed to the owning supervisor.
7. **The human's words authorize, not the agent's reading of them.**
   Supervisor-interpreted input must parse to the applied option, and
   supervisor text can never become human evidence.
8. **Identity comes from the gateway or the principal**, never from a
   message body. Workers have no human-ask authority.
9. **Every decision is an immutable packet**, applied through an operation
   ledger. An unknown application is reconciled, never retried blindly.
10. **Mode changes are generation compare-and-sets** under an advisory lock.
    A migrated destination never returns to `legacy`, and the compatibility
    deadline is never extended. [Project/configuration fencing](../design/ask-channel-config-fencing.md)
    requires current ownership for the ask's project and preserves unrelated
    destinations and legacy rows during configuration reconciliation.
11. **Escalations outrank everything else**, and the shared rate guard is
    never bypassed.
12. **Reports have a fallback before an author is woken**, carry no URLs in
    the authored text, and collect evidence read-only.
13. **Mechanism here, judgment in playbooks.** Planning, auto-resolve, the
    sweep, the watchdog and the digest are deterministic and make no LLM
    calls.
14. **One rate tracker and one hourly budget.** Startup configures the shared
    invalid-request tracker in place so logging and the bot count the same
    responses. Hourly report submissions use the digest's `MAX_CHARS` budget
    (600), including the dashboard footer and delivery marker; validation
    messages derive the limit from that constant. Escalation diagnostics and
    compatibility refusals both link to `/focus/asks/<id>`.

## 5. Known gaps

**Resolved audit findings.**

The 429 tracker binding, hourly report budget and ask URL findings are
corrected by the changes described in §4. Channel admission and configuration
retirement are corrected by task `amber-torrent-90`.

**Design hazards.**

- **Stuck ask applications have no sweep.** A decision saved in a paused
  project, or left `unknown`, stays `applying` until someone replays it,
  runs `aq ask retry-operation`, or runs doctor `--fix` (reviews only).
- **Inject consumes without reading.** `aq message inbox --inject` marks
  mail delivered with no `via` and no `read_at`, so "delivered" does not
  mean anyone acted on it.
- **`conversation-queued` rows depend on one sweep.** Only the routing step
  in the cycle moves them. Doctor has a stall check for it
  (`src/doctor/stall_checks.py`).
- **Direct escalation producers lag asks.** Only `escalation_create`
  publishes a `free_text` ask inline. The direct callers in §2.9 wait for
  the reconciler.
- **Ask roots initially post without buttons.** A following delivery edit adds
  the controls. With no public dashboard URL nothing is posted and a repair
  case opens; the dashboard remains the decision surface.
- **Two hourly authoring mechanisms coexist**: the P3
  `supervisor_authored` digest and `reports.hourly`. Nothing reconciles
  them, and the hourly playbook still polls `timer.1m`.
- **Severity drives nothing.** No routing, throttling or quiet hours.
- **Stateful features are off by default.** Auto-resolve, the sweep's
  periodic counterpart and state edits all need
  `discord.escalations.stateful`, which defaults to off.
- **Legacy JSON columns.** Four report tables and several escalation and
  digest columns use `sa.JSON`. They are allowlisted in
  `tests/test_migration_json_columns.py`, not migrated.

**Dead code and stale text.**

- `src/messaging/port.py` is unused. `_notify` and `_create_thread` in
  `src/orchestrator/core.py` are always `None`, and `_task_added_messages`
  is never filled. A comment on `_on_message_sent` in
  `src/messages/delivery.py` describes the old path.
- `message_queries.py` still mentions SQLite, and "Phase 3" comments remain
  in the message command module. The command contract returns `success`.
- `_SURFACES` in the reports code is hard-coded to the agent-queue project.

**Documentation audit (2026-10-23).**

The messaging overview, escalation and conversation guides, comms skill and module
catalog now describe independent internal delivery, receipt semantics, active
holders of task mail, canonical asks and channel ownership. The escalation guide
covers all eight commands, provider facts, stateful gating and P3 digest authoring.
The existing report guide now includes morning configuration, preview, authoring
and receipt diagnosis. Rollout guidance names merged revisions 96–98; historical
H08 evidence retains its provisional labels with an explicit mapping.

The Discord chat-extension design is marked as a historical snapshot. Its digest
outbox move is still incomplete, and H09 cleanup must not be described as shipped.
The pause contract remains current and is indexed separately from obsolete
platform designs. Installed skills are write-if-absent: shipping an updated
`aq-comms` source does not refresh an existing installation; operators inspect
`skills.installed_drift` through the normal reseeding procedure.

## 6. Planned work

Task and review statuses in this section are a **2026-10-20 snapshot**, not a
live queue query. The 2026-10-23 code audit below supersedes its verified-wait
implementation claims; production ask cutover still needs operator evidence.

### 6.1 Finishing the ask cutover: `vivid-pinnacle-60`, `calm-grove-14`, `vivid-rapids-36`

The ask system is built (`stark-forge-10`, COMPLETED). What remains moves
production onto it and removes the duplicate publishers. All three epics come
from the approved one-place-to-ask-Jack implementation spec (§2.10). Two
completed prerequisites changed the plan. `calm-delta-23` discarded all
legacy ask and escalation records, per Jack's 2026-10-19 reset directive.
`fair-journey-56` added `aq ask cutover-status`.

- **`vivid-pinnacle-60`, H08-OPS (PAUSED, operator work).** Run the ask
  migrations on the operator database, move each destination through the
  modes with an expected-generation compare-and-set, exercise the
  test-project matrix, and record each switch time and its 30-day
  compatibility deadline. Until the deadline, replies in legacy threads get
  a move notice. The epic text names the migrations 91/92 from the plan's
  numbering; on `dev` they are `a00000000096`–`098`. Its blockers are
  complete, but it is paused for the operator.
- **`calm-grove-14`, H09 (READY, blocked by `vivid-pinnacle-60`).** Remove
  the duplicate human publishers:
  - A guarded migration renames `escalation_messages` and
    `escalation_actions` to `ask_correspondence` and `ask_operations`,
    removes `notified_revision` and the old notification queries, and
    drains human `escalation_deliveries` rows into `outbound_deliveries`.
    The plan calls it migration 93, but that number is taken on `dev`, and
    the hibernation branch claims `a00000000103`–`106`, so it will need a
    later number.
  - Deletes `src/reviews/notifier.py` and its startup task, and the human
    paths in `src/escalations/dispatch.py`, `plan.py`, `render.py`,
    `sweep.py`, `autoresolve.py` and `src/discord/escalation_buttons.py`.
    No ask creates a thread, and the Needs-you counter is counted once.
  - Updates the guides, `../messaging/project-pauses.md`, the 2026-09-08
    Discord spec (§1, §5, §7, and its byte-identical vault mirror), the
    2026-10-03 chat-extension design (§5, §10), the `aq-reviews` and
    `aq-comms` skills, the templates and the reviewed bundles.

  After H09, §2.9's separate escalation outbox and pump are gone, and §2.5's
  shared scheduler is the only external pump.
- **`vivid-rapids-36`, H09-OPS (READY, operator work, after H09).** Verify
  the installed cleanup and its migration, the vault mirror of the Discord
  spec, profile and skill drift, and a final live sample of acceptance tests
  A01–A18.

The documentation drift listed in the earlier snapshot was corrected by this audit.
The two publishers and old notification queries remain cleanup work. Task
`amber-torrent-90` addresses channel admission and configuration retirement;
the missing application sweep remains a separate gap. The inconsistent ask URLs
are corrected by the changes described in §4.

### 6.2 Event-driven hibernation and verified waits

The verified-waits implementation is present on the audited `dev` baseline in
`src/wait_runtime/`; it is no longer only an epic-branch proposal. Version-2
registration, held mail, fenced suspension/checkpoints, outcomes and explicit
acknowledgement are documented in [agent waits](../../guides/agent-waits.md).
Legacy registration bodies are refused with `wait.upgrade_required` under the
normal configuration. Mail and human-wait behavior is described in §2.4; source
contracts and exact evidence belong to [wait source contracts](../../guides/wait-source-contract.md).

The historical epic was `bold-zenith-11`, based on the 2026-10-13 event-driven
hibernation design (review `rev-bold-pinnacle`) and implementation plan (review
`rev-fleet-orbit`). This audit does not infer current task states or production
migration/enablement from the presence of code.

### 6.3 Morning report on playbook building blocks: `clear-impact-72.5` (READY)

Part of `clear-impact-72` (IN_PROGRESS), which evaluates rewriting shipped
playbooks onto the new building blocks. `.5` evaluates and plans the rewrite
of the morning-report playbook. The plan goes to the vault at
`plans/2026-10-20-playbook-rewrite-morning-report.md`. The playbook is
policy (system 10); this system's `morning_report_tick`, brief and
validation are what it would call. `clear-impact-72.11`
(supervisor-failure-triage) is system 10's.

### 6.4 The system-spec set: `eager-vault-55` (this epic)

This spec is one of the 18 children. When the set is accepted, the
historical specs listed at the top, and `../messaging/project-pauses.md`,
should point here. `grand-zenith-64` (PAUSED) ingests the inventory spec.

Other open work was checked and does not change this system.

## 7. Further reading

- [`../../concepts/messaging.md`](../../concepts/messaging.md): the concept
  overview (see the drift in §5).
- [`../../reference/modules/communications.md`](../../reference/modules/communications.md):
  the module reference (see the drift in §5).
- [`../escalation-delivery.md`](../escalation-delivery.md): the escalation
  creation and delivery contract.
- [`../design/supervisor-agent.md`](../design/supervisor-agent.md): delivery
  service ownership, bounded retry and mailbox semantics.
- [`../design/human-ask-cutover-status.md`](../design/human-ask-cutover-status.md),
  [`../design/human-ask-legacy-reset.md`](../design/human-ask-legacy-reset.md),
  [`../../reference/human-asks-implementation.md`](../../reference/human-asks-implementation.md).
- Guides: [`../../guides/escalations.md`](../../guides/escalations.md),
  [`../../guides/discord-conversations.md`](../../guides/discord-conversations.md),
  [`../../guides/human-asks-rollout.md`](../../guides/human-asks-rollout.md),
  [`../../guides/supervisor-hourly-reports.md`](../../guides/supervisor-hourly-reports.md),
  [`../../guides/agent-waits.md`](../../guides/agent-waits.md).
- [`../../superpowers/specs/2026-10-03-discord-supervisor-chat-extension-design.md`](../../superpowers/specs/2026-10-03-discord-supervisor-chat-extension-design.md):
  the conversation design.
- [`../../superpowers/specs/2026-10-07-historical-park-retirement.md`](../../superpowers/specs/2026-10-07-historical-park-retirement.md):
  the park retirement behind the newer sweep rules.
- Vault specs (in `~/.agent-queue/vault/projects/agent-queue/specs/`):
  `2026-10-11-one-place-to-ask-jack-*` (design and implementation),
  `2026-10-03-discord-as-a-chat-extension-of-the-supervisor.md`,
  `2026-10-07-missed-supervisor-message-wake-up-rca-and-bounded-delivery-f.md`.
- Historical: [`../messaging/`](../messaging/README.md),
  [`../design/messaging-rework.md`](../design/messaging-rework.md),
  [`../implementation/messaging-rework.md`](../implementation/messaging-rework.md),
  [`../../superpowers/specs/2026-09-08-discord-simplification-implementation.md`](../../superpowers/specs/2026-09-08-discord-simplification-implementation.md).

## Operational escalation lifecycle

Operational wait, continuation, train-stall and delivery-watchdog incidents notify
the project supervisor first. Reconciliation must not turn them into human-channel
requests. A supervisor's explicit `escalate_to_human` wait decision authorizes human
delivery; other unresolved operational incidents can be escalated through the
supervisor's normal human-question command.

Every periodic auto-resolution pass inspects the complete open set, including old
incidents. Cancelled gates, terminal/removed batches, settled waits, released
continuations and completed source tasks retire their incidents with durable source
evidence and a non-null automatic outcome. Completed tasks alone do not prove that
delivery reached its target: delivery incidents require their bound containment
projection to settle. The explicit sweep uses the same source rules and revalidates
them before applying a saved plan.

Train and admission alarms measure lack of progress, rather than total queue age.
Active repairs, bounded running visits, deferred retries and recent admissions defer
alarms. Measured visit duration can increase the train threshold above its floor.
One batch or active admission-capacity episode produces one supervisor incident,
regardless of the number of members or waits observing it.
