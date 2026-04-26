"""In-memory tool catalog — what tools each MCP server exposes.

Populated by probes against every registry entry on startup, kept current
by the registry's vault watcher (see ``on_reload`` hook in
:func:`~src.profiles.mcp_registry.register_mcp_server_handlers`).  Lives
entirely in process memory; no DB table.

The synthetic ``agent-queue`` entry (the embedded MCP server inside the
daemon) is **not** probed over its declared HTTP transport — that would
be a circular call that requires the daemon to be fully up before the
registry can populate itself.  Instead the caller injects a
``builtin_resolver`` callable that returns the tool list directly from
the in-process command handler + plugin registry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.profiles.mcp_probe import ProbedTool, ProbeResult, probe_server

if TYPE_CHECKING:
    from src.profiles.mcp_registry import McpRegistry, McpServerConfig

logger = logging.getLogger(__name__)


# A resolver returns the tool list for the embedded agent-queue server in
# process — no network, no subprocess.
BuiltinResolver = Callable[[], list[ProbedTool]]


@dataclass
class CatalogEntry:
    """One server's snapshot in the tool catalog."""

    server_name: str
    project_id: str | None  # None = system scope
    transport: str
    tools: list[ProbedTool] = field(default_factory=list)
    last_probed_at: float = 0.0
    last_error: str | None = None
    is_builtin: bool = False

    @property
    def scope_key(self) -> tuple[str | None, str]:
        return (self.project_id, self.server_name)

    @property
    def ok(self) -> bool:
        return self.last_error is None

    def to_dict(self) -> dict:
        return {
            "server_name": self.server_name,
            "project_id": self.project_id,
            "scope": "project" if self.project_id else "system",
            "transport": self.transport,
            "tools": [t.to_dict() for t in self.tools],
            "tool_count": len(self.tools),
            "last_probed_at": self.last_probed_at,
            "last_error": self.last_error,
            "ok": self.ok,
            "is_builtin": self.is_builtin,
        }


# ---------------------------------------------------------------------------
# In-memory catalog store
# ---------------------------------------------------------------------------


class McpToolCatalog:
    """Per-server tool snapshot, scoped the same way as the registry."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str | None, str], CatalogEntry] = {}

    def upsert(self, entry: CatalogEntry) -> None:
        self._entries[entry.scope_key] = entry

    def remove(self, server_name: str, project_id: str | None = None) -> bool:
        return self._entries.pop((project_id, server_name), None) is not None

    def clear(self) -> None:
        self._entries.clear()

    def get(self, server_name: str, project_id: str | None = None) -> CatalogEntry | None:
        """Project-first, system-fallback lookup (matches registry)."""
        if project_id is not None:
            scoped = self._entries.get((project_id, server_name))
            if scoped is not None:
                return scoped
        return self._entries.get((None, server_name))

    def list_for_scope(self, project_id: str | None = None) -> list[CatalogEntry]:
        """Return every entry visible to a scope.

        ``project_id=None`` returns system entries only.  Scoped lookups
        return project entries plus any system entries whose names aren't
        shadowed by a project entry.
        """
        if project_id is None:
            return sorted(
                (e for (pid, _), e in self._entries.items() if pid is None),
                key=lambda e: e.server_name,
            )
        project_names = {n for (pid, n) in self._entries if pid == project_id}
        result: list[CatalogEntry] = []
        for (pid, name), entry in self._entries.items():
            if pid == project_id:
                result.append(entry)
            elif pid is None and name not in project_names:
                result.append(entry)
        return sorted(result, key=lambda e: e.server_name)

    def list_all(self) -> list[CatalogEntry]:
        return sorted(
            self._entries.values(),
            key=lambda e: ((e.project_id or ""), e.server_name),
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: tuple[str | None, str]) -> bool:
        return key in self._entries


# ---------------------------------------------------------------------------
# Builtin resolver (agent-queue embedded server)
# ---------------------------------------------------------------------------


def build_agent_queue_tools(
    *,
    excluded: set[str],
    plugin_tools: list[dict] | None = None,
) -> list[ProbedTool]:
    """Enumerate the tools the embedded agent-queue MCP server exposes.

    Mirrors the registration order used by
    :func:`src.mcp_registration.register_command_tools` but stops at the
    explicit + plugin passes — auto-discovered ``_cmd_*`` methods that
    lack a tool definition are intentionally excluded from the catalog
    because they have no rich schema and rarely belong in a user-facing
    tool picker.
    """
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS

    seen: set[str] = set()
    tools: list[ProbedTool] = []

    for td in _ALL_TOOL_DEFINITIONS:
        name = td.get("name", "")
        if not name or name in excluded or name in seen:
            continue
        seen.add(name)
        tools.append(
            ProbedTool(
                name=name,
                description=td.get("description", ""),
                input_schema=td.get("input_schema", {}),
            )
        )

    for td in plugin_tools or []:
        name = td.get("name", "")
        if not name or name in excluded or name in seen:
            continue
        seen.add(name)
        tools.append(
            ProbedTool(
                name=name,
                description=td.get("description", ""),
                input_schema=td.get("input_schema", {}),
            )
        )

    return tools


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------


def _probe_to_entry(
    config: McpServerConfig,
    result: ProbeResult,
) -> CatalogEntry:
    return CatalogEntry(
        server_name=config.name,
        project_id=config.project_id,
        transport=config.transport,
        tools=list(result.tools),
        last_probed_at=result.probed_at,
        last_error=result.error,
        is_builtin=config.is_builtin,
    )


def _builtin_entry(
    config: McpServerConfig,
    resolver: BuiltinResolver,
) -> CatalogEntry:
    try:
        tools = list(resolver())
        error: str | None = None
    except Exception as exc:
        logger.warning("Builtin tool resolver raised for %s: %s", config.name, exc)
        tools = []
        error = f"{type(exc).__name__}: {exc}"
    return CatalogEntry(
        server_name=config.name,
        project_id=config.project_id,
        transport=config.transport,
        tools=tools,
        last_probed_at=time.time(),
        last_error=error,
        is_builtin=True,
    )


async def refresh_one(
    catalog: McpToolCatalog,
    config: McpServerConfig,
    *,
    builtin_resolver: BuiltinResolver | None = None,
    timeout: float = 10.0,
) -> CatalogEntry:
    """Probe a single server and upsert the resulting catalog entry.

    Returns the entry that was written.  Builtins use the resolver and
    skip the network probe entirely.
    """
    if config.is_builtin:
        if builtin_resolver is None:
            entry = CatalogEntry(
                server_name=config.name,
                project_id=config.project_id,
                transport=config.transport,
                tools=[],
                last_probed_at=time.time(),
                last_error="no builtin resolver configured",
                is_builtin=True,
            )
        else:
            entry = _builtin_entry(config, builtin_resolver)
    else:
        result = await probe_server(config, timeout=timeout)
        entry = _probe_to_entry(config, result)

    catalog.upsert(entry)
    return entry


async def populate_catalog(
    registry: McpRegistry,
    catalog: McpToolCatalog,
    *,
    builtin_resolver: BuiltinResolver | None = None,
    timeout: float = 10.0,
) -> list[CatalogEntry]:
    """Probe every entry in the registry and rebuild the catalog.

    Probes run concurrently via :func:`asyncio.gather`.  Existing catalog
    entries for servers that are no longer in the registry are dropped.
    Returns the list of entries written (in registry order).
    """
    configs = registry.list_all()

    # Drop catalog entries for servers that no longer exist in the registry.
    keep_keys = {(c.project_id, c.name) for c in configs}
    for stale_key in [k for k in list(catalog._entries) if k not in keep_keys]:
        del catalog._entries[stale_key]

    if not configs:
        logger.info("MCP tool catalog: registry is empty, nothing to probe")
        return []

    results = await asyncio.gather(
        *(
            refresh_one(
                catalog,
                c,
                builtin_resolver=builtin_resolver,
                timeout=timeout,
            )
            for c in configs
        ),
        return_exceptions=False,
    )

    ok = sum(1 for e in results if e.ok)
    logger.info(
        "MCP tool catalog populated: %d entries (%d ok, %d failed)",
        len(results),
        ok,
        len(results) - ok,
    )
    return list(results)


def make_reload_hook(
    registry: McpRegistry,
    catalog: McpToolCatalog,
    *,
    builtin_resolver: BuiltinResolver | None = None,
    timeout: float = 10.0,
) -> Callable:
    """Build the ``on_reload`` callback for the registry's vault watcher.

    Returns a coroutine function that re-probes any server keys that the
    watcher reports as touched (added or modified).  Removed entries are
    handled by the watcher's delete branch (they vanish from the registry,
    and :func:`populate_catalog` would then drop them — but the simpler
    path here is to drop them from the catalog inline).
    """

    async def _on_reload(touched: list[tuple[str | None, str]]) -> None:
        for project_id, name in touched:
            config = registry.get(name, project_id=project_id)
            if config is None:
                # Removed — drop catalog entry too.
                catalog.remove(name, project_id=project_id)
                continue
            try:
                await refresh_one(
                    catalog,
                    config,
                    builtin_resolver=builtin_resolver,
                    timeout=timeout,
                )
            except Exception:
                logger.exception("Catalog refresh failed for %s", name)

    return _on_reload
