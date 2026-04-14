"""Plugin commands mixin — plugin lifecycle management."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PluginCommandsMixin:
    """Plugin management command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # Plugin management commands
    # ------------------------------------------------------------------

    async def _cmd_plugin_list(self, args: dict) -> dict:
        """List installed plugins."""
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        # Get all plugins from DB (includes disabled/errored)
        db_plugins = await self.db.list_plugins()
        loaded = self.orchestrator.plugin_registry.list_plugins()

        plugins = []
        for p in db_plugins:
            info = {
                "name": p["id"],
                "version": p.get("version", "?"),
                "status": p.get("status", "unknown"),
                "source_url": p.get("source_url", ""),
            }
            # Merge runtime info if loaded
            for lp in loaded:
                if lp["name"] == p["id"]:
                    info["description"] = lp.get("description", "")
                    info["commands"] = lp.get("commands", [])
                    info["tools"] = lp.get("tools", [])
                    break
            plugins.append(info)

        return {"plugins": plugins, "count": len(plugins)}

    async def _cmd_plugin_info(self, args: dict) -> dict:
        """Get detailed info for a specific plugin."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        # Try loaded plugin first
        info = self.orchestrator.plugin_registry.get_plugin(name)
        if info:
            return {"plugin": info}

        # Fall back to DB record
        db_plugin = await self.db.get_plugin(name)
        if db_plugin:
            return {"plugin": db_plugin}

        return {"error": f"Plugin '{name}' not found"}

    async def _cmd_plugin_install(self, args: dict) -> dict:
        """Install a plugin from a git repository."""
        url = args.get("url")
        if not url:
            return {"error": "url is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        branch = args.get("branch")
        name = args.get("name")

        try:
            installed_name = await self.orchestrator.plugin_registry.install_from_git(
                url,
                branch=branch,
                name=name,
            )
            return {
                "installed": installed_name,
                "message": f"Plugin '{installed_name}' installed successfully from {url}",
            }
        except Exception as e:
            return {"error": f"Installation failed: {e}"}

    async def _cmd_plugin_update(self, args: dict) -> dict:
        """Update an installed plugin."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        rev = args.get("rev")
        try:
            new_rev = await self.orchestrator.plugin_registry.update_plugin(
                name,
                rev=rev,
            )
            return {
                "updated": name,
                "rev": new_rev,
                "message": f"Plugin '{name}' updated to {new_rev[:8]}",
            }
        except Exception as e:
            return {"error": f"Update failed: {e}"}

    async def _cmd_plugin_remove(self, args: dict) -> dict:
        """Remove an installed plugin."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        try:
            await self.orchestrator.plugin_registry.remove_plugin(name)
            return {"removed": name, "message": f"Plugin '{name}' removed"}
        except Exception as e:
            return {"error": f"Removal failed: {e}"}

    async def _cmd_plugin_enable(self, args: dict) -> dict:
        """Enable a disabled plugin."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        try:
            await self.orchestrator.plugin_registry.enable_plugin(name)
            return {"enabled": name, "message": f"Plugin '{name}' enabled"}
        except Exception as e:
            return {"error": f"Enable failed: {e}"}

    async def _cmd_plugin_disable(self, args: dict) -> dict:
        """Disable a plugin (keeps it installed)."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        try:
            await self.orchestrator.plugin_registry.disable_plugin(name)
            return {"disabled": name, "message": f"Plugin '{name}' disabled"}
        except Exception as e:
            return {"error": f"Disable failed: {e}"}

    async def _cmd_plugin_reload(self, args: dict) -> dict:
        """Reload a plugin (shutdown -> load -> initialize)."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        try:
            await self.orchestrator.plugin_registry.reload_plugin(name)
            return {"reloaded": name, "message": f"Plugin '{name}' reloaded"}
        except Exception as e:
            return {"error": f"Reload failed: {e}"}

    async def _cmd_plugin_config(self, args: dict) -> dict:
        """Get or set plugin configuration."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        plugin_info = self.orchestrator.plugin_registry.get_plugin(name)
        if not plugin_info:
            # Try DB
            db_plugin = await self.db.get_plugin(name)
            if not db_plugin:
                return {"error": f"Plugin '{name}' not found"}
            import json

            try:
                config = json.loads(db_plugin.get("config", "{}"))
            except (json.JSONDecodeError, TypeError):
                config = {}
            return {"name": name, "config": config}

        loaded = self.orchestrator.plugin_registry._plugins.get(name)
        if loaded:
            config = loaded.context.get_config()
            new_config = args.get("config")
            if new_config:
                if isinstance(new_config, str):
                    import json

                    new_config = json.loads(new_config)
                await loaded.context.save_config(new_config)
                return {"name": name, "config": new_config, "message": "Config updated"}
            return {"name": name, "config": config}

        return {"error": f"Plugin '{name}' is not loaded"}

    async def _cmd_plugin_prompts(self, args: dict) -> dict:
        """List a plugin's prompt templates."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        loaded = self.orchestrator.plugin_registry._plugins.get(name)
        if not loaded:
            return {"error": f"Plugin '{name}' is not loaded"}

        prompts = loaded.context.list_prompts()
        return {"name": name, "prompts": prompts}

    async def _cmd_plugin_reset_prompts(self, args: dict) -> dict:
        """Re-copy all default prompts from plugin source."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        loaded = self.orchestrator.plugin_registry._plugins.get(name)
        if not loaded:
            return {"error": f"Plugin '{name}' is not loaded"}

        from src.plugins.loader import reset_prompts

        count = reset_prompts(loaded.install_path)
        return {"name": name, "reset_count": count, "message": f"Reset {count} prompts"}
