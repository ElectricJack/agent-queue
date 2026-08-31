"""Command-boundary tests for :class:`~src.commands.plugin_commands.PluginCommandsMixin`.

The plugin lifecycle commands are thin wrappers around ``PluginRegistry``, and
almost every wrapper branch (missing argument, registry absent, registry
raising, DB fallback, config write) was uncovered.  These tests drive the real
``_cmd_*`` methods against a stub registry and the real database
(test-coverage plan, commands 10–13).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class _StubContext:
    """Minimal ``PluginContext`` stand-in for the config/prompt commands."""

    def __init__(self, config: dict | None = None, prompts: list | None = None):
        self._config = dict(config or {})
        self._prompts = list(prompts or [])
        self.saved: list[dict] = []

    def get_config(self) -> dict:
        return dict(self._config)

    async def save_config(self, config: dict) -> None:
        self.saved.append(config)
        self._config = dict(config)

    def list_prompts(self) -> list:
        return list(self._prompts)


class _StubRegistry:
    """Registry stub: every lifecycle method raises unless overridden."""

    def __init__(self, **overrides):
        self._plugins: dict[str, SimpleNamespace] = {}
        self._loaded_info: dict[str, dict] = {}
        self._overrides = overrides

    def list_plugins(self) -> list[dict]:
        return list(self._loaded_info.values())

    def get_plugin(self, name: str) -> dict | None:
        return self._loaded_info.get(name)

    def add_loaded(self, name: str, *, info: dict, context: _StubContext, install_path: str = "/p"):
        self._loaded_info[name] = {"name": name, **info}
        self._plugins[name] = SimpleNamespace(context=context, install_path=install_path)

    def __getattr__(self, item: str):
        if item in self._overrides:
            return self._overrides[item]

        async def _boom(*a, **kw):
            raise RuntimeError(f"registry {item} exploded")

        return _boom


@pytest.fixture
async def plugin_handler(command_handler_factory):
    handler = await command_handler_factory()
    handler.orchestrator.plugin_registry = _StubRegistry()
    return handler


# ---------------------------------------------------------------------------
# 10: lifecycle validation + registry failure wrapping
# ---------------------------------------------------------------------------

_LIFECYCLE = [
    # (command, valid args, missing-arg error, error prefix on registry failure)
    (
        "_cmd_plugin_install",
        {"url": "https://example.test/p.git"},
        "url is required",
        "Installation failed",
    ),
    ("_cmd_plugin_update", {"name": "p"}, "name is required", "Update failed"),
    ("_cmd_plugin_remove", {"name": "p"}, "name is required", "Removal failed"),
    ("_cmd_plugin_enable", {"name": "p"}, "name is required", "Enable failed"),
    ("_cmd_plugin_disable", {"name": "p"}, "name is required", "Disable failed"),
    ("_cmd_plugin_reload", {"name": "p"}, "name is required", "Reload failed"),
]


@pytest.mark.parametrize(("command", "valid_args", "missing_error", "prefix"), _LIFECYCLE)
async def test_plugin_lifecycle_validates_inputs_and_wraps_registry_failures(
    plugin_handler, command, valid_args, missing_error, prefix
):
    cmd = getattr(plugin_handler, command)

    # 1. Missing required argument — rejected before the registry is touched.
    assert await cmd({}) == {"error": missing_error}

    # 2. Registry absent entirely.
    del plugin_handler.orchestrator.plugin_registry
    assert await cmd(dict(valid_args)) == {"error": "Plugin system not initialized"}
    plugin_handler.orchestrator.plugin_registry = _StubRegistry()

    # 3. Registry raises — wrapped into the command's error convention, never
    #    a success key and never an escaping exception.
    result = await cmd(dict(valid_args))
    assert set(result) == {"error"}
    assert result["error"].startswith(prefix)
    assert "exploded" in result["error"]


async def test_plugin_lifecycle_returns_success_payloads_from_the_registry(plugin_handler):
    async def _install(url, branch=None, name=None):
        assert url == "https://example.test/p.git"
        assert branch == "main"
        return "demo"

    async def _update(name, rev=None):
        return "abcdef1234567890"

    async def _noop(name):
        return None

    plugin_handler.orchestrator.plugin_registry = _StubRegistry(
        install_from_git=_install,
        update_plugin=_update,
        remove_plugin=_noop,
        enable_plugin=_noop,
        disable_plugin=_noop,
        reload_plugin=_noop,
    )
    h = plugin_handler

    installed = await h._cmd_plugin_install({"url": "https://example.test/p.git", "branch": "main"})
    assert installed["installed"] == "demo"

    updated = await h._cmd_plugin_update({"name": "demo"})
    assert updated == {
        "updated": "demo",
        "rev": "abcdef1234567890",
        "message": "Plugin 'demo' updated to abcdef12",
    }

    assert await h._cmd_plugin_remove({"name": "demo"}) == {
        "removed": "demo",
        "message": "Plugin 'demo' removed",
    }
    assert (await h._cmd_plugin_enable({"name": "demo"}))["enabled"] == "demo"
    assert (await h._cmd_plugin_disable({"name": "demo"}))["disabled"] == "demo"
    assert (await h._cmd_plugin_reload({"name": "demo"}))["reloaded"] == "demo"


# ---------------------------------------------------------------------------
# 11: list / info merge runtime metadata over the DB record
# ---------------------------------------------------------------------------


async def test_plugin_list_and_info_merge_runtime_and_database_fallback(plugin_handler):
    db = plugin_handler.db
    await db.create_plugin(
        plugin_id="loaded-one",
        version="1.2.3",
        source_url="https://example.test/one.git",
        status="active",
    )
    await db.create_plugin(plugin_id="db-only", version="0.9.0", status="disabled")

    registry = plugin_handler.orchestrator.plugin_registry
    registry.add_loaded(
        "loaded-one",
        info={
            "description": "runtime description",
            "commands": ["loaded-one.do"],
            "tools": ["loaded_one_tool"],
            "version": "1.2.3",
        },
        context=_StubContext(),
    )

    listing = await plugin_handler._cmd_plugin_list({})
    by_name = {p["name"]: p for p in listing["plugins"]}
    assert listing["count"] == 2

    # Loaded plugin: DB fields plus merged runtime metadata.
    assert by_name["loaded-one"]["version"] == "1.2.3"
    assert by_name["loaded-one"]["status"] == "active"
    assert by_name["loaded-one"]["source_url"] == "https://example.test/one.git"
    assert by_name["loaded-one"]["description"] == "runtime description"
    assert by_name["loaded-one"]["commands"] == ["loaded-one.do"]
    assert by_name["loaded-one"]["tools"] == ["loaded_one_tool"]

    # DB-only plugin: no runtime keys invented.
    assert by_name["db-only"]["status"] == "disabled"
    assert "description" not in by_name["db-only"]
    assert "commands" not in by_name["db-only"]

    # info(): loaded first...
    info = await plugin_handler._cmd_plugin_info({"name": "loaded-one"})
    assert info["plugin"]["description"] == "runtime description"

    # ...then the DB fallback for an installed-but-unloaded plugin...
    fallback = await plugin_handler._cmd_plugin_info({"name": "db-only"})
    assert fallback["plugin"]["id"] == "db-only"
    assert fallback["plugin"]["version"] == "0.9.0"

    # ...then a clean error.
    assert await plugin_handler._cmd_plugin_info({"name": "ghost"}) == {
        "error": "Plugin 'ghost' not found"
    }
    assert await plugin_handler._cmd_plugin_info({}) == {"error": "name is required"}

    del plugin_handler.orchestrator.plugin_registry
    assert await plugin_handler._cmd_plugin_list({}) == {"error": "Plugin system not initialized"}
    assert await plugin_handler._cmd_plugin_info({"name": "x"}) == {
        "error": "Plugin system not initialized"
    }


# ---------------------------------------------------------------------------
# 12 [inline extension]: config read/write + CMD-3 malformed caller JSON
# ---------------------------------------------------------------------------


async def test_plugin_config_reads_db_json_and_updates_loaded_context(plugin_handler):
    h = plugin_handler
    registry = h.orchestrator.plugin_registry

    assert await h._cmd_plugin_config({}) == {"error": "name is required"}
    assert await h._cmd_plugin_config({"name": "ghost"}) == {"error": "Plugin 'ghost' not found"}

    # Unloaded plugin: config comes from the DB, and unparseable stored JSON
    # degrades to {} rather than raising.
    await h.db.create_plugin(plugin_id="stored-bad", config="{not json")
    assert await h._cmd_plugin_config({"name": "stored-bad"}) == {
        "name": "stored-bad",
        "config": {},
    }
    await h.db.create_plugin(plugin_id="stored-ok", config=json.dumps({"a": 1}))
    assert await h._cmd_plugin_config({"name": "stored-ok"}) == {
        "name": "stored-ok",
        "config": {"a": 1},
    }

    # Loaded plugin: read returns the live context config.
    context = _StubContext({"threshold": 3})
    registry.add_loaded("live", info={"description": ""}, context=context)
    assert await h._cmd_plugin_config({"name": "live"}) == {
        "name": "live",
        "config": {"threshold": 3},
    }

    # Loaded plugin: a JSON-string update is parsed and saved.
    updated = await h._cmd_plugin_config({"name": "live", "config": '{"threshold": 9}'})
    assert updated == {"name": "live", "config": {"threshold": 9}, "message": "Config updated"}
    assert context.saved == [{"threshold": 9}]
    assert context.get_config() == {"threshold": 9}

    # A dict update is saved as-is.
    await h._cmd_plugin_config({"name": "live", "config": {"threshold": 11}})
    assert context.get_config() == {"threshold": 11}

    # A registry entry that reports as loaded but has no _plugins record.
    registry._loaded_info["phantom"] = {"name": "phantom"}
    assert await h._cmd_plugin_config({"name": "phantom"}) == {
        "error": "Plugin 'phantom' is not loaded"
    }


@pytest.mark.parametrize("bad_json", ["{", "[1, 2", "not json at all", '{"a": }'])
async def test_plugin_config_rejects_malformed_caller_json(plugin_handler, bad_json):
    """CMD-3: malformed caller-supplied JSON must use the error convention."""
    h = plugin_handler
    context = _StubContext({"threshold": 3})
    h.orchestrator.plugin_registry.add_loaded("live", info={}, context=context)

    result = await h._cmd_plugin_config({"name": "live", "config": bad_json})

    assert set(result) == {"error"}
    assert "config" in result["error"] and "JSON" in result["error"]
    # The rejected call changed nothing.
    assert context.saved == []
    assert context.get_config() == {"threshold": 3}


# ---------------------------------------------------------------------------
# 13: prompts / reset require a loaded plugin
# ---------------------------------------------------------------------------


async def test_plugin_prompts_and_reset_require_loaded_plugin(plugin_handler, monkeypatch):
    h = plugin_handler
    registry = h.orchestrator.plugin_registry

    assert await h._cmd_plugin_prompts({}) == {"error": "name is required"}
    assert await h._cmd_plugin_reset_prompts({}) == {"error": "name is required"}
    assert await h._cmd_plugin_prompts({"name": "live"}) == {"error": "Plugin 'live' is not loaded"}
    assert await h._cmd_plugin_reset_prompts({"name": "live"}) == {
        "error": "Plugin 'live' is not loaded"
    }

    context = _StubContext(prompts=[{"name": "review", "path": "prompts/review.md"}])
    registry.add_loaded("live", info={}, context=context, install_path="/opt/live")

    assert await h._cmd_plugin_prompts({"name": "live"}) == {
        "name": "live",
        "prompts": [{"name": "review", "path": "prompts/review.md"}],
    }

    seen: list[str] = []

    def _fake_reset(install_path: str) -> int:
        seen.append(install_path)
        return 4

    monkeypatch.setattr("src.plugins.loader.reset_prompts", _fake_reset)
    assert await h._cmd_plugin_reset_prompts({"name": "live"}) == {
        "name": "live",
        "reset_count": 4,
        "message": "Reset 4 prompts",
    }
    assert seen == ["/opt/live"]

    del h.orchestrator.plugin_registry
    assert await h._cmd_plugin_prompts({"name": "live"}) == {
        "error": "Plugin system not initialized"
    }
    assert await h._cmd_plugin_reset_prompts({"name": "live"}) == {
        "error": "Plugin system not initialized"
    }
