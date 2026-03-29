"""Plugin registry — central coordinator for all loaded plugins.

The PluginRegistry is responsible for:

- Discovering installed plugins on disk
- Loading/unloading plugin modules
- Managing plugin lifecycle (install, update, remove, enable, disable)
- Providing command and tool lookups for integration with CommandHandler
  and ToolRegistry
- Circuit breaker protection for failing plugins

Integration points:

- **CommandHandler**: calls ``get_command(name)`` as fallback after
  built-in ``_cmd_{name}`` lookup fails.
- **ToolRegistry**: calls ``get_all_tool_definitions()`` to merge
  plugin tools into the supervisor's tool list.
- **Orchestrator**: calls ``discover_plugins()`` and ``load_all()``
  during initialization.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TYPE_CHECKING

from src.plugins.base import (
    Plugin,
    PluginContext,
    PluginInfo,
    PluginStatus,
)
from src.plugins.loader import (
    clone_plugin_repo,
    get_current_rev,
    import_plugin_module,
    install_requirements,
    parse_plugin_yaml,
    pull_plugin_repo,
    reset_prompts,
    setup_prompts,
)

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.database import Database
    from src.event_bus import EventBus

logger = logging.getLogger(__name__)

# Circuit breaker: auto-disable after this many consecutive failures
MAX_CONSECUTIVE_FAILURES = 5


class _LoadedPlugin:
    """Internal bookkeeping for a loaded plugin instance."""

    def __init__(
        self,
        *,
        info: PluginInfo,
        instance: Plugin,
        context: PluginContext,
        install_path: str,
        status: PluginStatus = PluginStatus.ACTIVE,
    ):
        self.info = info
        self.instance = instance
        self.context = context
        self.install_path = install_path
        self.status = status
        self.consecutive_failures = 0
        self.loaded_at = time.time()


class PluginRegistry:
    """Central coordinator for all plugins.

    Usage::

        registry = PluginRegistry(db=db, bus=bus, config=config)
        await registry.discover_plugins()
        await registry.load_all()

        # CommandHandler integration
        handler = registry.get_command("my_plugin_cmd")
        if handler:
            result = await handler(args)

        # ToolRegistry integration
        tools = registry.get_all_tool_definitions()
    """

    def __init__(
        self,
        *,
        db: Database,
        bus: EventBus,
        config: AppConfig,
        notify_callback: Callable | None = None,
        execute_command_callback: Callable | None = None,
    ):
        self._db = db
        self._bus = bus
        self._config = config
        self._notify_callback = notify_callback
        self._execute_command_callback = execute_command_callback

        # Loaded plugin instances keyed by plugin name
        self._plugins: dict[str, _LoadedPlugin] = {}

        # Shared registries that plugins write into
        self._commands: dict[str, Callable] = {}
        self._tools: dict[str, dict] = {}
        self._event_types: set[str] = set()

        # Plugins base directory
        self._plugins_dir = Path(config.data_dir) / "plugins"
        self._plugins_dir.mkdir(parents=True, exist_ok=True)

    @property
    def plugins_dir(self) -> Path:
        """Base directory for all installed plugins."""
        return self._plugins_dir

    # ------------------------------------------------------------------
    # Discovery & Loading
    # ------------------------------------------------------------------

    async def discover_plugins(self) -> list[str]:
        """Scan the plugins directory for installed plugins.

        Checks each subdirectory for a valid plugin.yaml manifest and
        records metadata in the database if not already present.

        Returns:
            List of discovered plugin names.
        """
        discovered = []

        if not self._plugins_dir.exists():
            return discovered

        for entry in sorted(self._plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            src_dir = entry / "src"
            if not src_dir.exists():
                continue

            try:
                info = parse_plugin_yaml(str(entry))
                discovered.append(info.name)

                # Ensure DB record exists
                existing = await self._db.get_plugin(info.name)
                if not existing:
                    await self._db.create_plugin(
                        plugin_id=info.name,
                        version=info.version,
                        source_url=info.url,
                        source_rev=get_current_rev(str(entry)),
                        install_path=str(entry),
                        status=PluginStatus.INSTALLED.value,
                        config=json.dumps(info.default_config),
                        permissions=json.dumps(
                            [p.value for p in info.permissions]
                        ),
                    )
                    logger.info("Discovered plugin: %s v%s", info.name, info.version)
            except Exception as e:
                logger.warning(
                    "Skipping invalid plugin directory %s: %s", entry.name, e,
                )

        return discovered

    async def load_all(self) -> int:
        """Load all discovered plugins that are not disabled or errored.

        Returns:
            Number of plugins successfully loaded.
        """
        loaded = 0
        plugins = await self._db.list_plugins()

        for plugin_row in plugins:
            if plugin_row.get("status") == PluginStatus.DISABLED.value:
                logger.debug("Skipping disabled plugin: %s", plugin_row["id"])
                continue

            try:
                await self.load_plugin(plugin_row["id"])
                loaded += 1
            except Exception as e:
                logger.error(
                    "Failed to load plugin '%s': %s", plugin_row["id"], e,
                    exc_info=True,
                )
                await self._db.update_plugin(
                    plugin_row["id"],
                    status=PluginStatus.ERROR.value,
                    error_message=str(e),
                )

        return loaded

    async def load_plugin(self, name: str) -> None:
        """Load a single plugin by name.

        Args:
            name: Plugin name (matches directory name and plugin.yaml name).

        Raises:
            FileNotFoundError: If the plugin is not installed.
            ImportError: If the plugin module fails to import.
            ValueError: If the plugin is invalid.
        """
        # Unload first if already loaded
        if name in self._plugins:
            await self.unload_plugin(name)

        plugin_row = await self._db.get_plugin(name)
        install_path = None

        if plugin_row:
            install_path = plugin_row.get("install_path")

        if not install_path:
            install_path = str(self._plugins_dir / name)

        if not Path(install_path).exists():
            raise FileNotFoundError(f"Plugin '{name}' not found at {install_path}")

        # Parse manifest
        info = parse_plugin_yaml(install_path)

        # Import the plugin module
        plugin_class = import_plugin_module(install_path)
        instance = plugin_class()

        # Create context
        ctx = PluginContext(
            plugin_name=name,
            install_path=install_path,
            db=self._db,
            bus=self._bus,
            command_registry=self._commands,
            tool_registry=self._tools,
            event_type_registry=self._event_types,
            notify_callback=self._notify_callback,
            execute_command_callback=self._execute_command_callback,
        )

        # Initialize plugin
        try:
            await instance.initialize(ctx)
        except Exception as e:
            logger.error("Plugin '%s' initialization failed: %s", name, e)
            await self._db.update_plugin(
                name,
                status=PluginStatus.ERROR.value,
                error_message=f"Initialization failed: {e}",
            )
            raise

        # Store loaded plugin
        self._plugins[name] = _LoadedPlugin(
            info=info,
            instance=instance,
            context=ctx,
            install_path=install_path,
            status=PluginStatus.ACTIVE,
        )

        # Update DB status
        await self._db.update_plugin(
            name,
            status=PluginStatus.ACTIVE.value,
            version=info.version,
            error_message=None,
        )

        # Setup prompts (non-destructive)
        setup_prompts(install_path)

        logger.info("Loaded plugin: %s v%s", name, info.version)

        # Emit event
        await self._bus.emit("plugin.loaded", {
            "plugin": name,
            "version": info.version,
        })

    async def unload_plugin(self, name: str) -> None:
        """Unload a plugin, calling its shutdown method.

        Args:
            name: Plugin name to unload.
        """
        loaded = self._plugins.get(name)
        if not loaded:
            return

        try:
            await loaded.instance.shutdown(loaded.context)
        except Exception as e:
            logger.warning("Plugin '%s' shutdown error: %s", name, e)

        # Remove registered commands — find prefixed commands first,
        # then remove any short-name aliases that share the same handler.
        prefixed_cmds = {
            k for k in self._commands if k.startswith(f"{name}.")
        }
        plugin_handlers = {
            id(self._commands[k]) for k in prefixed_cmds if k in self._commands
        }
        to_remove_cmds = set(prefixed_cmds)
        for k, v in list(self._commands.items()):
            if id(v) in plugin_handlers:
                to_remove_cmds.add(k)
        for key in to_remove_cmds:
            self._commands.pop(key, None)

        # Remove registered tools
        to_remove_tools = [
            k for k, v in self._tools.items()
            if v.get("_plugin") == name
        ]
        for key in to_remove_tools:
            self._tools.pop(key, None)

        del self._plugins[name]
        logger.info("Unloaded plugin: %s", name)

        await self._bus.emit("plugin.unloaded", {"plugin": name})

    async def reload_plugin(self, name: str) -> None:
        """Reload a plugin (shutdown → load → initialize).

        Args:
            name: Plugin name to reload.
        """
        logger.info("Reloading plugin: %s", name)
        await self.unload_plugin(name)
        await self.load_plugin(name)
        await self._bus.emit("plugin.updated", {"plugin": name})

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    async def install_from_git(
        self,
        url: str,
        *,
        branch: str | None = None,
        name: str | None = None,
    ) -> str:
        """Install a plugin from a git repository.

        Args:
            url: Git repository URL.
            branch: Optional branch to clone.
            name: Optional plugin name override (defaults to repo name).

        Returns:
            The installed plugin's name.

        Raises:
            RuntimeError: If cloning or installation fails.
            ValueError: If the plugin is invalid.
        """
        # Derive name from URL if not provided
        if not name:
            name = url.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".git"):
                name = name[:-4]

        install_path = str(self._plugins_dir / name)

        # Create instance directory
        Path(install_path).mkdir(parents=True, exist_ok=True)

        # Clone
        rev = await clone_plugin_repo(url, install_path, branch=branch)

        # Parse manifest to validate
        info = parse_plugin_yaml(install_path)

        # Install requirements
        if not install_requirements(install_path):
            raise RuntimeError(
                f"Failed to install requirements for plugin '{info.name}'"
            )

        # Setup default config
        config_path = Path(install_path) / "config.yaml"
        if not config_path.exists() and info.default_config:
            import yaml
            with open(config_path, "w") as f:
                yaml.safe_dump(info.default_config, f, default_flow_style=False)

        # Record in DB
        await self._db.create_plugin(
            plugin_id=info.name,
            version=info.version,
            source_url=url,
            source_rev=rev,
            source_branch=branch or "",
            install_path=install_path,
            status=PluginStatus.INSTALLED.value,
            config=json.dumps(info.default_config),
            permissions=json.dumps([p.value for p in info.permissions]),
        )

        # Setup prompts
        setup_prompts(install_path)

        # Load the plugin
        await self.load_plugin(info.name)

        logger.info(
            "Installed plugin '%s' v%s from %s", info.name, info.version, url,
        )

        await self._bus.emit("plugin.installed", {
            "plugin": info.name,
            "version": info.version,
            "source": url,
        })

        return info.name

    async def install_from_path(self, source_path: str, name: str | None = None) -> str:
        """Install a plugin from a local directory (development mode).

        Args:
            source_path: Path to the plugin source directory.
            name: Optional plugin name override.

        Returns:
            The installed plugin's name.
        """
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source path not found: {source_path}")

        # Parse manifest from source
        # For local installs, the source IS the src dir
        temp_info_path = source / "plugin.yaml"
        if not temp_info_path.exists():
            temp_info_path = source / "plugin.yml"
        if not temp_info_path.exists():
            raise ValueError(f"No plugin.yaml in {source_path}")

        import yaml
        with open(temp_info_path) as f:
            data = yaml.safe_load(f)

        plugin_name = name or data.get("name")
        if not plugin_name:
            raise ValueError("Cannot determine plugin name")

        install_path = str(self._plugins_dir / plugin_name)
        Path(install_path).mkdir(parents=True, exist_ok=True)

        # Symlink or copy source to src/
        src_dir = Path(install_path) / "src"
        if src_dir.exists():
            if src_dir.is_symlink():
                src_dir.unlink()
            else:
                import shutil
                shutil.rmtree(src_dir)

        # Use symlink for development mode
        src_dir.symlink_to(source.resolve())

        info = parse_plugin_yaml(install_path)

        # Install requirements
        install_requirements(install_path)

        # Record in DB
        await self._db.create_plugin(
            plugin_id=info.name,
            version=info.version,
            source_url=f"local:{source_path}",
            source_rev="",
            install_path=install_path,
            status=PluginStatus.INSTALLED.value,
            config=json.dumps(info.default_config),
            permissions=json.dumps([p.value for p in info.permissions]),
        )

        setup_prompts(install_path)
        await self.load_plugin(info.name)

        return info.name

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_plugin(
        self,
        name: str,
        *,
        rev: str | None = None,
    ) -> str:
        """Update an installed plugin by pulling latest changes.

        Args:
            name: Plugin name to update.
            rev: Optional specific revision to checkout.

        Returns:
            The new HEAD revision SHA.
        """
        plugin_row = await self._db.get_plugin(name)
        if not plugin_row:
            raise ValueError(f"Plugin '{name}' not found")

        install_path = plugin_row["install_path"]

        # Shutdown if loaded
        if name in self._plugins:
            await self.unload_plugin(name)

        # Pull latest
        new_rev = await pull_plugin_repo(install_path, rev=rev)

        # Reinstall requirements
        install_requirements(install_path)

        # Re-parse manifest
        info = parse_plugin_yaml(install_path)

        # Setup prompts (non-destructive — only copies new ones)
        setup_prompts(install_path)

        # Update DB
        await self._db.update_plugin(
            name,
            version=info.version,
            source_rev=new_rev,
        )

        # Reload
        await self.load_plugin(name)

        logger.info("Updated plugin '%s' to rev %s", name, new_rev[:8])

        await self._bus.emit("plugin.updated", {
            "plugin": name,
            "version": info.version,
            "rev": new_rev,
        })

        return new_rev

    # ------------------------------------------------------------------
    # Removal
    # ------------------------------------------------------------------

    async def remove_plugin(self, name: str) -> None:
        """Completely remove an installed plugin.

        Unloads the plugin, deletes its database records and data,
        and removes the instance directory from disk.

        Args:
            name: Plugin name to remove.
        """
        # Unload if loaded
        if name in self._plugins:
            await self.unload_plugin(name)

        # Get install path before deleting DB record
        plugin_row = await self._db.get_plugin(name)
        install_path = None
        if plugin_row:
            install_path = plugin_row.get("install_path")

        # Delete DB records
        await self._db.delete_plugin_data_all(name)
        await self._db.delete_plugin(name)

        # Delete from disk
        if install_path and Path(install_path).exists():
            import shutil
            shutil.rmtree(install_path)
            logger.info("Removed plugin directory: %s", install_path)

        logger.info("Removed plugin: %s", name)

        await self._bus.emit("plugin.removed", {"plugin": name})

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    async def enable_plugin(self, name: str) -> None:
        """Enable a disabled plugin and load it.

        Args:
            name: Plugin name to enable.
        """
        await self._db.update_plugin(name, status=PluginStatus.INSTALLED.value)
        await self.load_plugin(name)

    async def disable_plugin(self, name: str) -> None:
        """Disable a plugin (unload but keep installed).

        Args:
            name: Plugin name to disable.
        """
        if name in self._plugins:
            await self.unload_plugin(name)
        await self._db.update_plugin(
            name, status=PluginStatus.DISABLED.value,
        )

    # ------------------------------------------------------------------
    # Command / Tool Lookups (Integration API)
    # ------------------------------------------------------------------

    def get_command(self, name: str) -> Callable | None:
        """Look up a plugin command handler by name.

        Called by CommandHandler.execute() as a fallback after checking
        built-in ``_cmd_{name}`` handlers.

        Args:
            name: Command name to look up.

        Returns:
            The async command handler, or None if not found.
        """
        return self._commands.get(name)

    def get_all_tool_definitions(self) -> list[dict]:
        """Return all registered plugin tool definitions.

        Called by ToolRegistry to merge plugin tools into the
        supervisor's available tool list.

        Returns:
            List of tool definition dicts (JSON Schema format).
        """
        return list(self._tools.values())

    def get_registered_event_types(self) -> list[str]:
        """Return all plugin-declared event types.

        Returns:
            Sorted list of event type strings.
        """
        return sorted(self._event_types)

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def list_plugins(self) -> list[dict]:
        """List all loaded plugins with their metadata.

        Returns:
            List of plugin info dicts.
        """
        result = []
        for name, loaded in self._plugins.items():
            result.append({
                "name": name,
                "version": loaded.info.version,
                "description": loaded.info.description,
                "author": loaded.info.author,
                "status": loaded.status.value,
                "install_path": loaded.install_path,
                "commands": list(
                    k for k in self._commands
                    if k.startswith(f"{name}.")
                ),
                "tools": [
                    k for k, v in self._tools.items()
                    if v.get("_plugin") == name
                ],
                "loaded_at": loaded.loaded_at,
            })
        return result

    def get_plugin(self, name: str) -> dict | None:
        """Get detailed info for a specific loaded plugin.

        Args:
            name: Plugin name.

        Returns:
            Plugin info dict, or None if not loaded.
        """
        loaded = self._plugins.get(name)
        if not loaded:
            return None

        return {
            "name": name,
            "version": loaded.info.version,
            "description": loaded.info.description,
            "author": loaded.info.author,
            "url": loaded.info.url,
            "status": loaded.status.value,
            "install_path": loaded.install_path,
            "permissions": [p.value for p in loaded.info.permissions],
            "commands": list(
                k for k in self._commands
                if k.startswith(f"{name}.") or k == name
            ),
            "tools": [
                k for k, v in self._tools.items()
                if v.get("_plugin") == name
            ],
            "event_types": loaded.info.event_types,
            "hooks": loaded.info.hooks,
            "loaded_at": loaded.loaded_at,
            "consecutive_failures": loaded.consecutive_failures,
        }

    def is_loaded(self, name: str) -> bool:
        """Check if a plugin is currently loaded."""
        return name in self._plugins

    # ------------------------------------------------------------------
    # Circuit Breaker
    # ------------------------------------------------------------------

    async def record_failure(self, name: str, error: str) -> None:
        """Record a plugin failure for circuit breaker tracking.

        After MAX_CONSECUTIVE_FAILURES, the plugin is auto-disabled.

        Args:
            name: Plugin name.
            error: Error description.
        """
        loaded = self._plugins.get(name)
        if not loaded:
            return

        loaded.consecutive_failures += 1
        logger.warning(
            "Plugin '%s' failure %d/%d: %s",
            name, loaded.consecutive_failures, MAX_CONSECUTIVE_FAILURES, error,
        )

        if loaded.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "Plugin '%s' exceeded failure threshold (%d), auto-disabling",
                name, MAX_CONSECUTIVE_FAILURES,
            )
            await self.disable_plugin(name)
            await self._db.update_plugin(
                name,
                status=PluginStatus.ERROR.value,
                error_message=f"Auto-disabled after {MAX_CONSECUTIVE_FAILURES} consecutive failures. Last: {error}",
            )
            await self._bus.emit("plugin.auto_disabled", {
                "plugin": name,
                "reason": error,
                "failures": loaded.consecutive_failures,
            })

    def record_success(self, name: str) -> None:
        """Record a successful plugin operation, resetting the failure counter.

        Args:
            name: Plugin name.
        """
        loaded = self._plugins.get(name)
        if loaded:
            loaded.consecutive_failures = 0

    # ------------------------------------------------------------------
    # Callbacks (injected after construction)
    # ------------------------------------------------------------------

    def set_notify_callback(self, callback: Callable) -> None:
        """Set the notification callback for all plugin contexts."""
        self._notify_callback = callback
        for loaded in self._plugins.values():
            loaded.context._notify_callback = callback

    def set_execute_command_callback(self, callback: Callable) -> None:
        """Set the command execution callback for all plugin contexts."""
        self._execute_command_callback = callback
        for loaded in self._plugins.values():
            loaded.context._execute_command_callback = callback
