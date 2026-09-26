"""Where pytest looks for test modules, answered statically.

Moved from ``src/cli/test_runner.py``, which still imports both helpers under
their old underscore names: ``aq test`` classifies a full-suite run with them,
and the selection catalogue enumerates its universe with them, so the two can
never disagree about which modules exist.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

#: Directories never worth walking for test modules.
SKIP_DIRS = frozenset({"__pycache__", "node_modules"})


def pytest_rootdir(cwd: Path) -> tuple[Path, list[Path]]:
    """``(rootdir, testpaths)`` from the nearest ``pyproject.toml`` configuring pytest.

    ``(cwd, [])`` when no such file is found above *cwd*.
    """
    for directory in (cwd, *cwd.parents):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        section = data.get("tool", {}).get("pytest")
        if not isinstance(section, dict):
            continue
        # ``[tool.pytest.ini_options]``, or pytest 9's native ``[tool.pytest]``.
        options = section.get("ini_options", section)
        testpaths = options.get("testpaths") if isinstance(options, dict) else None
        if isinstance(testpaths, str):
            testpaths = testpaths.split()
        return directory, [(directory / entry).resolve() for entry in testpaths or []]
    return cwd, []


def test_modules(roots: list[Path]) -> set[Path]:
    """Every ``test_*.py`` / ``*_test.py`` under *roots* (pytest's default ``python_files``)."""
    modules: set[Path] = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
            for name in filenames:
                if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                    modules.add(Path(dirpath, name))
    return modules


# The name is pytest-shaped: a test module that imports it must not collect it.
test_modules.__test__ = False  # type: ignore[attr-defined]


def relative_modules(rootdir: Path, roots: list[Path]) -> list[str]:
    """Sorted posix paths of every test module under *roots*, relative to *rootdir*."""
    root = rootdir.resolve()
    return sorted(m.resolve().relative_to(root).as_posix() for m in test_modules(roots))
