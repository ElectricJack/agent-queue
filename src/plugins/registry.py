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

import asyncio
import json
import logging
import time

import structlog
from collections.abc import Callable
from dataclasses import dataclass
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
    has_pyproject,
    import_plugin_module,
    install_plugin_from_url,
    install_plugin_package,
    load_plugin_via_entry_point,
    parse_plugin_metadata,
    parse_plugin_yaml,
    parse_pyproject_metadata,
    pull_plugin_repo,
    setup_prompts,
)
from src.schedule import matches_schedule

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.database import Database
    from src.event_bus import EventBus

logger = logging.getLogger(__name__)

# Circuit breaker: auto-disable after this many consecutive failures
MAX_CONSECUTIVE_FAILURES = 5

# Names reserved by built-in CLI groups and Discord commands.  Plugins must
# not use these because they would collide with core functionality.
RESERVED_PLUGIN_NAMES: frozenset[str] = frozenset(
    {
        # CLI groups
        "status",
        "task",
        "agent",
        "hook",
        "project",
        "plugin",
        # Discord commands that read as top-level groups
        "tasks",
        "projects",
        "agents",
        "events",
        "hooks",
        # Meta
        "aq",
        "help",
        "version",
    }
)


@dataclass
class _CronJob:
    """A cron-scheduled plugin method."""

    plugin_name: str
    method: Callable
    expression: str
    config_key: str | None = None
    last_run: float | None = None


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
        self._invoke_llm_callback: Callable | None = None

        # Loaded plugin instances keyed by plugin name
        self._plugins: dict[str, _LoadedPlugin] = {}

        # Shared registries that plugins write into
        self._commands: dict[str, Callable] = {}
        self._tools: dict[str, dict] = {}
        self._event_types: set[str] = set()

        # Cron-scheduled plugin methods
        self._cron_jobs: list[_CronJob] = []
        self._cron_tasks: dict[str, asyncio.Task] = {}

        # Plugins base directory (git clones)
        self._plugins_dir = Path(config.data_dir) / "plugins"
        self._plugins_dir.mkdir(parents=True, exist_ok=True)

        # Plugin data directory (config, data, prompts, logs — survives reinstalls)
        self._plugin_data_dir = Path(config.data_dir) / "plugin-data"
        self._plugin_data_dir.mkdir(parents=True, exist_ok=True)

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
                # Prefer pyproject.toml, fall back to plugin.yaml
                if has_pyproject(str(entry)):
                    meta = parse_pyproject_metadata(str(entry))
                    plugin_name = meta["name"]
                    version = meta["version"]
                    default_config: dict = {}
                    permissions: list = []
                else:
                    info = parse_plugin_yaml(str(entry))
                    logger.warning(
                        "Plugin '%s' uses legacy plugin.yaml — migrate to "
                        'pyproject.toml with [project.entry-points."aq.plugins"]',
                        info.name,
                    )
                    plugin_name = info.name
                    version = info.version
                    default_config = info.default_config
                    permissions = [p.value for p in info.permissions]

                discovered.append(plugin_name)

                # Ensure DB record exists
                existing = await self._db.get_plugin(plugin_name)
                if not existing:
                    await self._db.create_plugin(
                        plugin_id=plugin_name,
                        version=version,
                        source_url="",
                        source_rev=get_current_rev(str(entry)),
                        install_path=str(entry),
                        status=PluginStatus.INSTALLED.value,
                        config=json.dumps(default_config),
                        permissions=json.dumps(permissions),
                    )
                    logger.info("Discovered plugin: %s v%s", plugin_name, version)
            except Exception as e:
                logger.warning(
                    "Skipping invalid plugin directory %s: %s",
                    entry.name,
                    e,
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
                    "Failed to load plugin '%s': %s",
                    plugin_row["id"],
                    e,
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
            # Auto-recover: re-clone if we have the source URL in the DB
            source_url = plugin_row.get("source_url") if plugin_row else None
            if source_url:
                logger.info(
                    "Plugin '%s' missing at %s — re-cloning from %s",
                    name,
                    install_path,
                    source_url,
                )
                Path(install_path).mkdir(parents=True, exist_ok=True)
                branch = plugin_row.get("source_branch") or None
                rev = plugin_row.get("source_rev") or None
                try:
                    new_rev = await clone_plugin_repo(
                        source_url,
                        install_path,
                        branch=branch,
                        rev=rev,
                    )
                    if not install_plugin_package(install_path):
                        raise RuntimeError(f"Failed to install plugin at '{install_path}'")
                    await self._db.update_plugin(
                        name,
                        source_rev=new_rev,
                        status=PluginStatus.INSTALLED.value,
                        error_message=None,
                    )
                except Exception as e:
                    raise FileNotFoundError(
                        f"Plugin '{name}' missing at {install_path} and re-clone failed: {e}"
                    ) from e
            else:
                raise FileNotFoundError(f"Plugin '{name}' not found at {install_path}")

        # Load plugin class: try entry point first, fall back to plugin.py import
        use_pyproject = has_pyproject(install_path)
        plugin_class: type[Plugin] | None = None

        if use_pyproject:
            plugin_class = load_plugin_via_entry_point(name)
            if not plugin_class:
                # Entry point might use the distribution name instead
                try:
                    meta = parse_pyproject_metadata(install_path)
                    plugin_class = load_plugin_via_entry_point(meta["name"])
                except (FileNotFoundError, ValueError):
                    pass

        if not plugin_class:
            if use_pyproject:
                logger.debug(
                    "Plugin '%s' has pyproject.toml but no entry point found, "
                    "falling back to plugin.py import",
                    name,
                )
            plugin_class = import_plugin_module(install_path)

        # Build metadata
        if use_pyproject:
            info = parse_plugin_metadata(install_path, plugin_class)
        else:
            info = parse_plugin_yaml(install_path)

        instance = plugin_class()

        # Create context
        data_path = str(self._plugin_data_dir / name)
        ctx = PluginContext(
            plugin_name=name,
            install_path=install_path,
            data_path=data_path,
            db=self._db,
            bus=self._bus,
            command_registry=self._commands,
            tool_registry=self._tools,
            event_type_registry=self._event_types,
            notify_callback=self._notify_callback,
            execute_command_callback=self._execute_command_callback,
            invoke_llm_callback=self._invoke_llm_callback,
        )

        # Load config from DB before plugin init so get_config() works
        await ctx.load_config()

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

        # Collect @cron-decorated methods
        for attr_name in dir(instance):
            try:
                method = getattr(instance, attr_name)
            except Exception:
                continue
            if callable(method) and hasattr(method, "_cron_expression"):
                config_key = getattr(method, "_cron_config_key", None)
                self._cron_jobs.append(
                    _CronJob(
                        plugin_name=name,
                        method=method,
                        expression=method._cron_expression,
                        config_key=config_key,
                    )
                )
                extra = f" (configurable via '{config_key}')" if config_key else ""
                logger.info(
                    "Plugin '%s' registered cron job: %s [%s]%s",
                    name,
                    attr_name,
                    method._cron_expression,
                    extra,
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
        await self._bus.emit(
            "plugin.loaded",
            {
                "plugin": name,
                "version": info.version,
            },
        )

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
        prefixed_cmds = {k for k in self._commands if k.startswith(f"{name}.")}
        plugin_handlers = {id(self._commands[k]) for k in prefixed_cmds if k in self._commands}
        to_remove_cmds = set(prefixed_cmds)
        for k, v in list(self._commands.items()):
            if id(v) in plugin_handlers:
                to_remove_cmds.add(k)
        for key in to_remove_cmds:
            self._commands.pop(key, None)

        # Remove registered tools
        to_remove_tools = [k for k, v in self._tools.items() if v.get("_plugin") == name]
        for key in to_remove_tools:
            self._tools.pop(key, None)

        # Remove cron jobs and cancel running tasks
        self._cron_jobs = [j for j in self._cron_jobs if j.plugin_name != name]
        for key, task in list(self._cron_tasks.items()):
            if key.startswith(f"{name}."):
                task.cancel()
                self._cron_tasks.pop(key, None)

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
        # Validate reserved names before attempting install
        if name and name.lower() in RESERVED_PLUGIN_NAMES:
            raise ValueError(
                f"Plugin name '{name}' is reserved (conflicts with built-in "
                f"CLI/Discord commands). Choose a different name."
            )

        result = await install_plugin_from_url(
            url,
            self._plugins_dir,
            self._plugin_data_dir,
            branch=branch,
            name=name,
        )

        plugin_name = result["name"]
        if plugin_name.lower() in RESERVED_PLUGIN_NAMES:
            raise ValueError(
                f"Plugin name '{plugin_name}' is reserved (conflicts with "
                f"built-in CLI/Discord commands). Choose a different name."
            )

        # Record in DB
        await self._db.create_plugin(
            plugin_id=plugin_name,
            version=result["version"],
            source_url=url,
            source_rev=result["source_rev"],
            source_branch=branch or "",
            install_path=result["install_path"],
            status=PluginStatus.INSTALLED.value,
            config=json.dumps(result["default_config"]),
            permissions=json.dumps(result["permissions"]),
        )

        # Load the plugin
        await self.load_plugin(plugin_name)

        logger.info(
            "Installed plugin '%s' v%s from %s",
            plugin_name,
            result["version"],
            url,
        )

        await self._bus.emit(
            "plugin.installed",
            {
                "plugin": plugin_name,
                "version": result["version"],
                "source": url,
            },
        )

        return plugin_name

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

        # Determine plugin name from source directory
        # Try pyproject.toml first, then plugin.yaml
        pyproject_path = source / "pyproject.toml"
        if pyproject_path.exists():
            import tomllib

            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
            # Prefer entry-point name over project.name (dist name)
            ep_names = list(data.get("project", {}).get("entry-points", {}).get("aq.plugins", {}))
            plugin_name = (
                name or (ep_names[0] if ep_names else None) or data.get("project", {}).get("name")
            )
        else:
            temp_info_path = source / "plugin.yaml"
            if not temp_info_path.exists():
                temp_info_path = source / "plugin.yml"
            if not temp_info_path.exists():
                raise ValueError(f"No pyproject.toml or plugin.yaml in {source_path}")
            import yaml

            with open(temp_info_path) as f:
                data = yaml.safe_load(f)
            plugin_name = name or data.get("name")

        if not plugin_name:
            raise ValueError("Cannot determine plugin name")

        if plugin_name.lower() in RESERVED_PLUGIN_NAMES:
            raise ValueError(
                f"Plugin name '{plugin_name}' is reserved (conflicts with "
                f"built-in CLI/Discord commands). Choose a different name."
            )

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

        # Install: prefer pip install -e (pyproject.toml), fall back to requirements.txt
        install_plugin_package(install_path)

        # Parse metadata
        if has_pyproject(install_path):
            plugin_class = load_plugin_via_entry_point(plugin_name)
            if plugin_class:
                info = parse_plugin_metadata(install_path, plugin_class)
            else:
                info = parse_plugin_yaml(install_path)
        else:
            info = parse_plugin_yaml(install_path)

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

        # Reinstall
        install_plugin_package(install_path)

        # Re-parse metadata
        if has_pyproject(install_path):
            plugin_class = load_plugin_via_entry_point(name)
            if plugin_class:
                info = parse_plugin_metadata(install_path, plugin_class)
            else:
                info = parse_plugin_yaml(install_path)
        else:
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

        await self._bus.emit(
            "plugin.updated",
            {
                "plugin": name,
                "version": info.version,
                "rev": new_rev,
            },
        )

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

        # Delete from disk — but only if no other plugin record shares
        # the same install path (prevents accidental deletion when duplicate
        # records exist under different names).
        if install_path and Path(install_path).exists():
            other_plugins = await self._db.list_plugins()
            shared = any(p.get("install_path") == install_path for p in other_plugins)
            if shared:
                logger.warning(
                    "Not deleting %s — another plugin record shares this path",
                    install_path,
                )
            else:
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
            name,
            status=PluginStatus.DISABLED.value,
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
            result.append(
                {
                    "name": name,
                    "version": loaded.info.version,
                    "description": loaded.info.description,
                    "author": loaded.info.author,
                    "status": loaded.status.value,
                    "install_path": loaded.install_path,
                    "commands": list(k for k in self._commands if k.startswith(f"{name}.")),
                    "tools": [k for k, v in self._tools.items() if v.get("_plugin") == name],
                    "loaded_at": loaded.loaded_at,
                }
            )
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
            "commands": list(k for k in self._commands if k.startswith(f"{name}.") or k == name),
            "tools": [k for k, v in self._tools.items() if v.get("_plugin") == name],
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
            name,
            loaded.consecutive_failures,
            MAX_CONSECUTIVE_FAILURES,
            error,
        )

        if loaded.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "Plugin '%s' exceeded failure threshold (%d), auto-disabling",
                name,
                MAX_CONSECUTIVE_FAILURES,
            )
            await self.disable_plugin(name)
            await self._db.update_plugin(
                name,
                status=PluginStatus.ERROR.value,
                error_message=f"Auto-disabled after {MAX_CONSECUTIVE_FAILURES} consecutive failures. Last: {error}",
            )
            await self._bus.emit(
                "plugin.auto_disabled",
                {
                    "plugin": name,
                    "reason": error,
                    "failures": loaded.consecutive_failures,
                },
            )

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

    def set_invoke_llm_callback(self, callback: Callable) -> None:
        """Set the LLM invocation callback for all plugin contexts."""
        self._invoke_llm_callback = callback
        for loaded in self._plugins.values():
            loaded.context._invoke_llm_callback = callback

    # ------------------------------------------------------------------
    # Cron Scheduling
    # ------------------------------------------------------------------

    async def tick_cron(self) -> None:
        """Check and run due cron-scheduled plugin methods.

        Called each orchestrator cycle (~5s).  Each ``@cron``-decorated
        method on a loaded plugin is checked against ``matches_schedule``
        from ``src/schedule.py``.  Jobs run as ``asyncio.Task`` instances;
        overlapping invocations of the same job are skipped.
        """
        # Clean up finished tasks
        done_keys = [k for k, t in self._cron_tasks.items() if t.done()]
        for k in done_keys:
            self._cron_tasks.pop(k, None)

        from datetime import datetime, timezone

        now_dt = datetime.now(timezone.utc)
        for job in self._cron_jobs:
            # Skip if plugin is no longer loaded
            if job.plugin_name not in self._plugins:
                continue

            key = f"{job.plugin_name}.{job.method.__name__}"

            # Skip if still running from last invocation
            if key in self._cron_tasks and not self._cron_tasks[key].done():
                continue

            # Resolve expression: config override takes precedence
            expression = job.expression
            if job.config_key:
                ctx = self._plugins[job.plugin_name].context
                override = ctx.get_config_value(job.config_key)
                if override and isinstance(override, str):
                    expression = override

            last_run_dt = (
                datetime.fromtimestamp(job.last_run, tz=timezone.utc) if job.last_run else None
            )
            schedule = {"cron": expression}
            if matches_schedule(schedule, now=now_dt, last_run=last_run_dt):
                job.last_run = time.time()
                ctx = self._plugins[job.plugin_name].context
                task = asyncio.create_task(
                    self._run_cron_safe(job, ctx),
                    name=f"plugin-cron:{key}",
                )
                self._cron_tasks[key] = task

    async def _run_cron_safe(self, job: _CronJob, ctx: PluginContext) -> None:
        """Execute a cron job with error handling and circuit breaker."""
        key = f"{job.plugin_name}.{job.method.__name__}"
        try:
            with structlog.contextvars.bound_contextvars(
                plugin=job.plugin_name,
                component="plugin_cron",
                cron_method=job.method.__name__,
            ):
                await job.method(ctx)
            self.record_success(job.plugin_name)
            logger.debug("Plugin cron job completed: %s", key)
        except Exception as e:
            logger.error("Plugin cron job %s failed: %s", key, e, exc_info=True)
            await self.record_failure(job.plugin_name, f"Cron {key}: {e}")

    # ------------------------------------------------------------------
    # CLI / Discord Extension Accessors
    # ------------------------------------------------------------------

    def get_cli_groups(self) -> list[tuple[str, Any]]:
        """Collect CLI groups from all loaded plugins.

        Returns:
            List of (plugin_name, click.Group) tuples for plugins that
            provide CLI extensions.
        """
        result = []
        for name, loaded in self._plugins.items():
            try:
                group = loaded.instance.cli_group()
                if group is not None:
                    result.append((name, group))
            except Exception as e:
                logger.warning("Plugin '%s' cli_group() failed: %s", name, e)
        return result

    def get_discord_commands(self) -> list[Any]:
        """Collect Discord app command groups from all loaded plugins.

        Returns:
            List of ``discord.app_commands.Group`` instances from plugins
            that provide Discord extensions.
        """
        result = []
        for name, loaded in self._plugins.items():
            try:
                group = loaded.instance.discord_commands()
                if group is not None:
                    result.append(group)
            except Exception as e:
                logger.warning(
                    "Plugin '%s' discord_commands() failed: %s",
                    name,
                    e,
                )
        return result
