---
tags: [spec, messaging, base, interface]
---

# Messaging Abstraction

**Source files:** `src/messaging/base.py`, `src/messaging/port.py`, `src/messaging/factory.py`
**Related:** [[discord]], [[telegram]], [[supervisor]], [[command-handler]]

## 1. Overview

The messaging subsystem provides a platform-agnostic interface for chat platforms (Discord, Telegram). All platform-specific behavior is isolated behind the `MessagingAdapter` abstract base class. The orchestrator and supervisor interact with messaging exclusively through this interface.

## 2. MessagingAdapter Interface

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

## 3. Notification Types

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

## 4. Factory

`create_messaging_adapter(config, orchestrator)` in `src/messaging/factory.py` returns the appropriate adapter based on `config.messaging_platform`:

| Platform value | Adapter returned |
|---|---|
| `"telegram"` | `TelegramMessagingAdapter` |
| `"discord"` (default) | `DiscordMessagingAdapter` |
