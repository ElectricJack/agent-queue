"""Live, vault-backed registry of intelligence classes.

Mirrors :mod:`src.task_graph.formulas` and
:mod:`src.sessions.harness_registry`: one markdown file per class under
``vault/intelligence-classes/``, the shared vault watcher keeps the
in-memory store current, and there is **no database table** — the file is
the source of truth (principle #1).

The registry is a live :class:`~collections.abc.Mapping`, so every consumer
that holds a reference (``SessionSpecBuilder._intelligence_classes``, the
scheduler's routing snapshot, pool launch, agent create/edit) reads the
current classes without a daemon restart.

A parse failure keeps the previous entry rather than dropping it: a
half-saved file in an editor must not take a class — and every profile
that names it — offline mid-run.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING

from src.intelligence_classes import (
    IntelligenceClass,
    _parse_class_file,
    _upgrade_legacy_provider_defaults,
)

if TYPE_CHECKING:  # pragma: no cover
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

__all__ = [
    "INTELLIGENCE_CLASS_PATTERNS",
    "IntelligenceClassRegistry",
    "classes_dir",
    "derive_class_id",
    "register_intelligence_class_handlers",
]

#: Glob patterns handed to the vault watcher.  Intelligence classes are a
#: global namespace — there is no per-project scope.
INTELLIGENCE_CLASS_PATTERNS: list[str] = ["intelligence-classes/*.md"]


def classes_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "vault", "intelligence-classes")


def derive_class_id(rel_path: str) -> str | None:
    """Fallback class id from a vault-relative path, or ``None``."""
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) == 2 and parts[0] == "intelligence-classes" and parts[1].endswith(".md"):
        stem = parts[1][:-3]
        return stem or None
    return None


class IntelligenceClassRegistry(Mapping):
    """``{class_id: IntelligenceClass}`` that stays current with the vault.

    Implements ``Mapping`` so it is a drop-in replacement for the plain
    dict every consumer used to receive: ``dict(registry)``,
    ``registry.get(id)``, ``id in registry`` and ``sorted(registry)`` all
    behave the same, but read live state.
    """

    def __init__(self, classes: Mapping[str, IntelligenceClass] | None = None) -> None:
        self._classes: dict[str, IntelligenceClass] = dict(classes or {})
        #: ``{rel_path: class_id}`` — which file last produced which class, so
        #: a delete or a malformed save can be resolved back to its entry even
        #: when the declared ``id`` differs from the file name.
        self._by_path: dict[str, str] = {
            f"intelligence-classes/{cid}.md": cid for cid in self._classes
        }
        #: ``{rel_path: message}`` for files that failed to parse.  Surfaced
        #: by ``aq doctor --check intelligence_classes.parse``.
        self.errors: dict[str, str] = {}

    # -- Mapping -----------------------------------------------------------

    def __getitem__(self, key: str) -> IntelligenceClass:
        return self._classes[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._classes)

    def __len__(self) -> int:
        return len(self._classes)

    def snapshot(self) -> dict[str, IntelligenceClass]:
        """A detached copy of the current classes."""
        return dict(self._classes)

    # -- mutation ----------------------------------------------------------

    def replace(self, classes: Mapping[str, IntelligenceClass]) -> None:
        """Publish *classes* wholesale (used after an editor save)."""
        self._classes = dict(classes)
        self._by_path = {f"intelligence-classes/{cid}.md": cid for cid in self._classes}

    def reload(self, data_dir: str) -> list[str]:
        """Rescan the vault directory.  Returns one message per bad file.

        Classes whose file no longer parses keep their previous entry, so a
        malformed save never empties the registry.  Deleted files *do* drop
        their class — an absent file is an intentional removal.
        """
        root = classes_dir(data_dir)
        loaded: dict[str, IntelligenceClass] = {}
        by_path: dict[str, str] = {}
        errors: dict[str, str] = {}
        names: list[str] = []
        if os.path.isdir(root):
            names = sorted(n for n in os.listdir(root) if n.endswith(".md"))
        for name in names:
            path = os.path.join(root, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel_path = f"intelligence-classes/{name}"
            cls, error = _parse_class_file(path)
            if cls is None:
                errors[rel_path] = f"{rel_path}: {error}"
                previous = self._classes.get(self._by_path.get(rel_path, name[:-3]))
                if previous is not None:
                    loaded[previous.id] = previous
                    by_path[rel_path] = previous.id
                    logger.warning(
                        "Intelligence-class registry: %s failed to parse (%s) — "
                        "keeping previous entry",
                        rel_path,
                        error,
                    )
                else:
                    logger.warning(
                        "Intelligence-class registry: skipping %s: %s", rel_path, error
                    )
                continue
            loaded[cls.id] = _upgrade_legacy_provider_defaults(cls)
            by_path[rel_path] = cls.id
        self._classes = loaded
        self._by_path = by_path
        self.errors = errors
        logger.info(
            "Intelligence-class registry loaded: %d entries (%d errors)",
            len(loaded),
            len(errors),
        )
        return sorted(errors.values())


async def _on_intelligence_class_changed(
    changes: list[VaultChange],
    *,
    registry: IntelligenceClassRegistry,
) -> None:
    """Watcher callback — reparse changed files, update the registry."""
    for change in changes:
        fallback = derive_class_id(change.rel_path)
        if fallback is None:
            continue

        if change.operation == "deleted":
            registry.errors.pop(change.rel_path, None)
            class_id = registry._by_path.pop(change.rel_path, fallback)
            if registry._classes.pop(class_id, None) is not None:
                logger.info("Intelligence-class registry: removed %s", class_id)
            continue

        cls, error = _parse_class_file(change.path)
        if cls is None:
            # Keep the previous entry: a file being edited must not take a
            # class offline halfway through a save.
            registry.errors[change.rel_path] = f"{change.rel_path}: {error}"
            logger.warning(
                "Intelligence-class registry: %s parse failed: %s — keeping previous entry",
                change.rel_path,
                error,
            )
            continue

        registry.errors.pop(change.rel_path, None)
        # A file that renamed its declared id drops the entry it used to own.
        previous_id = registry._by_path.get(change.rel_path)
        if previous_id is not None and previous_id != cls.id:
            registry._classes.pop(previous_id, None)
        registry._by_path[change.rel_path] = cls.id
        registry._classes[cls.id] = _upgrade_legacy_provider_defaults(cls)
        logger.info(
            "Intelligence-class registry: %s %s", change.operation, cls.id
        )


def register_intelligence_class_handlers(
    watcher: VaultWatcher,
    registry: IntelligenceClassRegistry,
) -> list[str]:
    """Register vault-watcher handlers for ``intelligence-classes/*.md``."""

    async def _handler(changes: list[VaultChange]) -> None:
        await _on_intelligence_class_changed(changes, registry=registry)

    handler_ids = [
        watcher.register_handler(pattern, _handler, handler_id=f"intelligence-class:{pattern}")
        for pattern in INTELLIGENCE_CLASS_PATTERNS
    ]
    logger.info("Intelligence-class registry: registered %d handler(s)", len(handler_ids))
    return handler_ids
