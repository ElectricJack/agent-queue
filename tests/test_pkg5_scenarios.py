from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pkg5_scenarios" / "build_payloads.py"


def test_build_payloads_supports_output_directory_outside_repository(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in tmp_path.iterdir()} == {
        "01-branching.json",
        "02-convergence.json",
        "03-loop.json",
        "04-ai-node.json",
        "05-stale-contract.json",
        "06-diff-review.json",
        "07-run-overlay-old-artifact.json",
        "index.json",
    }
    assert f"wrote {tmp_path / '01-branching.json'}" in result.stdout
