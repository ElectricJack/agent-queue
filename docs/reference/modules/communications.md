# Module catalog: messaging, digests and escalations

Every production module behind inter-agent messages, the transport-neutral
messaging port, notification events, digests/reports, human asks and escalations,
shared external delivery, conversations and the Discord gateway itself. This is the
`communications` shard of the [module catalog](README.md); the pages in the
**Component** column are where each module's behaviour is explained in prose.

Paths are relative links into the source. Private helpers share a component
page with the module they support, but every module has its own row.

> **Current surface.** One configured channel carries digests, reports, canonical
> asks, legacy escalation decisions and opt-in supervisor conversations. The six
> former slash commands, general control views and lifecycle notification consumer
> remain removed; `/delete-task` and human-decision buttons are supported. Read
> [System 9](../../specs/systems/messaging-and-human-asks.md) for ownership and cutover.

## Inter-agent messages — `src/messages/`

The durable message queue's delivery half: which recipient gets a message, by
which of the three delivery paths, and what happens when nobody is listening.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/messages/\_\_init\_\_.py](../../../src/messages/__init__.py) | Exports the delivery engine, the session-lens protocol and the parking horizon. | [concepts/messaging.md](../../concepts/messaging.md) | The row model is `src.models.Message`; the queries are `src/database/queries/message_queries.py`. |
| [src/messages/delivery.py](../../../src/messages/delivery.py) | Runs one delivery pass: dispatches by recipient kind, nudges idle sessions or active task holders, skips busy ones, archives stale session mail and recovers only correlated human-origin prose replies. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_message_delivery.py`. Reads and writes only through the public message-query surface — no private engine access. |
| [src/messages/session_lens.py](../../../src/messages/session_lens.py) | The narrow window into the session runtime: coarse `idle`/`busy`/`sleeping`/`absent` activity, on-demand supervisor cold start, nudging, and the last assistant turn. | [concepts/sessions.md](../../concepts/sessions.md) | `tests/test_session_lens.py`. Translates the messaging address `supervisor-<pid>` to the runtime session name `n-supervisor--<pid>`; only supervisor-named sessions are wake-able. |
| [src/messages/service.py](../../../src/messages/service.py) | Runs the independent internal-mail pump on startup, coalesced message events and the periodic delivery interval. | [messaging.md](../../concepts/messaging.md) | — |
| [src/messages/submission.py](../../../src/messages/submission.py) | Validates sender provenance and ordinary sends, resolves scope, and preserves project attribution for authorized global-supervisor reports. | [messaging.md](../../concepts/messaging.md) | — |

## Supervisor conversations — `src/conversations/`

Opt-in channel, mention, DM and bound-thread routes to a live project supervisor
with global fallback. These modules own intake, durable delivery, recovery and
maintenance; the command layer owns conversation state. Ask channel ownership
precedes this route.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/conversations/\_\_init\_\_.py](../../../src/conversations/__init__.py) | Marks the optional supervisor conversation package and its authority boundary. | [concepts/messaging.md](../../concepts/messaging.md#optional-supervisor-conversations) | Disabled by default. |
| [src/conversations/envelope.py](../../../src/conversations/envelope.py) | Validates gateway-observed Discord provenance, identifiers and top-level root mentions. | [concepts/messaging.md](../../concepts/messaging.md#optional-supervisor-conversations) | An HTTP request body cannot establish gateway identity. |
| [src/conversations/intake.py](../../../src/conversations/intake.py) | Classifies observed messages as new conversations, follow-ups or silent ignores, and normalizes input text. | [concepts/messaging.md](../../concepts/messaging.md#optional-supervisor-conversations) | `tests/test_conversation_intake.py`. Escalation threads retain precedence. |
| [src/conversations/limits.py](../../../src/conversations/limits.py) | Defines shared input, reply, rate, backfill, delay and retention bounds. | [concepts/messaging.md](../../concepts/messaging.md#optional-supervisor-conversations) | `tests/test_conversation_limits.py`. |
| [src/conversations/outbox.py](../../../src/conversations/outbox.py) | Defines the idempotent outbound queue port, its refusing unbound implementation and an in-memory test implementation. | [concepts/messaging.md](../../concepts/messaging.md#optional-supervisor-conversations) | `tests/test_conversation_outbox.py`. Commands enqueue through this port. |
| [src/conversations/preconditions.py](../../../src/conversations/preconditions.py) | Names unmet configuration and daemon prerequisites before intake may proceed. | [concepts/messaging.md](../../concepts/messaging.md#optional-supervisor-conversations) | `tests/test_conversation_preconditions.py`. An empty author allowlist disables the route. |
| [src/conversations/render.py](../../../src/conversations/render.py) | Renders supervisor input briefs, fixed refusal notices and dashboard conversation pointers. | [concepts/messaging.md](../../concepts/messaging.md#optional-supervisor-conversations) | Pure text rendering. |
| [src/conversations/backfill.py](../../../src/conversations/backfill.py) | Recovers reconnect history through durable cursors and explicit bounded gaps. | [discord-conversations.md](../../guides/discord-conversations.md) | — |
| [src/conversations/delivery.py](../../../src/conversations/delivery.py) | Binds conversation actions to the shared leased external outbox. | [discord-conversations.md](../../guides/discord-conversations.md) | — |
| [src/conversations/maintenance.py](../../../src/conversations/maintenance.py) | Persists delay notices and applies 30/90-day retention. | [discord-conversations.md](../../guides/discord-conversations.md) | — |

## The messaging port — `src/messaging/`

The platform-agnostic seam. The orchestrator and `main.py` talk only to these
abstractions, so no Discord type leaks outside `src/discord/`.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/messaging/\_\_init\_\_.py](../../../src/messaging/__init__.py) | Re-exports the adapter ABC, the factory, the transport port and the callback aliases. | [concepts/messaging.md](../../concepts/messaging.md) | — |
| [src/messaging/base.py](../../../src/messaging/base.py) | Defines `MessagingAdapter`: lifecycle (start / ready / close), component access and connection health. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_messaging_adapter.py`. The send/thread methods remain as transport-neutral compatibility seams and default to no-ops. |
| [src/messaging/factory.py](../../../src/messaging/factory.py) | Selects the concrete adapter from `messaging_platform` (`discord` or `none`), with a dedicated error for the removed `telegram` value. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_messaging_adapter.py` |
| [src/messaging/null_adapter.py](../../../src/messaging/null_adapter.py) | The no-op adapter for `messaging_platform: "none"` — the daemon boots and schedules with no chat platform attached. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_null_messaging_boot.py`. Owns no command handler or supervisor; `main.py` supplies the daemon-wide ones. |
| [src/messaging/port.py](../../../src/messaging/port.py) | Defines `MessagingPort` plus the platform-neutral `RichNotification` / `NotificationAction` value types and the semantic colour vocabulary. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_messaging_port.py`. The entire port is unused outside tests; live decision buttons use the ask/escalation adapters instead. |
| [src/messaging/types.py](../../../src/messaging/types.py) | Callback type aliases shared by the orchestrator and any transport (notify, thread send, thread create, thread-root edit). | [concepts/messaging.md](../../concepts/messaging.md) | Types only; no behaviour. |

## Notification events — `src/notifications/`

Typed lifecycle events on the event bus. These are **not** a Discord feature:
the Discord consumer that turned them into channel posts was deleted, and what
remains feeds the dashboard's WebSocket stream and any subscribing plugin.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/notifications/\_\_init\_\_.py](../../../src/notifications/__init__.py) | Exports every `notify.*` event type. | [concepts/messaging.md](../../concepts/messaging.md) | — |
| [src/notifications/builder.py](../../../src/notifications/builder.py) | Converts domain objects (task, agent, workspace agent) into the API models notification events carry, so events and REST responses share one shape. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_notifications.py` |
| [src/notifications/events.py](../../../src/notifications/events.py) | Declares the typed `notify.*` events for task lifecycle, questions, PRs, merge conflicts, budgets, stuck chains and daemon health. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_notifications.py`, `tests/test_emit_schema_compliance.py`. Consumed by [src/api/websocket.py](../../../src/api/websocket.py). |
| [src/notifications/render.py](../../../src/notifications/render.py) | Pure markdown formatters for the notification text — `format_task_started`, `format_failed_blocked_report` and `classify_error`, which pattern-matches a raw failure into an actionable suggestion. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_notifications.py`. The Discord embed variants they replaced were deleted with `src/discord/notifications.py`. |

## The hourly digest — `src/digest/`

One short message an interval, silent when nothing durable happened. Fact
collection, eligibility, rendering and scheduling policy are deterministic. `dispatch`
owns runtime work; `supervisor` supplies P3 authoring policy without inline LLM calls.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/digest/\_\_init\_\_.py](../../../src/digest/__init__.py) | Exports the facts, eligibility verdict, renderer, schedule projection and the scheduling service. | [guides/escalations.md](../../guides/escalations.md) | — |
| [src/digest/facts.py](../../../src/digest/facts.py) | Defines what counts as evidence: a `WorkFact` with a stable key, an `ActiveTask` with a live attempt, the window bounds and the category vocabulary. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`. Heartbeats, `updated_at` churn and elapsed time are deliberately not facts. |
| [src/digest/eligibility.py](../../../src/digest/eligibility.py) | The send/silence decision table: applies destination visibility, drops facts and wordings already reported, collapses per-task duplicates, and names the suppression reason. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`. `no_activity` / `idle_only` / `all_filtered` / `already_reported` are contract values the dashboard renders. |
| [src/digest/render.py](../../../src/digest/render.py) | Turns an eligible window into one message: the window and its counts on the first line, two highlight sections, the overflow count, and the needs-you inbox link last, inside `render_budget.DIGEST` (600 characters), with every interpolated string stripped of mentions. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`, `tests/test_discord_render_budgets.py`. Raises rather than rendering a suppressed window. |
| [src/digest/aggregate.py](../../../src/digest/aggregate.py) | `build_digest` — the single entry point shared by the dry preview and real delivery, plus the reported keys and output hash the caller must persist. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`. Being one function is what makes a preview provably match the real send. |
| [src/digest/schedule.py](../../../src/digest/schedule.py) | Projects `discord.digest` settings onto a durable destination, a configuration generation, grid-aligned window bounds and the next evaluation time. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`, `tests/test_discord_settings.py`. A window-defining setting change rolls the generation; a mention list does not. |
| [src/digest/dispatch.py](../../../src/digest/dispatch.py) | The clock-owning half: reserves the grid-aligned window row before delivery, persists silence as durably as a message, coalesces an outage into one labelled catch-up, and pumps sends on a separate lease that stands down for escalations and the rate guard. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest_dispatch.py`. Ticked from the orchestrator cycle as `Orchestrator.digest_schedule`. |
| [src/digest/supervisor.py](../../../src/digest/supervisor.py) | Owns P3 authored-digest cadence, quiet hours, frozen facts, skips and deterministic fallback policy. | [escalations.md](../../guides/escalations.md) | — |

The read half is
[src/database/queries/digest_queries.py](../../../src/database/queries/digest_queries.py),
catalogued in the `database` shard (`docs/reference/modules/database.md`, not yet written).

## Escalations — `src/escalations/`

Durable human decisions. The core owns the incident; this package owns
presentation, planning and transport.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/escalations/\_\_init\_\_.py](../../../src/escalations/__init__.py) | Exports the facts, planner, renderer, transport port, dispatcher, intake and supervisor watchdog. | [guides/escalations.md](../../guides/escalations.md) | — |
| [src/escalations/facts.py](../../../src/escalations/facts.py) | Value objects for the delivery layer: the incident subset a transport may see, the delivery kinds and their priorities, the open/terminal state sets, the mention policy and the transport binding. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_escalation_delivery.py`. The mention policy is the only source of a ping this feature may emit. |
| [src/escalations/plan.py](../../../src/escalations/plan.py) | Pure planner: which deliveries the incident's current state implies, their dedup keys, the bounded backoff schedule, and when a deleted post earns a replacement generation. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_escalation_delivery.py`. Refuses a replacement for a terminal incident or while one is already pending. |
| [src/escalations/render.py](../../../src/escalations/render.py) | Every message Discord sees — root post, thread opener, acknowledgement, relay, resolution and the collapsed resolved root — with authored text sanitised, the operation marker embedded, and each message cut to its `render_budget` row. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_escalation_delivery.py`, `tests/test_discord_render_budgets.py`, `tests/test_dashboard_links.py`. Only the initial root carries the configured mention. |
| [src/escalations/transport.py](../../../src/escalations/transport.py) | The narrow transport port, its honest fault taxonomy (retryable, ambiguous, unavailable, missing) and the deterministic in-memory `SinkTransport`. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_escalation_delivery.py`. No test in the repository touches a real gateway. |
| [src/escalations/dispatch.py](../../../src/escalations/dispatch.py) | Reconciles open incidents into the outbox, leases a delivery, sends it once, reconciles an ambiguous send against the embedded marker, and records the receipt or an honest `unknown`. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_escalation_delivery.py`. Ticked from the orchestrator cycle as `Orchestrator.escalation_delivery`; never raises into it. |
| [src/escalations/intake.py](../../../src/escalations/intake.py) | Pure `classify_inbound`: whether an observed inbound message is an authorized human reply to a known incident, accept / closed / ignore, with a reason for the operator log. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_escalation_intake.py`. Reads no clock, no database and no field the author could set. |
| [src/escalations/supervisor.py](../../../src/escalations/supervisor.py) | Bounded watchdog that raises at most one operational incident when an internal supervisor notice stays undelivered past the configured timeout. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_supervisor_recovery.py`. Its `supervisor_delivery` source kind is deliberately not applicable as approval. |
| [src/escalations/autoresolve.py](../../../src/escalations/autoresolve.py) | Closes incidents whose source provably no longer needs an answer; stateful-gated. | [escalations.md](../../guides/escalations.md) | — |
| [src/escalations/interactions.py](../../../src/escalations/interactions.py) | Defines the choices offered by an escalation and the source action a press names. | [escalations.md](../../guides/escalations.md) | — |
| [src/escalations/state.py](../../../src/escalations/state.py) | Projects incident state deterministically from its evidence. | [escalations.md](../../guides/escalations.md) | — |
| [src/escalations/sweep.py](../../../src/escalations/sweep.py) | Plans or applies proof-based cleanup of old incidents; dry-run by default and stateful-gated. | [escalations.md](../../guides/escalations.md) | — |

## The Discord gateway — `src/discord/`

The Discord gateway and transport: verified ask/escalation decisions, optional
supervisor conversations and explicit operator deletion. Outbound transport is
shared by the domain-owned delivery adapters.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/discord/\_\_init\_\_.py](../../../src/discord/__init__.py) | Marks the package; holds no code. | [concepts/messaging.md](../../concepts/messaging.md) | — |
| [src/discord/adapter.py](../../../src/discord/adapter.py) | Wraps `AgentQueueBot` as a `MessagingAdapter` and exposes the bot for the transports that need it. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_discord_adapter.py`, `tests/test_health_platform.py` |
| [src/discord/bot.py](../../../src/discord/bot.py) | The gateway: runs the idempotent cutover pass in `on_ready` before inbound routing goes live, unregisters the retired command names, and routes observed messages through ask ownership, then legacy escalation and optional conversation intake. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_discord_bot_routing.py`, `tests/test_discord_simplification_lifecycle.py` |
| [src/discord/escalation_intake.py](../../../src/discord/escalation_intake.py) | Observes a Discord message, correlates it through the durable thread binding, and calls exactly one command — `escalation_reply` — under a `discord:<user id>` service principal. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_escalation_intake.py`. The cheap gates run before any database lookup. |
| [src/discord/escalation_transport.py](../../../src/discord/escalation_transport.py) | Implements the transport port against discord.py: resolves the channel by ID, opens the thread from the root message, classifies a deleted post apart from a rate limit, and reads history back for marker reconciliation. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_discord_escalation_transport.py`. Shared by the escalation and digest dispatchers. |
| [src/discord/cutover.py](../../../src/discord/cutover.py) | One-way idempotent migration: inventories durable questions, human gates and historical task threads, creates the missing escalation identities, adopts bot-owned cards in the configured channel, and reports conflicts. | [guides/discord-migration.md](../../guides/discord-migration.md) | `tests/test_discord_cutover.py`. Makes legacy content inert; never deletes a channel, post or thread. |
| [src/discord/rate_guard.py](../../../src/discord/rate_guard.py) | Sliding-window counter for Discord's invalid-request ban threshold, with warn / critical / halt circuit breaking and a log handler that catches discord.py's internal 429 retries. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_rate_guard.py`. Both dispatchers hold sends while it is hot rather than dropping them. |
| [src/discord/slash_commands.py](../../../src/discord/slash_commands.py) | Removes the six former AQ slash command names at startup and registers the explicit `/delete-task` operator action. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_slash_commands.py`. General task controls remain retired; plugin and third-party commands are left alone. |
| [src/discord/ask\_buttons.py](../../../src/discord/ask_buttons.py) | A gateway envelope for persistent opaque ask controls, including after restart. | [messaging-and-human-asks.md](../../specs/systems/messaging-and-human-asks.md) | — |
| [src/discord/escalation\_buttons.py](../../../src/discord/escalation_buttons.py) | Renders legacy escalation choices and verifies gateway presses. | [messaging-and-human-asks.md](../../specs/systems/messaging-and-human-asks.md) | — |
| [src/discord/inbound.py](../../../src/discord/inbound.py) | Routes ask channel ownership first, then legacy escalation ownership, then optional conversation intake; a refusal never transfers ownership. | [messaging-and-human-asks.md](../../specs/systems/messaging-and-human-asks.md) | — |
| [src/discord/intake\_diagnostics.py](../../../src/discord/intake_diagnostics.py) | Bounded in-memory counts of ignored inbound Discord messages, by reason code. | [messaging-and-human-asks.md](../../specs/systems/messaging-and-human-asks.md) | — |
| [src/discord/render\_budget.py](../../../src/discord/render_budget.py) | One size budget per kind of post, enforced here rather than by the author. | [messaging-and-human-asks.md](../../specs/systems/messaging-and-human-asks.md) | — |
| [src/discord/task\_delete.py](../../../src/discord/task_delete.py) | One operator delete action using the daemon's shared preview and execution. | [messaging-and-human-asks.md](../../specs/systems/messaging-and-human-asks.md) | — |

## Canonical human asks — `src/asks/`

Canonical identity, verified human evidence, immutable decisions and source-owned application. Mode switches and resets require operator authority.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/asks/\_\_init\_\_.py](../../../src/asks/__init__.py) | Canonical human requests; invoked through CommandHandler, never a second domain owner. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/adapters.py](../../../src/asks/adapters.py) | Static source ports. Domains own locks, authority checks and application receipts. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/authority.py](../../../src/asks/authority.py) | Audience and verified-input authority, independent of model interpretation. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/batch\_scope.py](../../../src/asks/batch_scope.py) | Scope answers are evidence for another fidelity review, never release authority. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/buttons.py](../../../src/asks/buttons.py) | Gateway-only opaque decisions; controls never contain a mutable option index. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/coverage.py](../../../src/asks/coverage.py) | Accepted external prerequisites over existing review waiters, on the source transaction. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/delivery.py](../../../src/asks/delivery.py) | One canonical ask root, with durable fencing across unknown HTTP outcomes. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/discussion.py](../../../src/asks/discussion.py) | Durable, revision-bound correspondence with the existing named supervisor. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/ingestion.py](../../../src/asks/ingestion.py) | Immutable approval reads and decision-keyed dispatch; never creates tasks itself. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/intake.py](../../../src/asks/intake.py) | Authenticate once, persist once, then correlate without transferring ownership. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/legacy\_reset.py](../../../src/asks/legacy_reset.py) | Explicit project fresh start: discard legacy state, publish only open human reviews. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/models.py](../../../src/asks/models.py) | Defines immutable source context, choice and decision-packet vocabulary. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/operations.py](../../../src/asks/operations.py) | Recoverable review application. Database effects commit with their source receipts. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/reconcile.py](../../../src/asks/reconcile.py) | Bounded source reconciliation and operator evidence, never inferred decisions. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/render.py](../../../src/asks/render.py) | Complete meaning for simple cards; longer decisions lead to their full packet. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/reset\_policy.py](../../../src/asks/reset_policy.py) | The project reset is a discard boundary, never transport or decision evidence. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/review.py](../../../src/asks/review.py) | Review adapter: source-first serialization, authenticated authority and frozen feedback. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/schema.py](../../../src/asks/schema.py) | PostgreSQL immutable audit guard shared by fresh-metadata creation. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/service.py](../../../src/asks/service.py) | Foundation for CommandHandler-owned ask publication and packet reservation. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/source\_operations.py](../../../src/asks/source_operations.py) | Recover source-owned effects without ever retrying an ambiguous submission. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/sources.py](../../../src/asks/sources.py) | Static human source adapters; domain transactions and receipts own every effect. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/standalone.py](../../../src/asks/standalone.py) | Standalone document adapter. The local approval command snapshots bytes first. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |
| [src/asks/transport\_controls.py](../../../src/asks/transport_controls.py) | Operator mode/repair ports. Live inventory verification belongs to rollout. | [human-asks-rollout.md](../../guides/human-asks-rollout.md) | — |

## Shared external delivery — `src/delivery/`

Typed domain adapters retain ownership of records; the shared scheduler owns fair dispatch, frozen payloads, markers and receipt reconciliation.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/delivery/\_\_init\_\_.py](../../../src/delivery/__init__.py) | Shared marker, lease and retry delivery for domain-owned outboxes. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/delivery/dispatch.py](../../../src/delivery/dispatch.py) | Escalation-first, bounded dispatch across typed domain adapters. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/delivery/message.py](../../../src/delivery/message.py) | Deliver one frozen message without assuming a network receipt. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/delivery/scheduler.py](../../../src/delivery/scheduler.py) | Pumps asks, conversations, morning reports and digest windows fairly, with escalation-first priority and bounded claims. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |

## Hourly and morning reports — `src/reports/`

Read-only evidence collection, bounded author briefs, deterministic fallback, zoned scheduling and durable report delivery.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/reports/\_\_init\_\_.py](../../../src/reports/__init__.py) | Durable supervisor report requests and authoring policy. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/authoring.py](../../../src/reports/authoring.py) | Validate morning prose against frozen evidence; the daemon owns provenance. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/delivery.py](../../../src/reports/delivery.py) | Morning domain policy and summaries on the shared delivery outbox. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/fallback.py](../../../src/reports/fallback.py) | Bounded deterministic report content drawn only from persisted evidence. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/git.py](../../../src/reports/git.py) | Bounded, daemon-side observations of a configured default branch. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/hourly.py](../../../src/reports/hourly.py) | Pure hourly report policy and bounded brief construction. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/morning.py](../../../src/reports/morning.py) | Read-only morning evidence collection, independent of schedule/delivery policy. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/morning\_due.py](../../../src/reports/morning_due.py) | Once-per-local-day `report.morning_due` for the reviewed morning-report playbook. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/queries.py](../../../src/reports/queries.py) | One bounded, read-only PostgreSQL snapshot for morning evidence. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |
| [src/reports/schedule.py](../../../src/reports/schedule.py) | Pure zoned daily boundaries, independent of the daemon host's clock zone. | [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md) | — |

## Operator links

The notification surfaces need a dashboard URL that is reachable from the
operator's browser without ever advertising a loopback-only address.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/remote_links.py](../../../src/remote_links.py) | The one dashboard origin that escalations, the digest, reviews and `digest_preview` link to: `dashboard.server.public_url`, else a Tailscale-confirmed `dashboard.server.host`, else an explicit notice. Never `health_check.base_url`. Cached async resolver, plus the diagnostics behind `aq dashboard link` and `dashboard.remote_link`. | [guides/dashboard.md](../../guides/dashboard.md#dashboard-links-in-discord-posts) | `tests/test_remote_links.py`. A configured origin is never reported as verified reachable. |

| [src/dashboard\_paths.py](../../../src/dashboard_paths.py) | Canonical focus paths for ask, report, review, task and inbox links. | [messaging-and-human-asks.md](../../specs/systems/messaging-and-human-asks.md) | Link origin resolution stays in `remote_links.py`. |

## Command and configuration surfaces

These modules belong to other shards but are where this subsystem is driven
from, and are listed here for navigation only:

| Module | Owning shard | What it does for this subsystem |
|---|---|---|
| [src/commands/message_commands.py](../../../src/commands/message_commands.py) | [`cli`](cli.md) | `message_send`, `message_reply`, `message_inbox`, `message_list`, `message_status` |
| [src/commands/escalation_commands.py](../../../src/commands/escalation_commands.py) | [`cli`](cli.md) | Eight scoped escalation commands, including sweep; legacy diagnostics and canonical ask compatibility fencing |
| [src/commands/digest_commands.py](../../../src/commands/digest_commands.py) | [`cli`](cli.md) | `digest_preview`, `digest_status` and P3 authoring via `digest_facts`, `digest_request`, `digest_post` |
| [src/commands/discord_commands.py](../../../src/commands/discord_commands.py) | [`cli`](cli.md) | `discord_purge_channel` — explicit historical-message housekeeping, nothing routine |
| [src/commands/ask_commands.py](../../../src/commands/ask_commands.py) | [`cli`](cli.md) | Publication, previews, decisions, operations, source reconciliation and cutover |
| [src/commands/ask_communication_commands.py](../../../src/commands/ask_communication_commands.py) | [`cli`](cli.md) | Verified input, discussion, opaque buttons and batch-scope answers |
| [src/commands/conversation_commands.py](../../../src/commands/conversation_commands.py) | [`cli`](cli.md) | Supervisor inbox intake, replies, status and maintenance |
| [src/commands/report_commands.py](../../../src/commands/report_commands.py) | [`cli`](cli.md) | Hourly request/brief/submit/reconcile and morning preview/tick/list/get |
| [src/config.py](../../../src/config.py) | `vault` | `messaging_platform`, `messages`, nested Discord digest/escalation/conversation settings and `reports` schedules with their bounds |
