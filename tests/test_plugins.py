"""Tests for the plugin system: base classes, loader, registry, and DB queries."""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.plugins.base import (
    Plugin,
    PluginContext,
    PluginInfo,
    PluginPermission,
    PluginStatus,
)
from src.plugins.loader import (
    import_plugin_module,
    install_requirements,
    parse_plugin_yaml,
    reset_prompts,
    setup_prompts,
)
from src.plugins.registry import PluginRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    """Create a minimal plugin directory structure."""
    src = tmp_path / "src"
    src.mkdir()

    # plugin.yaml
    (src / "plugin.yaml").write_text(textwrap.dedent("""\
        name: test-plugin
        version: "1.0.0"
        description: A test plugin
        author: Test Author
        permissions:
          - network
        commands:
          - greet
        tools:
          - greet_tool
        event_types:
          - test.greeting
        default_config:
          greeting: hello
    """))

    # plugin.py
    (src / "plugin.py").write_text(textwrap.dedent("""\
        from src.plugins.base import Plugin, PluginContext


        class TestPlugin(Plugin):
            async def initialize(self, ctx: PluginContext) -> None:
                ctx.register_command("greet", self.handle_greet)
                ctx.register_tool({
                    "name": "greet_tool",
                    "description": "Greet someone",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                        },
                    },
                })
                ctx.register_event_type("test.greeting")

            async def shutdown(self, ctx: PluginContext) -> None:
                pass

            async def handle_greet(self, args: dict) -> dict:
                name = args.get("name", "world")
                return {"greeting": f"Hello, {name}!"}
    """))

    # prompts
    prompts = src / "prompts"
    prompts.mkdir()
    (prompts / "greeting.md").write_text("Hello $name, welcome to $plugin!")

    return tmp_path


@pytest.fixture
def mock_db():
    """Create a mock database with plugin methods."""
    db = AsyncMock()
    db.get_plugin = AsyncMock(return_value=None)
    db.create_plugin = AsyncMock()
    db.update_plugin = AsyncMock()
    db.delete_plugin = AsyncMock()
    db.list_plugins = AsyncMock(return_value=[])
    db.get_plugin_data = AsyncMock(return_value=None)
    db.set_plugin_data = AsyncMock()
    db.delete_plugin_data = AsyncMock()
    db.delete_plugin_data_all = AsyncMock()
    return db


@pytest.fixture
def mock_bus():
    """Create a mock EventBus."""
    bus = MagicMock()
    bus.emit = AsyncMock()
    bus.subscribe = MagicMock()
    return bus


@pytest.fixture
def mock_config(tmp_path: Path):
    """Create a mock config with data_dir."""
    config = MagicMock()
    config.data_dir = str(tmp_path / "data")
    os.makedirs(config.data_dir, exist_ok=True)
    return config


# ---------------------------------------------------------------------------
# PluginInfo Tests
# ---------------------------------------------------------------------------


class TestPluginInfo:
    def test_from_dict_basic(self):
        info = PluginInfo.from_dict({
            "name": "my-plugin",
            "version": "2.0.0",
            "description": "A great plugin",
        })
        assert info.name == "my-plugin"
        assert info.version == "2.0.0"
        assert info.description == "A great plugin"
        assert info.permissions == []

    def test_from_dict_with_permissions(self):
        info = PluginInfo.from_dict({
            "name": "my-plugin",
            "permissions": ["network", "filesystem"],
        })
        assert PluginPermission.NETWORK in info.permissions
        assert PluginPermission.FILESYSTEM in info.permissions

    def test_from_dict_unknown_permission_ignored(self):
        info = PluginInfo.from_dict({
            "name": "my-plugin",
            "permissions": ["network", "teleport"],
        })
        assert len(info.permissions) == 1
        assert PluginPermission.NETWORK in info.permissions

    def test_from_dict_defaults(self):
        info = PluginInfo.from_dict({"name": "minimal"})
        assert info.version == "0.0.0"
        assert info.description == ""
        assert info.author == ""
        assert info.hooks == []
        assert info.commands == []
        assert info.tools == []
        assert info.default_config == {}


# ---------------------------------------------------------------------------
# PluginContext Tests
# ---------------------------------------------------------------------------


class TestPluginContext:
    def test_register_command(self, plugin_dir: Path, mock_db, mock_bus):
        commands = {}
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry=commands,
            tool_registry={},
            event_type_registry=set(),
        )

        async def handler(args):
            return {"ok": True}

        ctx.register_command("greet", handler)
        assert "greet" in commands
        assert "test-plugin.greet" in commands

    def test_register_tool(self, plugin_dir: Path, mock_db, mock_bus):
        tools = {}
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry=tools,
            event_type_registry=set(),
        )

        ctx.register_tool({
            "name": "my_tool",
            "description": "Does something",
            "input_schema": {"type": "object"},
        })
        assert "my_tool" in tools
        assert tools["my_tool"]["_plugin"] == "test-plugin"

    def test_register_tool_missing_name_raises(self, plugin_dir, mock_db, mock_bus):
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        with pytest.raises(ValueError, match="'name' field"):
            ctx.register_tool({"description": "No name"})

    def test_register_event_type(self, plugin_dir, mock_db, mock_bus):
        events = set()
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=events,
        )

        ctx.register_event_type("test.event")
        assert "test.event" in events

    @pytest.mark.asyncio
    async def test_emit_event(self, plugin_dir, mock_db, mock_bus):
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )

        await ctx.emit_event("test.event", {"key": "value"})
        mock_bus.emit.assert_called_once()
        call_args = mock_bus.emit.call_args
        assert call_args[0][0] == "test.event"
        assert call_args[0][1]["key"] == "value"
        assert call_args[0][1]["_plugin"] == "test-plugin"

    @pytest.mark.asyncio
    async def test_execute_command(self, plugin_dir, mock_db, mock_bus):
        callback = AsyncMock(return_value={"result": "ok"})
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
            execute_command_callback=callback,
        )

        result = await ctx.execute_command("list_tasks", {"project": "test"})
        assert result == {"result": "ok"}
        callback.assert_called_once_with("list_tasks", {"project": "test"})

    def test_get_config(self, plugin_dir, mock_db, mock_bus):
        # Write a config file
        import yaml
        config_path = plugin_dir / "config.yaml"
        config_path.write_text(yaml.dump({"greeting": "hi", "count": 5}))

        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )

        config = ctx.get_config()
        assert config["greeting"] == "hi"
        assert config["count"] == 5

    def test_get_config_missing_file(self, plugin_dir, mock_db, mock_bus):
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        assert ctx.get_config() == {}

    def test_prompt_management(self, plugin_dir, mock_db, mock_bus):
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )

        # Setup prompts first
        setup_prompts(str(plugin_dir))

        prompts = ctx.list_prompts()
        assert "greeting" in prompts

        text = ctx.get_prompt("greeting", {"name": "Alice", "plugin": "test"})
        assert "Alice" in text
        assert "test" in text

    def test_get_prompt_not_found(self, plugin_dir, mock_db, mock_bus):
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        with pytest.raises(FileNotFoundError):
            ctx.get_prompt("nonexistent")

    @pytest.mark.asyncio
    async def test_data_operations(self, plugin_dir, mock_db, mock_bus):
        mock_db.get_plugin_data = AsyncMock(return_value=42)

        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )

        await ctx.set_data("counter", 42)
        mock_db.set_plugin_data.assert_called_once_with("test-plugin", "counter", 42)

        val = await ctx.get_data("counter")
        assert val == 42

        await ctx.delete_data("counter")
        mock_db.delete_plugin_data.assert_called_once_with("test-plugin", "counter")

    def test_directories_created(self, plugin_dir, mock_db, mock_bus):
        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        assert (plugin_dir / "data").is_dir()
        assert (plugin_dir / "prompts").is_dir()
        assert (plugin_dir / "logs").is_dir()


# ---------------------------------------------------------------------------
# Loader Tests
# ---------------------------------------------------------------------------


class TestLoader:
    def test_parse_plugin_yaml(self, plugin_dir: Path):
        info = parse_plugin_yaml(str(plugin_dir))
        assert info.name == "test-plugin"
        assert info.version == "1.0.0"
        assert info.description == "A test plugin"
        assert PluginPermission.NETWORK in info.permissions

    def test_parse_plugin_yaml_not_found(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        with pytest.raises(FileNotFoundError):
            parse_plugin_yaml(str(tmp_path))

    def test_parse_plugin_yaml_no_name(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "plugin.yaml").write_text("version: '1.0.0'\n")
        with pytest.raises(ValueError, match="missing required 'name'"):
            parse_plugin_yaml(str(tmp_path))

    def test_import_plugin_module(self, plugin_dir: Path):
        plugin_class = import_plugin_module(str(plugin_dir))
        assert issubclass(plugin_class, Plugin)
        assert plugin_class.__name__ == "TestPlugin"

    def test_import_plugin_module_not_found(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        with pytest.raises(FileNotFoundError):
            import_plugin_module(str(tmp_path))

    def test_import_plugin_module_no_subclass(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "plugin.py").write_text("class NotAPlugin:\n    pass\n")
        with pytest.raises(ValueError, match="No Plugin subclass"):
            import_plugin_module(str(tmp_path))

    def test_setup_prompts_nondestructive(self, plugin_dir: Path):
        # First setup
        setup_prompts(str(plugin_dir))
        prompt_file = plugin_dir / "prompts" / "greeting.md"
        assert prompt_file.exists()

        # Modify the prompt
        prompt_file.write_text("Custom content")

        # Second setup should NOT overwrite
        setup_prompts(str(plugin_dir))
        assert prompt_file.read_text() == "Custom content"

    def test_reset_prompts_overwrites(self, plugin_dir: Path):
        setup_prompts(str(plugin_dir))
        prompt_file = plugin_dir / "prompts" / "greeting.md"
        prompt_file.write_text("Custom content")

        count = reset_prompts(str(plugin_dir))
        assert count == 1
        assert "Custom content" not in prompt_file.read_text()

    def test_install_requirements_no_file(self, plugin_dir: Path):
        # No requirements.txt → should return True
        assert install_requirements(str(plugin_dir)) is True


# ---------------------------------------------------------------------------
# Registry Tests
# ---------------------------------------------------------------------------


class TestPluginRegistry:
    @pytest.mark.asyncio
    async def test_load_plugin(self, plugin_dir, mock_db, mock_bus, mock_config):
        # Set up the plugins directory to contain our test plugin
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)

        # Symlink our test plugin into the plugins directory
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(
            db=mock_db,
            bus=mock_bus,
            config=mock_config,
        )

        await registry.load_plugin("test-plugin")

        assert registry.is_loaded("test-plugin")
        assert registry.get_command("greet") is not None
        assert len(registry.get_all_tool_definitions()) == 1
        assert "test.greeting" in registry.get_registered_event_types()

    @pytest.mark.asyncio
    async def test_unload_plugin(self, plugin_dir, mock_db, mock_bus, mock_config):
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("test-plugin")
        assert registry.is_loaded("test-plugin")

        await registry.unload_plugin("test-plugin")
        assert not registry.is_loaded("test-plugin")
        assert registry.get_command("greet") is None
        assert len(registry.get_all_tool_definitions()) == 0

    @pytest.mark.asyncio
    async def test_reload_plugin(self, plugin_dir, mock_db, mock_bus, mock_config):
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("test-plugin")
        await registry.reload_plugin("test-plugin")

        assert registry.is_loaded("test-plugin")
        assert registry.get_command("greet") is not None

    @pytest.mark.asyncio
    async def test_list_plugins(self, plugin_dir, mock_db, mock_bus, mock_config):
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("test-plugin")

        plugins = registry.list_plugins()
        assert len(plugins) == 1
        assert plugins[0]["name"] == "test-plugin"
        assert plugins[0]["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_get_plugin_detail(self, plugin_dir, mock_db, mock_bus, mock_config):
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("test-plugin")

        detail = registry.get_plugin("test-plugin")
        assert detail is not None
        assert detail["name"] == "test-plugin"
        assert detail["description"] == "A test plugin"
        assert "network" in detail["permissions"]

    @pytest.mark.asyncio
    async def test_circuit_breaker(self, plugin_dir, mock_db, mock_bus, mock_config):
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("test-plugin")

        # Record failures up to threshold
        for i in range(4):
            await registry.record_failure("test-plugin", f"Error {i}")
            assert registry.is_loaded("test-plugin")

        # 5th failure should auto-disable
        await registry.record_failure("test-plugin", "Error 4")
        assert not registry.is_loaded("test-plugin")

    @pytest.mark.asyncio
    async def test_record_success_resets_counter(self, plugin_dir, mock_db, mock_bus, mock_config):
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("test-plugin")

        # Record some failures
        for i in range(3):
            await registry.record_failure("test-plugin", f"Error {i}")

        # Success resets counter
        registry.record_success("test-plugin")

        # Now 4 more failures shouldn't trigger disable
        for i in range(4):
            await registry.record_failure("test-plugin", f"Error {i}")
        assert registry.is_loaded("test-plugin")

    @pytest.mark.asyncio
    async def test_plugin_not_found(self, mock_db, mock_bus, mock_config):
        mock_db.get_plugin.return_value = None

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)

        with pytest.raises(FileNotFoundError):
            await registry.load_plugin("nonexistent")

    @pytest.mark.asyncio
    async def test_discover_plugins(self, plugin_dir, mock_db, mock_bus, mock_config):
        # Set up plugins dir with our test plugin
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        discovered = await registry.discover_plugins()

        assert "test-plugin" in discovered

    @pytest.mark.asyncio
    async def test_command_execution_through_context(
        self, plugin_dir, mock_db, mock_bus, mock_config,
    ):
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("test-plugin")

        # Execute the plugin's command through the registry
        handler = registry.get_command("greet")
        assert handler is not None

        result = await handler({"name": "Alice"})
        assert result == {"greeting": "Hello, Alice!"}

    @pytest.mark.asyncio
    async def test_disable_enable(self, plugin_dir, mock_db, mock_bus, mock_config):
        plugins_dir = Path(mock_config.data_dir) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "test-plugin"
        if not target.exists():
            target.symlink_to(plugin_dir)

        mock_db.get_plugin.return_value = {
            "id": "test-plugin",
            "install_path": str(plugin_dir),
            "status": "installed",
        }

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("test-plugin")
        assert registry.is_loaded("test-plugin")

        await registry.disable_plugin("test-plugin")
        assert not registry.is_loaded("test-plugin")
        mock_db.update_plugin.assert_called()

        await registry.enable_plugin("test-plugin")
        assert registry.is_loaded("test-plugin")


# ---------------------------------------------------------------------------
# Database Plugin Queries Tests (with real SQLite)
# ---------------------------------------------------------------------------


class TestPluginDatabaseQueries:
    @pytest.mark.asyncio
    async def test_plugin_crud(self, tmp_path: Path):
        from src.database import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        try:
            # Create
            await db.create_plugin(
                plugin_id="test-plugin",
                version="1.0.0",
                source_url="https://github.com/test/plugin",
                source_rev="abc123",
                install_path="/tmp/plugins/test-plugin",
                status="installed",
                config='{"key": "value"}',
                permissions='["network"]',
            )

            # Read
            p = await db.get_plugin("test-plugin")
            assert p is not None
            assert p["id"] == "test-plugin"
            assert p["version"] == "1.0.0"
            assert p["status"] == "installed"

            # List
            plugins = await db.list_plugins()
            assert len(plugins) == 1

            # Update
            await db.update_plugin("test-plugin", status="active", version="1.1.0")
            p = await db.get_plugin("test-plugin")
            assert p["status"] == "active"
            assert p["version"] == "1.1.0"

            # Delete
            await db.delete_plugin("test-plugin")
            p = await db.get_plugin("test-plugin")
            assert p is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_plugin_data_crud(self, tmp_path: Path):
        from src.database import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        try:
            # Create plugin first
            await db.create_plugin(
                plugin_id="test-plugin",
                version="1.0.0",
            )

            # Set data
            await db.set_plugin_data("test-plugin", "counter", 42)
            await db.set_plugin_data("test-plugin", "config", {"nested": True})

            # Get data
            val = await db.get_plugin_data("test-plugin", "counter")
            assert val == 42

            val = await db.get_plugin_data("test-plugin", "config")
            assert val == {"nested": True}

            # Update data (upsert)
            await db.set_plugin_data("test-plugin", "counter", 100)
            val = await db.get_plugin_data("test-plugin", "counter")
            assert val == 100

            # List all data
            all_data = await db.list_plugin_data("test-plugin")
            assert "counter" in all_data
            assert "config" in all_data

            # Delete single
            await db.delete_plugin_data("test-plugin", "counter")
            val = await db.get_plugin_data("test-plugin", "counter")
            assert val is None

            # Delete all
            await db.delete_plugin_data_all("test-plugin")
            all_data = await db.list_plugin_data("test-plugin")
            assert len(all_data) == 0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_list_plugins_with_filter(self, tmp_path: Path):
        from src.database import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        try:
            await db.create_plugin(plugin_id="p1", version="1.0", status="active")
            await db.create_plugin(plugin_id="p2", version="2.0", status="disabled")
            await db.create_plugin(plugin_id="p3", version="3.0", status="active")

            active = await db.list_plugins(status="active")
            assert len(active) == 2

            disabled = await db.list_plugins(status="disabled")
            assert len(disabled) == 1
            assert disabled[0]["id"] == "p2"
        finally:
            await db.close()
