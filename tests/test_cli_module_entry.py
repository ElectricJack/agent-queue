"""``python -m src.cli.app`` must expose the same commands as the ``aq`` script.

Running the CLI as a module imports ``src/cli/app.py`` twice (as ``__main__``
and as ``src.cli.app``); the hand-crafted commands register on the latter.
Without the delegation in ``app.py``'s ``__main__`` block they vanish from
``python -m src.cli.app --help`` — which is how a pool worker's bootstrap
prompt ended up telling an agent to run an ``aq inbox`` that "did not exist".
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("command", ["inbox", "reply", "message", "schema", "prime", "handoff"])
def test_module_entry_exposes_hand_crafted_commands(command):
    proc = subprocess.run(
        [sys.executable, "-m", "src.cli.app", command, "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "No such command" not in proc.stderr
