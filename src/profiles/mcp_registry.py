"""MCP server registry — vault-sourced, in-memory store of MCP server configs.

Profiles reference MCP servers by **name** (``agent_profiles.mcp_servers`` is a
``list[str]``).  At task launch the orchestrator resolves each name against
this registry to produce the dict the agent adapter (Claude SDK) expects.

Storage model
-------------
Each MCP server config lives as a single markdown file in the vault:

- ``vault/mcp-servers/<name>.md`` — system-scope (visible to every project).
- ``vault/projects/<pid>/mcp-servers/<name>.md`` — project-scope (visible to
  one project; **shadows** any system-scope entry of the same name).

The vault file watcher reparses on change and updates the in-memory store.
There is no DB table for the registry — it is purely a vault projection.

The embedded ``agent-queue`` server (the daemon's own MCP endpoint that
surfaces plugin tools) is added as a synthetic system-scope entry on
:meth:`McpRegistry.set_builtin` and is marked ``is_builtin=True`` so the
dashboard can hide Edit/Delete actions and the registry can refuse CRUD
that would clobber it.

Markdown format
---------------
Frontmatter holds the entire structured config; the body is descriptive
notes that are not parsed::

    ---
    name: playwright
    transport: stdio
    description: Browser automation for web testing
    command: npx
    args:
      - "@anthropic/mcp-playwright"
    env:
      PWDEBUG: "0"
    ---

    # Playwright

    Free-form notes go here (not parsed).

For HTTP servers::

    ---
    name: my-api
    transport: http
    description: Internal API access
    url: https://api.internal/mcp
    headers:
      Authorization: Bearer ${API_TOKEN}
    ---
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.vault_watcher import VaultChange, VaultWatcher

# Type alias for the optional reload callback fired after a watcher batch.
OnReloadHook = Callable[[list[tuple[str | None, str]]], Awaitable[None]]

logger = logging.getLogger(__name__)


# Glob patterns matched by the vault watcher.  System scope plus per-project
# scope; both contain one .md file per server.
MCP_SERVER_PATTERNS: list[str] = [
    "mcp-servers/*.md",
    "projects/*/mcp-servers/*.md",
]


VALID_TRANSPORTS: frozenset[str] = frozenset({"stdio", "http"})


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class McpServerConfig:
    """A single MCP server registry entry.

    ``project_id=None`` means system scope.  ``is_builtin=True`` marks the
    embedded ``agent-queue`` entry so the registry refuses CRUD on it.
    """

    name: str
    transport: str  # "stdio" | "http"
    project_id: str | None = None  # None = system scope
    description: str = ""

    # stdio fields
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    # http fields
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    # Body of the markdown file (notes; not parsed).
    notes: str = ""

    # True for synthetic entries (the embedded agent-queue server).  The
    # registry refuses upsert/remove on builtins so a stray vault file
    # cannot replace the daemon's own MCP endpoint.
    is_builtin: bool = False

    @property
    def scope_key(self) -> tuple[str | None, str]:
        """Composite key used for in-memory lookup."""
        return (self.project_id, self.name)

    def to_adapter_dict(self) -> dict:
        """Render the dict the Claude adapter / agent SDK expects.

        For HTTP transports::

            {"type": "http", "url": "...", "headers": {...}}

        For stdio transports::

            {"command": "...", "args": [...], "env": {...}}

        Empty optional fields (env, headers) are omitted.
        """
        if self.transport == "http":
            d: dict = {"type": "http", "url": self.url}
            if self.headers:
                d["headers"] = dict(self.headers)
            return d
        # stdio
        d = {"command": self.command, "args": list(self.args)}
        if self.env:
            d["env"] = dict(self.env)
        return d

    def to_summary_dict(self) -> dict:
        """Compact dict for list-mcp-servers responses."""
        d: dict = {
            "name": self.name,
            "transport": self.transport,
            "scope": "project" if self.project_id else "system",
            "description": self.description,
            "is_builtin": self.is_builtin,
        }
        if self.project_id:
            d["project_id"] = self.project_id
        return d


# ---------------------------------------------------------------------------
# Markdown parse + render
# ---------------------------------------------------------------------------


@dataclass
class ParsedMcpServer:
    """Result of parsing an MCP server markdown file."""

    config: McpServerConfig | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.config is not None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter from the start of a markdown file.

    Returns ``(data, body)``.  Returns ``({}, text)`` if no frontmatter or
    if the YAML is malformed.
    """
    if not text or not text.lstrip().startswith("---"):
        return {}, text

    stripped = text.lstrip()
    after_open = stripped[3:].lstrip("\r\n")

    close_idx = after_open.find("\n---")
    if close_idx == -1:
        return {}, text

    yaml_text = after_open[:close_idx]
    body = after_open[close_idx + 4 :].lstrip("\r\n")

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return {}, text

    if not isinstance(data, dict):
        return {}, text

    return data, body


def parse_server_markdown(
    text: str,
    *,
    fallback_name: str | None = None,
    project_id: str | None = None,
) -> ParsedMcpServer:
    """Parse markdown text into an :class:`McpServerConfig`.

    Parameters
    ----------
    text:
        Raw markdown content of an mcp-server file.
    fallback_name:
        Used when the frontmatter omits ``name`` (typically derived from the
        file path via :func:`derive_server_id`).
    project_id:
        The project scope this file belongs to (``None`` for system scope).
        Derived from the vault path; never read from frontmatter.

    Returns
    -------
    ParsedMcpServer
        ``is_valid`` is ``True`` when no fatal errors occurred.
    """
    result = ParsedMcpServer()

    if not text or not text.strip():
        result.errors.append("Empty file")
        return result

    data, body = _parse_frontmatter(text)
    if not data:
        result.errors.append(
            "Missing or invalid YAML frontmatter — wrap the config between '---' markers"
        )
        return result

    name = str(data.get("name") or fallback_name or "").strip()
    if not name:
        result.errors.append(
            "Missing required field 'name' (set in frontmatter or use a recognised vault path)"
        )
        return result

    transport = str(data.get("transport") or "").strip().lower()
    if transport not in VALID_TRANSPORTS:
        result.errors.append(
            f"Invalid 'transport': {data.get('transport')!r}. "
            f"Must be one of {sorted(VALID_TRANSPORTS)}"
        )
        return result

    config = McpServerConfig(
        name=name,
        transport=transport,
        project_id=project_id,
        description=str(data.get("description") or "").strip(),
        notes=body.strip(),
    )

    if transport == "stdio":
        command = data.get("command")
        if not isinstance(command, str) or not command.strip():
            result.errors.append("stdio transport requires a non-empty 'command' string")
            return result
        config.command = command.strip()

        raw_args = data.get("args", [])
        if raw_args is None:
            raw_args = []
        if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
            result.errors.append("'args' must be a list of strings")
            return result
        config.args = list(raw_args)

        raw_env = data.get("env", {}) or {}
        if not isinstance(raw_env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()
        ):
            result.errors.append("'env' must be an object mapping strings to strings")
            return result
        config.env = dict(raw_env)
    else:  # http
        url = data.get("url")
        if not isinstance(url, str) or not url.strip():
            result.errors.append("http transport requires a non-empty 'url' string")
            return result
        config.url = url.strip()

        raw_headers = data.get("headers", {}) or {}
        if not isinstance(raw_headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw_headers.items()
        ):
            result.errors.append("'headers' must be an object mapping strings to strings")
            return result
        config.headers = dict(raw_headers)

    result.config = config
    return result


def render_server_markdown(config: McpServerConfig) -> str:
    """Render an :class:`McpServerConfig` to vault-format markdown."""
    fm: dict = {
        "name": config.name,
        "transport": config.transport,
    }
    if config.description:
        fm["description"] = config.description

    if config.transport == "stdio":
        fm["command"] = config.command
        if config.args:
            fm["args"] = list(config.args)
        if config.env:
            fm["env"] = dict(config.env)
    else:
        fm["url"] = config.url
        if config.headers:
            fm["headers"] = dict(config.headers)

    parts = ["---", yaml.dump(fm, default_flow_style=False, sort_keys=False).rstrip(), "---", ""]
    parts.append(f"# {config.name}")
    parts.append("")
    if config.notes:
        parts.append(config.notes)
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Path derivation
# ---------------------------------------------------------------------------


def derive_server_id(rel_path: str) -> tuple[str | None, str] | None:
    """Derive ``(project_id, name)`` from a vault-relative path.

    - ``mcp-servers/<name>.md`` → ``(None, name)`` (system scope)
    - ``projects/<pid>/mcp-servers/<name>.md`` → ``(pid, name)`` (project scope)
    - Any other path returns ``None``.
    """
    parts = rel_path.replace("\\", "/").split("/")

    # projects/<pid>/mcp-servers/<name>.md
    if (
        len(parts) == 4
        and parts[0] == "projects"
        and parts[2] == "mcp-servers"
        and parts[-1].endswith(".md")
    ):
        return (parts[1], parts[-1][:-3])

    # mcp-servers/<name>.md
    if len(parts) == 2 and parts[0] == "mcp-servers" and parts[-1].endswith(".md"):
        return (None, parts[-1][:-3])

    return None


def vault_path_for(data_dir: str, name: str, project_id: str | None) -> str:
    """Return the absolute vault path where this server's markdown lives."""
    base = os.path.join(data_dir, "vault")
    if project_id:
        return os.path.join(base, "projects", project_id, "mcp-servers", f"{name}.md")
    return os.path.join(base, "mcp-servers", f"{name}.md")


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------


class McpRegistry:
    """In-memory store of MCP server configs.

    Populated by :meth:`load_from_vault` at startup and kept current by
    watcher callbacks (:func:`register_mcp_server_handlers`).  Lookup is
    project-first, system-fallback (matches the profile resolution cascade).
    """

    def __init__(self) -> None:
        # (project_id, name) -> McpServerConfig
        self._configs: dict[tuple[str | None, str], McpServerConfig] = {}

    # ------------- mutation -------------

    def upsert(self, config: McpServerConfig, *, allow_builtin: bool = False) -> None:
        """Insert or replace a config.  Refuses to clobber a builtin entry.

        ``allow_builtin=True`` is required when seeding the synthetic
        ``agent-queue`` entry via :meth:`set_builtin`.
        """
        existing = self._configs.get(config.scope_key)
        if existing is not None and existing.is_builtin and not allow_builtin:
            raise ValueError(
                f"Cannot replace built-in MCP server '{config.name}' "
                f"(scope={config.project_id or 'system'})"
            )
        self._configs[config.scope_key] = config

    def remove(self, name: str, project_id: str | None = None) -> bool:
        """Remove an entry.  Returns False if not found.  Refuses builtins."""
        existing = self._configs.get((project_id, name))
        if existing is None:
            return False
        if existing.is_builtin:
            raise ValueError(
                f"Cannot remove built-in MCP server '{name}' (scope={project_id or 'system'})"
            )
        del self._configs[(project_id, name)]
        return True

    def clear(self) -> None:
        """Drop all entries.  Used between vault reloads in tests."""
        self._configs.clear()

    # ------------- lookup -------------

    def get(self, name: str, project_id: str | None = None) -> McpServerConfig | None:
        """Resolve a server name in the given scope.

        Project scope is checked first, then system scope.  ``project_id=None``
        skips the project lookup entirely.
        """
        if project_id is not None:
            scoped = self._configs.get((project_id, name))
            if scoped is not None:
                return scoped
        return self._configs.get((None, name))

    def list_for_scope(self, project_id: str | None = None) -> list[McpServerConfig]:
        """Return every server visible in the given scope.

        For ``project_id=None`` returns the system-scope entries only.  For a
        project, returns the project's entries plus inherited system entries
        whose names aren't shadowed by a project-scope entry.
        """
        if project_id is None:
            return sorted(
                (c for (pid, _), c in self._configs.items() if pid is None),
                key=lambda c: c.name,
            )

        project_names = {n for (pid, n) in self._configs if pid == project_id}
        result: list[McpServerConfig] = []
        for (pid, name), config in self._configs.items():
            if pid == project_id:
                result.append(config)
            elif pid is None and name not in project_names:
                result.append(config)
        return sorted(result, key=lambda c: c.name)

    def list_all(self) -> list[McpServerConfig]:
        """Return every entry across every scope (system + all projects)."""
        return sorted(
            self._configs.values(),
            key=lambda c: ((c.project_id or ""), c.name),
        )

    def __len__(self) -> int:
        return len(self._configs)

    def __contains__(self, key: tuple[str | None, str]) -> bool:
        return key in self._configs

    # ------------- builtin -------------

    def set_builtin(self, config: McpServerConfig) -> None:
        """Seed a synthetic always-present entry (the embedded daemon).

        Marks the config as ``is_builtin=True`` and bypasses the upsert
        guard.  Calling again replaces the previous builtin (e.g. after
        config reload).
        """
        config.is_builtin = True
        self._configs[config.scope_key] = config


# ---------------------------------------------------------------------------
# Vault scan + watcher integration
# ---------------------------------------------------------------------------


def _vault_root(data_dir: str) -> str:
    return os.path.join(data_dir, "vault")


def _iter_server_files(vault_root: str):
    """Yield ``(abs_path, rel_path)`` for every mcp-server file in the vault."""
    if not os.path.isdir(vault_root):
        return

    # System scope
    sys_dir = os.path.join(vault_root, "mcp-servers")
    if os.path.isdir(sys_dir):
        for fname in os.listdir(sys_dir):
            if fname.startswith(".") or not fname.endswith(".md"):
                continue
            abs_path = os.path.join(sys_dir, fname)
            if os.path.isfile(abs_path):
                yield abs_path, f"mcp-servers/{fname}"

    # Project scope
    projects_dir = os.path.join(vault_root, "projects")
    if not os.path.isdir(projects_dir):
        return
    for project_name in os.listdir(projects_dir):
        if project_name.startswith("."):
            continue
        project_mcp_dir = os.path.join(projects_dir, project_name, "mcp-servers")
        if not os.path.isdir(project_mcp_dir):
            continue
        for fname in os.listdir(project_mcp_dir):
            if fname.startswith(".") or not fname.endswith(".md"):
                continue
            abs_path = os.path.join(project_mcp_dir, fname)
            if os.path.isfile(abs_path):
                yield abs_path, f"projects/{project_name}/mcp-servers/{fname}"


def load_from_vault(registry: McpRegistry, vault_root: str) -> list[str]:
    """Populate the registry from every mcp-server file under ``vault_root``.

    Existing user entries (non-builtin) are dropped first so this can be
    used for full reloads.  Built-in entries are preserved.

    Returns the list of error strings encountered (one per malformed file).
    """
    errors: list[str] = []

    # Drop user entries; keep builtins.
    for key in [k for k, v in list(registry._configs.items()) if not v.is_builtin]:
        del registry._configs[key]

    for abs_path, rel_path in _iter_server_files(vault_root):
        derived = derive_server_id(rel_path)
        if derived is None:
            continue
        project_id, fallback_name = derived

        try:
            text = Path(abs_path).read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{rel_path}: read failed: {exc}")
            continue

        parsed = parse_server_markdown(text, fallback_name=fallback_name, project_id=project_id)
        if not parsed.is_valid or parsed.config is None:
            errors.append(f"{rel_path}: {'; '.join(parsed.errors)}")
            logger.warning("MCP registry: skipping %s: %s", rel_path, parsed.errors)
            continue

        try:
            registry.upsert(parsed.config)
        except ValueError as exc:
            errors.append(f"{rel_path}: {exc}")

    logger.info(
        "MCP registry loaded: %d entries (%d errors)",
        len(registry),
        len(errors),
    )
    return errors


def builtin_from_config(config: AppConfig) -> McpServerConfig | None:
    """Build the synthetic ``agent-queue`` entry from the daemon's config.

    Returns ``None`` when the embedded MCP server is disabled — in which
    case the registry simply has no ``agent-queue`` entry and any profile
    referencing it will error at task launch.
    """
    entry = config.mcp_server.task_mcp_entry()
    if not entry:
        return None
    name, sconf = next(iter(entry.items()))
    return McpServerConfig(
        name=name,
        transport="http",
        url=str(sconf.get("url", "")),
        description="Embedded agent-queue MCP server (always available).",
        is_builtin=True,
    )


async def _on_mcp_server_changed(
    changes: list[VaultChange],
    *,
    registry: McpRegistry,
    vault_root: str,
    on_reload: OnReloadHook | None = None,
) -> None:
    """Watcher callback — reparse changed files and update the registry.

    ``on_reload`` (when provided) is awaited once after the batch is applied.
    The probe layer wires this so the tool catalog re-probes any added or
    modified server.
    """
    touched: list[tuple[str | None, str]] = []

    for change in changes:
        derived = derive_server_id(change.rel_path)
        if derived is None:
            continue
        project_id, name = derived
        key = (project_id, name)

        if change.operation == "deleted":
            try:
                if registry.remove(name, project_id):
                    logger.info(
                        "MCP registry: removed %s (scope=%s)",
                        name,
                        project_id or "system",
                    )
                    touched.append(key)
            except ValueError as exc:
                logger.warning("MCP registry: %s", exc)
            continue

        # created or modified — read and parse
        try:
            text = Path(change.path).read_text(encoding="utf-8")
        except OSError:
            logger.error("MCP registry: cannot read %s", change.path, exc_info=True)
            continue

        parsed = parse_server_markdown(text, fallback_name=name, project_id=project_id)
        if not parsed.is_valid or parsed.config is None:
            logger.warning(
                "MCP registry: %s parse failed: %s — keeping previous entry",
                change.rel_path,
                parsed.errors,
            )
            continue

        try:
            registry.upsert(parsed.config)
            touched.append(key)
            logger.info(
                "MCP registry: %s %s (scope=%s)",
                change.operation,
                name,
                project_id or "system",
            )
        except ValueError as exc:
            logger.warning("MCP registry: %s", exc)

    if touched and on_reload is not None:
        try:
            await on_reload(touched)
        except Exception:
            logger.exception("MCP registry on_reload hook raised")


def register_mcp_server_handlers(
    watcher: VaultWatcher,
    registry: McpRegistry,
    *,
    vault_root: str,
    on_reload: OnReloadHook | None = None,
) -> list[str]:
    """Register vault-watcher handlers for both registry scopes.

    Returns the list of handler IDs (for unregistration).
    """

    async def _handler(changes: list[VaultChange]) -> None:
        await _on_mcp_server_changed(
            changes,
            registry=registry,
            vault_root=vault_root,
            on_reload=on_reload,
        )

    handler_ids: list[str] = []
    for pattern in MCP_SERVER_PATTERNS:
        hid = watcher.register_handler(
            pattern,
            _handler,
            handler_id=f"mcp-server:{pattern}",
        )
        handler_ids.append(hid)

    logger.info(
        "MCP registry: registered %d handler(s) for mcp-server patterns",
        len(handler_ids),
    )
    return handler_ids
