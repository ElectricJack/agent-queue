"""Internal plugin: memory operations (search, stats, reindex, compact, profile CRUD).

Extracted from ``CommandHandler._cmd_memory_search`` etc.  These
commands delegate to the MemoryManager on the orchestrator.
"""

from __future__ import annotations

from src.plugins.base import InternalPlugin, PluginContext


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOL_CATEGORY = "memory"

TOOL_DEFINITIONS = [
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
                "project_id": {
                    "type": "string",
                    "description": "Project ID to search memory for",
                },
                "query": {
                    "type": "string",
                    "description": "Single semantic search query",
                },
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Multiple search queries to run concurrently. Results are "
                        "returned grouped by query. Use instead of 'query' when "
                        "looking up multiple topics at once."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Results per query (default 10)",
                    "default": 10,
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "memory_stats",
        "description": (
            "Get memory index statistics for a project. Shows whether memory "
            "is enabled, the collection name, embedding provider, and "
            "auto-recall/auto-remember settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to get memory stats for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "memory_reindex",
        "description": (
            "Force a full reindex of a project's memory. Re-scans all markdown "
            "files in memory/ and notes/ directories, re-embeds changed content, "
            "and updates the vector index. Use when memory seems stale or after "
            "bulk file changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to reindex memory for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "view_profile",
        "description": (
            "View the project profile -- a synthesized understanding of the project's "
            "architecture, conventions, key decisions, common patterns, and pitfalls. "
            "The profile evolves automatically as tasks complete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to view profile for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "edit_project_profile",
        "description": (
            "Replace the project memory profile with new content. Use this to "
            "manually correct or enhance the project's synthesized understanding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to edit profile for",
                },
                "content": {
                    "type": "string",
                    "description": "New profile content (markdown)",
                },
            },
            "required": ["project_id", "content"],
        },
    },
    {
        "name": "regenerate_profile",
        "description": (
            "Force LLM regeneration of the project profile from the full task "
            "history. Use this when the profile has drifted or you want a fresh "
            "synthesis of everything the project has learned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to regenerate profile for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "write_memory",
        "description": (
            "Write a key-value entry to project memory. Use this for persistent "
            "data like timestamps, counters, status values, or any structured state "
            "that should survive across tasks and hook executions. The entry is "
            "indexed for semantic search via memory_search. Use read_memory to "
            "retrieve it later by key."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to store memory for",
                },
                "key": {
                    "type": "string",
                    "description": "Memory key (used as filename, e.g. 'last_sync_timestamp')",
                },
                "content": {
                    "type": "string",
                    "description": "Content to store (markdown or plain text)",
                },
            },
            "required": ["project_id", "key", "content"],
        },
    },
    {
        "name": "read_memory",
        "description": (
            "Read a specific memory entry by key. Returns the content stored "
            "via write_memory. For broad lookups use memory_search instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to read memory from",
                },
                "key": {
                    "type": "string",
                    "description": "Memory key (e.g. 'last_sync_timestamp')",
                },
            },
            "required": ["project_id", "key"],
        },
    },
    {
        "name": "compact_memory",
        "description": (
            "Trigger memory compaction for a project. Groups task memories "
            "by age: recent (kept as-is), medium (LLM-summarized into weekly "
            "digests), old (deleted after digesting). Returns stats on tasks "
            "inspected, digests created, and files removed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to compact memory for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "consolidate",
        "description": (
            "Run the daily knowledge consolidation process for a project. "
            "Reads staged facts from completed tasks, deduplicates them, "
            "and merges updates into the project factsheet and knowledge "
            "base topic files. Processed staging files are moved to "
            "staging/processed/ to prevent re-processing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to run consolidation for",
                },
            },
            "required": ["project_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# CLI formatters
# ---------------------------------------------------------------------------


def _fmt_search_results(results: list[dict]) -> list:
    """Format a list of search result dicts into Rich panels."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    panels = []
    for r in results:
        score = r.get("score", 0)
        content = r.get("content", r.get("text", ""))
        source = r.get("source", r.get("type", ""))
        snippet = content[:300] + ("..." if len(content) > 300 else "")
        meta = Text()
        meta.append(f"Score: {score:.2f}", style="dim")
        if source:
            meta.append(f"  Source: {source}", style="cyan")
        panels.append(
            Panel(
                Group(Text(snippet, style="white"), meta),
                border_style="bright_black",
                padding=(0, 1),
            )
        )
    return panels


def _fmt_memory_search(data: dict):
    from rich.console import Group
    from rich.text import Text

    # --- Multi-query mode ---
    results_by_query = data.get("results_by_query")
    if results_by_query is not None:
        total = data.get("total_count", 0)
        header = Text()
        header.append("  Memory batch search: ", style="dim")
        header.append(f"{len(results_by_query)} queries", style="bold")
        header.append(f"  ({total} total result(s))", style="dim")
        sections: list = [header]
        for q, hits in results_by_query.items():
            q_header = Text()
            q_header.append(f'\n  Query: "{q}"', style="bold yellow")
            q_header.append(f"  ({len(hits)} result(s))", style="dim")
            sections.append(q_header)
            if hits:
                sections.extend(_fmt_search_results(hits))
            else:
                sections.append(Text("    No results found.", style="dim"))
        return Group(*sections)

    # --- Single-query mode ---
    results = data.get("results", [])
    query = data.get("query", "")
    count = data.get("count", len(results))
    header = Text()
    header.append("  Memory search: ", style="dim")
    header.append(f'"{query}"', style="bold")
    header.append(f"  ({count} result(s))", style="dim")
    if not results:
        return Group(header, Text("  No results found.", style="dim"))
    return Group(header, *_fmt_search_results(results))


def _fmt_memory_stats(data: dict):
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    lines = []
    for key in (
        "enabled",
        "provider",
        "collection",
        "document_count",
        "embedding_model",
        "chunk_size",
        "notes_inform_profile",
    ):
        val = data.get(key)
        if val is not None:
            line = Text()
            line.append(f"  {key}: ", style="dim")
            line.append(str(val), style="white")
            lines.append(line)
    return Panel(
        Group(*lines) if lines else Text("  No stats available.", style="dim"),
        title="[bold bright_white]Memory Stats[/]",
        border_style="bright_cyan",
        padding=(0, 1),
    )


def _fmt_confirmation(data: dict):
    from src.cli.formatters import format_confirmation

    return format_confirmation(data)


def _fmt_text_content(data: dict):
    from src.cli.formatters import format_text_content

    return format_text_content(data)


def _build_cli_formatters():
    """Return CLI formatter specs for memory commands."""
    from src.cli.formatter_registry import FormatterSpec

    return {
        "memory_search": FormatterSpec(render=_fmt_memory_search, extract=None, many=False),
        "memory_stats": FormatterSpec(render=_fmt_memory_stats, extract=None, many=False),
        "write_memory": FormatterSpec(render=_fmt_confirmation, extract=None, many=False),
        "read_memory": FormatterSpec(render=_fmt_text_content, extract=None, many=False),
        "compact_memory": FormatterSpec(render=_fmt_confirmation, extract=None, many=False),
        "consolidate": FormatterSpec(render=_fmt_confirmation, extract=None, many=False),
        "memory_reindex": FormatterSpec(render=_fmt_confirmation, extract=None, many=False),
        "edit_project_profile": FormatterSpec(render=_fmt_confirmation, extract=None, many=False),
        "regenerate_profile": FormatterSpec(render=_fmt_confirmation, extract=None, many=False),
        "view_profile": FormatterSpec(render=_fmt_text_content, extract=None, many=False),
    }


CLI_FORMATTERS = _build_cli_formatters


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class MemoryPlugin(InternalPlugin):
    """Memory operations: search, stats, reindex, compact, profile CRUD."""

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._db = ctx.get_service("db")
        self._mem = ctx.get_service("memory")

        ctx.register_command("memory_search", self.cmd_memory_search)
        ctx.register_command("memory_stats", self.cmd_memory_stats)
        ctx.register_command("memory_reindex", self.cmd_memory_reindex)
        ctx.register_command("write_memory", self.cmd_write_memory)
        ctx.register_command("read_memory", self.cmd_read_memory)
        ctx.register_command("view_profile", self.cmd_view_profile)
        ctx.register_command("edit_project_profile", self.cmd_edit_project_profile)
        ctx.register_command("regenerate_profile", self.cmd_regenerate_profile)
        ctx.register_command("compact_memory", self.cmd_compact_memory)
        ctx.register_command("consolidate", self.cmd_consolidate)

        for tool_def in TOOL_DEFINITIONS:
            ctx.register_tool(dict(tool_def), category="memory")

    async def shutdown(self, ctx: PluginContext) -> None:
        pass

    # --- Helpers ---

    async def _require_workspace(self, project_id: str) -> tuple[str | None, dict | None]:
        """Validate project exists and has a workspace. Returns (workspace, error)."""
        project = await self._db.get_project(project_id)
        if not project:
            return None, {"error": f"Project '{project_id}' not found"}
        workspace = await self._db.get_project_workspace_path(project_id)
        if not workspace:
            return None, {
                "error": f"Project '{project_id}' has no workspaces. Use /add-workspace to create one."
            }
        return workspace, None

    # --- Commands ---

    async def cmd_memory_search(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        query = args.get("query")
        queries = args.get("queries")
        top_k = args.get("top_k", 10)

        if not query and not queries:
            return {"error": "Either 'query' (string) or 'queries' (array) is required"}

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        # --- Multi-query mode ---
        if queries:
            try:
                raw = await self._mem.batch_search(
                    project_id, workspace, queries, top_k=top_k
                )
            except Exception as e:
                return {"error": f"Memory batch search failed: {e}"}

            results_by_query: dict[str, list[dict]] = {}
            total = 0
            for q, hits in raw.items():
                formatted = []
                for i, mem in enumerate(hits, 1):
                    formatted.append(
                        {
                            "rank": i,
                            "source": mem.get("source", "unknown"),
                            "heading": mem.get("heading", ""),
                            "content": mem.get("content", ""),
                            "score": round(mem.get("score", 0), 4),
                        }
                    )
                results_by_query[q] = formatted
                total += len(formatted)

            return {
                "project_id": project_id,
                "queries": queries,
                "top_k": top_k,
                "results_by_query": results_by_query,
                "total_count": total,
            }

        # --- Single-query mode (backward compatible) ---
        try:
            results = await self._mem.search(project_id, workspace, query, top_k=top_k)
        except Exception as e:
            return {"error": f"Memory search failed: {e}"}

        formatted = []
        for i, mem in enumerate(results, 1):
            formatted.append(
                {
                    "rank": i,
                    "source": mem.get("source", "unknown"),
                    "heading": mem.get("heading", ""),
                    "content": mem.get("content", ""),
                    "score": round(mem.get("score", 0), 4),
                }
            )

        return {
            "project_id": project_id,
            "query": query,
            "top_k": top_k,
            "count": len(formatted),
            "results": formatted,
        }

    async def cmd_memory_stats(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        try:
            stats = await self._mem.stats(project_id, workspace)
        except Exception as e:
            return {"error": f"Failed to retrieve memory stats: {e}"}

        return {"project_id": project_id, **stats}

    async def cmd_memory_reindex(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        try:
            chunks_indexed = await self._mem.reindex(project_id, workspace)
        except Exception as e:
            return {"error": f"Memory reindex failed: {e}"}

        return {
            "project_id": project_id,
            "status": "reindex_complete",
            "chunks_indexed": chunks_indexed,
        }

    async def cmd_write_memory(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}
        content = args.get("content")
        if not content:
            return {"error": "content is required"}

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        try:
            path = await self._mem.write_memory(project_id, workspace, key, content)
        except Exception as e:
            return {"error": f"Failed to write memory: {e}"}

        if not path:
            return {"error": "Memory write failed"}

        return {
            "project_id": project_id,
            "key": key,
            "status": "memory_written",
            "path": path,
        }

    async def cmd_read_memory(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        key = args.get("key")
        if not key:
            return {"error": "key is required"}

        try:
            content = await self._mem.read_memory(project_id, key)
        except Exception as e:
            return {"error": f"Failed to read memory: {e}"}

        if content is None:
            return {
                "project_id": project_id,
                "key": key,
                "content": None,
                "message": f"No memory entry found for key '{key}'",
            }

        return {
            "project_id": project_id,
            "key": key,
            "content": content,
        }

    async def cmd_view_profile(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        try:
            profile = await self._mem.get_profile(project_id)
        except Exception as e:
            return {"error": f"Failed to read profile: {e}"}

        if not profile:
            return {
                "project_id": project_id,
                "profile": None,
                "message": "No project profile exists yet. It will be created after the first completed task.",
            }

        return {"project_id": project_id, "profile": profile}

    async def cmd_edit_project_profile(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        content = args.get("content")
        if not content:
            return {"error": "content is required"}

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        try:
            path = await self._mem.update_profile(project_id, content, workspace)
        except Exception as e:
            return {"error": f"Failed to update profile: {e}"}

        if not path:
            return {"error": "Profile update failed (profiles may be disabled)"}

        return {
            "project_id": project_id,
            "status": "profile_updated",
            "path": path,
        }

    async def cmd_regenerate_profile(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        try:
            new_profile = await self._mem.regenerate_profile(project_id, workspace)
        except Exception as e:
            return {"error": f"Profile regeneration failed: {e}"}

        if not new_profile:
            return {
                "project_id": project_id,
                "status": "no_change",
                "message": "Could not regenerate profile. The project may have no task history, or profiles may be disabled.",
            }

        return {
            "project_id": project_id,
            "status": "profile_regenerated",
            "profile": new_profile,
        }

    async def cmd_compact_memory(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        try:
            result = await self._mem.compact(project_id, workspace)
        except Exception as e:
            return {"error": f"Memory compaction failed: {e}"}

        return {"project_id": project_id, **result}

    async def cmd_consolidate(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        try:
            result = await self._mem.run_daily_consolidation(project_id, workspace)
        except Exception as e:
            return {"error": f"Memory consolidation failed: {e}"}

        return {"project_id": project_id, **result}
