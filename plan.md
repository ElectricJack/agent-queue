---
auto_tasks: true
---

# Plan: Improve Supervisor Agent Speed via Tool Name Awareness & Batch Memory Search

## Background

### Problem Statement

The supervisor agent currently uses a **tiered tool loading system** where only ~10 core tools are available at conversation start. When the supervisor needs a tool from a non-preloaded category, it must:

1. Call `browse_tools` to see available categories
2. Call `load_tools(category="...")` to load the category
3. *Then* call the actual tool

This adds **2 extra LLM round-trips** (sometimes 1 if categories are auto-preloaded by keyword matching). Each round-trip costs ~2-5 seconds of latency. For a typical task involving git + files + memory operations, this can add 10-15 seconds of overhead.

Additionally, the `memory_search` tool only supports a single query per call. When the supervisor needs to look up multiple related topics (e.g., "how to call git_commit" AND "how to call create_pr"), it must make separate sequential calls.

### Current Architecture

- **~80+ total tools** across 10 categories (git, project, agent, rules, memory, notes, files, task, plugin, system)
- **Core tools** (~10) are always loaded: `reply_to_user`, `create_task`, `list_tasks`, `edit_task`, `get_task`, `browse_tools`, `load_tools`, `send_message`
- **Auto-preload**: `search_relevant_categories()` uses keyword matching to preload up to 3 categories
- **Dynamic loading**: LLM calls `load_tools(category)` to expand its tool set mid-conversation
- **System prompt** (`src/prompts/supervisor_system.md`) lists only core tool names; non-core tools require `browse_tools` → `load_tools` round-trips
- **Memory search** (`memory_search` + `MemoryManager.search()`) accepts a single query string; no batch support

### Design Decisions

1. **Tool names in system prompt, not full schemas**: Including all tool names (just names grouped by category) adds ~300-400 tokens. This is negligible compared to the ~2000-3000 token system prompt. Full schemas would add ~20KB+ — names are enough to know *what* to load.

2. **Memory search for tool usage patterns**: Rather than including full tool documentation in the prompt, we tell the supervisor to use `memory_search` to look up how to call specific tools. Past task results and notes already contain tool invocation examples.

3. **Batch memory search**: Extending `memory_search` to accept multiple queries eliminates sequential round-trips when looking up multiple topics.

### Key Files

- `src/prompts/supervisor_system.md` — Supervisor system prompt template
- `src/supervisor.py` — Chat loop, tool assembly, `_build_system_prompt()` (lines 216-242)
- `src/tool_registry.py` — Tool definitions, categories, `CATEGORIES` dict, `get_core_tools()`, `search_relevant_categories()`
- `src/plugins/internal/memory.py` — Memory search tool definition (`TOOL_DEFINITIONS`) and `cmd_memory_search` handler
- `src/memory.py` — `MemoryManager` with `search()` method (line 1116)

---

## Phase 1: Add Complete Tool Name Index to Supervisor System Prompt

**Goal:** Include all tool names (grouped by category) in the supervisor system prompt so the LLM always knows what tools exist without needing `browse_tools`.

**Files to modify:**
- `src/tool_registry.py` — Add `get_tool_index() -> str` method
- `src/supervisor.py` — Inject tool index into system prompt via `_build_system_prompt()`
- `src/prompts/supervisor_system.md` — Update "Tool Navigation" section

**Changes:**

1. **In `src/tool_registry.py`**, add a new method `get_tool_index() -> str`:
   - Iterates all categories from `CATEGORIES`
   - For each category, collects tool names from both `_TOOL_CATEGORIES` mappings and plugin-registered tools
   - Returns a compact markdown string:
     ```
     **git**: git_status, git_commit, git_push, git_create_branch, ...
     **memory**: memory_search, memory_stats, memory_reindex, ...
     ```
   - One line per category, tool names only (no descriptions, no schemas)

2. **In `src/supervisor.py` `_build_system_prompt()`** (line 216), after setting identity and active project context, call `self._registry.get_tool_index()` and inject it via `builder.add_context("tool_index", tool_index_text)`.

3. **In `src/prompts/supervisor_system.md`**, update the "Tool Navigation" section (lines 24-37):
   - Change the discovery guidance to reference the injected tool index instead of `browse_tools`
   - New wording: "All available tools are listed by category in the Tool Index below. Call `load_tools(category=...)` to load a category's tools, or tools may already be pre-loaded based on your request."
   - Keep `browse_tools` / `load_tools` in the core tools list but de-emphasize `browse_tools` as a first step

**Expected impact:** Eliminates the `browse_tools` round-trip (~2-5s) in most conversations.

---

## Phase 2: Add Memory Search Guidance to Supervisor Prompt

**Goal:** Tell the supervisor to use `memory_search` to look up how to call unfamiliar tools.

**Files to modify:**
- `src/prompts/supervisor_system.md` — Add guidance in the Tool Navigation section

**Changes:**

1. Add a new subsection after the tool index guidance in `supervisor_system.md`:
   ```markdown
   ### Tool Usage Lookup

   If you need to call a tool but aren't sure of its exact parameters, use
   `memory_search` to look up past usage examples:
   - Search for the tool name (e.g., query: "git_create_branch parameters")
   - Past task results and notes often contain examples of successful tool invocations
   - This is faster than guessing parameter names or asking the user
   - Use the `queries` array parameter to look up multiple tools in one call
   ```

2. Position this after the tool index and before the core tools list, creating a natural flow: see tool names → look up how to use them → load them.

**Expected impact:** Reduces failed tool calls and retry loops when the LLM encounters unfamiliar tools.

---

## Phase 3: Extend Memory Search to Support Multiple Queries

**Goal:** Allow `memory_search` to accept multiple queries in a single call, reducing round-trips.

**Files to modify:**
- `src/memory.py` — Add `batch_search()` method to `MemoryManager`
- `src/plugins/internal/memory.py` — Update tool definition and `cmd_memory_search` handler
- Tests for the new functionality

**Changes:**

### 3a. Add `MemoryManager.batch_search()` method

In `src/memory.py`, add alongside the existing `search()` method (after line 1132):

```python
async def batch_search(
    self, project_id: str, workspace_path: str,
    queries: list[str], top_k: int = 10,
) -> dict[str, list[dict]]:
    """Run multiple semantic searches concurrently.

    Returns a dict mapping each query string to its results list.
    Individual query failures return empty lists without blocking others.
    """
    instance = await self.get_instance(project_id, workspace_path)
    if not instance:
        return {q: [] for q in queries}

    async def _single(q: str) -> tuple[str, list[dict]]:
        try:
            results = await instance.search(q, top_k=top_k)
            return (q, results if results else [])
        except Exception as e:
            logger.warning("Memory batch_search query %r failed: %s", q, e)
            return (q, [])

    pairs = await asyncio.gather(*[_single(q) for q in queries])
    return dict(pairs)
```

### 3b. Update `memory_search` tool schema

In `src/plugins/internal/memory.py`, update the `memory_search` entry in `TOOL_DEFINITIONS`:

```python
{
    "name": "memory_search",
    "description": (
        "Search project memory for relevant context. Returns semantically "
        "similar past task results, notes, and knowledge-base entries. "
        "Supports single query (via 'query') or multiple concurrent queries "
        "(via 'queries' array) for batch lookups."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID to search memory for"},
            "query": {"type": "string", "description": "Single semantic search query"},
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Multiple search queries to run concurrently. Results are "
                    "returned grouped by query. Use instead of 'query' when "
                    "looking up multiple topics at once."
                ),
            },
            "top_k": {"type": "integer", "description": "Results per query (default 10)", "default": 10},
        },
        "required": ["project_id"],
    },
}
```

### 3c. Update `cmd_memory_search` handler

In the handler method, add multi-query support while keeping backward compatibility:

- If `queries` is provided (array): call `MemoryManager.batch_search()`, return results grouped by query
- If `query` is provided (string): existing single-query behavior, unchanged
- If neither: return error `"Either 'query' or 'queries' is required"`

Multi-query response format:
```python
{
    "project_id": "...",
    "queries": ["q1", "q2"],
    "top_k": 10,
    "results_by_query": {
        "q1": [{"rank": 1, "source": "...", "heading": "...", "content": "...", "score": 0.87}, ...],
        "q2": [{"rank": 1, "source": "...", "heading": "...", "content": "...", "score": 0.72}, ...],
    },
    "total_count": 15,
}
```

### 3d. Update CLI formatter

Update `_fmt_memory_search` in the same file to handle the grouped response format. When `results_by_query` is present (multi-query mode), render results under each query heading.

### 3e. Tests

Add tests to verify:
- `batch_search` with multiple queries returns correct dict structure
- `batch_search` with empty queries list returns empty dict
- `batch_search` with one failing query still returns results for others
- `cmd_memory_search` backward compatibility (single `query` still works identically)
- `cmd_memory_search` with `queries` array returns `results_by_query` structure
- Error when neither `query` nor `queries` provided
