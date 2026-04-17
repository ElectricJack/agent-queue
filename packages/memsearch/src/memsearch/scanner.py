"""Multi-path file scanner for markdown knowledge bases."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScannedFile:
    """Metadata for a discovered markdown file."""

    path: Path
    mtime: float
    size: int


def scan_paths(
    paths: list[str | Path],
    *,
    extensions: tuple[str, ...] = (".md", ".markdown"),
    ignore_hidden: bool = True,
) -> list[ScannedFile]:
    """Recursively scan *paths* for markdown files.

    Each entry in *paths* may be a file or directory.  Directories are
    walked recursively.  Hidden files/dirs (starting with ``"."``) are
    skipped when *ignore_hidden* is ``True``.
    """
    results: list[ScannedFile] = []
    seen: set[str] = set()

    for p in paths:
        root = Path(p).expanduser().resolve()
        if root.is_file():
            _maybe_add(root, extensions, seen, results)
        elif root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                if ignore_hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fname in filenames:
                    if ignore_hidden and fname.startswith("."):
                        continue
                    fp = Path(dirpath) / fname
                    _maybe_add(fp, extensions, seen, results)

    results.sort(key=lambda f: f.path)
    return results


def _maybe_add(
    fp: Path,
    extensions: tuple[str, ...],
    seen: set[str],
    results: list[ScannedFile],
) -> None:
    if fp.suffix.lower() not in extensions:
        return
    real = str(fp.resolve())
    if real in seen:
        return
    seen.add(real)
    stat = fp.stat()
    results.append(ScannedFile(path=fp, mtime=stat.st_mtime, size=stat.st_size))
