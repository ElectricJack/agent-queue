"""MCP server commands mixin — registry CRUD + probe/catalog reads.

Wraps :mod:`src.profiles.mcp_registry` and :mod:`src.profiles.mcp_catalog` so
they're reachable from Discord, MCP clients, the ``aq`` CLI, and the
dashboard via the standard ``CommandHandler.execute`` path.

Mutations write the vault markdown file first; the vault watcher reparses
and updates the in-memory registry.  An immediate probe is also issued so
the tool catalog reflects the new server before the next read.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _server_to_response(entry: Any) -> dict:
    """Common serialization helper for registry + catalog responses."""
    return entry.to_summary_dict() if hasattr(entry, "to_summary_dict") else dict(entry)


class McpCommandsMixin:
    """MCP server registry + tool catalog commands."""

    # -----------------------------------------------------------------------
    # Read-only commands
    # -----------------------------------------------------------------------

    async def _cmd_list_mcp_servers(self, args: dict) -> dict:
        """List MCP servers visible to a scope.

        Args:
            project_id: Optional. When provided, returns project-scoped
                servers plus inherited system entries.  Omit for system
                scope only.
        """
        project_id = (args.get("project_id") or "").strip() or None
        registry = getattr(self.orchestrator, "mcp_registry", None)
        catalog = getattr(self.orchestrator, "mcp_tool_catalog", None)
        if registry is None:
            return {"servers": [], "count": 0}

        configs = registry.list_for_scope(project_id)
        servers: list[dict] = []
        for c in configs:
            d = c.to_summary_dict()
            if catalog is not None:
                cat = catalog.get(c.name, project_id=c.project_id)
                if cat is not None:
                    d["tool_count"] = len(cat.tools)
                    d["last_probed_at"] = cat.last_probed_at
                    d["last_error"] = cat.last_error
            servers.append(d)

        return {"servers": servers, "count": len(servers)}

    async def _cmd_get_mcp_server(self, args: dict) -> dict:
        """Return a single MCP server's full config."""
        name = (args.get("name") or "").strip()
        if not name:
            return {"error": "name is required"}
        project_id = (args.get("project_id") or "").strip() or None
        registry = getattr(self.orchestrator, "mcp_registry", None)
        if registry is None:
            return {"error": "MCP registry not available"}

        # Lookup respects scope cascade (project first, then system).
        config = registry.get(name, project_id=project_id)
        if config is None:
            return {"error": f"MCP server '{name}' not found"}

        d = config.to_summary_dict()
        d["adapter_config"] = config.to_adapter_dict()
        # Surface inline config fields for dashboard editing.
        d["command"] = config.command
        d["args"] = list(config.args)
        d["env"] = dict(config.env)
        d["url"] = config.url
        d["headers"] = dict(config.headers)
        d["notes"] = config.notes
        return d

    async def _cmd_list_mcp_tool_catalog(self, args: dict) -> dict:
        """Return the cached tool list for one or more servers.

        Args:
            project_id: Optional scope for the listing.
            server_names: Optional list of server names to filter to.
        """
        project_id = (args.get("project_id") or "").strip() or None
        wanted = args.get("server_names") or []
        catalog = getattr(self.orchestrator, "mcp_tool_catalog", None)
        if catalog is None:
            return {"servers": {}}

        entries = catalog.list_for_scope(project_id)
        out: dict[str, dict] = {}
        for e in entries:
            if wanted and e.server_name not in wanted:
                continue
            out[e.server_name] = e.to_dict()
        return {"servers": out, "count": len(out)}

    async def _cmd_probe_mcp_server(self, args: dict) -> dict:
        """Probe a single server and refresh its catalog entry."""
        name = (args.get("name") or "").strip()
        if not name:
            return {"error": "name is required"}
        project_id = (args.get("project_id") or "").strip() or None

        registry = getattr(self.orchestrator, "mcp_registry", None)
        catalog = getattr(self.orchestrator, "mcp_tool_catalog", None)
        if registry is None or catalog is None:
            return {"error": "MCP registry/catalog not initialised"}

        config = registry.get(name, project_id=project_id)
        if config is None:
            return {"error": f"MCP server '{name}' not found"}

        from src.profiles.mcp_catalog import refresh_one

        resolver = getattr(self.orchestrator, "_mcp_builtin_resolver", None)
        entry = await refresh_one(catalog, config, builtin_resolver=resolver)
        return {"probed": entry.to_dict()}

    # -----------------------------------------------------------------------
    # Write commands (vault-write — watcher syncs in-memory registry)
    # -----------------------------------------------------------------------

    def _vault_mcp_server_path(self, name: str, project_id: str | None) -> str:
        """Compute the on-disk path for a registry markdown file."""
        from src.profiles.mcp_registry import vault_path_for

        return vault_path_for(self.config.data_dir, name, project_id)

    def _build_mcp_config(self, args: dict, *, project_id: str | None):
        """Validate args and build an :class:`McpServerConfig`."""
        from src.profiles.mcp_registry import McpServerConfig

        name = (args.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")

        transport = (args.get("transport") or "").strip().lower()
        if transport not in {"stdio", "http"}:
            raise ValueError("transport must be 'stdio' or 'http'")

        config = McpServerConfig(
            name=name,
            transport=transport,
            project_id=project_id,
            description=str(args.get("description") or "").strip(),
            notes=str(args.get("notes") or "").strip(),
        )
        if transport == "stdio":
            command = (args.get("command") or "").strip()
            if not command:
                raise ValueError("stdio transport requires 'command'")
            config.command = command
            raw_args = args.get("args") or []
            if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
                raise ValueError("'args' must be a list of strings")
            config.args = list(raw_args)
            raw_env = args.get("env") or {}
            if not isinstance(raw_env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()
            ):
                raise ValueError("'env' must be an object of strings to strings")
            config.env = dict(raw_env)
        else:
            url = (args.get("url") or "").strip()
            if not url:
                raise ValueError("http transport requires 'url'")
            config.url = url
            raw_headers = args.get("headers") or {}
            if not isinstance(raw_headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in raw_headers.items()
            ):
                raise ValueError("'headers' must be an object of strings to strings")
            config.headers = dict(raw_headers)
        return config

    async def _cmd_create_mcp_server(self, args: dict) -> dict:
        """Create a new registry entry by writing its vault markdown.

        Args:
            name: Slug-ish identifier.  Becomes the filename and the key
                profiles use to reference this server.
            transport: ``"stdio"`` or ``"http"``.
            project_id: Optional. Omit for system scope.
            command, args, env: stdio fields.
            url, headers: http fields.
            description, notes: optional.
        """
        from src.profiles.mcp_registry import render_server_markdown

        project_id = (args.get("project_id") or "").strip() or None
        try:
            config = self._build_mcp_config(args, project_id=project_id)
        except ValueError as exc:
            return {"error": str(exc)}

        # Refuse to clobber the embedded agent-queue builtin.
        registry = getattr(self.orchestrator, "mcp_registry", None)
        if registry is not None:
            existing = registry.get(config.name, project_id=project_id)
            if existing is not None and existing.is_builtin:
                return {
                    "error": (
                        f"Cannot create MCP server '{config.name}' — name is "
                        "reserved by the embedded agent-queue server"
                    )
                }

        target = self._vault_mcp_server_path(config.name, project_id)
        if os.path.isfile(target):
            return {
                "error": (
                    f"MCP server '{config.name}' already exists (scope={project_id or 'system'})"
                )
            }

        os.makedirs(os.path.dirname(target), exist_ok=True)
        Path(target).write_text(render_server_markdown(config), encoding="utf-8")

        # Eager update + probe so the dashboard sees the server immediately
        # without waiting for the watcher's debounce window.
        if registry is not None:
            registry.upsert(config)
        catalog = getattr(self.orchestrator, "mcp_tool_catalog", None)
        if catalog is not None:
            from src.profiles.mcp_catalog import refresh_one

            resolver = getattr(self.orchestrator, "_mcp_builtin_resolver", None)
            try:
                await refresh_one(catalog, config, builtin_resolver=resolver)
            except Exception:
                logger.debug("Eager probe failed for new MCP server %s", config.name, exc_info=True)

        return {
            "created": config.name,
            "scope": "project" if project_id else "system",
            "project_id": project_id,
            "path": target,
        }

    async def _cmd_edit_mcp_server(self, args: dict) -> dict:
        """Update fields on an existing registry entry.

        Loads the current markdown, merges the patch, writes back.
        Watcher picks up the change; an eager re-probe runs inline.
        """
        from src.profiles.mcp_registry import (
            parse_server_markdown,
            render_server_markdown,
        )

        name = (args.get("name") or "").strip()
        if not name:
            return {"error": "name is required"}
        project_id = (args.get("project_id") or "").strip() or None

        target = self._vault_mcp_server_path(name, project_id)
        if not os.path.isfile(target):
            return {
                "error": (
                    f"MCP server '{name}' not found in vault (scope={project_id or 'system'})"
                )
            }

        text = Path(target).read_text(encoding="utf-8")
        parsed = parse_server_markdown(text, fallback_name=name, project_id=project_id)
        if not parsed.is_valid or parsed.config is None:
            return {"error": f"Existing markdown is malformed: {parsed.errors}"}

        current = parsed.config
        # Apply patch — only fields explicitly present in args.
        merged_args: dict = {
            "name": name,
            "transport": args.get("transport") or current.transport,
            "description": args.get("description", current.description),
            "notes": args.get("notes", current.notes),
            "command": args.get("command", current.command),
            "args": args.get("args", list(current.args)),
            "env": args.get("env", dict(current.env)),
            "url": args.get("url", current.url),
            "headers": args.get("headers", dict(current.headers)),
        }
        try:
            new_config = self._build_mcp_config(merged_args, project_id=project_id)
        except ValueError as exc:
            return {"error": str(exc)}

        Path(target).write_text(render_server_markdown(new_config), encoding="utf-8")

        registry = getattr(self.orchestrator, "mcp_registry", None)
        if registry is not None:
            registry.upsert(new_config)
        catalog = getattr(self.orchestrator, "mcp_tool_catalog", None)
        if catalog is not None:
            from src.profiles.mcp_catalog import refresh_one

            resolver = getattr(self.orchestrator, "_mcp_builtin_resolver", None)
            try:
                await refresh_one(catalog, new_config, builtin_resolver=resolver)
            except Exception:
                logger.debug("Eager probe failed for edited MCP server %s", name, exc_info=True)

        return {
            "updated": name,
            "scope": "project" if project_id else "system",
            "path": target,
        }

    async def _cmd_delete_mcp_server(self, args: dict) -> dict:
        """Delete a registry entry's vault file.

        Refuses if any profile still references the name (returns the
        list of referencing profiles so the dashboard can deep-link
        each one for cleanup).
        """
        name = (args.get("name") or "").strip()
        if not name:
            return {"error": "name is required"}
        project_id = (args.get("project_id") or "").strip() or None

        registry = getattr(self.orchestrator, "mcp_registry", None)
        if registry is not None:
            existing = registry.get(name, project_id=project_id)
            if existing is not None and existing.is_builtin:
                return {"error": (f"Cannot delete '{name}' — it's the embedded agent-queue server")}

        # Find profiles referencing this name (any scope).  We only
        # block deletion when a project-scope deletion still has the
        # project's own profiles using it, or system deletion has any
        # profile (anywhere) using it.
        referencing: list[str] = []
        try:
            profiles = await self.db.list_profiles()
        except Exception:
            profiles = []
        for p in profiles:
            names = list(p.mcp_servers or [])
            if name not in names:
                continue
            if project_id is None:
                referencing.append(p.id)
            else:
                # Project-scope: only count profiles in this project.
                if p.id.startswith(f"project:{project_id}:"):
                    referencing.append(p.id)
        if referencing:
            return {
                "error": (
                    f"Cannot delete MCP server '{name}': still referenced by "
                    f"{len(referencing)} profile(s)"
                ),
                "referenced_by": referencing,
            }

        target = self._vault_mcp_server_path(name, project_id)
        if not os.path.isfile(target):
            return {"error": f"MCP server '{name}' not found"}

        os.remove(target)

        # Eager registry/catalog drop so subsequent reads are correct
        # before the watcher runs.
        if registry is not None:
            try:
                registry.remove(name, project_id=project_id)
            except ValueError:
                pass
        catalog = getattr(self.orchestrator, "mcp_tool_catalog", None)
        if catalog is not None:
            catalog.remove(name, project_id=project_id)

        return {
            "deleted": name,
            "scope": "project" if project_id else "system",
            "path": target,
        }
