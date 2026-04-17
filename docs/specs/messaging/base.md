---
tags: [spec, messaging, base, interface]
---

# Messaging Abstraction

**Source files:** `src/messaging/base.py`, `src/messaging/port.py`, `src/messaging/factory.py`
**Related:** [[messaging/discord]], [[messaging/telegram]], [[specs/supervisor]], [[specs/command-handler]]

## 1. Overview

The messaging subsystem provides a platform-agnostic interface for chat platforms (Discord, Telegram). All platform-specific behavior is isolated behind the `MessagingAdapter` abstract base class. The orchestrator and supervisor interact with messaging exclusively through this interface.

---

## 2. Three-Layer Architecture

Both platform implementations follow the same three-layer decomposition:

| Layer | Responsibility | Discord | Telegram |
|---|---|---|---|
| **Bot Core** | Connection, routing, authorization, history, thread management | `AgentQueueBot` (`src/discord/bot.py`) | `TelegramBot` (`src/telegram/bot.py`) |
| **Commands** | Interactive commands registered on the platform; thin wrappers that delegate to `CommandHandler` | Slash commands (`src/discord/commands.py`) | `/command` handlers (`src/telegram/commands.py`) |
| **Notifications** | Pure formatting functions that produce platform-native output for task lifecycle events | `src/discord/notifications.py` | `src/telegram/notifications.py` |

The Bot Core layer owns all runtime state. The Commands layer is stateless — every command handler receives the bot/handler reference, extracts platform-specific parameters, calls `CommandHandler.execute(name, args)`, and formats the result. The Notifications layer contains pure functions that accept task/agent data and return formatted strings.

---

## 3. MessagingAdapter Interface

Defined in `src/messaging/base.py`. Abstract base class with:

### Lifecycle

- `start()` -- Initialize and connect to the platform.
- `wait_until_ready()` -- Block until the connection is established.
- `close()` -- Graceful shutdown.

### Messaging

- `send_message(text, project_id, embed, view)` -- Send a message to a project's channel.
- `create_task_thread(thread_name, initial_message, project_id, task_id)` -- Create a task-specific thread or topic.
- `edit_thread_root_message()` -- Update the first message in a thread.

### Components

- `get_command_handler()` -> `CommandHandler`
- `get_supervisor()` -> `Supervisor`

### Health

- `is_connected()` -> `bool`
- `platform_name` property -> `str`

---

## 4. Shared Authorization Model

Both platforms enforce the same authorization pattern:

1. A config list of authorized user IDs (`config.discord.authorized_users` or `config.telegram.authorized_users`).
2. If the list is empty, all users are permitted.
3. If non-empty, `str(user_id)` must appear in the list.
4. Authorization is checked at two levels:
   - **Commands** -- Unauthorized users receive an error/rejection. On Discord this is an ephemeral message; on Telegram the callback query returns "Unauthorized."
   - **Messages** -- Unauthorized users are silently ignored (no response, no log).

---

## 5. Message History Pattern

Both platforms maintain a per-channel/chat message buffer and build LLM-compatible history from it.

### Buffer

- Maximum size: `MAX_HISTORY_MESSAGES = 50` (deque with `maxlen`).
- Each buffered message stores: message ID, author name, is-bot flag, text content, timestamp, and channel/chat identifier.
- Discord buffers from the Discord API message history on each call; Telegram maintains a local `CachedMessage` deque per chat (since Telegram's API does not expose channel history to bots).
- Telegram additionally tracks `_buffer_last_access` per chat and drops idle buffers after `BUFFER_IDLE_TIMEOUT = 3600` seconds.

### History Construction

`_build_message_history()` converts the buffer into a list of `{"role": "user"|"assistant", "content": str}` dicts for `Supervisor.chat()`:

- Bot messages become `{"role": "assistant", "content": msg.content}`.
- Other messages become `{"role": "user", "content": "[from {display_name}]: {msg.content}"}` (Discord) or `{"role": "user", "content": "[{author_name}]: {content}"}` (Telegram).
- Consecutive messages with the same role are merged (Anthropic API requirement -- Discord performs this explicitly; Telegram's local buffer naturally avoids it).

### LLM Call Serialization

Both platforms serialize LLM calls per channel/chat using an `asyncio.Lock` dictionary (`_channel_locks` on Discord, `_chat_locks` on Telegram) to prevent duplicate concurrent responses.

---

## 6. Channel / Chat Routing

Both platforms map `project_id` to a platform-native channel concept:

| Concept | Discord | Telegram |
|---|---|---|
| Channel type | `discord.TextChannel` object | Integer `chat_id` |
| Forward mapping | `_project_channels[project_id]` | `_project_chats[project_id]` |
| Reverse mapping | `_channel_to_project[channel_id]` | `_chat_to_project[chat_id]` |
| Global fallback | `_channel` (named channel, default `"agent-queue"`) | `_main_chat_id` (from config) |
| Runtime update | `update_project_channel(project_id, channel)` | `update_project_chat(project_id, chat_id)` |
| Cleanup on delete | `clear_project_channels(project_id)` | `clear_project_chats(project_id)` |

Resolution logic (`_get_channel` / `_get_chat_id`): return the project-specific channel if one is cached; otherwise fall back to the global channel. Return `None`/`0` if no channel is available.

When routing a message to the global channel for a project that has no dedicated channel, Discord prefixes the message with a `` [`project-id`] `` tag. Telegram does not currently add a project prefix in the global chat.

---

## 7. Thread / Topic Creation Pattern

Both platforms create a task-scoped conversation space for streaming agent output. The entry point returns the same callback pair:

```
(send_to_thread, notify_main_channel)
```

- `send_to_thread(text)` -- Sends content into the task's thread/topic. Logs but does not raise on errors.
- `notify_main_channel(text)` -- Sends a notification visible in the main channel feed, linked to the task thread. Falls back to a plain send if the link fails.

Returns `None` if no channel is available.

| Aspect | Discord | Telegram |
|---|---|---|
| Thread type | Discord thread on a root message | Forum topic (if supported) or reply chain |
| Name limit | 100 characters | 128 characters (topics) |
| Reuse | No reuse; new thread per task run | Topics reused per `task_id` when possible |
| Root message | `"**Agent working:** {name}"` in channel | `"*Agent working:* {name}"` in MarkdownV2, or topic title |
| Global channel prefix | `[{project_id}]` prepended to thread name | None |

---

## 8. Orchestrator Callback Wiring

Both platforms register two callbacks with the orchestrator during startup:

| Callback | Purpose | Signature |
|---|---|---|
| `notify_callback` | Send a plain text notification to a project's channel | `(text: str, project_id: str) -> None` |
| `create_thread_callback` | Create a streaming thread and return the callback pair | `(thread_name, initial_message, project_id) -> (send_to_thread, notify_main_channel)` |

Discord wires these in `on_ready` via `orchestrator.set_notify_callback()` and `orchestrator.set_create_thread_callback()`. Telegram wires them during `start()` via `orchestrator.set_command_handler()` and `orchestrator.set_supervisor()`, with notification delivery handled through the EventBus (`TelegramNotificationHandler` subscribes to `notify.*` events).

---

## 9. Notification Types (Shared Semantics)

Both platforms implement the same set of notification formatters. The notification types represent task lifecycle events emitted by the orchestrator. Each platform renders them in its native format (Discord: plain text with markdown; Telegram: MarkdownV2 with inline keyboards).

### 9.1 Notification Type Catalog

| Notification | Trigger | Key Data |
|---|---|---|
| **task_completed** | Task finishes successfully | task, agent, tokens used, summary, files changed |
| **task_failed** | Task fails (before exhausting retries) | task, agent, retry count, error classification, fix suggestion |
| **task_blocked** | Task exhausts retry limit | task, last error, error classification |
| **pr_created** | Agent creates a GitHub PR | task, PR URL |
| **agent_question** | Agent enters WAITING_INPUT | task, agent, question text (truncated to 500 chars) |
| **chain_stuck** | Blocked task has stuck downstream dependents | blocked task, list of stuck tasks (up to 10) |
| **stuck_defined_task** | DEFINED task not promoted for extended period | task, blocking deps (up to 5), stuck duration in hours |
| **budget_warning** | Project token usage crosses threshold | project name, usage, limit, percentage |

### 9.2 Interactive Action Buttons

Notifications that require user action include interactive buttons. Both platforms render the same logical actions, adapted to their UI:

| Notification | Buttons |
|---|---|
| task_started | View Context, Stop Task |
| task_failed | Retry, Skip, View Error |
| task_approval | Approve, Restart |
| task_blocked | Restart, Skip |
| agent_question | Reply, Skip |
| plan_approval | Approve Plan, Delete Plan |

Discord renders these as `discord.ui.View` subclasses with `discord.ui.Button` components. Telegram renders them as `InlineKeyboardMarkup` with `InlineKeyboardButton` rows. Both route button presses back to `CommandHandler.execute()`.

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

`create_messaging_adapter(config, orchestrator)` in `src/messaging/factory.py` returns the appropriate adapter based on `config.messaging_platform`:

| Platform value | Adapter returned |
|---|---|
| `"telegram"` | `TelegramMessagingAdapter` |
| `"discord"` (default) | `DiscordMessagingAdapter` |
