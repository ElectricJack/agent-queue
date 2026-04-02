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

**chat(text, user_name, history=None, on_progress=None) → str**
Main entry point for direct address. Processes user message through
multi-turn tool-use loop. If tools were used, triggers a reflection pass.
Starts with core tools only; expands via `load_tools`.

**reflect(trigger, action_summary, action_results, messages, active_tools) → None**
Runs a reflection pass for the given trigger. Called after actions complete.
Evaluates results, checks rules, may take follow-up actions (depth-limited).
Never raises — all exceptions are caught internally.

**process_hook_llm(hook_context, rendered_prompt, project_id, hook_name, on_progress) → str**
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
