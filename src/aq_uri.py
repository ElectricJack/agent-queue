"""Resolver for ``aq://`` resource URIs.

Playbooks and tool calls reference portable resources with URIs like
``aq://prompts/consolidation_task.md`` or
``aq://vault/projects/<id>/memory/consolidation.md``.  The daemon resolves
these to concrete filesystem paths using its config and database — callers
never hardcode machine-specific absolute paths.

Authorities (v1, read-only):

    aq://prompts/<path>                 -> src/prompts/<path> (bundled)
    aq://vault/<path>                   -> {vault_root}/<path>
    aq://logs/<path>                    -> {data_dir}/logs/<path>
    aq://tasks/<path>                   -> {data_dir}/tasks/<path>
    aq://attachments/<path>             -> {data_dir}/attachments/<path>
    aq://workspace/<project_id>/<path>  -> primary workspace for project
    aq://workspace-id/<ws_id>/<path>    -> specific workspace by DB id
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


AQ_SCHEME = "aq://"

# Authorities that take a subpath under a fixed root (no id segment).
_SINGLETON_AUTHORITIES = frozenset(
    {"prompts", "vault", "logs", "tasks", "attachments"}
)

# Authorities whose first path segment is an id (project or workspace).
_ID_AUTHORITIES = frozenset({"workspace", "workspace-id"})


class AqUriError(ValueError):
    """Raised when an ``aq://`` URI cannot be resolved."""


class _ConfigLike(Protocol):
    data_dir: str
    vault_root: str


class _DbLike(Protocol):
    async def get_project_workspace_path(self, project_id: str) -> str | None: ...
    async def get_workspace(self, workspace_id: str): ...  # returns Workspace | None


def is_aq_uri(value: str | None) -> bool:
    """Return True if *value* is an ``aq://`` URI string."""
    return isinstance(value, str) and value.startswith(AQ_SCHEME)


def _prompts_root() -> Path:
    """Root of bundled prompts, portable across installs.

    Matches ``src/prompt_builder.py``'s ``_DEFAULT_PROMPTS_DIR``.
    """
    return Path(__file__).parent / "prompts"


def _split(uri: str) -> tuple[str, str]:
    """Split ``aq://<authority>/<rest>`` into ``(authority, rest)``.

    Raises ``AqUriError`` if the URI is malformed or has no path.
    """
    if not is_aq_uri(uri):
        raise AqUriError(f"Not an aq:// URI: {uri!r}")
    remainder = uri[len(AQ_SCHEME):]
    if "/" not in remainder:
        raise AqUriError(f"aq:// URI missing path: {uri!r}")
    authority, rest = remainder.split("/", 1)
    if not authority:
        raise AqUriError(f"aq:// URI missing authority: {uri!r}")
    if not rest:
        raise AqUriError(f"aq:// URI missing path after authority: {uri!r}")
    return authority, rest


def _reject_traversal(subpath: str, *, uri: str) -> None:
    """Reject path segments that would escape the authority's root."""
    # Normalize separators; aq:// always uses forward slashes on the wire.
    parts = [p for p in subpath.replace("\\", "/").split("/") if p]
    for part in parts:
        if part == "..":
            raise AqUriError(f"aq:// URI rejects '..' path segments: {uri!r}")
        # An absolute segment (empty was already stripped; leading slash case
        # is handled by _split, but a Windows-style drive letter would slip
        # through str.split — reject anything that os.path.isabs would treat
        # as absolute).
        if os.path.isabs(part):
            raise AqUriError(f"aq:// URI rejects absolute path segments: {uri!r}")


async def resolve(
    uri: str,
    *,
    config: _ConfigLike,
    db: _DbLike | None = None,
) -> Path:
    """Resolve an ``aq://`` URI to an absolute filesystem path.

    Parameters
    ----------
    uri : str
        The ``aq://<authority>/<path>`` URI.
    config : Config
        Daemon config; provides ``data_dir`` and ``vault_root``.
    db : DatabaseQueries, optional
        Required only for ``workspace`` and ``workspace-id`` authorities.

    Raises
    ------
    AqUriError
        If the URI is malformed, uses an unknown authority, contains ``..``,
        or references a missing project/workspace id.
    """
    authority, rest = _split(uri)

    # ID-bearing authorities: first segment of ``rest`` is the id.
    if authority in _ID_AUTHORITIES:
        if "/" not in rest:
            raise AqUriError(
                f"aq://{authority}/ requires <id>/<path>: {uri!r}"
            )
        ident, subpath = rest.split("/", 1)
        if not ident:
            raise AqUriError(f"aq://{authority}/ id segment is empty: {uri!r}")
        if not subpath:
            raise AqUriError(f"aq://{authority}/ path is empty: {uri!r}")
        _reject_traversal(subpath, uri=uri)

        if db is None:
            raise AqUriError(
                f"aq://{authority}/ requires a db handle but none was provided"
            )

        if authority == "workspace":
            ws_path = await db.get_project_workspace_path(ident)
            if not ws_path:
                raise AqUriError(
                    f"No workspace found for project {ident!r}: {uri!r}"
                )
            return Path(ws_path) / subpath

        # authority == "workspace-id"
        ws = await db.get_workspace(ident)
        if ws is None:
            raise AqUriError(f"Workspace {ident!r} not found: {uri!r}")
        return Path(ws.workspace_path) / subpath

    # Singleton authorities: the entire ``rest`` is the subpath.
    if authority not in _SINGLETON_AUTHORITIES:
        raise AqUriError(
            f"Unknown aq:// authority {authority!r}. Known: "
            f"{sorted(_SINGLETON_AUTHORITIES | _ID_AUTHORITIES)}"
        )

    _reject_traversal(rest, uri=uri)

    if authority == "prompts":
        return _prompts_root() / rest
    if authority == "vault":
        return Path(config.vault_root) / rest
    if authority == "logs":
        return Path(config.data_dir) / "logs" / rest
    if authority == "tasks":
        return Path(config.data_dir) / "tasks" / rest
    if authority == "attachments":
        return Path(config.data_dir) / "attachments" / rest

    # Unreachable: we already checked membership in _SINGLETON_AUTHORITIES.
    raise AqUriError(f"Unhandled aq:// authority {authority!r}: {uri!r}")
