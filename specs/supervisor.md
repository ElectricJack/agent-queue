# Supervisor Specification

The Supervisor is the single intelligent entity in the agent-queue system.
It replaces the former `ChatAgent` class. All LLM reasoning flows through it.

## Class: Supervisor (src/supervisor.py)

### Constructor
- `Supervisor(orchestrator, config, llm_logger=None)`
- Creates a `CommandHandler` for tool execution
- Creates a `ReflectionEngine` from `config.supervisor.reflection`

### Methods

**initialize() → bool**
Creates the LLM provider. Returns True if ready.

**is_ready** (property)
Returns whether the provider is initialized (`_provider is not None`).

**model** (property)
Returns the provider's model name, or `None` if no provider is set.

**is_model_loaded() → bool**
Async method that checks if the LLM model is loaded and ready (delegates to provider).

**set_active_project(project_id: str | None) → None**
Sets the active project on the command handler.

**reload_credentials() → bool**
Re-creates the LLM provider (e.g. after token refresh). Returns True on success.

**chat(text, user_name, history=None, on_progress=None, _reflection_trigger="user.request") → str**
Main entry point for direct address. Processes user message through
multi-turn tool-use loop. The loop is structured around the LLM calling
a `reply_to_user` tool to deliver its final response — if the LLM stops
without calling it, a nudge mechanism (up to 2 nudges) prompts it to
use the tool. If tools were used prior to `reply_to_user`, triggers a
reflection pass. Starts with core tools only; expands via `load_tools`.

When reflection returns a verdict that did not pass, the supervisor
recursively calls `chat()` with a retry prompt (self-correction loop).

Supports cancellation via an internal `cancel_event` — checked between
rounds and after tool execution. Returns `"Cancelled."` if cancelled.

Serializes conversation context via `_serialize_conversation_context()`
so tasks created during a chat session inherit the conversation thread.

**cancel() → None**
Cancels a running `chat()` call via an internal `asyncio.Event`.

**is_chatting** (property)
Returns True while a `chat()` call is in progress.

**reflect(trigger, action_summary, action_results, messages, active_tools) → ReflectionVerdict | None**
Runs a reflection pass for the given trigger. Called after actions complete.
Evaluates results, checks rules, may take follow-up actions (depth-limited).
Never raises — all exceptions are caught internally. Returns a verdict
that the caller uses to decide whether to trigger a reflection retry loop.

**process_hook_llm(hook_context, rendered_prompt, project_id=None, hook_name="unknown", on_progress=None) → str**
Entry point for hook engine LLM invocations. Sets active project,
combines context with prompt, processes through `chat()` with
`_reflection_trigger="hook.completed"`.

**observe(messages, project_id) → dict**
Stage 2 LLM pass for passive observation. Receives a batch of messages
that passed the ChatObserver's Stage 1 keyword filter. Makes a lightweight
LLM call (max_tokens=256) to decide: ignore, update memory, or post
a suggestion. Returns a dict with "action" key. Never raises — returns
`{"action": "ignore"}` on any error.

**on_task_completed(task_id, project_id, workspace_path) → dict**
Handles task.completed events. Called by the orchestrator's completion
pipeline BEFORE merge (while workspace is still available). Delegates
to `process_task_completion` command to discover plan files, then runs
a reflection pass. Returns dict with `plan_found` (bool) so the
orchestrator can transition to AWAITING_PLAN_APPROVAL if needed.
Never raises — returns `{"plan_found": False}` on error.

**break_plan_into_tasks(raw_plan, parent_task_id, project_id, ...) → list[dict]**
Sends a plan to the LLM to break into executable tasks via tool calls.
Substantial method (~100 lines) with significant business logic including
task post-processing, dependency chaining, and conversation context
propagation. Used by the plan approval workflow.

**expand_rule_prompt(rule_content, project_id) → str | None**
Uses the LLM to expand a rule's natural language description into an
actionable hook prompt. Used by the rule system when creating hooks
from rules.

**summarize(transcript) → str | None**
Summarizes a conversation transcript.

### Backward Compatibility
- `src/chat_agent.py` re-exports `Supervisor` as `ChatAgent`
- All existing imports continue to work
- `TOOLS` and `SYSTEM_PROMPT_TEMPLATE` re-exported for compatibility

### Three Activation Modes
1. **Direct Address** — user @mentions, full authority
2. **Passive Observation** — absorbs chat, suggests only (Phase 5)
3. **Reflection** — periodic/event-driven self-verification

### Direct Work vs. Task Delegation

The Supervisor has filesystem tools (via the `files` tool category) for direct
investigation and small changes. These include `read_file`, `write_file`,
`edit_file`, `grep`, `glob_files`, `search_files`, `list_directory`, and
`run_command`.

**Use file tools directly when:**
- Investigating a bug or reading code to answer a question
- Making small, targeted edits (config changes, single-file fixes)
- Running quick commands (tests, status checks, builds)

**Create a task for an agent when:**
- The work spans multiple files or requires significant reasoning
- It's a feature, refactor, or multi-step implementation
- You need Claude Code's full context window and tool suite

### Invariants
- Only one Supervisor instance per bot (created by AgentQueueBot)
- All LLM reasoning goes through the Supervisor
- Reflection never blocks or breaks the primary response
- Hook engine uses process_hook_llm() instead of creating fresh instances
