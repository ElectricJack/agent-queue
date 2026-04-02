---
auto_tasks: false
---

# Investigation: Supervisor LLM Failed to Understand User Request

## What Happened

On 2026-04-01 at ~19:07 UTC, user ElectricJack sent a message via Discord in the
`#project-agent-queue` channel. The supervisor LLM (qwen3.5:27b-10k running on
Ollama) responded:

> It looks like your message got cut off! You wrote "You, can we ins" — could you
> please complete your question?

The user's actual message was NOT cut off at the Discord level — the LLM only saw a
truncated fragment because **the input exceeded the model's context window and
Ollama silently truncated it**.

## Root Cause: Context Window Overflow

The model `qwen3.5:27b-10k` has `num_ctx=10240` (10K tokens). The supervisor chat
sends the following to the LLM:

| Component | Estimated Tokens |
|-----------|-----------------|
| System prompt (4,843 chars) | ~1,200 |
| Tool definitions (62 tools) | ~9,300 |
| Message history (2 messages) | ~350 |
| **Total** | **~10,850** |
| **Context window** | **10,240** |
| **Overflow** | **~600 tokens** |

The tool definitions alone consume **~90% of the context window**, leaving almost
no room for the actual conversation.

### How Ollama Handles Overflow

When the total prompt exceeds `num_ctx`, Ollama **silently truncates** the input to
fit. It does NOT raise an error. This caused the user's message to be partially
dropped, and the LLM saw only the fragment "You, can we ins" instead of the full
request.

### Why It Worked Before (Sometimes)

- **Hook executions** use only ~14 tools (hook-specific subset), leaving plenty of
  room for messages. These work reliably.
- **Earlier user conversations** on the same day also had 62 tools but shorter or
  longer history. Whether the truncation hits the user's message depends on total
  message history length and which part Ollama decides to drop.

### Contributing Factor: Context Prefix Overhead

Each user message has ~282 characters of overhead prepended:
- `[from ElectricJack]: ` (21 chars)
- `[Context: this is the channel for project 'agent-queue'...Other known projects: 'skinnable-imgui'...]` (~261 chars)

This means from the user's 390-char message, only ~108 chars were their actual
request — and even those were truncated by the context overflow.

### Additional Issue: Token Estimation Is Misleading

The `input_tokens_est` field in logs (1,381 tokens for the failed request) only
counts `system_prompt + messages`, **excluding tool definitions**. This makes the
context usage appear well within limits when it's actually overflowing. See
`src/llm_logger.py:169-171`.

## Recommendations

### Immediate Fix Options

1. **Increase `num_ctx`** — Change the Ollama model to use a larger context window
   (e.g., `num_ctx=32768`). The base qwen3.5 model supports 262K context. Trade-off:
   higher VRAM usage and potentially slower inference.

2. **Reduce tools sent per request** — The 62 tools loaded for user chat are
   excessive. The category system exists (`browse_tools`/`load_tools`) but the core
   tool set still includes 62+ tools. Move more tools into categories so only
   essential ones (maybe ~10-15) are always loaded.

3. **Add context window guard** — Before calling the LLM, estimate total token
   usage (including tool definitions) and either:
   - Trim message history to fit
   - Warn the user that context is limited
   - Dynamically reduce tool count

### Longer-Term Improvements

4. **Fix token estimation** — Include tool definition size in `input_tokens_est`
   so logs accurately reflect real context usage.

5. **Dynamic tool loading** — Start with a minimal core set (list_tasks,
   create_task, get_task, edit_task, memory_search, browse_tools, load_tools,
   reply_to_user, send_message, list_rules, save_rule — ~12 tools) and let the LLM
   load additional categories on demand. This is already partially implemented but
   the "core" set defined by `get_core_tools()` is too large (everything NOT in
   `_TOOL_CATEGORIES` is treated as core).

## Evidence

- **LLM Log**: `~/.agent-queue/logs/llm/2026-04-01/chat_provider.jsonl`, entries 410-413
- **Daemon Log**: `~/.agent-queue/daemon.log` (19:07-19:09 UTC entries)
- **Model Config**: `num_ctx=10240` confirmed via `ollama api/show`
- **Config**: `~/.agent-queue/config.yaml` → `model: qwen3.5:27b-10k`
