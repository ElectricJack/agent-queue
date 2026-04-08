---
tags: [design, memory, scoping, tools]
---

# Memory Scoping & Agent Tools

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#6 specificity wins, #9 simple interfaces)
**Related:** [[memory-plugin]], [[vault]], [[profiles]], [[self-improvement]], [[playbooks]]

---

## 1. Overview

Memory is organized into **scopes** that form a specificity hierarchy. More specific
scopes override or supplement broader ones. Agents interact with memory through a
unified set of MCP tools that handle scope resolution automatically.

---

## 2. Scope Hierarchy (Broadest to Most Specific)

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

## 3. Override Model

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

## 4. Multi-Scope Query

The [[memory-plugin]] queries all relevant collections in parallel and merges
results. This applies to both semantic search and KV lookups:

```python
async def search(
    query: str,
    agent_type: str,
    project_id: str,
    limit: int = 10,
) -> list[MemoryResult]:
    """Semantic search across all relevant scopes, weighted by specificity."""
    results = await asyncio.gather(
        self._search_collection(f"aq_project_{project_id}", query, weight=1.0),
        self._search_collection(f"aq_agenttype_{agent_type}", query, weight=0.7),
        self._search_collection(f"aq_system", query, weight=0.4),
    )
    return merge_and_rank(results, limit=limit)

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

Override files (`overrides/coding.md`) are indexed into the project collection. They
are found by project-scope search and naturally weighted highest.

---

## 5. Agent Memory Tools (MCP)

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

## 6. `memory_save` Flow

```
Agent calls memory_save(content, tags)
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

## 7. Reflection Playbook (Periodic Consolidation)

Agents write insights immediately during task execution. A separate
**[[playbooks|reflection playbook]]** runs periodically to:

1. Review recent task records and logs for the agent type
2. Extract patterns the agent didn't catch in real-time
3. Consolidate overlapping memories (merge duplicates, update outdated ones)
4. Promote insights that apply broadly (re-tag for wider scope)
5. Archive memories that are no longer relevant

This is the "step back and think" complement to the "capture in the moment" writes.
The combination ensures both immediate learning and systematic knowledge management.
