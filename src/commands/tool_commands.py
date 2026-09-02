"""Tool commands mixin — load_tools, find_applicable_tool."""

from __future__ import annotations

#: Human-readable hints explaining where an unimplemented tool's backing
#: implementation is supposed to come from.  Keyed by tool-name prefix.
_MISSING_BACKEND_HINTS: tuple[tuple[str, str], ...] = (
    (
        "memory_",
        "the external 'aq-memory' plugin is not installed "
        "(install it with `aq plugin install <aq-memory-url>`)",
    ),
)


def _missing_backend_hint(name: str) -> str:
    """Return a reason string for why *name* has no executable backing."""
    for prefix, hint in _MISSING_BACKEND_HINTS:
        if name.startswith(prefix):
            return hint
    return "no backing implementation is registered"


class ToolCommandsMixin:
    """Tool navigation command methods mixed into CommandHandler."""

    def _executable_tool_names(self, names: list[str]) -> tuple[list[str], list[str]]:
        """Split *names* into (executable, unavailable) by dispatchability.

        A tool definition without a backing ``_cmd_*`` method or plugin
        command cannot be executed — advertising it from ``load_tools``
        leads to a confusing "Unknown command" error on the next turn.
        """
        available: list[str] = []
        unavailable: list[str] = []
        for name in names:
            (available if self.has_command(name) else unavailable).append(name)
        return self._capability_filtered(available), unavailable

    def _capability_filtered(self, names: list[str]) -> list[str]:
        """Drop names the current principal could not dispatch.

        Discovery and execution must agree (Playbook V2 Package 0 §4.3):
        advertising a tool the next turn will refuse is exactly as confusing
        as advertising one with no backing handler.  Uses the same
        ``command_allowed`` predicate the dispatch gate uses, so the two
        cannot drift.  Silently dropped rather than reported as
        "unavailable": which commands a caller *cannot* reach is operator
        information, not agent information (§4.4).
        """
        from src.commands.authorization import command_allowed
        from src.commands.principal import current_principal

        principal = current_principal()
        if principal is None or not principal.enforced:
            return names
        resolver = self._command_resolver
        return [n for n in names if command_allowed(n, principal, resolver=resolver)]

    async def _cmd_load_tools(self, args: dict) -> dict:
        """Load tools by category or individual name.

        The actual schema injection happens in the chat layer (Supervisor),
        not here. This command returns the list of tool names so the chat
        layer knows which schemas to add.
        """
        from src.tools import ToolRegistry

        tool_name = args.get("tool_name", "")
        category = args.get("category", "")
        registry = ToolRegistry()
        if hasattr(self, "orchestrator") and self.orchestrator:
            if hasattr(self.orchestrator, "plugin_registry") and self.orchestrator.plugin_registry:
                registry.set_plugin_registry(self.orchestrator.plugin_registry)

        # Single-tool mode
        if tool_name:
            defn = registry.get_tool_definition(tool_name)
            if not defn:
                return {"error": f"Unknown tool: '{tool_name}'."}
            cat = registry.get_tool_category(tool_name)
            if not cat:
                return {"error": f"'{tool_name}' is a core tool (already loaded)."}
            if not self.has_command(tool_name):
                return {
                    "error": (
                        f"Tool '{tool_name}' is defined but not currently "
                        f"executable: {_missing_backend_hint(tool_name)}. "
                        "Do not call it — use another approach."
                    ),
                    "unavailable": [tool_name],
                }
            if not self._capability_filtered([tool_name]):
                from src.commands.authorization import denial_result

                return denial_result(tool_name)
            return {
                "loaded": cat,
                "tools_added": [tool_name],
                "single_tool": True,
                "message": f"Tool '{tool_name}' is now available.",
            }

        # Category mode
        if not category:
            return {"error": "Provide 'category' or 'tool_name'."}

        names = registry.get_category_tool_names(category)
        if names is None:
            available = [c["name"] for c in registry.get_categories()]
            return {
                "error": f"Unknown category: {category}. Available: {', '.join(available)}",
            }
        available, unavailable = self._executable_tool_names(names)
        if not available:
            reasons = ", ".join(sorted({_missing_backend_hint(n) for n in unavailable}))
            return {
                "error": (
                    f"Category '{category}' has no executable tools right now: "
                    f"{reasons}. Do not call {', '.join(sorted(unavailable))}."
                ),
                "unavailable": sorted(unavailable),
            }

        result = {
            "loaded": category,
            "tools_added": available,
            "message": f"{len(available)} {category} tools are now available.",
        }
        if unavailable:
            reasons = ", ".join(sorted({_missing_backend_hint(n) for n in unavailable}))
            result["unavailable"] = sorted(unavailable)
            result["message"] += (
                f" Not available (do not call): {', '.join(sorted(unavailable))} — {reasons}."
            )
        return result

    async def _cmd_find_applicable_tool(self, args: dict) -> dict:
        """Semantic search over all tool definitions.

        Agents describe what they want to do and get back the best
        matching tools ranked by relevance.
        """
        description = args.get("description", "")
        if not description:
            return {"error": "description is required"}

        top_k = args.get("top_k", 5)

        # Use the orchestrator's tool registry index
        from src.tools import ToolRegistry

        registry = ToolRegistry()
        if hasattr(self, "orchestrator") and self.orchestrator:
            if hasattr(self.orchestrator, "plugin_registry") and self.orchestrator.plugin_registry:
                registry.set_plugin_registry(self.orchestrator.plugin_registry)
            # Prefer the pre-built index from the orchestrator's registry
            if hasattr(self.orchestrator, "_tool_registry") and self.orchestrator._tool_registry:
                registry = self.orchestrator._tool_registry

        idx = registry.tool_index
        if idx and idx.ready:
            results = await idx.search(description, top_k=top_k)
            return {"query": description, "matches": results}

        # Fallback: keyword matching if embeddings aren't available
        all_tools = registry.get_all_tools()
        query_words = set(description.lower().split())
        scored = []
        for t in all_tools:
            text = f"{t['name']} {t.get('description', '')}".lower()
            overlap = sum(1 for w in query_words if w in text)
            if overlap > 0:
                scored.append((overlap, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        matches = [
            {"name": t["name"], "description": t.get("description", ""), "score": s}
            for s, t in scored[:top_k]
        ]
        return {"query": description, "matches": matches, "method": "keyword"}
