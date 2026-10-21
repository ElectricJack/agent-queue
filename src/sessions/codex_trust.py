"""Pre-trust a Codex session's project before the TUI asks.

Codex asks "Trust this folder?" the first time it starts in a project it has
no ``[projects."<root>"]`` entry for, and stays on that screen until someone
answers.  Its own answer is a ``trust_level = "trusted"`` row in
``$CODEX_HOME/config.toml`` keyed by the *repository root* — for a linked
worktree, the main checkout's root (verified on codex-cli 0.162.1: a session
in ``<repo>/.aq/worktrees/slot-1`` offers to trust ``<repo>``).

On 2026-10-21 both Codex pool sessions of a newly registered project sat on
that screen for 40+ minutes: Codex 0.162 had renamed the prompt, the vault
dialog rule no longer matched, and the menu's ``› 1. Trust and continue`` row
passed for the ready composer.  Writing the row ourselves before launch makes
the screen never appear, whatever the harness file's dialog rules say.

The builder only asks for this when the launch already qualifies for the
isolated-worktree trust argument (:func:`~src.sessions.spec.skip_permissions_allowed`),
so a session in a linked checkout still gets Codex's own question.  An entry
the operator already wrote — including ``trust_level = "untrusted"`` — is
never changed, and a config this module cannot parse is left alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import tomllib
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["codex_config_path", "ensure_project_trusted", "pretrust_codex_project", "trust_root"]

#: Serialises read-modify-write of the config within the daemon, where two
#: pool sessions of one project routinely start in the same cycle.
_LOCK = threading.Lock()


def trust_root(work_dir: str | os.PathLike[str]) -> Path:
    """The directory Codex keys its trust decision on for *work_dir*.

    The nearest enclosing git checkout; for a linked worktree, the root of
    the checkout that owns its common git directory.  Outside git, the
    directory itself.
    """
    path = Path(work_dir).resolve()
    for candidate in (path, *path.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return candidate
        if dot_git.is_file():
            return _main_checkout_root(dot_git) or candidate
    return path


def _main_checkout_root(dot_git: Path) -> Path | None:
    try:
        first = dot_git.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    git_dir = Path(first.removeprefix("gitdir:").strip())
    if not git_dir.is_absolute():
        git_dir = dot_git.parent / git_dir
    try:
        common = (git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip())
    except OSError:
        return None
    common = common.resolve()
    # A bare common directory has no checkout to name; keep the worktree.
    return common.parent if common.name == ".git" else None


def codex_config_path(env: Mapping[str, str]) -> Path:
    """``config.toml`` under the ``CODEX_HOME`` the session will see."""
    codex_home = env.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "config.toml"
    home = env.get("HOME")
    return (Path(home) if home else Path.home()) / ".codex" / "config.toml"


def ensure_project_trusted(work_dir: str | os.PathLike[str], config_path: Path) -> str:
    """Record *work_dir*'s trust root as trusted in *config_path*.

    Returns ``"added"``, ``"present"`` (an entry already names a trust level),
    or ``"skipped"`` when the file cannot be parsed or the new row would not
    round-trip — the file is then left byte-for-byte as it was.
    """
    root = str(trust_root(work_dir))
    with _LOCK:
        try:
            text = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        try:
            current = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            logger.warning("Codex config %s does not parse (%s); not pre-trusting %s",
                           config_path, exc, root)
            return "skipped"
        projects = current.get("projects")
        entry = projects.get(root) if isinstance(projects, dict) else None
        if isinstance(entry, dict) and "trust_level" in entry:
            return "present"

        # json.dumps without ASCII escaping is a valid TOML basic string.
        header = f"[projects.{json.dumps(root, ensure_ascii=False)}]"
        separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        updated = f"{text}{separator}{header}\ntrust_level = \"trusted\"\n"
        try:
            check = tomllib.loads(updated)["projects"][root]["trust_level"]
        except (tomllib.TOMLDecodeError, KeyError, TypeError):
            check = None
        if check != "trusted":
            # e.g. an inline ``projects = {...}`` table or a header for this
            # root without a trust level: appending would corrupt the file.
            logger.warning("Codex config %s cannot take a trust row for %s; left unchanged",
                           config_path, root)
            return "skipped"
        _atomic_write(config_path, updated)
    logger.info("Pre-trusted %s for Codex in %s", root, config_path)
    return "added"


def _atomic_write(path: Path, text: str) -> None:
    target = path.resolve() if path.exists() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix=".config.toml.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def pretrust_codex_project(work_dir: str, env: Mapping[str, str]) -> str:
    """Launch-path wrapper: never raises; the dialog rules remain the fallback."""
    config_path = codex_config_path(env)
    try:
        return await asyncio.to_thread(ensure_project_trusted, work_dir, config_path)
    except OSError as exc:
        logger.warning("Could not pre-trust %s for Codex in %s: %s", work_dir, config_path, exc)
        return "skipped"
