---
tags: [spec, messaging, telegram, bot]
---

# Telegram Bot

**Source files:** `src/telegram/bot.py`, `src/telegram/adapter.py`, `src/telegram/commands.py`, `src/telegram/views.py`, `src/telegram/notifications.py`
**Related:** [[base]], [[discord]], [[supervisor]], [[command-handler]]

> **Future evolution:** See [[design/agent-coordination]] for how messaging platforms surface workflow status and human-in-the-loop prompts.

## 1. Overview

The Telegram bot provides a chat interface to Agent Queue via the `python-telegram-bot` library (async, long polling). It implements the [[base|MessagingAdapter]] interface, providing feature parity with the [[discord|Discord bot]] through the shared [[command-handler|CommandHandler]] and [[supervisor|Supervisor]].

## 2. TelegramMessagingAdapter

Thin wrapper in `src/telegram/adapter.py` that delegates all `MessagingAdapter` methods to `TelegramBot`. Constructor initializes the bot with config and orchestrator.

---

## 3. TelegramBot

Core bot class in `src/telegram/bot.py`.

### 3.1 Message Handling

- Routes incoming messages to `Supervisor.chat()` for natural language processing.
- Per-chat message buffers (50-message deque) for conversation history.
- Per-chat async locks serialize concurrent LLM calls.
- Responses sent as MarkdownV2 with automatic splitting at the 4096-character limit.

### 3.2 Thread / Topic Model

- Uses **forum topics** when the chat supports them (Telegram supergroup forums).
- Falls back to **reply chains** (`reply_to_message_id`) for standard groups and direct chats.
- Topics are reused per `task_id` when possible.
- Returns a `(send_to_thread, notify_main_channel)` callback pair matching the Discord pattern.

### 3.3 Authorization

- Authorized by Telegram user ID from config.
- Unauthorized users receive no response.

### 3.4 Chat Routing

- Per-project chat mapping (`project_id` -> integer `chat_id`).
- Configurable via `update_project_chat(project_id, chat_id)`.

---

## 4. Slash Commands

7 commands registered in `src/telegram/commands.py`:

| Command | Description |
|---------|-------------|
| `/create_task` | Create a new task |
| `/list_tasks` | List tasks for a project |
| `/status` | System status overview |
| `/cancel_task` | Cancel a running task |
| `/retry_task` | Retry a failed task |
| `/approve_task` | Approve a pending task |
| `/skip_task` | Skip/complete a task |

All commands delegate to `CommandHandler.execute()` and format results back via `_send_result()`.

---

## 5. Interactive Actions (Inline Keyboards)

Defined in `src/telegram/views.py`.

### 5.1 Keyboard Generation

- `notification_actions_keyboard()` converts a `NotificationAction` list to `InlineKeyboardMarkup` (max 3 buttons per row).
- Task-specific keyboards for common workflows: started, failed, approval, blocked, agent question, plan approval.

### 5.2 Callback Handling

- `_handle_callback_query()` parses callback data format: `"action:key=val,key2=val2"`.
- Routes to `CommandHandler.execute()` with parsed arguments.
- `disable_keyboard_after_action()` replaces the keyboard with a status indicator after use.

---

## 6. Formatting

Defined in `src/telegram/notifications.py`.

### Utilities

- `escape_markdown()` -- escapes Telegram MarkdownV2 special characters.
- `bold()`, `italic()`, `code()`, `code_block()`, `link()` -- inline formatting helpers.

### Rich Notification Rendering

- `format_embed_as_text()` converts a `RichNotification` to readable MarkdownV2.

### Limits

- Message limit: 4096 characters (vs Discord's 2000).
