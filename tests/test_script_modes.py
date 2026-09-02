"""The e2e kit's entry-point scripts must be executable in the index.

``e2e-smoke.sh`` and ``e2e-daemon.sh`` invoke their siblings by absolute
path rather than through ``bash``, and docs/guides/e2e-swarm.md tells
people to run them directly, so a 100644 mode in git breaks a fresh clone
or worktree with "Permission denied".
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# e2e-common.sh is intentionally excluded: it is sourced, never executed.
EXECUTABLE_SCRIPTS = [
    "scripts/e2e-daemon.sh",
    "scripts/e2e-dashboard.sh",
    "scripts/e2e-env.sh",
    "scripts/e2e-smoke.sh",
]


def _index_mode(path: str) -> str:
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{path} is not tracked by git"
    return out.split()[0]


def test_e2e_entry_point_scripts_are_executable_in_the_index():
    modes = {path: _index_mode(path) for path in EXECUTABLE_SCRIPTS}
    assert modes == {path: "100755" for path in EXECUTABLE_SCRIPTS}
