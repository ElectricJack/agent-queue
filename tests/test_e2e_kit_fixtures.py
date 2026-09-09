"""The swarm e2e kit's Tier-1 stand-ins must close the way a real agent would.

Under ``sessions.provider: fake`` nothing is spawned: ``scripts/e2e/smoke.py``
*is* the session, and it produces no commits.  The e2e remote is a bare
local repository, so no pull request can ever exist for a task branch there.
Two things follow, and each is pinned here because losing either one turns
S4 into "close refused: No open PR found" (task solid-forge-63):

* the runner closes a formula child with ``--work-outcome no-op`` — the
  pipeline's own word for "this task produced no code" — not ``shipped``,
  which under the ``pull_request`` default demands an open PR;
* the generated ``reviewer`` fixture is ``read_only`` like the shipped
  reviewer profile, so a review is a no-code task by declaration and never
  has to answer the PR gate at all.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.profiles.parser import parse_profile

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "scripts" / "e2e" / "smoke.py"
E2E_ENV = REPO_ROOT / "scripts" / "e2e-env.sh"
CLEANUP = REPO_ROOT / "scripts" / "e2e-clean.sh"
DBSETUP = REPO_ROOT / "scripts" / "e2e" / "dbsetup.py"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("e2e_smoke", SMOKE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolve ``from __future__ import annotations`` through
    # ``sys.modules[cls.__module__]``, so the module must be registered
    # before its body runs.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_s4_child_close_reports_a_no_op_work_outcome(monkeypatch):
    smoke = _load_smoke()
    calls: list[tuple] = []

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("task", "children"):
            return {"children": [{"id": "child-1", "status": "IN_PROGRESS"}]}
        if args[:2] == ("session", "list"):
            return {"sessions": [{"id": "sess-1", "task_id": "child-1", "state": "running"}]}
        if args[:2] == ("task", "close"):
            return {"success": True}
        raise AssertionError(f"unexpected aq call: {args}")

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "session_token", lambda session_id: f"tok-{session_id}")

    smoke._close_next_child("container-1")

    closes = [(a, kw) for a, kw in calls if a[:2] == ("task", "close")]
    assert len(closes) == 1
    args, kwargs = closes[0]
    assert args[2] == "child-1"
    assert kwargs == {"token": "tok-sess-1", "session_id": "sess-1"}
    outcome = args[args.index("--work-outcome") + 1]
    assert outcome == "no-op", (
        "the Tier-1 runner produces no commits; anything but no-op makes the "
        "pull_request default demand a PR the bare e2e remote cannot carry"
    )


def _vault_fixture_section() -> str:
    """The ``3. Vault fixtures`` block of e2e-env.sh, as bash source."""
    text = E2E_ENV.read_text()
    match = re.search(r"^# 3\. Vault fixtures\n.*?(?=^# -+\n# 4\. Config)", text, re.DOTALL | re.MULTILINE)
    assert match, "e2e-env.sh no longer has a '3. Vault fixtures' section"
    return match.group(0)


@pytest.fixture
def generated_profiles(tmp_path):
    vault = tmp_path / "vault"
    (vault / "agent-types").mkdir(parents=True)
    (vault / "formulas").mkdir()
    env = {**os.environ, "E2E_VAULT": str(vault), "REPO_ROOT": str(REPO_ROOT)}
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _vault_fixture_section()],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    def read(role: str):
        parsed = parse_profile((vault / "agent-types" / role / "profile.md").read_text())
        assert not parsed.errors, parsed.errors
        return parsed

    return read


def test_e2e_reviewer_fixture_is_read_only_like_the_shipped_reviewer(generated_profiles):
    reviewer = generated_profiles("reviewer")
    assert reviewer.config.get("read_only") is True
    allowed = set((reviewer.tools or {}).get("allowed") or [])
    assert not allowed & {"Write", "Edit"}, allowed


def test_e2e_coding_fixture_keeps_its_write_tools(generated_profiles):
    coding = generated_profiles("coding")
    assert not coding.config.get("read_only")
    allowed = set((coding.tools or {}).get("allowed") or [])
    assert {"Write", "Edit"} <= allowed, allowed


def test_pool_worker_close_reports_a_no_op_work_outcome(monkeypatch):
    smoke = _load_smoke()
    calls: list[tuple] = []

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        return {"success": True, "next": {"result": "drain_requested"}}

    monkeypatch.setattr(smoke, "aq", fake_aq)
    worker = smoke.Worker(session_id="sess-1", token="tok", claim_epoch=3, task_id="t-1")

    worker.close(claim_next=True, summary="S2 task")

    (args, kwargs), = calls
    assert args[:2] == ("task", "close")
    assert args[args.index("--work-outcome") + 1] == "no-op"
    assert args[args.index("--claim-epoch") + 1] == "3"
    assert "--claim-next" in args
    assert kwargs == {"token": "tok", "session_id": "sess-1", "check_ok": True}


def test_cli_subprocesses_replace_only_db_sentinels_with_disposable_resources(monkeypatch):
    smoke = _load_smoke()
    monkeypatch.setenv("AQ_E2E_HOME", "/tmp/aq-e2e-owned")
    monkeypatch.setenv("E2E_DB_URL", "postgresql+asyncpg://example/e2e_only")
    monkeypatch.setenv("AGENT_QUEUE_DB", "postgresql://refusal/sentinel")
    monkeypatch.setenv("AQ_DATABASE_URL", "postgresql://refusal/sentinel")
    monkeypatch.setenv("AQ_DB_SCOPE", "worker")

    env = smoke._cli_env()

    assert env["AGENT_QUEUE_DATA"] == "/tmp/aq-e2e-owned"
    assert env["AGENT_QUEUE_DB"] == "postgresql+asyncpg://example/e2e_only"
    assert env["AQ_DATABASE_URL"] == "postgresql+asyncpg://example/e2e_only"
    assert env["AQ_DB_SCOPE"] == "worker"


def test_fresh_workers_quiesces_every_project_in_the_global_profile(monkeypatch):
    smoke = _load_smoke()
    open_tasks = {
        smoke.PROJECT: {"e2e-leftover"},
        smoke.OTHER_PROJECT: {"other-leftover"},
    }
    live = [
        {"id": "s-e2e", "project_id": smoke.PROJECT, "state": "running"},
        {"id": "s-other", "project_id": smoke.OTHER_PROJECT, "state": "running"},
    ]
    created = []

    def fake_aq(*args, **kwargs):
        if args[:2] == ("task", "list"):
            project = args[args.index("--project") + 1]
            return [{"id": task_id} for task_id in sorted(open_tasks[project])]
        if args[:2] == ("task", "delete"):
            task_id = args[args.index("--task-id") + 1]
            for task_ids in open_tasks.values():
                task_ids.discard(task_id)
            return {"success": True}
        if args[:2] == ("session", "list"):
            return {"sessions": list(live)}
        if args[:2] == ("session", "kill"):
            session_id = args[2]
            live[:] = [row for row in live if row["id"] != session_id]
            return {"success": True}
        if args[:2] == ("session", "token"):
            return {"token": f"token-{args[2]}"}
        raise AssertionError(f"unexpected aq call: {args}")

    def fake_create(title, **_kwargs):
        created.append(title)
        task_id = f"primer-{len(created)}"
        open_tasks[smoke.PROJECT].add(task_id)
        if len(created) == 2:
            live.extend(
                [
                    {"id": "fresh-1", "project_id": smoke.PROJECT, "state": "running"},
                    {"id": "fresh-2", "project_id": smoke.PROJECT, "state": "running"},
                ]
            )
        return task_id

    def immediate_wait(predicate, **_kwargs):
        for _ in range(10):
            value = predicate()
            if value:
                return value
        raise AssertionError("predicate did not converge")

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "create_task", fake_create)
    monkeypatch.setattr(smoke, "wait_for", immediate_wait)

    workers = smoke.fresh_workers(2)

    assert [worker.session_id for worker in workers] == ["fresh-1", "fresh-2"]
    assert "other-leftover" not in open_tasks[smoke.OTHER_PROJECT]


def test_capability_report_uses_exhaustive_status_vocabulary():
    smoke = _load_smoke()
    passed = smoke.Scenario("P", "pass", lambda _: None, ("passing family",), ok=True)
    broken = smoke.Scenario("B", "break", lambda _: None, ("broken family",), ok=False)

    statuses = {
        row["status"] for row in smoke.Report(scenarios=[passed, broken]).capability_rows()
    }

    assert statuses == {
        "passed",
        "broken",
        "unsupported",
        "dependency-unavailable",
        "explicitly-untested",
    }


def test_stateful_scenarios_cover_the_audited_mutation_families():
    smoke = _load_smoke()
    by_key = {scenario.key: scenario for scenario in smoke.SCENARIOS}

    assert set(by_key) == {f"S{number}" for number in range(1, 15)}
    assert by_key["S9"].families == ("task CRUD/rollback",)
    assert set(by_key["S10"].families) == {"workspace CRUD", "file/git/note CRUD"}
    assert by_key["S11"].families == ("message CRUD",)
    assert by_key["S12"].families == ("MCP registry CRUD",)
    assert by_key["S13"].families == ("plugin extension startup",)
    assert by_key["S14"].families == ("graph/vault",)


def test_e2e_env_generates_an_opt_in_plugin_entry_point_and_local_message_sink():
    text = E2E_ENV.read_text()

    assert "AQ_E2E_PLUGIN_FIXTURE/aq_e2e_fixture-1.0.dist-info/entry_points.txt" in text
    assert "e2e-fixture = e2e_fixture_plugin:E2EFixturePlugin" in text
    assert "messaging_platform: none" in text
    assert re.search(r"^messages:\n  enabled: true$", text, re.MULTILINE)


@pytest.mark.parametrize("name", ["agent_queue", "customer_production", "postgres_master"])
def test_e2e_dbsetup_refuses_non_e2e_database_names_before_connecting(name):
    result = subprocess.run(
        [sys.executable, str(DBSETUP), "postgresql://unused/unused", name, "--drop"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "refusing to manage" in result.stderr


def _cleanup_env(tmp_path: Path, home: Path, *, socket: str = "aq-e2e-test"):
    """Return an environment whose destructive commands only append to a trace."""
    bin_dir = tmp_path / "command-stubs"
    bin_dir.mkdir(exist_ok=True)
    trace = tmp_path / "cleanup-actions"
    stub = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$AQ_E2E_ACTION_LOG"
exit 0
"""
    for command in ("tmux", "python3", "rm"):
        path = bin_dir / command
        path.write_text(stub)
        path.chmod(0o755)
    env = {
        **os.environ,
        "AQ_E2E_HOME": str(home),
        "AQ_E2E_SESSION_PROVIDER": "tmux",
        "AQ_E2E_TMUX_SOCKET": socket,
        "AQ_E2E_ACTION_LOG": str(trace),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return env, trace


def _start_src_main_decoy(home: Path):
    """Give cleanup a matching pid that is safe to signal if the guard regresses."""
    process = subprocess.Popen(["bash", "-c", "exec -a src.main sleep 60"])
    (home / "daemon.pid").write_text(str(process.pid))
    return process


def _assert_cleanup_refused_without_actions(env, trace: Path, decoy=None):
    try:
        result = subprocess.run(
            [str(CLEANUP)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert not trace.exists(), trace.read_text() if trace.exists() else ""
        if decoy is not None:
            assert decoy.poll() is None, "cleanup signalled the decoy src.main process"
    finally:
        if decoy is not None and decoy.poll() is None:
            decoy.terminate()
            decoy.wait(timeout=5)


def test_cleanup_rejects_an_unresolvable_home_before_any_action(tmp_path):
    home = tmp_path / "missing"
    env, trace = _cleanup_env(tmp_path, home)

    _assert_cleanup_refused_without_actions(env, trace)


def test_cleanup_rejects_an_unmarked_home_before_any_action(tmp_path):
    home = tmp_path / "unmarked"
    home.mkdir()
    decoy = _start_src_main_decoy(home)
    env, trace = _cleanup_env(tmp_path, home)

    _assert_cleanup_refused_without_actions(env, trace, decoy)


def test_cleanup_rejects_a_protected_home_before_any_action(tmp_path):
    home = tmp_path / "protected-home"
    home.mkdir()
    (home / ".aq-e2e").touch()
    decoy = _start_src_main_decoy(home)
    env, trace = _cleanup_env(tmp_path, home)
    env["HOME"] = str(home)

    _assert_cleanup_refused_without_actions(env, trace, decoy)


@pytest.mark.parametrize("socket", ["aq", "default"])
def test_cleanup_rejects_operator_and_default_tmux_sockets_before_any_action(
    tmp_path, socket
):
    home = tmp_path / f"owned-{socket}"
    home.mkdir()
    (home / ".aq-e2e").touch()
    decoy = _start_src_main_decoy(home)
    env, trace = _cleanup_env(tmp_path, home, socket=socket)

    _assert_cleanup_refused_without_actions(env, trace, decoy)
