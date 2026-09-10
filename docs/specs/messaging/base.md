---
tags: [spec, messaging, base, interface]
---

# Messaging Abstraction

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../../README.md) for what AQ does today, and
> see [historical material](../../history/README.md) for how this material is
> organised.

**Source files:** `src/messaging/base.py`, `src/messaging/port.py`, `src/messaging/factory.py`, `src/messaging/null_adapter.py`
**Related:** [messaging/discord](discord.md), [specs/supervisor](../supervisor.md), [specs/command-handler](../command-handler.md), [Discord migration runbook](../../guides/discord-migration.md)

## 1. Overview

The messaging subsystem is the platform-agnostic seam between the daemon and a
chat transport. There is exactly one real implementation — Discord — and one
`none` implementation; Telegram was removed with its dependency, so this
document describes a one-transport port kept for substitutability and testing,
not a multi-platform abstraction.

The seam also shrank. Since the Discord simplification the transport carries two
outbound products and one inbound one:

| Direction | What crosses the seam | Owner |
|---|---|---|
| Out | The hourly activity digest — one message per eligible window | `src/discord/embeds.py`, driven by the digest scheduler |
| Out | One escalation root post and its thread, plus acks, relays and the resolution edit | `src/escalations` + `src/discord/escalation_transport.py` |
| In | A verified reply in a known escalation thread → `escalation_reply` | `src/discord/escalation_intake.py` |

Everything else that used to cross it — per-task execution threads, project-channel
chat, gate and task buttons, immediate lifecycle posts, slash commands — is gone.
See the [replacement capability checklist](../../guides/discord-replacement-checklist.md).

---

## 2. Layers

| Layer | Responsibility | Discord |
|---|---|---|
| **Adapter / bot core** | Connection, authorization, the startup cutover pass, and inbound escalation-reply routing | `AgentQueueBot` (`src/discord/bot.py`), `DiscordMessagingAdapter` (`src/discord/adapter.py`) |
| **Transports** | Durable, leased, deduplicated outbound delivery for escalations and digests | `src/discord/escalation_transport.py`, the digest dispatcher |
| **Formatting** | Pure functions producing platform-native output | `src/discord/embeds.py`, `src/discord/notifications.py` |

There is no Commands layer. `src/discord/commands.py` and the 122-command mirror
are deleted, and `src/discord/slash_commands.py` now exists only to *unregister*
the six retired commands at sync time. Plugin-registered slash commands are the
one thing the bot still adds to the command tree; Agent Queue itself registers
none.

`src/discord/notifications.py` retains the lifecycle formatters as pure
functions. Nothing in `src/discord/` subscribes them to the bus any more — the
daemon imports `classify_error` and `format_task_started` directly — so treat
that module as a formatting library, not as an active notification consumer.

---

## 3. MessagingAdapter Interface

Defined in `src/messaging/base.py`. Abstract base class with:

### Lifecycle

- `start()` -- Initialize and connect to the platform.
- `wait_until_ready()` -- Block until the connection is established.
- `close()` -- Graceful shutdown.

### Messaging

- `send_message(text, project_id=None, *, embed=None, view=None)`
- `create_task_thread(thread_name, initial_message, project_id=None, task_id=None)`
- `get_thread_last_message_url(task_id)`
- `edit_thread_root_message(task_id, content=None, embed=None)`

**All four are retired no-ops on the Discord adapter.** They remain on the ABC
so an old caller cannot crash the daemon, and each returns `None` without
touching Discord. A new outbound product does not go through them: it gets a
durable transport with its own outbox row, lease and confirmed receipt, because
a fire-and-forget send cannot survive a restart or an ambiguous timeout.
Historical threads and roots are left exactly as they are.

### Components

- `get_command_handler()` -- the daemon-wide `CommandHandler` wired by `main.py`.
- `get_supervisor()` -- retained for compatibility; the bot owns no private
  Supervisor since the chat cutover.

### Health

- `is_connected()` / `platform_name()`.

---

## 4. Authorization Model

1. `config.discord.authorized_users` lists the Discord user IDs allowed to act.
2. An empty list permits everybody; a non-empty one requires `str(user_id)` to
   appear in it.
3. An unauthorized user is **silently ignored**. There is no rejection message,
   because the configured channel is shared with people and the adapter must not
   behave like a chatbot.

Authorization alone is not authority. An accepted reply is executed under
`ExecutionPrincipal.service("discord:<user id>")`, which the core turns into
`human:discord:<user id>`; identity is never read from a request body. See
[Durable human escalations](../../guides/escalations.md).

---

## 5. Message History

Removed. There is no per-channel message buffer, no `_build_message_history()`,
no `Supervisor.chat()` relay and no per-channel LLM lock, because Discord no
longer hosts a conversation. Supervisor chat lives in the dashboard, and the
supervisor session owns its own conversation memory.

The one conversation the transport does carry — an escalation thread — is
persisted as immutable inbound/outbound message rows in core state, not buffered
in the adapter.

---

## 6. Channel Routing

One installation, one channel. `discord.channel_id` is a numeric channel ID that
survives a rename; every item in it names its project. There is no
project→channel map, no reverse map, no global-channel fallback and no runtime
`update_project_channel`. `per_project_channels` is ignored with a warning and
cannot re-enable channel creation, and the adapter never creates or deletes a
channel.

Legacy channel *names* survive only as one-way migration inventory. Resolving
them, and what happens when they disagree, is
[the migration runbook's §4](../../guides/discord-migration.md).

---

## 7. Threads

The only thread the adapter creates is an escalation's own thread, opened from
its root post and owned by the escalation delivery outbox — one row per
`(escalation, kind, generation)`, leased, deduplicated and rebound after a
restart from stored channel/message/thread IDs. Task-scoped execution threads
and the `(send_to_thread, notify_main_channel)` callback pair are retired; the
adapter's `create_task_thread` returns `None`.

---

## 8. Orchestrator Wiring

`main.py` builds the adapter and hands the bot the daemon-wide
`CommandHandler`. The orchestrator ticks the escalation delivery service once
per cycle when a bot is present and `discord.escalation.enabled` is true, and
the digest scheduler evaluates its window on its own interval. Neither is on the
critical path: a Discord outage, a rate-limit halt or a missing channel is a
recorded delivery fault, never a stall in the EventBus or the scheduler.

The old `set_notify_callback` / `set_create_thread_callback` wiring is gone with
the notification handler that used it.

---

## 9. Notification Semantics

Lifecycle events are still published on the bus for the dashboard, plugins and
playbooks. What changed is that Discord is no longer an immediate consumer of
them:

| Old Discord behavior | Now |
|---|---|
| One post per task completed / failed / blocked, PR created, budget warning, chain stuck, stuck DEFINED task | Aggregated into the hourly digest, or silence when the window has no qualifying activity |
| Per-task streamed output | Dashboard live session view and recorded attempts |
| Agent question card with a reply modal | Supervisor triage; a human decision becomes a durable escalation |
| Action buttons (retry / skip / stop / approve / restart / reply) | Dashboard controls and the Gates drawer; the Discord view classes and callbacks are deleted |

`NotificationAction` (§11) survives on the platform-neutral port, but no Discord
message renders an actionable button any more: the transport's messages are
text, and the one inbound action is a typed reply.

---

## 10. Error Classification (Shared Logic)

`classify_error(error_message)` maps error messages to `(label, suggestion)` pairs by keyword matching on the lowercased error string. The first matching pattern wins. This logic is platform-agnostic and used by notification formatters on both platforms.

| Keyword | Label | Suggestion |
|---|---|---|
| `"error_max_structured_output_retries"` | Structured-output failure | Simplify the task description or remove JSON-schema constraints. |
| `"auth"` or `"authentication"` | Authentication error | Check that ANTHROPIC_API_KEY (or claude login) is valid and not expired. |
| `"rate_limit"`, `"rate limit"`, or `"429"` | Rate-limit | The API rate limit was hit. The task will be retried automatically. |
| `"quota"` | Token quota exhausted | Daily or session token quota exceeded. Wait for quota reset or increase limits. |
| `"token"` | Token limit | The context window or token budget was exceeded. Break the task into smaller pieces. |
| `"timeout"` | Timeout | The agent exceeded the stuck-timeout. Increase stuck_timeout_seconds or simplify the task. |
| `"config"` | Configuration error | A config value is invalid. Check model name, allowed_tools, and MCP server settings. |
| `"mcp"` | MCP server error | An MCP server failed. Verify MCP server configs in the task context. |
| `"permission"` | Permission denied | The agent couldn't access a file or directory. Check workspace permissions. |
| `"cancelled"` | Cancelled | The task was stopped manually. |
| (no match) | Unexpected error | Check daemon logs (`~/.agent-queue/daemon.log`) for full details. |
| (empty/None message) | Unknown error | Check daemon logs for details. |

---

## 11. Notification Data Types

Defined in `src/messaging/port.py`.

### RichNotification

Platform-neutral rich message dataclass:

| Field | Type | Description |
|---|---|---|
| `title` | `str` | Header text |
| `description` | `str` | Body content |
| `color` | `str` | Semantic color: `default`, `success`, `error`, `warning`, `info`, `critical` |
| `fields` | `list[tuple[str, str, bool]]` | List of (name, value, inline) tuples |
| `footer` | `str \| None` | Optional footer text |
| `url` | `str \| None` | Optional URL metadata |
| `actions` | `list[NotificationAction]` | Interactive buttons |

Transports convert `RichNotification` to platform-native formats -- Discord embeds/views, Telegram MarkdownV2/inline keyboards.

### NotificationAction

Interactive button dataclass:

| Field | Type | Description |
|---|---|---|
| `label` | `str` | Display text |
| `action_id` | `str` | Command to execute when pressed |
| `style` | `str` | `primary`, `secondary`, or `danger` |
| `args` | `dict[str, str]` | Parameters passed to the command |

---

## 12. Factory

`create_messaging_adapter(config, orchestrator)` in `src/messaging/factory.py`
returns an adapter for `config.messaging_platform`:

| Platform value | Result |
|---|---|
| `"discord"` (default) | `DiscordMessagingAdapter` |
| `"none"` | `NullMessagingAdapter` — the daemon runs with no chat transport at all |
| `"telegram"` | `ValueError` naming the removal and the two supported values |
| anything else | `ValueError` listing the supported values |

`"none"` is a first-class configuration, not a degraded one: escalations are
still created, supervisors are still notified and the dashboard inbox still
works. Only the external post is absent.
