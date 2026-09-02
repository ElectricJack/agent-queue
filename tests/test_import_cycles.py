"""Import-order regression tests.

``src.commands`` composes ``CommandHandler`` from every mixin, and
``session_commands`` needs constants that live in
``src.sessions.reconciler``.  So the moment anything under
``src.sessions`` imports from ``src.commands`` at module scope, the two
packages form a cycle whose failure depends on which one is imported
first: ``Orchestrator.__init__`` reaches the reconciler first and blows
up with ``cannot import name 'DRAIN_ACK_KEY' from partially initialized
module``, while a test that happened to import ``src.commands`` first
passes.  These tests pin both orders.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_ORDERS = [
    # The order Orchestrator.__init__ produces — the one that used to fail.
    ("src.sessions.reconciler", "src.commands"),
    ("src.commands", "src.sessions.reconciler"),
]


@pytest.mark.parametrize(("first", "second"), _ORDERS, ids=lambda m: m.rsplit(".", 1)[-1])
def test_reconciler_and_commands_import_in_either_order(first, second):
    """Neither package may depend on the other having been imported first."""
    code = f"import {first}; import {second}"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=_REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr


def test_claim_file_helpers_stay_importable_from_claim_commands():
    """The helpers moved to ``src.claim_file``; the old path is a re-export."""
    from src.claim_file import CLAIM_FILE as leaf_const
    from src.claim_file import write_claim_file as leaf_write
    from src.commands.claim_commands import CLAIM_FILE, write_claim_file

    assert CLAIM_FILE is leaf_const
    assert write_claim_file is leaf_write


def test_claim_file_module_has_no_project_imports():
    """``src.claim_file`` is a leaf on purpose — stdlib only."""
    import ast

    import src.claim_file

    source = pathlib.Path(src.claim_file.__file__).read_text()
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not [m for m in modules if m.split(".")[0] == "src"], sorted(modules)
