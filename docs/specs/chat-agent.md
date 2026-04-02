# ChatAgent Specification

> **DEPRECATED:** `ChatAgent` has been renamed to `Supervisor` (`src/supervisor.py`).
> `src/chat_agent.py` is now a backward-compatibility shim that re-exports `Supervisor`
> as `ChatAgent`. See `specs/supervisor.md` for the current specification.
> This spec is retained for historical reference only.

## 1. Overview

`ChatAgent` (`src/chat_agent.py`) is the LLM-powered natural language interface for the AgentQueue system. It sits between Discord (or any caller) and the `CommandHandler`, translating free-form user messages into structured tool calls and returning plain-English responses.

Its responsibilities are:

- Hold the `TOOLS` list that the LLM can invoke.
- Build a per-request system prompt that includes workspace context and the active project.
- Run the multi-turn tool-use loop: send messages to the LLM, execute any tool calls it requests, feed results back, and repeat until the LLM produces a text-only reply.
- Provide a `summarize()` method for history compaction (called by the Discord bot layer).
- Delegate all business logic to `CommandHandler.execute()` — ChatAgent contains no business logic itself.

`ChatAgent` is platform-agnostic. The Discord bot (`src/discord/bot.py`) constructs it, calls `chat()`, and handles rendering the response text to Discord. A CLI or HTTP server could use the same `ChatAgent` without modification.

---

## Source Files
- `src/chat_agent.py`

---

## 2. Tool Definitions

The module-level `TOOLS` list contains 61 tool definitions passed verbatim to the LLM on every request. Each entry is a dict with `name`, `description`, and `input_schema` (JSON Schema object). The LLM reads these definitions to decide which tool to invoke.

Tools are grouped below by functional category. Every tool name listed is an exact string in the `TOOLS` list.

### Project Management

| Tool | Purpose |
|------|---------|
| `list_projects` | List all projects in the system |
| `create_project` | Create a new project with optional channel auto-creation |
| `pause_project` | Pause a project so no new tasks are scheduled |
| `resume_project` | Resume a paused project |
| `edit_project` | Edit a project's name, credit weight, or max concurrent agents |
| `delete_project` | Delete a project and all cascading data; optionally archive its Discord channels |
| `set_active_project` | Set or clear the active project context for this conversation |

### Per-Project Discord Channel Routing

| Tool | Purpose |
|------|---------|
| `set_project_channel` | Link a Discord channel ID to a project |
| `set_control_interface` | Link a Discord channel to a project by channel name (string lookup) |
| `get_project_channels` | Get the Discord channel ID configured for a project |
| `get_project_for_channel` | Reverse lookup: given a channel ID, return which project it belongs to |

### Task Management

| Tool | Purpose |
|------|---------|
| `list_tasks` | List tasks, optionally filtered by project or status |
| `create_task` | Create a new task; uses the active project if no project_id given |
| `get_task` | Get full details of a specific task |
| `edit_task` | Edit a task's title, description, or priority |
| `stop_task` | Stop an in-progress task; cancels the agent and marks the task BLOCKED |
| `restart_task` | Reset a completed, failed, or blocked task back to READY |
| `delete_task` | Delete a task (cannot delete an in-progress task) |
| `approve_task` | Manually approve and complete an AWAITING_APPROVAL task |
| `skip_task` | Skip a BLOCKED or FAILED task to unblock its dependency chain |
| `get_task_result` | Retrieve a completed task's output: summary, files changed, error, tokens used |
| `get_task_diff` | Show the git diff for a task's branch against the base branch |
| `get_chain_health` | Show downstream tasks stuck because of a blocked task |
| `set_task_status` | Manually override a task's status, bypassing the state machine |
| `get_agent_error` | Get the last error recorded for a task, including classification and suggested fix |

### Repository Management

| Tool | Purpose |
|------|---------|
| `add_repo` | Register a repository for a project (clone, link, or init) |
| `list_repos` | List registered repositories, optionally filtered by project |

### Agent Management

| Tool | Purpose |
|------|---------|
| `list_agents` | List all configured agents and their current state |
| `create_agent` | Register a new agent; optionally assign a repo as its permanent workspace |

### System Status and Monitoring

| Tool | Purpose |
|------|---------|
| `get_status` | High-level system overview: project, agent, and task counts |
| `get_recent_events` | Get recent system events (completions, failures, etc.) |
| `get_token_usage` | Get token usage breakdown by project or task |
| `orchestrator_control` | Pause, resume, or check the status of the orchestrator |
| `restart_daemon` | Restart the agent-queue daemon process |

### Workspace File Operations

| Tool | Purpose |
|------|---------|
| `read_file` | Read a file's contents with optional offset and line limit |
| `write_file` | Write or create a file (creates parent directories if needed) |
| `edit_file` | Targeted string replacement in a file (unique match required unless `replace_all=true`) |
| `grep` | Search file contents with regex, context lines, file type filtering, and case-insensitive options |
| `glob_files` | Find files matching a glob pattern (e.g. `**/*.py`), sorted by modification time |
| `search_files` | Search file contents (grep mode) or filenames (find mode) in a workspace |
| `list_directory` | List files and directories at a given path within a project workspace |
| `run_command` | Execute a shell command in a workspace directory |

### Git Operations (repo-ID based)

These tools take a `repo_id` directly and are lower-level.

| Tool | Purpose |
|------|---------|
| `git_commit` | Stage all changes and create a commit |
| `git_push` | Push a branch to the remote origin |
| `git_create_branch` | Create and switch to a new branch |
| `git_merge` | Merge a branch into the default branch; aborts on conflicts |
| `git_create_pr` | Create a GitHub pull request using the gh CLI |
| `git_changed_files` | List files changed compared to a base branch |

### Git Operations (project-ID based)

These tools take a `project_id` and resolve the repository automatically. `get_git_status`, `git_log`, and `git_diff` report across all repos for a project. The remaining five are convenience wrappers that use the first repo for a project.

| Tool | Purpose |
|------|---------|
| `get_git_status` | Get current branch, working tree status, and recent commits for a project's repos |
| `git_log` | Show recent commits for a project's repository |
| `git_diff` | Show the git diff for a project's repository |
| `create_branch` | Create a new branch in a project's repository |
| `checkout_branch` | Switch to an existing branch |
| `commit_changes` | Stage all changes and commit |
| `push_branch` | Push a branch to the remote |
| `merge_branch` | Merge a branch into the default branch |

### Notes Management

| Tool | Purpose |
|------|---------|
| `list_notes` | List all markdown notes for a project |
| `write_note` | Create or overwrite a project note |
| `delete_note` | Delete a project note |
| `read_note` | Read note contents by title |
| `append_note` | Append content to note (stream-of-consciousness) |
| `compare_specs_notes` | List specs and notes for gap analysis |

### Hook System

| Tool | Purpose |
|------|---------|
| `create_hook` | Create a hook that auto-triggers context gathering and LLM actions on a schedule or event |
| `list_hooks` | List hooks, optionally filtered by project |
| `edit_hook` | Update any field of an existing hook |
| `delete_hook` | Delete a hook and its run history |
| `list_hook_runs` | Show recent execution history for a hook |
| `fire_hook` | Manually trigger a hook immediately, ignoring cooldown |

---

## 3. Conversation Loop

`ChatAgent.chat(text, user_name, history)` runs the multi-turn tool-use loop. It is an `async` method that returns a single `str` response.

### Input Preparation

1. The caller passes `history`: a list of `{"role": "user"|"assistant", "content": ...}` dicts representing prior turns in the conversation. History construction is the caller's responsibility (the Discord bot builds it from channel messages; see Section 5 below).
2. The current user message is formatted as `"[from {user_name}]: {text}"` and appended to the message list. If the last existing message already has role `"user"`, the new content is concatenated with a newline rather than adding a new message (to maintain the alternating-role requirement of the Anthropic API).

### Tool-Use Loop

```mermaid
flowchart TD
    A[Call provider.create_message] --> B{Tool uses<br/>in response?}
    B -- No --> C[Return text response]
    B -- Yes --> D[Execute each tool via<br/>CommandHandler.execute]
    D --> E[Append tool results<br/>to message history]
    E --> F{Iteration<br/>< 10?}
    F -- Yes --> A
    F -- No --> G["Return fallback:<br/>'Done. Actions taken: ...'"]
```

The loop runs up to 10 iterations:

1. Call `provider.create_message(messages, system, tools, max_tokens=1024)`. The system prompt and full `TOOLS` list are passed on every call.
2. The response is a `ChatResponse` with `.text_parts` (list of text strings) and `.tool_uses` (list of `ToolUseBlock`).
3. **No tool uses:** The loop exits. The final response is `"\n".join(resp.text_parts).strip()`. If that is empty but tools were executed during this request, the response falls back to `"Done. Actions taken: {comma-separated tool names}"`. If no tools were executed at all, the response is `"Done."`.
4. **Tool uses present:** Only the `ToolUseBlock` objects (not any pre-tool text commentary) are appended to `messages` as an `"assistant"` turn. Each tool is executed in sequence via `_execute_tool()`, and results are collected into a `"user"` turn with content type `"tool_result"`. The tool name is also tracked in `tool_actions` for fallback messaging. The loop continues with the enriched message list.
5. After 10 iterations without a text-only response, the loop exits and returns the same fallback message as above.

### Tool Execution

`_execute_tool(name, input_data)` delegates directly to `CommandHandler.execute(name, input_data)`. There is no in-agent validation or transformation — the command handler owns all business logic. The result is serialized as `json.dumps(result)` and placed in the tool result content.

---

## 4. System Prompt

The system prompt is built fresh on every LLM call by `_build_system_prompt()`.

### Template (`SYSTEM_PROMPT_TEMPLATE`)

The template is a long multi-paragraph string with one substitution: `{workspace_dir}`, filled from `config.workspace_dir`. The template covers:

- **Identity**: AgentQueue Discord bot that manages an AI agent task queue.
- **Workspace layout**: Workspaces root path; each project gets a subdirectory.
- **Capability summary**: A prose + bullet list of every tool group the LLM can use.
- **Repository source types**: Explains `clone`, `link`, and `init` semantics and when to use each.
- **Agent workspace model**: How agents get assigned repos; how to set up parallel workspaces.
- **Notes management workflow**: How to read, edit, and write notes; when to turn notes into tasks.
- **Hook system overview**: Periodic vs. event hooks, context steps, short-circuit conditions, and template variables.
- **Per-project Discord channel routing**: How to configure and use dedicated channels.
- **Dispatcher mandate (IMPORTANT)**: The agent is explicitly a dispatcher, not a worker. It must create tasks for technical work but use its own tools for management actions. Never create a task for something a tool can do directly.
- **Task description quality (CRITICAL)**: Task descriptions must be completely self-contained with all context an engineer needs. When a plan is discussed and approved, the full plan must be included in the description — not a summary.
- **Response style**: Be concise, use markdown, act on requests using tools. After completing a management action, respond with exactly one short confirmation line (specific examples are given). Generate IDs from names using lowercase kebab-case.

### Active Project Injection

After formatting the template, if `_active_project_id` is set, the following text is appended:

```
ACTIVE PROJECT: `{project_id}`.
Use this as the default project_id for all tools unless the user
explicitly specifies a different project. When creating tasks,
listing notes, or any project-scoped operation, use this project.
```

This means the active project is communicated to the LLM through the system prompt, not through any special message or tool call.

### System Prompt Assembly

The system prompt is assembled using `PromptBuilder` (see `specs/prompt-builder.md`).
`_build_system_prompt()` uses `PromptBuilder.set_identity("chat-agent-system")` with
the `workspace_dir` variable, and optionally adds an active project context block.

---

## 5. History Compaction

History compaction is implemented in the Discord bot layer (`src/discord/bot.py`), not in `ChatAgent` itself. `ChatAgent` only provides the `summarize()` method that the bot calls.

### Constants

| Constant | Value | Meaning |
|----------|-------|---------|
| `MAX_HISTORY_MESSAGES` | 50 | Maximum messages fetched from Discord channel history |
| `COMPACT_THRESHOLD` | 20 | If message count exceeds this, older messages are summarized |
| `RECENT_KEEP` | 14 | How many recent messages are always kept verbatim |

### When Compaction Triggers

Before every `agent.chat()` call, `_build_message_history()` fetches up to 50 prior messages from the Discord channel. If the count exceeds 20, the messages are split:

- **Older messages**: everything except the last 14.
- **Recent messages**: the last 14, always kept verbatim.

If there are no older messages (total count is 20 or fewer), no compaction occurs.

### How Summarization Works

`_get_or_create_summary(channel_id, older_messages)` manages a per-channel summary cache stored in `self._channel_summaries` (a dict mapping `channel_id` to `(last_message_id, summary_text)`).

1. **Cache hit**: If the cached summary covers messages up to or beyond the last message ID in `older_messages`, the cached summary is returned directly without calling the LLM.
2. **Cache miss**: A plaintext transcript is built from the older messages with lines formatted as `"{author}: {content}"` (using "AgentQueue" for bot messages). The transcript is passed to `agent.summarize()`.

`ChatAgent.summarize(transcript)` calls the provider with:
- A single `"user"` message asking to summarize the transcript, instructing it to preserve project names, task IDs, repo names, decisions made, and pending questions.
- System prompt: `"You are a helpful assistant that summarizes conversations."`
- `max_tokens=512`

The first text part of the response is returned. Failures are caught, logged to stdout, and return `None`.

### Injecting the Summary into History

If a summary is produced, two messages are prepended to the history list passed to `agent.chat()`:

```python
{"role": "user", "content": "[CONVERSATION SUMMARY — earlier messages]\n{summary}"}
{"role": "assistant", "content": "Understood, I have the conversation context."}
```

The assistant acknowledgment message is required to satisfy the Anthropic API's alternating-role constraint.

Recent messages are then appended verbatim, with bot messages mapped to role `"assistant"` and all other messages mapped to role `"user"` with a `"[from {display_name}]: "` prefix.

### Consecutive Same-Role Merging

After all messages are assembled, consecutive messages with the same role are merged by concatenating their content with a newline. This is a final normalization step to comply with the Anthropic API requirement that messages alternate between `"user"` and `"assistant"` roles.

---

## 6. Active Project

The active project is a session-level concept that makes all project-scoped tools default to a specific project without requiring the user to specify it each time.

### Storage

The active project ID is stored in `CommandHandler._active_project_id` (a `str | None`). `ChatAgent` exposes it via a pass-through property and setter:

```python
@property
def _active_project_id(self) -> str | None:
    return self.handler._active_project_id

def set_active_project(self, project_id: str | None) -> None:
    self.handler.set_active_project(project_id)
```

### Setting It

Active project can be set two ways:

1. **Via the `set_active_project` tool**: The LLM calls this tool when the user says something like "work on project X" or "switch to project Y". This routes through `CommandHandler.execute("set_active_project", {"project_id": ...})`.
2. **Directly by the caller**: The Discord bot can call `agent.set_active_project(project_id)` before invoking `chat()`. The bot does this when a message is received in a per-project channel or a notes thread (it prepends context text to the user message rather than calling `set_active_project` directly — see the channel-context injection in Section 3).

### Effect on Tool Calls

The active project ID is injected into the system prompt as an `ACTIVE PROJECT:` directive (see Section 4). The LLM is instructed to use it as the default `project_id` for all project-scoped tool calls unless the user explicitly specifies a different project.

### Channel Context Injection

When a user message arrives in a per-project channel or a notes thread, the Discord bot prepends a context prefix to `user_text` before calling `agent.chat()`:

- **Project channel**: `"[Context: this is the channel for project `{project_id}`. Default to using project_id='{project_id}' for all project-scoped commands.]\n{original_text}"`
- **Notes thread**: `"[Context: this is the notes thread for project `{project_id}`. Default to using notes tools with project_id='{project_id}'.]\n{original_text}"` — Enhanced NOTES MODE: When operating in a notes thread, the agent enters a special mode with auto-categorization behavior. Stream-of-consciousness input from the user is handled by appending to the appropriate note (using `append_note`) rather than requiring structured commands. The agent uses brief confirmation responses (e.g., "Added to {note title}.") to minimize noise in the thread.

This is additional guidance beyond (and independent of) the active project system prompt injection.

---

## 7. Initialization

### `__init__`

```python
def __init__(self, orchestrator: Orchestrator, config: AppConfig):
```

- Stores references to `orchestrator` and `config`.
- Sets `_provider` to `None`.
- Creates `CommandHandler(orchestrator, config)` and stores it as `self.handler`.

### `initialize() -> bool`

Calls `create_chat_provider(config.chat_provider)` from `src/chat_providers/__init__.py`, which selects and constructs the appropriate `ChatProvider` implementation (Anthropic or Ollama) based on config. Returns `True` if a provider was created, `False` otherwise.

### `is_ready` (property)

Returns `True` if `_provider is not None`. The Discord bot checks this before attempting `chat()` and responds with a guidance message if `False`.

### `model` (property)

Returns `_provider.model_name` if a provider is set, otherwise `None`.

### `reload_credentials() -> bool`

Re-calls `initialize()`. Used by the Discord bot to recover from `anthropic.AuthenticationError`: after a token refresh (e.g., `claude login`), the bot calls this and then retries the failed `chat()` call once. Returns `True` on success.

---

## 8. Tiered Tool Loading

The `chat()` method uses a mutable tool set per interaction:

1. Initializes `active_tools` from `ToolRegistry.get_core_tools()` (~11 tools)
2. Passes `active_tools` to each `create_message()` call
3. When the LLM calls `load_tools(category)`, the category's tool definitions
   are added to `active_tools` for subsequent turns
4. Tool expansion does not persist across separate `chat()` invocations

The LLM sees only core tools initially. It uses `browse_tools` to discover
categories and `load_tools` to expand its capabilities as needed.

Tool definitions live in `src/tool_registry.py`. The `TOOLS` name in
`chat_agent.py` is a backward-compatible alias that returns all tools.

---

## 9. Streaming

`ChatAgent` does not implement streaming. The `chat()` method is a coroutine that awaits the full LLM response before returning. The provider's `create_message()` interface returns a complete `ChatResponse` object, not an async generator.

The `_provider.create_message()` call waits for the full response, then the loop processes it synchronously. There is no partial-response yielding at any layer in `ChatAgent`.

From the caller's perspective, `chat()` returns a single `str` when the entire multi-turn loop completes. The Discord bot then sends this string to the channel as a single message (split into multiple Discord messages if it exceeds the 2000-character limit, via `_send_long_message()`).
