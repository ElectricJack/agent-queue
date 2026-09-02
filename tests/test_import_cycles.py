"""Import-order regression guard.

``src.sessions.reconciler`` used to import ``src.commands.claim_commands``,
which pulls in ``src.commands.__init__`` → ``handler`` → ``session_commands``
→ back into the half-initialised reconciler, so ``DRAIN_ACK_KEY`` and
``LIVE_SESSION_STATES`` did not exist yet.  It only failed when the reconciler
happened to be imported *first* (importing ``Orchestrator`` is one such path),
which made it look like a flaky collection error rather than a cycle.

Each case runs in a fresh interpreter: an already-imported ``sys.modules``
hides the ordering entirely, so an in-process import proves nothing.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Modules that sit on both sides of the command-handler / session boundary.
#: Each must import cleanly as the *first* thing a process does.
ENTRY_MODULES = [
    "src.sessions.reconciler",
    "src.commands",
    "src.commands.claim_commands",
    "src.commands.session_commands",
    "src.orchestrator",
    "src.claim_file",
]


def _import_in_fresh_interpreter(statement: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    result = _import_in_fresh_interpreter(f"import {module}")
    assert result.returncode == 0, f"importing {module} first failed:\n{result.stderr}"


def test_reconciler_constants_survive_a_reconciler_first_import() -> None:
    """The exact symbols the cycle used to hide, read after a bare import."""
    result = _import_in_fresh_interpreter(
        "import src.sessions.reconciler as r; "
        "assert r.DRAIN_ACK_KEY == 'AQ_DRAIN_ACK'; "
        "assert r.LIVE_SESSION_STATES == ('starting', 'running', 'draining')"
    )
    assert result.returncode == 0, result.stderr


def test_session_commands_binds_reconciler_constants_after_reconciler_first() -> None:
    """Importing the reconciler first must not leave session_commands unbound."""
    result = _import_in_fresh_interpreter(
        "import src.sessions.reconciler; "
        "import src.commands.session_commands as sc; "
        "assert sc.DRAIN_ACK_KEY == 'AQ_DRAIN_ACK'"
    )
    assert result.returncode == 0, result.stderr


def test_claim_file_helpers_are_importable_without_the_commands_package() -> None:
    """``src.claim_file`` is a leaf: it must not drag in ``src.commands``."""
    result = _import_in_fresh_interpreter(
        "import sys; import src.claim_file; "
        "assert 'src.commands' not in sys.modules, sorted(sys.modules)"
    )
    assert result.returncode == 0, result.stderr


def test_claim_commands_still_re_exports_the_claim_file_helpers() -> None:
    """Call sites that import the helpers from ``claim_commands`` keep working."""
    from src.claim_file import read_claim_file, write_claim_file
    from src.commands import claim_commands

    assert claim_commands.write_claim_file is write_claim_file
    assert claim_commands.read_claim_file is read_claim_file
