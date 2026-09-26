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
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Keep S1-S3 together: the claim and filing cases use the pool S1 creates.
# Provider outage and recovery prepare independent fixtures in separate groups.
SCENARIO_GROUPS = {
    "claims": ("S1", "S2", "S3", "S4", "S7", "S18"),
    "cli": ("S5", *(f"S{number}" for number in range(8, 15))),
    "graphs": ("S6", "S16b", "S19"),
    "failover": ("S15", "S16a", "S17"),
}


def _unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
@pytest.mark.timeout(540)
@pytest.mark.parametrize("scenarios", SCENARIO_GROUPS.values(), ids=list(SCENARIO_GROUPS))
def test_disposable_daemon_stateful_cli_smoke(tmp_path, scenarios):
    env = {
        **os.environ,
        "AQ_E2E_HOME": str(tmp_path / "aq-e2e"),
        "AQ_E2E_PORT": str(_unused_loopback_port()),
        "E2E_DB_NAME": f"aq_e2e_cli_{os.getpid()}_{uuid.uuid4().hex[:8]}",
        "AQ_E2E_SESSION_PROVIDER": "fake",
        "AQ_E2E_CONVERGE_TIMEOUT": "90",
        "PYTHONUNBUFFERED": "1",
    }
    postgres_test_dsn = env.get("POSTGRES_TEST_DSN")
    if postgres_test_dsn:
        parsed = urlsplit(postgres_test_dsn)
        env.update(
            {
                "E2E_PG_HOST": parsed.hostname or "localhost",
                "E2E_PG_PORT": str(parsed.port or 5432),
                "E2E_PG_USER": parsed.username or "agent_queue",
                "E2E_PG_PASSWORD": parsed.password or "agent_queue_dev",
            }
        )
    setup = REPO_ROOT / "scripts" / "e2e-env.sh"
    smoke = REPO_ROOT / "scripts" / "e2e-smoke.sh"
    cleanup = REPO_ROOT / "scripts" / "e2e-clean.sh"

    try:
        subprocess.run([str(setup), "--reset"], cwd=REPO_ROOT, env=env, check=True, timeout=180)
        result = subprocess.run(
            [str(smoke), *scenarios],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            check=False,
            text=True,
            # CI gives each group its own runner and a five-minute job budget.
            # Leave thirty seconds for environment setup and cleanup; daemon
            # startup is part of this subprocess, alongside the scenarios.
            timeout=270,
        )
        # Keep the scenario durations visible on successful CI runs too;
        # a slow tail can otherwise only be diagnosed after a failure.
        print("\n".join(
            line for line in result.stdout.splitlines()
            if line.startswith(("PASS S", "FAIL S"))
        ))
        assert result.returncode == 0, f"{result.stdout}\n--- stderr ---\n{result.stderr}"
        assert f"{len(scenarios)}/{len(scenarios)} scenarios passed" in result.stdout
        passed = {
            line.split()[1] for line in result.stdout.splitlines() if line.startswith("PASS S")
        }
        assert passed == set(scenarios), result.stdout
        for status in (
            "passed",
            "unsupported",
            "dependency-unavailable",
            "explicitly-untested",
        ):
            assert status in result.stdout
    except subprocess.TimeoutExpired as exc:
        # Preserve completed scenario timings when a group exceeds its budget.
        # TimeoutExpired carries bytes even when subprocess.run uses text=True.
        print((exc.stdout or b"").decode(errors="replace"), flush=True)
        print((exc.stderr or b"").decode(errors="replace"), flush=True)
        raise
    finally:
        if Path(env["AQ_E2E_HOME"]).exists():
            subprocess.run([str(cleanup)], cwd=REPO_ROOT, env=env, check=False, timeout=90)
