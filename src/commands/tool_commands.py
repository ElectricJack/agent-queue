"""Tool commands mixin — load_tools, find_applicable_tool."""

from __future__ import annotations


class ToolCommandsMixin:
    """Tool navigation command methods mixed into CommandHandler."""

    async def _cmd_load_tools(self, args: dict) -> dict:
        """Load a tool category's definitions for the current interaction.

        The actual schema injection happens in the chat layer (Supervisor),
        not here. This command returns the list of tool names so the chat
        layer knows which schemas to add.
        """
        from src.tools import ToolRegistry

        category = args.get("category", "")
        registry = ToolRegistry()
        if hasattr(self, "orchestrator") and self.orchestrator:
            if hasattr(self.orchestrator, "plugin_registry") and self.orchestrator.plugin_registry:
                registry.set_plugin_registry(self.orchestrator.plugin_registry)
        names = registry.get_category_tool_names(category)
        if names is None:
            available = [c["name"] for c in registry.get_categories()]
            return {
                "error": (f"Unknown category: {category}. Available: {', '.join(available)}"),
            }
        return {
            "loaded": category,
            "tools_added": names,
            "message": (f"{len(names)} {category} tools are now available."),
        }

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
