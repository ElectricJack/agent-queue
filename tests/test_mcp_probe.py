"""Tests for the MCP probe module (src/profiles/mcp_probe.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.profiles.mcp_probe import (
    ProbedTool,
    ProbeResult,
    _normalise_tool,
    probe_many,
    probe_server,
)
from src.profiles.mcp_registry import McpServerConfig


# ---------------------------------------------------------------------------
# Tool normalisation
# ---------------------------------------------------------------------------


class TestNormaliseTool:
    def test_dict_form_camelcase(self):
        raw = {
            "name": "foo",
            "description": "does foo",
            "inputSchema": {"type": "object"},
        }
        t = _normalise_tool(raw)
        assert t.name == "foo"
        assert t.description == "does foo"
        assert t.input_schema == {"type": "object"}

    def test_dict_form_snakecase(self):
        raw = {"name": "foo", "input_schema": {"type": "object"}}
        t = _normalise_tool(raw)
        assert t.input_schema == {"type": "object"}

    def test_attribute_form(self):
        raw = SimpleNamespace(
            name="bar",
            description="bars",
            inputSchema={"type": "string"},
        )
        t = _normalise_tool(raw)
        assert t.name == "bar"
        assert t.description == "bars"
        assert t.input_schema == {"type": "string"}

    def test_missing_optional_fields(self):
        t = _normalise_tool(SimpleNamespace(name="x"))
        assert t.name == "x"
        assert t.description == ""
        assert t.input_schema == {}


# ---------------------------------------------------------------------------
# probe_server — routing + result shape
# ---------------------------------------------------------------------------


def _stdio_config(name: str = "x") -> McpServerConfig:
    return McpServerConfig(name=name, transport="stdio", command="ls", args=["-la"])


def _http_config(name: str = "x") -> McpServerConfig:
    return McpServerConfig(name=name, transport="http", url="http://localhost:9999/mcp")


class TestProbeServerRouting:
    @pytest.mark.asyncio
    async def test_stdio_routes_to_stdio_probe(self):
        with patch(
            "src.profiles.mcp_probe._probe_stdio",
            new=AsyncMock(return_value=[ProbedTool(name="t1")]),
        ) as mock_stdio:
            result = await probe_server(_stdio_config(), timeout=5.0)
            mock_stdio.assert_awaited_once()
            assert result.ok
            assert result.server_name == "x"
            assert result.transport == "stdio"
            assert [t.name for t in result.tools] == ["t1"]

    @pytest.mark.asyncio
    async def test_http_routes_to_http_probe(self):
        with patch(
            "src.profiles.mcp_probe._probe_http",
            new=AsyncMock(return_value=[ProbedTool(name="t2")]),
        ) as mock_http:
            result = await probe_server(_http_config(), timeout=5.0)
            mock_http.assert_awaited_once()
            assert result.ok
            assert [t.name for t in result.tools] == ["t2"]

    @pytest.mark.asyncio
    async def test_unknown_transport_returns_error(self):
        config = McpServerConfig(name="x", transport="ws")
        result = await probe_server(config, timeout=1.0)
        assert not result.ok
        assert "Unsupported transport" in (result.error or "")


# ---------------------------------------------------------------------------
# probe_server — timeout + error handling
# ---------------------------------------------------------------------------


class TestProbeServerTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_error_not_raise(self):
        async def slow_probe(*_a, **_kw):
            await asyncio.sleep(5.0)
            return []

        with patch("src.profiles.mcp_probe._probe_stdio", new=slow_probe):
            result = await probe_server(_stdio_config(), timeout=0.1)
        assert not result.ok
        assert "timed out" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_exception_in_transport_returns_error(self):
        async def boom(*_a, **_kw):
            raise ConnectionError("server down")

        with patch("src.profiles.mcp_probe._probe_http", new=boom):
            result = await probe_server(_http_config(), timeout=1.0)
        assert not result.ok
        assert "ConnectionError" in (result.error or "")
        assert "server down" in (result.error or "")

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        async def hang(*_a, **_kw):
            await asyncio.sleep(60)
            return []

        with patch("src.profiles.mcp_probe._probe_stdio", new=hang):
            task = asyncio.create_task(probe_server(_stdio_config(), timeout=30.0))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


# ---------------------------------------------------------------------------
# probe_many
# ---------------------------------------------------------------------------


class TestProbeMany:
    @pytest.mark.asyncio
    async def test_empty_input(self):
        assert await probe_many([]) == []

    @pytest.mark.asyncio
    async def test_runs_concurrently_results_in_order(self):
        async def fake_stdio(cmd, *_a, **_kw):
            # Sleep proportional to first arg so we'd see serial timing.
            delay = 0.2 if cmd == "slow" else 0.01
            await asyncio.sleep(delay)
            return [ProbedTool(name=cmd)]

        configs = [
            McpServerConfig(name="a", transport="stdio", command="slow"),
            McpServerConfig(name="b", transport="stdio", command="fast"),
            McpServerConfig(name="c", transport="stdio", command="fast"),
        ]

        with patch("src.profiles.mcp_probe._probe_stdio", new=fake_stdio):
            t0 = asyncio.get_event_loop().time()
            results = await probe_many(configs, timeout=5.0)
            elapsed = asyncio.get_event_loop().time() - t0

        assert [r.server_name for r in results] == ["a", "b", "c"]
        # Concurrent — total time ≈ slowest probe, not sum of all.
        assert elapsed < 0.5
        assert all(r.ok for r in results)

    @pytest.mark.asyncio
    async def test_one_failure_does_not_break_others(self):
        async def fake_stdio(cmd, *_a, **_kw):
            if cmd == "broken":
                raise ConnectionError("nope")
            return [ProbedTool(name=cmd)]

        configs = [
            McpServerConfig(name="a", transport="stdio", command="broken"),
            McpServerConfig(name="b", transport="stdio", command="ok"),
        ]
        with patch("src.profiles.mcp_probe._probe_stdio", new=fake_stdio):
            results = await probe_many(configs, timeout=5.0)

        assert len(results) == 2
        assert not results[0].ok
        assert results[1].ok


# ---------------------------------------------------------------------------
# Real-ish stdio probe (no SDK mocking) — verifies error path
# ---------------------------------------------------------------------------


class TestRealStdioErrorPath:
    @pytest.mark.asyncio
    async def test_nonexistent_binary_returns_error(self):
        config = McpServerConfig(
            name="bogus",
            transport="stdio",
            command="this-binary-does-not-exist-anywhere-12345",
        )
        # Real probe — let the SDK try and fail.  Should not raise.
        result = await probe_server(config, timeout=5.0)
        assert not result.ok
        assert result.error  # something descriptive

    @pytest.mark.asyncio
    async def test_command_that_does_not_speak_mcp_times_out(self):
        # `cat` reads stdin forever; the SDK's initialize will never
        # complete because cat doesn't speak MCP.  Should hit timeout.
        config = McpServerConfig(
            name="cat",
            transport="stdio",
            command="cat",
        )
        result = await probe_server(config, timeout=0.5)
        assert not result.ok
        # Either a timeout or an SDK-side handshake error — both are valid
        # "this isn't a real MCP server" outcomes.
        assert result.error is not None


# ---------------------------------------------------------------------------
# ProbeResult API
# ---------------------------------------------------------------------------


class TestProbeResult:
    def test_to_dict(self):
        r = ProbeResult(
            server_name="x",
            transport="stdio",
            tools=[ProbedTool(name="a", description="d")],
            probed_at=123.0,
        )
        d = r.to_dict()
        assert d["server_name"] == "x"
        assert d["transport"] == "stdio"
        assert d["tools"] == [{"name": "a", "description": "d", "input_schema": {}}]
        assert d["error"] is None
        assert d["ok"] is True

    def test_to_dict_with_error(self):
        r = ProbeResult(server_name="x", transport="http", error="boom")
        d = r.to_dict()
        assert d["ok"] is False
        assert d["error"] == "boom"
        assert d["tools"] == []
