"""In-memory harness registry, projected from the vault.

Mirrors :mod:`src.profiles.mcp_registry`: one markdown file per harness,
project scope shadows system scope, the vault watcher keeps the in-memory
store current, and there is **no database table** — the file is the source
of truth (principle #1).

- ``vault/harnesses/<name>.md`` — system scope
- ``vault/projects/<pid>/harnesses/<name>.md`` — project scope (shadows)

A parse failure keeps the previous entry rather than dropping it: a
half-saved file in an editor must not take the harness offline mid-run.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from src.sessions.harness_parser import Harness, parse_harness_markdown, resolve_base

if TYPE_CHECKING:  # pragma: no cover
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

__all__ = [
    "HARNESS_PATTERNS",
    "HarnessRegistry",
    "derive_harness_id",
    "load_from_vault",
    "register_harness_handlers",
    "vault_path_for",
]

#: Glob patterns handed to the vault watcher.
HARNESS_PATTERNS: list[str] = [
    "harnesses/*.md",
    "projects/*/harnesses/*.md",
]

OnReloadHook = Callable[[list[tuple[str | None, str]]], Awaitable[None]]


def derive_harness_id(rel_path: str) -> tuple[str | None, str] | None:
    """``(project_id, name)`` from a vault-relative path, or None."""
    parts = rel_path.replace("\\", "/").split("/")
    if (
        len(parts) == 4
        and parts[0] == "projects"
        and parts[2] == "harnesses"
        and parts[-1].endswith(".md")
    ):
        return (parts[1], parts[-1][:-3])
    if len(parts) == 2 and parts[0] == "harnesses" and parts[-1].endswith(".md"):
        return (None, parts[-1][:-3])
    return None


def vault_path_for(data_dir: str, name: str, project_id: str | None) -> str:
    base = os.path.join(data_dir, "vault")
    if project_id:
        return os.path.join(base, "projects", project_id, "harnesses", f"{name}.md")
    return os.path.join(base, "harnesses", f"{name}.md")


class HarnessRegistry:
    """Project-first, system-fallback lookup of :class:`Harness` entries."""

    def __init__(self) -> None:
        self._harnesses: dict[tuple[str | None, str], Harness] = {}

    # -- mutation ----------------------------------------------------------

    def upsert(self, harness: Harness) -> None:
        self._harnesses[harness.scope_key] = harness

    def remove(self, name: str, project_id: str | None = None) -> bool:
        return self._harnesses.pop((project_id, name), None) is not None

    def clear(self) -> None:
        self._harnesses.clear()

    # -- lookup ------------------------------------------------------------

    def get(self, name: str, project_id: str | None = None) -> Harness | None:
        """Resolve *name*, preferring the project-scoped file.

        ``base:`` inheritance is folded in at lookup time against the same
        scope, so a project harness can derive from a system one.
        """
        harness = None
        if project_id is not None:
            harness = self._harnesses.get((project_id, name))
        if harness is None:
            harness = self._harnesses.get((None, name))
        if harness is None:
            return None
        if not harness.base:
            return harness

        def _lookup(base_name: str) -> Harness | None:
            if project_id is not None:
                scoped = self._harnesses.get((project_id, base_name))
                if scoped is not None:
                    return scoped
            return self._harnesses.get((None, base_name))

        resolved, errors = resolve_base(harness, _lookup)
        if errors:
            logger.warning("Harness registry: %s", "; ".join(errors))
        return resolved

    def list_for_scope(self, project_id: str | None = None) -> list[Harness]:
        if project_id is None:
            return sorted(
                (h for (pid, _), h in self._harnesses.items() if pid is None),
                key=lambda h: h.id,
            )
        project_names = {n for (pid, n) in self._harnesses if pid == project_id}
        out: list[Harness] = []
        for (pid, name), harness in self._harnesses.items():
            if pid == project_id or (pid is None and name not in project_names):
                out.append(harness)
        return sorted(out, key=lambda h: h.id)

    def list_all(self) -> list[Harness]:
        return sorted(self._harnesses.values(), key=lambda h: ((h.project_id or ""), h.id))

    def __len__(self) -> int:
        return len(self._harnesses)

    def __contains__(self, key: tuple[str | None, str]) -> bool:
        return key in self._harnesses


# ---------------------------------------------------------------------------
# Vault scan + watcher integration
# ---------------------------------------------------------------------------


def _iter_harness_files(vault_root: str):
    """Yield ``(abs_path, rel_path)`` for every harness file in the vault."""
    if not os.path.isdir(vault_root):
        return

    sys_dir = os.path.join(vault_root, "harnesses")
    if os.path.isdir(sys_dir):
        for fname in sorted(os.listdir(sys_dir)):
            if fname.startswith(".") or not fname.endswith(".md"):
                continue
            abs_path = os.path.join(sys_dir, fname)
            if os.path.isfile(abs_path):
                yield abs_path, f"harnesses/{fname}"

    projects_dir = os.path.join(vault_root, "projects")
    if not os.path.isdir(projects_dir):
        return
    for project_name in sorted(os.listdir(projects_dir)):
        if project_name.startswith("."):
            continue
        pdir = os.path.join(projects_dir, project_name, "harnesses")
        if not os.path.isdir(pdir):
            continue
        for fname in sorted(os.listdir(pdir)):
            if fname.startswith(".") or not fname.endswith(".md"):
                continue
            abs_path = os.path.join(pdir, fname)
            if os.path.isfile(abs_path):
                yield abs_path, f"projects/{project_name}/harnesses/{fname}"


def load_from_vault(registry: HarnessRegistry, vault_root: str) -> list[str]:
    """Populate *registry* from every harness file under *vault_root*.

    Full reload: existing entries are dropped first.  Returns one error
    string per malformed file (the file is skipped, the load continues).
    """
    errors: list[str] = []
    registry.clear()

    for abs_path, rel_path in _iter_harness_files(vault_root):
        derived = derive_harness_id(rel_path)
        if derived is None:
            continue
        project_id, fallback = derived
        try:
            text = Path(abs_path).read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{rel_path}: read failed: {exc}")
            continue
        parsed = parse_harness_markdown(text, fallback_id=fallback, project_id=project_id)
        if not parsed.is_valid or parsed.harness is None:
            errors.append(f"{rel_path}: {'; '.join(parsed.errors)}")
            logger.warning("Harness registry: skipping %s: %s", rel_path, parsed.errors)
            continue
        for warning in parsed.warnings:
            # Warnings are load-bearing: a dialog rule that can never
            # match leaves a session stuck on its trust screen.
            logger.warning("Harness registry: %s: %s", rel_path, warning)
        registry.upsert(parsed.harness)

    logger.info("Harness registry loaded: %d entries (%d errors)", len(registry), len(errors))
    return errors


async def _on_harness_changed(
    changes: list[VaultChange],
    *,
    registry: HarnessRegistry,
    on_reload: OnReloadHook | None = None,
) -> None:
    """Watcher callback — reparse changed files, update the registry."""
    touched: list[tuple[str | None, str]] = []

    for change in changes:
        derived = derive_harness_id(change.rel_path)
        if derived is None:
            continue
        project_id, name = derived

        if change.operation == "deleted":
            if registry.remove(name, project_id):
                logger.info(
                    "Harness registry: removed %s (scope=%s)", name, project_id or "system"
                )
                touched.append((project_id, name))
            continue

        try:
            text = Path(change.path).read_text(encoding="utf-8")
        except OSError:
            logger.error("Harness registry: cannot read %s", change.path, exc_info=True)
            continue

        parsed = parse_harness_markdown(text, fallback_id=name, project_id=project_id)
        if not parsed.is_valid or parsed.harness is None:
            # Keep the previous entry: a file being edited must not take a
            # running harness offline halfway through a save.
            logger.warning(
                "Harness registry: %s parse failed: %s — keeping previous entry",
                change.rel_path,
                parsed.errors,
            )
            continue
        registry.upsert(parsed.harness)
        touched.append((project_id, name))
        logger.info(
            "Harness registry: %s %s (scope=%s)",
            change.operation,
            name,
            project_id or "system",
        )

    if touched and on_reload is not None:
        try:
            await on_reload(touched)
        except Exception:
            logger.exception("Harness registry on_reload hook raised")


def register_harness_handlers(
    watcher: VaultWatcher,
    registry: HarnessRegistry,
    *,
    on_reload: OnReloadHook | None = None,
) -> list[str]:
    """Register vault-watcher handlers for both harness scopes."""

    async def _handler(changes: list[VaultChange]) -> None:
        await _on_harness_changed(changes, registry=registry, on_reload=on_reload)

    handler_ids: list[str] = []
    for pattern in HARNESS_PATTERNS:
        handler_ids.append(
            watcher.register_handler(pattern, _handler, handler_id=f"harness:{pattern}")
        )
    logger.info("Harness registry: registered %d handler(s)", len(handler_ids))
    return handler_ids
