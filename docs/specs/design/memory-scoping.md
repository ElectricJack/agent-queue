---
tags: [design, memory, scoping, tools]
---

# Memory Scoping & Agent Tools

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#6 specificity wins, #9 simple interfaces)
**Related:** [[memory-plugin]], [[vault]], [[profiles]], [[self-improvement]], [[playbooks]]

---

## 1. Overview

Memory is organized into **scopes** that form a specificity hierarchy and **tiers**
that control when knowledge is loaded. Not all memory requires a search — some
knowledge is important enough to be pre-loaded into every agent context. Agents
interact with memory through a unified set of MCP tools that handle scope resolution
and tier management automatically.

Inspired by tiered memory architectures (see
[MemPalace](https://github.com/milla-jovovich/mempalace)), the system distinguishes
between knowledge that is always present, knowledge loaded on-demand by topic, and
knowledge found through deep semantic search.

---

## 2. Memory Tiers (L0–L3)

Not everything should require a search query. Memory is organized into four tiers
based on how and when it's loaded into agent context:

| Tier | Name | Token Budget | When Loaded | What It Contains |
|---|---|---|---|---|
| **L0** | Identity | ~50 tokens | Always | Agent type role description from [[profiles]] `## Role` section |
| **L1** | Critical Facts | ~200 tokens | Always at task start | Project `facts.md` KV entries + agent-type `facts.md` entries. Eagerly loaded, no search needed. |
| **L2** | Topic Context | ~500 tokens | On-demand by topic | Memories filtered by `topic` field matching the current work area. Loaded when the agent enters a topic or the playbook specifies one. |
| **L3** | Deep Search | Variable | Explicit query | Full semantic search across all scopes. Agent calls `memory_search` when it needs to find something not covered by L0–L2. |

### How Tiers Compose at Task Start

When an agent starts a task on project `mech-fighters`:

```
1. L0: Inject profile.md ## Role section (always present)
2. L1: Load project facts.md + agent-type facts.md KV entries
       → "tech_stack: [Python, SQLAlchemy, Pygame]"
       → "test_command: pytest tests/ -v"
       → "deploy_branch: main"
3. L2: If the task description mentions "combat system", pre-filter
       memories with topic: combat from project + agent-type scopes
       → "vibecop frequently catches unhandled None checks in combat systems"
4. L3: Available via memory_search tool if the agent needs more
```

L0 and L1 are **injected automatically** — the agent never needs to search for them.
L2 is **topic-triggered** — loaded when the context implies a topic. L3 is
**agent-initiated** — the agent decides when to search.

This tiering keeps the base context small (~250 tokens of always-present knowledge)
while ensuring critical facts are never missed because the agent forgot to search.

---

## 3. Topic Filtering

Memories can be categorized by **topic** — a structured field that enables
intra-scope filtering before vector search runs. This dramatically improves
retrieval precision for large collections.

### Why Topics Matter

Flat semantic search across a large collection returns noisy results. A project with
hundreds of memories about authentication, database, testing, deployment, and UI will
return a mix of all topics for any query. Filtering by topic first narrows the search
space, improving both precision and performance.

Evidence from similar systems shows structured scoping before semantic search can
improve retrieval by 30%+ compared to flat search alone.

### Topic Field

Every memory file can include a `topic` field in its frontmatter:

```markdown
---
tags: [insight, auto-generated]
topic: authentication
source_task: task-abc123
created: 2026-04-07
---

# OAuth token refresh requires explicit scope re-request

When refreshing expired OAuth tokens, the provider requires...
```

Topics are:
- **Optional** — memories without a topic are included in all searches (no filtering)
- **Free-form strings** — no predefined list, agents create topics naturally
- **Indexed as scalar fields** in Milvus for fast pre-filtering
- **Auto-detected** when possible — the `memory_save` tool can infer a topic from
  the content and the current task context

### Topic-Filtered Search

When a topic is known (from task context, playbook node, or explicit query), the
search pipeline adds a metadata filter before vector similarity:

```python
async def search(
    query: str,
    agent_type: str,
    project_id: str,
    topic: str | None = None,
    limit: int = 10,
) -> list[MemoryResult]:
    """Semantic search with optional topic pre-filtering."""
    filter_expr = f'topic == "{topic}"' if topic else None
    results = await asyncio.gather(
        self._search_collection(f"aq_project_{project_id}", query,
                                weight=1.0, filter=filter_expr),
        self._search_collection(f"aq_agenttype_{agent_type}", query,
                                weight=0.7, filter=filter_expr),
        self._search_collection(f"aq_system", query,
                                weight=0.4, filter=filter_expr),
    )
    return merge_and_rank(results, limit=limit)
```

If the topic filter returns too few results (< 3), the search automatically falls
back to unfiltered search to avoid missing relevant cross-topic knowledge.

---

## 4. Scope Hierarchy (Broadest to Most Specific)

```
system
  └── agent-type (e.g., coding)
       └── project (e.g., mech-fighters)
            └── project + agent-type override (e.g., mech-fighters/overrides/coding.md)
```

### How Scopes Compose

When a coding agent working on mech-fighters searches memory, the system queries
multiple vector collections and merges results weighted by specificity:

1. **Project override** (`projects/mech-fighters/overrides/coding.md`) — highest weight
2. **Project memory** (`projects/mech-fighters/memory/`) — project-specific knowledge
3. **Agent-type memory** (`agent-types/coding/memory/`) — cross-project coding wisdom
4. **System memory** (`system/memory/`) — lowest weight, broadest knowledge

Results are interleaved by relevance score adjusted for scope weight. A moderately
relevant project-specific memory outranks a highly relevant system memory.

---

## 5. Override Model

Overrides are **freeform English** that supplement or tweak the parent agent-type
[[profiles|profile]] for a specific project. They are not structured config — an LLM
interprets them as contextual guidance.

Example `projects/mech-fighters/overrides/coding.md`:

```markdown
---
tags: [override, coding, mech-fighters]
agent_type: coding
---

# Coding Agent Overrides — Mech Fighters

This project uses a custom ECS framework. Do not use inheritance for
game entities — always use composition via the component system.

Prefer integration tests that spin up the full game loop over unit
tests of individual components. The component system has too many
implicit interactions for isolated unit tests to catch real bugs.

The project has a custom asset pipeline — never modify files in
assets/generated/ directly. Always edit the source files in
assets/source/ and run the pipeline.
```

The override is injected into the agent's context alongside its base profile. The
LLM resolves any tension between the base profile and the override naturally, with
the override taking precedence as the more specific guidance.

---

## 6. Multi-Scope Query

The [[memory-plugin]] queries all relevant collections in parallel and merges
results. This applies to both semantic search and KV lookups. Searches can
optionally be filtered by topic (see Section 3) for improved precision.

```python
async def recall(
    key: str,
    agent_type: str,
    project_id: str,
    namespace: str | None = None,
) -> str | None:
    """KV lookup with scope resolution. First match wins (most specific)."""
    for scope in [f"aq_project_{project_id}", f"aq_agenttype_{agent_type}", "aq_system"]:
        result = self._kv_get(scope, key, namespace)
        if result is not None:
            return result
    return None
```

KV lookups follow **first-match-wins** (most specific scope first). Semantic search
uses **weighted merging** (all scopes contribute, specificity boosts ranking).
See Section 3 for the full `search()` signature with topic filtering.

Override files (`overrides/coding.md`) are indexed into the project collection. They
are found by project-scope search and naturally weighted highest.

---

## 7. Agent Memory Tools (MCP)

Agents interact with memory through MCP tools, not direct file access. This ensures
proper indexing, deduplication, and file placement.

**Semantic (unstructured knowledge):**

| Tool | Description |
|---|---|
| `memory_search` | Semantic search across relevant scopes (vector similarity) |
| `memory_save` | Save an insight/learning as a memory file (with dedup) |
| `memory_list` | Browse memories in a scope |

**Key-value (structured facts):**

| Tool | Description |
|---|---|
| `memory_recall` | Exact KV lookup by key, with scope resolution (most specific wins) |
| `memory_store` | Store a key-value pair in the appropriate scope |
| `memory_list_facts` | List all KV entries in a scope/namespace |

**Unified (auto-routing):**

| Tool | Description |
|---|---|
| `memory_get` | Smart retrieval: tries KV exact match first, falls back to semantic search. Agents use this when they're not sure which retrieval strategy is appropriate |

The `memory_` prefix keeps all tools grouped and discoverable. Agents can use the
specific tools when they know what they want, or `memory_get` when they don't.

---

## 8. `memory_save` Flow

```
Agent calls memory_save(content, tags, topic?)
       │
       ▼
  MemoryManager determines scope:
    - Agent type → vault/agent-types/{type}/memory/
    - Project (if specified) → vault/projects/{id}/memory/insights/
       │
       ▼
  Check for duplicates (semantic similarity search in target scope)
       │
       ├── Similarity > 0.95: Near-identical. Update timestamp on existing
       │   memory, append source task reference. No content change needed.
       │
       ├── Similarity 0.8–0.95: Related. Invoke LLM to merge:
       │   - Provide both old and new content
       │   - Instructions: "Combine these into a single memory. If they
       │     contradict, prefer the newer information but note the change.
       │     Preserve tags from both."
       │   - Write merged content back to existing file
       │   - Update vector embedding for the merged version
       │
       ├── Similarity < 0.8: Distinct. Create new file.
       │
       ▼
  Write/update markdown file with frontmatter (tags, timestamp, source task)
       │
       ▼
  Index into appropriate vector collection
       │
       ▼
  Return confirmation to agent with action taken (created/merged/deduplicated)
```

The merge LLM call is lightweight — it's combining two short documents, not
reasoning about a complex problem. It uses a cheap/fast model.

---

## 9. Summary + Original Pattern

When saving a memory, the system stores both a **summary** (optimized for search)
and the **original content** (available for full context):

- The **summary** is the indexed document in the vector collection. It's concise,
  focused on the key insight, and produces better search results than verbose originals.
- The **original** is preserved in the memory file's body below the summary, or in
  a linked attachment for very large content.

This pattern means retrieval returns focused, relevant summaries. If the agent needs
the full context, it can request the original via the `source` field.

For `memory_save`, the flow is:
1. Agent provides full content
2. If content exceeds ~200 tokens, the system generates a summary (cheap LLM call)
3. Summary is embedded and indexed; original is stored in the file body
4. Search results return summaries; `memory_get` with `full=true` returns the original

Short insights (< 200 tokens) are stored as-is — they're already summary-length.

---

## 10. Reflection Playbook (Periodic Consolidation)

Agents write insights immediately during task execution. A separate
**[[playbooks|reflection playbook]]** runs periodically to:

1. Review recent task records and logs for the agent type
2. Extract patterns the agent didn't catch in real-time
3. Consolidate overlapping memories (merge duplicates, update outdated ones)
4. Promote insights that apply broadly (re-tag for wider scope)
5. Archive memories that are no longer relevant

This is the "step back and think" complement to the "capture in the moment" writes.
The combination ensures both immediate learning and systematic knowledge management.
