"""The swarm e2e kit's Tier-1 stand-ins must close the way a real agent would.

Under ``sessions.provider: fake`` nothing is spawned: ``scripts/e2e/smoke.py``
*is* the session. Most scenarios produce no commits; S20 exercises real Git
delivery with a credential adapter limited to the disposable home. The e2e remote is a bare
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
import json
import os
import re
import subprocess
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.config import load_config
from src.jobs.adapters import finite_command
from src.jobs.policy import presets, validate_args
from src.profiles.parser import parse_profile
from src.routing.planner import ProfileFacts, Snapshot, TaskFacts, plan_route, worker_classes
from tests.test_e2e_cli_stateful import SCENARIO_GROUPS
from tests.test_routing_planner import DIGEST as ROUTING_DIGEST
from tests.test_routing_planner import POLICY as ROUTING_POLICY

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "scripts" / "e2e" / "smoke.py"
E2E_ENV = REPO_ROOT / "scripts" / "e2e-env.sh"
CLEANUP = REPO_ROOT / "scripts" / "e2e-clean.sh"
DBSETUP = REPO_ROOT / "scripts" / "e2e" / "dbsetup.py"
APP_TRAIN = REPO_ROOT / "scripts" / "e2e" / "app_train.py"


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


def test_app_train_files_hint_only_graph_then_runs_review_playbook(monkeypatch, tmp_path):
    app = _load_app_train(monkeypatch, tmp_path)
    app.write_profiles(None)
    state = {"project_id": "fixture-project"}
    calls = []

    def fake_operator_text(*args, **_kwargs):
        assert args[:4] == ("task", "create", "--project", "fixture-project")
        graph = yaml.safe_load((tmp_path / "epic-s4.yaml").read_text())
        assert graph["defaults"] == {"intelligence_class": app.TRAIN_CLASS,
                                     "task_type": "feature"}
        assert [node["key"] for node in graph["nodes"]] == ["leaf"]
        assert "profile" not in graph["nodes"][0]
        return 0, "valid"

    def fake_operator(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("task", "create"):
            return {"parent_id": "epic-1", "nodes": [{"key": "leaf", "task_id": "epic-1.1"}]}
        if args[:2] == ("task", "route"):
            assert args == ("task", "route", "--task-id", "epic-1.1",
                            "--profile-id", app.WORKER_PROFILE,
                            "--intelligence-class", app.TRAIN_CLASS)
            return {"success": True}
        if args[:2] == ("playbook", "import"):
            assert any(call[:2] == ("task", "route") for call in calls)
            return {"success": True, "activated": True}
        if args[:2] == ("playbook", "run"):
            assert any(call[:2] == ("playbook", "import") for call in calls)
            return {"status": "completed", "failed_steps": []}
        if args[:2] == ("task", "children"):
            children = [{"id": "epic-1.1", "profile_id": "train-worker"}]
            if any(call[:2] == ("playbook", "run") for call in calls):
                children.append({"id": "epic-1.2", "profile_id": "reviewer"})
            return children
        if args[:2] == ("task", "deps"):
            return {"provenance": [{"id": "epic-1.1", "dep_type": "discovered-from"}]}
        raise AssertionError(args)

    monkeypatch.setattr(app, "require_approval", lambda: "approved")
    monkeypatch.setattr(app, "load_state", lambda: state)
    monkeypatch.setattr(app, "save_state", lambda _state: None)
    monkeypatch.setattr(app, "operator_text", fake_operator_text)
    monkeypatch.setattr(app, "operator", fake_operator)
    monkeypatch.setattr(app, "save_payload", lambda *_args: None)
    monkeypatch.setattr(app, "fixture_main_sha", lambda: "fixture-main-sha")
    monkeypatch.setattr(app, "gh_api", lambda *_args, **_kwargs: {
        "object": {"sha": "fixture-main-sha"}})
    monkeypatch.setattr(app, "record", lambda *_args, **_kwargs: None)

    app.create_epic(SimpleNamespace(scenario="S4", title="fixture change", write=["x.txt=x"],
                                    copy=None, delete=None))

    assert state["scenarios"]["S4"]["review_id"] == "epic-1.2"
    assert state["scenarios"]["S4"]["leaf_routed"] is True
    assert state["scenarios"]["S4"]["epic_recorded"] is True
    artifact_path = tmp_path / "vault" / "reviewed-playbooks" / "app-train-review-s4" / "artifact.json"
    artifact = json.loads(artifact_path.read_text())
    steps = artifact["steps"]
    assert steps["file-review--ensure"]["inputs"]["profile_id"]["value"] == "reviewer"
    assert steps["file-review--ensure"]["inputs"]["parent_id"]["path"] == "parent_task_id"
    assert steps["file-review--provenance"]["inputs"]["dep_type"]["value"] == "discovered-from"
    assert "file-review--blocks" not in steps
    assert any(call[:2] == ("playbook", "import") for call in calls)
    assert any(call[:2] == ("playbook", "run") for call in calls)

    state["scenarios"]["S4"].pop("review_id")
    state["scenarios"]["S4"].pop("epic_recorded")
    first_runs = sum(call[:2] == ("playbook", "run") for call in calls)
    first_routes = sum(call[:2] == ("task", "route") for call in calls)
    app.create_epic(SimpleNamespace(scenario="S4", title="fixture change", write=["x.txt=x"],
                                    copy=None, delete=None))
    assert sum(call[:2] == ("playbook", "run") for call in calls) == first_runs
    assert sum(call[:2] == ("task", "route") for call in calls) == first_routes
    assert state["scenarios"]["S4"]["review_id"] == "epic-1.2"


def test_app_train_refuses_a_divergent_old_fixture_branch(monkeypatch, tmp_path):
    app = _load_app_train(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "gh_api", lambda *_args, **_kwargs: {
        "object": {"sha": "old-work-sha"}})
    assert app._fixture_branch_collides("aq/old-leaf", "fixture-main-sha")


def test_app_train_verifier_retries_fixable_close_before_draining(monkeypatch, tmp_path):
    app = _load_app_train(monkeypatch, tmp_path)
    results = iter([
        {"status": "IN_PROGRESS", "result": "verification_failed", "escalated": False,
         "issues": ["Parent integration completion was refused: stale_verification."]},
        {"status": "COMPLETED", "pipeline_ok": True},
    ])
    recorded = []
    monkeypatch.setattr(app, "worker_aq", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(app, "record", lambda *args: recorded.append(args))
    claimed = {"task_id": "verify-1", "claim_epoch": 1}

    assert app._verifier_close("S4", claimed, tmp_path) is None
    assert recorded == [("S4", "verifier_close_retry", {
        "task": "verify-1", "escalated": False, "issues": [
            "Parent integration completion was refused: stale_verification."]})]
    assert app._verifier_close("S4", claimed, tmp_path) == {
        "status": "COMPLETED", "pipeline_ok": True}


def test_app_train_verifier_keeps_waiting_for_the_named_trusted_evidence_refusal(
    monkeypatch, tmp_path
):
    """The named wait refusal (escalated on an unchanged replay) still waits."""
    app = _load_app_train(monkeypatch, tmp_path)
    results = iter([
        {"status": "IN_PROGRESS", "result": "verification_failed", "escalated": True,
         "issues": ["Parent integration completion was refused: "
                    "awaiting_trusted_verification (verification_not_recorded)."]},
        {"status": "COMPLETED", "pipeline_ok": True},
    ])
    recorded = []
    monkeypatch.setattr(app, "worker_aq", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(app, "record", lambda *args: recorded.append(args))
    claimed = {"task_id": "verify-1", "claim_epoch": 1}

    assert app._verifier_close("S4", claimed, tmp_path) is None
    assert recorded[0][0:2] == ("S4", "verifier_close_retry")
    assert recorded[0][2]["escalated"] is True
    assert app._verifier_close("S4", claimed, tmp_path) == {
        "status": "COMPLETED", "pipeline_ok": True}


def test_app_train_verifier_rejects_unexpected_close_refusal(monkeypatch, tmp_path):
    app = _load_app_train(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "worker_aq", lambda *_args, **_kwargs: {
        "result": "verification_failed", "issues": ["Checkout is dirty."],
    })

    with pytest.raises(app.Failure, match="Checkout is dirty"):
        app._verifier_close("S4", {"task_id": "verify-1", "claim_epoch": 1}, tmp_path)


def test_app_train_worker_aq_preserves_close_refusal_details(monkeypatch, tmp_path):
    app = _load_app_train(monkeypatch, tmp_path)
    payload = {"schema_version": 1, "data": None, "error": {
        "code": "command_error", "message": "close refused", "details": {
            "result": "verification_failed", "issues": ["Check evidence is pending."]}}}
    monkeypatch.setattr(app.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout=json.dumps(payload), stderr=""))

    result = app.worker_aq({"token": "scratch", "session_id": "scratch", "claim_epoch": 1},
                           "task", "close", cwd=tmp_path, check_ok=False)

    assert result["result"] == "verification_failed"
    assert result["issues"] == ["Check evidence is pending."]
    assert result["_error"]["code"] == "command_error"


def test_development_validation_preset_checks_the_committed_readme(tmp_path):
    smoke = _load_smoke()
    smoke._seed_development_validation(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("development fixture\n")
    preset, args = finite_command(smoke.DEVELOPMENT_VALIDATION_COMMAND)
    command = validate_args(presets(REPO_ROOT)[preset], args, tmp_path, worker_cap=1)

    passed = subprocess.run(
        command, cwd=tmp_path, capture_output=True, check=False, text=True, timeout=30
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "1 passed" in passed.stdout

    readme.unlink()
    failed = subprocess.run(
        command, cwd=tmp_path, capture_output=True, check=False, text=True, timeout=30
    )
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "test_readme_exists" in failed.stdout and "1 failed" in failed.stdout


@pytest.mark.parametrize("test_dsn", [None, "postgresql+asyncpg://test@localhost:5534/postgres"])
def test_e2e_config_enables_jobs_with_a_separate_test_database(tmp_path, test_dsn):
    (tmp_path / "onboarding").mkdir()
    text = E2E_ENV.read_text()
    section = re.search(
        r"^# 4\. Config\n.*?(?=^# -+\n# 5\. Database)", text, re.DOTALL | re.MULTILINE
    )
    assert section, "e2e-env.sh no longer has a '4. Config' section"
    env = {**os.environ, "AQ_E2E_HOME": str(tmp_path), "REPO_ROOT": str(REPO_ROOT)}
    env.pop("POSTGRES_TEST_DSN", None)
    if test_dsn:
        env["POSTGRES_TEST_DSN"] = test_dsn
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c",
         'source "$REPO_ROOT/scripts/e2e-common.sh"\n' + section.group(0)],
        check=True, env=env, capture_output=True, text=True,
    )
    config = load_config(str(tmp_path / "config.yaml"))
    assert config.resources.jobs.enabled
    assert config.resources.jobs.test_database_url == (
        test_dsn or
        "postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres"
    )
    assert config.resources.jobs.test_database_url != config.database.url


def test_s4_child_close_reports_a_no_op_work_outcome(monkeypatch):
    smoke = _load_smoke()
    calls: list[tuple] = []

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("task", "close"):
            return {"success": True}
        raise AssertionError(f"unexpected aq call: {args}")

    def fake_api(command, args):
        if command == "task_children":
            assert args == {"task_id": "container-1"}
            return {"children": [{"id": "child-1", "status": "IN_PROGRESS"}]}
        assert command == "session_list" and args == {}
        return {"sessions": [{"id": "sess-1", "task_id": "child-1", "state": "running"}]}

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "api", fake_api)
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


def _load_app_train(monkeypatch, home: Path):
    """Import ``scripts/e2e/app_train.py`` against *home* as its ``AQ_E2E_HOME``."""
    monkeypatch.setenv("AQ_E2E_HOME", str(home))
    # app_train puts its own directory on sys.path to import ``smoke``; a
    # copy keeps that insert out of the real sys.path.
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("e2e_app_train", APP_TRAIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _pool_routing_facts(profile_id: str, parsed) -> ProfileFacts:
    """``_routing_static_facts`` for one vault pool profile (``slots`` is ``max_active``).

    Every policy class is taken to have a model on the profile's provider.
    """
    config = parsed.config
    return ProfileFacts(
        id=profile_id,
        harness=str(config.get("harness") or ""),
        provider=str(config.get("harness") or ""),
        lifecycle="pool",
        default_class=str(config.get("default_class") or ""),
        classes=frozenset(ROUTING_POLICY.class_order),
        slots=int(config.get("max_active") or 1),
        template=bool(parsed.frontmatter.template),
        read_only=config.get("read_only") is True,
    )


def test_app_train_delegates_route_to_the_train_worker_pool(
    generated_profiles, tmp_path, monkeypatch
):
    """The router sends the App-mode train's verifier and repair delegates to train-worker.

    Integration repairs and parent verifiers are filed unrouted, with the train
    policy's class as a hint, and ``play_verifier`` / ``play_repair`` claim
    them as ``WORKER_PROFILE``.  That works only while train-worker is the
    router's one worker candidate at ``TRAIN_CLASS``.  A hand-written pool
    profile qualifies (mandatory-routing spec §4); it needs no ``extends``.
    Pools are the only candidates in this world: a task-lifecycle profile's
    slots are its enabled worker agents, and the kit registers none.
    """
    app_train = _load_app_train(monkeypatch, tmp_path)
    app_train.write_profiles(None)

    pools = []
    for path in sorted((tmp_path / "vault" / "agent-types").glob("*/profile.md")):
        parsed = parse_profile(path.read_text())
        if not parsed.errors and parsed.config.get("lifecycle") == "pool":
            pools.append(_pool_routing_facts(path.parent.name, parsed))
    by_id = {facts.id: facts for facts in pools}
    assert worker_classes(by_id[app_train.WORKER_PROFILE]) == {app_train.TRAIN_CLASS}
    assert worker_classes(by_id[app_train.REVIEWER_PROFILE]) == frozenset()
    assert [p.id for p in pools if app_train.TRAIN_CLASS in worker_classes(p)] == [
        app_train.WORKER_PROFILE
    ]

    snapshot = Snapshot(profiles=tuple(pools))
    delegates = {
        "repair": TaskFacts(
            task_id="repair-op-0", class_hint=app_train.TRAIN_CLASS,
            created_by_kind="integration_repair",
        ),
        "verifier": TaskFacts(task_id="verify-op", class_hint=app_train.TRAIN_CLASS),
    }
    for name, task in delegates.items():
        result = plan_route(task, ROUTING_POLICY, snapshot, policy_sha256=ROUTING_DIGEST)
        assert result.outcome == "planned", (name, result)
        assert result.value["profile_id"] == app_train.WORKER_PROFILE, (name, result.value)
        assert result.value["intelligence_class"] == app_train.TRAIN_CLASS, name
        assert result.value["classification"] is None, name


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


@pytest.fixture
def s2_surfaces(monkeypatch):
    smoke = _load_smoke()
    worker = smoke.Worker(session_id="holder", token="holder-token")
    responses = [{"result": "claimed", "task": {"id": "s1-2"}, "claim_epoch": 7}]
    calls = []
    replacement = {"id": "replacement", "state": "running"}
    sessions = [{"id": "holder", "state": "running"}, replacement]

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("task", "claim"):
            return responses.pop(0) if len(responses) > 1 else responses[0]
        if args[:2] == ("task", "heartbeat") and kwargs.get("check_ok") is False:
            return {"_error": smoke.CliError({"error": {
                "message": "stale epoch", "details": {"result": "stale_claim"},
            }}, "")}
        if args[:2] == ("task", "close"):
            sessions[:] = [replacement]
            return {"success": True, "next": {
                "result": "drain_requested", "session": {"claims": 1},
            }}
        return {"success": True}

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "idle_worker", lambda: worker)
    monkeypatch.setattr(smoke, "pool_sessions", lambda: list(sessions))
    monkeypatch.setattr(smoke, "_open_pool_tasks", lambda: [
        {"id": "s1-1", "title": "S1 worker task 1"},
        {"id": "s1-2", "title": "S1 worker task 2"},
        {"id": "unrelated", "title": "unrelated fixture"},
    ])
    monkeypatch.setattr(smoke, "task_show", lambda task_id: {
        "id": task_id, "status": "COMPLETED" if not worker.task_id else "READY",
        "profile_id": smoke.POOL_PROFILE, "route_source": "router", "is_blocked": False,
    })
    monkeypatch.setattr(smoke, "api_checked", lambda command, args: {
        "session": {"state": "stopped", "end_reason": "drained"},
    })
    monkeypatch.setattr(smoke, "_swarm_checks", lambda: {
        "pools.orphan_agents": {"severity": "ok"},
    })
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))
    monkeypatch.setattr(smoke, "CONVERGE_TIMEOUT", 1)
    monkeypatch.setattr(smoke, "wait_for", partial(smoke.wait_for, interval=0.25))
    monkeypatch.setattr(smoke, "wait_for_pool_session", lambda predicate, **kwargs: predicate())
    # The replacement must appear only after close to exercise its identity check.
    sessions.pop()
    return smoke, responses, calls, clock


@pytest.mark.parametrize("empty_attempts", [0, 2])
def test_s2_retries_empty_claims_then_proves_fencing_and_retirement(s2_surfaces, empty_attempts):
    smoke, responses, calls, _clock = s2_surfaces
    responses[:0] = [{"result": "no_ready_work"}] * empty_attempts

    result = smoke.s2_claim_loop({})

    assert "claimed 1/1 then drain_requested; holder retired, replaced by replacement" == result
    assert sum(args[:2] == ("task", "claim") for args, _ in calls) == empty_attempts + 1
    heartbeats = [args for args, _ in calls if args[:2] == ("task", "heartbeat")]
    assert heartbeats == [
        ("task", "heartbeat", "--claim-epoch", "48"),
        ("task", "heartbeat", "--claim-epoch", "7"),
    ]
    assert any(args[:2] == ("task", "close") and "--claim-next" in args for args, _ in calls)


@pytest.mark.parametrize("result", ["prepare_failed", "drain_requested", "not_admissible"])
def test_s2_does_not_retry_other_claim_failures(s2_surfaces, result):
    smoke, responses, calls, clock = s2_surfaces
    responses[:] = [{"result": result}]

    with pytest.raises(smoke.Failure, match=result):
        smoke.s2_claim_loop({})

    assert len(calls) == 1
    assert clock.now == 0


def test_s2_rejects_a_claim_outside_its_s1_fixtures(s2_surfaces):
    smoke, responses, calls, _clock = s2_surfaces
    responses[0]["task"]["id"] = "unrelated"

    with pytest.raises(smoke.Failure, match="s1-1.*s1-2.*unrelated"):
        smoke.s2_claim_loop({})

    assert len(calls) == 1


def test_s2_empty_claim_timeout_reports_every_fixture(s2_surfaces):
    smoke, responses, calls, clock = s2_surfaces
    responses[:] = [{"result": "no_ready_work", "session": {"id": "holder", "claims": 0}}]

    with pytest.raises(smoke.Failure, match="timed out after 1s") as error:
        smoke.s2_claim_loop({})

    assert clock.now == 1
    assert all(args[:2] == ("task", "claim") for args, _ in calls)
    for detail in ("s1-1", "s1-2", "no_ready_work", "holder", "router"):
        assert detail in str(error.value)


@pytest.fixture
def s5_surfaces(monkeypatch):
    smoke = _load_smoke()
    holder = smoke.Worker(session_id="holder", token="holder-token")
    intruder = smoke.Worker(session_id="intruder", token="intruder-token")
    responses = [{"result": "claimed", "task": {"id": "holder-task"}, "claim_epoch": 7}]
    calls = []

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("task", "claim"):
            return responses.pop(0) if len(responses) > 1 else responses[0]
        if args[:2] == ("task", "heartbeat") or args[0] == "prime":
            return {"_error": smoke.CliError({"error": {
                "message": "out of scope", "details": {"result": "out_of_scope"},
            }}, "")}
        return {"success": True}

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "fresh_workers", lambda _count: [holder, intruder])
    monkeypatch.setattr(smoke, "create_task", lambda _title, **kwargs:
                        "foreign-task" if kwargs.get("project_id") == smoke.OTHER_PROJECT
                        else "holder-task")
    monkeypatch.setattr(smoke, "task_show", lambda task_id: {
        "id": task_id, "status": "READY", "profile_id": smoke.POOL_PROFILE,
        "route_source": "router", "is_blocked": False,
    })
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))
    monkeypatch.setattr(smoke, "CONVERGE_TIMEOUT", 1)
    monkeypatch.setattr(smoke, "wait_for", partial(smoke.wait_for, interval=0.25))
    return smoke, responses, calls, clock


@pytest.mark.parametrize("empty_attempts", [0, 2])
def test_s5_waits_for_its_fixture_then_checks_both_scope_refusals(s5_surfaces, empty_attempts):
    smoke, responses, calls, _clock = s5_surfaces
    responses[:0] = [{"result": "no_ready_work"}] * empty_attempts

    result = smoke.s5_fence_and_scope({})

    assert "cross-session heartbeat and cross-project prime both refused" in result
    assert sum(args[:2] == ("task", "claim") for args, _ in calls) == empty_attempts + 1
    heartbeat = next((args, kwargs) for args, kwargs in calls
                     if args[:2] == ("task", "heartbeat"))
    assert heartbeat == (("task", "heartbeat", "holder-task", "--claim-epoch", "7"), {
        "token": "intruder-token", "session_id": "intruder", "check_ok": False,
    })
    prime = next((args, kwargs) for args, kwargs in calls if args[0] == "prime")
    assert prime == (("prime", "--task-id", "foreign-task"), {
        "token": "holder-token", "session_id": "holder", "check_ok": False,
    })
    assert any(args[:2] == ("task", "close") for args, _ in calls)


@pytest.mark.parametrize("result", ["prepare_failed", "drain_requested", "not_admissible"])
def test_s5_does_not_retry_other_claim_failures(s5_surfaces, result):
    smoke, responses, calls, clock = s5_surfaces
    responses[:] = [{"result": result}]

    with pytest.raises(smoke.Failure, match=result):
        smoke.s5_fence_and_scope({})

    assert len(calls) == 1
    assert clock.now == 0


def test_s5_rejects_a_claim_of_a_different_task(s5_surfaces):
    smoke, responses, calls, _clock = s5_surfaces
    responses[0]["task"]["id"] = "leftover-task"

    with pytest.raises(smoke.Failure, match="holder-task.*leftover-task"):
        smoke.s5_fence_and_scope({})

    assert len(calls) == 1


def test_s5_claim_wait_is_bounded_and_reports_fixture_and_last_claim(s5_surfaces):
    smoke, responses, calls, clock = s5_surfaces
    responses[:] = [{"result": "no_ready_work", "session": {"id": "holder", "claims": 0}}]

    with pytest.raises(smoke.Failure, match="timed out after 1s") as error:
        smoke.s5_fence_and_scope({})

    assert clock.now == 1
    assert all(args[:2] == ("task", "claim") for args, _ in calls)
    for detail in ("holder-task", "no_ready_work", "holder", "READY", "router"):
        assert detail in str(error.value)


@pytest.fixture
def s18_surfaces(monkeypatch):
    smoke = _load_smoke()
    worker = smoke.Worker(session_id="triage-worker", token="triage-token")
    responses = [{"result": "claimed", "task": {"id": "triage-task"}, "claim_epoch": 7}]
    calls = []

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("task", "claim"):
            return responses.pop(0) if len(responses) > 1 else responses[0]
        return {"success": True}

    def fake_api(command, args):
        if command == "list_playbook_runs":
            return {"runs": [{"run_id": "triage-run"}]}
        if command == "inspect_playbook_run":
            return {"run": {
                "run_id": "triage-run", "event": {"task_id": "triage-task"},
                "lifecycle": "completed",
            }}
        if command == "message_list":
            assert args["to_id"] == f"supervisor-{smoke.PROJECT}"
            return {"messages": [{"id": "triage-notice", "body": "Failed triage-task"}]}
        raise AssertionError(command)

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "api_checked", fake_api)
    monkeypatch.setattr(smoke, "fresh_workers", lambda _count: [worker])
    monkeypatch.setattr(smoke, "create_task", lambda *_args, **_kwargs: "triage-task")
    monkeypatch.setattr(smoke, "task_show", lambda task_id: {
        "id": task_id,
        "status": "BLOCKED" if any(args[:2] == ("task", "close") for args, _ in calls)
        else "READY",
        "profile_id": smoke.POOL_PROFILE, "route_source": "router", "is_blocked": False,
    })
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        time=lambda: 100.0,
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))
    monkeypatch.setattr(smoke, "CONVERGE_TIMEOUT", 1)
    monkeypatch.setattr(smoke, "wait_for", partial(smoke.wait_for, interval=0.25))
    return smoke, responses, calls, clock


@pytest.mark.parametrize("empty_attempts", [0, 2])
def test_s18_retries_empty_claims_then_verifies_failure_triage(s18_surfaces, empty_attempts):
    smoke, responses, calls, _clock = s18_surfaces
    responses[:0] = [{"result": "no_ready_work"}] * empty_attempts

    result = smoke.s18_supervisor_failure_triage({})

    assert "triage run triage-run" in result and "notice triage-notice" in result
    assert sum(args[:2] == ("task", "claim") for args, _ in calls) == empty_attempts + 1
    close_args, close_kwargs = next((args, kwargs) for args, kwargs in calls
                                   if args[:2] == ("task", "close"))
    for flag, expected in (("--outcome", "fail"), ("--failure-class", "hard"),
                           ("--work-outcome", "abandoned"), ("--claim-epoch", "7")):
        assert close_args[close_args.index(flag) + 1] == expected
    assert close_kwargs["token"] == "triage-token"
    assert any(args[:2] == ("session", "drain-ack") for args, _ in calls)
    assert any(args[:2] == ("task", "delete") for args, _ in calls)


@pytest.mark.parametrize("result", ["prepare_failed", "drain_requested", "not_admissible"])
def test_s18_does_not_retry_other_claim_failures(s18_surfaces, result):
    smoke, responses, calls, clock = s18_surfaces
    responses[:] = [{"result": result}]

    with pytest.raises(smoke.Failure, match=result):
        smoke.s18_supervisor_failure_triage({})

    assert len(calls) == 1
    assert clock.now == 0


def test_s18_rejects_a_claim_of_a_different_task(s18_surfaces):
    smoke, responses, calls, _clock = s18_surfaces
    responses[0]["task"]["id"] = "leftover-task"

    with pytest.raises(smoke.Failure, match="triage-task.*leftover-task"):
        smoke.s18_supervisor_failure_triage({})

    assert len(calls) == 1


def test_s18_claim_wait_is_bounded_and_reports_fixture_and_last_claim(s18_surfaces):
    smoke, responses, calls, clock = s18_surfaces
    responses[:] = [{"result": "no_ready_work", "session": {"id": "triage-worker"}}]

    with pytest.raises(smoke.Failure, match="timed out after 1s") as error:
        smoke.s18_supervisor_failure_triage({})

    assert clock.now == 1
    assert all(args[:2] == ("task", "claim") for args, _ in calls)
    for detail in ("triage-task", "no_ready_work", "triage-worker", "READY", "router"):
        assert detail in str(error.value)


@pytest.fixture(params=["std", "solo"])
def s16_canary_surfaces(monkeypatch, request):
    """Exercise S16's probation-canary claim without a live daemon."""
    smoke = _load_smoke()
    profile = smoke.STD_A if request.param == "std" else smoke.SOLO_A
    expected = "pinned-task" if request.param == "std" else "solo-task"
    responses = [{"result": "claimed", "task": {"id": expected}, "claim_epoch": 7}]
    calls = []

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("session", "token"):
            return {"token": "canary-token"}
        if args[:2] == ("task", "claim"):
            return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(args)

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "live_sessions_for", lambda profile_id: (
        [{"id": "canary", "profile_id": profile}] if profile_id == profile else []
    ))
    monkeypatch.setattr(smoke, "task_show", lambda task_id: {
        "id": task_id, "status": "READY", "profile_id": profile,
        "route_source": "pin", "is_blocked": False,
    })
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))
    monkeypatch.setattr(smoke, "CONVERGE_TIMEOUT", 1)
    monkeypatch.setattr(smoke, "wait_for", partial(smoke.wait_for, interval=0.25))
    return smoke, responses, calls, clock, profile, expected


def _claims(calls):
    return [args for args, _ in calls if args[:2] == ("task", "claim")]


@pytest.mark.parametrize("empty_attempts", [0, 2])
def test_s16_canary_retries_empty_claims_then_holds_its_pools_task(
    s16_canary_surfaces, empty_attempts,
):
    smoke, responses, calls, _clock, profile, expected = s16_canary_surfaces
    responses[:0] = [{"result": "no_ready_work"}] * empty_attempts

    worker, canary, held = smoke._claim_probation_canary("pinned-task", "solo-task")

    assert (canary["id"], canary["profile_id"], held) == ("canary", profile, expected)
    assert (worker.session_id, worker.token) == ("canary", "canary-token")
    assert (worker.task_id, worker.claim_epoch) == (expected, 7)
    assert len(_claims(calls)) == empty_attempts + 1
    assert all(kwargs["token"] == "canary-token" for args, kwargs in calls
               if args[:2] == ("task", "claim"))


@pytest.mark.parametrize("result", ["prepare_failed", "drain_requested", "not_admissible"])
def test_s16_canary_does_not_retry_other_claim_failures(s16_canary_surfaces, result):
    smoke, responses, calls, clock, _profile, _expected = s16_canary_surfaces
    responses[:] = [{"result": result}]

    with pytest.raises(smoke.Failure, match=result):
        smoke._claim_probation_canary("pinned-task", "solo-task")

    assert len(_claims(calls)) == 1
    assert clock.now == 0


def test_s16_canary_rejects_the_other_pools_held_task(s16_canary_surfaces):
    smoke, responses, calls, _clock, _profile, expected = s16_canary_surfaces
    other = "solo-task" if expected == "pinned-task" else "pinned-task"
    responses[0]["task"]["id"] = other

    with pytest.raises(smoke.Failure, match=f"{expected}.*{other}"):
        smoke._claim_probation_canary("pinned-task", "solo-task")

    assert len(_claims(calls)) == 1


def test_s16_canary_claim_wait_is_bounded_and_reports_fixture_and_last_claim(
    s16_canary_surfaces,
):
    smoke, responses, calls, clock, profile, expected = s16_canary_surfaces
    # The shape of the CI failure this retry absorbs (run 37983264206).
    responses[:] = [{"result": "no_ready_work", "session": {
        "id": "canary", "claims": 0, "claim_phase": "claiming",
    }}]

    with pytest.raises(smoke.Failure, match="timed out after 1s") as error:
        smoke._claim_probation_canary("pinned-task", "solo-task")

    assert clock.now == 1
    assert len(_claims(calls)) > 1
    for detail in (expected, "no_ready_work", "claim_phase", "READY", profile, "pin"):
        assert detail in str(error.value)


@pytest.fixture
def s19_claim_surfaces(monkeypatch, tmp_path):
    """Exercise both S19 claims up to the checklist, without a live daemon."""
    smoke = _load_smoke()
    planner = smoke.Worker(session_id="planner", token="planner-token")
    worker = smoke.Worker(session_id="child-worker", token="child-token")
    workers = {"planner": planner, "child": worker}
    responses = {
        stage: [{"result": "claimed", "task": {"id": f"{stage}-task"}, "claim_epoch": 7}]
        for stage in workers
    }
    claims = []
    created = False

    def refusal(message):
        return {"_error": smoke.CliError({"error": {"message": message}}, "")}

    def fake_aq(*args, **kwargs):
        nonlocal created
        if args[:2] == ("task", "claim"):
            stage = "planner" if kwargs["token"] == planner.token else "child"
            claims.append(stage)
            replies = responses[stage]
            return replies.pop(0) if len(replies) > 1 else replies[0]
        if args[:2] == ("task", "deps"):
            return {"provenance": [{"id": "planner-task", "dep_type": "discovered-from"}]}
        assert args[:2] == ("task", "create")
        if "--parent" in args:
            return refusal("hierarchy.parent_out_of_scope")
        if smoke.OTHER_PROJECT in args:
            return refusal("project_id mismatch")
        if "--root" in args:
            return refusal("graph.root_needs_parent")
        if "--dry-run" in args:
            return {"parent_id": "planner-task"}
        created = True
        # Model an immediate router decision and scheduler assignment. The
        # production override must still refuse a child that already started.
        clock.child_running = not clock.scheduling_paused
        return {"created": True, "request_id": "graph-request",
                "nodes": [{"task_id": "child-task"}]}

    def fake_api(command, _args, **_kwargs):
        if command == "set_project_constraint":
            assert _args == {"project_id": smoke.PROJECT, "pause_scheduling": True}
            assert not _kwargs.get("token")
            clock.scheduling_paused = True
            clock.scheduling_events.append("pause")
            return {"constraint_set": True}
        if command == "release_project_constraint":
            assert _args == {"project_id": smoke.PROJECT, "fields": ["pause_scheduling"]}
            assert not _kwargs.get("token")
            clock.scheduling_paused = False
            clock.scheduling_events.append("release")
            return {"constraint_released": True}
        if command == "task_children":
            return {"count": int(created)}
        assert command == "create_task_graph"
        return {"code": "graph.root_needs_parent"}

    def fake_override(task_id, profile, intelligence_class):
        assert (task_id, profile, intelligence_class) == (
            "child-task", smoke.POOL_PROFILE, smoke.POOL_CLASS,
        )
        if clock.child_running:
            raise smoke.Failure("routing.not_routable: child already started")
        clock.scheduling_events.append("override")

    class ChecklistReached(Exception):
        pass

    def stop_at_checklist(*args, **_kwargs):
        assert args == ("prime",)
        raise ChecklistReached

    monkeypatch.setenv("AQ_E2E_HOME", str(tmp_path))
    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "api", fake_api)
    monkeypatch.setattr(smoke, "fresh_workers", lambda _count: [worker])
    monkeypatch.setattr(smoke, "create_task", lambda _title, **kwargs:
                        "foreign-task" if kwargs.get("project_id") == smoke.OTHER_PROJECT
                        else "planner-task")
    monkeypatch.setattr(smoke, "wait_for_pool_session", lambda *_args, **_kwargs:
                        {"id": planner.session_id})
    monkeypatch.setattr(smoke.Worker, "adopt", lambda _session_id: planner)
    monkeypatch.setattr(smoke, "task_show", lambda task_id: {
        "id": task_id, "status": "READY", "parent_task_id": "planner-task",
        "profile_id": smoke.PLANNER_PROFILE if task_id == "planner-task" else smoke.POOL_PROFILE,
        "route_source": "router", "is_blocked": False,
    })
    monkeypatch.setattr(smoke, "override_route", fake_override)
    monkeypatch.setattr(smoke, "run_aq", stop_at_checklist)
    clock = SimpleNamespace(
        now=0.0, scheduling_paused=False, child_running=False, scheduling_events=[],
    )
    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))
    monkeypatch.setattr(smoke, "CONVERGE_TIMEOUT", 1)
    monkeypatch.setattr(smoke, "wait_for", partial(smoke.wait_for, interval=0.25))
    return smoke, workers, responses, claims, clock, ChecklistReached


def test_s19_routes_child_before_scheduler_can_assign(s19_claim_surfaces):
    smoke, _workers, _responses, claims, clock, reached = s19_claim_surfaces

    with pytest.raises(reached):
        smoke.s19_scoped_planner_graph({})

    assert clock.scheduling_events == ["pause", "override", "release"]
    assert not clock.scheduling_paused and not clock.child_running
    assert claims == ["planner", "child"]


@pytest.mark.parametrize("stage", ["create", "override"])
def test_s19_releases_scheduling_pause_after_graph_setup_failure(
    s19_claim_surfaces, monkeypatch, stage
):
    smoke, _workers, _responses, claims, clock, _reached = s19_claim_surfaces

    def fail(*_args, **_kwargs):
        raise RuntimeError("graph setup failed")

    if stage == "create":
        original = smoke.aq

        def fail_create(*args, **kwargs):
            if args[:2] == ("task", "create") and "--dry-run" not in args:
                return fail()
            return original(*args, **kwargs)

        monkeypatch.setattr(smoke, "aq", fail_create)
    else:
        monkeypatch.setattr(smoke, "override_route", fail)

    with pytest.raises(RuntimeError, match="graph setup failed"):
        smoke.s19_scoped_planner_graph({})

    assert clock.scheduling_events == ["pause", "release"]
    assert not clock.scheduling_paused
    assert claims == ["planner"]


@pytest.mark.parametrize("stage", ["planner", "child"])
@pytest.mark.parametrize("empty_attempts", [0, 2])
def test_s19_retries_empty_claims_before_checking_the_graph_checklist(
    s19_claim_surfaces, stage, empty_attempts
):
    smoke, workers, responses, claims, _clock, reached = s19_claim_surfaces
    responses[stage][:0] = [{"result": "no_ready_work"}] * empty_attempts

    with pytest.raises(reached):
        smoke.s19_scoped_planner_graph({})

    assert claims.count(stage) == empty_attempts + 1
    for name, worker in workers.items():
        assert worker.task_id == f"{name}-task"
        assert worker.claim_epoch == 7


@pytest.mark.parametrize("stage", ["planner", "child"])
@pytest.mark.parametrize("result", ["prepare_failed", "drain_requested", "not_admissible"])
def test_s19_does_not_retry_other_claim_failures(s19_claim_surfaces, stage, result):
    smoke, _workers, responses, claims, clock, _reached = s19_claim_surfaces
    responses[stage][:] = [{"result": result}]

    with pytest.raises(smoke.Failure, match=result):
        smoke.s19_scoped_planner_graph({})

    assert claims.count(stage) == 1
    assert clock.now == 0


@pytest.mark.parametrize("stage", ["planner", "child"])
def test_s19_rejects_a_claim_of_a_different_task(s19_claim_surfaces, stage):
    smoke, _workers, responses, claims, _clock, _reached = s19_claim_surfaces
    responses[stage][0]["task"]["id"] = "leftover-task"

    with pytest.raises(smoke.Failure, match="leftover-task") as error:
        smoke.s19_scoped_planner_graph({})

    assert f"{stage}-task" in str(error.value)
    assert claims.count(stage) == 1


@pytest.mark.parametrize("stage", ["planner", "child"])
def test_s19_claim_wait_is_bounded_and_reports_fixture_and_last_claim(s19_claim_surfaces, stage):
    smoke, workers, responses, claims, clock, _reached = s19_claim_surfaces
    responses[stage][:] = [{"result": "no_ready_work", "session": {"id": workers[stage].session_id}}]

    with pytest.raises(smoke.Failure, match="timed out after 1s") as error:
        smoke.s19_scoped_planner_graph({})

    assert clock.now == 1
    assert claims.count(stage) > 1
    for detail in (f"{stage}-task", "no_ready_work", workers[stage].session_id, "READY", "router"):
        assert detail in str(error.value)


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


@pytest.mark.parametrize("as_worker", [False, True])
def test_cli_subprocesses_do_not_inherit_the_callers_task_or_claim(monkeypatch, as_worker):
    smoke = _load_smoke()
    caller = {
        "AQ_API_TOKEN": "outer-token",
        "AQ_SESSION_ID": "outer-session",
        "AQ_TASK_ID": "outer-task",
        "AQ_CLAIM_EPOCH": "73",
    }
    for name, value in caller.items():
        monkeypatch.setenv(name, value)

    env = smoke._cli_env(
        token="fixture-token" if as_worker else None,
        session_id="fixture-session" if as_worker else None,
    )

    assert "AQ_TASK_ID" not in env
    assert "AQ_CLAIM_EPOCH" not in env
    if as_worker:
        assert env["AQ_API_TOKEN"] == "fixture-token"
        assert env["AQ_SESSION_ID"] == "fixture-session"
    else:
        assert "AQ_API_TOKEN" not in env
        assert "AQ_SESSION_ID" not in env
    assert {name: os.environ[name] for name in caller} == caller


def test_collection_rows_accepts_versioned_envelope_data_and_legacy_wrappers():
    smoke = _load_smoke()
    rows = [{"id": "one"}, {"id": "two"}]

    assert smoke.collection_rows(rows, "workspaces") == rows
    assert smoke.collection_rows({"workspaces": rows}, "workspaces") == rows


def test_fresh_workers_quiesces_every_project_in_the_global_profile(monkeypatch):
    smoke = _load_smoke()
    open_tasks = {
        smoke.PROJECT: {"e2e-leftover"},
        smoke.OTHER_PROJECT: {"other-leftover"},
    }
    live = [
        {"id": "s-e2e", "project_id": smoke.PROJECT, "state": "running"},
        {"id": "s-other", "project_id": smoke.OTHER_PROJECT, "state": "running"},
        {"id": "s-draining", "project_id": smoke.PROJECT, "state": "draining"},
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

    def fake_api(command, args):
        if command == "session_kill":
            return fake_aq("session", "kill", args["session_id"])
        if command == "delete_task":
            assert args["cascade"] is True
            return fake_aq("task", "delete", "--task-id", args["task_id"])
        if command == "session_list":
            assert args == {"lifecycle": "pool"}
            return {"sessions": list(live)}
        assert command == "list_tasks"
        return {"tasks": [{"id": task_id} for task_id in sorted(open_tasks[args["project_id"]])]}

    def immediate_wait(predicate, **_kwargs):
        for _ in range(10):
            value = predicate()
            if value:
                return value
        raise AssertionError("predicate did not converge")

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "api", fake_api)
    monkeypatch.setattr(smoke, "create_task", fake_create)
    monkeypatch.setattr(smoke, "wait_for", immediate_wait)

    workers = smoke.fresh_workers(2)

    assert [worker.session_id for worker in workers] == ["fresh-1", "fresh-2"]
    assert "other-leftover" not in open_tasks[smoke.OTHER_PROJECT]
    assert not any(row["id"] == "s-draining" for row in live)


@pytest.fixture
def s1_surfaces(monkeypatch):
    smoke = _load_smoke()
    observed = SimpleNamespace(now=0.0, stages=[], session_reads=0, audit_reads=0)

    def stage():
        return observed.stages[min(int(observed.now / 2), len(observed.stages) - 1)]

    def pool_row():
        idle, starting, _sessions, _audit = stage()
        return {"max_active": 2, "ready": 3, "running_idle": idle,
                "running_busy": 0, "starting": starting}

    def pool_sessions():
        observed.session_reads += 1
        return [{"id": f"session-{n}"} for n in range(stage()[2])]

    def api(command, args):
        assert command == "get_recent_events"
        observed.audit_reads += 1
        return {"events": [{"project_id": smoke.PROJECT, "payload": "start 2 worker"}]
                if stage()[3] else []}

    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=lambda: observed.now,
        sleep=lambda seconds: setattr(observed, "now", observed.now + seconds),
        time=lambda: observed.now,
    ))
    monkeypatch.setattr(smoke, "create_task", lambda title, **kwargs: title)
    monkeypatch.setattr(smoke, "pool_row", pool_row)
    monkeypatch.setattr(smoke, "pool_sessions", pool_sessions)
    monkeypatch.setattr(smoke, "api", api)
    monkeypatch.setattr(smoke, "pool_wait_state", lambda *_: {"enabled": False})
    original_wait = smoke.wait_for_pool_session
    monkeypatch.setattr(smoke, "wait_for_pool_session", lambda predicate, **kwargs:
                        original_wait(predicate, **kwargs, timeout=8))
    return smoke, observed


def test_s1_waits_for_reserved_launches_session_rows_and_scale_audit(s1_surfaces):
    smoke, observed = s1_surfaces
    # Reservations already fill max_active before either durable row exists.
    # The scale audit may also lag the second row while its launch finishes.
    observed.stages = [(0, 2, 0, False), (1, 1, 1, False),
                       (2, 0, 2, False), (2, 0, 2, True)]
    state = {}

    result = smoke.s1_pool_sizing(state)

    assert observed.now == 6
    assert observed.session_reads == 4
    assert observed.audit_reads == 2
    assert len(state["s1_tasks"]) == 3
    assert "2 sessions for 3 ready tasks" in result


@pytest.mark.parametrize("stage, message", [
    ((2, 1, 2, True), "pool supply exceeded max_active=2: saw 3"),
    ((0, 0, 3, True), "live pool sessions exceeded max_active=2: saw 3"),
])
def test_s1_rejects_oversubscription_without_waiting(s1_surfaces, stage, message):
    smoke, observed = s1_surfaces
    observed.stages = [stage]

    with pytest.raises(smoke.Failure, match=message):
        smoke.s1_pool_sizing({})

    assert observed.now == 0


@pytest.mark.parametrize("stage", [(1, 1, 1, False), (2, 0, 2, False)])
def test_s1_requires_both_durable_sessions_and_scale_audit(s1_surfaces, stage):
    smoke, observed = s1_surfaces
    observed.stages = [stage]

    with pytest.raises(smoke.Failure, match="timed out after 8s"):
        smoke.s1_pool_sizing({})


def test_pool_wait_extends_once_while_daemon_has_unplaced_demand(monkeypatch):
    smoke = _load_smoke()
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        smoke, "time", SimpleNamespace(
            monotonic=lambda: clock.now,
            sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
            time=lambda: clock.now,
        ),
    )
    monkeypatch.setattr(smoke, "pool_wait_state", lambda *_: {
        "project": smoke.PROJECT,
        "profile": smoke.POOL_PROFILE,
        "enabled": True,
        "ready": 1,
        "desired": 1,
        "running_idle": 0,
        "running_busy": 0,
        "starting": 0,
        "placement": {"ready": 1, "workspace_capacity": 1},
    })

    session = smoke.wait_for_pool_session(
        lambda: {"id": "new"} if clock.now >= 1.5 else None,
        what="a session", timeout=1,
    )

    assert session == {"id": "new"}
    assert clock.now == 2


def test_pool_wait_reports_placement_blocker_without_extending(monkeypatch):
    smoke = _load_smoke()
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        smoke, "time", SimpleNamespace(
            monotonic=lambda: clock.now,
            sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
            time=lambda: clock.now,
        ),
    )
    monkeypatch.setattr(smoke, "pool_wait_state", lambda *_: {
        "project": smoke.PROJECT,
        "profile": smoke.POOL_PROFILE,
        "enabled": True,
        "ready": 1,
        "desired": 1,
        "running_idle": 0,
        "running_busy": 0,
        "starting": 0,
        "placement": {"ready": 1, "workspace_capacity": 5,
                      "quarantined_until": 600,
                      "quarantined_reason": "rapid crash"},
        "instances": [],
    })

    with pytest.raises(smoke.Failure, match="timed out after 1s") as error:
        smoke.wait_for_pool_session(lambda: None, what="a session", timeout=1)

    assert clock.now == 1
    assert '"workspace_capacity": 5' in str(error.value)
    assert "rapid crash" in str(error.value)


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

    # S17 covers the phased development graph; S18 runs the command-only
    # durable failure-triage path; and S19 exercises planner-scoped graph
    # filing under the fake provider; S20 proves ordinary train delivery.
    assert set(by_key) == {f"S{number}" for number in range(1, 21)}
    assert by_key["S9"].families == ("task CRUD/rollback",)
    assert set(by_key["S10"].families) == {"workspace CRUD", "file/git/note CRUD"}
    assert by_key["S11"].families == ("message CRUD",)
    assert by_key["S12"].families == ("MCP registry CRUD",)
    assert by_key["S13"].families == ("plugin extension startup",)
    assert by_key["S14"].families == ("graph/vault",)
    assert by_key["S15"].families == ("integration/CLI",)
    assert by_key["S16"].families == ("provider availability/failover",)
    assert by_key["S17"].families == ("task graph/phases/subtasks",)
    assert by_key["S18"].families == ("playbooks/failure triage",)
    assert by_key["S19"].families == ("authentication/scoped graph/quota",)
    assert by_key["S20"].families == ("integration/train/provenance",)


def test_ci_scenario_groups_cover_every_scenario_once():
    smoke = _load_smoke()
    grouped = [key for group in SCENARIO_GROUPS.values() for key in group]

    assert len(SCENARIO_GROUPS) == 4
    assert len(grouped) == len(set(grouped))
    assert set(grouped) == (
        {scenario.key for scenario in smoke.SCENARIOS} - {"S16"}
    ) | {phase.key for phase in smoke.FAILOVER_PHASES}
    assert {phase.key for phase in smoke.FAILOVER_PHASES} == {"S16a", "S16b"}


def test_s16_pauses_automatic_failover_while_it_drives_manual_sweeps(monkeypatch):
    smoke = _load_smoke()
    calls: list[tuple[str, ...]] = []
    restored: list[bool] = []

    def fake_aq(*args, **_kwargs):
        calls.append(args)
        return {"enabled": args[-1] == "--enabled"}

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "_s16", lambda state: "manual sweep complete")
    monkeypatch.setattr(smoke, "_restore_providers", lambda: restored.append(True))

    assert smoke.s16_provider_failover({}) == "manual sweep complete"
    assert calls == [
        ("playbook", "set-enabled", "--playbook-id", "provider-failover", "--no-enabled"),
        ("playbook", "set-enabled", "--playbook-id", "provider-failover", "--enabled"),
    ]
    assert restored == [True]


def test_s16_logout_stops_late_and_draining_recovery_sessions_once(monkeypatch):
    smoke = _load_smoke()
    stopped = []
    snapshots = iter([
        [
            {"id": "recovery", "profile_id": smoke.STD_A},
            {"id": "draining", "profile_id": smoke.STD_B},
            {"id": "claude", "profile_id": smoke.POOL_PROFILE},
        ],
        [
            {"id": "recovery", "profile_id": smoke.STD_A},
            {"id": "late-launch", "profile_id": smoke.SOLO_A},
        ],
        [],
    ])

    def pool_sessions(project_id, *, include_draining):
        assert project_id is None and include_draining is True
        return next(snapshots)

    def kill(command, args):
        assert command == "session_kill"
        stopped.append(args["session_id"])
        return {"success": True}

    def provider(key):
        assert key == smoke.PROVA
        return {"state": "unauthenticated" if "late-launch" in stopped else "available"}

    def wait(predicate, *, what):
        assert "the second logout" in what
        assert predicate() is None
        assert predicate() is None
        return predicate()

    monkeypatch.setattr(smoke, "pool_sessions", pool_sessions)
    monkeypatch.setattr(smoke, "api_checked", kill)
    monkeypatch.setattr(smoke, "provider", provider)
    monkeypatch.setattr(smoke, "wait_for", wait)

    assert smoke._wait_for_failover_logout() == {"state": "unauthenticated"}
    assert stopped == ["recovery", "draining", "late-launch"]


def test_s16_logout_still_requires_provider_failure(monkeypatch):
    smoke = _load_smoke()
    bounded_wait = smoke.wait_for
    monkeypatch.setattr(smoke, "provider", lambda _key: {"state": "available"})
    monkeypatch.setattr(smoke, "pool_sessions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        smoke, "wait_for", lambda predicate, **kwargs: bounded_wait(predicate, timeout=0, **kwargs),
    )

    with pytest.raises(smoke.Failure, match="the second logout"):
        smoke._wait_for_failover_logout()


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
        check=False,
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
            check=False,
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


def test_scenario_selection_is_individual_and_keeps_declared_prerequisites(monkeypatch):
    from tests.test_e2e_cli_stateful import World

    world = World("claims", {})
    calls = []
    resets = []

    def run(argv, **_kwargs):
        keys = argv[1:]
        calls.append(keys)
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join(
            [*(f"PASS {key} scenario" for key in keys),
             f"{len(keys)}/{len(keys)} scenarios passed",
             "passed unsupported dependency-unavailable explicitly-untested"]))

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(world, "reset", lambda: resets.append(True))
    world.run("S2")
    world.run("S3")
    assert calls == [["S1", "S2"], ["S3"]]
    assert resets == [True]


@pytest.mark.parametrize("failure", ["assertion", "timeout"])
def test_scenario_failure_cleans_chain_and_rebuilds_prerequisites(monkeypatch, failure):
    from tests.test_e2e_cli_stateful import World

    world = World("claims", {})
    world.completed.add("S1")
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv[1:])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 270, output=b"PASS S1 (1s)")
        return SimpleNamespace(returncode=1, stdout="FAIL S2", stderr="failure")

    def reset():
        world.completed.clear()
        calls.append("reset")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(world, "reset", reset)
    with pytest.raises(AssertionError):
        world.run("S2")
    assert calls == [["S2"], "reset"]
    assert not world.completed


def test_world_reset_removes_terminal_tasks_sessions_and_restores_globals(monkeypatch):
    from tests.test_e2e_cli_stateful import World

    world = World("graphs", {})
    world.swarm = {"enabled": True, "max_starts_per_tick": 2, "scale_down_grace": 3600}
    smoke = world.smoke
    sessions = [{"id": "holding"}, {"id": "draining"}]
    tasks = [{"id": "completed", "status": "COMPLETED"},
             {"id": "failed", "status": "FAILED"}]
    calls = []
    modes = []

    def api(command, args):
        calls.append((command, args))
        if command == "session_list":
            assert args == {"live_only": True}
            return {"sessions": list(sessions)}
        if command == "session_kill":
            sessions[:] = [row for row in sessions if row["id"] != args["session_id"]]
        if command == "list_projects":
            return [{"id": "e2e"}]
        if command == "list_tasks":
            assert args["include_completed"]
            return list(tasks)
        if command == "delete_task":
            assert args["cascade"]
            tasks[:] = [row for row in tasks if row["id"] != args["task_id"]]
        return {"success": True}

    monkeypatch.setattr(smoke, "api", api)
    monkeypatch.setattr(smoke, "fake_script", lambda: modes.append("healthy"))
    monkeypatch.setattr(smoke, "wait_for", lambda fn, **_: fn() or fn())
    world.completed.add("S1")
    world.reset()
    assert not tasks and not sessions and not world.completed
    assert modes == ["healthy"]
    assert ("update_config", {"section": "swarm", "data": world.swarm}) in calls
    assert ("set_playbook_enabled", {"playbook_id": smoke.FAILOVER_PLAYBOOK, "enabled": True}) in calls
    assert {args["provider"] for cmd, args in calls if cmd == "provider_set_state"} == {
        "claude", "codex", smoke.PROVA, smoke.PROVB,
    }


def test_cleanup_failure_prevents_reusing_the_world(monkeypatch):
    from tests.test_e2e_cli_stateful import World

    world = World("graphs", {})
    monkeypatch.setattr(world.smoke, "wait_for", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(RuntimeError("cleanup failed")))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        world.reset()
    assert world.poisoned
    with pytest.raises(AssertionError, match="contaminated"):
        world.run("S18")


@pytest.mark.parametrize("fail_stage", [None, "build", "start", "scenario"])
def test_disposable_fixture_builds_once_and_cleans_even_after_failure(monkeypatch, tmp_path, fail_stage):
    from tests import test_e2e_cli_stateful as acceptance

    home = tmp_path / "world"
    env = acceptance.world_env(home)
    calls = []
    world = SimpleNamespace(timing=lambda _: None, cli_server=None)
    monkeypatch.setattr(acceptance, "World", lambda *_: world)
    monkeypatch.setattr(acceptance, "start_cli_server", lambda _: None)

    def run(argv, **_kwargs):
        script = Path(argv[0]).name
        calls.append(script)
        if script == "e2e-env.sh":
            home.mkdir()
            (home / ".aq-e2e").touch()
            (home / "config.yaml").write_text("swarm:\n  enabled: true\n")
        stage = {"e2e-env.sh": "build", "e2e-daemon.sh": "start"}.get(script)
        return SimpleNamespace(returncode=int(stage == fail_stage and stage is not None),
                               stdout="fixture", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    try:
        with acceptance.disposable_world("graphs", env) as fixture:
            assert fixture is world
            if fail_stage == "scenario":
                raise AssertionError("scenario failure")
    except AssertionError:
        assert fail_stage is not None
    assert calls.count("e2e-env.sh") == 1
    assert calls.count("e2e-clean.sh") == 1
    assert calls.count("e2e-daemon.sh") == int(fail_stage != "build")


def test_parallel_worlds_fence_inherited_e2e_targets(monkeypatch, tmp_path):
    from tests.test_e2e_cli_stateful import world_env

    for key in ("AQ_E2E_HOME", "AQ_E2E_API_URL", "E2E_DB_NAME", "E2E_FAKE_SCRIPT"):
        monkeypatch.setenv(key, "operator-owned")
    first = world_env(tmp_path / "one")
    second = world_env(tmp_path / "two")
    for key in ("AQ_E2E_HOME", "AQ_E2E_API_URL", "E2E_DB_NAME", "AQ_E2E_TMUX_SOCKET"):
        assert first[key] != second[key]
        assert first[key] != "operator-owned"
    assert "E2E_FAKE_SCRIPT" not in first


def test_s16_restoration_failure_is_a_failure_and_still_restores_policy(monkeypatch):
    smoke = _load_smoke()
    restored = []
    monkeypatch.setattr(smoke, "set_failover_policy_enabled", restored.append)
    monkeypatch.setattr(smoke, "_s16", lambda _: "passed")
    monkeypatch.setattr(smoke, "_restore_providers", lambda:
                        (_ for _ in ()).throw(RuntimeError("provider cleanup failed")))
    with pytest.raises(RuntimeError, match="provider cleanup failed"):
        smoke.s16_provider_failover({})
    assert restored == [False, True]


@pytest.mark.asyncio
async def test_local_train_transport_keeps_real_git_leases_and_disposable_scope(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    from scripts.e2e.daemon import install_local_git_transport
    from src.git.manager import GitError, GitManager
    from tests.test_integration_gitops import git as run_git

    home = tmp_path / "e2e"
    home.mkdir()
    (home / ".aq-e2e").touch()
    remote, checkout = home / "remote.git", home / "checkout"
    run_git(home, "init", "--bare", str(remote))
    run_git(home, "clone", str(remote), str(checkout))
    run_git(checkout, "config", "user.name", "AQ E2E")
    run_git(checkout, "config", "user.email", "e2e@example.test")
    run_git(checkout, "commit", "--allow-empty", "-m", "base")
    oid = run_git(checkout, "rev-parse", "HEAD")
    git = GitManager()
    resolver = AsyncMock(return_value="original")
    orch = SimpleNamespace(git=git, github_repository_binding_resolver=resolver)
    original_head, original_push = git.aremote_branch_head, git.apush_repository_oid
    restore = install_local_git_transport(orch, home)
    try:
        binding = await orch.github_repository_binding_resolver(SimpleNamespace(url=str(remote)))
        assert await git.aremote_branch_head(repository=binding, branch="main") is None
        await git.apush_repository_oid(
            str(checkout), repository=binding, tip_oid=oid, branch="main", expected_old_oid="0" * 40,
        )
        assert await git.aremote_branch_head(repository=binding, branch="main") == oid
        observed = await git.als_remote_ref(
            str(checkout), "main", repository_url=f"https://github.com/{binding.full_name}.git",
        )
        assert observed.oid == oid
        run_git(checkout, "commit", "--allow-empty", "-m", "next")
        next_oid = run_git(checkout, "rev-parse", "HEAD")
        with pytest.raises(GitError, match="authority expired"):
            await git.apush_repository_oid(
                str(checkout), repository=binding, tip_oid=next_oid, branch="main",
                expected_old_oid=oid, authority_deadline=asyncio.get_running_loop().time() - 1,
            )
        with pytest.raises(GitError):
            await git.apush_repository_oid(
                str(checkout), repository=binding, tip_oid=next_oid, branch="main",
                expected_old_oid="0" * 40,
            )
        assert await git.aremote_branch_head(repository=binding, branch="main") == oid
        outside = SimpleNamespace(url=str(tmp_path))
        assert await orch.github_repository_binding_resolver(outside) == "original"
        resolver.assert_awaited_once_with(outside)
    finally:
        restore()
    assert orch.github_repository_binding_resolver is resolver
    assert git.aremote_branch_head == original_head and git.apush_repository_oid == original_push
    with pytest.raises(ValueError, match="marked disposable"):
        install_local_git_transport(orch, tmp_path)


@pytest.mark.asyncio
async def test_negative_provider_assertion_observes_completed_scheduler_cycles(monkeypatch, tmp_path):
    from scripts.e2e.daemon import run_fake_cycles

    smoke = _load_smoke()
    monkeypatch.setenv("AQ_E2E_HOME", str(tmp_path))
    observed = []
    orch = SimpleNamespace(run_one_cycle=None)

    async def original():
        observed.append("cycle")

    async def scheduler(worker, _shutdown, *, interval):
        assert interval == 0.25
        for _ in range(3):
            await worker.run_one_cycle()

    orch.run_one_cycle = original
    await run_fake_cycles(scheduler, orch, None, 0.25)
    assert (tmp_path / "scheduler-cycles").read_text() == "3"
    assert orch.run_one_cycle is original

    def wait(predicate, **_kwargs):
        assert not predicate()
        (tmp_path / "scheduler-cycles").write_text("4")
        assert not predicate()
        (tmp_path / "scheduler-cycles").write_text("5")
        assert predicate()

    monkeypatch.setattr(smoke, "wait_for", wait)
    smoke.observe_scheduler_cycles(lambda: observed.append("assertion"))
    assert observed == ["cycle"] * 3 + ["assertion"] * 3


def test_preloaded_cli_keeps_output_exit_codes_and_environment_isolation(tmp_path):
    import signal
    from scripts.e2e.cli_server import request
    from tests.test_e2e_cli_stateful import World, start_cli_server, world_env

    env = world_env(tmp_path)
    world = World("probe", env)
    start_cli_server(world)
    try:
        for argv in (["--json", "schema"], ["--json", "task", "show"]):
            normal = subprocess.run([sys.executable, str(REPO_ROOT / "scripts/e2e/aq.py"), *argv],
                                    env={k: v for k, v in env.items() if k != "AQ_E2E_CLI_SOCKET"},
                                    capture_output=True, text=True, timeout=30)
            fast = request(env["AQ_E2E_CLI_SOCKET"], argv, env)
            assert fast["returncode"] == normal.returncode
            assert fast["stdout"] == normal.stdout
        legacy_env = {**env, "AQ_JSON_LEGACY": "1"}
        legacy = request(env["AQ_E2E_CLI_SOCKET"], ["--json", "schema"], legacy_env)
        fresh = request(env["AQ_E2E_CLI_SOCKET"], ["--json", "schema"], env)
        assert "data" not in json.loads(legacy["stdout"])
        assert "data" in json.loads(fresh["stdout"])
    finally:
        os.killpg(world.cli_server.pid, signal.SIGTERM)
        world.cli_server.wait(timeout=5)


@pytest.mark.parametrize("argv", [
    ["task", "claim", "--help"],
    ["task", "create", "--graph", "/definitely-missing.yaml"],
    ["e2e-fixture", "ping"],
])
def test_race_and_plugin_probes_bypass_cli_preloading(tmp_path, argv):
    env = {**os.environ, "AQ_E2E_CLI_SOCKET": str(tmp_path / "missing.sock")}
    result = subprocess.run([sys.executable, str(REPO_ROOT / "scripts/e2e/aq.py"), *argv],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode in (0, 2)
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("group", SCENARIO_GROUPS)
def test_pytest_collection_selects_only_the_requested_shard(group):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_e2e_cli_stateful.py", "--co", "-q",
         "-m", "integration", "-k", f"{group}-"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    nodes = [line for line in result.stdout.splitlines()
             if line.startswith("tests/test_e2e_cli_stateful.py::")]
    assert {line.rsplit("[", 1)[1].removesuffix("]") for line in nodes} == {
        f"{group}-{key}" for key in SCENARIO_GROUPS[group]
    }
