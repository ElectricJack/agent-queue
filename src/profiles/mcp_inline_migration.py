"""One-shot migration: extract inline ``mcp_servers`` configs into the registry.

Older profiles (in vault markdown and in ``~/.agent-queue/config.yaml``) stored
the full MCP server config inline under ``mcp_servers``::

    mcp_servers:
      playwright:
        command: npx
        args: ["@anthropic/mcp-playwright"]

The new shape is a flat list of registry names::

    mcp_servers:
      - playwright

This module walks the vault on startup and rewrites every profile.md whose
``## MCP Servers`` block is still a dict.  For each inline entry it writes
a registry file at ``vault/[projects/<pid>/]mcp-servers/<name>.md`` (only
if the file does not already exist — never clobber a user-authored entry).
The profile.md is then rewritten with the new ``list[str]`` form.

The migration is idempotent: profiles whose ``mcp_servers`` is already a
list are untouched.  Re-running over already-migrated data is a no-op.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from src.profiles.mcp_registry import (
    McpServerConfig,
    render_server_markdown,
    vault_path_for,
)
from src.profiles.parser import (
    agent_profile_to_markdown,
    parse_profile,
)

if TYPE_CHECKING:
    from src.profiles.mcp_registry import McpRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ExtractedEntry:
    """One inline server config that the migration moved out to the registry."""

    name: str
    project_id: str | None
    target_path: str
    action: str  # "written" | "skipped" (file already existed)


@dataclass
class MigratedProfile:
    """One profile.md whose ``## MCP Servers`` block was rewritten."""

    profile_path: str
    profile_id: str | None
    extracted_names: list[str]


@dataclass
class InlineMigrationReport:
    """Aggregate result of one migration pass."""

    profiles_examined: int = 0
    profiles_migrated: int = 0
    extracted: list[ExtractedEntry] = field(default_factory=list)
    migrated_profiles: list[MigratedProfile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "profiles_examined": self.profiles_examined,
            "profiles_migrated": self.profiles_migrated,
            "extracted_count": len(self.extracted),
            "extracted": [
                {
                    "name": e.name,
                    "scope": "project" if e.project_id else "system",
                    "project_id": e.project_id,
                    "action": e.action,
                }
                for e in self.extracted
            ],
            "migrated_profiles": [
                {
                    "profile_path": m.profile_path,
                    "profile_id": m.profile_id,
                    "extracted_names": m.extracted_names,
                }
                for m in self.migrated_profiles
            ],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_transport(config: dict) -> str:
    """Guess transport from a legacy inline config dict.

    Inline configs from old profiles use either:
      * ``{"type": "http", "url": "...", "headers": {...}}`` for HTTP
      * ``{"type": "sdk", ...}`` (deprecated; treat as stdio fallback)
      * ``{"command": "...", "args": [...], "env": {...}}`` for stdio
    """
    if not isinstance(config, dict):
        return "stdio"
    if config.get("type") == "http":
        return "http"
    if "url" in config and "command" not in config:
        return "http"
    return "stdio"


def _inline_to_server_config(
    name: str,
    inline: dict,
    *,
    project_id: str | None,
) -> McpServerConfig | None:
    """Build an :class:`McpServerConfig` from a legacy inline config dict.

    Returns ``None`` when the inline data is too malformed to recover.
    """
    if not isinstance(inline, dict):
        return None

    transport = _infer_transport(inline)
    if transport == "http":
        url = inline.get("url")
        if not isinstance(url, str) or not url.strip():
            return None
        raw_headers = inline.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raw_headers = {}
        headers = {str(k): str(v) for k, v in raw_headers.items() if isinstance(k, str)}
        return McpServerConfig(
            name=name,
            transport="http",
            project_id=project_id,
            url=url.strip(),
            headers=headers,
            description=str(inline.get("description") or ""),
        )

    # stdio
    command = inline.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    raw_args = inline.get("args") or []
    if not isinstance(raw_args, list):
        raw_args = []
    args = [str(a) for a in raw_args if isinstance(a, str)]
    raw_env = inline.get("env") or {}
    if not isinstance(raw_env, dict):
        raw_env = {}
    env = {str(k): str(v) for k, v in raw_env.items() if isinstance(k, str)}
    return McpServerConfig(
        name=name,
        transport="stdio",
        project_id=project_id,
        command=command.strip(),
        args=args,
        env=env,
        description=str(inline.get("description") or ""),
    )


def coerce_mcp_server_names(mcp_servers) -> list[str]:
    """Normalise legacy + new ``mcp_servers`` field values to ``list[str]``.

    Accepts:
      * ``list[str]`` — returned as a copy with non-string entries dropped.
      * ``dict[str, dict]`` — keys taken as the registry names.
      * ``None`` / other → ``[]``.
    """
    if isinstance(mcp_servers, dict):
        return [str(k) for k in mcp_servers if isinstance(k, str)]
    if isinstance(mcp_servers, list):
        return [str(n) for n in mcp_servers if isinstance(n, str)]
    return []


def _profile_id_to_project(profile_id: str | None) -> str | None:
    """Project ID for a profile — only project-scoped profiles get one.

    System profiles → None (registry entry goes to vault/mcp-servers/).
    Project-scoped profiles (id like ``project:<pid>:<type>``) →
    ``<pid>`` (registry entry goes to ``vault/projects/<pid>/mcp-servers/``).
    """
    if not profile_id:
        return None
    if profile_id.startswith("project:"):
        parts = profile_id.split(":", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def _write_registry_file(
    data_dir: str,
    config: McpServerConfig,
) -> tuple[str, str]:
    """Write the registry markdown if it doesn't already exist.

    Returns ``(target_path, action)`` where action is ``"written"`` or
    ``"skipped"``.
    """
    target = vault_path_for(data_dir, config.name, config.project_id)
    if os.path.isfile(target):
        return target, "skipped"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    Path(target).write_text(render_server_markdown(config), encoding="utf-8")
    return target, "written"


# ---------------------------------------------------------------------------
# Profile.md migration
# ---------------------------------------------------------------------------


def _walk_profile_files(vault_root: str):
    """Yield (abs_path, rel_path) for every profile.md in the vault."""
    if not os.path.isdir(vault_root):
        return
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Skip hidden directories.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if fname != "profile.md":
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, vault_root).replace(os.sep, "/")
            yield abs_path, rel_path


def migrate_vault_profiles(
    vault_root: str,
    *,
    registry: McpRegistry | None = None,
) -> InlineMigrationReport:
    """Walk every profile.md and extract inline ``mcp_servers`` to the registry.

    Idempotent — profiles whose ``mcp_servers`` is already a list are
    untouched.  When a registry entry is added, the in-memory *registry*
    (if provided) is updated immediately so the catalog probe sees the
    new entries on the same startup pass.

    Errors per file are collected in the report; one bad file never blocks
    the rest of the walk.
    """
    report = InlineMigrationReport()
    data_dir = os.path.dirname(vault_root.rstrip(os.sep))

    for abs_path, rel_path in _walk_profile_files(vault_root):
        report.profiles_examined += 1

        try:
            text = Path(abs_path).read_text(encoding="utf-8")
        except OSError as exc:
            report.errors.append(f"{rel_path}: read failed: {exc}")
            continue

        parsed = parse_profile(text)
        # Note: parsed.errors may contain validation errors from the legacy
        # mcp_servers block itself (empty commands, malformed args).  We
        # *don't* bail on those — the migration extracts what it can and
        # records the rest in report.errors so the operator sees both.
        legacy = parsed.mcp_servers_legacy
        if not legacy:
            continue  # already migrated (or never had inline configs)

        # Profile id needed to compute target scope.
        profile_id = parsed.frontmatter.id or _derive_profile_id_from_path(rel_path)
        project_id = _profile_id_to_project(profile_id)

        extracted_names: list[str] = []
        for name, inline in legacy.items():
            sconfig = _inline_to_server_config(name, inline, project_id=project_id)
            if sconfig is None:
                report.errors.append(
                    f"{rel_path}: cannot extract inline config for '{name}' "
                    f"(scope={project_id or 'system'}); inline data dropped"
                )
                continue

            try:
                target, action = _write_registry_file(data_dir, sconfig)
            except OSError as exc:
                report.errors.append(f"{rel_path}: write failed for {name}: {exc}")
                continue

            report.extracted.append(
                ExtractedEntry(
                    name=name,
                    project_id=project_id,
                    target_path=target,
                    action=action,
                )
            )
            extracted_names.append(name)

            # Update the in-memory registry so the same-startup probe pass
            # sees the new entry.  `upsert` is no-op-safe if the entry was
            # already loaded from the file we just wrote.
            if registry is not None:
                try:
                    registry.upsert(sconfig)
                except ValueError:
                    pass  # never happens for non-builtin entries

        # Now rewrite the profile.md with the migrated list[str] form.
        # parse_profile already populated parsed.mcp_servers with the keys.
        try:
            new_text = _rewrite_profile_md(parsed, mcp_server_names=parsed.mcp_servers)
            Path(abs_path).write_text(new_text, encoding="utf-8")
            report.profiles_migrated += 1
            report.migrated_profiles.append(
                MigratedProfile(
                    profile_path=abs_path,
                    profile_id=profile_id,
                    extracted_names=extracted_names,
                )
            )
        except Exception as exc:
            report.errors.append(f"{rel_path}: rewrite failed: {exc}")

    if report.profiles_migrated or report.extracted:
        logger.info(
            "MCP inline migration: examined %d profile(s), migrated %d, "
            "extracted %d server config(s) (%d errors)",
            report.profiles_examined,
            report.profiles_migrated,
            len(report.extracted),
            len(report.errors),
        )
    else:
        logger.debug(
            "MCP inline migration: %d profile(s) examined, none required migration",
            report.profiles_examined,
        )

    return report


def _derive_profile_id_from_path(rel_path: str) -> str | None:
    """Mirror src.profiles.sync.derive_profile_id without the import cycle."""
    parts = rel_path.split("/")
    if (
        len(parts) == 5
        and parts[0] == "projects"
        and parts[2] == "agent-types"
        and parts[-1] == "profile.md"
    ):
        return f"project:{parts[1]}:{parts[3]}"
    if len(parts) >= 3 and parts[0] == "agent-types" and parts[-1] == "profile.md":
        return parts[1]
    return None


def _rewrite_profile_md(parsed, mcp_server_names: list[str]) -> str:
    """Render the parsed profile back to markdown with mcp_servers as a list."""
    fm = parsed.frontmatter
    return agent_profile_to_markdown(
        id=fm.id,
        name=fm.name or fm.id,
        description=str(fm.extra.get("description") or ""),
        model=str(parsed.config.get("model") or ""),
        permission_mode=str(parsed.config.get("permission_mode") or ""),
        allowed_tools=list(parsed.tools.get("allowed") or []),
        mcp_servers=list(mcp_server_names) if mcp_server_names else None,
        install=parsed.install if parsed.install else None,
        role=parsed.role,
        rules=parsed.rules,
        reflection=parsed.reflection,
        tags=fm.tags or None,
    )


# ---------------------------------------------------------------------------
# YAML config migration (no YAML rewrite — extract only)
# ---------------------------------------------------------------------------


def migrate_yaml_config_profiles(
    config_profiles: list,
    vault_root: str,
    *,
    registry: McpRegistry | None = None,
) -> InlineMigrationReport:
    """Extract inline ``mcp_servers`` from YAML AgentProfileConfig entries.

    YAML profiles are global (system scope), so extracted entries land in
    ``vault/mcp-servers/<name>.md``.  The YAML file itself is *not*
    rewritten — operators are notified via log so they can clean it up
    by hand.  The next time the YAML is loaded, the dict shape is
    coerced to a list of names by the seeding code so no data is lost
    at runtime.
    """
    report = InlineMigrationReport()
    data_dir = os.path.dirname(vault_root.rstrip(os.sep))

    for pc in config_profiles or []:
        report.profiles_examined += 1
        mcp = getattr(pc, "mcp_servers", None)
        if not isinstance(mcp, dict) or not mcp:
            continue

        extracted_names: list[str] = []
        for name, inline in mcp.items():
            sconfig = _inline_to_server_config(name, inline, project_id=None)
            if sconfig is None:
                report.errors.append(f"YAML profile '{pc.id}': cannot extract '{name}'")
                continue
            try:
                target, action = _write_registry_file(data_dir, sconfig)
            except OSError as exc:
                report.errors.append(f"YAML profile '{pc.id}': write failed for {name}: {exc}")
                continue
            report.extracted.append(
                ExtractedEntry(
                    name=name,
                    project_id=None,
                    target_path=target,
                    action=action,
                )
            )
            extracted_names.append(name)
            if registry is not None:
                try:
                    registry.upsert(sconfig)
                except ValueError:
                    pass

        if extracted_names:
            report.profiles_migrated += 1
            report.migrated_profiles.append(
                MigratedProfile(
                    profile_path=f"<yaml:{pc.id}>",
                    profile_id=pc.id,
                    extracted_names=extracted_names,
                )
            )
            logger.warning(
                "YAML profile '%s' has inline mcp_servers — extracted to "
                "vault/mcp-servers/{%s}.md.  Update your config.yaml to "
                "reference these by name once you've verified the registry "
                "files.",
                pc.id,
                ",".join(extracted_names),
            )

    return report
