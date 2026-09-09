"""Packaging ratchet: every declared console script must exist in this tree.

``pyproject.toml`` once declared ``agent-queue-mcp =
"packages.mcp_server.mcp_server:main"`` long after that package was folded into
the daemon as ``src/embedded_mcp.py``.  Nothing caught it, because an entry
point is only resolved when the installed script is run -- so ``pip install``
happily created a binary that died at import.  These tests resolve every
declared target statically, from the checkout alone.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _module_file(module: str) -> Path | None:
    """Resolve a dotted module name to a file in the checkout, or None."""
    base = ROOT / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _defines(path: Path, name: str) -> bool:
    """Whether ``path`` defines ``name`` at module level (no import needed)."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return True
    return False


@pytest.mark.parametrize("script", sorted(_pyproject()["project"].get("scripts", {})))
def test_console_script_target_exists(script: str):
    target = _pyproject()["project"]["scripts"][script]
    module, _, attr = target.partition(":")
    path = _module_file(module)
    assert path is not None, (
        f"console script {script!r} points at {target!r}, but module {module!r} "
        f"does not exist in the tree"
    )
    assert _defines(path, attr), (
        f"console script {script!r} points at {target!r}, but "
        f"{path.relative_to(ROOT)} defines no top-level {attr!r}"
    )


def test_no_standalone_mcp_console_script():
    """The MCP server is embedded in the daemon; it has no script of its own."""
    scripts = _pyproject()["project"].get("scripts", {})
    assert "agent-queue-mcp" not in scripts, (
        "agent-queue-mcp was retired when the MCP server moved into "
        "src/embedded_mcp.py -- the daemon serves it, so there is nothing for a "
        "standalone script to start"
    )


@pytest.mark.parametrize("testpath", _pyproject()["tool"]["pytest"]["ini_options"]["testpaths"])
def test_testpaths_are_directories_that_hold_tests(testpath: str):
    """A testpath with no tests is only a filesystem walk on every collection."""
    directory = ROOT / testpath
    assert directory.is_dir(), f"testpaths entry {testpath!r} is not a directory"
    assert any(directory.rglob("test_*.py")), (
        f"testpaths entry {testpath!r} contains no test modules"
    )
