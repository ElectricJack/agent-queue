"""Internal plugin: Memory v2 — unified memory operations via memsearch/Milvus.

Backed by a memsearch fork and Milvus.  See the design spec at
``docs/specs/design/memory-plugin.md`` (especially Sections 3–4) for the full
architecture.

Agent-facing interface
----------------------
Only three tools are exposed to agents:

* ``memory_store`` — unified write with LLM-based auto-classification
  (routes facts to KV, insights/knowledge/guidance to semantic memory)
* ``memory_recall`` — smart retrieval (KV exact match → semantic fallback)
* ``memory_delete`` — explicit removal by chunk_hash

All other handlers (search, KV ops, facts, health, compaction, etc.) are
retained for internal use by subsystems (memory extractor, consolidation,
reflection engine) but are NOT registered as agent-facing tools.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from src.plugins.base import InternalPlugin, PluginContext

if TYPE_CHECKING:
    from src.plugins.internal.memory_v2.service import MemoryV2Service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions — new v2 architecture
# ---------------------------------------------------------------------------

TOOL_CATEGORY = "memory"

# Agent-facing tools — only these are registered for LLM tool use.
# All other handlers remain available internally for subsystems.
AGENT_TOOLS: frozenset[str] = frozenset(
    {
        "memory_store",
        "memory_recall",
        "memory_delete",
        "memory_follow",
        # Exposed for consolidation / curation workflows so supervisor
        # tasks can rewrite, promote, and discover memories directly.
        "memory_update",
        "memory_promote",
        "memory_promote_to_knowledge",
        "memory_search",
    }
)

# Backward-compat alias used by tests and other modules.
V2_ONLY_TOOLS = AGENT_TOOLS

TOOL_DEFINITIONS: list[dict] = [
    # ---- Unified store (agent-facing) ----
    {
        "name": "memory_store",
        "description": (
            "Store information in project memory.  Automatically classifies "
            "content: key-value facts (e.g. 'the test framework is pytest') "
            "are stored for fast exact lookup; insights, knowledge, and "
            "guidance are stored as semantic memories with deduplication and "
            "vector indexing.  Topic and tags are auto-inferred when not "
            "provided.  Use this for ALL memory writes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The information to store.  Can be a fact "
                        "('the test framework is pytest'), an insight, "
                        "a pattern, guidance, or any knowledge worth "
                        "remembering."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags.  Auto-generated if omitted.",
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional topic override (e.g. 'testing', "
                        "'authentication').  Auto-inferred if omitted."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Optional scope override.  One of 'system', "
                        "'supervisor', 'agenttype_{type}', or "
                        "'project_{id}'.  Defaults to project scope."
                    ),
                },
                "source_task": {
                    "type": "string",
                    "description": "Task ID that produced this information.",
                },
                "source_playbook": {
                    "type": "string",
                    "description": "Playbook name that generated this memory.",
                },
            },
            "required": ["content"],
        },
    },
    # ---- Save (spec §8) — internal, not agent-facing ----
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
                "source_playbook": {
                    "type": "string",
                    "description": (
                        "Playbook name that generated this memory (e.g. "
                        "'task-outcome', 'reflection').  For provenance tracking."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Memory scope.  One of 'system', 'supervisor', "
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
                        "Memory scope to search.  One of 'system', 'supervisor', "
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
                        "Memory scope.  One of 'system', 'supervisor', "
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
                        "Memory scope to browse.  One of 'system', 'supervisor', "
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
                    "enum": ["document", "kv", "temporal", "all"],
                    "description": (
                        "Filter by entry type.  Defaults to 'document' (semantic "
                        "memories/insights).  Use 'all' to list all entry types."
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
    # ---- Consolidation Tools (spec §10) ----
    {
        "name": "memory_delete",
        "description": (
            "Delete a memory entry by its chunk_hash.  Removes the entry "
            "from the Milvus index and deletes the corresponding vault "
            "markdown file.  Use during reflection consolidation to remove "
            "duplicate or stale memories after merging them into stronger "
            "entries.  Returns confirmation of what was deleted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines default scope).",
                },
                "chunk_hash": {
                    "type": "string",
                    "description": (
                        "The chunk_hash (unique identifier) of the memory "
                        "entry to delete.  Obtain this from memory_search "
                        "or memory_list results."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Memory scope containing the entry.  One of 'system', "
                        "'supervisor', 'agenttype_{type}', or 'project_{id}'.  "
                        "Defaults to the project scope derived from project_id."
                    ),
                },
            },
            "required": ["chunk_hash"],
        },
    },
    # ---- Link Following (vault knowledge graph navigation) ----
    {
        "name": "memory_follow",
        "description": (
            "Follow a wiki-link from a memory result to read the linked "
            "vault file.  Use this to get additional context from "
            "related_links returned by memory_recall.  Returns the file "
            "content plus any further related_links for continued "
            "navigation through the knowledge graph.  Works with hub "
            "pages (index files), glossary entries, guidance docs, "
            "insights, playbooks, and any other vault file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "link": {
                    "type": "string",
                    "description": (
                        "Wiki-link target path from a related_links entry "
                        "(e.g. 'projects/agent-queue/memory/guidance/qa/"
                        "testing-patterns' or 'glossary/reflection-engine')"
                    ),
                },
            },
            "required": ["link"],
        },
    },
    {
        "name": "memory_update",
        "description": (
            "Update an existing memory entry's content, tags, or topic.  "
            "Use during reflection consolidation to correct outdated "
            "insights, change confidence tags (e.g. #provisional → "
            "#verified), add tags, or rewrite content based on new "
            "evidence.  The entry's embedding is recomputed if content "
            "changes.  Unlike memory_save, this directly targets a known "
            "entry by chunk_hash — no dedup search is performed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (determines default scope).",
                },
                "chunk_hash": {
                    "type": "string",
                    "description": (
                        "The chunk_hash of the memory entry to update.  "
                        "Obtain from memory_search or memory_list results."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "New content to replace the existing entry's content.  "
                        "If omitted, the content is not changed (useful for "
                        "tag-only updates)."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "New tags to set on the entry.  Replaces the existing "
                        "tags entirely.  To add tags, include both old and new."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "New topic to set on the entry.  Replaces the existing "
                        "topic.  Omit to leave unchanged."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Memory scope.  One of 'system', 'supervisor', "
                        "'agenttype_{type}', or 'project_{id}'.  Defaults to "
                        "the project scope derived from project_id."
                    ),
                },
            },
            "required": ["project_id", "chunk_hash"],
        },
    },
    {
        "name": "memory_promote_to_knowledge",
        "description": (
            "Promote a stable insight into the curated 'knowledge/' "
            "subdirectory for this scope.  The entry is rewritten under "
            "vault/.../memory/knowledge/ with the 'knowledge' tag, "
            "re-indexed into Milvus, and the source insight is deleted.  "
            "Use during nightly consolidation when a fact has proven "
            "durable (multiple retrievals, aged, representative of a "
            "cluster).  Optionally rewrite the content/topic/tags in "
            "place — use this to merge a cluster of near-duplicate "
            "insights into one canonical knowledge entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (used for scope resolution).",
                },
                "chunk_hash": {
                    "type": "string",
                    "description": "chunk_hash of the source insight to promote.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Optional rewritten content.  If omitted, the "
                        "source insight's content is preserved verbatim."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional topic.  Defaults to the source insight's "
                        "topic.  Set this to the canonical cluster topic "
                        "when merging related insights."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional tag list.  The 'knowledge' and 'curated' "
                        "tags are always added; 'insight' and "
                        "'auto-extracted' are always removed."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Optional scope override (e.g. 'project_{id}', "
                        "'agenttype_{type}').  Defaults to the project scope."
                    ),
                },
            },
            "required": ["project_id", "chunk_hash"],
        },
    },
    {
        "name": "memory_promote",
        "description": (
            "Promote a memory from a narrower scope to a broader scope.  "
            "Copies the entry to the target scope (e.g. project → "
            "agent-type, or agent-type → system) and optionally deletes "
            "the original.  Use during reflection consolidation when an "
            "insight discovered in one project applies broadly across "
            "projects — it should live in agent-type memory, not project "
            "memory.  The entry is re-embedded in the target collection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (used for scope resolution and context).",
                },
                "chunk_hash": {
                    "type": "string",
                    "description": ("The chunk_hash of the source memory entry to promote."),
                },
                "source_scope": {
                    "type": "string",
                    "description": (
                        "Scope to copy FROM (e.g. 'project_my-app').  Defaults "
                        "to the project scope derived from project_id."
                    ),
                },
                "target_scope": {
                    "type": "string",
                    "description": (
                        "Scope to copy TO (e.g. 'agenttype_coding', 'system').  "
                        "Must be broader than source_scope."
                    ),
                },
                "delete_source": {
                    "type": "boolean",
                    "description": (
                        "If true, delete the original entry from the source "
                        "scope after successful promotion.  Default false "
                        "(keeps both copies)."
                    ),
                    "default": False,
                },
            },
            "required": ["project_id", "chunk_hash", "target_scope"],
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
                        "Use 'system' or 'supervisor' for global reindex."
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
    {
        "name": "memory_health",
        "description": (
            "Get memory health metrics for a project.  Surfaces collection "
            "sizes, growth rate (new documents in last 7 days), stale document "
            "count (not retrieved in N days), most-retrieved documents, "
            "retrieval hit rate, and contradiction count (entries tagged "
            "#contested).  Per spec §6 — Memory Health View."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to check health for.",
                },
                "scope": {
                    "type": "string",
                    "description": "Scope to inspect.  Defaults to project scope.",
                },
                "stale_days": {
                    "type": "integer",
                    "description": (
                        "Number of days without retrieval before a document is "
                        "considered stale.  Default 30."
                    ),
                    "default": 30,
                },
                "top_n": {
                    "type": "integer",
                    "description": (
                        "Number of most-retrieved documents to include.  Default 10."
                    ),
                    "default": 10,
                },
            },
            "required": ["project_id"],
        },
    },
    # ---- Stale memory detection (spec §6 — Roadmap 6.5.3) ----
    {
        "name": "memory_stale",
        "description": (
            "Find stale memory documents — candidates for archival.  "
            "Returns documents not retrieved in N days (default 30) or "
            "never retrieved at all.  Each result includes days since "
            "last retrieval, retrieval count, creation date, and a "
            "content preview.  Supports pagination via offset/limit.  "
            "Per spec §6 — Memory Health View: 'Stale memories — Not "
            "retrieved in N days — candidates for archival.'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to find stale memories for.",
                },
                "scope": {
                    "type": "string",
                    "description": "Scope to inspect.  Defaults to project scope.",
                },
                "stale_days": {
                    "type": "integer",
                    "description": (
                        "Number of days without retrieval before a document is "
                        "considered stale.  Default 30."
                    ),
                    "default": 30,
                },
                "sort": {
                    "type": "string",
                    "description": (
                        "Sort order: 'staleness' (default, never-retrieved first "
                        "then oldest-retrieved), 'created' (oldest first), "
                        "'retrieval_count' (least retrieved first)."
                    ),
                    "enum": ["staleness", "created", "retrieval_count"],
                    "default": "staleness",
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of entries to skip for pagination.  Default 0.",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum entries to return.  Default 50, max 200."
                    ),
                    "default": 50,
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

    def _resolve_project_id(self, args: dict) -> str | None:
        """Resolve project_id from args, preferring the active project.

        When an active project is set, always use it — the LLM often
        guesses project IDs incorrectly (e.g. underscores instead of
        hyphens).  The active project ID from the system is authoritative.
        """
        ctx = getattr(self, "_ctx", None)
        active = getattr(ctx, "active_project_id", None) if ctx is not None else None
        if isinstance(active, str) and active:
            args["project_id"] = active
            return active
        return args.get("project_id")

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._log = ctx.logger

        # Initialize the MemoryV2Service backend.
        self._service: MemoryV2Service | None = None
        self._extractor: Any = None  # MemoryExtractor, created below
        await self._init_service(ctx)

        # -- Map command names to handlers --
        # Full command table — all handlers.  Only AGENT_TOOLS are
        # registered for LLM tool use; the rest remain available for
        # internal subsystem calls.
        all_commands: dict[str, object] = {
            # Unified store (agent-facing)
            "memory_store": self.cmd_memory_store,
            # Save (spec §8) — internal
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
            # Consolidation / curation
            "memory_delete": self.cmd_memory_delete,
            "memory_update": self.cmd_memory_update,
            "memory_promote": self.cmd_memory_promote,
            "memory_promote_to_knowledge": self.cmd_memory_promote_to_knowledge,
            # Index management
            "memory_reindex": self.cmd_memory_reindex,
            "memory_stats": self.cmd_memory_stats,
            # Health (spec §6)
            "memory_health": self.cmd_memory_health,
            # Profile / knowledge
            "view_profile": self.cmd_view_profile,
            "edit_project_profile": self.cmd_edit_project_profile,
            "project_factsheet": self.cmd_project_factsheet,
            "project_knowledge": self.cmd_project_knowledge,
            # Compaction
            "compact_memory": self.cmd_compact_memory,
            "consolidate": self.cmd_consolidate,
        }

        # -- Register all commands (callable via handler.execute()) --
        for name, handler in all_commands.items():
            ctx.register_command(name, handler)

        # -- Register only agent-facing tool schemas (visible to LLM) --
        agent_tools_registered = 0
        for tool_def in TOOL_DEFINITIONS:
            if tool_def["name"] in AGENT_TOOLS:
                ctx.register_tool(dict(tool_def), category=TOOL_CATEGORY)
                agent_tools_registered += 1

        # Initialize the MemoryExtractor (event-driven background extraction).
        await self._init_extractor(ctx)

        status = "connected" if self._service and self._service.available else "degraded"
        extractor_status = "active" if self._extractor else "disabled"
        self._log.info(
            "MemoryV2Plugin initialized (%s, extractor=%s, %d agent-facing tools / %d total commands)",
            status,
            extractor_status,
            agent_tools_registered,
            len(all_commands),
        )

    async def _init_extractor(self, ctx: PluginContext) -> None:
        """Initialize the MemoryExtractor for background knowledge extraction.

        Creates and starts the extractor if:
        - The memory service is available
        - The ``memory_extractor.enabled`` config flag is set

        The extractor subscribes to system events (task completion, etc.)
        and automatically extracts facts/insights into the memory system.
        """
        if not self._service or not self._service.available:
            return

        try:
            config_svc = ctx.get_service("config")
            extractor_cfg = getattr(config_svc, "memory_extractor", None)
            if not extractor_cfg or not extractor_cfg.get("enabled"):
                return

            from src.plugins.internal.memory_v2.extractor import MemoryExtractor

            db_svc = ctx.get_service("db")
            chat_provider_cfg = getattr(config_svc, "chat_provider", {})

            self._extractor = MemoryExtractor(
                bus=ctx.bus,
                db=db_svc,
                memory_service=self._service,
                config=extractor_cfg,
                chat_provider_config=chat_provider_cfg,
                save_callback=self._do_memory_save,
            )
            self._extractor.subscribe()
            await self._extractor.start()
            self._log.info("MemoryExtractor started (background extraction active)")
        except Exception as e:
            self._log.warning("MemoryExtractor initialization failed: %s", e)
            self._extractor = None

    async def _init_service(self, ctx: PluginContext) -> None:
        """Initialize the MemoryV2Service backend from config.

        Reads Milvus/embedding settings from the ``config`` service and
        creates the service.  If memsearch is not installed or config is
        unavailable, the plugin operates in degraded mode (all commands
        return graceful error responses).
        """
        try:
            from src.plugins.internal.memory_v2.service import MemoryV2Service

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
                # Populate the profile-to-shared-scope alias map so
                # ``agenttype_{id}`` lookups redirect when a profile
                # declares ``memory_scope_id``.  Done once at startup;
                # profile changes require a daemon restart or an explicit
                # ``refresh_memory_scope_aliases`` call to propagate.
                await self._refresh_memory_scope_aliases(ctx)
                # Start vault file watchers for auto-indexing
                self._start_vault_watchers(config_svc, memory_cfg)
            else:
                reason = (
                    getattr(self._service, "unavailable_reason", None)
                    or "unknown (check earlier log entries)"
                )
                self._log.warning(
                    "MemoryV2Service initialized but not available: %s",
                    reason,
                )
        except Exception:
            self._log.warning(
                "Failed to initialize MemoryV2Service — operating in degraded mode",
                exc_info=True,
            )
            self._service = None

    async def _refresh_memory_scope_aliases(self, ctx: Any) -> None:
        """Rebuild the service's profile-to-shared-scope alias map.

        Reads all ``agent_profiles`` rows and forwards the subset that
        declare ``memory_scope_id`` to ``MemoryV2Service.set_scope_alias_map``.
        Silently no-ops when the DB is not reachable or the service is
        unavailable.
        """
        if not self._service or not self._service.available:
            return
        try:
            db_svc = ctx.get_service("db")
        except Exception:
            return
        try:
            profiles = await db_svc.list_profiles()
        except Exception:
            self._log.debug(
                "Failed to load profiles for scope alias map", exc_info=True
            )
            return
        aliases: dict[str, str] = {
            p.id: p.memory_scope_id
            for p in profiles
            if getattr(p, "memory_scope_id", None)
        }
        self._service.set_scope_alias_map(aliases)
        if aliases:
            self._log.info(
                "Memory scope alias map loaded (%d profile(s)): %s",
                len(aliases),
                ", ".join(f"{k}->{v}" for k, v in sorted(aliases.items())),
            )

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
        # Stop memory extractor first (it writes to the service)
        if self._extractor:
            try:
                await self._extractor.stop()
                self._log.info("MemoryExtractor stopped")
            except Exception:
                pass
            self._extractor = None

        # Stop vault watchers
        for watcher in getattr(self, "_vault_watchers", []):
            try:
                watcher.stop()
            except Exception:
                pass
        self._vault_watchers = []

        if self._service:
            await self._service.shutdown()
            self._service = None

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _start_vault_watchers(self, config_svc: Any, memory_cfg: dict) -> None:
        """Start MemSearch file watchers for auto-indexing vault changes.

        Creates a MemSearch instance per scope (system + each project) that
        watches the vault directories for markdown changes and auto-indexes
        them into the corresponding Milvus collection.
        """
        self._vault_watchers: list = []

        try:
            from memsearch import MemSearch
            from memsearch.scoping import (
                MemoryScope,
                collection_name,
                vault_paths,
            )
        except ImportError:
            self._log.debug("memsearch not available, skipping vault watchers")
            return

        app_config = getattr(config_svc, "_config", None) if config_svc else None
        data_dir = config_svc.data_dir if config_svc else ""
        if not data_dir:
            return

        milvus_uri = memory_cfg.get("milvus_uri", "~/.agent-queue/memsearch/milvus.db")
        milvus_token = memory_cfg.get("milvus_token", "")
        embedding_provider = memory_cfg.get("embedding_provider", "openai")
        embedding_model = memory_cfg.get("embedding_model", "")
        embedding_base_url = memory_cfg.get("embedding_base_url", "")
        embedding_api_key = memory_cfg.get("embedding_api_key", "")

        # Collect scopes to watch: system + all projects
        scopes: list[tuple[str, list[str]]] = []

        # System scope
        sys_paths = vault_paths(MemoryScope.SYSTEM)
        sys_dirs = [
            os.path.join(data_dir, p) for p in sys_paths
            if not p.endswith(".md")  # only directories, not individual files
        ]
        sys_dirs = [d for d in sys_dirs if os.path.isdir(d)]
        if sys_dirs:
            scopes.append((
                collection_name(MemoryScope.SYSTEM),
                sys_dirs,
            ))

        # Project scopes — scan vault/projects/ directory for project folders
        projects_dir = os.path.join(data_dir, "vault", "projects")
        if os.path.isdir(projects_dir):
            for pid in os.listdir(projects_dir):
                pid_path = os.path.join(projects_dir, pid)
                if not os.path.isdir(pid_path):
                    continue
                proj_paths = vault_paths(MemoryScope.PROJECT, pid)
                proj_dirs = [
                    os.path.join(data_dir, p) for p in proj_paths
                    if not p.endswith(".md")
                ]
                proj_dirs = [d for d in proj_dirs if os.path.isdir(d)]
                if proj_dirs:
                    scopes.append((
                        collection_name(MemoryScope.PROJECT, pid),
                        proj_dirs,
                    ))

        # Create a MemSearch instance per scope and start watching
        for coll_name, paths in scopes:
            try:
                mem = MemSearch(
                    paths=paths,
                    embedding_provider=embedding_provider,
                    embedding_model=embedding_model or None,
                    embedding_base_url=embedding_base_url or None,
                    embedding_api_key=embedding_api_key or None,
                    milvus_uri=milvus_uri,
                    milvus_token=milvus_token or None,
                    collection=coll_name,
                )
                watcher = mem.watch(debounce_ms=3000)
                self._vault_watchers.append(watcher)
                self._log.info(
                    "Vault watcher started for collection %s (%d paths)",
                    coll_name, len(paths),
                )
            except Exception:
                self._log.warning(
                    "Failed to start vault watcher for %s", coll_name,
                    exc_info=True,
                )

        if self._vault_watchers:
            self._log.info(
                "Started %d vault watcher(s) for auto-indexing",
                len(self._vault_watchers),
            )

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
        """Return a response for when the service is not available.

        Surfaces the concrete reason (missing ollama package, bad API
        key, unreachable Milvus, etc.) captured during
        :meth:`MemoryV2Service.initialize` so agents can report the
        actual cause instead of spawning follow-up tasks that re-diagnose
        the generic "not available" message.
        """
        reason = getattr(self._service, "unavailable_reason", None) if self._service else None
        if not reason:
            reason = (
                "MemoryV2Service is not available. Ensure memsearch is "
                "installed and `memory.enabled: true` in config.yaml."
            )
        return {
            "error": f"{command}: {reason}",
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
    # Command handler — Unified store (agent-facing)
    # -----------------------------------------------------------------

    # Regex for obvious key: value or key = value lines.
    _KV_LINE_RE = re.compile(
        r"^\s*"
        r"[-•*]?\s*"                       # optional bullet
        r"(?P<key>[A-Za-z][A-Za-z0-9_ .-]*?)"  # key (starts with letter)
        r"\s*[:=]\s*"                       # separator
        r"(?P<value>.+?)\s*$",             # value
    )

    # Regex for headings that act as category/namespace markers.
    _HEADING_RE = re.compile(r"^\s*#{1,3}\s+(.+?)\s*$")

    def _try_parse_facts(self, content: str) -> list[dict] | None:
        """Try to deterministically parse content as one or more key:value facts.

        Returns a list of ``{"key": ..., "value": ..., "namespace": ...}``
        dicts if the content looks like facts, or ``None`` if it doesn't.

        Handles:
        - Single line: ``resolution_x: 1080``
        - Multiple lines: ``resolution_x: 1080\\nresolution_y: 720``
        - Bullet lists: ``- framework: pytest\\n- db: sqlite``
        - Equals sign: ``max_retries = 3``
        - Headings as categories: ``## Display\\nresolution_x: 1080``
        """
        lines = [ln for ln in content.strip().splitlines() if ln.strip()]
        if not lines:
            return None

        facts: list[dict] = []
        namespace = "project"
        for line in lines:
            # Check for heading → sets namespace for subsequent facts
            hm = self._HEADING_RE.match(line)
            if hm:
                raw_ns = hm.group(1).strip()
                namespace = raw_ns.lower().replace(" ", "_").replace("-", "_")
                namespace = re.sub(r"_+", "_", namespace).strip("_")
                continue

            m = self._KV_LINE_RE.match(line)
            if not m:
                # If any non-empty, non-heading line doesn't match,
                # bail out — this isn't a pure fact list.
                return None
            raw_key = m.group("key").strip()
            value = m.group("value").strip()
            # Normalize key to snake_case
            key = raw_key.lower().replace(" ", "_").replace("-", "_").replace(".", "_")
            key = re.sub(r"_+", "_", key).strip("_")
            if not key or not value:
                return None
            facts.append({"key": key, "value": value, "namespace": namespace})

        return facts if facts else None

    _CLASSIFY_PROMPT = (
        "Classify this content for a project memory system.\n\n"
        "CONTENT:\n{content}\n\n"
        "TYPES:\n"
        "- facts: One or more concrete key-value pairs.\n"
        "  Examples of facts: 'test framework is pytest', "
        "'resolution is 1080x720', 'max retries: 3', "
        "'db_host: localhost port: 5432'\n"
        "- insight: An observation or pattern discovered\n"
        "- knowledge: Deeper understanding of how something works\n"
        "- guidance: A rule or prescription for how to work\n\n"
        "IMPORTANT: If the content contains ANY concrete settings, "
        "parameters, configuration values, or named properties with "
        "values, classify as facts. When in doubt between fact and "
        "insight, prefer fact.\n\n"
        "CONTROLLED TOPICS: {topics}\n\n"
        "OUTPUT: A single JSON object.\n"
        "For single fact: {{\"type\": \"facts\", \"items\": "
        "[{{\"key\": \"snake_case\", \"value\": \"the value\", "
        "\"namespace\": \"category\"}}]}}\n"
        "For multiple facts: {{\"type\": \"facts\", \"items\": "
        "[{{\"key\": \"k1\", \"value\": \"v1\", \"namespace\": \"display\"}}, "
        "{{\"key\": \"k2\", \"value\": \"v2\", \"namespace\": \"display\"}}]}}\n"
        "(namespace defaults to 'project' — use a descriptive category "
        "when the content implies one, e.g. 'display', 'database', "
        "'deployment', 'conventions')\n"
        "For non-facts: {{\"type\": \"insight\", "
        "\"topic\": \"topic-from-list\"}}\n"
        "(Use the best topic from CONTROLLED TOPICS, or a new lowercase "
        "hyphenated topic if none fit.)\n"
        "Output ONLY the JSON object, nothing else."
    )

    async def _classify_content(self, content: str) -> dict:
        """Classify content for routing to the appropriate storage backend.

        First tries deterministic key:value parsing.  Falls back to an
        LLM call for ambiguous content.

        Returns
        -------
        dict
            ``{"type": "facts", "items": [{"key": ..., "value": ...}, ...]}``
            for facts, or ``{"type": "insight"|"knowledge"|"guidance",
            "topic": ...}`` for non-facts.
        """
        # --- Deterministic fast path: obvious key:value lines ---
        parsed = self._try_parse_facts(content)
        if parsed:
            return {"type": "facts", "items": parsed}

        # --- LLM classification for ambiguous content ---
        topics_list = ", ".join(self.CONTROLLED_TOPICS)
        prompt = self._CLASSIFY_PROMPT.format(
            content=content[:2000],
            topics=topics_list,
        )
        try:
            raw = await self._ctx.invoke_llm(
                prompt,
                model="claude-haiku-4-20250514",
            )
            # Strip markdown fences if present
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            result = json.loads(text)
            item_type = result.get("type", "insight")

            # Handle both old "fact" and new "facts" format from LLM
            if item_type in ("fact", "facts"):
                items = result.get("items", [])
                # Single-fact backward compat
                if not items and result.get("key") and result.get("value"):
                    items = [{"key": result["key"], "value": result["value"]}]
                if items and all(i.get("key") and i.get("value") for i in items):
                    return {"type": "facts", "items": items}
                # Incomplete fact extraction — fall through to insight
                return {"type": "insight", "topic": result.get("topic")}

            # Normalize topic the same way _infer_topic_via_llm does
            topic = result.get("topic")
            if topic:
                topic = topic.strip().lower().strip("\"'")
                topic = topic.replace(" ", "-").replace("_", "-")
                topic = re.sub(r"[^a-z0-9-]", "", topic)
                topic = re.sub(r"-{2,}", "-", topic).strip("-")
                if not topic:
                    topic = None
            return {"type": item_type if item_type in ("knowledge", "guidance") else "insight",
                    "topic": topic}
        except Exception:
            self._log.debug("Content classification failed, defaulting to insight")
            return {"type": "insight", "topic": None}

    async def cmd_memory_store(self, args: dict) -> dict:
        """Unified memory storage with LLM-based content classification.

        Classifies content as either a key-value fact or a semantic memory
        (insight/knowledge/guidance) and routes to the appropriate backend.
        """
        content = args.get("content")
        if not content:
            return {"error": "content is required"}

        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_store")

        try:
            classification = await self._classify_content(content)
        except Exception:
            classification = {"type": "insight", "topic": None}

        scope = args.get("scope")

        try:
            if classification["type"] == "facts":
                items = classification.get("items", [])
                stored: list[dict] = []
                for item in items:
                    result = await self._service.kv_set(
                        project_id=project_id,
                        namespace=item.get("namespace", "project"),
                        key=item["key"],
                        value=item["value"],
                        scope=scope,
                    )
                    stored.append({
                        "key": item["key"],
                        "value": item["value"],
                        "namespace": item.get("namespace", "project"),
                    })
                return {
                    "success": True,
                    "stored_as": "fact",
                    "count": len(stored),
                    "facts": stored,
                    "project_id": project_id,
                }
            else:
                # insight / knowledge / guidance → semantic memory save
                stored_type = classification["type"]
                tags = args.get("tags") or [stored_type, "auto-generated"]
                # Use pre-inferred topic to skip the second LLM call
                topic = args.get("topic") or classification.get("topic")
                result = await self._do_memory_save(
                    project_id=project_id,
                    content=content,
                    tags=tags,
                    topic=topic,
                    source_task=args.get("source_task"),
                    source_playbook=args.get("source_playbook"),
                    scope=scope,
                )
                result["stored_as"] = stored_type
                return result
        except Exception as e:
            self._log.error("memory_store failed: %s", e, exc_info=True)
            return {"error": f"Store failed: {e}"}

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
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        content = args.get("content")
        if not content:
            return {"error": "content is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_save")

        tags = args.get("tags") or ["insight", "auto-generated"]
        topic = args.get("topic")
        source_task = args.get("source_task")
        source_playbook = args.get("source_playbook")
        scope = args.get("scope")

        try:
            return await self._do_memory_save(
                project_id=project_id,
                content=content,
                tags=tags,
                topic=topic,
                source_task=source_task,
                source_playbook=source_playbook,
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
        source_playbook: str | None = None,
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
                track_retrieval=False,  # dedup check is internal, not a user retrieval
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
                source_playbook=source_playbook,
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
                source_playbook=source_playbook,
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
        source_playbook: str | None = None,
        scope: str | None,
    ) -> dict:
        """Handle related dedup (similarity 0.8–0.95) — merge or contest.

        First checks whether the new content *contradicts* the existing
        memory (spec §7 Q2 — contradiction detection).  If a contradiction
        is detected, both memories are tagged ``#contested`` and saved as
        separate entries so a human can review them.

        If no contradiction, proceeds with the normal merge flow: invokes
        the LLM to combine old + new content, then updates the existing
        entry with the merged result.  If the merged content exceeds
        ~200 tokens, a summary is generated per spec §9.
        """
        chunk_hash = existing.get("chunk_hash", "")
        old_content = existing.get("content", "")
        old_tags = self._decode_tags(existing.get("tags", "[]"))

        # --- Contradiction detection (spec §7 Q2) ---
        # Before merging, check if the new content contradicts the existing
        # memory.  Contradictory memories are tagged #contested and kept
        # separate for human review.
        is_contradiction = await self._detect_contradiction(old_content, content)

        if is_contradiction:
            return await self._handle_contradiction(
                project_id=project_id,
                content=content,
                existing=existing,
                similarity=similarity,
                tags=tags,
                topic=topic,
                source_task=source_task,
                source_playbook=source_playbook,
                scope=scope,
            )

        # --- Normal merge flow (no contradiction) ---

        # Merge tags (preserve both, deduplicate)
        merged_tags = list(dict.fromkeys(old_tags + tags))

        # Attempt LLM merge — pass tag context for better results
        merged_content = await self._merge_via_llm(
            old_content,
            content,
            old_tags=old_tags,
            new_tags=tags,
        )

        if not chunk_hash:
            self._log.warning("Merge match has no chunk_hash, creating new entry instead")
            # Fall through to creating new with merged content
            return await self._handle_create_new(
                project_id=project_id,
                content=merged_content,
                tags=merged_tags,
                topic=topic,
                source_task=source_task,
                source_playbook=source_playbook,
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
            source_task=source_task,
            scope=scope,
        )
        return {
            "success": True,
            "action": "merged",
            "project_id": project_id,
            "similarity_score": round(similarity, 4),
            "merged_with": chunk_hash,
            "merged_tags": merged_tags,
            "has_summary": summary is not None,
            **result,
        }

    async def _handle_contradiction(
        self,
        *,
        project_id: str,
        content: str,
        existing: dict,
        similarity: float,
        tags: list[str],
        topic: str | None,
        source_task: str | None,
        source_playbook: str | None = None,
        scope: str | None,
    ) -> dict:
        """Handle contradictory memories — tag as ``#contested`` and keep both.

        Per spec §7 Q2 (Memory conflicts between agents): when two memories
        contradict each other, both are tagged ``#contested`` and the new
        memory is saved as a separate entry.  This preserves both viewpoints
        for human review.

        The existing memory's tags are updated to include ``contested``.
        The new memory is created with ``contested`` in its tags.

        Parameters
        ----------
        project_id:
            Project identifier.
        content:
            The new (contradictory) content.
        existing:
            The existing memory entry that was matched.
        similarity:
            Similarity score between old and new.
        tags:
            Tags for the new memory.
        topic:
            Topic for the new memory.
        source_task:
            Source task ID.
        source_playbook:
            Source playbook name.
        scope:
            Memory scope.

        Returns
        -------
        dict
            Result with ``action: "contested"``, details of both memories.
        """
        chunk_hash = existing.get("chunk_hash", "")
        old_tags = self._decode_tags(existing.get("tags", "[]"))

        # --- Tag the existing memory as contested ---
        if chunk_hash and "contested" not in old_tags:
            contested_old_tags = list(dict.fromkeys(old_tags + ["contested"]))
            try:
                await self._service.update_document_content(
                    project_id,
                    chunk_hash,
                    existing.get("content", ""),
                    tags=contested_old_tags,
                    source_task=source_task,
                    scope=scope,
                )
                self._log.info(
                    "Tagged existing memory %s as #contested (contradiction detected)",
                    chunk_hash,
                )
            except Exception:
                self._log.warning(
                    "Failed to tag existing memory %s as #contested",
                    chunk_hash,
                    exc_info=True,
                )

        # --- Create the new memory as a separate contested entry ---
        contested_new_tags = list(dict.fromkeys(tags + ["contested"]))

        result = await self._handle_create_new(
            project_id=project_id,
            content=content,
            tags=contested_new_tags,
            topic=topic,
            source_task=source_task,
            source_playbook=source_playbook,
            scope=scope,
        )

        # Override action to indicate this was a contradiction, not a fresh create
        result["action"] = "contested"
        result["contradiction"] = True
        result["contested_with"] = chunk_hash
        result["similarity_score"] = round(similarity, 4)
        result["existing_content_preview"] = existing.get("content", "")[:200]
        return result

    async def _handle_create_new(
        self,
        *,
        project_id: str,
        content: str,
        tags: list[str],
        topic: str | None,
        source_task: str | None,
        source_playbook: str | None = None,
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
            source_playbook=source_playbook,
            scope=scope,
        )
        return {
            "success": True,
            "action": "created",
            "project_id": project_id,
            "has_summary": summary is not None,
            **result,
        }

    async def _merge_via_llm(
        self,
        old_content: str,
        new_content: str,
        *,
        old_tags: list[str] | None = None,
        new_tags: list[str] | None = None,
    ) -> str:
        """Merge two related memories via a lightweight LLM call.

        Per spec §8: "Combine these into a single memory.  If they
        contradict, prefer the newer information but note the change.
        Preserve tags from both."

        Falls back to simple concatenation if LLM is unavailable.

        Parameters
        ----------
        old_content:
            The existing (older) memory content.
        new_content:
            The incoming (newer) memory content.
        old_tags:
            Tags on the existing memory (for context).
        new_tags:
            Tags on the new memory (for context).
        """
        # Build tag context line if tags are available
        tag_context = ""
        if old_tags or new_tags:
            parts = []
            if old_tags:
                parts.append(f"Existing tags: {', '.join(old_tags)}")
            if new_tags:
                parts.append(f"New tags: {', '.join(new_tags)}")
            tag_context = f"\nTAG CONTEXT:\n{'; '.join(parts)}\n"

        prompt = (
            "You are merging two related memory entries into one concise, unified memory.\n\n"
            "EXISTING (OLDER) MEMORY:\n"
            f"{old_content}\n\n"
            "NEW (MORE RECENT) MEMORY:\n"
            f"{new_content}\n"
            f"{tag_context}\n"
            "RULES:\n"
            "1. Combine both into a single, cohesive memory entry.\n"
            "2. If they contradict, PREFER the newer information. Briefly note "
            'what changed (e.g. "Previously X, now Y." or "Updated: …").\n'
            "3. Preserve all distinct facts from both — do not drop information "
            "that is only in one of them.\n"
            "4. Keep the result concise — no longer than the longer of the two inputs.\n"
            "5. Output ONLY the merged memory content, no preamble or explanation.\n"
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
    # Contradiction detection (spec §7 Q2)
    # -----------------------------------------------------------------

    async def _detect_contradiction(
        self,
        existing_content: str,
        new_content: str,
    ) -> bool:
        """Detect whether two memories contradict each other.

        Per spec §7 Q2 (self-improvement.md): two agents might write
        contradictory insights.  This method uses a lightweight LLM call
        to determine if the new content contradicts the existing content.

        Returns ``True`` if a contradiction is detected, ``False`` if the
        memories are compatible (complementary, overlapping, or unrelated).

        Falls back to ``False`` (no contradiction) if the LLM is
        unavailable — this is a conservative default that preserves the
        existing merge behavior when detection is not possible.

        Parameters
        ----------
        existing_content:
            The content of the existing memory entry.
        new_content:
            The content of the incoming memory entry.

        Returns
        -------
        bool
            ``True`` if the memories contradict each other.
        """
        prompt = (
            "You are a contradiction detector for a developer knowledge base.\n\n"
            "EXISTING MEMORY:\n"
            f"{existing_content[:1500]}\n\n"
            "NEW MEMORY:\n"
            f"{new_content[:1500]}\n\n"
            "INSTRUCTIONS:\n"
            "Determine if these two memories CONTRADICT each other. "
            "A contradiction means they make opposing or mutually exclusive claims "
            "about the same topic — for example:\n"
            '- "Use approach A" vs "Never use approach A"\n'
            '- "Feature X is enabled" vs "Feature X is disabled"\n'
            '- "The default timeout is 30s" vs "The default timeout is 60s"\n\n'
            "The following are NOT contradictions:\n"
            "- One memory adds detail the other lacks (complementary)\n"
            "- Both say similar things in different words (overlapping)\n"
            "- They discuss different aspects of the same topic (orthogonal)\n"
            "- One is a more recent update that supersedes the other (evolution)\n\n"
            "Output ONLY one word: CONTRADICTION or COMPATIBLE\n"
        )
        try:
            raw = await self._ctx.invoke_llm(
                prompt,
                model="claude-haiku-4-20250514",
            )
            result = raw.strip().upper()
            is_contradiction = "CONTRADICTION" in result
            if is_contradiction:
                self._log.info("Contradiction detected between existing and new memory")
            return is_contradiction
        except Exception:
            self._log.debug("LLM contradiction detection unavailable, assuming compatible")
            return False

    # -----------------------------------------------------------------
    # Command handlers — Semantic Search
    # -----------------------------------------------------------------

    async def cmd_memory_search(self, args: dict) -> dict:
        """Unified memory search — vector search + KV keyword matching.

        Searches both the semantic vector index and KV entries (facts) in
        parallel, merging results.  KV entries whose key or value contains
        query keywords are surfaced alongside vector results so that
        structured facts (pip-audit results, project metadata, etc.) are
        discoverable even when vector similarity alone would miss them.
        """
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

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
                # Batch search — KV matching not applied to batch mode
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

                # Also search KV entries (facts) for keyword matches.
                # This catches structured data like pip-audit results that
                # may not have strong vector similarity to the query.
                kv_matches = await self._search_kv_keyword(project_id, query)

                formatted = [self._format_search_result(r) for r in results]

                return {
                    "success": True,
                    "project_id": project_id,
                    "query": query,
                    "count": len(formatted),
                    "results": formatted,
                    **({"kv_matches": kv_matches} if kv_matches else {}),
                }
        except Exception as e:
            self._log.error("memory_search failed: %s", e, exc_info=True)
            return {"error": f"Search failed: {e}"}

    async def _search_kv_keyword(
        self, project_id: str, query: str
    ) -> list[dict[str, str]]:
        """Search KV entries (facts) for keyword matches against a query.

        Tokenises the query into lowercase words and matches against both
        keys and values.  Returns entries where any query word appears in
        the key or value.
        """
        assert self._service is not None
        words = [w.lower() for w in query.split() if len(w) >= 3]
        if not words:
            return []

        matches: list[dict[str, str]] = []
        # Search across common namespaces
        for ns in ("project", "playbook-runner", "system", ""):
            try:
                entries = await self._service.kv_list(project_id, ns)
            except Exception:
                continue
            for entry in entries:
                key = str(entry.get("key", "")).lower()
                value = str(entry.get("value", "")).lower()
                combined = f"{key} {value}"
                if any(w in combined for w in words):
                    matches.append({
                        "namespace": ns or "(default)",
                        "key": entry.get("key", ""),
                        "value": str(entry.get("value", ""))[:500],
                    })
        return matches

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
            "retrieval_count": result.get("retrieval_count", 0),
            "last_retrieved": result.get("last_retrieved", 0),
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
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_list")

        scope = args.get("scope")
        topic = args.get("topic")
        tag = args.get("tag")
        entry_type = args.get("entry_type", "document")
        if entry_type == "all":
            entry_type = ""
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
            "last_retrieved": entry.get("last_retrieved", 0),
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
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
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
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
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
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
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

        project_id = self._resolve_project_id(args)
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

        project_id = self._resolve_project_id(args)
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

        project_id = self._resolve_project_id(args)
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
                "retrieval_count": r.get("retrieval_count", 0),
                "last_retrieved": r.get("last_retrieved", 0),
            }
            if full and original:
                entry["full"] = True
            if "_collection" in r:
                entry["collection"] = r["_collection"]
            if "_scope" in r:
                entry["scope"] = r["_scope"]
            if "_scope_id" in r:
                entry["scope_id"] = r["_scope_id"]

            # Parse wiki-links from content for agent navigation
            try:
                from src.wiki_links import parse_wiki_links

                raw_content = original if original else content
                links = parse_wiki_links(raw_content)
                if links:
                    entry["related_links"] = links
            except Exception:
                pass  # wiki_links module not available — skip gracefully

            formatted.append(entry)
        return formatted

    # -----------------------------------------------------------------
    # Command handlers — Temporal Facts
    # -----------------------------------------------------------------

    async def cmd_memory_fact_get(self, args: dict) -> dict:
        """Get current (or as-of) value of a temporal fact."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
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
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
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
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
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
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

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
    # Command handlers — Consolidation Tools (spec §10)
    # -----------------------------------------------------------------

    async def cmd_memory_delete(self, args: dict) -> dict:
        """Delete a memory entry by chunk_hash.

        Used during reflection consolidation to remove duplicate or stale
        memories after they have been merged into stronger entries.
        Deletes from both the Milvus index and the vault filesystem.
        """
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        chunk_hash = args.get("chunk_hash")
        if not chunk_hash:
            return {"error": "chunk_hash is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_delete")

        scope = args.get("scope")

        try:
            result = await self._service.delete_document(project_id, chunk_hash, scope=scope)
            return {"success": True, "action": "deleted", **result}
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            self._log.error("memory_delete failed: %s", e, exc_info=True)
            return {"error": f"Delete failed: {e}"}

    async def cmd_memory_follow(self, args: dict) -> dict:
        """Follow a wiki-link to read the linked vault file.

        Reads a vault file by its wiki-link path and returns the content
        plus any outgoing wiki-links for continued navigation through
        the knowledge graph.
        """
        link = (args.get("link") or "").strip()
        if not link:
            return {"success": False, "error": "link parameter is required"}

        from src.wiki_links import resolve_wiki_link, parse_wiki_links, strip_frontmatter

        vault_root = self._get_vault_root()
        if not vault_root:
            return {"success": False, "error": "vault root not available"}

        filepath = resolve_wiki_link(vault_root, link)
        if not filepath or not filepath.exists():
            return {"success": False, "error": f"Could not resolve link: {link}"}

        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            return {"success": False, "error": f"Failed to read file: {e}"}

        stripped = strip_frontmatter(content)
        outgoing = parse_wiki_links(content)

        result: dict = {
            "success": True,
            "path": str(filepath.relative_to(vault_root)),
            "content": stripped,
        }
        if outgoing:
            result["related_links"] = outgoing
        return result

    def _get_vault_root(self) -> str | None:
        """Get the vault root directory path."""
        if self._service and hasattr(self._service, "_data_dir"):
            from pathlib import Path

            data_dir = self._service._data_dir
            base = (
                Path(data_dir).expanduser()
                if data_dir
                else Path.home() / ".agent-queue"
            )
            return str(base / "vault")
        return None

    async def cmd_memory_update(self, args: dict) -> dict:
        """Update an existing memory entry's content, tags, or topic.

        Directly targets a known entry by chunk_hash — no dedup search.
        Useful for reflection consolidation: changing confidence tags,
        correcting outdated content, or refining topic classification.
        """
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        chunk_hash = args.get("chunk_hash")
        if not chunk_hash:
            return {"error": "chunk_hash is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_update")

        content = args.get("content")
        tags = args.get("tags")
        topic = args.get("topic")
        scope = args.get("scope")

        if content is None and tags is None and topic is None:
            return {"error": "At least one of content, tags, or topic must be provided"}

        try:
            result = await self._service.update_document(
                project_id,
                chunk_hash,
                content=content,
                tags=tags,
                topic=topic,
                scope=scope,
            )
            return {"success": True, "action": "updated", **result}
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            self._log.error("memory_update failed: %s", e, exc_info=True)
            return {"error": f"Update failed: {e}"}

    async def cmd_memory_promote_to_knowledge(self, args: dict) -> dict:
        """Promote an insight into the curated ``knowledge/`` subdirectory.

        Used by the nightly consolidation pass when an insight has proven
        stable enough (retrieved multiple times, durable content) to deserve
        a canonical, curated home.  The promoted entry:

        - Is written under ``vault/projects/{id}/memory/knowledge/`` (or
          the equivalent scope directory) instead of ``insights/``.
        - Carries the ``knowledge`` tag for tag-filtered retrieval.
        - Is re-indexed into Milvus with the new vault path.

        The source insight is deleted (both from Milvus and the vault) so
        consumers converge on the knowledge entry.

        Required args: ``chunk_hash``.  Optional args: ``content`` (to
        rewrite while promoting), ``topic``, ``tags`` (in addition to the
        auto-added ``knowledge`` tag), ``scope``.
        """
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        chunk_hash = args.get("chunk_hash")
        if not chunk_hash:
            return {"error": "chunk_hash is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_promote_to_knowledge")

        scope = args.get("scope")
        override_content = args.get("content")
        override_topic = args.get("topic")
        override_tags = args.get("tags")

        try:
            store = self._service._get_store(project_id, scope)
            import asyncio

            entry = await asyncio.to_thread(store.get, chunk_hash)
            if not entry:
                return {"error": f"Entry not found: {chunk_hash}"}

            source_content = entry.get("original") or entry.get("content", "")
            source_tags = self._decode_tags(entry.get("tags", "[]"))
            source_topic = entry.get("topic", "") or None
            source_task = entry.get("source_task") or None

            final_content = override_content or source_content
            final_topic = override_topic if override_topic is not None else source_topic
            base_tags = list(override_tags) if override_tags is not None else list(source_tags)
            # Normalize tags: drop insight-era markers, add knowledge marker.
            dropped = {"insight", "auto-extracted", "auto-generated"}
            base_tags = [t for t in base_tags if t not in dropped]
            if "knowledge" not in base_tags:
                base_tags.insert(0, "knowledge")
            if "curated" not in base_tags:
                base_tags.append("curated")

            # Write the knowledge entry via save_document (bypassing dedup —
            # promotion is an explicit caller decision, not a discovery).
            save_result = await self._service.save_document(
                project_id,
                final_content,
                tags=base_tags,
                topic=final_topic,
                source_task=source_task,
                source_playbook="memory-consolidation",
                scope=scope,
                subdir="knowledge",
            )

            # Delete the original insight entry.
            deleted_source = False
            try:
                await self._service.delete_document(project_id, chunk_hash, scope=scope)
                deleted_source = True
            except Exception as e:
                self._log.warning(
                    "Failed to delete source after knowledge promote: %s", e
                )

            return {
                "success": True,
                "action": "promoted_to_knowledge",
                "source_chunk_hash": chunk_hash,
                "source_deleted": deleted_source,
                "knowledge_chunk_hash": save_result.get("chunk_hash"),
                "knowledge_vault_path": save_result.get("vault_path"),
                "topic": final_topic or "",
                "tags": base_tags,
            }
        except Exception as e:
            self._log.error("memory_promote_to_knowledge failed: %s", e, exc_info=True)
            return {"error": f"Promote to knowledge failed: {e}"}

    async def cmd_memory_promote(self, args: dict) -> dict:
        """Promote a memory from a narrower scope to a broader scope.

        Copies the entry to the target scope and optionally deletes the
        source.  Used when reflection discovers an insight applies broadly
        across projects — e.g. promoting from project memory to agent-type
        memory.
        """
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        chunk_hash = args.get("chunk_hash")
        if not chunk_hash:
            return {"error": "chunk_hash is required"}
        target_scope = args.get("target_scope")
        if not target_scope:
            return {"error": "target_scope is required"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_promote")

        source_scope = args.get("source_scope")
        delete_source = args.get("delete_source", False)

        try:
            # Step 1: Read the source entry
            store = self._service._get_store(project_id, source_scope)
            import asyncio

            entry = await asyncio.to_thread(store.get, chunk_hash)
            if not entry:
                return {"error": f"Entry not found in source scope: {chunk_hash}"}

            source_content = entry.get("original") or entry.get("content", "")
            source_tags = self._decode_tags(entry.get("tags", "[]"))
            source_topic = entry.get("topic", "") or None

            # Step 2: Save to the target scope via the normal save flow
            # (this handles embedding, vault file creation, and dedup in target)
            result = await self._do_memory_save(
                project_id=project_id,
                content=source_content,
                tags=source_tags,
                topic=source_topic,
                source_task=entry.get("source_task"),
                scope=target_scope,
            )

            # Step 3: Optionally delete the source entry
            source_deleted = False
            if delete_source and result.get("success"):
                try:
                    await self._service.delete_document(project_id, chunk_hash, scope=source_scope)
                    source_deleted = True
                except Exception as e:
                    self._log.warning("Failed to delete source after promote: %s", e)

            return {
                "success": True,
                "action": "promoted",
                "source_scope": source_scope or f"project_{project_id}",
                "target_scope": target_scope,
                "source_deleted": source_deleted,
                "target_result": result,
            }
        except Exception as e:
            self._log.error("memory_promote failed: %s", e, exc_info=True)
            return {"error": f"Promote failed: {e}"}

    # -----------------------------------------------------------------
    # Command handlers — Index Management
    # -----------------------------------------------------------------

    async def cmd_memory_reindex(self, args: dict) -> dict:
        """Reindex vault filesystem into Milvus."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        # TODO: implement vault scanning and re-indexing via MemSearch
        # This requires a per-scope MemSearch instance that scans vault
        # directories and re-embeds changed content.
        return self._not_implemented("memory_reindex")

    async def cmd_memory_stats(self, args: dict) -> dict:
        """Get collection statistics."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_stats")

        scope = args.get("scope")

        try:
            stats = await self._service.stats(project_id, scope=scope)
            return {"success": True, **stats}
        except Exception as e:
            self._log.error("memory_stats failed: %s", e, exc_info=True)
            return {"error": f"Stats failed: {e}"}

    async def cmd_memory_health(self, args: dict) -> dict:
        """Get memory health metrics (spec §6 — Memory Health View)."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_health")

        scope = args.get("scope")
        stale_days = args.get("stale_days", 30)
        top_n = args.get("top_n", 10)

        try:
            result = await self._service.health(
                project_id,
                scope=scope,
                stale_days=int(stale_days),
                top_n=int(top_n),
            )
            if "error" in result:
                return result
            return {"success": True, **result}
        except Exception as e:
            self._log.error("memory_health failed: %s", e, exc_info=True)
            return {"error": f"Health check failed: {e}"}

    async def cmd_memory_stale(self, args: dict) -> dict:
        """Find stale memory documents — candidates for archival (spec §6, 6.5.3)."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        if not self._service or not self._service.available:
            return self._unavailable("memory_stale")

        scope = args.get("scope")
        stale_days = args.get("stale_days", 30)
        sort = args.get("sort", "staleness")
        offset = args.get("offset", 0)
        limit = args.get("limit", 50)

        try:
            result = await self._service.find_stale(
                project_id,
                scope=scope,
                stale_days=int(stale_days),
                sort=str(sort),
                offset=int(offset),
                limit=int(limit),
            )
            if "error" in result:
                return result
            return {"success": True, **result}
        except Exception as e:
            self._log.error("memory_stale failed: %s", e, exc_info=True)
            return {"error": f"Stale memory detection failed: {e}"}

    # -----------------------------------------------------------------
    # Command stubs — Profile / Factsheet / Knowledge
    # (remain stubs until v1 is deprecated and these tools transfer)
    # -----------------------------------------------------------------

    async def cmd_view_profile(self, args: dict) -> dict:
        """View the project profile."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        return self._not_implemented("view_profile")

    async def cmd_edit_project_profile(self, args: dict) -> dict:
        """Replace the project profile content."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        content = args.get("content")
        if not content:
            return {"error": "content is required"}
        return self._not_implemented("edit_project_profile")

    async def cmd_project_factsheet(self, args: dict) -> dict:
        """View or update the project factsheet."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        action = args.get("action", "view")
        if action not in ("view", "update"):
            return {"error": f"Unknown action '{action}'. Use 'view' or 'update'."}
        return self._not_implemented("project_factsheet")

    async def cmd_project_knowledge(self, args: dict) -> dict:
        """Read or list knowledge topics."""
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

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
        """Compact memory — consolidate insights into knowledge topic files.

        Scans the project's vault insights directory, groups entries by topic,
        and uses an LLM to consolidate each group into a structured knowledge
        file.  Deduplicates and removes redundant raw insights after merging.
        """
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        if not self._service or not self._service.available:
            return {"error": "Memory service not available"}

        config_svc = self._ctx.get_service("config")
        app_config = getattr(config_svc, "_config", None) if config_svc else None
        data_dir = config_svc.data_dir if config_svc else ""
        if not data_dir:
            return {"error": "data_dir not available"}

        # Scan vault insights directory for this project
        insights_dir = os.path.join(
            data_dir, "vault", "projects", project_id, "memory", "insights"
        )
        knowledge_dir = os.path.join(
            data_dir, "vault", "projects", project_id, "memory", "knowledge"
        )
        os.makedirs(knowledge_dir, exist_ok=True)

        if not os.path.isdir(insights_dir):
            return {"project_id": project_id, "status": "no insights directory", "consolidated": 0}

        # Read all insight files
        insights: list[dict] = []
        for fname in os.listdir(insights_dir):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(insights_dir, fname)
            try:
                with open(fpath, encoding="utf-8") as f:
                    raw = f.read()
                # Parse frontmatter
                topic = "general"
                tags: list[str] = []
                content = raw
                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        import yaml
                        try:
                            fm = yaml.safe_load(parts[1])
                            topic = fm.get("topic", "general") if fm else "general"
                            tags = fm.get("tags", []) if fm else []
                        except Exception:
                            pass
                        content = parts[2].strip()
                insights.append({
                    "file": fname,
                    "path": fpath,
                    "topic": topic,
                    "tags": tags,
                    "content": content,
                })
            except Exception:
                continue

        if not insights:
            return {"project_id": project_id, "status": "no insights to consolidate", "consolidated": 0}

        # Get knowledge topics from config
        topics = ("architecture", "api-and-endpoints", "deployment", "dependencies",
                  "gotchas", "conventions", "decisions")
        if app_config and hasattr(app_config, "memory"):
            topics = getattr(app_config.memory, "knowledge_topics", topics)

        # Group insights by topic
        by_topic: dict[str, list[dict]] = {}
        for insight in insights:
            t = insight["topic"].lower().replace(" ", "-")
            # Map to closest known topic, or use "general"
            matched = t if t in topics else "general"
            for known in topics:
                if known in t or t in known:
                    matched = known
                    break
            by_topic.setdefault(matched, []).append(insight)

        # Get LLM provider for consolidation
        try:
            from src.chat_providers import create_chat_provider
            memory_cfg = self._get_memory_config(config_svc)
            provider_name = memory_cfg.get("embedding_provider", "gemini")
            # Use the main chat provider, not the embedding provider
            provider = create_chat_provider(app_config.chat_provider)
        except Exception as e:
            return {"error": f"Failed to create LLM provider: {e}"}

        # Consolidate each topic group
        consolidated_count = 0
        removed_count = 0
        topic_results = []

        for topic_name, topic_insights in by_topic.items():
            if len(topic_insights) < 1:
                continue

            # Build consolidation prompt
            insight_texts = "\n\n---\n\n".join(
                f"**{ins['file']}** (topic: {ins['topic']})\n{ins['content']}"
                for ins in topic_insights
            )

            system = (
                "You are a knowledge consolidation system. Merge the following "
                "raw insights into a single, well-organized knowledge document. "
                "Remove duplicates, resolve contradictions (prefer newer info), "
                "and organize into clear sections. Each fact should be 1-2 "
                "sentences. Include source attribution where possible.\n\n"
                "Output clean markdown with no frontmatter."
            )
            user = (
                f"Topic: {topic_name}\n"
                f"Project: {project_id}\n\n"
                f"Raw insights to consolidate ({len(topic_insights)} entries):\n\n"
                f"{insight_texts}\n\n"
                f"Produce a consolidated knowledge document for the '{topic_name}' topic."
            )

            try:
                max_tokens = app_config.chat_provider.max_tokens
                resp = await provider.create_message(
                    messages=[{"role": "user", "content": user}],
                    system=system,
                    max_tokens=max_tokens,
                )
                consolidated_text = "\n".join(resp.text_parts).strip()
                if not consolidated_text:
                    continue
            except Exception as e:
                self._log.warning("Consolidation LLM call failed for topic %s: %s", topic_name, e)
                continue

            # Write knowledge topic file
            knowledge_path = os.path.join(knowledge_dir, f"{topic_name}.md")
            header = (
                f"---\n"
                f"topic: {topic_name}\n"
                f"project: {project_id}\n"
                f"consolidated_from: {len(topic_insights)} insights\n"
                f"last_consolidated: {__import__('datetime').date.today().isoformat()}\n"
                f"---\n\n"
            )
            with open(knowledge_path, "w", encoding="utf-8") as f:
                f.write(header + consolidated_text)

            consolidated_count += 1

            # Remove raw insights that were consolidated (if more than 1)
            if len(topic_insights) > 1:
                for ins in topic_insights:
                    try:
                        os.remove(ins["path"])
                        removed_count += 1
                    except Exception:
                        pass

            topic_results.append({
                "topic": topic_name,
                "insights_merged": len(topic_insights),
                "knowledge_file": f"knowledge/{topic_name}.md",
            })

        return {
            "project_id": project_id,
            "status": "completed",
            "topics_consolidated": consolidated_count,
            "insights_processed": len(insights),
            "insights_removed": removed_count,
            "topics": topic_results,
        }

    async def cmd_consolidate(self, args: dict) -> dict:
        """Run knowledge consolidation pipeline.

        Modes:
        - daily: Run compact_memory to merge insights into knowledge topics
        - deep: compact + prune stale entries + resolve contradictions
        - bootstrap: Generate initial knowledge from all existing memories
        """
        project_id = self._resolve_project_id(args)
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        mode = args.get("mode", "daily")
        if mode not in ("daily", "deep", "bootstrap"):
            return {"error": (f"Invalid mode '{mode}'. Use 'daily', 'deep', or 'bootstrap'.")}

        if mode == "daily" or mode == "bootstrap":
            # Daily and bootstrap both consolidate raw insights
            return await self.cmd_compact_memory({"project_id": project_id})

        if mode == "deep":
            # Deep: compact first, then prune stale
            compact_result = await self.cmd_compact_memory({"project_id": project_id})

            # Find and report stale entries
            stale_result = {}
            if self._service and self._service.available:
                try:
                    stale = await self._service.find_stale(
                        project_id=project_id,
                        days=30,
                        limit=20,
                    )
                    stale_result = {
                        "stale_entries": len(stale) if stale else 0,
                        "stale_candidates": [
                            {"content": s.get("content", "")[:100], "days_stale": s.get("days_since_retrieval", 0)}
                            for s in (stale or [])[:5]
                        ],
                    }
                except Exception:
                    stale_result = {"stale_entries": "check failed"}

            return {
                **compact_result,
                "mode": "deep",
                **stale_result,
            }

        return {"error": f"Mode '{mode}' not yet implemented"}
