"""Plugin base classes, context API, and data models.

This module defines the stable API surface that plugins interact with:

- **Plugin** — Abstract base class that all plugins must subclass.
- **PluginContext** — The API object passed to plugin lifecycle methods.
  Provides command/tool registration, event emission, configuration,
  data storage, and prompt management.
- **PluginInfo** — Metadata parsed from ``plugin.yaml``.
- **PluginStatus** — Lifecycle states for installed plugins.
- **PluginPermission** — Granular permission flags.
"""

from __future__ import annotations

import abc
import logging
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from string import Template
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.database import Database
    from src.event_bus import EventBus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PluginStatus(Enum):
    """Lifecycle states for an installed plugin."""
    INSTALLED = "installed"      # On disk, not yet loaded
    ACTIVE = "active"            # Loaded and running
    DISABLED = "disabled"        # Explicitly disabled by user
    ERROR = "error"              # Failed to load or crashed


class PluginPermission(Enum):
    """Granular permission flags declared in plugin.yaml."""
    NETWORK = "network"          # HTTP/socket access
    FILESYSTEM = "filesystem"    # Read/write outside plugin data dir
    DATABASE = "database"        # Direct DB queries (discouraged)
    SHELL = "shell"              # Subprocess execution


# ---------------------------------------------------------------------------
# PluginInfo — metadata from plugin.yaml
# ---------------------------------------------------------------------------


@dataclass
class PluginInfo:
    """Metadata parsed from a plugin's ``plugin.yaml`` manifest.

    This is the static declaration of what a plugin is and what it needs.
    It does NOT change at runtime.
    """
    name: str
    version: str
    description: str = ""
    author: str = ""
    url: str = ""
    min_aq_version: str = ""
    permissions: list[PluginPermission] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    config_schema: dict = field(default_factory=dict)
    default_config: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> PluginInfo:
        """Parse a plugin.yaml dict into PluginInfo."""
        permissions = []
        for p in data.get("permissions", []):
            try:
                permissions.append(PluginPermission(p))
            except ValueError:
                logger.warning("Unknown permission '%s' in plugin.yaml", p)

        return cls(
            name=data["name"],
            version=data.get("version", "0.0.0"),
            description=data.get("description", ""),
            author=data.get("author", ""),
            url=data.get("url", ""),
            min_aq_version=data.get("min_aq_version", ""),
            permissions=permissions,
            hooks=data.get("hooks", []),
            commands=data.get("commands", []),
            tools=data.get("tools", []),
            event_types=data.get("event_types", []),
            config_schema=data.get("config_schema", {}),
            default_config=data.get("default_config", {}),
        )


# ---------------------------------------------------------------------------
# PluginContext — the stable API surface for plugins
# ---------------------------------------------------------------------------


class PluginContext:
    """API object passed to plugin lifecycle methods.

    All plugin interactions with the host system go through this context.
    Plugins cannot access other plugins' data or modify core behavior.

    The context is scoped to a single plugin instance and enforces
    isolation boundaries.
    """

    def __init__(
        self,
        *,
        plugin_name: str,
        install_path: str,
        db: Database,
        bus: EventBus,
        command_registry: dict[str, Callable],
        tool_registry: dict[str, dict],
        event_type_registry: set[str],
        notify_callback: Callable | None = None,
        execute_command_callback: Callable | None = None,
    ):
        self._plugin_name = plugin_name
        self._install_path = Path(install_path)
        self._db = db
        self._bus = bus
        self._command_registry = command_registry
        self._tool_registry = tool_registry
        self._event_type_registry = event_type_registry
        self._notify_callback = notify_callback
        self._execute_command_callback = execute_command_callback

        # Ensure instance directories exist
        self._data_dir = self._install_path / "data"
        self._prompts_dir = self._install_path / "prompts"
        self._logs_dir = self._install_path / "logs"
        for d in (self._data_dir, self._prompts_dir, self._logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def plugin_name(self) -> str:
        """The plugin's unique name."""
        return self._plugin_name

    @property
    def install_path(self) -> Path:
        """Root of the plugin's instance directory."""
        return self._install_path

    @property
    def data_dir(self) -> Path:
        """Plugin-scoped persistent data directory."""
        return self._data_dir

    # --- Command Registration ---

    def register_command(self, name: str, handler: Callable) -> None:
        """Register a command handler.

        The handler signature must be ``async def handler(args: dict) -> dict``,
        matching CommandHandler conventions.

        Args:
            name: Command name (e.g. "my_plugin_scan").
            handler: Async callable accepting args dict, returning result dict.
        """
        qualified = f"{self._plugin_name}.{name}" if "." not in name else name
        self._command_registry[qualified] = handler
        # Also register short name for convenience
        if "." not in name:
            self._command_registry[name] = handler
        logger.debug("Plugin '%s' registered command: %s", self._plugin_name, name)

    # --- Tool Registration ---

    def register_tool(self, definition: dict) -> None:
        """Register a tool definition (JSON Schema format).

        The tool will appear in the supervisor's tool list. Execution
        routes through CommandHandler as usual.

        Args:
            definition: Tool definition dict with 'name', 'description',
                       and 'input_schema' keys.
        """
        name = definition.get("name", "")
        if not name:
            raise ValueError("Tool definition must have a 'name' field")
        # Tag the tool with its source plugin
        definition["_plugin"] = self._plugin_name
        self._tool_registry[name] = definition
        logger.debug("Plugin '%s' registered tool: %s", self._plugin_name, name)

    # --- Event System ---

    def register_event_type(self, event_type: str) -> None:
        """Declare a custom event type this plugin may emit.

        This is informational — the EventBus is freeform and doesn't
        require pre-registration. However, declaring event types helps
        with discoverability and documentation.

        Args:
            event_type: Event type string (e.g. "my_plugin.scan_complete").
        """
        self._event_type_registry.add(event_type)
        logger.debug("Plugin '%s' registered event type: %s", self._plugin_name, event_type)

    async def emit_event(self, event_type: str, data: dict | None = None) -> None:
        """Emit an event on the system EventBus.

        Args:
            event_type: Event type string.
            data: Optional event payload dict.
        """
        payload = dict(data) if data else {}
        payload["_plugin"] = self._plugin_name
        await self._bus.emit(event_type, payload)

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """Subscribe to events on the system EventBus.

        Args:
            event_type: Event type to listen for. Use "*" for all events.
            handler: Async or sync callable receiving event data dict.
        """
        self._bus.subscribe(event_type, handler)

    # --- Command Execution ---

    async def execute_command(self, name: str, args: dict | None = None) -> dict:
        """Execute a system command by name.

        This allows plugins to invoke core commands (e.g. create_task)
        without importing internal modules.

        Args:
            name: Command name.
            args: Command arguments dict.

        Returns:
            Command result dict.
        """
        if self._execute_command_callback:
            return await self._execute_command_callback(name, args or {})
        return {"error": "Command execution not available"}

    # --- Notifications ---

    async def notify(self, message: str, project_id: str | None = None) -> None:
        """Send a notification through the system's notification channel.

        Args:
            message: Notification text.
            project_id: Optional project to scope the notification to.
        """
        if self._notify_callback:
            await self._notify_callback(message, project_id=project_id)
        else:
            logger.info("Plugin '%s' notification (no callback): %s",
                       self._plugin_name, message)

    # --- Configuration ---

    def get_config(self) -> dict:
        """Load the plugin's instance configuration.

        Returns:
            Config dict from ``config.yaml`` in the install directory.
        """
        import yaml

        config_path = self._install_path / "config.yaml"
        if config_path.exists():
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    async def save_config(self, config: dict) -> None:
        """Save the plugin's instance configuration.

        Args:
            config: Config dict to write to ``config.yaml``.
        """
        import yaml

        config_path = self._install_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False)

    # --- Persistent Data (DB-backed) ---

    async def get_data(self, key: str) -> Any:
        """Retrieve a plugin-scoped value from the database.

        Args:
            key: Data key.

        Returns:
            The stored value (deserialized from JSON), or None.
        """
        row = await self._db.get_plugin_data(self._plugin_name, key)
        return row

    async def set_data(self, key: str, value: Any) -> None:
        """Store a plugin-scoped value in the database.

        Args:
            key: Data key.
            value: Value to store (must be JSON-serializable).
        """
        await self._db.set_plugin_data(self._plugin_name, key, value)

    async def delete_data(self, key: str) -> None:
        """Delete a plugin-scoped value from the database.

        Args:
            key: Data key to delete.
        """
        await self._db.delete_plugin_data(self._plugin_name, key)

    # --- Prompt Management ---

    def get_prompt(self, name: str, variables: dict | None = None) -> str:
        """Load a prompt template and optionally substitute variables.

        Prompts are stored as text files in the plugin's ``prompts/``
        directory. Variable substitution uses Python's ``string.Template``
        (``$var`` or ``${var}`` syntax).

        Args:
            name: Prompt template name (without extension).
            variables: Optional dict of template variables.

        Returns:
            The prompt text with variables substituted.

        Raises:
            FileNotFoundError: If the prompt template doesn't exist.
        """
        prompt_path = self._prompts_dir / f"{name}.md"
        if not prompt_path.exists():
            prompt_path = self._prompts_dir / f"{name}.txt"
        if not prompt_path.exists():
            raise FileNotFoundError(
                f"Prompt '{name}' not found in {self._prompts_dir}"
            )

        text = prompt_path.read_text()
        if variables:
            text = Template(text).safe_substitute(variables)
        return text

    async def save_prompt(self, name: str, content: str) -> None:
        """Save or update a prompt template.

        Args:
            name: Prompt template name (without extension).
            content: Prompt template content.
        """
        prompt_path = self._prompts_dir / f"{name}.md"
        prompt_path.write_text(content)

    def list_prompts(self) -> list[str]:
        """List available prompt template names.

        Returns:
            List of prompt names (without extensions).
        """
        prompts = []
        if self._prompts_dir.exists():
            for f in self._prompts_dir.iterdir():
                if f.is_file() and f.suffix in (".md", ".txt"):
                    prompts.append(f.stem)
        return sorted(prompts)


# ---------------------------------------------------------------------------
# Plugin ABC
# ---------------------------------------------------------------------------


class Plugin(abc.ABC):
    """Abstract base class that all plugins must subclass.

    Plugins implement lifecycle methods that the registry calls during
    installation, loading, and shutdown. The ``PluginContext`` provides
    the API for registering capabilities and interacting with the system.

    Minimal example::

        class MyPlugin(Plugin):
            async def initialize(self, ctx: PluginContext) -> None:
                ctx.register_command("my_command", self.handle_command)

            async def shutdown(self, ctx: PluginContext) -> None:
                pass  # Cleanup resources

            async def handle_command(self, args: dict) -> dict:
                return {"result": "hello from plugin"}
    """

    @abc.abstractmethod
    async def initialize(self, ctx: PluginContext) -> None:
        """Called when the plugin is loaded.

        Use this to register commands, tools, event types, and
        subscribe to events. The context is fully ready for use.

        Args:
            ctx: The plugin's context API object.
        """
        ...

    @abc.abstractmethod
    async def shutdown(self, ctx: PluginContext) -> None:
        """Called when the plugin is being unloaded or disabled.

        Clean up any resources, cancel background tasks, etc.

        Args:
            ctx: The plugin's context API object.
        """
        ...

    async def on_config_changed(self, ctx: PluginContext, config: dict) -> None:
        """Called when the plugin's configuration is updated.

        Override this to react to config changes without a full reload.

        Args:
            ctx: The plugin's context API object.
            config: The new configuration dict.
        """
        pass
