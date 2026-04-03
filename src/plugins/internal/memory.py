"""Internal plugin: memory operations (search, stats, reindex, compact, profile CRUD).

Extracted from ``CommandHandler._cmd_memory_search`` etc.  These
commands delegate to the MemoryManager on the orchestrator.
"""

from __future__ import annotations

import logging

from src.plugins.base import InternalPlugin, PluginContext

logger = logging.getLogger(__name__)


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
            "Use this when the user asks about past work, wants to find related "
            "context, or says 'search memory', 'what do we know about', etc."
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
                    "description": "Semantic search query",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default 10)",
                    "default": 10,
                },
            },
            "required": ["project_id", "query"],
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
]


# ---------------------------------------------------------------------------
# CLI formatters
# ---------------------------------------------------------------------------


def _fmt_memory_search(data: dict):
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text
    results = data.get("results", [])
    query = data.get("query", "")
    count = data.get("count", len(results))
    header = Text()
    header.append("  Memory search: ", style="dim")
    header.append(f'"{query}"', style="bold")
    header.append(f"  ({count} result(s))", style="dim")
    if not results:
        return Group(header, Text("  No results found.", style="dim"))
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
        panels.append(Panel(Group(Text(snippet, style="white"), meta), border_style="bright_black", padding=(0, 1)))
    return Group(header, *panels)


def _fmt_memory_stats(data: dict):
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text
    lines = []
    for key in ("enabled", "provider", "collection", "document_count",
                "embedding_model", "chunk_size", "notes_inform_profile"):
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
        "compact_memory": FormatterSpec(render=_fmt_confirmation, extract=None, many=False),
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
        ctx.register_command("view_profile", self.cmd_view_profile)
        ctx.register_command("edit_project_profile", self.cmd_edit_project_profile)
        ctx.register_command("regenerate_profile", self.cmd_regenerate_profile)
        ctx.register_command("compact_memory", self.cmd_compact_memory)

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
            return None, {"error": f"Project '{project_id}' has no workspaces. Use /add-workspace to create one."}
        return workspace, None

    # --- Commands ---

    async def cmd_memory_search(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        query = args.get("query")
        if not query:
            return {"error": "query is required"}
        top_k = args.get("top_k", 10)

        workspace, err = await self._require_workspace(project_id)
        if err:
            return err

        try:
            results = await self._mem.search(project_id, workspace, query, top_k=top_k)
        except Exception as e:
            return {"error": f"Memory search failed: {e}"}

        formatted = []
        for i, mem in enumerate(results, 1):
            formatted.append({
                "rank": i,
                "source": mem.get("source", "unknown"),
                "heading": mem.get("heading", ""),
                "content": mem.get("content", ""),
                "score": round(mem.get("score", 0), 4),
            })

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
