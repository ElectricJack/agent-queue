---
auto_tasks: true
---

# Plan: Improve Supervisor Agent Speed via Tool Name Inclusion & Batch Memory Search

## Problem Statement

The supervisor agent currently uses a **two-step tool discovery pattern**: first calling `browse_tools` to see categories, then `load_tools(category=...)` to load specific categories. Each of these is a full LLM round-trip, adding latency before the supervisor can actually use the tools it needs.

Additionally, when the supervisor (or worker agents) need to recall how to use specific tools, there's no efficient way to look up tool calling conventions from memory — each `memory_search` call retrieves results for only a single query, requiring multiple sequential LLM round-trips to search for multiple topics.

### Current Architecture

- **~137 total tools** across 10 categories (git, project, agent, rules, memory, notes, files, task, plugin, system)
- **Core tools** (~21) are always loaded with full schemas
- **Category tools** loaded on-demand via `load_tools` or pre-loaded via keyword matching (`search_relevant_categories`)
- **Supervisor system prompt** (`src/prompts/supervisor_system.md`) lists only core tool names; non-core tools require `browse_tools` → `load_tools` round-trips
- **Memory search** (`memory_search` tool + `MemoryManager.search()`) accepts a single query string only
- **MemSearch library** exposes `search(query, top_k)` — single query per call, but multiple calls can be made concurrently with `asyncio.gather`

### Key Insight

Including a **complete tool name index** (just names grouped by category — no schemas) in the system prompt costs very few tokens (~300-400) but eliminates 1-2 LLM round-trips for tool discovery. The supervisor can see all available tool names immediately, decide which categories to load, and call `load_tools` in the same turn it starts working — or skip `browse_tools` entirely.

Combined with a recommendation to use `memory_search` to look up tool calling conventions, and extending `memory_search` to support multiple queries in a single call, this creates a fast path: see tool name → search memory for usage pattern → call the tool, all in fewer LLM turns.

---

## Phase 1: Add Tool Name Index to Supervisor System Prompt

**Files to modify:**
- `src/prompts/supervisor_system.md` — Add a "Tool Index" section listing all tool names by category
- `src/supervisor.py` — In `_build_system_prompt`, dynamically generate the tool index from `ToolRegistry` and inject it as a context block via `PromptBuilder.add_context()`

**Implementation details:**

1. In `src/supervisor.py`, before building the system prompt, call `ToolRegistry().get_categories()` and for each category call `get_category_tool_names(category)` to collect a flat list of tool names per category.

2. Format as a compact index block (example):
   ```
   ## Tool Index (load with `load_tools(category="...")`)

   **git:** checkout_branch, commit_changes, create_branch, git_diff, git_log, git_merge, git_pull, git_push, git_create_pr, ...
   **project:** list_projects, create_project, edit_project, delete_project, add_workspace, ...
   **task:** stop_task, restart_task, approve_task, get_task_result, get_task_tree, ...
   **agent:** list_agents, list_profiles, create_profile, get_profile, ...
   **rules:** browse_rules, fire_rule, toggle_rule, rule_runs, ...
   **memory:** memory_search, memory_stats, view_profile, regenerate_profile, compact_memory, ...
   **notes:** list_notes, read_note, write_note, append_note, delete_note, promote_note, ...
   **files:** read_file, write_file, edit_file, glob_files, grep, ...
   **system:** get_status, get_token_usage, reload_config, shutdown, ...
   **plugin:** plugin_list, plugin_install, plugin_remove, plugin_reload, ...
   ```

3. Inject this as a `PromptBuilder.add_context("tool_index", ...)` block so it appears in the system prompt dynamically (reflecting actual registered tools including plugins).

4. Update the "Tool Navigation" section in `supervisor_system.md` to reference the index:
   - Remove the instruction to call `browse_tools` as a first discovery step
   - Instead: "Consult the Tool Index below to find the tool you need. Call `load_tools(category=...)` to load its category, then call the tool."
   - Add: "If you're unsure how to call a tool, use `memory_search` to look up past usage examples and calling conventions."

**Token budget:** ~300-400 tokens for the index. This is a small fraction of the typical system prompt (~2000-3000 tokens) and eliminates 1-2 round-trips that each cost 500-2000+ tokens in input/output.

---

## Phase 2: Add Memory Search Guidance to Supervisor Prompts

**Files to modify:**
- `src/prompts/supervisor_system.md` — Add guidance on using memory search for tool usage lookup

**Implementation details:**

1. Add a subsection under "Tool Navigation" that recommends memory search as the go-to method for understanding how to call unfamiliar tools:

   ```markdown
   ### Tool Usage Lookup

   When you need to use a tool you haven't used recently, search memory for
   usage examples rather than guessing at parameters:

   - `memory_search(project_id, queries=["how to call git_create_pr", "PR creation workflow"])`
   - Past task results often contain successful tool invocations you can reference
   - The project profile may document preferred tool calling patterns
   ```

2. This guidance complements the tool index: the index tells the supervisor *what* tools exist, and memory search tells it *how* to use them. Together they eliminate the pattern of loading a category just to read the schema, then guessing at parameters.

---

## Phase 3: Extend `memory_search` to Support Multiple Queries

**Files to modify:**
- `src/plugins/internal/memory.py` — Update `memory_search` tool definition and handler to accept `queries` (array) in addition to `query` (string)
- `src/memory.py` — Add `search_multi()` method that runs multiple queries concurrently via `asyncio.gather`

**Implementation details:**

### 3a. Add `MemoryManager.search_multi()` method

In `src/memory.py`, add a new method alongside `search()`:

```python
async def search_multi(
    self,
    project_id: str,
    workspace_path: str,
    queries: list[str],
    top_k: int = 10,
) -> dict[str, list[dict]]:
    """Run multiple semantic searches concurrently, returning results keyed by query."""
    instance = await self.get_instance(project_id, workspace_path)
    if not instance:
        return {q: [] for q in queries}

    async def _single(q: str) -> tuple[str, list[dict]]:
        try:
            results = await instance.search(q, top_k=top_k)
            return (q, results)
        except Exception:
            return (q, [])

    pairs = await asyncio.gather(*[_single(q) for q in queries])
    return dict(pairs)
```

This runs all queries concurrently against the same MemSearch instance, which is safe since MemSearch uses Milvus (thread-safe client).

### 3b. Update `memory_search` tool schema

In `src/plugins/internal/memory.py`, modify the tool definition to accept either `query` (string, single) or `queries` (array, multiple):

```python
"input_schema": {
    "type": "object",
    "properties": {
        "project_id": {"type": "string", ...},
        "query": {
            "type": "string",
            "description": "Single semantic search query (use this OR queries, not both)",
        },
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Multiple search queries to run concurrently. Results returned grouped by query.",
        },
        "top_k": {"type": "integer", "default": 10, ...},
    },
    "required": ["project_id"],
    # At least one of query/queries must be provided — validated in handler
}
```

### 3c. Update handler

In the `cmd_memory_search` handler:
- If `queries` is provided, call `search_multi()` and return results keyed by query
- If `query` is provided (backwards compatible), call existing `search()` as today
- If neither is provided, return an error

**Response format for multi-query:**
```python
{
    "project_id": "...",
    "results_by_query": {
        "query 1": [{"rank": 1, "source": "...", ...}, ...],
        "query 2": [{"rank": 1, "source": "...", ...}, ...],
    },
    "total_results": 15,
}
```

### 3d. Tests

Add tests in `tests/test_memory.py`:
- Test `search_multi` with multiple queries returns correct structure
- Test `search_multi` with empty queries list returns empty dict
- Test `search_multi` with one failing query still returns results for others
- Test `memory_search` tool handler backwards compatibility (single `query` still works)
- Test `memory_search` tool handler with `queries` array
