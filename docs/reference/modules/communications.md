# Module catalog: messaging, digests and escalations

Every production module behind inter-agent messages, the transport-neutral
messaging port, the notification event layer, the hourly Discord digest,
durable human escalations, and the Discord gateway itself. This is the
`communications` shard of the [module catalog](README.md); the pages in the
**Component** column are where each module's behaviour is explained in prose.

Paths are relative links into the source. Private helpers share a component
page with the module they support, but every module has its own row.

> **Discord is notification-only.** Several modules below survive only as
> formatting helpers or as retirement machinery — the slash-command mirror,
> the control views and the lifecycle notification consumer were removed in
> the single-channel cutover, and each such row says so.

## Inter-agent messages — `src/messages/`

The durable message queue's delivery half: which recipient gets a message, by
which of the three delivery paths, and what happens when nobody is listening.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/messages/\_\_init\_\_.py](../../../src/messages/__init__.py) | Exports the delivery engine, the session-lens protocol and the parking horizon. | [concepts/messaging.md](../../concepts/messaging.md) | The row model is `src.models.Message`; the queries are `src/database/queries/message_queries.py`. |
| [src/messages/delivery.py](../../../src/messages/delivery.py) | Runs one delivery pass per cascade cycle: dispatches by recipient kind, nudges idle sessions, skips busy ones, parks stale session mail, and sweeps unanswered messages into transcript-tail replies. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_message_delivery.py`. Reads and writes only through the public message-query surface — no private engine access. |
| [src/messages/session_lens.py](../../../src/messages/session_lens.py) | The narrow window into the session runtime: coarse `idle`/`busy`/`sleeping`/`absent` activity, on-demand supervisor cold start, nudging, and the last assistant turn. | [concepts/sessions.md](../../concepts/sessions.md) | `tests/test_session_lens.py`. Translates the messaging address `supervisor-<pid>` to the runtime session name `n-supervisor--<pid>`; only supervisor-named sessions are wake-able. |

## The messaging port — `src/messaging/`

The platform-agnostic seam. The orchestrator and `main.py` talk only to these
abstractions, so no Discord type leaks outside `src/discord/`.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/messaging/\_\_init\_\_.py](../../../src/messaging/__init__.py) | Re-exports the adapter ABC, the factory, the transport port and the callback aliases. | [concepts/messaging.md](../../concepts/messaging.md) | — |
| [src/messaging/base.py](../../../src/messaging/base.py) | Defines `MessagingAdapter`: lifecycle (start / ready / close), component access and connection health. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_messaging_adapter.py`. The send/thread methods remain as transport-neutral compatibility seams and default to no-ops. |
| [src/messaging/factory.py](../../../src/messaging/factory.py) | Selects the concrete adapter from `messaging_platform` (`discord` or `none`), with a dedicated error for the removed `telegram` value. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_messaging_adapter.py` |
| [src/messaging/null_adapter.py](../../../src/messaging/null_adapter.py) | The no-op adapter for `messaging_platform: "none"` — the daemon boots and schedules with no chat platform attached. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_null_messaging_boot.py`. Owns no command handler or supervisor; `main.py` supplies the daemon-wide ones. |
| [src/messaging/port.py](../../../src/messaging/port.py) | Defines `MessagingPort` plus the platform-neutral `RichNotification` / `NotificationAction` value types and the semantic colour vocabulary. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_messaging_port.py`. The action/button half has no live consumer: interactive Discord controls were retired. |
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

## The hourly digest — `src/digest/`

One short message an interval, silent when nothing durable happened. Every
module except `dispatch` is pure: no clock, no network, no LLM.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/digest/\_\_init\_\_.py](../../../src/digest/__init__.py) | Exports the facts, eligibility verdict, renderer, schedule projection and the scheduling service. | [guides/escalations.md](../../guides/escalations.md) | — |
| [src/digest/facts.py](../../../src/digest/facts.py) | Defines what counts as evidence: a `WorkFact` with a stable key, an `ActiveTask` with a live attempt, the window bounds and the category vocabulary. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`. Heartbeats, `updated_at` churn and elapsed time are deliberately not facts. |
| [src/digest/eligibility.py](../../../src/digest/eligibility.py) | The send/silence decision table: applies destination visibility, drops facts and wordings already reported, collapses per-task duplicates, and names the suppression reason. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`. `no_activity` / `idle_only` / `all_filtered` / `already_reported` are contract values the dashboard renders. |
| [src/digest/render.py](../../../src/digest/render.py) | Turns an eligible window into one message: counts, at most three highlights, overflow count and dashboard link, under 1,200 characters, with every interpolated string stripped of mentions. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`. Raises rather than rendering a suppressed window. |
| [src/digest/aggregate.py](../../../src/digest/aggregate.py) | `build_digest` — the single entry point shared by the dry preview and real delivery, plus the reported keys and output hash the caller must persist. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`. Being one function is what makes a preview provably match the real send. |
| [src/digest/schedule.py](../../../src/digest/schedule.py) | Projects `discord.digest` settings onto a durable destination, a configuration generation, grid-aligned window bounds and the next evaluation time. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest.py`, `tests/test_discord_settings.py`. A window-defining setting change rolls the generation; a mention list does not. |
| [src/digest/dispatch.py](../../../src/digest/dispatch.py) | The clock-owning half: reserves the grid-aligned window row before delivery, persists silence as durably as a message, coalesces an outage into one labelled catch-up, and pumps sends on a separate lease that stands down for escalations and the rate guard. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_digest_dispatch.py`. Ticked from the orchestrator cycle as `Orchestrator.digest_schedule`. |

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
| [src/escalations/render.py](../../../src/escalations/render.py) | Every message Discord sees — root post, thread opener, acknowledgement, relay, resolution and resolved root — with authored text sanitised and the operation marker embedded. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_escalation_delivery.py`. Only the initial root carries the configured mention. |
| [src/escalations/transport.py](../../../src/escalations/transport.py) | The narrow transport port, its honest fault taxonomy (retryable, ambiguous, unavailable, missing) and the deterministic in-memory `SinkTransport`. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_escalation_delivery.py`. No test in the repository touches a real gateway. |
| [src/escalations/dispatch.py](../../../src/escalations/dispatch.py) | Reconciles open incidents into the outbox, leases a delivery, sends it once, reconciles an ambiguous send against the embedded marker, and records the receipt or an honest `unknown`. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_escalation_delivery.py`. Ticked from the orchestrator cycle as `Orchestrator.escalation_delivery`; never raises into it. |
| [src/escalations/intake.py](../../../src/escalations/intake.py) | Pure `classify_inbound`: whether an observed inbound message is an authorized human reply to a known incident, accept / closed / ignore, with a reason for the operator log. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_escalation_intake.py`. Reads no clock, no database and no field the author could set. |
| [src/escalations/supervisor.py](../../../src/escalations/supervisor.py) | Bounded watchdog that raises at most one operational incident when an internal supervisor notice stays undelivered past the configured timeout. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_supervisor_recovery.py`. Its `supervisor_delivery` source kind is deliberately not applicable as approval. |

## The Discord gateway — `src/discord/`

The only place the `discord` library is imported. Its inbound surface is one
escalation reply; its outbound surface is the escalation and digest transport.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/discord/\_\_init\_\_.py](../../../src/discord/__init__.py) | Marks the package; holds no code. | [concepts/messaging.md](../../concepts/messaging.md) | — |
| [src/discord/adapter.py](../../../src/discord/adapter.py) | Wraps `AgentQueueBot` as a `MessagingAdapter` and exposes the bot for the transports that need it. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_discord_adapter.py`, `tests/test_health_platform.py` |
| [src/discord/bot.py](../../../src/discord/bot.py) | The gateway: runs the idempotent cutover pass in `on_ready` before inbound routing goes live, unregisters the retired command names, and routes `on_message` to escalation intake and nowhere else. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_discord_bot_routing.py`, `tests/test_discord_simplification_lifecycle.py` |
| [src/discord/escalation_intake.py](../../../src/discord/escalation_intake.py) | Observes a Discord message, correlates it through the durable thread binding, and calls exactly one command — `escalation_reply` — under a `discord:<user id>` service principal. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_escalation_intake.py`. The cheap gates run before any database lookup. |
| [src/discord/escalation_transport.py](../../../src/discord/escalation_transport.py) | Implements the transport port against discord.py: resolves the channel by ID, opens the thread from the root message, classifies a deleted post apart from a rate limit, and reads history back for marker reconciliation. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_discord_escalation_transport.py`. Shared by the escalation and digest dispatchers. |
| [src/discord/cutover.py](../../../src/discord/cutover.py) | One-way idempotent migration: inventories durable questions, human gates and historical task threads, creates the missing escalation identities, adopts bot-owned cards in the configured channel, and reports conflicts. | [guides/discord-migration.md](../../guides/discord-migration.md) | `tests/test_discord_cutover.py`. Makes legacy content inert; never deletes a channel, post or thread. |
| [src/discord/rate_guard.py](../../../src/discord/rate_guard.py) | Sliding-window counter for Discord's invalid-request ban threshold, with warn / critical / halt circuit breaking and a log handler that catches discord.py's internal 429 retries. | [guides/escalations.md](../../guides/escalations.md) | `tests/test_rate_guard.py`. Both dispatchers hold sends while it is hot rather than dropping them. |
| [src/discord/slash_commands.py](../../../src/discord/slash_commands.py) | Retirement list only: removes the six former AQ slash command names from the tree at startup so a stale registration cannot linger. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_slash_commands.py`. Registers nothing; plugin and third-party commands are left alone. |
| [src/discord/embeds.py](../../../src/discord/embeds.py) | Embed construction helpers plus the shared status emoji map, progress bar and truncation limits. | [concepts/messaging.md](../../concepts/messaging.md) | The embed builders no longer reach a channel — the notification consumer was removed. `STATUS_EMOJIS` and `progress_bar` are still used by the command layer's rendering. |
| [src/discord/notifications.py](../../../src/discord/notifications.py) | Lifecycle message and embed formatters, plus `classify_error`, which pattern-matches a raw failure into an actionable suggestion. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_discord_commands.py`. Retained as formatters for logs, the dashboard and the orchestrator's monitoring reports; the interactive views that used them were deleted. |
| [src/discord/views.py](../../../src/discord/views.py) | `ExpiredInteractionTolerantView`, a `discord.ui.View` that swallows only the expired-interaction error code. | [concepts/messaging.md](../../concepts/messaging.md) | `tests/test_views.py`. No in-tree consumer: interactive Discord controls were retired. |

## Command and configuration surfaces

These modules belong to other shards but are where this subsystem is driven
from, and are listed here for navigation only:

| Module | Owning shard | What it does for this subsystem |
|---|---|---|
| [src/commands/message_commands.py](../../../src/commands/message_commands.py) | [`cli`](cli.md) | `message_send`, `message_reply`, `message_inbox`, `message_list`, `message_status` |
| [src/commands/escalation_commands.py](../../../src/commands/escalation_commands.py) | [`cli`](cli.md) | The six scoped escalation commands and their stable error codes |
| [src/commands/digest_commands.py](../../../src/commands/digest_commands.py) | [`cli`](cli.md) | `digest_preview` (dry) and `digest_status` (schedule and delivery health) |
| [src/commands/discord_commands.py](../../../src/commands/discord_commands.py) | [`cli`](cli.md) | `discord_purge_channel` — explicit historical-message housekeeping, nothing routine |
| [src/config.py](../../../src/config.py) | `vault` | `messaging_platform`, the `messages:` section and the nested `discord.digest` / `discord.escalation` settings with their bounds |
