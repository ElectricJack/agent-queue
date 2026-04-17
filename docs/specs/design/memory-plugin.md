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

## 6. Collection Schema

Each scope's collection uses a unified schema supporting three entry types: documents
(semantic search), KV pairs (exact lookup), and temporal facts (validity-windowed).

```python
# Unified schema for each Milvus collection
fields = [
    # Core identity
    FieldSchema("entry_id", DataType.VARCHAR, is_primary=True),
    FieldSchema("entry_type", DataType.VARCHAR),       # "document" | "kv" | "temporal"

    # Vector search (documents only)
    FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=768),  # zero for KV/temporal
    FieldSchema("content", DataType.VARCHAR),           # Summary text (indexed)
    FieldSchema("original", DataType.VARCHAR),          # Full original (not indexed)

    # KV fields
    FieldSchema("kv_namespace", DataType.VARCHAR),      # "project", "conventions", "stats"
    FieldSchema("kv_key", DataType.VARCHAR),
    FieldSchema("kv_value", DataType.VARCHAR),          # JSON-encoded

    # Temporal validity (KV and temporal entries)
    FieldSchema("valid_from", DataType.INT64),          # Unix timestamp, 0 = always
    FieldSchema("valid_to", DataType.INT64),            # Unix timestamp, 0 = current/open

    # Topic filtering (documents)
    FieldSchema("topic", DataType.VARCHAR),             # e.g., "authentication", "testing"

    # Metadata (all entry types)
    FieldSchema("source", DataType.VARCHAR),            # Vault file path
    FieldSchema("tags", DataType.VARCHAR),              # JSON array
    FieldSchema("updated_at", DataType.INT64),
]
```

### Entry Types

**`document`** — Memory files with embeddings for semantic search. The `content`
field holds a summary (optimized for retrieval); `original` holds the full text.
`topic` enables pre-filtering before vector search (see [[memory-scoping]] Section 3).

**`kv`** — Key-value pairs for exact lookup. No embedding needed. Queried via
scalar filters on `kv_namespace` and `kv_key`.

**`temporal`** — Facts with validity windows. Like KV entries but with `valid_from`
and `valid_to` timestamps. Enables "as-of" queries and automatic expiry detection.

### KV Queries

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

### Temporal Queries

```python
import time

# Current value — valid_to is 0 (open) or in the future
now = int(time.time())
results = collection.query(
    filter=f'entry_type == "temporal" AND kv_key == "deploy_branch" '
           f'AND valid_from <= {now} AND (valid_to == 0 OR valid_to > {now})',
    output_fields=["kv_value", "valid_from", "valid_to"]
)

# Historical "as-of" query — what was the deploy branch on a specific date?
as_of = int(datetime(2026, 3, 15).timestamp())
results = collection.query(
    filter=f'entry_type == "temporal" AND kv_key == "deploy_branch" '
           f'AND valid_from <= {as_of} AND (valid_to == 0 OR valid_to > {as_of})',
    output_fields=["kv_value", "valid_from", "valid_to"]
)

# Full history of a key
results = collection.query(
    filter='entry_type == "temporal" AND kv_key == "deploy_branch"',
    output_fields=["kv_value", "valid_from", "valid_to"],
)
# Returns: [("main", 0, 1710000000), ("release", 1710000000, 0)]
```

### Temporal Fact Lifecycle

When a temporal fact is updated (e.g., deploy branch changes from `main` to `release`):
1. The current entry's `valid_to` is set to now (closing the validity window)
2. A new entry is created with `valid_from` = now and `valid_to` = 0 (open)
3. Both entries persist — the history is preserved
4. The vault `facts.md` file is updated to show the current value

The reflection playbook can use temporal history to detect patterns: "this project
changes deploy branches frequently" or "this config was stable for 6 months then
changed — investigate why."

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

---

## 8. Open Questions

1. **Embedding model consistency.** If the embedding model changes (e.g., upgrade from
   one provider to another), all collections need re-indexing. How do we handle this
   gracefully — background re-index? Version tracking per collection?
