"""Opt-in real-daemon acceptance test for the disposable CLI smoke kit.

This is intentionally excluded by ``aq test``'s default marker expression.
It owns a unique PostgreSQL database, API port, data directory, repositories,
vault, plugin fixture and fake-session daemon, then destroys all of them.
"""

from __future__ import annotations

import os
import socket
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_disposable_daemon_stateful_cli_smoke(tmp_path):
    env = {
        **os.environ,
        "AQ_E2E_HOME": str(tmp_path / "aq-e2e"),
        "AQ_E2E_PORT": str(_unused_loopback_port()),
        "E2E_DB_NAME": f"aq_e2e_cli_{os.getpid()}_{uuid.uuid4().hex[:8]}",
        "AQ_E2E_SESSION_PROVIDER": "fake",
        "AQ_E2E_CONVERGE_TIMEOUT": "90",
    }
    setup = REPO_ROOT / "scripts" / "e2e-env.sh"
    smoke = REPO_ROOT / "scripts" / "e2e-smoke.sh"
    cleanup = REPO_ROOT / "scripts" / "e2e-clean.sh"

    try:
        subprocess.run([str(setup), "--reset"], cwd=REPO_ROOT, env=env, check=True, timeout=180)
        result = subprocess.run(
            [str(smoke)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            check=False,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, f"{result.stdout}\n--- stderr ---\n{result.stderr}"
        assert "14/14 scenarios passed" in result.stdout
        for status in (
            "passed",
            "unsupported",
            "dependency-unavailable",
            "explicitly-untested",
        ):
            assert status in result.stdout
    finally:
        if Path(env["AQ_E2E_HOME"]).exists():
            subprocess.run(
                [str(cleanup)], cwd=REPO_ROOT, env=env, check=False, timeout=90
            )
