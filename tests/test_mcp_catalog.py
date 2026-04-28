"""Tests for the MCP tool catalog (src/profiles/mcp_catalog.py)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.profiles.mcp_catalog import (
    CatalogEntry,
    McpToolCatalog,
    build_agent_queue_tools,
    make_reload_hook,
    populate_catalog,
    refresh_one,
)
from src.profiles.mcp_probe import ProbedTool, ProbeResult
from src.profiles.mcp_registry import McpRegistry, McpServerConfig


def _config(
    name: str,
    project_id: str | None = None,
    transport: str = "stdio",
    is_builtin: bool = False,
    **kw,
) -> McpServerConfig:
    c = McpServerConfig(
        name=name,
        transport=transport,
        project_id=project_id,
        command=kw.pop("command", "ls" if transport == "stdio" else ""),
        url=kw.pop("url", "http://x" if transport == "http" else ""),
    )
    c.is_builtin = is_builtin
    return c


# ---------------------------------------------------------------------------
# Catalog store
# ---------------------------------------------------------------------------


class TestCatalogStore:
    def test_upsert_and_get(self):
        cat = McpToolCatalog()
        entry = CatalogEntry(
            server_name="a", project_id=None, transport="stdio", tools=[ProbedTool("t1")]
        )
        cat.upsert(entry)
        assert cat.get("a") is entry

    def test_project_shadows_system(self):
        cat = McpToolCatalog()
        cat.upsert(CatalogEntry(server_name="a", project_id=None, transport="stdio"))
        cat.upsert(CatalogEntry(server_name="a", project_id="proj", transport="stdio"))
        # system lookup ignores project entries
        assert cat.get("a", project_id=None).project_id is None
        # project lookup returns project entry
        assert cat.get("a", project_id="proj").project_id == "proj"
        # project lookup that misses falls back to system
        cat.upsert(CatalogEntry(server_name="b", project_id=None, transport="stdio"))
        assert cat.get("b", project_id="proj").project_id is None

    def test_list_for_project_includes_inherited(self):
        cat = McpToolCatalog()
        cat.upsert(CatalogEntry(server_name="sys-only", project_id=None, transport="stdio"))
        cat.upsert(CatalogEntry(server_name="shared", project_id=None, transport="stdio"))
        cat.upsert(CatalogEntry(server_name="shared", project_id="proj", transport="stdio"))
        cat.upsert(CatalogEntry(server_name="proj-only", project_id="proj", transport="stdio"))

        names = sorted(e.server_name for e in cat.list_for_scope("proj"))
        assert names == ["proj-only", "shared", "sys-only"]
        # The shared entry returned should be the project-scoped one.
        shared = next(e for e in cat.list_for_scope("proj") if e.server_name == "shared")
        assert shared.project_id == "proj"

    def test_list_for_system_only(self):
        cat = McpToolCatalog()
        cat.upsert(CatalogEntry(server_name="a", project_id=None, transport="stdio"))
        cat.upsert(CatalogEntry(server_name="b", project_id="proj", transport="stdio"))
        names = [e.server_name for e in cat.list_for_scope(None)]
        assert names == ["a"]

    def test_remove(self):
        cat = McpToolCatalog()
        cat.upsert(CatalogEntry(server_name="a", project_id=None, transport="stdio"))
        assert cat.remove("a") is True
        assert cat.remove("a") is False
        assert cat.get("a") is None


# ---------------------------------------------------------------------------
# Builtin resolver — agent-queue tool list
# ---------------------------------------------------------------------------


class TestBuildAgentQueueTools:
    def test_includes_explicit_tools(self):
        # Smoke test against the real _ALL_TOOL_DEFINITIONS.  We don't pin
        # specific names because the set evolves; just verify it returns
        # something and that exclusions work.
        tools = build_agent_queue_tools(excluded=set())
        assert len(tools) > 0
        all_names = {t.name for t in tools}

        # Pick one we know to exist and test exclusion.
        excluded_name = next(iter(all_names))
        filtered = build_agent_queue_tools(excluded={excluded_name})
        assert excluded_name not in {t.name for t in filtered}
        assert len(filtered) == len(tools) - 1

    def test_includes_plugin_tools(self):
        plugin = [{"name": "my_plugin_tool", "description": "does plugin things"}]
        tools = build_agent_queue_tools(excluded=set(), plugin_tools=plugin)
        names = {t.name for t in tools}
        assert "my_plugin_tool" in names

    def test_plugin_tool_does_not_double_register(self):
        # If a plugin tool name collides with an explicit definition,
        # the plugin one should be skipped (explicit wins).
        explicit_names = {t.name for t in build_agent_queue_tools(excluded=set())}
        collide = next(iter(explicit_names))
        with_collision = build_agent_queue_tools(
            excluded=set(),
            plugin_tools=[{"name": collide, "description": "plugin override attempt"}],
        )
        descs = {t.name: t.description for t in with_collision}
        # The explicit description wins, not "plugin override attempt".
        assert descs[collide] != "plugin override attempt"

    def test_empty_plugin_tools_is_ok(self):
        tools = build_agent_queue_tools(excluded=set(), plugin_tools=None)
        assert tools  # non-empty


# ---------------------------------------------------------------------------
# refresh_one
# ---------------------------------------------------------------------------


class TestRefreshOne:
    @pytest.mark.asyncio
    async def test_probes_external_server(self):
        cat = McpToolCatalog()
        config = _config("ext", transport="http", url="http://example/mcp")

        async def fake_probe(c, *, timeout):
            return ProbeResult(
                server_name=c.name,
                transport=c.transport,
                tools=[ProbedTool("toolA")],
                probed_at=42.0,
            )

        with patch("src.profiles.mcp_catalog.probe_server", new=fake_probe):
            entry = await refresh_one(cat, config)

        assert entry.tools == [ProbedTool("toolA")]
        assert entry.last_probed_at == 42.0
        assert entry.last_error is None
        assert cat.get("ext") is entry

    @pytest.mark.asyncio
    async def test_uses_builtin_resolver_for_builtin(self):
        cat = McpToolCatalog()
        config = _config("agent-queue", transport="http", url="http://x", is_builtin=True)

        def resolver():
            return [ProbedTool("create_task"), ProbedTool("list_projects")]

        # Patch probe_server so we can prove it is NOT called.
        called = False

        async def boom(c, *, timeout):
            nonlocal called
            called = True
            raise AssertionError("should not probe builtins")

        with patch("src.profiles.mcp_catalog.probe_server", new=boom):
            entry = await refresh_one(cat, config, builtin_resolver=resolver)

        assert not called
        assert {t.name for t in entry.tools} == {"create_task", "list_projects"}
        assert entry.is_builtin
        assert entry.last_error is None

    @pytest.mark.asyncio
    async def test_builtin_without_resolver_records_error(self):
        cat = McpToolCatalog()
        config = _config("agent-queue", transport="http", url="http://x", is_builtin=True)
        entry = await refresh_one(cat, config, builtin_resolver=None)
        assert entry.tools == []
        assert "no builtin resolver" in (entry.last_error or "")

    @pytest.mark.asyncio
    async def test_probe_error_recorded_in_entry(self):
        cat = McpToolCatalog()
        config = _config("ext", transport="http", url="http://example/mcp")

        async def fake_probe(c, *, timeout):
            return ProbeResult(server_name=c.name, transport=c.transport, error="boom")

        with patch("src.profiles.mcp_catalog.probe_server", new=fake_probe):
            entry = await refresh_one(cat, config)

        assert entry.last_error == "boom"
        assert entry.tools == []


# ---------------------------------------------------------------------------
# populate_catalog
# ---------------------------------------------------------------------------


class TestPopulateCatalog:
    @pytest.mark.asyncio
    async def test_empty_registry(self):
        reg = McpRegistry()
        cat = McpToolCatalog()
        results = await populate_catalog(reg, cat)
        assert results == []
        assert len(cat) == 0

    @pytest.mark.asyncio
    async def test_probes_every_registry_entry(self):
        reg = McpRegistry()
        reg.upsert(_config("a"))
        reg.upsert(_config("b", project_id="proj"))
        cat = McpToolCatalog()

        seen: list[str] = []

        async def fake_probe(c, *, timeout):
            seen.append(c.name)
            return ProbeResult(
                server_name=c.name, transport=c.transport, tools=[ProbedTool(c.name + "-tool")]
            )

        with patch("src.profiles.mcp_catalog.probe_server", new=fake_probe):
            await populate_catalog(reg, cat, timeout=5.0)

        assert sorted(seen) == ["a", "b"]
        assert cat.get("a") is not None
        assert cat.get("b", project_id="proj") is not None

    @pytest.mark.asyncio
    async def test_drops_stale_entries(self):
        reg = McpRegistry()
        reg.upsert(_config("kept"))
        cat = McpToolCatalog()
        cat.upsert(CatalogEntry(server_name="stale", project_id=None, transport="stdio"))

        async def fake_probe(c, *, timeout):
            return ProbeResult(server_name=c.name, transport=c.transport)

        with patch("src.profiles.mcp_catalog.probe_server", new=fake_probe):
            await populate_catalog(reg, cat)

        assert cat.get("stale") is None
        assert cat.get("kept") is not None

    @pytest.mark.asyncio
    async def test_builtin_handled_by_resolver(self):
        reg = McpRegistry()
        reg.set_builtin(McpServerConfig(name="agent-queue", transport="http", url="http://x"))
        reg.upsert(_config("ext", transport="http", url="http://ext"))
        cat = McpToolCatalog()

        def resolver():
            return [ProbedTool("ag-tool")]

        async def fake_probe(c, *, timeout):
            return ProbeResult(
                server_name=c.name,
                transport=c.transport,
                tools=[ProbedTool("ext-tool")],
            )

        with patch("src.profiles.mcp_catalog.probe_server", new=fake_probe):
            await populate_catalog(reg, cat, builtin_resolver=resolver)

        ag = cat.get("agent-queue")
        ext = cat.get("ext")
        assert [t.name for t in ag.tools] == ["ag-tool"]
        assert [t.name for t in ext.tools] == ["ext-tool"]
        assert ag.is_builtin
        assert not ext.is_builtin


# ---------------------------------------------------------------------------
# make_reload_hook (vault watcher integration)
# ---------------------------------------------------------------------------


class TestReloadHook:
    @pytest.mark.asyncio
    async def test_modified_entry_reprobed(self):
        reg = McpRegistry()
        reg.upsert(_config("pw"))
        cat = McpToolCatalog()

        seen: list[str] = []

        async def fake_probe(c, *, timeout):
            seen.append(c.name)
            return ProbeResult(server_name=c.name, transport=c.transport)

        hook = make_reload_hook(reg, cat)
        with patch("src.profiles.mcp_catalog.probe_server", new=fake_probe):
            await hook([(None, "pw")])

        assert seen == ["pw"]
        assert cat.get("pw") is not None

    @pytest.mark.asyncio
    async def test_removed_entry_dropped_from_catalog(self):
        reg = McpRegistry()  # registry intentionally empty
        cat = McpToolCatalog()
        cat.upsert(CatalogEntry(server_name="gone", project_id=None, transport="stdio"))

        hook = make_reload_hook(reg, cat)
        await hook([(None, "gone")])

        assert cat.get("gone") is None

    @pytest.mark.asyncio
    async def test_builtin_entry_uses_resolver(self):
        reg = McpRegistry()
        reg.set_builtin(McpServerConfig(name="agent-queue", transport="http", url="http://x"))
        cat = McpToolCatalog()

        def resolver():
            return [ProbedTool("aq")]

        async def boom(c, *, timeout):
            raise AssertionError("should not probe builtin")

        hook = make_reload_hook(reg, cat, builtin_resolver=resolver)
        with patch("src.profiles.mcp_catalog.probe_server", new=boom):
            await hook([(None, "agent-queue")])

        assert [t.name for t in cat.get("agent-queue").tools] == ["aq"]
