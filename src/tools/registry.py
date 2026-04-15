"""Tiered tool registry for on-demand tool loading.

Splits the monolithic TOOLS list into core tools (always loaded) and
named categories (loaded on demand via ``browse_tools``/``load_tools``).
This keeps the LLM's initial context window small — only ~10 core tools
are loaded at conversation start.  When the LLM needs specialised tools
(git, hooks, memory, etc.) it calls ``browse_tools`` to discover categories,
then ``load_tools`` to inject that category's definitions into the active
tool set for subsequent turns.

The registry only manages tool *definitions* (JSON Schema dicts that
describe each tool's name, description, and input schema).  Execution
still flows through ``CommandHandler.execute()`` regardless of whether
a tool is "loaded" in the LLM's context — the loading mechanism is
purely an attention/context optimisation.

Key components:

- ``CATEGORIES`` — named groups (git, project, agent, hooks, memory,
  files, system) with human-readable descriptions.
- ``_TOOL_CATEGORIES`` — mapping of tool name → category.  Tools not
  listed here are "core" (always loaded).
- ``_ALL_TOOL_DEFINITIONS`` — the master list of ~80 tool JSON Schema
  dicts.  Each entry corresponds to a ``CommandHandler._cmd_*`` method.
- ``ToolRegistry`` — the public API: ``get_core_tools()``,
  ``get_category_tools(cat)``, ``get_all_tools()``.

See ``specs/supervisor.md`` for the tool-use loop that drives loading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.tools.definitions import (
    _ALL_TOOL_DEFINITIONS,
    _CLI_CATEGORY_OVERRIDES,
    _TOOL_CATEGORIES,
)


@dataclass(frozen=True)
class CategoryMeta:
    """Metadata for a tool category."""

    name: str
    description: str


# Category definitions with human-readable descriptions
CATEGORIES: dict[str, CategoryMeta] = {
    "git": CategoryMeta(
        name="git",
        description=(
            "Branch, commit, push, PR, merge, and remote URL operations for project repositories"
        ),
    ),
    "project": CategoryMeta(
        name="project",
        description=(
            "Project CRUD, workspace management, channel configuration, "
            "project metadata (repo URL, GitHub URL, workspace path)"
        ),
    ),
    "agent": CategoryMeta(
        name="agent",
        description=("Agent management, agent profiles, profile import/export"),
    ),
    "rules": CategoryMeta(
        name="rules",
        description=(
            "DEPRECATED — rules have been replaced by playbooks. "
            "Use the playbooks category instead."
        ),
    ),
    "memory": CategoryMeta(
        name="memory",
        description=("Semantic memory — search, project profiles, compaction, reindexing"),
    ),
    "notes": CategoryMeta(
        name="notes",
        description=("Project notes — list, read, write, append, delete, promote notes to specs"),
    ),
    "files": CategoryMeta(
        name="files",
        description=(
            "Filesystem tools — read, write, edit files, glob pattern "
            "matching, grep/ripgrep-style content search"
        ),
    ),
    "task": CategoryMeta(
        name="task",
        description=("Task lifecycle, approval, dependencies, archives, and results"),
    ),
    "playbook": CategoryMeta(
        name="playbook",
        description=("Playbook compilation, run management, human-in-the-loop review and resume"),
    ),
    "plugin": CategoryMeta(
        name="plugin",
        description=("Plugin installation, configuration, and lifecycle management"),
    ),
    "system": CategoryMeta(
        name="system",
        description=(
            "Token usage, log access, event history, config reload, diagnostics, "
            "prompt management, daemon control"
        ),
    ),
}


class ToolRegistry:
    """Registry that categorizes tools into core and on-demand categories.

    Initialised with a list of tool definition dicts (JSON Schema format).
    Each tool is either "core" (always loaded) or belongs to a named
    category that can be loaded on demand via ``load_tools``.

    Usage::

        registry = ToolRegistry()
        core = registry.get_core_tools()          # always-on tools
        git  = registry.get_category_tools("git")  # on-demand category

    The registry is stateless — it doesn't track which categories are
    currently "loaded" in a conversation.  That state lives in the
    Supervisor's ``active_tools`` dict.

    Attributes:
        _all_tools: Mapping of tool name → tool definition dict.
    """

    def __init__(self, tools: list[dict] | None = None):
        """Initialize with tool definitions.

        Args:
            tools: List of tool definition dicts. If None, uses the
                   built-in _ALL_TOOL_DEFINITIONS.
        """
        if tools is None:
            tools = list(_ALL_TOOL_DEFINITIONS)
        self._all_tools: dict[str, dict] = {t["name"]: t for t in tools}
        self._plugin_registry = None
        # Add new tools that don't exist in the legacy TOOLS list
        self._ensure_navigation_tools()

    def set_plugin_registry(self, plugin_registry) -> None:
        """Set the plugin registry for dynamic tool merging."""
        self._plugin_registry = plugin_registry

    def _ensure_navigation_tools(self) -> None:
        """Add browse_tools, load_tools, send_message, reply_to_user stubs if absent.

        These tools are synthesised at init time rather than being defined in
        ``_ALL_TOOL_DEFINITIONS`` because they need special handling in the
        Supervisor's tool-use loop (e.g. ``load_tools`` expands the active set,
        ``reply_to_user`` terminates the loop).
        """
        if "browse_tools" not in self._all_tools:
            self._all_tools["browse_tools"] = {
                "name": "browse_tools",
                "description": (
                    "List available tool categories. Returns category "
                    "names, descriptions, and tool counts. Use this to "
                    "discover what tools are available, then call "
                    "load_tools to load a category."
                ),
                "input_schema": {"type": "object", "properties": {}},
            }
        if "load_tools" not in self._all_tools:
            self._all_tools["load_tools"] = {
                "name": "load_tools",
                "description": (
                    "Load all tools from a specific category, making "
                    "them available for the remainder of this "
                    "interaction. Call browse_tools first to see "
                    "available categories."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": ("Category name to load (e.g. 'git', 'project')"),
                        },
                    },
                    "required": ["category"],
                },
            }
        if "send_message" not in self._all_tools:
            self._all_tools["send_message"] = {
                "name": "send_message",
                "description": (
                    "Post a message to a Discord channel. Use this to "
                    "notify users, post updates, or communicate outside "
                    "the current conversation thread."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel_id": {
                            "type": "string",
                            "description": ("Discord channel ID to post to"),
                        },
                        "content": {
                            "type": "string",
                            "description": "Message content to post",
                        },
                    },
                    "required": ["channel_id", "content"],
                },
            }
        # reply_to_user — mandatory response delivery tool
        if "reply_to_user" not in self._all_tools:
            self._all_tools["reply_to_user"] = {
                "name": "reply_to_user",
                "description": (
                    "Deliver your final response to the user. You MUST call "
                    "this tool when you are done processing a request. Do not "
                    "stop calling tools until you have gathered enough "
                    "information to provide a complete answer, then call this "
                    "tool with your response. The message should directly "
                    "address the user's request — not just list what tools "
                    "you called."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": (
                                "The complete response to send to the user. "
                                "Must directly answer their question or "
                                "confirm the action taken with relevant "
                                "details."
                            ),
                        },
                    },
                    "required": ["message"],
                },
            }
        # Rule tools removed — rules have been replaced by playbooks
        # (playbooks spec §13 Phase 3). Deprecated stub definitions remain
        # in _ALL_TOOL_DEFINITIONS for backward compatibility.

    # ------------------------------------------------------------------
    # Schema compression for small-context LLMs
    # ------------------------------------------------------------------

    @staticmethod
    def compress_tool_schema(tool: dict) -> dict:
        """Return a minimal version of a tool definition for small-context LLMs.

        Strips verbose descriptions down to short phrases and removes
        parameter descriptions where the name is self-explanatory.
        Keeps: name, compressed description, input_schema with types/required/enums.
        """
        compressed = {"name": tool["name"]}

        # Compress description to first sentence, max ~80 chars
        desc = tool.get("description", "")
        # Take first sentence
        for sep in [". ", ".\n", ".  "]:
            if sep in desc:
                desc = desc[: desc.index(sep) + 1]
                break
        # Truncate if still long
        if len(desc) > 80:
            desc = desc[:77] + "..."
        compressed["description"] = desc

        # Compress input_schema: keep types, required, enums; drop descriptions
        schema = tool.get("input_schema", {})
        if not schema.get("properties"):
            compressed["input_schema"] = {"type": "object", "properties": {}}
            return compressed

        compressed_props = {}
        for prop_name, prop_def in schema.get("properties", {}).items():
            if not isinstance(prop_def, dict):
                compressed_props[prop_name] = prop_def
                continue
            # Keep only structural info: type, enum, default, items
            compact = {}
            for key in ("type", "enum", "default", "items"):
                if key in prop_def:
                    compact[key] = prop_def[key]
            compressed_props[prop_name] = compact

        compressed_schema = {"type": "object", "properties": compressed_props}
        if "required" in schema:
            compressed_schema["required"] = schema["required"]
        compressed["input_schema"] = compressed_schema
        return compressed

    def _get_plugin_tools(self) -> dict[str, dict]:
        """Collect plugin-registered tools (keyed by name).

        Plugin tools with ``_category`` are included in category queries.
        Plugin tools are merged on top of built-in tools (plugin wins on
        name collision).
        """
        if not self._plugin_registry:
            return {}
        return {t["name"]: t for t in self._plugin_registry.get_all_tool_definitions()}

    def _tool_category(self, name: str, tool: dict) -> str | None:
        """Return the category a tool belongs to, or None if core."""
        # Plugin-declared category takes precedence
        cat = tool.get("_category")
        if cat:
            return cat
        # Fall back to hardcoded mapping
        return _TOOL_CATEGORIES.get(name)

    def get_core_tools(self, compressed: bool = False) -> list[dict]:
        """Return tool definitions that are always loaded.

        Args:
            compressed: If True, return minimal schemas for small-context LLMs.

        Returns:
            List of tool definition dicts for tools not assigned to any
            category (i.e. not present in ``_TOOL_CATEGORIES`` and without
            a ``_category`` tag from a plugin).
        """
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}
        tools = [t for name, t in merged.items() if self._tool_category(name, t) is None]
        if compressed:
            return [self.compress_tool_schema(t) for t in tools]
        return tools

    def get_categories(self) -> list[dict]:
        """Return category metadata list for ``browse_tools`` response.

        Includes both built-in categories and plugin-created categories.

        Returns:
            List of dicts with ``name``, ``description``, and ``tool_count``
            keys — one per registered category.
        """
        result = []
        # Built-in categories
        for cat_name, meta in CATEGORIES.items():
            tools = self.get_category_tools(cat_name)
            result.append(
                {
                    "name": meta.name,
                    "description": meta.description,
                    "tool_count": len(tools) if tools else 0,
                }
            )
        # Plugin-created categories not in CATEGORIES
        plugin_tools = self._get_plugin_tools()
        plugin_cats: dict[str, list[dict]] = {}
        for name, tool in plugin_tools.items():
            cat = tool.get("_category")
            if cat and cat not in CATEGORIES:
                plugin_cats.setdefault(cat, []).append(tool)
        for cat_name, tools in sorted(plugin_cats.items()):
            # Build a description from tool names
            tool_names = ", ".join(t.get("name", "?") for t in tools)
            result.append(
                {
                    "name": cat_name,
                    "description": f"Plugin tools: {tool_names}",
                    "tool_count": len(tools),
                }
            )
        return result

    def get_category_tools(
        self,
        category: str,
        compressed: bool = False,
    ) -> list[dict] | None:
        """Return all tool definitions for a category.

        Includes both hardcoded ``_TOOL_CATEGORIES`` entries and
        plugin-registered tools with matching ``_category``.  Plugins
        can create their own categories (e.g. ``"vibecop"``) — these
        don't need to be in the built-in ``CATEGORIES`` dict.

        Args:
            category: Category name (e.g. ``"git"``, ``"vibecop"``).
            compressed: If True, return minimal schemas for small-context LLMs.

        Returns:
            List of tool definition dicts, or ``None`` if no tools
            match the category.
        """
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}

        tools = [t for name, t in merged.items() if self._tool_category(name, t) == category]
        if not tools:
            return None
        if compressed:
            return [self.compress_tool_schema(t) for t in tools]
        return tools

    def get_tool_index(self, exclude: set[str] | None = None) -> str:
        """Return a compact tool name index grouped by category.

        Lists all tool names (no descriptions or schemas) organized by
        category, one line per category.  Intended for injection into the
        supervisor system prompt so the LLM always knows which tools exist
        without calling ``browse_tools``.

        Args:
            exclude: Optional set of category names to omit (e.g. categories
                already fully loaded in the active tool set, or deprecated
                categories).  When ``None``, all categories are included.

        Returns:
            Markdown-formatted string, e.g.::

                **git**: git_status, git_commit, git_push, ...
                **memory**: memory_store, memory_recall, ...
        """
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}

        skip = exclude or set()
        # Collect all categories (built-in + plugin-created)
        all_cats: dict[str, list[str]] = {}
        for name, t in merged.items():
            cat = self._tool_category(name, t)
            if cat and cat not in skip:
                all_cats.setdefault(cat, []).append(name)

        lines: list[str] = []
        # Built-in categories first (in CATEGORIES order), then plugin cats
        ordered = [c for c in CATEGORIES if c in all_cats]
        ordered += sorted(c for c in all_cats if c not in CATEGORIES)
        for cat_name in ordered:
            names = sorted(all_cats[cat_name])
            lines.append(f"**{cat_name}**: {', '.join(names)}")
        return "\n".join(lines)

    def get_category_tool_names(self, category: str) -> list[str] | None:
        """Return tool names for a category (built-in or plugin-created).

        Args:
            category: Category name (e.g. ``"git"``, ``"vibecop"``).

        Returns:
            List of tool name strings, or ``None`` if no tools match.
        """
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}

        names = [name for name, t in merged.items() if self._tool_category(name, t) == category]
        return names or None

    def get_all_tools(self) -> list[dict]:
        """Return all tool definitions (core + all categories + plugins).

        Returns:
            List of every tool definition dict known to the registry,
            including any tools contributed by loaded plugins.
        """
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}
        return list(merged.values())

    # ------------------------------------------------------------------
    # Prompt-based tool search
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Tokenize text into lowercase words, splitting on underscores too.

        Filters out very short tokens (len < 3) and common stop words to
        reduce noise in keyword matching.
        """
        _STOP_WORDS = frozenset(
            {
                "the",
                "and",
                "for",
                "this",
                "that",
                "with",
                "from",
                "are",
                "was",
                "were",
                "been",
                "have",
                "has",
                "had",
                "not",
                "but",
                "can",
                "will",
                "all",
                "its",
                "use",
                "set",
                "get",
                "new",
                "one",
                "two",
                "any",
                "our",
                "you",
                "your",
            }
        )
        # Split on non-alphanumeric, underscores, and hyphens
        words = re.split(r"[^a-zA-Z0-9]+", text.lower())
        return {w for w in words if len(w) >= 3 and w not in _STOP_WORDS}

    def _tool_search_text(self, tool: dict) -> str:
        """Build searchable text from a tool definition.

        Combines the tool name (with underscores split into words) and
        the tool description into a single string for keyword matching.
        """
        name = tool.get("name", "")
        desc = tool.get("description", "")
        # Also include property names/descriptions from input_schema
        schema_parts: list[str] = []
        schema = tool.get("input_schema", {})
        for prop_name, prop_def in schema.get("properties", {}).items():
            schema_parts.append(prop_name)
            if isinstance(prop_def, dict) and "description" in prop_def:
                schema_parts.append(prop_def["description"])
        return f"{name} {desc} {' '.join(schema_parts)}"

    # Categories to never auto-preload (low-value or noisy).
    _SKIP_PRELOAD: frozenset[str] = frozenset()

    def search_relevant_categories(
        self,
        query: str,
        max_categories: int = 2,
        min_score: float = 0.15,
    ) -> list[str]:
        """Search tool definitions and return categories relevant to a query.

        Scores each non-core tool against the query using keyword overlap
        between the query tokens and the tool's name + description + schema.
        Categories are ranked by a composite of their best-matching tool's
        score (primary) and the sum of all tool scores (tiebreaker).

        Args:
            query: The user's prompt or search query.
            max_categories: Maximum number of categories to return.
            min_score: Minimum score threshold (0-1) for a category to be
                included. Categories whose best tool score falls below this
                are excluded.

        Returns:
            List of category names, ordered by relevance (best first).
            May be empty if no categories score above ``min_score``.
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        # Score each categorized tool, track best and sum per category
        category_best: dict[str, float] = {}
        category_sum: dict[str, float] = {}

        # Merge built-in categories with plugin tools
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}

        for tool_name, tool in merged.items():
            category = self._tool_category(tool_name, tool)
            if not category:
                continue
            tool_tokens = self._tokenize(self._tool_search_text(tool))
            if not tool_tokens:
                continue

            # Score: fraction of query tokens found in tool text
            matches = query_tokens & tool_tokens
            score = len(matches) / len(query_tokens)

            if score > 0:
                category_sum[category] = category_sum.get(category, 0.0) + score
                if score > category_best.get(category, 0.0):
                    category_best[category] = score

        # Rank by (best_score, sum_score) so ties are broken by breadth
        ranked = sorted(
            category_best.items(),
            key=lambda x: (x[1], category_sum.get(x[0], 0.0)),
            reverse=True,
        )
        return [
            cat
            for cat, score in ranked
            if score >= min_score and cat not in self._SKIP_PRELOAD
        ][:max_categories]
