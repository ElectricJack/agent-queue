"""Individually selectable CLI scenarios sharing one disposable world per shard.

Use ``--dist loadgroup`` under xdist: a shard stays on one worker. Every fixture
still owns a unique database, API port and home if another worker selects it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# Each scenario item's own limit, including a fresh world's setup on its
# shard's first item.
E2E_TEST_TIMEOUT_SECONDS = 540
SCENARIO_GROUPS = {
    "claims": ("S1", "S2", "S3", "S7", "S19"),
    "cli": ("S5", "S8", "S9", "S12", "S17", "S20"),
    "graphs": ("S10", "S16b", "S18", "S6"),
    "failover": ("S4", "S11", "S13", "S14", "S15", "S16a"),
}
PREREQUISITES = {"S2": ("S1",), "S3": ("S1", "S2")}


def _unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def world_env(home: Path) -> dict[str, str]:
    env = {
        **{k: v for k, v in os.environ.items()
           if not k.startswith(("AQ_E2E_", "E2E_"))},
        "AQ_E2E_HOME": str(home),
        "AQ_E2E_PORT": str(_unused_loopback_port()),
        "AQ_E2E_TMUX_SOCKET": f"aq-e2e-{uuid.uuid4().hex[:12]}",
        "E2E_DB_NAME": f"aq_e2e_cli_{os.getpid()}_{uuid.uuid4().hex[:8]}",
        "AQ_E2E_SESSION_PROVIDER": "fake",
        "AQ_E2E_CONVERGE_TIMEOUT": "90",
        "AQ_E2E_CYCLE_SECONDS": "1",
        "AQ_E2E_CONFIG_POLL_SECONDS": "0.5",
        "AQ_E2E_GRAPH_SWEEP_SECONDS": "1",
        "AQ_E2E_POLL_SECONDS": "0.25",
        "PYTHONUNBUFFERED": "1",
    }
    if target := os.environ.get("AQ_E2E_TIMINGS_DIR"):
        env["AQ_E2E_TIMINGS_DIR"] = target
    # A caller's operator/other E2E endpoint must never override our free port.
    env["AQ_E2E_API_URL"] = f"http://127.0.0.1:{env['AQ_E2E_PORT']}"
    env["AQ_API_URL"] = env["AQ_E2E_API_URL"]
    env.pop("AQ_E2E_EXTRA_CONFIG", None)
    postgres_test_dsn = env.get("POSTGRES_TEST_DSN")
    if postgres_test_dsn:
        parsed = urlsplit(postgres_test_dsn)
        env.update({
            "E2E_PG_HOST": parsed.hostname or "localhost",
            "E2E_PG_PORT": str(parsed.port or 5432),
            "E2E_PG_USER": parsed.username or "agent_queue",
            "E2E_PG_PASSWORD": parsed.password or "agent_queue_dev",
        })
    return env


def load_smoke(env):
    spec = importlib.util.spec_from_file_location("e2e_fixture_smoke", REPO_ROOT / "scripts/e2e/smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        with patch.dict(os.environ, env):
            spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class World:
    def __init__(self, group, env):
        self.group = group
        self.env = env
        self.completed: set[str] = set()
        self.poisoned = False
        self.smoke = load_smoke(env)
        self.swarm = None
        self.cli_server = None

    def timing(self, data):
        row = {"group": self.group, **data}
        print("TIMING " + json.dumps(row), flush=True)
        if target := self.env.get("AQ_E2E_TIMINGS_DIR"):
            directory = Path(target)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f"{self.group}-{self.env['E2E_DB_NAME']}.jsonl").open("a") as stream:
                stream.write(json.dumps(row) + "\n")

    def reset(self):
        """Clear frontiers and held work through the disposable daemon's API."""
        self.completed.clear()
        smoke = self.smoke
        with patch.dict(os.environ, self.env):
            def empty():
                sessions = smoke.collection_rows(
                    smoke.api_checked("session_list", {"live_only": True}), "sessions",
                )
                for session in sessions:
                    smoke.api_checked("session_kill", {"session_id": session["id"]})
                projects = smoke.collection_rows(smoke.api_checked("list_projects", {}), "projects")
                remaining = []
                for project in projects:
                    rows = smoke.collection_rows(smoke.api_checked("list_tasks", {
                        "project_id": project["id"], "include_completed": True,
                    }), "tasks")
                    for task in rows:
                        if not task.get("parent_task_id"):
                            smoke.api("delete_task", {
                                "task_id": task["id"], "cascade": True, "branches": "delete",
                            })
                    remaining.extend(smoke.collection_rows(smoke.api_checked("list_tasks", {
                        "project_id": project["id"], "include_completed": True,
                    }), "tasks"))
                return not sessions and not remaining

            try:
                smoke.wait_for(empty, what="all scenario tasks and sessions to be removed")
                # These are the fixture's declared defaults. Even a failure before
                # a scenario's own finally block must restore every global switch.
                smoke.api_checked("update_config", {"section": "swarm", "data": self.swarm})
                smoke.fake_script()
                for provider in ("claude", "codex", smoke.PROVA, smoke.PROVB):
                    smoke.api_checked("provider_set_state", {"provider": provider, "state": "auto"})
                smoke.api_checked("set_playbook_enabled", {
                    "playbook_id": smoke.FAILOVER_PLAYBOOK, "enabled": True,
                })
            except BaseException:
                self.poisoned = True
                raise

    def run(self, key):
        assert not self.poisoned, "fixture cleanup failed; refusing to reuse contaminated state"
        scenarios = [p for p in PREREQUISITES.get(key, ()) if p not in self.completed] + [key]
        succeeded = False
        started = time.monotonic()
        try:
            result = subprocess.run(
                [str(REPO_ROOT / "scripts/e2e-smoke.sh"), *scenarios],
                cwd=REPO_ROOT, env=self.env, capture_output=True, text=True,
                check=False, timeout=270,
            )
            print(result.stdout, flush=True)
            for line in result.stdout.splitlines():
                if line.startswith("TIMING "):
                    self.timing(json.loads(line.removeprefix("TIMING ")))
            assert result.returncode == 0, f"{result.stdout}\n--- stderr ---\n{result.stderr}"
            passed = {line.split()[1] for line in result.stdout.splitlines() if line.startswith("PASS S")}
            assert passed == set(scenarios), result.stdout
            assert f"{len(scenarios)}/{len(scenarios)} scenarios passed" in result.stdout
            for status in ("passed", "unsupported", "dependency-unavailable", "explicitly-untested"):
                assert status in result.stdout
            self.completed.update(scenarios)
            succeeded = True
        except subprocess.TimeoutExpired as exc:
            for output in (exc.stdout, exc.stderr):
                print(output.decode(errors="replace") if isinstance(output, bytes) else output or "")
            raise AssertionError(f"scenario {key} exceeded its 270s budget") from None
        finally:
            # Preserve only the explicitly declared S1-S3 chain, and only on
            # success. An assertion failure never carries state into another item.
            if key not in ("S1", "S2") or not succeeded:
                self.reset()
            self.timing({"item": key, "item_seconds": time.monotonic() - started})


def start_cli_server(world):
    # Start once per world. Each CLI command forks an isolated child while
    # retaining Click/REST/formatting coverage and its real process exit.
    world.env["AGENT_QUEUE_DATA"] = world.env["AQ_E2E_HOME"]
    world.env["AQ_E2E_CLI_SOCKET"] = str(Path(world.env["AQ_E2E_HOME"]) / "cli.sock")
    started = time.monotonic()
    with (Path(world.env["AQ_E2E_HOME"]) / "cli-server.log").open("w") as log:
        world.cli_server = subprocess.Popen(
            [sys.executable, "-m", "scripts.e2e.cli_server", world.env["AQ_E2E_CLI_SOCKET"]],
            cwd=REPO_ROOT, env=world.env, stdout=log, stderr=log, start_new_session=True,
        )
    deadline = time.monotonic() + 30
    while not Path(world.env["AQ_E2E_CLI_SOCKET"]).exists():
        assert world.cli_server.poll() is None, "CLI preload server exited"
        assert time.monotonic() < deadline, "CLI preload server did not become ready"
        time.sleep(0.05)
    world.timing({"stage": "cli_preload", "seconds": time.monotonic() - started})


@contextmanager
def disposable_world(group, env):
    if group == "cli":
        # S20 must prove publication by the active train. Keep the selector
        # in this shard's isolated config, never the operator's install.
        fragment = Path(env["AQ_E2E_HOME"]).with_suffix(".yaml")
        fragment.write_text("integration:\n  git_first: active\n")
        env["AQ_E2E_EXTRA_CONFIG"] = str(fragment)
    world = World(group, env)
    try:
        for stage, script, args in (
            ("build", "e2e-env.sh", ["--reset"]),
            ("start", "e2e-daemon.sh", ["start"]),
        ):
            started = time.monotonic()
            result = subprocess.run([str(REPO_ROOT / "scripts" / script), *args],
                                    cwd=REPO_ROOT, env=env, check=False, timeout=180,
                                    capture_output=True, text=True)
            assert result.returncode == 0, f"{stage} failed: {result.stdout}\n{result.stderr}"
            world.timing({"stage": stage, "seconds": time.monotonic() - started})
        start_cli_server(world)
        world.swarm = yaml.safe_load((Path(env["AQ_E2E_HOME"]) / "config.yaml").read_text())["swarm"]
        yield world
    finally:
        if world.cli_server is not None:
            try:
                os.killpg(world.cli_server.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                world.cli_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(world.cli_server.pid, signal.SIGKILL)
                world.cli_server.wait(timeout=5)
        if (Path(env["AQ_E2E_HOME"]) / ".aq-e2e").exists():
            started = time.monotonic()
            # A cleanup refusal is a test failure, never a silent successful run.
            result = subprocess.run([str(REPO_ROOT / "scripts/e2e-clean.sh")],
                                    cwd=REPO_ROOT, env=env, check=False, timeout=90,
                                    capture_output=True, text=True)
            assert result.returncode == 0, f"cleanup failed: {result.stdout}\n{result.stderr}"
            world.timing({"stage": "teardown", "seconds": time.monotonic() - started})


@pytest.fixture(scope="module")
def e2e_world(request, tmp_path_factory):
    with disposable_world(request.param, world_env(tmp_path_factory.mktemp(f"e2e-{request.param}") / "aq-e2e")) as world:
        yield world


@pytest.mark.integration
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize(
    "e2e_world,scenario_key",
    [pytest.param(group, key, id=f"{group}-{key}", marks=pytest.mark.xdist_group(group))
     for group, keys in SCENARIO_GROUPS.items() for key in keys],
    indirect=["e2e_world"], scope="module",
)
def test_disposable_daemon_scenario(e2e_world, scenario_key):
    e2e_world.run(scenario_key)


@pytest.mark.integration
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_dashboard_pool_toggle(tmp_path):
    """Real browser enable/disable persists across reload, even with slow reads."""
    env = world_env(tmp_path / "aq-e2e")
    env["AQ_E2E_DASHBOARD_PORT"] = str(_unused_loopback_port())
    with disposable_world("dashboard-pool-toggle", env) as world:
        result = subprocess.run(
            ["node", "scripts/e2e/pool-toggle.mjs"],
            cwd=REPO_ROOT, env=world.env, capture_output=True, text=True,
            check=False, timeout=120,
        )
        assert result.returncode == 0, f"browser toggle failed: {result.stdout}\n{result.stderr}"
        print(result.stdout)


@pytest.mark.perf
def test_backend_responsiveness_benchmark(tmp_path, perf_strict):
    """Opt-in matched production cadence, CPU/DB idle load and transition samples."""
    from scripts.e2e.benchmark import run

    env = world_env(tmp_path / "aq-e2e")
    env.update(AQ_E2E_CYCLE_SECONDS="5", AQ_E2E_CONFIG_POLL_SECONDS="30",
               AQ_E2E_GRAPH_SWEEP_SECONDS="5", AQ_E2E_POLL_SECONDS="0.25")
    with disposable_world("benchmark", env) as world:
        result = run(world)
    if target := os.environ.get("AQ_E2E_BENCHMARK_OUT"):
        Path(target).write_text(json.dumps(result, indent=2) + "\n")
