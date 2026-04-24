"""Compile-time rewriter for ``aq://`` resource URIs.

Playbook authors reference portable resources with URIs like
``aq://prompts/consolidation_task.md`` or
``aq://vault/projects/<id>/memory/consolidation.md``.  The playbook compiler
calls :func:`rewrite_aq_uris` on the markdown body before handing it to the
compiling LLM — by the time any tool runs, every ``aq://`` URI has been
replaced with an absolute filesystem path.  Runtime placeholders like
``<project_id>`` inside the path portion pass through untouched.

Authorities (read-only, all config-rooted):

    aq://prompts/<path>       -> src/prompts/<path> (bundled)
    aq://vault/<path>         -> {config.vault_root}/<path>
    aq://logs/<path>          -> {config.data_dir}/logs/<path>
    aq://tasks/<path>         -> {config.data_dir}/tasks/<path>
    aq://attachments/<path>   -> {config.data_dir}/attachments/<path>

Workspace-scoped authorities are intentionally absent — they would require
DB lookups, which defeats the purpose of compile-time rewriting.  Add a
dedicated runtime resolver tool if a use case emerges.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

AQ_SCHEME = "aq://"

# Matches aq://<authority>/<path>, where <path> is a run of non-whitespace,
# non-quote, non-backtick characters.  Angle brackets ARE allowed so runtime
# placeholders like ``<project_id>`` survive the rewrite unchanged.
_AQ_URI_RE = re.compile(r"aq://([a-zA-Z][a-zA-Z0-9_-]*)/([^\s\"'`]+)")


class AqUriError(ValueError):
    """Raised when an ``aq://`` URI cannot be rewritten."""


class _ConfigLike(Protocol):
    data_dir: str
    vault_root: str


def is_aq_uri(value: str | None) -> bool:
    """Return True if *value* is an ``aq://`` URI string."""
    return isinstance(value, str) and value.startswith(AQ_SCHEME)


def _prompts_root() -> Path:
    """Root of bundled prompts, portable across installs.

    Matches ``src/prompt_builder.py``'s ``_DEFAULT_PROMPTS_DIR``.
    """
    return Path(__file__).parent / "prompts"


def allowed_roots(config: _ConfigLike) -> list[Path]:
    """Return the absolute directory roots under which ``aq://`` paths resolve.

    Callers that accept a resolved ``aq://`` path at runtime (e.g. MCP-exposed
    prompt commands) can use this to validate the path was produced by the
    compile-time rewrite, not supplied directly by an untrusted client.
    """
    return [
        _prompts_root().resolve(),
        Path(config.vault_root).resolve(),
        (Path(config.data_dir) / "logs").resolve(),
        (Path(config.data_dir) / "tasks").resolve(),
        (Path(config.data_dir) / "attachments").resolve(),
    ]


def _resolve_authority(authority: str, subpath: str, *, config: _ConfigLike, full_uri: str) -> str:
    """Return absolute path for ``aq://<authority>/<subpath>``."""
    if ".." in subpath.replace("\\", "/").split("/"):
        raise AqUriError(f"aq:// URI rejects '..' path segments: {full_uri!r}")
    if authority == "prompts":
        return str(_prompts_root() / subpath)
    if authority == "vault":
        return str(Path(config.vault_root) / subpath)
    if authority == "logs":
        return str(Path(config.data_dir) / "logs" / subpath)
    if authority == "tasks":
        return str(Path(config.data_dir) / "tasks" / subpath)
    if authority == "attachments":
        return str(Path(config.data_dir) / "attachments" / subpath)
    raise AqUriError(
        f"Unknown aq:// authority {authority!r} in {full_uri!r}. "
        "Known: prompts, vault, logs, tasks, attachments"
    )


def rewrite_aq_uris(text: str, *, config: _ConfigLike) -> str:
    """Replace every ``aq://<authority>/<path>`` in *text* with an absolute path.

    Raises ``AqUriError`` on unknown authority or ``..`` traversal.
    """
    def _sub(match: re.Match[str]) -> str:
        authority = match.group(1)
        subpath = match.group(2)
        return _resolve_authority(
            authority, subpath, config=config, full_uri=match.group(0)
        )
    return _AQ_URI_RE.sub(_sub, text)
