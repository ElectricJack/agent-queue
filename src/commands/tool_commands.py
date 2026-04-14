"""Tool commands mixin — browse_tools, load_tools."""

from __future__ import annotations


class ToolCommandsMixin:
    """Tool navigation command methods mixed into CommandHandler."""

    # -------------------------------------------------------------------
    # Tool navigation commands (Phase 3 -- tiered tool system)
    # -------------------------------------------------------------------

    async def _cmd_browse_tools(self, args: dict) -> dict:
        """List available tool categories with metadata."""
        from src.tools import ToolRegistry

        registry = ToolRegistry()
        if hasattr(self.orchestrator, "plugin_registry") and self.orchestrator.plugin_registry:
            registry.set_plugin_registry(self.orchestrator.plugin_registry)
        return {"categories": registry.get_categories()}

    async def _cmd_load_tools(self, args: dict) -> dict:
        """Load a tool category's definitions for the current interaction.

        The actual schema injection happens in the chat layer (Supervisor),
        not here. This command returns the list of tool names so the chat
        layer knows which schemas to add.
        """
        from src.tools import ToolRegistry

        category = args.get("category", "")
        registry = ToolRegistry()
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
