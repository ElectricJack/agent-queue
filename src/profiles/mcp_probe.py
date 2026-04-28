"""MCP server probe — connect to a server and list its tools.

Used by the tool catalog (B3) to populate the per-server tool list shown in
the dashboard's tool picker.  Builtin servers (the embedded ``agent-queue``
endpoint) are *not* probed by this module — callers resolve their tools
in-process via the plugin registry.

Probes always run inside ``asyncio.wait_for`` with a hard timeout so a hung
or slow server cannot stall the daemon's event loop.  On timeout the stdio
subprocess is cancelled (which terminates the child via the SDK's stream
context manager) and an error is returned in :class:`ProbeResult` rather
than raised.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.profiles.mcp_registry import McpServerConfig

logger = logging.getLogger(__name__)


DEFAULT_PROBE_TIMEOUT = 10.0


@dataclass
class ProbedTool:
    """One tool surfaced by an MCP server's ``tools/list`` response."""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ProbeResult:
    """Outcome of one probe attempt."""

    server_name: str
    transport: str
    tools: list[ProbedTool] = field(default_factory=list)
    error: str | None = None
    probed_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return {
            "server_name": self.server_name,
            "transport": self.transport,
            "tools": [t.to_dict() for t in self.tools],
            "error": self.error,
            "probed_at": self.probed_at,
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_tool(raw) -> ProbedTool:
    """Convert an SDK ``Tool`` (or compatible dict) to :class:`ProbedTool`."""
    if isinstance(raw, dict):
        return ProbedTool(
            name=str(raw.get("name", "")),
            description=str(raw.get("description") or ""),
            input_schema=dict(raw.get("inputSchema") or raw.get("input_schema") or {}),
        )
    # SDK pydantic model — attribute access.
    return ProbedTool(
        name=str(getattr(raw, "name", "")),
        description=str(getattr(raw, "description", None) or ""),
        input_schema=dict(getattr(raw, "inputSchema", None) or {}),
    )


async def _list_tools_via_session(read_stream, write_stream) -> list[ProbedTool]:
    """Open a ClientSession on the streams, initialise, and list tools."""
    from mcp import ClientSession

    async with ClientSession(read_stream, write_stream) as session:
        await session.initialize()
        result = await session.list_tools()
    return [_normalise_tool(t) for t in (result.tools or [])]


async def _probe_stdio(
    command: str,
    args: list[str],
    env: dict[str, str] | None,
) -> list[ProbedTool]:
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=command,
        args=list(args or []),
        env=dict(env) if env else None,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        return await _list_tools_via_session(read_stream, write_stream)


async def _probe_http(
    url: str,
    headers: dict[str, str] | None,
) -> list[ProbedTool]:
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=dict(headers) if headers else None) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        return await _list_tools_via_session(read_stream, write_stream)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def probe_server(
    config: McpServerConfig,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> ProbeResult:
    """Probe a single MCP server and return its tool list.

    Never raises — failures (connection refused, timeout, malformed
    responses) are caught and returned as ``ProbeResult.error``.

    Builtin servers should be resolved in-process by the caller (via
    ``plugin_registry.get_all_tool_definitions()``); this function will
    still try to probe them over their declared transport, which is rarely
    what you want.

    Parameters
    ----------
    config:
        The :class:`~src.profiles.mcp_registry.McpServerConfig` to probe.
    timeout:
        Hard ceiling on the probe in seconds.  The probe is wrapped in
        ``asyncio.wait_for``; on timeout the underlying connection (and
        any spawned subprocess) is cancelled and a timeout error is
        returned.
    """
    started = time.time()

    async def _do_probe() -> list[ProbedTool]:
        if config.transport == "stdio":
            return await _probe_stdio(config.command, config.args, config.env)
        if config.transport == "http":
            return await _probe_http(config.url, config.headers)
        raise ValueError(f"Unsupported transport: {config.transport!r}")

    try:
        tools = await asyncio.wait_for(_do_probe(), timeout=timeout)
        return ProbeResult(
            server_name=config.name,
            transport=config.transport,
            tools=tools,
            probed_at=started,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "MCP probe timed out after %.1fs: server=%s transport=%s",
            timeout,
            config.name,
            config.transport,
        )
        return ProbeResult(
            server_name=config.name,
            transport=config.transport,
            error=f"Probe timed out after {timeout:.0f}s",
            probed_at=started,
        )
    except asyncio.CancelledError:
        # Propagate cancellation — the caller is shutting us down.
        raise
    except Exception as exc:
        logger.warning(
            "MCP probe failed: server=%s transport=%s error=%s",
            config.name,
            config.transport,
            exc,
        )
        return ProbeResult(
            server_name=config.name,
            transport=config.transport,
            error=f"{type(exc).__name__}: {exc}",
            probed_at=started,
        )


async def probe_many(
    configs: list[McpServerConfig],
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> list[ProbeResult]:
    """Probe many servers concurrently.

    Each probe runs inside its own ``wait_for`` so a single hung server
    cannot delay the rest beyond ``timeout``.  Returns one result per
    input config in the same order.
    """
    if not configs:
        return []
    return list(
        await asyncio.gather(
            *(probe_server(c, timeout=timeout) for c in configs),
            return_exceptions=False,
        )
    )
