"""The modules that run whenever a package or a test directory is loaded.

Python runs a package's ``__init__.py``, and everything it imports, before
any submodule of the package; pytest runs every ``conftest.py`` above a test
module, and everything it imports, before the module. pytest-impacted records
neither edge (the fixture evaluation in ``tests/test_selection_static_impact.py``),
so a change to a module these files load can reach tests the engine does not
name. :func:`load_closure` finds those modules: it starts at every
``__init__.py`` and ``conftest.py`` under the given directories and follows
every import, a function-local one included, to a module file under them.

:mod:`src.test_selection.static_impact` runs this file as a script, ``python
-I load_closure.py <workspace> <directory>... [--skip <path>]...``, which
prints the closure as a JSON list of workspace-relative posix paths. It
imports only the standard library, so the daemon runs its own copy of this
file against the workspace and never the workspace's code; a subprocess also
keeps a few seconds of parsing off the daemon's event loop.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

#: Files Python or pytest runs before a module that neither imports.
LOAD_ROOTS = ("__init__.py", "conftest.py")


def load_closure(workspace: Path, directories: Sequence[str], skip: Iterable[str] = ()) -> set[str]:
    """Every module file a load root under *directories* runs, the roots included.

    *skip* names roots to leave out: the caller skips a root whose whole
    directory is new, since every module there is itself a change.
    """
    files = _module_files(workspace, directories)
    bases = {PurePosixPath()} | {PurePosixPath(d).parent for d in directories}
    by_name: dict[str, str] = {}
    for rel in files:
        for name in _names(rel, bases):
            by_name.setdefault(name, rel)

    skipped = set(skip)
    pending = [rel for rel in files if PurePosixPath(rel).name in LOAD_ROOTS and rel not in skipped]
    closure = set(pending)
    while pending:
        rel = pending.pop()
        for target in _targets(workspace, rel, bases, by_name):
            if target not in closure:
                closure.add(target)
                pending.append(target)
    return closure


def _module_files(workspace: Path, directories: Sequence[str]) -> list[str]:
    files: set[str] = set()
    for directory in directories:
        for dirpath, dirnames, filenames in os.walk(workspace / directory):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            relative = PurePosixPath(os.path.relpath(dirpath, workspace))
            files.update((relative / f).as_posix() for f in filenames if f.endswith(".py"))
    return sorted(files)


def _names(rel: str, bases: set[PurePosixPath]) -> list[str]:
    """*rel*'s dotted names: from the workspace root, and from each directory's parent.

    ``src/pkg/a.py`` is ``src.pkg.a``; under a ``src/mypkg`` directory it is
    also ``mypkg.a``, the name a src-layout project imports it by.
    """
    path = PurePosixPath(rel)
    parts = path.with_suffix("").parts
    if path.name == "__init__.py":
        parts = parts[:-1]
    names = []
    for base in bases:
        if base == PurePosixPath() or PurePosixPath(*parts).is_relative_to(base):
            dotted = parts[len(base.parts) :]
            if dotted:
                names.append(".".join(dotted))
    return names


def _targets(
    workspace: Path, rel: str, bases: set[PurePosixPath], by_name: dict[str, str]
) -> set[str]:
    """The module files *rel* imports, and the packages each import runs on the way."""
    try:
        tree = ast.parse((workspace / rel).read_bytes())
    except (OSError, SyntaxError, ValueError, RecursionError):
        return set()  # as the engine does: an unreadable module adds no edge
    is_package = PurePosixPath(rel).name == "__init__.py"
    targets: set[str] = set()
    for name in _names(rel, bases):
        package = name if is_package else name.rpartition(".")[0]
        for imported in _imported_names(tree, package):
            parts = imported.split(".")
            for end in range(1, len(parts) + 1):
                target = by_name.get(".".join(parts[:end]))
                if target is not None:
                    targets.add(target)
    return targets


def _imported_names(tree: ast.AST, package: str) -> set[str]:
    """Every dotted name *tree* imports; ``from p import n`` yields ``p`` and ``p.n``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(package, node.level, node.module) if node.level else node.module or ""
            if base:
                found.add(base)
            found.update(
                f"{base}.{alias.name}" if base else alias.name
                for alias in node.names
                if alias.name != "*"
            )
    return found


def _absolute(package: str, level: int, module: str | None) -> str:
    parts = package.split(".") if package else []
    if level > 1:
        parts = parts[: len(parts) - (level - 1)] if len(parts) >= level - 1 else []
    if module:
        parts.append(module)
    return ".".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace")
    parser.add_argument("directories", nargs="+")
    parser.add_argument("--skip", action="append", default=[])
    args = parser.parse_args(argv)
    closure = load_closure(Path(args.workspace), args.directories, args.skip)
    json.dump(sorted(closure), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
