"""Plugin base classes, context API, and data models.

This module defines the stable API surface that plugins interact with:

- **Plugin** — Abstract base class that all plugins must subclass.
- **PluginContext** — The API object passed to plugin lifecycle methods.
  Provides command/tool registration, event emission, configuration,
  data storage, prompt management, and LLM invocation.
- **PluginInfo** — Metadata from ``pyproject.toml`` (preferred) or ``plugin.yaml``.
- **PluginStatus** — Lifecycle states for installed plugins.
- **PluginPermission** — Granular permission flags.
- **cron** — Decorator to schedule plugin methods on a cron expression.
"""

from __future__ import annotations

import abc
import logging
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
# Cron decorator
# ---------------------------------------------------------------------------


def cron(expression: str, *, config_key: str | None = None):
    """Mark a plugin method as a cron-scheduled function.

    The decorated method will be called automatically by the plugin registry
    on the schedule defined by the cron expression.  The method must accept
    a single ``PluginContext`` argument::

        @cron("0 */4 * * *")
        async def periodic_check(self, ctx: PluginContext) -> None:
            ...

    Use ``config_key`` to make the schedule user-configurable.  The
    expression becomes the default, and the plugin's instance config
    can override it at runtime without editing code or restarting::

        @cron("0 */4 * * *", config_key="check_schedule")
        async def periodic_check(self, ctx: PluginContext) -> None:
            ...

    The user can then change the schedule via
    ``aq myplugin config check_schedule="0 */2 * * *"`` and the new
    schedule takes effect on the next orchestrator tick.

    Standard 5-field cron syntax is supported (minute, hour, day-of-month,
    month, day-of-week).  See ``src/schedule.py`` for full syntax details.

    Args:
        expression: A 5-field cron expression string (default schedule).
        config_key: Optional config key whose value overrides ``expression``
                    at runtime.
    """

    def decorator(func: Callable) -> Callable:
        func._cron_expression = expression  # type: ignore[attr-defined]
        func._cron_config_key = config_key  # type: ignore[attr-defined]
        return func

    return decorator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PluginStatus(Enum):
    """Lifecycle states for an installed plugin."""

    INSTALLED = "installed"  # On disk, not yet loaded
    ACTIVE = "active"  # Loaded and running
    DISABLED = "disabled"  # Explicitly disabled by user
    ERROR = "error"  # Failed to load or crashed


class PluginPermission(Enum):
    """Granular permission flags declared in plugin.yaml."""

    NETWORK = "network"  # HTTP/socket access
    FILESYSTEM = "filesystem"  # Read/write outside plugin data dir
    DATABASE = "database"  # Direct DB queries (discouraged)
    SHELL = "shell"  # Subprocess execution


class TrustLevel(Enum):
    """Trust level controlling what services a plugin can access."""
    EXTERNAL = "external"    # Standard third-party plugins
    INTERNAL = "internal"    # Ships with the repo, full service access


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
        data_path: str | None = None,
        db: Database,
        bus: EventBus,
        command_registry: dict[str, Callable],
        tool_registry: dict[str, dict],
        event_type_registry: set[str],
        notify_callback: Callable | None = None,
        execute_command_callback: Callable | None = None,
        invoke_llm_callback: Callable | None = None,
        trust_level: TrustLevel = TrustLevel.EXTERNAL,
        services: dict[str, Any] | None = None,
        active_project_id_getter: Callable | None = None,
    ):
        self._plugin_name = plugin_name
        self._install_path = Path(install_path)
        # Plugin data lives outside the install dir so reinstalls don't nuke config
        self._data_path = Path(data_path) if data_path else self._install_path
        self._db = db
        self._bus = bus
        self._command_registry = command_registry
        self._tool_registry = tool_registry
        self._event_type_registry = event_type_registry
        self._notify_callback = notify_callback
        self._execute_command_callback = execute_command_callback
        self._invoke_llm_callback = invoke_llm_callback
        self._trust_level = trust_level
        self._services: dict[str, Any] = services or {}
        self._active_project_id_getter = active_project_id_getter

        # Ensure instance directories exist
        self._data_dir = self._data_path / "data"
        self._prompts_dir = self._data_path / "prompts"
        self._logs_dir = self._data_path / "logs"
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

    @property
    def trust_level(self) -> TrustLevel:
        """The trust level of this plugin context."""
        return self._trust_level

    @property
    def active_project_id(self) -> str | None:
        """The currently active project ID (shared conversational context)."""
        if self._active_project_id_getter:
            return self._active_project_id_getter()
        return None

    # --- Service Access ---

    def get_service(self, name: str) -> Any:
        """Get a service by name.

        Internal plugins (``TrustLevel.INTERNAL``) can access all services.
        External plugins can only access services allowed by their permissions.

        Available services for internal plugins:
        ``"git"``, ``"db"``, ``"memory"``, ``"workspace"``, ``"config"``.

        Args:
            name: Service name.

        Returns:
            The service object.

        Raises:
            ValueError: If the service is unknown.
            PermissionError: If the plugin lacks access.
        """
        service = self._services.get(name)
        if service is None:
            available = list(self._services.keys())
            raise ValueError(
                f"Unknown service: {name!r}. Available: {available}"
            )
        if self._trust_level == TrustLevel.EXTERNAL:
            raise PermissionError(
                f"Service {name!r} requires TrustLevel.INTERNAL"
            )
        return service

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

    def register_tool(self, definition: dict, *, category: str | None = None) -> None:
        """Register a tool definition (JSON Schema format).

        The tool will appear in the supervisor's tool list. Execution
        routes through CommandHandler as usual.

        Args:
            definition: Tool definition dict with 'name', 'description',
                       and 'input_schema' keys.
            category: Optional tool category for tiered loading (e.g.
                      ``"git"``, ``"files"``, ``"memory"``).  When set,
                      the tool appears in ``browse_tools``/``load_tools``
                      under this category.
        """
        name = definition.get("name", "")
        if not name:
            raise ValueError("Tool definition must have a 'name' field")
        # Tag the tool with its source plugin
        definition["_plugin"] = self._plugin_name
        if category:
            definition["_category"] = category
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
            logger.info("Plugin '%s' notification (no callback): %s", self._plugin_name, message)

    # --- LLM Invocation ---

    async def invoke_llm(
        self,
        prompt: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        tools: list[dict] | None = None,
    ) -> str:
        """Invoke the LLM with a prompt and return the response text.

        This uses the system's ChatProvider (Anthropic API or Ollama) for
        lightweight message-in/message-out LLM calls.  For heavy autonomous
        work that requires file editing, shell access, etc., use
        ``execute_command("create_task", {...})`` instead.

        Args:
            prompt: The user message to send to the LLM.
            model: Optional model override (e.g. ``"claude-opus-4-20250514"``).
                   Defaults to the system-configured model.
            provider: Optional provider override (``"anthropic"`` or ``"ollama"``).
                      Defaults to the system-configured provider.
            tools: Optional tool definitions for a tool-use loop.

        Returns:
            The LLM's response text.

        Raises:
            RuntimeError: If LLM invocation is not available (e.g. during tests).
        """
        if not self._invoke_llm_callback:
            raise RuntimeError("LLM invocation not available")
        return await self._invoke_llm_callback(
            prompt,
            self._plugin_name,
            model=model,
            provider=provider,
            tools=tools,
        )

    # --- Configuration ---

    def get_config(self) -> dict:
        """Load the plugin's instance configuration.

        Returns:
            Config dict from ``config.yaml`` in the plugin data directory.
        """
        import yaml

        config_path = self._data_path / "config.yaml"
        if config_path.exists():
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        # Fall back to install dir for backwards compatibility
        legacy_path = self._install_path / "config.yaml"
        if legacy_path.exists():
            with open(legacy_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """Get a single configuration value by key.

        Args:
            key: Config key to look up.
            default: Value to return if the key is not set.

        Returns:
            The config value, or *default*.
        """
        return self.get_config().get(key, default)

    async def save_config(self, config: dict) -> None:
        """Save the plugin's instance configuration.

        Args:
            config: Config dict to write to ``config.yaml``.
        """
        import yaml

        self._data_path.mkdir(parents=True, exist_ok=True)
        config_path = self._data_path / "config.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False)

    async def set_config_value(self, key: str, value: Any) -> None:
        """Set a single configuration value by key.

        Reads the current config, updates the key, and writes it back.

        Args:
            key: Config key to set.
            value: Value to store.
        """
        config = self.get_config()
        config[key] = value
        await self.save_config(config)

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
            raise FileNotFoundError(f"Prompt '{name}' not found in {self._prompts_dir}")

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

    Class attributes replace ``plugin.yaml`` for declaring plugin-specific
    metadata that doesn't belong in standard Python packaging::

        class MyPlugin(Plugin):
            plugin_permissions = [PluginPermission.NETWORK]
            config_schema = {"api_key": {"type": "string"}}
            default_config = {"api_key": ""}

            async def initialize(self, ctx: PluginContext) -> None:
                ctx.register_command("my_command", self.handle_command)

            async def shutdown(self, ctx: PluginContext) -> None:
                pass

            @cron("0 */4 * * *")
            async def periodic_check(self, ctx: PluginContext) -> None:
                result = await ctx.invoke_llm("Summarize events")
                await ctx.notify(result)

            async def handle_command(self, args: dict) -> dict:
                return {"result": "hello from plugin"}
    """

    # --- Class attributes (replaces plugin.yaml capability declarations) ---

    plugin_permissions: list[PluginPermission] = []
    """Permissions this plugin requires (e.g. network, filesystem, shell)."""

    config_schema: dict = {}
    """JSON Schema for plugin-specific configuration."""

    default_config: dict = {}
    """Default configuration values."""

    def __init__(self) -> None:
        # Seed self.config so CLI commands work even before initialize() runs.
        # initialize() should overwrite this with ctx.get_config().
        self.config: dict = dict(self.default_config)

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

    def cli_group(self) -> Any | None:
        """Return a Click group to mount as ``aq <plugin-name> ...``.

        Override this to add CLI commands for your plugin.  The returned
        object should be a ``click.Group`` instance::

            import click

            def cli_group(self):
                @click.group("my-plugin")
                def grp():
                    \"\"\"My plugin commands.\"\"\"
                @grp.command()
                def status():
                    click.echo("OK")
                return grp

        Returns:
            A Click group, or None if no CLI extension is provided.
        """
        return None

    def discord_commands(self) -> Any | None:
        """Return a Discord app command group to register on the bot's tree.

        Override this to add Discord slash commands for your plugin.
        The returned object should be a ``discord.app_commands.Group``,
        which namespaces your commands under ``/<group-name> <subcommand>``::

            from discord import app_commands

            def discord_commands(self):
                grp = app_commands.Group(
                    name="email", description="Email plugin commands"
                )

                @grp.command(name="check", description="Check for new emails")
                async def check(interaction):
                    await interaction.response.send_message("Checking...")

                @grp.command(name="status", description="Email plugin status")
                async def status(interaction):
                    await interaction.response.send_message("OK")

                return grp

        Returns:
            A ``discord.app_commands.Group``, or None if no Discord
            extension is provided.
        """
        return None


# ---------------------------------------------------------------------------
# InternalPlugin — base class for commands extracted from CommandHandler
# ---------------------------------------------------------------------------


class InternalPlugin(Plugin):
    """Base class for internal plugins that ship with the repository.

    Internal plugins are command groups extracted from CommandHandler.
    They receive ``TrustLevel.INTERNAL`` contexts with full service access,
    are always loaded, cannot be disabled by users, and are exempt from
    reserved name checks and circuit breaker logic.

    Subclasses must implement ``initialize()`` and ``shutdown()``::

        class FilesPlugin(InternalPlugin):
            async def initialize(self, ctx: PluginContext) -> None:
                ctx.register_command("read_file", self.cmd_read_file)
                ctx.register_tool({...}, category="files")

            async def shutdown(self, ctx: PluginContext) -> None:
                pass

            async def cmd_read_file(self, args: dict) -> dict:
                ws = ctx.get_service("workspace")
                ...
    """
    _internal: bool = True
