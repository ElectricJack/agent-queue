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

    The plugin delegates all operations to :class:`MemoryV2Service` which
    wraps the memsearch fork's :class:`CollectionRouter` and
    :class:`MilvusStore`.
    """

    # Auto-discovered and loaded alongside v1 MemoryPlugin.  Both plugins
    # are active: v1 owns existing tool names, v2 owns new ones.
    _internal: bool = True

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

        if not self._service or not self._service.available:
            return self._unavailable("memory_kv_set")

        try:
            entry = await self._service.kv_set(project_id, namespace, key, value)
            return {
                "success": True,
                "project_id": project_id,
                **self._format_kv_entry(entry),
            }
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
