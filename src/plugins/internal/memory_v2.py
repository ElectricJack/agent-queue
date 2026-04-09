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

Status: **skeleton** — tool definitions and command stubs only.  The memsearch
backend is not yet wired up.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.plugins.base import InternalPlugin, PluginContext

if TYPE_CHECKING:
    pass

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
        "memory_search_by_tag",
        "memory_kv_get",
        "memory_kv_set",
        "memory_kv_list",
        "memory_fact_get",
        "memory_fact_set",
        "memory_fact_history",
    }
)

TOOL_DEFINITIONS: list[dict] = [
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
            "Write a key-value entry to the scoped Milvus collection.  "
            "Creates or updates an entry in the given namespace.  The value "
            "is JSON-encoded and also synced to the vault facts file for "
            "human-readable access."
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
                    "description": "The key to set.",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "The value to store.  Stored as JSON-encoded string.  "
                        "For simple values pass the string directly."
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

    Status: **skeleton** — command handlers are stubs.
    """

    # Auto-discovered and loaded alongside v1 MemoryPlugin.  Both plugins
    # are active: v1 owns existing tool names, v2 owns new ones.
    _internal: bool = True

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._log = ctx.logger

        # MemoryServiceImpl wraps MemoryManager — provides both
        # single-project MemSearch instances and the shared
        # CollectionRouter for multi-scope search.
        try:
            self._memory_service = ctx.get_service("memory")
        except (ValueError, PermissionError):
            self._memory_service = None

        # CollectionRouter for cross-scope tag search.  Obtained lazily
        # from the MemoryManager when first needed (it creates the router
        # on demand with the configured Milvus URI and embedding dimension).
        self._router = None

        # -- Map command names to handlers --
        # Full command table — includes both v2-only and overlapping names.
        # During transition only V2_ONLY_TOOLS are registered; overlapping
        # names stay with v1 MemoryPlugin.
        all_commands: dict[str, object] = {
            # Semantic search
            "memory_search": self.cmd_memory_search,
            "memory_search_by_tag": self.cmd_memory_search_by_tag,
            # KV operations
            "memory_kv_get": self.cmd_memory_kv_get,
            "memory_kv_set": self.cmd_memory_kv_set,
            "memory_kv_list": self.cmd_memory_kv_list,
            # Temporal facts
            "memory_fact_get": self.cmd_memory_fact_get,
            "memory_fact_set": self.cmd_memory_fact_set,
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

        self._log.info(
            "MemoryV2Plugin initialized (skeleton, %d/%d v2-only commands registered)",
            registered,
            len(all_commands),
        )

    async def shutdown(self, ctx: PluginContext) -> None:
        # TODO: close memsearch client / Milvus connections
        pass

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _not_implemented(self, command: str) -> dict:
        """Return a standard 'not yet implemented' response."""
        return {
            "error": f"{command} is not yet implemented (memory v2 skeleton)",
            "plugin": "memory_v2",
        }

    @staticmethod
    def _format_result(r: dict) -> dict:
        """Format a raw search result for the tool response."""
        return {
            "content": r.get("content", ""),
            "source": r.get("source", ""),
            "heading": r.get("heading", ""),
            "score": r.get("score", 0.0),
            "weighted_score": r.get("weighted_score", 0.0),
            "scope": r.get("_scope", ""),
            "scope_id": r.get("_scope_id"),
            "weight": r.get("_weight", 0.0),
            "collection": r.get("_collection", ""),
            "chunk_hash": r.get("chunk_hash", ""),
            "topic": r.get("topic", ""),
            "topic_fallback": r.get("topic_fallback", False),
        }

    def _resolve_scope(self, project_id: str, scope: str | None = None) -> str:
        """Resolve scope string to a Milvus collection name.

        Default scope is ``aq_project_{project_id}``.
        """
        if scope is None:
            return f"aq_project_{project_id}"
        if scope == "system":
            return "aq_system"
        if scope == "orchestrator":
            return "aq_orchestrator"
        if scope.startswith("agenttype_"):
            agent_type = scope.removeprefix("agenttype_")
            return f"aq_agenttype_{agent_type}"
        if scope.startswith("project_"):
            pid = scope.removeprefix("project_")
            return f"aq_project_{pid}"
        # Assume it's already a full collection name
        return scope

    # -----------------------------------------------------------------
    # Command stubs — Semantic Search
    # -----------------------------------------------------------------

    async def cmd_memory_search(self, args: dict) -> dict:
        """Semantic vector search across scoped collections.

        Uses :meth:`MemoryManager.scoped_search` for multi-scope weighted
        merge per spec §6.  Searches project, agent-type, and system
        collections in parallel and merges results weighted by specificity
        (project=1.0, agent-type=0.7, system=0.4).
        """
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        query = args.get("query")
        queries = args.get("queries")
        if not query and not queries:
            return {"error": "Either 'query' or 'queries' is required"}

        topic = args.get("topic")
        top_k = args.get("top_k", 10)
        # The scope field can hint at an agent_type for cross-scope search.
        # When scope starts with "agenttype_", extract the agent type ID.
        scope = args.get("scope")
        agent_type = None
        if scope and scope.startswith("agenttype_"):
            agent_type = scope.removeprefix("agenttype_")

        svc = self._memory_service
        if svc is None:
            return self._not_implemented("memory_search")

        try:
            if queries:
                # Batch multi-scope search
                results_map = await svc.scoped_batch_search(
                    queries,
                    project_id=project_id,
                    agent_type=agent_type,
                    topic=topic,
                    top_k=top_k,
                )
                return {
                    "success": True,
                    "queries": {
                        q: [self._format_result(r) for r in hits] for q, hits in results_map.items()
                    },
                }
            else:
                # Single multi-scope search
                results = await svc.scoped_search(
                    query,
                    project_id=project_id,
                    agent_type=agent_type,
                    topic=topic,
                    top_k=top_k,
                )
                return {
                    "success": True,
                    "query": query,
                    "count": len(results),
                    "results": [self._format_result(r) for r in results],
                }
        except Exception as e:
            self._log.error("memory_search failed: %s", e, exc_info=True)
            return {"error": f"Search failed: {e}"}

    async def _get_router(self) -> object | None:
        """Lazily obtain the CollectionRouter from MemoryManager.

        The MemoryServiceImpl wraps MemoryManager; the router is
        accessible via ``_mm._get_router()`` on the underlying manager.
        """
        if self._router is not None:
            return self._router
        svc = self._memory_service
        if svc is None or not hasattr(svc, "_mm") or svc._mm is None:
            return None
        try:
            self._router = await svc._mm._get_router()
        except Exception:
            return None
        return self._router

    async def cmd_memory_search_by_tag(self, args: dict) -> dict:
        """Cross-scope search by tag across all collections.

        Uses CollectionRouter.search_by_tag_async to query ALL aq_*
        collections in the Milvus instance filtered by tag.  Per spec §7.3.
        """
        tag = args.get("tag")
        if not tag:
            return {"error": "tag is required"}

        entry_type = args.get("entry_type")
        topic = args.get("topic")
        limit = args.get("limit", 10)

        router = await self._get_router()
        if not router:
            return self._not_implemented("memory_search_by_tag")

        try:
            results = await router.search_by_tag_async(
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
                        "tags": r.get("tags", "[]"),
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
    # Command stubs — KV Operations
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

        _scope = self._resolve_scope(project_id)

        # TODO: collection.query(filter='entry_type == "kv" AND ...')
        return self._not_implemented("memory_kv_get")

    async def cmd_memory_kv_set(self, args: dict) -> dict:
        """Write a KV entry to the scoped collection and vault."""
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

        _scope = self._resolve_scope(project_id)

        # TODO: upsert KV entry in Milvus + sync vault facts.md
        return self._not_implemented("memory_kv_set")

    async def cmd_memory_kv_list(self, args: dict) -> dict:
        """List all KV entries in a namespace."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        namespace = args.get("namespace")
        if not namespace:
            return {"error": "namespace is required"}

        _scope = self._resolve_scope(project_id)

        # TODO: collection.query(filter='entry_type == "kv" AND kv_namespace == ...')
        return self._not_implemented("memory_kv_list")

    # -----------------------------------------------------------------
    # Command stubs — Temporal Facts
    # -----------------------------------------------------------------

    async def cmd_memory_fact_get(self, args: dict) -> dict:
        """Get current (or as-of) value of a temporal fact."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}

        _scope = self._resolve_scope(project_id)
        _as_of = args.get("as_of")  # None means current time

        # TODO: temporal query with valid_from/valid_to window check
        return self._not_implemented("memory_fact_get")

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

        _scope = self._resolve_scope(project_id)

        # TODO:
        # 1. Find current entry (valid_to == 0), set valid_to = now
        # 2. Insert new entry with valid_from = now, valid_to = 0
        # 3. Update vault facts.md
        return self._not_implemented("memory_fact_set")

    async def cmd_memory_fact_history(self, args: dict) -> dict:
        """Retrieve full history of a temporal fact."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}

        _scope = self._resolve_scope(project_id)

        # TODO: query all temporal entries for this key, ordered by valid_from
        return self._not_implemented("memory_fact_history")

    # -----------------------------------------------------------------
    # Command stubs — Index Management
    # -----------------------------------------------------------------

    async def cmd_memory_reindex(self, args: dict) -> dict:
        """Reindex vault filesystem into Milvus."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        _scope = self._resolve_scope(project_id, args.get("scope"))
        _full = args.get("full", False)

        # TODO: scan vault, embed documents, upsert into Milvus
        return self._not_implemented("memory_reindex")

    async def cmd_memory_stats(self, args: dict) -> dict:
        """Get collection statistics."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        _scope = self._resolve_scope(project_id, args.get("scope"))

        # TODO: collection.num_entities, entry type counts, storage info
        return self._not_implemented("memory_stats")

    # -----------------------------------------------------------------
    # Command stubs — Profile / Factsheet / Knowledge
    # -----------------------------------------------------------------

    async def cmd_view_profile(self, args: dict) -> dict:
        """View the project profile."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        # TODO: read profile document from vault / Milvus
        return self._not_implemented("view_profile")

    async def cmd_edit_project_profile(self, args: dict) -> dict:
        """Replace the project profile content."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        content = args.get("content")
        if not content:
            return {"error": "content is required"}

        # TODO: update vault file + Milvus document entry
        return self._not_implemented("edit_project_profile")

    async def cmd_project_factsheet(self, args: dict) -> dict:
        """View or update the project factsheet."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        action = args.get("action", "view")
        if action not in ("view", "update"):
            return {"error": f"Unknown action '{action}'. Use 'view' or 'update'."}

        # TODO: read/write factsheet from vault + sync KV entries
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

        # TODO: read topic documents from vault / Milvus
        return self._not_implemented("project_knowledge")

    # -----------------------------------------------------------------
    # Command stubs — Compaction / Consolidation
    # -----------------------------------------------------------------

    async def cmd_compact_memory(self, args: dict) -> dict:
        """Compact memory — summarize old entries, remove stale ones."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        # TODO: age-based compaction of document entries
        return self._not_implemented("compact_memory")

    async def cmd_consolidate(self, args: dict) -> dict:
        """Run knowledge consolidation."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        mode = args.get("mode", "daily")
        if mode not in ("daily", "deep", "bootstrap"):
            return {"error": f"Invalid mode '{mode}'. Use 'daily', 'deep', or 'bootstrap'."}

        # TODO: delegate to consolidation engine
        return self._not_implemented("consolidate")
