---
tags: [design, memory, plugin, milvus, memsearch]
---

# Memory Plugin v2

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#8 plugins own their dependencies, #10 fewer moving parts)
**Related:** [[vault]], [[memory-scoping]], [[self-improvement]], [[specs/plugin-system]]

---

## 1. Overview

The memory system is implemented as a **self-contained internal plugin** that replaces
the current `memory.py` MemoryManager (2958 lines) and memory plugin facade (1046
lines) with a unified v2 plugin.

---

## 2. Current Architecture (Being Replaced)

```
MemoryPlugin (thin facade, 1046 lines)
    → MemoryService (protocol wrapper)
        → MemoryManager (implementation, 2958 lines)
            → memsearch (external, Milvus vector DB)
            → filesystem (markdown files)
```

Three layers, two files, scattered responsibilities. The MemoryManager owns
profiles, factsheets, knowledge topics, consolidation, compaction, and indexing.
The plugin just translates tool calls.

---

## 3. New Architecture

```
MemoryPlugin v2 (self-contained internal plugin)
    → memsearch fork (unified backend)
        → Milvus (single .db file or server)
            ├── Vector fields (semantic search)
            ├── Scalar fields (KV lookups via query/get)
            └── Metadata fields (tags, timestamps, scope)
    → Vault filesystem (human-readable source of truth)
```

One plugin. One external dependency. One storage backend. The plugin is the single
gateway for all memory operations — agents, playbooks, and the orchestrator all go
through the same interface.

---

## 4. Why a Plugin (Not Core)

- **Self-contained.** The plugin brings its own storage (Milvus via memsearch fork).
  No dependency on the host system's database. Plugins should never require the core
  infrastructure's database — that creates tight coupling.
- **Replaceable.** A different memory implementation could swap in as a plugin without
  touching core code.
- **Plugin architecture stress test.** This exercises the plugin system's ability to
  handle a complex internal subsystem, validating the architecture for future
  sophisticated plugins.
- **Clean boundary.** The core system doesn't need to know how memory works. It calls
  `ctx.get_service("memory")` and gets results.

---

## 5. memsearch Fork

The upstream [memsearch](https://github.com/zilliztech/memsearch) package provides
vector search over markdown files using Milvus. Our fork extends it with:

1. **Key-value storage.** Milvus already supports pure scalar queries via
   `query(filter='key == "test_command"')` and primary key lookups via `get(ids=[...])`.
   The fork adds a KV-oriented collection schema and convenience methods.
2. **Multi-collection search.** Query multiple collections in parallel with weighted
   result merging for scope-based retrieval.
3. **Scope-aware operations.** Collections are named by scope (`aq_system`,
   `aq_agenttype_coding`, `aq_project_mechfighters`). The fork handles scope
   resolution and routing.
4. **Metadata indexing.** Tags, timestamps, retrieval counts, and source tracking
   as scalar fields alongside vectors.

Milvus is a hybrid database — it natively supports vector similarity search AND
scalar field queries in the same collection. Key-value lookups use `query()` with
filter expressions, no vector computation needed. This means both semantic search
and exact KV retrieval go through one backend with zero additional infrastructure.

---

## 6. KV Storage in Milvus

Each scope's collection includes entries that are pure key-value (no vector):

```python
# KV schema within each Milvus collection
kv_fields = [
    FieldSchema("entry_id", DataType.VARCHAR, is_primary=True),
    FieldSchema("entry_type", DataType.VARCHAR),   # "document" | "kv"
    FieldSchema("kv_namespace", DataType.VARCHAR),  # "project", "conventions", "stats"
    FieldSchema("kv_key", DataType.VARCHAR),
    FieldSchema("kv_value", DataType.VARCHAR),      # JSON-encoded
    FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=768),  # null/zero for KV entries
    FieldSchema("content", DataType.VARCHAR),
    FieldSchema("source", DataType.VARCHAR),
    FieldSchema("tags", DataType.VARCHAR),           # JSON array
    FieldSchema("updated_at", DataType.INT64),
]
```

KV entries have `entry_type = "kv"` and are queried via scalar filters:

```python
# Exact KV lookup — no vector search, pure scalar query
results = collection.query(
    filter='entry_type == "kv" AND kv_namespace == "project" AND kv_key == "test_command"',
    output_fields=["kv_value"]
)

# List all KV entries in a namespace
results = collection.query(
    filter='entry_type == "kv" AND kv_namespace == "conventions"',
    output_fields=["kv_key", "kv_value"]
)
```

Document entries (memory files with embeddings) have `entry_type = "document"` and
are queried via vector similarity as before.

---

## 7. Milvus Backend Topology

### One Collection per Memory Scope

Each scope maps to a single Milvus collection containing both document entries
(with embeddings for semantic search) and KV entries (scalar-only for exact lookup):

| Scope | Collection Name | Indexes |
|---|---|---|
| System | `aq_system` | `vault/system/memory/`, `vault/system/facts.md` |
| Orchestrator | `aq_orchestrator` | `vault/orchestrator/memory/`, `vault/orchestrator/facts.md` |
| Agent type | `aq_agenttype_{type}` | `vault/agent-types/{type}/memory/`, `vault/agent-types/{type}/facts.md` |
| Project | `aq_project_{id}` | `vault/projects/{id}/memory/`, `vault/projects/{id}/notes/`, `vault/projects/{id}/references/`, `vault/projects/{id}/facts.md` |

Project collections also index **workspace specs and docs** directly from the project
repo (not duplicated into the vault). This is the existing workspace indexing behavior,
retained.

### Fact Files (KV Source of Truth)

Each scope can have a `facts.md` in the [[vault]] — a human-readable file of structured
key-value data that gets synced to Milvus KV entries:

```markdown
---
tags: [facts, auto-updated]
---

# Project Facts — Mech Fighters

## Project
- tech_stack: [Python 3.12, SQLAlchemy, Pygame]
- deploy_branch: main
- test_command: pytest tests/ -v
- repo_url: github.com/user/mech-fighters

## Conventions
- orm_pattern: repository
- naming: snake_case

## Stats
- total_tasks_completed: 47
- avg_task_tokens: 32000
```

The format is `key: value` pairs under markdown headings (namespaces). Parsed
deterministically (no LLM needed). The file watcher detects changes and syncs
to Milvus KV entries. Agents can also write KV entries via MCP tools, which
update both the Milvus collection and the vault fact file.

### Tag-Based Cross-Scope Discovery

Standard search is scoped. But some queries need to cross boundaries — "what
do we know about SQLite across all projects and agent types?"

Tags are stored as scalar fields in Milvus. A secondary search mode queries **all
collections** filtered by tag:

```python
async def search_by_tag(
    tag: str,
    limit: int = 10,
) -> list[MemoryResult]:
    """Search across ALL collections for memories with a specific tag."""
    # Uses Milvus scalar filter: tags LIKE '%"sqlite"%'
    ...
```

This is the mechanism for cross-cutting discovery. The Obsidian graph view renders
the same connections visually.
