"""Tests for the plugin system: base classes, loader, registry, and DB queries."""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.plugins.base import (
    Plugin,
    PluginContext,
    PluginInfo,
    PluginPermission,
    PluginStatus,
)
from src.plugins.base import cron
from src.plugins.loader import (
    has_pyproject,
    import_plugin_module,
    install_requirements,
    load_plugin_via_entry_point,
    parse_plugin_metadata,
    parse_plugin_yaml,
    parse_pyproject_metadata,
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
    (src / "plugin.yaml").write_text(
        textwrap.dedent("""\
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
    """)
    )

    # plugin.py
    (src / "plugin.py").write_text(
        textwrap.dedent("""\
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
    """)
    )

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


@pytest.mark.asyncio
async def test_install_from_git_reserved_name_leaves_no_residue(
    mock_db, mock_bus, mock_config, monkeypatch
):
    """A metadata-derived reserved name must not leave a discoverable clone."""
    registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
    rejected_path = registry._plugins_dir / "task"
    rejected_path.mkdir()
    (rejected_path / "plugin.yaml").write_text("name: task\n")

    async def fake_install(*args, **kwargs):
        return {
            "name": "task",
            "version": "1",
            "source_rev": "rev",
            "install_path": str(rejected_path),
            "default_config": {},
            "permissions": [],
        }

    monkeypatch.setattr("src.plugins.registry.install_plugin_from_url", fake_install)

    with pytest.raises(ValueError, match="reserved"):
        await registry.install_from_git("https://example.invalid/task.git")

    assert not rejected_path.exists()
    assert "task" not in await registry.discover_plugins()


@pytest.mark.asyncio
async def test_enable_plugin_failed_load_sets_error_status(
    mock_db, mock_bus, mock_config, monkeypatch
):
    registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)

    async def fail_load(name):
        raise RuntimeError("initialize failed")

    monkeypatch.setattr(registry, "load_plugin", fail_load)
    with pytest.raises(RuntimeError, match="initialize failed"):
        await registry.enable_plugin("broken")

    mock_db.update_plugin.assert_awaited_once_with("broken", status=PluginStatus.ERROR.value)
    assert "broken" not in registry._plugins


# ---------------------------------------------------------------------------
# PluginInfo Tests
# ---------------------------------------------------------------------------


class TestPluginInfo:
    def test_from_dict_basic(self):
        info = PluginInfo.from_dict(
            {
                "name": "my-plugin",
                "version": "2.0.0",
                "description": "A great plugin",
            }
        )
        assert info.name == "my-plugin"
        assert info.version == "2.0.0"
        assert info.description == "A great plugin"
        assert info.permissions == []

    def test_from_dict_with_permissions(self):
        info = PluginInfo.from_dict(
            {
                "name": "my-plugin",
                "permissions": ["network", "filesystem"],
            }
        )
        assert PluginPermission.NETWORK in info.permissions
        assert PluginPermission.FILESYSTEM in info.permissions

    def test_from_dict_unknown_permission_ignored(self):
        info = PluginInfo.from_dict(
            {
                "name": "my-plugin",
                "permissions": ["network", "teleport"],
            }
        )
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

        ctx.register_tool(
            {
                "name": "my_tool",
                "description": "Does something",
                "input_schema": {"type": "object"},
            }
        )
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

    @pytest.mark.asyncio
    async def test_get_config(self, plugin_dir, mock_db, mock_bus):
        mock_db.get_plugin = AsyncMock(
            return_value={"config": json.dumps({"greeting": "hi", "count": 5})}
        )

        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        await ctx.load_config()

        config = ctx.get_config()
        assert config["greeting"] == "hi"
        assert config["count"] == 5

    @pytest.mark.asyncio
    async def test_get_config_no_db_record(self, plugin_dir, mock_db, mock_bus):
        mock_db.get_plugin = AsyncMock(return_value=None)

        ctx = PluginContext(
            plugin_name="test-plugin",
            install_path=str(plugin_dir),
            db=mock_db,
            bus=mock_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        await ctx.load_config()
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
        PluginContext(
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
        self,
        plugin_dir,
        mock_db,
        mock_bus,
        mock_config,
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


# ---------------------------------------------------------------------------
# @cron Decorator Tests
# ---------------------------------------------------------------------------


class TestCronDecorator:
    def test_cron_sets_attribute(self):
        """The @cron decorator stores the expression on the function."""

        @cron("0 */4 * * *")
        async def my_job(ctx):
            pass

        assert hasattr(my_job, "_cron_expression")
        assert my_job._cron_expression == "0 */4 * * *"
        assert my_job._cron_config_key is None

    def test_cron_with_config_key(self):
        """The @cron decorator stores the config_key."""

        @cron("0 */4 * * *", config_key="check_schedule")
        async def my_job(ctx):
            pass

        assert my_job._cron_expression == "0 */4 * * *"
        assert my_job._cron_config_key == "check_schedule"

    def test_cron_on_method(self):
        """The @cron decorator works on class methods."""

        class MyPlugin(Plugin):
            plugin_permissions = []

            async def initialize(self, ctx):
                pass

            async def shutdown(self, ctx):
                pass

            @cron("30 2 * * 1-5")
            async def weekday_check(self, ctx):
                pass

        instance = MyPlugin()
        assert hasattr(instance.weekday_check, "_cron_expression")
        assert instance.weekday_check._cron_expression == "30 2 * * 1-5"

    def test_cron_preserves_function(self):
        """The @cron decorator doesn't alter function identity or callability."""

        @cron("0 0 * * *")
        async def midnight_job(ctx):
            return "done"

        assert midnight_job.__name__ == "midnight_job"
        assert asyncio.iscoroutinefunction(midnight_job)


# ---------------------------------------------------------------------------
# Plugin Class Attributes Tests
# ---------------------------------------------------------------------------


class TestPluginClassAttributes:
    def test_default_class_attrs(self):
        """Plugin subclass inherits empty defaults for class attributes."""

        class MinimalPlugin(Plugin):
            async def initialize(self, ctx):
                pass

            async def shutdown(self, ctx):
                pass

        assert MinimalPlugin.plugin_permissions == []
        assert MinimalPlugin.config_schema == {}
        assert MinimalPlugin.default_config == {}

    def test_custom_class_attrs(self):
        """Plugin subclass can override class attributes."""

        class CustomPlugin(Plugin):
            plugin_permissions = [PluginPermission.NETWORK, PluginPermission.SHELL]
            config_schema = {"api_key": {"type": "string"}}
            default_config = {"api_key": ""}

            async def initialize(self, ctx):
                pass

            async def shutdown(self, ctx):
                pass

        assert PluginPermission.NETWORK in CustomPlugin.plugin_permissions
        assert PluginPermission.SHELL in CustomPlugin.plugin_permissions
        assert CustomPlugin.config_schema == {"api_key": {"type": "string"}}
        assert CustomPlugin.default_config == {"api_key": ""}

    def test_cli_group_default_none(self):
        """Default cli_group() returns None."""

        class MinimalPlugin(Plugin):
            async def initialize(self, ctx):
                pass

            async def shutdown(self, ctx):
                pass

        assert MinimalPlugin().cli_group() is None

    def test_discord_commands_default_none(self):
        """Default discord_commands() returns None."""

        class MinimalPlugin(Plugin):
            async def initialize(self, ctx):
                pass

            async def shutdown(self, ctx):
                pass

        assert MinimalPlugin().discord_commands() is None


# ---------------------------------------------------------------------------
# Config Helpers Tests
# ---------------------------------------------------------------------------


class TestConfigHelpers:
    @pytest.mark.asyncio
    async def test_get_config_value(self, tmp_path):
        """get_config_value reads a single key."""

        db = AsyncMock()
        db.get_plugin = AsyncMock(
            return_value={"config": json.dumps({"server": "imap.example.com", "port": 993})}
        )

        ctx = PluginContext(
            plugin_name="test",
            install_path=str(tmp_path),
            db=db,
            bus=MagicMock(),
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        await ctx.load_config()
        assert ctx.get_config_value("server") == "imap.example.com"
        assert ctx.get_config_value("port") == 993
        assert ctx.get_config_value("missing") is None
        assert ctx.get_config_value("missing", default="fallback") == "fallback"

    @pytest.mark.asyncio
    async def test_set_config_value(self, tmp_path):
        """set_config_value updates a single key without clobbering others."""

        db = AsyncMock()
        db.get_plugin = AsyncMock(
            return_value={"config": json.dumps({"server": "imap.example.com", "port": 993})}
        )
        db.update_plugin = AsyncMock()

        ctx = PluginContext(
            plugin_name="test",
            install_path=str(tmp_path),
            db=db,
            bus=MagicMock(),
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        await ctx.load_config()
        await ctx.set_config_value("port", 465)
        assert ctx.get_config_value("port") == 465
        assert ctx.get_config_value("server") == "imap.example.com"  # untouched

    @pytest.mark.asyncio
    async def test_set_config_value_no_existing_config(self, tmp_path):
        """set_config_value works when no config exists yet."""
        db = AsyncMock()
        db.get_plugin = AsyncMock(return_value=None)
        db.update_plugin = AsyncMock()

        ctx = PluginContext(
            plugin_name="test",
            install_path=str(tmp_path),
            db=db,
            bus=MagicMock(),
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        await ctx.load_config()
        await ctx.set_config_value("schedule", "0 */2 * * *")
        assert ctx.get_config_value("schedule") == "0 */2 * * *"


# ---------------------------------------------------------------------------
# PluginContext.invoke_llm Tests
# ---------------------------------------------------------------------------


class TestInvokeLLM:
    @pytest.mark.asyncio
    async def test_invoke_llm_calls_callback(self, tmp_path):
        callback = AsyncMock(return_value="LLM response")
        ctx = PluginContext(
            plugin_name="test",
            install_path=str(tmp_path),
            db=AsyncMock(),
            bus=MagicMock(),
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
            invoke_llm_callback=callback,
        )
        assert await ctx.invoke_llm("What is 2+2?") == "LLM response"
        callback.assert_called_once_with(
            "What is 2+2?",
            "test",
            intelligence_class=None,
            model=None,
            provider=None,
            tools=None,
            system="",
        )

    @pytest.mark.asyncio
    async def test_invoke_llm_passes_overrides(self, tmp_path):
        callback = AsyncMock(return_value="ok")
        ctx = PluginContext(
            plugin_name="test",
            install_path=str(tmp_path),
            db=AsyncMock(),
            bus=MagicMock(),
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
            invoke_llm_callback=callback,
        )
        tools = [{"name": "t", "input_schema": {"type": "object"}}]
        await ctx.invoke_llm(
            "p",
            intelligence_class="fast-low",
            model="m",
            provider="google",
            tools=tools,
            system="s",
        )
        callback.assert_called_once_with(
            "p",
            "test",
            intelligence_class="fast-low",
            model="m",
            provider="google",
            tools=tools,
            system="s",
        )

    @pytest.mark.asyncio
    async def test_invoke_llm_raises_without_callback(self, tmp_path):
        """invoke_llm raises RuntimeError if no callback is configured."""
        ctx = PluginContext(
            plugin_name="test",
            install_path=str(tmp_path),
            db=AsyncMock(),
            bus=MagicMock(),
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
        )
        with pytest.raises(RuntimeError, match="LLM invocation not available"):
            await ctx.invoke_llm("hello")


# ---------------------------------------------------------------------------
# pyproject.toml Loader Tests
# ---------------------------------------------------------------------------


class TestPyprojectLoader:
    def test_has_pyproject_true(self, tmp_path):
        """has_pyproject returns True when pyproject.toml has aq.plugins entry."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "aq-test"
            version = "1.0.0"

            [project.entry-points."aq.plugins"]
            test = "test_mod:TestPlugin"
        """)
        )
        assert has_pyproject(str(tmp_path)) is True

    def test_has_pyproject_false_no_entry_point(self, tmp_path):
        """has_pyproject returns False when no aq.plugins entry point."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "aq-test"
            version = "1.0.0"
        """)
        )
        assert has_pyproject(str(tmp_path)) is False

    def test_has_pyproject_false_no_file(self, tmp_path):
        """has_pyproject returns False when pyproject.toml doesn't exist."""
        src = tmp_path / "src"
        src.mkdir()
        assert has_pyproject(str(tmp_path)) is False

    def test_parse_pyproject_metadata(self, tmp_path):
        """parse_pyproject_metadata reads name/version/description/author."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "aq-email-reviewer"
            version = "2.1.0"
            description = "Email review plugin"
            authors = [{name = "David"}]

            [project.entry-points."aq.plugins"]
            email-reviewer = "email_reviewer:Plugin"
        """)
        )
        meta = parse_pyproject_metadata(str(tmp_path))
        assert meta["name"] == "email-reviewer"
        assert meta["version"] == "2.1.0"
        assert meta["description"] == "Email review plugin"
        assert meta["author"] == "David"

    def test_parse_pyproject_metadata_missing_raises(self, tmp_path):
        """parse_pyproject_metadata raises FileNotFoundError if no file."""
        src = tmp_path / "src"
        src.mkdir()
        with pytest.raises(FileNotFoundError):
            parse_pyproject_metadata(str(tmp_path))

    def test_parse_plugin_metadata_from_class(self, tmp_path):
        """parse_plugin_metadata merges package data with class attributes."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "aq-test-plugin"
            version = "3.0.0"
            description = "From pyproject"

            [project.entry-points."aq.plugins"]
            test = "test:TestPlugin"
        """)
        )

        class TestPlugin(Plugin):
            plugin_permissions = [PluginPermission.NETWORK]
            config_schema = {"key": {"type": "string"}}
            default_config = {"key": "val"}

            async def initialize(self, ctx):
                pass

            async def shutdown(self, ctx):
                pass

        info = parse_plugin_metadata(str(tmp_path), TestPlugin)
        assert info.name == "test"
        assert info.version == "3.0.0"
        assert info.description == "From pyproject"
        assert PluginPermission.NETWORK in info.permissions
        assert info.config_schema == {"key": {"type": "string"}}
        assert info.default_config == {"key": "val"}

    def test_load_plugin_via_entry_point_returns_none_for_unknown(self):
        """load_plugin_via_entry_point returns None for non-existent plugins."""
        result = load_plugin_via_entry_point("definitely-not-a-plugin-xyz")
        assert result is None


# ---------------------------------------------------------------------------
# Registry Cron Job Collection Tests
# ---------------------------------------------------------------------------


class TestReservedNames:
    @pytest.mark.asyncio
    async def test_install_reserved_name_rejected(
        self,
        tmp_path,
        mock_db,
        mock_bus,
        mock_config,
    ):
        """Installing a plugin with a reserved name raises ValueError."""
        from src.plugins.registry import RESERVED_PLUGIN_NAMES

        registry = PluginRegistry(
            db=mock_db,
            bus=mock_bus,
            config=mock_config,
        )
        for reserved in ["task", "status", "plugin", "hook"]:
            assert reserved.lower() in RESERVED_PLUGIN_NAMES
            with pytest.raises(ValueError, match="reserved"):
                await registry.install_from_git(
                    "https://example.com/repo.git",
                    name=reserved,
                )


# ---------------------------------------------------------------------------
# Registry Cron Job Collection Tests
# ---------------------------------------------------------------------------


class TestRegistryCronJobs:
    @pytest.mark.asyncio
    async def test_cron_jobs_collected_on_load(
        self,
        tmp_path,
        mock_db,
        mock_bus,
        mock_config,
    ):
        """@cron-decorated methods are collected when a plugin is loaded."""
        # Create a plugin with a cron method
        mock_config.data_dir = str(tmp_path / "data")
        os.makedirs(mock_config.data_dir, exist_ok=True)

        plugin_dir = tmp_path / "data" / "plugins" / "cron-test"
        src = plugin_dir / "src"
        src.mkdir(parents=True)

        (src / "plugin.yaml").write_text(
            textwrap.dedent("""\
            name: cron-test
            version: "1.0.0"
        """)
        )
        (src / "plugin.py").write_text(
            textwrap.dedent("""\
            from src.plugins.base import Plugin, PluginContext, cron

            class CronPlugin(Plugin):
                async def initialize(self, ctx: PluginContext) -> None:
                    pass

                async def shutdown(self, ctx: PluginContext) -> None:
                    pass

                @cron("0 */4 * * *")
                async def every_four_hours(self, ctx: PluginContext) -> None:
                    pass

                @cron("30 2 * * 1-5")
                async def weekday_check(self, ctx: PluginContext) -> None:
                    pass
        """)
        )

        mock_db.get_plugin = AsyncMock(
            return_value={
                "id": "cron-test",
                "install_path": str(plugin_dir),
                "status": "installed",
            }
        )

        registry = PluginRegistry(
            db=mock_db,
            bus=mock_bus,
            config=mock_config,
        )
        await registry.load_plugin("cron-test")

        assert len(registry._cron_jobs) == 2
        expressions = {j.expression for j in registry._cron_jobs}
        assert "0 */4 * * *" in expressions
        assert "30 2 * * 1-5" in expressions
        assert all(j.plugin_name == "cron-test" for j in registry._cron_jobs)

    @pytest.mark.asyncio
    async def test_cron_jobs_removed_on_unload(
        self,
        tmp_path,
        mock_db,
        mock_bus,
        mock_config,
    ):
        """Cron jobs are removed when a plugin is unloaded."""
        mock_config.data_dir = str(tmp_path / "data")
        os.makedirs(mock_config.data_dir, exist_ok=True)

        plugin_dir = tmp_path / "data" / "plugins" / "cron-test"
        src = plugin_dir / "src"
        src.mkdir(parents=True)

        (src / "plugin.yaml").write_text(
            textwrap.dedent("""\
            name: cron-test
            version: "1.0.0"
        """)
        )
        (src / "plugin.py").write_text(
            textwrap.dedent("""\
            from src.plugins.base import Plugin, PluginContext, cron

            class CronPlugin(Plugin):
                async def initialize(self, ctx: PluginContext) -> None:
                    pass

                async def shutdown(self, ctx: PluginContext) -> None:
                    pass

                @cron("0 0 * * *")
                async def midnight(self, ctx: PluginContext) -> None:
                    pass
        """)
        )

        mock_db.get_plugin = AsyncMock(
            return_value={
                "id": "cron-test",
                "install_path": str(plugin_dir),
                "status": "installed",
            }
        )

        registry = PluginRegistry(
            db=mock_db,
            bus=mock_bus,
            config=mock_config,
        )
        await registry.load_plugin("cron-test")
        assert len(registry._cron_jobs) == 1

        await registry.unload_plugin("cron-test")
        assert len(registry._cron_jobs) == 0


# ---------------------------------------------------------------------------
# tick_cron scheduling (coverage plan §plugins items 6-8)
# ---------------------------------------------------------------------------


def _install_file_plugin(tmp_path: Path, mock_db, name: str, plugin_body: str) -> Path:
    """Write a plugin.yaml + plugin.py plugin dir and point mock_db at it."""
    plugin_dir = tmp_path / "data" / "plugins" / name
    src = plugin_dir / "src"
    src.mkdir(parents=True)
    (src / "plugin.yaml").write_text(f"name: {name}\nversion: '1.0.0'\n")
    (src / "plugin.py").write_text(textwrap.dedent(plugin_body))
    mock_db.get_plugin = AsyncMock(
        return_value={"id": name, "install_path": str(plugin_dir), "status": "installed"}
    )
    return plugin_dir


class TestTickCron:
    @pytest.mark.asyncio
    async def test_tick_cron_runs_due_job_and_honours_config_override(
        self, tmp_path, mock_db, mock_bus, mock_config
    ):
        """A user's config value for a @cron(config_key=...) job overrides
        the decorator default at tick time."""
        _install_file_plugin(
            tmp_path,
            mock_db,
            "cron-override",
            """\
            from src.plugins.base import Plugin, PluginContext, cron

            class CronPlugin(Plugin):
                async def initialize(self, ctx: PluginContext) -> None:
                    self.calls = []

                async def shutdown(self, ctx: PluginContext) -> None:
                    pass

                # February 31st never exists — the default can never be due.
                @cron("0 0 31 2 *", config_key="sched")
                async def job(self, ctx: PluginContext) -> None:
                    self.calls.append(1)
            """,
        )
        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("cron-override")
        instance = registry._plugins["cron-override"].instance

        # Without an override the impossible default never fires.
        await registry.tick_cron()
        assert registry._cron_tasks == {}
        assert instance.calls == []

        ctx = registry._plugins["cron-override"].context
        await ctx.save_config({"sched": "* * * * *"})
        await registry.tick_cron()
        assert registry._cron_tasks
        await asyncio.gather(*registry._cron_tasks.values())
        assert instance.calls == [1]

    @pytest.mark.asyncio
    async def test_tick_cron_skips_job_still_running(
        self, tmp_path, mock_db, mock_bus, mock_config
    ):
        """A job still running from the previous tick is skipped, not
        double-started."""
        _install_file_plugin(
            tmp_path,
            mock_db,
            "cron-overlap",
            """\
            import asyncio

            from src.plugins.base import Plugin, PluginContext, cron

            class CronPlugin(Plugin):
                async def initialize(self, ctx: PluginContext) -> None:
                    self.calls = []
                    self.gate = asyncio.Event()

                async def shutdown(self, ctx: PluginContext) -> None:
                    pass

                @cron("* * * * *")
                async def job(self, ctx: PluginContext) -> None:
                    self.calls.append(1)
                    await self.gate.wait()
            """,
        )
        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("cron-overlap")
        instance = registry._plugins["cron-overlap"].instance

        await registry.tick_cron()
        assert len(registry._cron_tasks) == 1
        await asyncio.sleep(0)  # let the job start and park on the gate
        assert instance.calls == [1]

        # Clear the schedule dedup so only the overlap guard can skip.
        registry._cron_jobs[0].last_run = None
        await registry.tick_cron()
        assert len(registry._cron_tasks) == 1
        assert instance.calls == [1]

        instance.gate.set()
        await asyncio.gather(*registry._cron_tasks.values())

    @pytest.mark.asyncio
    async def test_cron_failure_feeds_circuit_breaker(
        self, tmp_path, mock_db, mock_bus, mock_config, caplog
    ):
        """_run_cron_safe routes an exception into record_failure and a
        success into record_success (spec §9 circuit breaker loop)."""
        import logging

        _install_file_plugin(
            tmp_path,
            mock_db,
            "cron-breaker",
            """\
            from src.plugins.base import Plugin, PluginContext, cron

            class CronPlugin(Plugin):
                async def initialize(self, ctx: PluginContext) -> None:
                    self.fail = True

                async def shutdown(self, ctx: PluginContext) -> None:
                    pass

                @cron("* * * * *")
                async def job(self, ctx: PluginContext) -> None:
                    if self.fail:
                        raise RuntimeError("boom")
            """,
        )
        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        await registry.load_plugin("cron-breaker")
        loaded = registry._plugins["cron-breaker"]

        with caplog.at_level(logging.WARNING, logger="src.plugins.registry"):
            await registry.tick_cron()
            await asyncio.gather(*registry._cron_tasks.values())
        assert loaded.consecutive_failures == 1
        assert "failed" in caplog.text
        assert "boom" in caplog.text

        # A subsequent success resets the counter.
        loaded.instance.fail = False
        registry._cron_jobs[0].last_run = None
        await registry.tick_cron()
        await asyncio.gather(*registry._cron_tasks.values())
        assert loaded.consecutive_failures == 0


# ---------------------------------------------------------------------------
# remove_plugin data-loss guards (coverage plan §plugins items 20-21)
# ---------------------------------------------------------------------------


class TestRemovePlugin:
    @pytest.mark.asyncio
    async def test_remove_plugin_does_not_delete_symlinked_dev_source(
        self, tmp_path, mock_db, mock_bus, mock_config
    ):
        """Removing a dev-mode install (src is a symlink) must delete the
        install dir but never the developer's source tree."""
        source = tmp_path / "dev-source"
        source.mkdir()
        (source / "plugin.yaml").write_text("name: devplug\nversion: '1.0.0'\n")
        (source / "plugin.py").write_text(
            textwrap.dedent("""\
            from src.plugins.base import Plugin, PluginContext

            class DevPlugin(Plugin):
                async def initialize(self, ctx: PluginContext) -> None:
                    pass

                async def shutdown(self, ctx: PluginContext) -> None:
                    pass
            """)
        )

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        name = await registry.install_from_path(str(source))
        assert name == "devplug"
        install_path = registry.plugins_dir / "devplug"
        assert (install_path / "src").is_symlink()
        assert registry.is_loaded("devplug")

        mock_db.get_plugin = AsyncMock(
            return_value={"id": "devplug", "install_path": str(install_path)}
        )
        mock_db.list_plugins = AsyncMock(return_value=[])
        await registry.remove_plugin("devplug")

        assert not install_path.exists()
        assert source.is_dir()
        assert (source / "plugin.py").is_file()
        assert (source / "plugin.yaml").is_file()

    @pytest.mark.asyncio
    async def test_remove_plugin_keeps_directory_shared_by_another_record(
        self, tmp_path, mock_db, mock_bus, mock_config, caplog
    ):
        """Two DB records pointing at one directory: removing one must not
        delete the shared directory."""
        import logging

        shared = tmp_path / "shared-install"
        shared.mkdir()
        (shared / "keep.txt").write_text("data")

        mock_db.get_plugin = AsyncMock(return_value={"id": "dup-a", "install_path": str(shared)})
        mock_db.list_plugins = AsyncMock(
            return_value=[{"id": "dup-b", "install_path": str(shared)}]
        )

        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)
        with caplog.at_level(logging.WARNING, logger="src.plugins.registry"):
            await registry.remove_plugin("dup-a")

        assert shared.is_dir()
        assert (shared / "keep.txt").is_file()
        assert "shares this path" in caplog.text
        mock_db.delete_plugin.assert_awaited_once_with("dup-a")
        mock_db.delete_plugin_data_all.assert_awaited_once_with("dup-a")


# ---------------------------------------------------------------------------
# Internal plugin discovery resilience (coverage plan §plugins item 22)
# ---------------------------------------------------------------------------


class TestInternalDiscoveryResilience:
    def test_internal_plugin_discovery_survives_a_broken_module(self, monkeypatch, caplog):
        """One broken internal module must not take down plugin loading:
        discovery, tool collection, and formatter collection all skip it."""
        import importlib
        import logging

        import src.plugins.internal as internal

        baseline_formatters = internal.collect_internal_formatters()
        assert "read_file" in baseline_formatters  # sanity: files contributes

        real_import = importlib.import_module

        def broken_import(name, *args, **kwargs):
            if name == "src.plugins.internal.files":
                raise RuntimeError("broken module")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("src.plugins.internal.importlib.import_module", broken_import)

        with caplog.at_level(logging.ERROR, logger="src.plugins.internal"):
            plugins = internal.discover_internal_plugins()
            tools = internal.collect_internal_tool_definitions()
            formatters = internal.collect_internal_formatters()

        modnames = {m for m, _ in plugins}
        assert "src.plugins.internal.files" not in modnames
        assert any(m.endswith(".git") for m in modnames)
        assert any(m.endswith(".notes") for m in modnames)

        categories = {c for c, _ in tools}
        assert "files" not in categories
        assert {"git", "notes"} <= categories

        assert "read_file" not in formatters
        assert "list_notes" in formatters

        assert "Failed to import internal plugin" in caplog.text


# ---------------------------------------------------------------------------
# PluginContext isolation boundaries (coverage plan §plugins items 23-24)
# ---------------------------------------------------------------------------


class TestContextBoundaries:
    def test_context_bus_requires_internal_trust(self, plugin_context_factory):
        from src.plugins.base import TrustLevel

        external = plugin_context_factory(trust_level=TrustLevel.EXTERNAL)
        with pytest.raises(PermissionError, match="INTERNAL"):
            _ = external.bus

        internal = plugin_context_factory(trust_level=TrustLevel.INTERNAL)
        assert internal.bus is internal._bus

    @pytest.mark.asyncio
    async def test_load_config_falls_back_to_empty_on_corrupt_json(self, plugin_context_factory):
        """A malformed stored config row must not make the plugin
        unloadable — load_config falls back to {}."""
        ctx = plugin_context_factory()
        ctx._db.get_plugin = AsyncMock(return_value={"config": "{not json"})
        assert await ctx.load_config() == {}
        assert ctx.get_config() == {}

        ctx._db.get_plugin = AsyncMock(return_value={"config": None})
        assert await ctx.load_config() == {}

        ctx._db = None
        assert await ctx.load_config() == {}
        assert ctx.get_config() == {}


# ---------------------------------------------------------------------------
# Unload alias removal (PLG-6, FU-10)
# ---------------------------------------------------------------------------


async def _shared_noop_command(args: dict) -> dict:
    return {"ok": True}


class TestUnloadAliasRemoval:
    @pytest.mark.asyncio
    async def test_unload_removes_only_own_aliases_when_handler_shared(
        self, tmp_path, mock_db, mock_bus, mock_config
    ):
        """PLG-6: two plugins registering the *same callable* under
        different names — unloading one must not remove the other's
        registrations (the old id()-based removal did)."""
        registry = PluginRegistry(db=mock_db, bus=mock_bus, config=mock_config)

        class AlphaPlugin(Plugin):
            plugin_name = "alpha"

            async def initialize(self, ctx: PluginContext) -> None:
                ctx.register_command("scan", _shared_noop_command)

            async def shutdown(self, ctx: PluginContext) -> None:
                pass

        class BetaPlugin(Plugin):
            plugin_name = "beta"

            async def initialize(self, ctx: PluginContext) -> None:
                ctx.register_command("check", _shared_noop_command)

            async def shutdown(self, ctx: PluginContext) -> None:
                pass

        await registry.register_in_memory_plugin(AlphaPlugin)
        await registry.register_in_memory_plugin(BetaPlugin)
        for key in ("alpha.scan", "scan", "beta.check", "check"):
            assert registry.get_command(key) is _shared_noop_command

        await registry.unload_plugin("alpha")

        assert registry.get_command("alpha.scan") is None
        assert registry.get_command("scan") is None
        # beta's registrations of the identical callable survive.
        assert registry.get_command("beta.check") is _shared_noop_command
        assert registry.get_command("check") is _shared_noop_command


# ---------------------------------------------------------------------------
# Loader git operations against local throwaway repos (FU-10)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> str:
    import subprocess

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class TestLoaderGitOperations:
    """clone/pull/rev helpers driven against real local repos — no network."""

    def _make_origin(self, tmp_path: Path) -> tuple[Path, str]:
        origin = tmp_path / "origin"
        origin.mkdir()
        _run_git(["init", "-b", "main"], origin)
        (origin / "file.txt").write_text("v1")
        _run_git(["add", "."], origin)
        _run_git(["commit", "-m", "first"], origin)
        return origin, _run_git(["rev-parse", "HEAD"], origin)

    @pytest.mark.asyncio
    async def test_clone_plugin_repo_clones_into_src_and_returns_head(self, tmp_path):
        from src.plugins.loader import clone_plugin_repo

        origin, head = self._make_origin(tmp_path)
        target = tmp_path / "install"
        target.mkdir()

        rev = await clone_plugin_repo(str(origin), target)

        assert rev == head
        assert (target / "src" / "file.txt").read_text() == "v1"

    @pytest.mark.asyncio
    async def test_clone_plugin_repo_checks_out_requested_branch_and_rev(self, tmp_path):
        from src.plugins.loader import clone_plugin_repo

        origin, first = self._make_origin(tmp_path)
        _run_git(["checkout", "-b", "feature"], origin)
        (origin / "feature.txt").write_text("feat")
        _run_git(["add", "."], origin)
        _run_git(["commit", "-m", "feature work"], origin)
        feature_head = _run_git(["rev-parse", "HEAD"], origin)
        _run_git(["checkout", "main"], origin)

        by_branch = tmp_path / "by-branch"
        by_branch.mkdir()
        rev = await clone_plugin_repo(str(origin), by_branch, branch="feature")
        assert rev == feature_head
        assert (by_branch / "src" / "feature.txt").is_file()

        # rev checkout (local clones keep full history).
        by_rev = tmp_path / "by-rev"
        by_rev.mkdir()
        rev = await clone_plugin_repo(str(origin), by_rev, rev=first)
        assert rev == first
        assert not (by_rev / "src" / "feature.txt").exists()

    @pytest.mark.asyncio
    async def test_clone_plugin_repo_raises_on_bad_url(self, tmp_path):
        from src.plugins.loader import clone_plugin_repo

        target = tmp_path / "install"
        target.mkdir()
        with pytest.raises(RuntimeError, match="Git clone failed"):
            await clone_plugin_repo(str(tmp_path / "no-such-repo"), target)

    @pytest.mark.asyncio
    async def test_pull_plugin_repo_fast_forwards_and_reports_new_head(self, tmp_path):
        from src.plugins.loader import clone_plugin_repo, pull_plugin_repo

        origin, _ = self._make_origin(tmp_path)
        install = tmp_path / "install"
        install.mkdir()
        await clone_plugin_repo(str(origin), install)

        (origin / "new.txt").write_text("v2")
        _run_git(["add", "."], origin)
        _run_git(["commit", "-m", "second"], origin)
        new_head = _run_git(["rev-parse", "HEAD"], origin)

        rev = await pull_plugin_repo(install)

        assert rev == new_head
        assert (install / "src" / "new.txt").read_text() == "v2"

    @pytest.mark.asyncio
    async def test_pull_plugin_repo_raises_when_src_missing(self, tmp_path):
        from src.plugins.loader import pull_plugin_repo

        with pytest.raises(RuntimeError, match="source directory not found"):
            await pull_plugin_repo(tmp_path / "not-installed")

    @pytest.mark.asyncio
    async def test_get_current_rev_returns_sha_or_empty(self, tmp_path):
        from src.plugins.loader import clone_plugin_repo, get_current_rev

        origin, head = self._make_origin(tmp_path)
        install = tmp_path / "install"
        install.mkdir()
        await clone_plugin_repo(str(origin), install)

        assert get_current_rev(install) == head
        # No src/ directory → empty string, no exception.
        assert get_current_rev(tmp_path / "not-installed") == ""
        # src/ exists but is not a git repo → empty string.
        plain = tmp_path / "plain"
        (plain / "src").mkdir(parents=True)
        assert get_current_rev(plain) == ""
