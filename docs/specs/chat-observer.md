# Chat Observer Specification

The ChatObserver replaces the ChatAnalyzer with integrated observation
in the [[supervisor|Supervisor]]. It processes project channel messages through a two-stage
pipeline: deterministic filtering (Stage 1) followed by [[supervisor|Supervisor]] LLM
evaluation (Stage 2).

## Class: ChatObserver (src/chat_observer.py)

### Two-Stage Processing

**Stage 1 — Deterministic filter (zero LLM cost):**
- Project-specific terms from project profiles
- Action words: deploy, release, broken, bug, error, merge, etc.
- Custom keywords from config
- Message length threshold (200+ chars)
- Bot messages are always filtered out

**Stage 2 — Supervisor LLM pass (via Supervisor.observe()):**
- Only processes messages that pass Stage 1
- Lightweight call with conversation batch + project context
- Returns structured decision: ignore, memory update, or suggestion

### Conversation Batching
- Messages buffered per channel
- Flushed after configurable quiet window (default 60s) or max count (default 20)
- Processed as single conversation snippet

### Suggestion UI
- Reuses SuggestionView (src/discord/views.py) with Accept/Dismiss buttons
- Accepted suggestions trigger Supervisor in direct-address mode
- Dismissed suggestions logged to memory

### Configuration (under supervisor.observation)
- enabled: bool (default true)
- batch_window_seconds: int (default 60)
- max_buffer_size: int (default 20)
- stage1_keywords: list[str] (additional custom keywords)

### Invariants
- Stage 1 is purely deterministic — zero token cost for filtered messages
- Observer errors never crash the Discord bot
- Bot's own messages are always filtered out
- Empty batches are never sent to Stage 2

## Integration Points

### Discord Bot (src/discord/bot.py)

**Initialization:**
- ChatObserver created in `__init__` with `ObservationConfig` from `config.supervisor.observation`
- Project profiles built from `_channel_to_project` mapping using `_build_project_profiles()`
- Each project gets keyword set from its ID (split on `-` and `_`)

**Message Flow:**
1. Discord message arrives → `on_message()`
2. After `chat.message` event emitted → `_chat_observer.on_message()`
3. Stage 1 filter runs — message buffered or dropped
4. Background timer checks for ready batches every 10s
5. When batch ready → `_process_observation_batch()` callback
6. Supervisor.observe() performs Stage 2 LLM analysis
7. Result routed:
   - `"ignore"` → no action
   - `"memory"` → `_store_observation_memory()`
   - `"suggest"` → `_post_observation_suggestion()`

**Lifecycle:**
- `start()` called in `on_ready` after channels resolved
- Project profiles updated after channel resolution
- Observer runs until bot shutdown

**Error Handling:**
- All observer methods fail-open — errors logged but never crash bot
- Missing callbacks return gracefully
- Database errors in suggestion creation handled gracefully

## Supervisor Integration (src/supervisor.py)

**observe() method:**
- Input: list of messages (dicts with author, content, timestamp), project_id
- Builds conversation transcript from messages
- Single LLM call with max_tokens=256 (lightweight)
- Parses JSON response: `{"action": "ignore"|"memory"|"suggest", ...}`
- Returns decision dict with optional content, suggestion_type, task_title

**System Prompt:**
- Instructs LLM to observe passively (no direct action on project)
- Three decision types explained with examples
- Strict JSON-only response format

**Error Handling:**
- Returns `{"action": "ignore"}` on any error
- Parses JSON with markdown code block support
- Validates action is one of allowed values

## Database Schema

**chat_analyzer_suggestions table** (reused from ChatAnalyzer):
- `id` (primary key)
- `project_id`
- `channel_id`
- `suggestion_type` (task, answer, context, warning)
- `suggestion_text`
- `status` (pending, accepted, dismissed)
- `created_at`
- `resolved_at`

Created when suggestion posted, updated when user interacts with buttons.

## UI Components (src/discord/views.py)

**format_suggestion_embed():**
- Color-coded by type: green (answer), blue (task), yellow (context), red (warning)
- Confidence bar visualization (5-block scale)
- Footer with project ID and confidence percentage

**SuggestionView:**
- Two buttons: Accept (green checkmark), Dismiss (red X)
- Persistent across restarts via `custom_id` encoding
- Accept behavior depends on type:
  - answer: post suggestion text as bot message
  - task: create task via CommandHandler
  - context: post informational embed
  - warning: acknowledge + suggest manual task creation
- Dismiss: update DB status, grey out embed, disable buttons

## Performance Characteristics

**Token Cost:**
- Stage 1: zero tokens (pure keyword matching)
- Stage 2: ~100-200 tokens per batch (lightweight prompt + response)
- Only triggered after quiet window or buffer full

**Latency:**
- Stage 1: <1ms (in-memory filter)
- Stage 2: ~500-1000ms (LLM call) only when batch ready
- No impact on Discord message handling speed

**Memory:**
- One buffer per active channel (max 20 messages each)
- Buffers auto-flush when processed
- No long-term memory accumulation

## Differences from ChatAnalyzer

**Removed:**
- Separate ChatAnalyzer class and process
- Memory-informed context retrieval
- Confidence scoring model
- Cooldown tracking per suggestion type
- Auto-execution of high-confidence suggestions

**Simplified:**
- Two-stage pipeline replaces multi-step analysis
- Single LLM call instead of multiple rounds
- Suggestion types reduced to 4 (was 6+)
- No separate suggestion queue or batch processing

**Improved:**
- Zero token cost for trivial messages (Stage 1 filter)
- Integrated with Supervisor (single LLM entity)
- Reuses existing UI components (SuggestionView)
- Simpler configuration (part of SupervisorConfig)

## Testing Strategy

**Unit Tests (tests/test_chat_observer.py):**
- Stage 1 filter logic (project terms, action words, length threshold)
- Buffer operations (add, flush, size)
- Batch readiness (count and time thresholds)
- on_message filtering (accept/reject cases)
- start/stop lifecycle

**Integration Tests:**
- Full flow: filter → buffer → flush
- Config wiring to SupervisorConfig
- Callback invocation

**Manual Testing:**
- Discord message flow with real channels
- Stage 2 LLM decisions
- Suggestion UI interactions
- Error recovery (LLM failures, network issues)
