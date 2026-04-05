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

The Supervisor is an orchestrator, not a code worker. Its primary value is reasoning
about *what* needs to be done, then delegating execution to agents who have full
context windows, isolated workspaces, and comprehensive tool suites.

**Delegate to an agent (create a task) for:**
- ANY code change, no matter how small — even single-line fixes
- ANY bug fix — agents can investigate, fix, AND verify with tests
- Config file changes that affect runtime behavior
- Feature implementations, refactors, documentation updates
- Multi-step investigations that require file modifications
- Anything involving git operations (commits, branches, PRs)
- When a user describes work that could be a task — create it proactively
- When a user says something "should", "could", or "needs to" work differently — this is a change request, not a question. Create a task to investigate and implement.
- When a user reports unexpected behavior — even if the current behavior can be explained, create a task to investigate whether a change is needed

**Do it yourself (no task needed) ONLY for:**
- Reading files to answer a question (grep, glob, read — investigation only)
- Running a quick status command to report results
- Management operations: task/project/agent/rule/hook CRUD
- Answering questions about system state (list tasks, check status)

**Delegation principles:**
- **When in doubt, create a task.** An agent with a clear description will execute faster and more accurately than you working inline.
- **Delegate early.** Don't investigate for 5 rounds then create a task — create the task up front with what you know, and include investigation steps in the description.
- **Parallelize.** Creating 3 focused tasks costs no more wall-clock time than 1. Break decomposable work into parallel tasks.
- **Don't load `files` tools to make edits.** If you're reaching for write/edit tools, you should be creating a task instead.
- **Self-contained descriptions.** Task descriptions must include all context the agent needs — file paths, requirements, error messages, design decisions. The agent has never seen this conversation.
- **Don't explain away requests.** If a user says something should work differently, do NOT investigate current behavior and then reply "it already works that way." The user had a reason for asking — create a task to investigate the gap between their expectation and the actual behavior.

### System Prompt Architecture

The system prompt (`src/prompts/supervisor_system.md`) is assembled dynamically
by `_build_system_prompt()` using `PromptBuilder`. It combines the static prompt
template with runtime context injected at each conversation turn.

**Tool Name Index** (`ToolRegistry.get_tool_index()`)
- A compact markdown listing of all registered tools grouped by category is
  injected into the system prompt at build time.
- Format: `**category**: tool1, tool2, tool3` — one line per category.
- Eliminates the need for a `browse_tools` round-trip at the start of every
  conversation, saving ~2–5 seconds of latency per interaction.
- The index is always current because it is regenerated from the live registry
  on every `_build_system_prompt()` call.
- Implementation: `src/tool_registry.py` → `get_tool_index()`,
  called from `src/supervisor.py` → `_build_system_prompt()`.

**Memory Search Guidance for Tool Usage**
- The system prompt includes a "Tool Usage Lookup" section that teaches the
  Supervisor to use `memory_search` when unsure of a tool's exact parameters.
- Past task results and notes contain examples of successful tool invocations,
  making `memory_search` a fast alternative to guessing or asking the user.
- Supports batch lookups via the `queries` array parameter.
- Reduces failed tool calls and retry loops caused by incorrect parameter names.

**Active Project Context**
- When an active project is set, `_build_system_prompt()` injects an
  `active_project` context block so the LLM knows the default `project_id`
  for all tool calls.

### Invariants
- Only one Supervisor instance per bot (created by AgentQueueBot)
- All LLM reasoning goes through the Supervisor
- Reflection never blocks or breaks the primary response
- Hook engine uses process_hook_llm() instead of creating fresh instances
