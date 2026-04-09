"""Internal plugin: Memory v2 — unified memory operations via memsearch/Milvus.

Replaces the v1 MemoryPlugin (``memory.py``) with a single self-contained
plugin backed by a memsearch fork and Milvus.  See the design spec at
``docs/specs/design/memory-plugin.md`` (especially Sections 3–4) for the full
architecture.

Key differences from v1
-----------------------
* **One backend** — Milvus (via memsearch fork) replaces filesystem-only storage.
* **Unified collection schema** — documents (vector), KV pairs (scalar), and
  temporal facts all live in the same per-scope collection.
* **Scope-aware** — collections named by scope (``aq_system``,
  ``aq_project_{id}``, etc.) with cross-scope tag search.
* **Vault as source of truth** — human-readable markdown files in the vault
  directory are the canonical representation; Milvus is a derived index.

Transition (v1 + v2 coexistence)
--------------------------------
Both v1 (``MemoryPlugin``) and v2 (``MemoryV2Plugin``) are active during the
transition period.  v1 continues to own existing tool names (``memory_search``,
``view_profile``, etc.) while v2 registers **only** the new tool names that
are unique to the v2 architecture:

* ``memory_search_by_tag`` — cross-scope tag search
* ``memory_kv_get``, ``memory_kv_set``, ``memory_kv_list`` — KV operations
* ``memory_fact_get``, ``memory_fact_set``, ``memory_fact_history`` — temporal facts

Once the memsearch backend is wired up and v2 is fully functional, v1 will be
deprecated and v2 will take over all tool names.

Status: **connected** — v2-only commands are wired to MemoryV2Service which
delegates to the memsearch fork (CollectionRouter + MilvusStore).  Overlapping
commands (memory_search, view_profile, etc.) remain stubs pending v1 deprecation.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from src.plugins.base import InternalPlugin, PluginContext

if TYPE_CHECKING:
    from src.memory_v2_service import MemoryV2Service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions — new v2 architecture
# ---------------------------------------------------------------------------

TOOL_CATEGORY = "memory"

# Tool names unique to v2 — only these are registered during the v1/v2
# transition.  Overlapping names (memory_search, view_profile, etc.) remain
# owned by v1's MemoryPlugin until v1 is fully deprecated.
V2_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "memory_save",
        "memory_search",
        "memory_search_by_tag",
        "memory_kv_get",
        "memory_kv_set",
        "memory_kv_list",
        "memory_fact_get",
        "memory_fact_set",
        "memory_fact_list",
        "memory_fact_history",
        "memory_fact_recall",
        "memory_recall",
        "memory_get",
        "memory_list",
    }
)

TOOL_DEFINITIONS: list[dict] = [
    # ---- Save (spec §8) ----
    {
        "name": "memory_save",
        "description": (
            "Save an insight or learning as a memory file with automatic "
            "deduplication.  Checks for semantically similar existing memories "
            "and either creates a new file (distinct), merges with an existing "
            "one (related, similarity 0.8–0.95), or updates the timestamp on a "
            "near-duplicate (similarity > 0.95).  Writes to the vault as a "
            "markdown file with frontmatter and indexes into the scoped Milvus "
            "collection for vector search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines scope collection).",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The insight or learning to save.  For short insights "
                        "(< 200 tokens) this is stored as-is.  For longer content "
                        "a summary is generated and the original is preserved."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tags for the memory (e.g. ['insight', 'authentication', "
                        "'bug-fix']).  Defaults to ['insight', 'auto-generated']."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional topic for intra-scope filtering (e.g. "
                        "'authentication', 'testing', 'deployment').  Improves "
                        "retrieval precision by 30%+."
                    ),
                },
                "source_task": {
                    "type": "string",
                    "description": "Task ID that produced this insight (for provenance).",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Memory scope.  One of 'system', 'orchestrator', "
                        "'agenttype_{type}', or 'project_{id}'.  Defaults to "
                        "the project scope derived from project_id."
                    ),
                },
            },
            "required": ["project_id", "content"],
        },
    },
    # ---- Semantic Search ----
    {
        "name": "memory_search",
        "description": (
            "Semantic search across project memory using vector similarity. "
            "Searches the scoped Milvus collection for document entries whose "
            "embeddings are closest to the query. Supports single query (via "
            "'query') or batch queries (via 'queries' array). Results can be "
            "filtered by topic and limited by scope."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID whose collection to search.",
                },
                "query": {
                    "type": "string",
                    "description": "Single semantic search query.",
                },
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Multiple search queries to run concurrently.  Results "
                        "are returned grouped by query.  Use instead of 'query' "
                        "for batch lookups."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Memory scope to search.  One of 'system', 'orchestrator', "
                        "'agenttype_{type}', or 'project_{id}'.  Defaults to the "
                        "project scope derived from project_id."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional topic filter (e.g. 'authentication', 'testing'). "
                        "When set, only document entries tagged with this topic are "
                        "considered for vector search."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum results per query (default 10).",
                    "default": 10,
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "memory_search_by_tag",
        "description": (
            "Cross-scope tag search — queries ALL collections for entries "
            "matching a specific tag.  Use for cross-cutting discovery like "
            "'what do we know about SQLite across all projects?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Tag to search for across all scopes.",
                },
                "entry_type": {
                    "type": "string",
                    "enum": ["document", "kv", "temporal"],
                    "description": "Optional filter by entry type.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 10).",
                    "default": 10,
                },
            },
            "required": ["tag"],
        },
    },
    # ---- KV Operations ----
    {
        "name": "memory_kv_get",
        "description": (
            "Exact key-value lookup via Milvus scalar query.  Retrieves an "
            "entry by namespace and key without any vector computation.  Fast "
            "O(1) lookups for structured data like project settings, "
            "conventions, or cached stats."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines scope collection).",
                },
                "namespace": {
                    "type": "string",
                    "description": ("KV namespace (e.g. 'project', 'conventions', 'stats')."),
                },
                "key": {
                    "type": "string",
                    "description": "The key to look up.",
                },
            },
            "required": ["project_id", "namespace", "key"],
        },
    },
    {
        "name": "memory_kv_set",
        "description": (
            "Store a key-value pair in the appropriate scope's Milvus "
            "collection and vault facts file.  Creates or updates an entry "
            "in the given namespace.  The value is also synced to the vault "
            "facts.md file for human-readable access and L1 tier injection "
            "at task start."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines default scope collection).",
                },
                "namespace": {
                    "type": "string",
                    "description": ("KV namespace (e.g. 'project', 'conventions', 'stats')."),
                },
                "key": {
                    "type": "string",
                    "description": "The key to set.",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "The value to store.  Stored as JSON-encoded string.  "
                        "For simple values pass the string directly."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Memory scope.  One of 'system', 'orchestrator', "
                        "'agenttype_{type}', or 'project_{id}'.  Defaults to "
                        "the project scope derived from project_id.  Use this "
                        "to write cross-project or system-wide facts."
                    ),
                },
            },
            "required": ["project_id", "namespace", "key", "value"],
        },
    },
    {
        "name": "memory_kv_list",
        "description": (
            "List all key-value entries in a namespace.  Returns keys and "
            "values without vector search — pure scalar query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines scope collection).",
                },
                "namespace": {
                    "type": "string",
                    "description": "KV namespace to list entries from.",
                },
            },
            "required": ["project_id", "namespace"],
        },
    },
    # ---- Scoped KV Recall (spec §6) ----
    {
        "name": "memory_fact_recall",
        "description": (
            "Exact KV lookup by key with automatic scope resolution.  "
            "Searches scopes in order of specificity — project → agent-type "
            "→ system — and returns the first match (most specific wins).  "
            "Use this instead of memory_kv_get when you want automatic "
            "scope fallback rather than querying a single scope."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The key to look up.",
                },
                "project_id": {
                    "type": "string",
                    "description": (
                        "Project ID.  When set, the project scope is "
                        "searched first (highest priority)."
                    ),
                },
                "agent_type": {
                    "type": "string",
                    "description": (
                        "Agent type name (e.g. 'coding').  When set, the "
                        "agent-type scope is searched second."
                    ),
                },
                "namespace": {
                    "type": "string",
                    "description": (
                        "Optional KV namespace filter (e.g. 'project', "
                        "'conventions').  When set, only entries in this "
                        "namespace are considered."
                    ),
                },
            },
            "required": ["key"],
        },
    },
    # ---- Unified Smart Recall (spec §7) ----
    {
        "name": "memory_recall",
        "description": (
            "Smart memory retrieval — tries KV exact match first, then "
            "falls back to semantic search.  Use when you're not sure "
            "whether the information is a structured fact or an "
            "unstructured insight.  For the KV attempt, the query is used "
            "as the key with scope resolution (project → agent-type → "
            "system).  If no KV match is found, performs multi-scope "
            "semantic search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query.  Used as the KV key for exact match "
                        "and as the semantic search query for fallback."
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": "Project ID for scope resolution.",
                },
                "agent_type": {
                    "type": "string",
                    "description": ("Agent type name (e.g. 'coding') for scope resolution."),
                },
                "namespace": {
                    "type": "string",
                    "description": (
                        "KV namespace for the exact-match attempt (e.g. 'project', 'conventions')."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": ("Topic filter for the semantic search fallback."),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max semantic search results (default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    # ---- Unified Auto-Routing (spec §7 — memory_get) ----
    {
        "name": "memory_get",
        "description": (
            "Get information from memory — the default retrieval tool.  "
            "Automatically routes between KV exact match and semantic search: "
            "first tries the query as a KV key (with scope resolution: "
            "project → agent-type → system), and if no exact match is found, "
            "falls back to multi-scope semantic search.  Use this when you "
            "just want to retrieve something from memory without choosing "
            "a retrieval strategy.  For explicit KV lookup use "
            "memory_fact_recall; for explicit semantic search use "
            "memory_search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to retrieve.  Used as the KV key for exact "
                        "match and as the semantic search query for fallback.  "
                        "Examples: 'deploy_branch' (KV hit), 'how does auth "
                        "work?' (semantic search)."
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": (
                        "Project ID for scope resolution.  When set, the "
                        "project scope is searched first (highest priority)."
                    ),
                },
                "agent_type": {
                    "type": "string",
                    "description": ("Agent type name (e.g. 'coding') for scope resolution."),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Topic filter for the semantic search fallback "
                        "(e.g. 'authentication', 'testing')."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max semantic search results if KV miss (default 5).",
                    "default": 5,
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "When true, return the original content instead of "
                        "the summary for semantic search results.  Use this "
                        "when you need the full context of a memory, not "
                        "just the search-optimized summary.  Per spec §9."
                    ),
                    "default": False,
                },
            },
            "required": ["query"],
        },
    },
    # ---- Temporal Facts ----
    {
        "name": "memory_fact_get",
        "description": (
            "Get the current value of a temporal fact.  Temporal facts have "
            "validity windows (valid_from / valid_to); this returns the entry "
            "whose window includes the current time (or a specified as-of time)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines scope collection).",
                },
                "key": {
                    "type": "string",
                    "description": "Temporal fact key (e.g. 'deploy_branch').",
                },
                "as_of": {
                    "type": "integer",
                    "description": (
                        "Optional Unix timestamp for historical 'as-of' query.  "
                        "Defaults to current time."
                    ),
                },
            },
            "required": ["project_id", "key"],
        },
    },
    {
        "name": "memory_fact_set",
        "description": (
            "Set a temporal fact.  Closes the validity window on the current "
            "value (sets valid_to = now) and creates a new entry with "
            "valid_from = now, valid_to = 0 (open).  Both old and new entries "
            "are preserved for history.  Also updates the vault facts file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines scope collection).",
                },
                "key": {
                    "type": "string",
                    "description": "Temporal fact key (e.g. 'deploy_branch').",
                },
                "value": {
                    "type": "string",
                    "description": "New value for the temporal fact.",
                },
            },
            "required": ["project_id", "key", "value"],
        },
    },
    {
        "name": "memory_fact_history",
        "description": (
            "Retrieve the full history of a temporal fact — all values it has "
            "held, with their validity windows.  Useful for detecting patterns "
            "(e.g. 'deploy branch changes frequently') or auditing changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines scope collection).",
                },
                "key": {
                    "type": "string",
                    "description": "Temporal fact key to get history for.",
                },
            },
            "required": ["project_id", "key"],
        },
    },
    {
        "name": "memory_fact_list",
        "description": (
            "List all temporal fact entries in a scope/namespace.  Returns "
            "keys and their current values without vector search — pure "
            "scalar query.  By default only currently-active facts are "
            "returned; set current_only=false to include superseded entries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines scope collection).",
                },
                "namespace": {
                    "type": "string",
                    "description": (
                        "Namespace to list facts from.  Empty string "
                        "(default) returns facts with no namespace."
                    ),
                    "default": "",
                },
                "current_only": {
                    "type": "boolean",
                    "description": (
                        "If true (default), only return currently-active "
                        "facts (valid_to == 0).  If false, include all "
                        "entries including superseded ones."
                    ),
                    "default": True,
                },
            },
            "required": ["project_id"],
        },
    },
    # ---- Browse / List ----
    {
        "name": "memory_list",
        "description": (
            "Browse memories in a scope.  Returns metadata for each entry "
            "(title/heading, topic, tags, retrieval_count, source, updated_at) "
            "without performing vector search.  Use for discovery — 'what "
            "memories exist about this project?' — before deciding whether "
            "to search for specific content.  Supports filtering by topic, "
            "tag, and entry type.  Results sorted newest-first with pagination."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID whose collection to browse.",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Memory scope to browse.  One of 'system', 'orchestrator', "
                        "'agenttype_{type}', or 'project_{id}'.  Defaults to the "
                        "project scope derived from project_id."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Filter by topic (e.g. 'authentication', 'testing').  "
                        "Only entries with this exact topic are returned."
                    ),
                },
                "tag": {
                    "type": "string",
                    "description": (
                        "Filter by tag.  Returns entries whose tags array contains this value."
                    ),
                },
                "entry_type": {
                    "type": "string",
                    "enum": ["document", "kv", "temporal", ""],
                    "description": (
                        "Filter by entry type.  Defaults to 'document' (semantic "
                        "memories/insights).  Use '' to list all entry types."
                    ),
                    "default": "document",
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of entries to skip for pagination (default 0).",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": ("Maximum entries to return (default 50, max 200)."),
                    "default": 50,
                },
            },
            "required": ["project_id"],
        },
    },
    # ---- Index Management ----
    {
        "name": "memory_reindex",
        "description": (
            "Reindex the vault filesystem into Milvus.  Scans vault markdown "
            "files, re-embeds changed content, updates vector and scalar "
            "entries, and syncs fact files to KV entries.  Use after bulk "
            "vault edits or when the index seems stale."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to reindex.",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Scope to reindex.  Defaults to project scope.  "
                        "Use 'system' or 'orchestrator' for global reindex."
                    ),
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "If true, drop and rebuild the collection from scratch.  "
                        "Default is incremental (only changed files)."
                    ),
                    "default": False,
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "memory_stats",
        "description": (
            "Get statistics for a scoped Milvus collection.  Shows entry "
            "counts by type (document, kv, temporal), collection name, "
            "embedding model, storage size, and vault sync status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to get stats for.",
                },
                "scope": {
                    "type": "string",
                    "description": ("Scope to inspect.  Defaults to project scope."),
                },
            },
            "required": ["project_id"],
        },
    },
    # ---- Profile / Factsheet / Knowledge (carried forward from v1) ----
    {
        "name": "view_profile",
        "description": (
            "View the project profile — a synthesized understanding of the "
            "project's architecture, conventions, key decisions, and patterns.  "
            "Stored as a document entry in the project's Milvus collection "
            "and as a vault markdown file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to view profile for.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "edit_project_profile",
        "description": (
            "Replace the project memory profile with new content.  Updates "
            "both the vault file and the Milvus document entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to edit profile for.",
                },
                "content": {
                    "type": "string",
                    "description": "New profile content (markdown).",
                },
            },
            "required": ["project_id", "content"],
        },
    },
    {
        "name": "project_factsheet",
        "description": (
            "View or update the project factsheet — structured YAML metadata "
            "(URLs, tech stack, contacts, environments, key paths) plus a "
            "short markdown summary.  Synced as KV entries in the project "
            "collection for fast lookup."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to view/update factsheet for.",
                },
                "action": {
                    "type": "string",
                    "enum": ["view", "update"],
                    "description": "'view' or 'update' (default: 'view').",
                    "default": "view",
                },
                "updates": {
                    "type": "object",
                    "description": (
                        "For action='update': dict of dot-notation field paths to new values."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "content": {
                    "type": "string",
                    "description": (
                        "For action='update': full replacement content for the "
                        "factsheet (YAML frontmatter + markdown body)."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "project_knowledge",
        "description": (
            "Read organized knowledge about a specific topic for a project.  "
            "Topics (architecture, conventions, decisions, etc.) are stored "
            "as document entries with topic tags in the project collection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to read knowledge for.",
                },
                "action": {
                    "type": "string",
                    "enum": ["read", "list"],
                    "description": "'read' a topic or 'list' available topics.",
                    "default": "read",
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Topic to read (required for action='read').  E.g. "
                        "'architecture', 'conventions', 'decisions'."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    # ---- Compaction / Consolidation ----
    {
        "name": "compact_memory",
        "description": (
            "Trigger memory compaction.  Summarizes older document entries "
            "into digests, removes stale entries, and defragments the "
            "Milvus collection.  Returns stats on entries processed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to compact memory for.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "consolidate",
        "description": (
            "Run knowledge consolidation.  'daily' processes staged facts, "
            "'deep' prunes and resolves the full knowledge base, 'bootstrap' "
            "generates initial knowledge from task history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to run consolidation for.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["daily", "deep", "bootstrap"],
                    "description": "Consolidation mode (default: 'daily').",
                    "default": "daily",
                },
            },
            "required": ["project_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class MemoryV2Plugin(InternalPlugin):
    """Memory v2: unified memory via memsearch/Milvus with scoped collections.

    Registered alongside v1 MemoryPlugin during transition (§4 of the spec).
    Only the new v2-specific tool names are registered here; overlapping
    tool names remain owned by v1.  See ``docs/specs/design/memory-plugin.md``.

    The plugin delegates all operations to :class:`MemoryV2Service` which
    wraps the memsearch fork's :class:`CollectionRouter` and
    :class:`MilvusStore`.
    """

    # Auto-discovered and loaded alongside v1 MemoryPlugin.  Both plugins
    # are active: v1 owns existing tool names, v2 owns new ones.
    _internal: bool = True

    @property
    def service(self) -> MemoryV2Service | None:
        """The :class:`MemoryV2Service` backend, or ``None`` if unavailable.

        Exposed so the orchestrator can wire the service to subsystems
        that need it after plugin initialization (e.g. facts.md KV sync).
        """
        return self._service

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._log = ctx.logger

        # Initialize the MemoryV2Service backend.
        self._service: MemoryV2Service | None = None
        await self._init_service(ctx)

        # -- Map command names to handlers --
        # Full command table — includes both v2-only and overlapping names.
        # During transition only V2_ONLY_TOOLS are registered; overlapping
        # names stay with v1 MemoryPlugin.
        all_commands: dict[str, object] = {
            # Save (spec §8)
            "memory_save": self.cmd_memory_save,
            # Semantic search
            "memory_search": self.cmd_memory_search,
            "memory_search_by_tag": self.cmd_memory_search_by_tag,
            # Browse / list
            "memory_list": self.cmd_memory_list,
            # KV operations
            "memory_kv_get": self.cmd_memory_kv_get,
            "memory_kv_set": self.cmd_memory_kv_set,
            "memory_kv_list": self.cmd_memory_kv_list,
            # Scoped recall (spec §6–§7)
            "memory_fact_recall": self.cmd_memory_fact_recall,
            "memory_recall": self.cmd_memory_recall,
            # Unified auto-routing (spec §7)
            "memory_get": self.cmd_memory_get,
            # Temporal facts
            "memory_fact_get": self.cmd_memory_fact_get,
            "memory_fact_set": self.cmd_memory_fact_set,
            "memory_fact_list": self.cmd_memory_fact_list,
            "memory_fact_history": self.cmd_memory_fact_history,
            # Index management
            "memory_reindex": self.cmd_memory_reindex,
            "memory_stats": self.cmd_memory_stats,
            # Profile / knowledge
            "view_profile": self.cmd_view_profile,
            "edit_project_profile": self.cmd_edit_project_profile,
            "project_factsheet": self.cmd_project_factsheet,
            "project_knowledge": self.cmd_project_knowledge,
            # Compaction
            "compact_memory": self.cmd_compact_memory,
            "consolidate": self.cmd_consolidate,
        }

        # -- Register only v2-only commands during transition --
        registered = 0
        for name, handler in all_commands.items():
            if name in V2_ONLY_TOOLS:
                ctx.register_command(name, handler)
                registered += 1

        # -- Register only v2-only tool schemas during transition --
        for tool_def in TOOL_DEFINITIONS:
            if tool_def["name"] in V2_ONLY_TOOLS:
                ctx.register_tool(dict(tool_def), category=TOOL_CATEGORY)

        status = "connected" if self._service and self._service.available else "degraded"
        self._log.info(
            "MemoryV2Plugin initialized (%s, %d/%d v2-only commands registered)",
            status,
            registered,
            len(all_commands),
        )

    async def _init_service(self, ctx: PluginContext) -> None:
        """Initialize the MemoryV2Service backend from config.

        Reads Milvus/embedding settings from the ``config`` service and
        creates the service.  If memsearch is not installed or config is
        unavailable, the plugin operates in degraded mode (all commands
        return graceful error responses).
        """
        try:
            from src.memory_v2_service import MemoryV2Service

            # Get config values from the config service
            config_svc = ctx.get_service("config")
            data_dir = config_svc.data_dir if config_svc else ""

            # Access the raw AppConfig for memory settings via the
            # config service's internal reference.
            memory_cfg = self._get_memory_config(config_svc)

            self._service = MemoryV2Service(
                milvus_uri=memory_cfg.get("milvus_uri", "~/.agent-queue/memsearch/milvus.db"),
                milvus_token=memory_cfg.get("milvus_token", ""),
                embedding_provider=memory_cfg.get("embedding_provider", "openai"),
                embedding_model=memory_cfg.get("embedding_model", ""),
                embedding_base_url=memory_cfg.get("embedding_base_url", ""),
                embedding_api_key=memory_cfg.get("embedding_api_key", ""),
                data_dir=data_dir,
            )
            await self._service.initialize()

            if self._service.available:
                self._log.info("MemoryV2Service backend connected")
            else:
                self._log.warning(
                    "MemoryV2Service initialized but not available (memsearch may not be installed)"
                )
        except Exception:
            self._log.warning(
                "Failed to initialize MemoryV2Service — operating in degraded mode",
                exc_info=True,
            )
            self._service = None

    def _get_memory_config(self, config_svc: Any) -> dict[str, Any]:
        """Extract memory configuration as a dict.

        Tries to access the ``AppConfig.memory`` attribute through the
        config service.  Falls back to safe defaults if unavailable.
        """
        try:
            app_config = getattr(config_svc, "_config", None)
            if app_config and hasattr(app_config, "memory"):
                mem = app_config.memory
                return {
                    "milvus_uri": getattr(mem, "milvus_uri", ""),
                    "milvus_token": getattr(mem, "milvus_token", ""),
                    "embedding_provider": getattr(mem, "embedding_provider", "openai"),
                    "embedding_model": getattr(mem, "embedding_model", ""),
                    "embedding_base_url": getattr(mem, "embedding_base_url", ""),
                    "embedding_api_key": getattr(mem, "embedding_api_key", ""),
                }
        except Exception:
            pass
        return {}

    async def shutdown(self, ctx: PluginContext) -> None:
        if self._service:
            await self._service.shutdown()
            self._service = None

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _not_implemented(self, command: str) -> dict:
        """Return a standard 'not yet implemented' response.

        Used for overlapping commands that are still owned by v1.
        """
        return {
            "error": (
                f"{command} is not yet implemented in memory v2 (owned by v1 during transition)"
            ),
            "plugin": "memory_v2",
        }

    def _unavailable(self, command: str) -> dict:
        """Return a response for when the service is not available."""
        return {
            "error": (
                f"{command}: MemoryV2Service is not available. "
                "Ensure memsearch is installed and memory is enabled "
                "in config."
            ),
            "plugin": "memory_v2",
        }

    def _format_kv_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Format a KV entry for API response."""
        return {
            "namespace": entry.get("kv_namespace", ""),
            "key": entry.get("kv_key", ""),
            "value": self._decode_kv_value(entry.get("kv_value", "")),
            "updated_at": entry.get("updated_at", 0),
            "tags": self._decode_tags(entry.get("tags", "[]")),
            "source": entry.get("source", ""),
        }

    def _format_temporal_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Format a temporal entry for API response."""
        return {
            "key": entry.get("kv_key", ""),
            "value": self._decode_kv_value(entry.get("kv_value", "")),
            "valid_from": entry.get("valid_from", 0),
            "valid_to": entry.get("valid_to", 0),
            "updated_at": entry.get("updated_at", 0),
            "tags": self._decode_tags(entry.get("tags", "[]")),
            "source": entry.get("source", ""),
        }

    @staticmethod
    def _decode_kv_value(raw: str) -> Any:
        """Decode a JSON-encoded KV value, returning the raw string on failure."""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    @staticmethod
    def _decode_tags(raw: str) -> list[str]:
        """Decode a JSON-encoded tags array."""
        try:
            tags = json.loads(raw)
            return tags if isinstance(tags, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    # -----------------------------------------------------------------
    # Topic auto-detection (spec §3)
    # -----------------------------------------------------------------

    # Controlled vocabulary for consistent topic assignment.
    # The LLM chooses from this list when possible; it may suggest a new
    # single-word topic if none fit.  Keyword fallback also uses this list.
    CONTROLLED_TOPICS: list[str] = [
        "architecture",
        "authentication",
        "build",
        "ci-cd",
        "cli",
        "configuration",
        "database",
        "debugging",
        "dependencies",
        "deployment",
        "documentation",
        "error-handling",
        "git",
        "hooks",
        "logging",
        "memory",
        "messaging",
        "monitoring",
        "networking",
        "performance",
        "plugins",
        "refactoring",
        "scheduling",
        "security",
        "tasks",
        "testing",
        "ui",
        "workflow",
    ]

    # Keyword → topic mapping for the fallback classifier.
    # Each keyword is checked as a case-insensitive substring of the content.
    _KEYWORD_TOPIC_MAP: dict[str, str] = {
        # architecture
        "architecture": "architecture",
        "design pattern": "architecture",
        "module structure": "architecture",
        "component": "architecture",
        # authentication
        "auth": "authentication",
        "oauth": "authentication",
        "login": "authentication",
        "token refresh": "authentication",
        "credential": "authentication",
        "jwt": "authentication",
        "saml": "authentication",
        "sso": "authentication",
        # build
        "build": "build",
        "compile": "build",
        "setuptools": "build",
        "pyproject": "build",
        "webpack": "build",
        # ci-cd
        "ci/cd": "ci-cd",
        "pipeline": "ci-cd",
        "github actions": "ci-cd",
        "ci ": "ci-cd",
        "continuous integration": "ci-cd",
        "continuous delivery": "ci-cd",
        # cli
        "command line": "cli",
        "argparse": "cli",
        "click": "cli",
        "cli ": "cli",
        # configuration
        "config": "configuration",
        "yaml": "configuration",
        "env var": "configuration",
        "settings": "configuration",
        "environment variable": "configuration",
        # database
        "database": "database",
        "sql": "database",
        "sqlite": "database",
        "postgres": "database",
        "migration": "database",
        "alembic": "database",
        "milvus": "database",
        "query": "database",
        "schema": "database",
        # debugging
        "debug": "debugging",
        "breakpoint": "debugging",
        "traceback": "debugging",
        "stack trace": "debugging",
        # deployment
        "deploy": "deployment",
        "docker": "deployment",
        "container": "deployment",
        "kubernetes": "deployment",
        "k8s": "deployment",
        # dependencies
        "dependency": "dependencies",
        "dependencies": "dependencies",
        "package": "dependencies",
        "pip install": "dependencies",
        "requirements": "dependencies",
        # documentation
        "documentation": "documentation",
        "docstring": "documentation",
        "readme": "documentation",
        "mkdocs": "documentation",
        "sphinx": "documentation",
        # error-handling
        "error handling": "error-handling",
        "exception": "error-handling",
        "try/except": "error-handling",
        "raise ": "error-handling",
        # git
        "git ": "git",
        "branch": "git",
        "merge conflict": "git",
        "commit": "git",
        "rebase": "git",
        "pull request": "git",
        # hooks
        "hook": "hooks",
        "pre-commit": "hooks",
        "post-commit": "hooks",
        "webhook": "hooks",
        # logging
        "logging": "logging",
        "logger": "logging",
        "structlog": "logging",
        "log level": "logging",
        # memory
        "memory": "memory",
        "vault": "memory",
        "embedding": "memory",
        "vector search": "memory",
        "memsearch": "memory",
        "semantic search": "memory",
        # messaging
        "discord": "messaging",
        "telegram": "messaging",
        "slack": "messaging",
        "notification": "messaging",
        "message": "messaging",
        # monitoring
        "monitoring": "monitoring",
        "metrics": "monitoring",
        "health check": "monitoring",
        "observability": "monitoring",
        # networking
        "network": "networking",
        "http": "networking",
        "api endpoint": "networking",
        "rest api": "networking",
        "websocket": "networking",
        # performance
        "performance": "performance",
        "optimization": "performance",
        "latency": "performance",
        "throughput": "performance",
        "cache": "performance",
        "slow": "performance",
        # plugins
        "plugin": "plugins",
        "extension": "plugins",
        "addon": "plugins",
        # refactoring
        "refactor": "refactoring",
        "clean up": "refactoring",
        "rename": "refactoring",
        "extract": "refactoring",
        # scheduling
        "schedule": "scheduling",
        "cron": "scheduling",
        "timer": "scheduling",
        "rate limit": "scheduling",
        "throttle": "scheduling",
        "queue": "scheduling",
        # security
        "security": "security",
        "vulnerability": "security",
        "cve": "security",
        "encryption": "security",
        "secret": "security",
        "permission": "security",
        # tasks
        "task ": "tasks",
        "orchestrat": "tasks",
        "supervisor": "tasks",
        "agent ": "tasks",
        "work queue": "tasks",
        # testing
        "test": "testing",
        "pytest": "testing",
        "fixture": "testing",
        "mock": "testing",
        "assertion": "testing",
        "coverage": "testing",
        # ui
        "ui ": "ui",
        "frontend": "ui",
        "css": "ui",
        "html": "ui",
        "react": "ui",
        "template": "ui",
        # workflow
        "workflow": "workflow",
        "playbook": "workflow",
        "process": "workflow",
        "automation": "workflow",
    }

    async def _infer_topic(
        self,
        content: str,
        *,
        source_task: str | None = None,
        tags: list[str] | None = None,
    ) -> str | None:
        """Infer the topic from content and task context.

        Uses an LLM call (Haiku) to classify the content into one of the
        :attr:`CONTROLLED_TOPICS`.  Falls back to keyword-based matching
        if the LLM is unavailable.

        Parameters
        ----------
        content:
            The memory content to classify.
        source_task:
            Optional source task ID for additional context.
        tags:
            Optional tags that may hint at the topic.

        Returns
        -------
        str or None
            The inferred topic, or ``None`` if no confident match.
        """
        # Build context snippet from tags and source task
        context_parts: list[str] = []
        if tags:
            context_parts.append(f"Tags: {', '.join(tags)}")
        if source_task:
            context_parts.append(f"Source task: {source_task}")
        context_str = "\n".join(context_parts)

        # Try LLM classification first
        topic = await self._infer_topic_via_llm(content, context_str)
        if topic:
            return topic

        # Fallback to keyword matching
        return self._infer_topic_via_keywords(content)

    async def _infer_topic_via_llm(
        self,
        content: str,
        context: str,
    ) -> str | None:
        """Classify content into a controlled topic via LLM.

        Returns the topic string or ``None`` if classification fails.
        """
        topics_list = ", ".join(self.CONTROLLED_TOPICS)
        prompt = (
            "You are a topic classifier for a developer knowledge base.\n\n"
            "CONTENT:\n"
            f"{content[:1000]}\n\n"
        )
        if context:
            prompt += f"CONTEXT:\n{context}\n\n"
        prompt += (
            f"CONTROLLED TOPICS: {topics_list}\n\n"
            "INSTRUCTIONS:\n"
            "- Choose the single best topic from the CONTROLLED TOPICS list.\n"
            "- If none fit well, output a new single lowercase hyphenated topic "
            "(e.g. 'data-migration', 'api-design').\n"
            "- Output ONLY the topic string, nothing else. No quotes, no explanation.\n"
        )
        try:
            raw = await self._ctx.invoke_llm(
                prompt,
                model="claude-haiku-4-20250514",
            )
            topic = raw.strip().lower().strip("\"'")
            # Normalize: replace spaces/underscores with hyphens, remove
            # non-alphanumeric chars except hyphens
            topic = topic.replace(" ", "-").replace("_", "-")
            topic = re.sub(r"[^a-z0-9-]", "", topic)
            topic = re.sub(r"-{2,}", "-", topic).strip("-")
            if not topic:
                return None
            return topic
        except Exception:
            self._log.debug("LLM topic inference unavailable, using keyword fallback")
            return None

    def _infer_topic_via_keywords(self, content: str) -> str | None:
        """Classify content into a topic using keyword matching.

        Scans the content for keywords from :attr:`_KEYWORD_TOPIC_MAP`
        and returns the topic with the most keyword hits.

        Returns ``None`` if no keywords match.
        """
        content_lower = content.lower()
        # Count hits per topic
        topic_scores: dict[str, int] = {}
        for keyword, topic in self._KEYWORD_TOPIC_MAP.items():
            if keyword in content_lower:
                topic_scores[topic] = topic_scores.get(topic, 0) + 1

        if not topic_scores:
            return None

        # Return topic with highest score
        return max(topic_scores, key=topic_scores.get)  # type: ignore[arg-type]

    # -----------------------------------------------------------------
    # Command handlers — Save (spec §8)
    # -----------------------------------------------------------------

    # Similarity thresholds for dedup (per spec §8)
    _DEDUP_NEAR_IDENTICAL: float = 0.95
    _DEDUP_RELATED: float = 0.80
    # Approximate token threshold for summary generation (§9)
    _SUMMARY_CHAR_THRESHOLD: int = 800  # ~200 tokens ≈ 800 chars
    # Minimum word count for dedup check — similarity is unreliable on
    # very short content so we skip the dedup search entirely.
    _DEDUP_MIN_WORDS: int = 5

    async def cmd_memory_save(self, args: dict) -> dict:
        """Save an insight with dedup check, summary/original, topic assignment.

        Implements the full ``memory_save`` flow from spec §8:

        1. Search for semantic duplicates in the target scope.
        2. Based on top similarity score:
           - **> 0.95** — near-identical → update timestamp, append source task.
           - **0.8–0.95** — related → merge via LLM, update content + embedding.
           - **< 0.8** — distinct → create new vault file + Milvus entry.
        3. If content is long (> ~200 tokens), generate a summary via LLM (§9).
        4. Write/update vault markdown file with frontmatter.
        5. Index into the scoped Milvus collection.
        """
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        content = args.get("content")
        if not content:
            return {"error": "content is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_save")

        tags = args.get("tags") or ["insight", "auto-generated"]
        topic = args.get("topic")
        source_task = args.get("source_task")
        scope = args.get("scope")

        try:
            return await self._do_memory_save(
                project_id=project_id,
                content=content,
                tags=tags,
                topic=topic,
                source_task=source_task,
                scope=scope,
            )
        except Exception as e:
            self._log.error("memory_save failed: %s", e, exc_info=True)
            return {"error": f"Save failed: {e}"}

    async def _do_memory_save(
        self,
        *,
        project_id: str,
        content: str,
        tags: list[str],
        topic: str | None,
        source_task: str | None,
        scope: str | None,
    ) -> dict:
        """Core orchestration for memory_save.

        Separated from ``cmd_memory_save`` for testability and clarity.
        """
        assert self._service is not None  # guarded by caller

        # ----- Step 0: Infer topic if not provided (spec §3) -----
        topic_auto_detected = False
        if topic is None:
            inferred = await self._infer_topic(
                content,
                source_task=source_task,
                tags=tags,
            )
            if inferred:
                topic = inferred
                topic_auto_detected = True
                self._log.info("Auto-detected topic %r for memory_save", topic)

        # ----- Step 1: Dedup check via semantic search -----

        # Skip dedup for very short content — similarity scores are
        # unreliable for content shorter than _DEDUP_MIN_WORDS words.
        word_count = len(content.split())
        skip_dedup = word_count < self._DEDUP_MIN_WORDS

        best_match: dict | None = None
        best_score: float = 0.0

        if not skip_dedup:
            # Resolve target scope so the dedup search is scoped to the same
            # collection we'll save into.  When *scope* is None the target is
            # the project scope — we must NOT use multi-scope search (which
            # would also query the system collection) for dedup.
            dedup_scope = scope if scope is not None else f"project_{project_id}"

            dedup_results = await self._service.search(
                project_id,
                content[:500],  # search using first ~500 chars for efficiency
                scope=dedup_scope,
                topic=topic,
                top_k=5,
            )

            # Find the best match
            for result in dedup_results:
                score = result.get("score", 0.0)
                # Only consider document entries (not KV/temporal)
                if result.get("entry_type", "document") == "document" and score > best_score:
                    best_score = score
                    best_match = result

        # ----- Step 2: Apply dedup logic -----

        if best_match and best_score > self._DEDUP_NEAR_IDENTICAL:
            # Near-identical: just update timestamp
            result = await self._handle_dedup_identical(
                project_id=project_id,
                existing=best_match,
                similarity=best_score,
                source_task=source_task,
                scope=scope,
            )
        elif best_match and best_score >= self._DEDUP_RELATED:
            # Related: merge via LLM
            result = await self._handle_dedup_merge(
                project_id=project_id,
                content=content,
                existing=best_match,
                similarity=best_score,
                tags=tags,
                topic=topic,
                source_task=source_task,
                scope=scope,
            )
        else:
            # ----- Step 3: Distinct — create new entry -----
            result = await self._handle_create_new(
                project_id=project_id,
                content=content,
                tags=tags,
                topic=topic,
                source_task=source_task,
                scope=scope,
            )

        # Annotate with auto-detection info
        if topic_auto_detected:
            result["topic_auto_detected"] = True
        return result

    async def _handle_dedup_identical(
        self,
        *,
        project_id: str,
        existing: dict,
        similarity: float,
        source_task: str | None,
        scope: str | None,
    ) -> dict:
        """Handle near-identical dedup (similarity > 0.95).

        Updates timestamp on existing memory and appends source task.
        """
        chunk_hash = existing.get("chunk_hash", "")
        if not chunk_hash:
            self._log.warning("Dedup match has no chunk_hash, falling back to create")
            return {"success": False, "error": "Dedup match missing chunk_hash"}

        result = await self._service.update_document_timestamp(
            project_id,
            chunk_hash,
            source_task=source_task,
            scope=scope,
        )
        return {
            "success": True,
            "action": "deduplicated",
            "project_id": project_id,
            "similarity_score": round(similarity, 4),
            "existing_chunk_hash": chunk_hash,
            **result,
        }

    async def _handle_dedup_merge(
        self,
        *,
        project_id: str,
        content: str,
        existing: dict,
        similarity: float,
        tags: list[str],
        topic: str | None,
        source_task: str | None,
        scope: str | None,
    ) -> dict:
        """Handle related dedup (similarity 0.8–0.95) — merge via LLM.

        Invokes the LLM to combine old + new content, then updates the
        existing entry with the merged result.  If the merged content
        exceeds ~200 tokens, a summary is generated per spec §9 —
        the summary is embedded/indexed and the full merged content
        is preserved as ``original``.
        """
        chunk_hash = existing.get("chunk_hash", "")
        old_content = existing.get("content", "")
        old_tags = self._decode_tags(existing.get("tags", "[]"))

        # Merge tags (preserve both, deduplicate)
        merged_tags = list(dict.fromkeys(old_tags + tags))

        # Attempt LLM merge
        merged_content = await self._merge_via_llm(old_content, content)

        if not chunk_hash:
            self._log.warning("Merge match has no chunk_hash, creating new entry instead")
            # Fall through to creating new with merged content
            return await self._handle_create_new(
                project_id=project_id,
                content=merged_content,
                tags=merged_tags,
                topic=topic,
                source_task=source_task,
                scope=scope,
            )

        # Generate summary for long merged content (spec §9)
        summary: str | None = None
        original: str | None = None
        if len(merged_content) > self._SUMMARY_CHAR_THRESHOLD:
            summary = await self._generate_summary(merged_content)
            original = merged_content

        result = await self._service.update_document_content(
            project_id,
            chunk_hash,
            summary or merged_content,
            original=original,
            tags=merged_tags,
            scope=scope,
        )
        return {
            "success": True,
            "action": "merged",
            "project_id": project_id,
            "similarity_score": round(similarity, 4),
            "merged_with": chunk_hash,
            "has_summary": summary is not None,
            **result,
        }

    async def _handle_create_new(
        self,
        *,
        project_id: str,
        content: str,
        tags: list[str],
        topic: str | None,
        source_task: str | None,
        scope: str | None,
    ) -> dict:
        """Create a new memory entry (distinct content).

        If content is long, generates a summary via LLM (spec §9).
        """
        summary: str | None = None
        original: str | None = None

        # Generate summary for long content (§9)
        if len(content) > self._SUMMARY_CHAR_THRESHOLD:
            summary = await self._generate_summary(content)
            original = content
        # else: content is already summary-length, used as-is

        result = await self._service.save_document(
            project_id,
            content,
            summary=summary,
            original=original,
            tags=tags,
            topic=topic,
            source_task=source_task,
            scope=scope,
        )
        return {
            "success": True,
            "action": "created",
            "project_id": project_id,
            "has_summary": summary is not None,
            **result,
        }

    async def _merge_via_llm(self, old_content: str, new_content: str) -> str:
        """Merge two related memories via a lightweight LLM call.

        Per spec §8: "Combine these into a single memory.  If they
        contradict, prefer the newer information but note the change.
        Preserve tags from both."

        Falls back to simple concatenation if LLM is unavailable.
        """
        prompt = (
            "You are merging two related memory entries into one concise, unified memory.\n\n"
            "EXISTING MEMORY:\n"
            f"{old_content}\n\n"
            "NEW MEMORY:\n"
            f"{new_content}\n\n"
            "INSTRUCTIONS:\n"
            "- Combine both into a single, cohesive memory entry.\n"
            "- If they contradict, prefer the newer information but briefly note what changed.\n"
            "- Keep the result concise — no longer than the longer of the two inputs.\n"
            "- Output ONLY the merged memory content, no preamble or explanation.\n"
        )
        try:
            return await self._ctx.invoke_llm(
                prompt,
                model="claude-haiku-4-20250514",
            )
        except Exception:
            self._log.warning("LLM merge unavailable, using concatenation fallback")
            return f"{old_content}\n\n---\n\n**Updated:** {new_content}"

    async def _generate_summary(self, content: str) -> str:
        """Generate a concise summary of long content via LLM.

        Per spec §9: summary is embedded/indexed for search; original
        is preserved for full context retrieval.

        Falls back to truncation if LLM is unavailable.
        """
        prompt = (
            "Summarize the following insight or learning into a concise memory entry "
            "optimized for semantic search retrieval. Keep the key facts, decisions, "
            "and actionable knowledge. Aim for 2-4 sentences.\n\n"
            "CONTENT:\n"
            f"{content}\n\n"
            "OUTPUT ONLY the summary, no preamble.\n"
        )
        try:
            return await self._ctx.invoke_llm(
                prompt,
                model="claude-haiku-4-20250514",
            )
        except Exception:
            self._log.warning("LLM summary unavailable, using truncation fallback")
            # Truncate to ~200 tokens worth
            lines = content.split("\n")
            truncated = []
            char_count = 0
            for line in lines:
                if char_count + len(line) > self._SUMMARY_CHAR_THRESHOLD:
                    break
                truncated.append(line)
                char_count += len(line)
            return "\n".join(truncated)

    # -----------------------------------------------------------------
    # Command handlers — Semantic Search
    # -----------------------------------------------------------------

    async def cmd_memory_search(self, args: dict) -> dict:
        """Semantic vector search across a scoped collection."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        query = args.get("query")
        queries = args.get("queries")
        if not query and not queries:
            return {"error": "Either 'query' or 'queries' is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_search")

        scope = args.get("scope")
        topic = args.get("topic")
        top_k = args.get("top_k", 10)

        try:
            if queries:
                # Batch search
                results = await self._service.batch_search(
                    project_id, queries, scope=scope, topic=topic, top_k=top_k
                )
                return {
                    "success": True,
                    "project_id": project_id,
                    "batch": True,
                    "results": {
                        q: [self._format_search_result(r) for r in hits]
                        for q, hits in results.items()
                    },
                }
            else:
                results = await self._service.search(
                    project_id, query, scope=scope, topic=topic, top_k=top_k
                )
                return {
                    "success": True,
                    "project_id": project_id,
                    "query": query,
                    "count": len(results),
                    "results": [self._format_search_result(r) for r in results],
                }
        except Exception as e:
            self._log.error("memory_search failed: %s", e, exc_info=True)
            return {"error": f"Search failed: {e}"}

    def _format_search_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Format a search result for API response."""
        return {
            "content": result.get("content", ""),
            "source": result.get("source", ""),
            "heading": result.get("heading", ""),
            "score": result.get("score", 0.0),
            "weighted_score": result.get("weighted_score", 0.0),
            "entry_type": result.get("entry_type", "document"),
            "topic": result.get("topic", ""),
            "tags": self._decode_tags(result.get("tags", "[]")),
            "chunk_hash": result.get("chunk_hash", ""),
            "scope": result.get("_scope", ""),
            "scope_id": result.get("_scope_id"),
            "collection": result.get("_collection", ""),
        }

    async def cmd_memory_search_by_tag(self, args: dict) -> dict:
        """Cross-scope search by tag across all collections."""
        tag = args.get("tag")
        if not tag:
            return {"error": "tag is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_search_by_tag")

        entry_type = args.get("entry_type")
        topic = args.get("topic")
        limit = args.get("limit", 10)

        try:
            results = await self._service.search_by_tag(
                tag,
                entry_type=entry_type,
                topic=topic,
                limit=limit,
            )
            return {
                "success": True,
                "tag": tag,
                "count": len(results),
                "results": [
                    {
                        "content": r.get("content", ""),
                        "source": r.get("source", ""),
                        "entry_type": r.get("entry_type", "document"),
                        "tags": self._decode_tags(r.get("tags", "[]")),
                        "scope": r.get("_scope", ""),
                        "scope_id": r.get("_scope_id"),
                        "collection": r.get("_collection", ""),
                        "chunk_hash": r.get("chunk_hash", ""),
                    }
                    for r in results
                ],
            }
        except Exception as e:
            self._log.error("memory_search_by_tag failed: %s", e, exc_info=True)
            return {"error": f"Tag search failed: {e}"}

    # -----------------------------------------------------------------
    # Command handlers — Browse / List
    # -----------------------------------------------------------------

    async def cmd_memory_list(self, args: dict) -> dict:
        """Browse memories in a scope, returning metadata."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_list")

        scope = args.get("scope")
        topic = args.get("topic")
        tag = args.get("tag")
        entry_type = args.get("entry_type", "document")
        offset = args.get("offset", 0)
        limit = args.get("limit", 50)

        try:
            entries = await self._service.list_memories(
                project_id,
                scope=scope,
                topic=topic,
                tag=tag,
                entry_type=entry_type,
                offset=offset,
                limit=limit,
            )
            return {
                "success": True,
                "project_id": project_id,
                "scope": scope or f"project_{project_id}",
                "count": len(entries),
                "offset": offset,
                "limit": limit,
                "filters": {
                    k: v
                    for k, v in {
                        "topic": topic,
                        "tag": tag,
                        "entry_type": entry_type,
                    }.items()
                    if v
                },
                "entries": [self._format_list_entry(e) for e in entries],
            }
        except Exception as e:
            self._log.error("memory_list failed: %s", e, exc_info=True)
            return {"error": f"Memory list failed: {e}"}

    def _format_list_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Format a memory entry for the list/browse response.

        Returns metadata fields without the full content — just a
        truncated preview for discovery purposes.
        """
        content = entry.get("content", "")
        # Truncate content to a preview (first 200 chars)
        preview = content[:200] + "…" if len(content) > 200 else content

        return {
            "chunk_hash": entry.get("chunk_hash", ""),
            "title": entry.get("heading", "") or self._extract_title(content),
            "topic": entry.get("topic", ""),
            "tags": self._decode_tags(entry.get("tags", "[]")),
            "source": entry.get("source", ""),
            "entry_type": entry.get("entry_type", "document"),
            "retrieval_count": entry.get("retrieval_count", 0),
            "updated_at": entry.get("updated_at", 0),
            "content_preview": preview,
        }

    @staticmethod
    def _extract_title(content: str) -> str:
        """Extract a title from the first line of content.

        Falls back to the first ~80 characters if no markdown heading
        is found.
        """
        if not content:
            return ""
        first_line = content.split("\n", 1)[0].strip()
        # Strip leading markdown heading markers
        if first_line.startswith("#"):
            first_line = first_line.lstrip("#").strip()
        return first_line[:80] if len(first_line) > 80 else first_line

    # -----------------------------------------------------------------
    # Command handlers — KV Operations
    # -----------------------------------------------------------------

    async def cmd_memory_kv_get(self, args: dict) -> dict:
        """Exact key-value lookup via Milvus scalar query."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        namespace = args.get("namespace")
        if not namespace:
            return {"error": "namespace is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_kv_get")

        try:
            entry = await self._service.kv_get(project_id, namespace, key)
            if entry is None:
                return {
                    "success": True,
                    "found": False,
                    "project_id": project_id,
                    "namespace": namespace,
                    "key": key,
                }
            return {
                "success": True,
                "found": True,
                "project_id": project_id,
                **self._format_kv_entry(entry),
            }
        except Exception as e:
            self._log.error("memory_kv_get failed: %s", e, exc_info=True)
            return {"error": f"KV get failed: {e}"}

    async def cmd_memory_kv_set(self, args: dict) -> dict:
        """Write a KV entry to the scoped collection and vault facts file."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        namespace = args.get("namespace")
        if not namespace:
            return {"error": "namespace is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}
        value = args.get("value")
        if value is None:
            return {"error": "value is required"}

        scope = args.get("scope")  # optional explicit scope override

        if not self._service or not self._service.available:
            return self._unavailable("memory_kv_set")

        try:
            entry = await self._service.kv_set(project_id, namespace, key, value, scope=scope)
            result: dict[str, Any] = {
                "success": True,
                "project_id": project_id,
                **self._format_kv_entry(entry),
            }
            # Include vault sync info in response
            if "_vault_path" in entry:
                result["vault_path"] = entry["_vault_path"]
            if "_scope" in entry:
                result["scope"] = entry["_scope"]
            if "_scope_id" in entry:
                result["scope_id"] = entry["_scope_id"]
            return result
        except Exception as e:
            self._log.error("memory_kv_set failed: %s", e, exc_info=True)
            return {"error": f"KV set failed: {e}"}

    async def cmd_memory_kv_list(self, args: dict) -> dict:
        """List all KV entries in a namespace."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        namespace = args.get("namespace")
        if not namespace:
            return {"error": "namespace is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_kv_list")

        try:
            entries = await self._service.kv_list(project_id, namespace)
            return {
                "success": True,
                "project_id": project_id,
                "namespace": namespace,
                "count": len(entries),
                "entries": [self._format_kv_entry(e) for e in entries],
            }
        except Exception as e:
            self._log.error("memory_kv_list failed: %s", e, exc_info=True)
            return {"error": f"KV list failed: {e}"}

    # -----------------------------------------------------------------
    # Command handlers — Scoped Recall (spec §6–§7)
    # -----------------------------------------------------------------

    async def cmd_memory_fact_recall(self, args: dict) -> dict:
        """KV lookup with scope resolution — first match wins.

        Searches scopes in order: project → agent-type → system.
        Per spec ``docs/specs/design/memory-scoping.md`` §6.
        """
        key = args.get("key")
        if not key:
            return {"error": "key is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_fact_recall")

        project_id = args.get("project_id")
        agent_type = args.get("agent_type")
        namespace = args.get("namespace")

        try:
            entry = await self._service.kv_recall(
                key,
                project_id=project_id,
                agent_type=agent_type,
                namespace=namespace,
            )
            if entry is None:
                return {
                    "success": True,
                    "found": False,
                    "key": key,
                    "project_id": project_id or "",
                    "agent_type": agent_type or "",
                    "namespace": namespace or "",
                    "scopes_searched": self._build_scope_list(project_id, agent_type),
                }
            formatted = self._format_kv_entry(entry)
            return {
                "success": True,
                "found": True,
                "key": key,
                "resolved_scope": entry.get("_scope", ""),
                "resolved_scope_id": entry.get("_scope_id", ""),
                "collection": entry.get("_collection", ""),
                **formatted,
            }
        except Exception as e:
            self._log.error("memory_fact_recall failed: %s", e, exc_info=True)
            return {"error": f"Fact recall failed: {e}"}

    async def cmd_memory_recall(self, args: dict) -> dict:
        """Smart retrieval: KV exact match first, then semantic search.

        Per spec ``docs/specs/design/memory-scoping.md`` §7.
        """
        query = args.get("query")
        if not query:
            return {"error": "query is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_recall")

        project_id = args.get("project_id")
        agent_type = args.get("agent_type")
        namespace = args.get("namespace")
        topic = args.get("topic")
        top_k = args.get("top_k", 5)

        try:
            result = await self._service.recall(
                query,
                project_id=project_id,
                agent_type=agent_type,
                namespace=namespace,
                topic=topic,
                top_k=top_k,
            )
            source = result.get("source", "unavailable")
            raw_results = result.get("results", [])

            if source == "kv":
                # Single KV match — format it
                formatted = [self._format_kv_entry(r) for r in raw_results]
                return {
                    "success": True,
                    "source": "kv",
                    "query": query,
                    "count": len(formatted),
                    "results": formatted,
                }
            elif source == "semantic":
                # Semantic search results
                formatted = self._format_search_results(raw_results)
                return {
                    "success": True,
                    "source": "semantic",
                    "query": query,
                    "count": len(formatted),
                    "results": formatted,
                }
            else:
                return {
                    "success": False,
                    "source": source,
                    "query": query,
                    "count": 0,
                    "results": [],
                }
        except Exception as e:
            self._log.error("memory_recall failed: %s", e, exc_info=True)
            return {"error": f"Recall failed: {e}"}

    async def cmd_memory_get(self, args: dict) -> dict:
        """Unified auto-routing retrieval: KV first, then semantic search.

        Per spec ``docs/specs/design/memory-scoping.md`` §7.  This is the
        default retrieval tool for agents — pass a query and let the system
        decide the best retrieval strategy.

        Delegates to :meth:`MemoryV2Service.recall` which tries KV exact
        match (scope-resolved) first and falls back to multi-scope semantic
        search.
        """
        query = args.get("query")
        if not query:
            return {"error": "query is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_get")

        project_id = args.get("project_id")
        agent_type = args.get("agent_type")
        topic = args.get("topic")
        top_k = args.get("top_k", 5)
        full = args.get("full", False)

        try:
            result = await self._service.recall(
                query,
                project_id=project_id,
                agent_type=agent_type,
                topic=topic,
                top_k=top_k,
                full=full,
            )
            source = result.get("source", "unavailable")
            raw_results = result.get("results", [])

            if source == "kv":
                formatted = [self._format_kv_entry(r) for r in raw_results]
                return {
                    "success": True,
                    "source": "kv",
                    "query": query,
                    "count": len(formatted),
                    "results": formatted,
                    "scopes_searched": self._build_scope_list(project_id, agent_type),
                }
            elif source == "semantic":
                formatted = self._format_search_results(raw_results, full=full)
                return {
                    "success": True,
                    "source": "semantic",
                    "query": query,
                    "count": len(formatted),
                    "results": formatted,
                }
            else:
                return {
                    "success": False,
                    "source": source,
                    "query": query,
                    "count": 0,
                    "results": [],
                    "message": (
                        "Memory service unavailable.  Ensure memsearch is "
                        "installed and memory is enabled in config."
                    ),
                }
        except Exception as e:
            self._log.error("memory_get failed: %s", e, exc_info=True)
            return {"error": f"memory_get failed: {e}"}

    @staticmethod
    def _build_scope_list(project_id: str | None, agent_type: str | None) -> list[str]:
        """Build the list of scopes that would be searched, for diagnostics."""
        scopes: list[str] = []
        if project_id:
            scopes.append(f"project_{project_id}")
        if agent_type:
            scopes.append(f"agenttype_{agent_type}")
        scopes.append("system")
        return scopes

    def _format_search_results(self, results: list[dict], *, full: bool = False) -> list[dict]:
        """Format semantic search results for API response.

        Parameters
        ----------
        results:
            Raw search results from memsearch.
        full:
            When ``True``, replace ``content`` (summary) with the
            ``original`` field when available.  Per spec §9: "Search
            returns summary; ``memory_get`` with ``full=true`` returns
            the original."
        """
        formatted: list[dict] = []
        for r in results:
            content = r.get("content", "")
            original = r.get("original", "")

            if full and original:
                # Return original content instead of summary
                display_content = original
            else:
                display_content = content

            entry: dict[str, Any] = {
                "content": display_content,
                "heading": r.get("heading", ""),
                "source": r.get("source", ""),
                "score": r.get("score", 0.0),
                "topic": r.get("topic", ""),
                "tags": self._decode_tags(r.get("tags", "[]")),
            }
            if full and original:
                entry["full"] = True
            if "_collection" in r:
                entry["collection"] = r["_collection"]
            if "_scope" in r:
                entry["scope"] = r["_scope"]
            if "_scope_id" in r:
                entry["scope_id"] = r["_scope_id"]
            formatted.append(entry)
        return formatted

    # -----------------------------------------------------------------
    # Command handlers — Temporal Facts
    # -----------------------------------------------------------------

    async def cmd_memory_fact_get(self, args: dict) -> dict:
        """Get current (or as-of) value of a temporal fact."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_fact_get")

        as_of = args.get("as_of")

        try:
            entry = await self._service.fact_get(project_id, key, as_of=as_of)
            if entry is None:
                return {
                    "success": True,
                    "found": False,
                    "project_id": project_id,
                    "key": key,
                }
            return {
                "success": True,
                "found": True,
                "project_id": project_id,
                **self._format_temporal_entry(entry),
            }
        except Exception as e:
            self._log.error("memory_fact_get failed: %s", e, exc_info=True)
            return {"error": f"Fact get failed: {e}"}

    async def cmd_memory_fact_set(self, args: dict) -> dict:
        """Set a temporal fact, closing the previous validity window."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}
        value = args.get("value")
        if value is None:
            return {"error": "value is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_fact_set")

        try:
            entry = await self._service.fact_set(project_id, key, value)
            return {
                "success": True,
                "project_id": project_id,
                **self._format_temporal_entry(entry),
            }
        except Exception as e:
            self._log.error("memory_fact_set failed: %s", e, exc_info=True)
            return {"error": f"Fact set failed: {e}"}

    async def cmd_memory_fact_history(self, args: dict) -> dict:
        """Retrieve full history of a temporal fact."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_fact_history")

        try:
            entries = await self._service.fact_history(project_id, key)
            return {
                "success": True,
                "project_id": project_id,
                "key": key,
                "count": len(entries),
                "history": [self._format_temporal_entry(e) for e in entries],
            }
        except Exception as e:
            self._log.error("memory_fact_history failed: %s", e, exc_info=True)
            return {"error": f"Fact history failed: {e}"}

    async def cmd_memory_fact_list(self, args: dict) -> dict:
        """List all temporal fact entries in a namespace."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_fact_list")

        namespace = args.get("namespace", "")
        current_only = args.get("current_only", True)

        try:
            entries = await self._service.fact_list(
                project_id, namespace, current_only=current_only
            )
            return {
                "success": True,
                "project_id": project_id,
                "namespace": namespace,
                "current_only": current_only,
                "count": len(entries),
                "entries": [self._format_temporal_entry(e) for e in entries],
            }
        except Exception as e:
            self._log.error("memory_fact_list failed: %s", e, exc_info=True)
            return {"error": f"Fact list failed: {e}"}

    # -----------------------------------------------------------------
    # Command handlers — Index Management
    # -----------------------------------------------------------------

    async def cmd_memory_reindex(self, args: dict) -> dict:
        """Reindex vault filesystem into Milvus."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        # TODO: implement vault scanning and re-indexing via MemSearch
        # This requires a per-scope MemSearch instance that scans vault
        # directories and re-embeds changed content.
        return self._not_implemented("memory_reindex")

    async def cmd_memory_stats(self, args: dict) -> dict:
        """Get collection statistics."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_stats")

        scope = args.get("scope")

        try:
            stats = await self._service.stats(project_id, scope=scope)
            return {"success": True, **stats}
        except Exception as e:
            self._log.error("memory_stats failed: %s", e, exc_info=True)
            return {"error": f"Stats failed: {e}"}

    # -----------------------------------------------------------------
    # Command stubs — Profile / Factsheet / Knowledge
    # (remain stubs until v1 is deprecated and these tools transfer)
    # -----------------------------------------------------------------

    async def cmd_view_profile(self, args: dict) -> dict:
        """View the project profile."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        return self._not_implemented("view_profile")

    async def cmd_edit_project_profile(self, args: dict) -> dict:
        """Replace the project profile content."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        content = args.get("content")
        if not content:
            return {"error": "content is required"}
        return self._not_implemented("edit_project_profile")

    async def cmd_project_factsheet(self, args: dict) -> dict:
        """View or update the project factsheet."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        action = args.get("action", "view")
        if action not in ("view", "update"):
            return {"error": f"Unknown action '{action}'. Use 'view' or 'update'."}
        return self._not_implemented("project_factsheet")

    async def cmd_project_knowledge(self, args: dict) -> dict:
        """Read or list knowledge topics."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        action = args.get("action", "read")
        if action not in ("read", "list"):
            return {"error": f"Unknown action '{action}'. Use 'read' or 'list'."}

        if action == "read" and not args.get("topic"):
            return {"error": "topic is required for action='read'"}
        return self._not_implemented("project_knowledge")

    # -----------------------------------------------------------------
    # Command stubs — Compaction / Consolidation
    # -----------------------------------------------------------------

    async def cmd_compact_memory(self, args: dict) -> dict:
        """Compact memory — summarize old entries, remove stale ones."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        return self._not_implemented("compact_memory")

    async def cmd_consolidate(self, args: dict) -> dict:
        """Run knowledge consolidation."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        mode = args.get("mode", "daily")
        if mode not in ("daily", "deep", "bootstrap"):
            return {"error": (f"Invalid mode '{mode}'. Use 'daily', 'deep', or 'bootstrap'.")}
        return self._not_implemented("consolidate")
