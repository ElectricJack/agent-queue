---
tags: [spec, discord, bot, interface]
---

# Discord Integration Specification

## 1. Overview

Discord is the exclusive control plane for Agent Queue. There are no other user-facing interfaces — all commands, status queries, and notifications flow through Discord. The integration has three distinct layers:

- **Bot Core** (`src/discord/bot.py`) — `AgentQueueBot`, a `discord.ext.commands.Bot` subclass. Owns channel routing, message history, authorization, and thread management.
- **Slash Commands** (`src/discord/commands.py`) — All interactive commands registered on the application command tree. Thin wrappers that delegate business logic to the shared [[command-handler|CommandHandler]].
- **Notification Formatters** (`src/discord/notifications.py`) — Pure functions that produce structured Discord message text for task lifecycle events.

The bot uses `discord.Intents.default()` plus `message_content`. The command prefix `!` is registered but unused; all interaction happens through slash commands and @-mentions.

## Source Files
- `src/discord/bot.py`
- `src/discord/commands.py`
- `src/discord/notifications.py`

---

## 2. Bot Core (`AgentQueueBot`)

### 2.1 Initialization

`AgentQueueBot.__init__` receives `AppConfig` and [[orchestrator|Orchestrator]]. It constructs a `ChatAgent` (the LLM interface) and wires the `_on_project_deleted` callback from the command handler to `self.clear_project_channels`, so that any caller that deletes a project (not just Discord commands) automatically clears the bot's in-memory channel caches.

Key instance state initialized at construction:

| Attribute | Type | Purpose |
|---|---|---|
| `_channel` | `discord.TextChannel \| None` | Global fallback channel (name from `config.discord.channels["channel"]`, default `"agent-queue"`) |
| `_project_channels` | `dict[str, TextChannel]` | Forward mapping: `project_id -> channel` |
| `_channel_to_project` | `dict[int, str]` | Reverse mapping: `channel_id -> project_id` (O(1) lookup) |
| `_processed_messages` | `set[int]` | Deduplication guard for `on_message` |
| `_channel_summaries` | `dict[int, tuple[int, str]]` | History compaction cache: `channel_id -> (last_message_id, summary_text)` |
| `_channel_locks` | `dict[int, asyncio.Lock]` | Per-channel mutex for LLM calls |
| `_notes_threads` | `dict[int, str]` | Maps thread IDs to project IDs for the notes subsystem |
| `_boot_time` | `float \| None` | UTC timestamp recorded on `on_ready`, used to discard pre-boot messages |
| `_restart_requested` | `bool` | Set by `/restart` command |
| `_guild` | `discord.Guild \| None` | Cached guild reference |
| `_note_viewers` | `dict[int, dict[str, int]]` | Maps thread_id → {note_filename: message_id} for auto-refresh |
| `_notes_toc_messages` | `dict[int, int]` | Maps thread_id → TOC message_id for view persistence |
| `_note_refresh_timers` | `dict[str, asyncio.TimerHandle]` | Debounce timers for note refresh (keyed by `project_id:filename`) |

The `_notes_threads` mapping is persisted to disk at `<database_dir>/notes_threads.json` as a JSON object with two keys: `threads` (thread_id → project_id) and `toc_messages` (thread_id → toc_message_id). Loaded at startup and saved on every modification. Keys are stored as strings in JSON and converted back to `int` on load. Legacy format (flat dict without `threads`/`toc_messages` keys) is supported for backward compatibility.

### 2.2 `setup_hook`

`setup_hook` runs before the bot connects and performs two actions:

1. Calls `setup_commands(self)` to register all slash commands on the application command tree.
2. Installs a global authorization guard (`_auth_interaction_check`) on `self.tree.interaction_check`. This guard runs before every slash command interaction. Unauthorized users receive an ephemeral `"You don't have permission to use this command."` response and the command is dropped. The guard falls through to the original `interaction_check` if the user is authorized.

If `guild_id` is configured, commands are copied to the guild and synced immediately so they appear without the standard propagation delay.

### 2.3 `on_ready`

`on_ready` performs startup tasks:

1. Records `_boot_time` as the current UTC timestamp.
2. Resolves the global channel by scanning `guild.text_channels` for the configured name. If not found, a warning is logged.
3. Calls `_resolve_project_channels()` to populate the per-project channel cache from the database.
4. Registers orchestrator callbacks (`set_notify_callback` and `set_create_thread_callback`) if any usable channel exists — either the global channel or at least one per-project channel. This allows projects with dedicated channels to receive notifications and task threads even when no global channel is configured.
5. Initializes the `ChatAgent` LLM client, logging whether credentials were found.
6. Calls `_reattach_notes_views()` to restore persistent `NotesView` button handlers on existing TOC messages from `_notes_toc_messages`.

### 2.4 Authorization

Authorization is enforced at two levels:

**Slash commands** — The `_auth_interaction_check` hook on `self.tree.interaction_check` runs before every interaction. Unauthorized users receive an ephemeral rejection and the command is not executed.

**Messages** — `on_message` silently ignores messages from unauthorized users (no response, no log).

The check itself (`_is_authorized`) reads `config.discord.authorized_users` (a list of user ID strings). If the list is empty, all users are permitted. Otherwise, `str(user_id)` must appear in the list.

### 2.5 Channel Management

#### Per-project channels

Each project can have a dedicated `discord.TextChannel`. The bot maintains two synchronized mappings:

- `_project_channels[project_id]` — the channel object for fast notification routing.
- `_channel_to_project[channel_id]` — the reverse, used by `on_message` and command resolution helpers to identify which project a message belongs to.

#### `_resolve_project_channels()`

Called at startup. Iterates all projects from the database. For each project with a `discord_channel_id`:
- If the channel exists in the guild, it is added to both caches with a log message.
- If the channel no longer exists (deleted from Discord), the stale ID is cleared from the database (`db.update_project(project_id, discord_channel_id=None)`) and a warning is logged. This prevents orphaned references from accumulating.

#### `update_project_channel(project_id, channel)`

Called immediately after `/set-channel` or `/create-channel` so routing takes effect without a restart. Removes the stale reverse-mapping entry for any previously cached channel before adding the new one.

#### `clear_project_channels(project_id)`

Called after project deletion (via the `_on_project_deleted` callback). Removes:
- `_project_channels[project_id]`
- `_channel_to_project` entry for the old channel
- All `_notes_threads` entries mapping to the deleted project (persisted to disk)
- `_channel_summaries` and `_channel_locks` entries for all affected channel/thread IDs

#### `get_project_for_channel(channel_id)`

O(1) lookup against `_channel_to_project`. Returns `project_id` or `None`.

### 2.6 Message Routing

`_get_channel(project_id)` returns the dedicated project channel if one is cached, otherwise falls back to the global `_channel`. If no channel is available, it returns `None`.

When a message is routed to the **global channel** for a project that has no dedicated channel, the message text is prefixed with a `` [`project-id`]  `` tag so users can identify the source project. This applies to both plain messages (`_send_message`) and thread-root messages (`_create_task_thread`).

`_is_global_channel(channel, project_id)` — returns `True` if `channel` is the global fallback and `project_id` has no dedicated channel.

### 2.7 `on_message` — Message Routing and Context

`on_message` responds to messages from users in the following situations:

| Condition | Responds |
|---|---|
| Message in the global bot channel | Yes |
| Message in a per-project channel | Yes |
| Bot is @-mentioned anywhere | Yes |
| Message in a registered notes thread | Yes |
| Any other channel, not mentioned | No (silent) |

```mermaid
flowchart TD
    M[Incoming message] --> A{Own message?}
    A -- Yes --> X1[Ignore]
    A -- No --> B{Authorized user?}
    B -- No --> X2[Ignore]
    B -- Yes --> C{Already processed?}
    C -- Yes --> X3[Ignore]
    C -- No --> D{Before boot_time?}
    D -- Yes --> X4[Ignore]
    D -- No --> E[Determine channel context<br/><i>global / project / mention / notes</i>]
    E --> F[Strip bot mention]
    F --> G{Text empty?}
    G -- Yes --> H[Reply with prompt]
    G -- No --> I[Route to Chat Agent]
```

For per-project channels (when not the global bot channel), a context prefix is prepended to the user message before passing it to the LLM:
```
[Context: this is the channel for project `{project_id}`. Default to using project_id='{project_id}' for all project-scoped commands.]
{user_text}
```

For notes threads (when not the global bot channel), a rich NOTES MODE context is injected:
```
[NOTES MODE for project '{notes_project_id}'. BEHAVIOR: The user will type stream-of-consciousness thoughts. 1. Call list_notes to see existing notes. 2. Categorize input — decide which note it belongs to or create new. 3. Use append_note to add to existing, or write_note for new. 4. Respond with BRIEF confirmation: which note updated + 1-line summary. 5. For browsing/management/comparison requests, use appropriate tools. Default project_id='{notes_project_id}'.]
{user_text}
```

LLM calls are serialized per channel using `_channel_locks` to prevent duplicate concurrent responses. If `ANTHROPIC_API_KEY` is not configured, the bot responds with a fixed message directing the user to slash commands only.

On `AuthenticationError`, credentials are reloaded via `agent.reload_credentials()` and the chat call is retried once before returning an error message.

### 2.8 Message History

`_build_message_history(channel, before)` fetches up to `MAX_HISTORY_MESSAGES = 50` messages from the channel history (oldest-first after reversing). It converts them to the LLM message format:

- Bot messages → `{"role": "assistant", "content": msg.content}`
- Other messages → `{"role": "user", "content": "[from {display_name}]: {msg.content}"}`

Consecutive messages with the same role are merged (Anthropic API requirement).

### 2.9 History Compaction

Constants:
- `MAX_HISTORY_MESSAGES = 50` — maximum messages fetched from Discord
- `COMPACT_THRESHOLD = 20` — if more than this many messages exist, compact the older portion
- `RECENT_KEEP = 14` — number of recent messages preserved verbatim after compaction

When history length exceeds `COMPACT_THRESHOLD`, messages are split into two groups:
- **older** — all messages except the last `RECENT_KEEP`
- **recent** — the last `RECENT_KEEP` messages, kept verbatim

The older group is summarized via `_get_or_create_summary()`. The summary is injected as a synthetic user message:
```
[CONVERSATION SUMMARY — earlier messages]
{summary}
```
followed by a synthetic assistant acknowledgement `"Understood, I have the conversation context."` so the message list maintains proper role alternation.

`_get_or_create_summary(channel_id, older_messages)`:
- Returns `None` if the message list is empty or the LLM is not ready.
- Checks `_channel_summaries[channel_id]`: if the cached `(last_message_id, summary)` covers the same messages (`cached[0] >= last_id`), returns the cached summary.
- Otherwise builds a transcript (`"{author}: {content}"` per line) and calls `agent.summarize(transcript)`, caching the result.

### 2.10 Thread Creation

`_create_task_thread(thread_name, initial_message, project_id)` creates a Discord thread for streaming agent output. It:

1. Resolves the target channel (project-specific or global).
2. If routing to the global channel, prepends `[{project_id}]` to the displayed thread name.
3. Sends a thread-root message `"**Agent working:** {display_name}"` in the channel.
4. Creates a thread on that message (name capped at 100 chars).
5. Sends `initial_message` inside the thread.
6. Returns a tuple of two async callbacks:
   - `send_to_thread(text)` — sends content into the thread using `_send_long_message`. Logs but does not raise on errors.
   - `notify_main_channel(text)` — replies to the thread-root message, visually linking the notification to the thread in the channel feed. Falls back to a plain `channel.send` if the reply fails.
7. Returns `None` if no channel is available.

### 2.11 Long Message Handling

`_send_long_message(channel, text, *, reply_to, filename)`:
- `len(text) <= 2000`: sent as-is (with `reply_to` if provided).
- `2000 < len(text) <= 6000`: split at line boundaries into ≤2000-char chunks. First chunk respects `reply_to`. Lines exceeding 2000 chars are hard-split.
- `len(text) > 6000`: a preview (first paragraph or first 300 chars) is sent with the full content attached as a file (`filename`, default `"response.md"`).

### 2.12 Orchestrator Callbacks

The orchestrator receives two callbacks wired in `on_ready`:

- `notify_callback` → `_send_message(text, project_id)` — sends a plain text message to the project's channel (or the global channel with a project tag).
- `create_thread_callback` → `_create_task_thread(thread_name, initial_message, project_id)` — creates a streaming thread and returns the `(send_to_thread, notify_main_channel)` callback pair.

---

## 3. Slash Commands

All slash commands are registered in `setup_commands(bot)` inside `src/discord/commands.py`. Every command delegates its business logic to `bot.agent.handler` (the shared [[command-handler|CommandHandler]]). Commands are thin formatting wrappers; they handle Discord-specific concerns (embeds, file uploads, deferral, ephemeral responses) but contain no business logic themselves.

### Channel-to-project resolution

A helper function `_resolve_project_from_context(interaction, project_id)` is used by all project-scoped commands that accept an optional `project_id`. If the user supplies a `project_id`, it is used unchanged. If not, the interaction's channel is looked up in `bot._channel_to_project`. This means commands run inside a project channel automatically target that project without requiring explicit parameter input.

When resolution fails and the command requires a project, an ephemeral error is returned: `"Could not determine project — please provide project_id or run this command from a project channel."`

### In-progress task warnings

Several destructive git commands may return a `warning` key in the result dict when IN_PROGRESS tasks exist in the target project. The `_with_warning(msg, result)` helper appends this warning to the success message.

### Status emojis and colors

All task statuses have associated emoji and hex color values used in embeds and the `TaskReportView`:

| Status | Emoji | Hex Color |
|---|---|---|
| DEFINED | ⚪ | #95a5a6 |
| READY | 🔵 | #3498db |
| ASSIGNED | 📋 | #9b59b6 |
| IN_PROGRESS | 🟡 | #f39c12 |
| WAITING_INPUT | 💬 | #1abc9c |
| PAUSED | ⏸️ | #7f8c8d |
| AWAITING_APPROVAL | ⏳ | #e67e22 |
| COMPLETED | 🟢 | #2ecc71 |
| FAILED | 🔴 | #e74c3c |
| BLOCKED | ⛔ | #992d22 |

---

### 3.1 Status Commands

#### `/status`
Shows a system-wide status overview.

**Output includes:**
- Orchestrator paused banner (if paused)
- Total task count broken down by IN_PROGRESS, READY, COMPLETED, FAILED, PAUSED
- Per-agent state and current task (if any)
- Up to 5 queued (READY) tasks

No parameters.

#### `/projects`
Lists all projects. Shows name, ID, status, credit weight, and Discord channel mention (if assigned).

No parameters.

#### `/agents`
Lists all registered agents with their state and current task ID (if working).

No parameters.

#### `/budget`
Shows token budget usage broken down by project.

No parameters.

#### `/events`
Shows recent system events.

| Parameter | Type | Description |
|---|---|---|
| `limit` | int (optional, default 10) | Number of events to show |

Output includes event type, project ID, task ID, and timestamp.

---

### 3.2 Project Management Commands

#### `/edit-project`
Edits an existing project's settings.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project to edit |
| `name` | str (optional) | New name |
| `credit_weight` | float (optional) | New scheduling weight |
| `max_concurrent_agents` | int (optional) | New max agents |

#### `/delete-project`
Deletes a project and all its associated data. Clears the bot's channel caches via `_on_project_deleted`.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project to delete |
| `archive_channels` | choice (optional) | "Yes – archive channels" or "No – leave channels as-is" |

When archiving, the bot sets `send_messages = False` for `@everyone` on each linked channel and renames it to `archived-{channel.name}`.

#### `/pause`
Pauses a project, halting scheduling for it.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Project ID (auto-detected from channel) |

#### `/resume`
Resumes a paused project.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Project ID (auto-detected from channel) |

#### `/set-project`
Sets or clears the active project for the chat agent's context.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Project to set as active; omit to clear |

---

### 3.3 Channel Management Commands

#### `/set-channel`
Links an existing Discord channel to a project. Updates the bot's cache immediately.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project to link |
| `channel` | TextChannel | Discord channel to link |

#### `/set-control-interface`
Alias for `/set-channel` that accepts a channel name string instead of a channel mention. Strips leading `#` from the input, then looks up the channel by name in the guild.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project to link |
| `channel_name` | str | Name of the channel (e.g., `"my-project"` or `"#my-project"`) |

#### `/create-channel`
Creates a new Discord text channel and links it to a project. Validates the project exists first. Response is deferred.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project to link |
| `channel_name` | str (optional) | Channel name (defaults to `project_id`) |
| `category` | CategoryChannel (optional) | Discord category to place the channel in |

#### `/channel-map`
Shows all project-to-channel mappings in the server. Splits projects into those with dedicated channels and those using the global channel.

No parameters.

---

### 3.4 Task Management Commands

#### `/tasks`
Lists tasks for a project using the interactive `TaskReportView` UI.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Filter by project (auto-detected from channel) |

#### `/task`
Shows full details of a single task: title, status, project, priority, assigned agent, retry count, and description (truncated to 800 chars).

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task ID |

#### `/add-task`
Adds a task manually. Auto-detects project from channel context, falling back to `handler._active_project_id`. Returns an error if no project can be resolved. Title is truncated to 100 chars from the description.

| Parameter | Type | Description |
|---|---|---|
| `description` | str | Task description (also used as title, truncated) |

Returns an embed showing the new task ID, project, and status (READY).

#### `/edit-task`
Edits a task's metadata.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to edit |
| `title` | str (optional) | New title |
| `description` | str (optional) | New description |
| `priority` | int (optional) | New priority |

#### `/stop-task`
Stops an IN_PROGRESS task.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to stop |

#### `/restart-task`
Resets a task back to READY for re-execution.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to restart |

Response shows the transition: `{previous_status} → READY`.

#### `/delete-task`
Deletes a task permanently.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to delete |

#### `/approve-task`
Approves a task in AWAITING_APPROVAL state, marking it as COMPLETED.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to approve |

#### `/skip-task`
Marks a blocked or failed task as COMPLETED (skipped), unblocking its dependency chain.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to skip |

Response includes the count and IDs of tasks unblocked by skipping.

#### `/chain-health`
Checks dependency chain health for stuck tasks. Can be used in two modes:
- With `task_id`: checks a specific blocked task and lists its stuck downstream tasks.
- Without `task_id` (project mode): scans all blocked chains in the project and summarizes how many tasks each blocked task is holding up.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str (optional) | Check a specific task |
| `project_id` | str (optional) | Check all chains in project (auto-detected from channel) |

#### `/set-status`
Manually overrides a task's status. Available target statuses: DEFINED, READY, IN_PROGRESS, COMPLETED, FAILED, BLOCKED.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to update |
| `status` | choice | New status value |

Returns an embed showing the old and new status with emoji.

#### `/task-result`
Shows the result output of a completed task (ephemeral).

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to inspect |

Returns an embed with: result, summary (up to 1000 chars), files changed (up to 20), tokens used, and error message (up to 500 chars if any).

#### `/task-diff`
Shows the git diff for a task's branch (ephemeral). Diffs longer than 1800 chars are attached as a `.patch` file.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to inspect |

#### `/agent-error`
Shows the full error report for a task (ephemeral). Includes error type, error detail (up to 1000 chars), suggested fix, and agent summary (up to 500 chars).

| Parameter | Type | Description |
|---|---|---|
| `task_id` | str | Task to inspect |

---

### 3.5 Agent Management Commands

#### `/agents`
Lists all agents (also in Status group above).

#### `/create-agent`
Registers a new agent.

| Parameter | Type | Description |
|---|---|---|
| `name` | str | Agent display name |
| `agent_type` | choice (optional) | `claude`, `codex`, `cursor`, or `aider` |
| `repo_id` | str (optional) | Repository to assign as workspace |

Returns an embed with the agent name, generated ID, and type.

---

### 3.6 Repo Management Commands

#### `/repos`
Lists registered repositories.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Filter by project (auto-detected from channel) |

Shows repo ID, source type (clone/link/init), URL or path, project ID, and default branch.

#### `/add-repo`
Registers a repository for a project.

| Parameter | Type | Description |
|---|---|---|
| `source` | choice | `clone`, `link`, or `init` |
| `project_id` | str (optional) | Project (auto-detected from channel) |
| `url` | str (optional) | Git URL (for clone) |
| `path` | str (optional) | Existing directory path (for link) |
| `name` | str (optional) | Repo name |
| `default_branch` | str (optional, default `"main"`) | Default branch |

Returns an embed with the repo ID, source type, and project.

---

### 3.7 Git Operations

There are two families of git commands. The **project-oriented** commands auto-detect the project from channel context and resolve the first associated repo by default. The **repo-oriented** commands take an explicit `repo_id` and operate directly on that repository.

Most git commands that may take more than 3 seconds use `await interaction.response.defer()` before executing. Exceptions: `/git-log` responds directly without deferring; `/git-diff` only defers when the diff exceeds 1800 chars.

#### Project-oriented commands (auto-detect from channel)

##### `/git-status`
Shows git status (current branch, working tree, recent commits) for all repositories in a project.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Project (auto-detected from channel) |

##### `/git-branches`
Lists branches or creates a new branch. If `name` is provided, creates and switches to the new branch. Otherwise lists all branches and shows the current one.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Project (auto-detected from channel) |
| `name` | str (optional) | New branch name to create |
| `repo_id` | str (optional) | Specific repo (uses first repo if omitted) |

##### `/git-checkout`
Switches to an existing branch.

| Parameter | Type | Description |
|---|---|---|
| `branch_name` | str | Branch to check out |
| `project_id` | str (optional) | Project (auto-detected from channel) |
| `repo_id` | str (optional) | Specific repo |

Appends any IN_PROGRESS task warning to the response.

##### `/project-commit`
Stages all changes and commits in a project's repository.

| Parameter | Type | Description |
|---|---|---|
| `message` | str | Commit message |
| `project_id` | str (optional) | Project (auto-detected from channel) |
| `repo_id` | str (optional) | Specific repo |

##### `/project-push`
Pushes a branch to origin.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Project (auto-detected from channel) |
| `branch_name` | str (optional) | Branch to push (defaults to current branch) |
| `repo_id` | str (optional) | Specific repo |

##### `/project-merge`
Merges a branch into the default branch. On conflict, reports the conflict and notes the merge was aborted.

| Parameter | Type | Description |
|---|---|---|
| `branch_name` | str | Branch to merge |
| `project_id` | str (optional) | Project (auto-detected from channel) |
| `repo_id` | str (optional) | Specific repo |

##### `/project-create-branch`
Creates and switches to a new branch.

| Parameter | Type | Description |
|---|---|---|
| `branch_name` | str | New branch name |
| `project_id` | str (optional) | Project (auto-detected from channel) |
| `repo_id` | str (optional) | Specific repo |

##### `/git-log`
Shows recent git commits.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project ID |
| `count` | int (optional, default 10) | Number of commits |
| `repo_id` | str (optional) | Specific repo |

##### `/git-diff`
Shows git diff for a project's repo. Diffs over 1800 chars are attached as a `.patch` file.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project ID |
| `base_branch` | str (optional) | Base branch to diff against (shows working tree diff if omitted) |
| `repo_id` | str (optional) | Specific repo |

These additional project-oriented commands also accept a required `project_id` and an optional `repo_id` (not to be confused with the true repo-oriented commands below that take only `repo_id`):

##### `/create-branch`
Creates a new git branch.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project ID |
| `branch_name` | str | New branch name |
| `repo_id` | str (optional) | Specific repo |

##### `/checkout-branch`
Switches to an existing branch.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project ID |
| `branch_name` | str | Branch to check out |
| `repo_id` | str (optional) | Specific repo |

##### `/commit`
Stages all changes and commits.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project ID |
| `message` | str | Commit message |
| `repo_id` | str (optional) | Specific repo |

##### `/push`
Pushes a branch to the remote.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project ID |
| `branch_name` | str (optional) | Branch to push |
| `repo_id` | str (optional) | Specific repo |

##### `/merge`
Merges a branch into the default branch.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str | Project ID |
| `branch_name` | str | Branch to merge |
| `repo_id` | str (optional) | Specific repo |

#### Repo-oriented commands (explicit repo_id)

The following commands take a required `repo_id` as their primary identifier and do not accept a `project_id`.

##### `/git-commit`
Stages all changes and commits. Takes explicit `repo_id`.

| Parameter | Type | Description |
|---|---|---|
| `repo_id` | str | Repository ID |
| `message` | str | Commit message |

##### `/git-push`
Pushes a branch to remote origin. Takes explicit `repo_id`.

| Parameter | Type | Description |
|---|---|---|
| `repo_id` | str | Repository ID |
| `branch` | str (optional) | Branch to push (defaults to current) |

##### `/git-branch`
Creates and switches to a new branch. Takes explicit `repo_id`.

| Parameter | Type | Description |
|---|---|---|
| `repo_id` | str | Repository ID |
| `branch_name` | str | New branch name |

##### `/git-merge`
Merges a branch into the default branch. Takes explicit `repo_id`. Accepts an override for the target branch.

| Parameter | Type | Description |
|---|---|---|
| `repo_id` | str | Repository ID |
| `branch_name` | str | Branch to merge |
| `default_branch` | str (optional) | Target branch (defaults to repo's default) |

##### `/git-pr`
Creates a GitHub pull request.

| Parameter | Type | Description |
|---|---|---|
| `repo_id` | str | Repository ID |
| `title` | str | PR title |
| `body` | str (optional) | PR description |
| `branch` | str (optional) | Head branch (defaults to current) |
| `base` | str (optional) | Base branch (defaults to repo default) |

##### `/git-files`
Lists files changed compared to a base branch (up to 50 files).

| Parameter | Type | Description |
|---|---|---|
| `repo_id` | str | Repository ID |
| `base_branch` | str (optional) | Branch to compare against |

---

### 3.8 Hook Management Commands

#### `/hooks`
Lists automation hooks.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Filter by project (auto-detected from channel) |

Shows hook name, ID, enabled state, trigger type/details (periodic with interval, or event with event type), and project.

#### `/create-hook`
Creates an automation hook.

| Parameter | Type | Description |
|---|---|---|
| `name` | str | Hook name |
| `trigger_type` | choice | `periodic` or `event` |
| `trigger_value` | str | Interval in seconds (periodic) or event type string (event) |
| `prompt_template` | str | Prompt template with `{{step_N}}` and `{{event}}` placeholders |
| `project_id` | str (optional) | Project (auto-detected from channel) |
| `cooldown_seconds` | int (optional, default 3600) | Minimum seconds between runs |

For periodic triggers, `trigger_value` must be an integer; the command validates this and returns an ephemeral error if not.

#### `/edit-hook`
Edits a hook's enabled state, prompt template, or cooldown.

| Parameter | Type | Description |
|---|---|---|
| `hook_id` | str | Hook to edit |
| `enabled` | bool (optional) | Enable or disable |
| `prompt_template` | str (optional) | New prompt template |
| `cooldown_seconds` | int (optional) | New cooldown |

#### `/delete-hook`
Deletes an automation hook.

| Parameter | Type | Description |
|---|---|---|
| `hook_id` | str | Hook to delete |

#### `/hook-runs`
Shows recent execution history for a hook.

| Parameter | Type | Description |
|---|---|---|
| `hook_id` | str | Hook to inspect |
| `limit` | int (optional, default 10) | Number of runs to show |

Each run shows: status emoji (completed/failed/skipped), trigger reason, tokens used, and skip reason if applicable.

#### `/fire-hook`
Manually triggers a hook immediately, bypassing its schedule and cooldown.

| Parameter | Type | Description |
|---|---|---|
| `hook_id` | str | Hook to fire |

---

### 3.9 Notes Commands

#### `/notes`
Opens an interactive notes browser for a project with button-based navigation.

| Parameter | Type | Description |
|---|---|---|
| `project_id` | str (optional) | Project (auto-detected from channel) |

**Behavior:**
1. Fetches notes via `list_notes` command
2. Sends a `NotesView` — an interactive table-of-contents message with buttons per note file
3. Creates a Discord thread on that message named `"Notes: {project_name}"` (`auto_archive_duration=1440`)
4. Sends a welcome message in the thread explaining stream-of-consciousness input
5. Registers the thread with `bot.register_notes_thread(thread_id, project_id)`
6. Stores the TOC message ID in `_notes_toc_messages` for view persistence

**`NotesView`** (module-level class in `commands.py`, `timeout=None`):
- Rows 1–4: Up to 20 note buttons (`ButtonStyle.secondary`), one per note file, label is note title (truncated to 72 chars)
- Row 5: Control buttons — `Refresh`, `Close Thread`, plus `◀ Prev`/`Next ▶` when notes > 20
- All buttons use `custom_id` for persistence across bot restarts
- Custom ID scheme: `notes:{project_id}:view:{note_slug}`, `notes:{project_id}:refresh`, `notes:{project_id}:close`, `notes:{project_id}:page:{direction}`

**Note button callback:** Reads note content via `read_note`, sends as a message in the thread with a `NoteContentView` (Dismiss button). Deletes any previous view of the same note first. Tracks in `_note_viewers[thread_id][filename] = message_id`.

**`NoteContentView`**: Single `Dismiss` button (`ButtonStyle.danger`). On click: deletes the message and removes from `_note_viewers`. Custom ID: `notes:{project_id}:dismiss:{note_slug}`.

**Refresh button:** Re-fetches notes and edits the TOC message with updated content and buttons.

**Close button:** Archives the thread, removes from `_notes_threads` and `_notes_toc_messages`, cleans up `_note_viewers`.

**Auto-refresh:** When a note is written or appended (via `on_note_written` callback from CommandHandler), any active viewer of that note in any notes thread for the project is automatically refreshed — the old viewing message is deleted and replaced with updated content. A 2-second debounce prevents Discord rate limiting.

**View persistence:** On bot startup (`on_ready`), `_reattach_notes_views()` iterates `_notes_toc_messages`, fetches current notes for each project, creates fresh `NotesView` instances, and calls `bot.add_view(view, message_id=toc_msg_id)` to reattach button handlers.

#### `/write-note`
Creates or updates a project note.

| Parameter | Type | Description |
|---|---|---|
| `title` | str | Note title |
| `content` | str | Note content (markdown) |
| `project_id` | str (optional) | Project (auto-detected from channel) |

#### `/delete-note`
Deletes a project note by title.

| Parameter | Type | Description |
|---|---|---|
| `title` | str | Note title |
| `project_id` | str (optional) | Project (auto-detected from channel) |

---

### 3.10 File Browsing Commands

#### `/browse`
Opens an interactive file browser for a project workspace. The initial listing is fetched via `handler.execute("list_directory", ...)`.

| Parameter | Type | Description |
|---|---|---|
| `path` | str (optional) | Starting subdirectory path |
| `workspace` | str (optional, autocomplete) | Workspace name or ID (defaults to first workspace) |

**Behavior:**
1. Resolves the project from channel context.
2. Calls `list_directory` to fetch the directory listing, which resolves the workspace path to an absolute path via `os.path.realpath()`.
3. Creates a `_FileBrowserView` — a persistent interactive view (5-minute timeout) with:
   - **Parent directory button** (row 0): navigates up one level (hidden at root).
   - **Directory dropdown** (row 1): select a subdirectory to navigate into, paginated at 20 items per page.
   - **File dropdown** (row 2): select a file to view its info (size, path) with action buttons, paginated at 20 items per page.
   - **Pagination buttons** (row 3): "◀ Prev Dirs/Files" and "Next Dirs/Files ▶" when entries exceed one page.
4. The embed shows:
   - Title: `📂 <current path>`
   - Description: project ID and workspace name
   - Contents field: directory and file counts
   - Footer: resolved absolute workspace path (for transparency)

**Refresh / Navigation:** All directory navigation (parent, subdirectory selection) goes through `_refresh`, which calls `handler.execute("list_directory", ...)` with the workspace name. This ensures consistent workspace path resolution through the same code path as the initial load, rather than doing inline filesystem listing.

**File view:** Selecting a file shows an ephemeral embed with file path and size, plus action buttons via `_FileInfoView`.

**Workspace autocomplete:** The `workspace` parameter provides autocomplete that lists all workspaces for the resolved project, showing lock status (🔒) for workspaces in use by agents.

#### `/edit-file`
Opens a text editor modal dialog for editing a file in the project workspace. Not documented in detail here — see the source for the full implementation.

---

### 3.11 System / Admin Commands

#### `/orchestrator`
Pauses, resumes, or queries the orchestrator's scheduling state.

| Parameter | Type | Description |
|---|---|---|
| `action` | choice | `pause`, `resume`, or `status` |

Status response shows current state (PAUSED or RUNNING) and number of running tasks.

#### `/restart`
Signals the daemon to restart. Gathers git context (current commit hash, commits behind origin) before initiating the restart.

| Parameter | Type | Description |
|---|---|---|
| `reason` | str (required) | Why the restart is being requested |

**Behavior:**
1. Constructs a `full_reason` string including the user's display name and their stated reason.
2. Gathers git info for the restart message via `asyncio.to_thread(subprocess.run, ...)`:
   - Gets the short commit hash via `git rev-parse --short HEAD`.
   - Fetches remote and counts commits behind upstream via `git rev-list --count HEAD..@{u}`.
3. Sends a warning embed showing the reason and git context (commit hash, how many commits behind origin if any).
4. Calls `handler.execute("restart_daemon", {"reason": full_reason})`.

---

#### `/update`
Pulls latest source, installs dependencies, and restarts the daemon.

| Parameter | Type | Description |
|---|---|---|
| `reason` | str (optional) | Why the update is being requested (auto-filled if omitted) |

**Behavior:**
1. Defers the interaction (non-ephemeral).
2. Records the current commit hash before pulling.
3. Calls `handler.execute("update_and_restart", {"reason": full_reason})`.
4. On error, sends an error embed.
5. On success, records the new commit hash, shows a summary of what changed (`{before_hash} → {after_hash}` or "Already up to date"), and sends a success embed with abbreviated pull output.

---

### 3.12 UI Components — `TaskReportView`

`/tasks` renders an interactive `discord.ui.View` with collapsible status sections.

#### `TaskReportView`

A `discord.ui.View` with `timeout=600` (10 minutes).

**State:**
- `tasks_by_status` — dict mapping status string to list of task dicts
- `total` — total task count
- `expanded` — set of currently expanded status names

**Initial expansion:** Active and actionable statuses are expanded by default — `IN_PROGRESS`, `ASSIGNED`, `READY`, `FAILED`, `BLOCKED`, `PAUSED`, `WAITING_INPUT`, `AWAITING_APPROVAL`. If none of those have tasks, the first non-empty status in `_STATUS_ORDER` is expanded.

Status display order: `IN_PROGRESS`, `ASSIGNED`, `READY`, `DEFINED`, `PAUSED`, `WAITING_INPUT`, `AWAITING_APPROVAL`, `AWAITING_PLAN_APPROVAL`, `FAILED`, `BLOCKED`, `COMPLETED`.

#### `StatusToggleButton`

One button per status that has tasks. Clicking toggles that status between expanded and collapsed. Expanded sections use `ButtonStyle.primary`; collapsed use `ButtonStyle.secondary`. The label is `"{Status Display Name} ({count})"` with the status emoji.

#### `TaskDetailSelect`

A dropdown populated with tasks from all currently expanded sections, capped at 25 options (Discord's select menu limit). Each option shows the task title (truncated to 95 chars) as the label and the task ID as the description. Selecting a task fetches and displays full task details as an ephemeral followup via `_format_task_detail`.

#### Content rendering

Expanded sections show: a `### {emoji} {Display} ({count})` header, then each task as `**{title}** \`{id}\``, capped at `_MAX_TASKS_PER_SECTION = 15` tasks. A `"...and N more"` line is appended if the section is larger.

Collapsed sections show a single line: `{emoji} **{Display}** ({count})`.

If the rendered content exceeds 1950 chars, it re-renders with a tighter cap of 8 tasks per expanded section.

---

## 4. Notification Formats

All notification formatters are pure functions in `src/discord/notifications.py`. They return plain text strings (no embeds). They are called by the orchestrator and consumed by the `notify_callback`, which routes them to the appropriate channel.

### 4.1 Error Classification

`classify_error(error_message)` maps error messages to `(label, suggestion)` pairs by keyword matching on the lowercased error string. The first matching pattern wins.

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

### 4.2 Notification Types

#### `format_task_completed(task, agent, output)`

Emitted when a task finishes successfully.

```
**Task Completed:** `{task.id}` — {task.title}
Project: `{task.project_id}` | Agent: {agent.name}
Tokens used: {output.tokens_used:,}
Summary: {output.summary}           [only if summary present]
Files changed: {file1}, {file2}, …  [only if files_changed present]
```

#### `format_task_failed(task, agent, output)`

Emitted when a task fails (before exhausting retries).

```
**Task Failed:** `{task.id}` — {task.title}
Project: `{task.project_id}` | Agent: {agent.name} | Retry: {retry_count}/{max_retries}
Error type: **{error_type_label}**
```
{error_message[:300]}…
```
💡 {fix_suggestion}
_Use `/agent-error {task.id}` for the full error log._
```

The error message is truncated to 300 chars with `…` (unicode ellipsis) appended if longer.

#### `format_task_blocked(task, last_error)`

Emitted when a task exhausts its retry limit and enters BLOCKED state.

```
**Task Blocked:** `{task.id}` — {task.title}
Project: `{task.project_id}` | Max retries ({max_retries}) exhausted. Manual intervention required.
Last error type: **{error_type_label}**  [only if last_error present]
💡 {fix_suggestion}                      [only if last_error present]
_Use `/agent-error {task.id}` to inspect the last error._
```

#### `format_pr_created(task, pr_url)`

Emitted when an agent creates a GitHub pull request and the task moves to AWAITING_APPROVAL.

```
**PR Created:** `{task.id}` — {task.title}
Project: `{task.project_id}`
Review and merge to complete: {pr_url}
Status: AWAITING_APPROVAL
```

#### `format_agent_question(task, agent, question)`

Emitted when an agent enters WAITING_INPUT state with a question requiring human input.

```
**Agent Question:** `{task.id}` — {task.title}
Project: `{task.project_id}` | Agent: {agent.name}
> {question[:500]}
```

The question is truncated to 500 chars.

#### `format_chain_stuck(blocked_task, stuck_tasks)`

Emitted when a blocked task has downstream dependents that are now permanently stuck.

```
⛓️ **Dependency Chain Stuck:** `{blocked_task.id}` — {blocked_task.title} is BLOCKED
Project: `{blocked_task.project_id}` | {N} downstream task(s) are now permanently stuck:
  • `{id}` — {title} (status: {status})
  …                                         [up to 10 tasks; then "… and N more"]
_Use `/skip-task {blocked_task.id}` to skip the blocked task and unblock the chain, or `/restart-task {blocked_task.id}` to retry it._
```

#### `format_stuck_defined_task(task, blocking_deps, stuck_hours)`

Emitted by the orchestrator's stuck-task monitor when a DEFINED task has not been promoted to READY for an extended period.

```
⏳ **Stuck Task:** `{task.id}` — {task.title}
Project: `{task.project_id}` | Has been DEFINED for **{stuck_hours:.1f} hours** without promotion to READY.
Blocked by:
  • `{dep_id}` — {dep_title} (status: {dep_status})
  …                                         [up to 5 deps; then "… and N more"]
_Use `/skip-task <blocking-task-id>` to skip a blocker, or `/restart-task <blocking-task-id>` to retry it._
```

If no unmet dependencies are found, the blocking section is replaced with: `_No unmet dependencies found — this may be a bug in promotion logic._`

#### `format_budget_warning(project_name, usage, limit)`

Emitted when a project's token usage crosses a warning threshold.

```
**Budget Warning:** Project **{project_name}** at {pct:.0f}% ({usage:,} / {limit:,} tokens)
```

`pct` is `usage / limit * 100`. If `limit` is zero, `pct` is reported as 0.
