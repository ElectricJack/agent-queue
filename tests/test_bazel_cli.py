"""Bazel CLI selections and real shared admission/cancellation boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.cli.test_runner import _bazel_plan, _run_forwarding_signals
from src.resources.box_lock import BoxLock
from src.resources.semaphore import SlotSemaphore, full_suite_lock_dir
from src.test_selection.bazel_manifest import hydrate_manifest


@pytest.fixture(autouse=True)
def _pg_backend():
    """These tests exercise admission without allocating PostgreSQL."""


@pytest.fixture
def setup(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="agent-queue"\n[tool.pytest.ini_options]\ntestpaths=["tests"]\n'
    )
    tests = root / "tests"
    tests.mkdir()
    modules = {}
    nodes = []
    suites = {
        key: {"marker": marker, "modules": {}, "targets": []}
        for key, marker in (
            ("module", "not slow"),
            ("all_nodes", ""),
            ("slow", "slow"),
            ("cli", "not slow"),
        )
    }
    for index in range(6):
        path = f"tests/test_m{index}.py"
        (root / path).write_text("")
        normal, slow = len(nodes), len(nodes) + 1
        nodes.extend([{"markers": []}, {"markers": ["slow"]}])
        targets = {}
        for key, indices in (
            ("module", [normal]),
            ("all_nodes", [normal, slow]),
            ("slow", [slow]),
            ("cli", [normal]),
        ):
            if key == "cli" and index:
                continue
            target = f"//tests:m{index}__{key}"
            targets[key] = target
            suites[key]["modules"][path] = indices
            suites[key]["targets"].append(target)
        modules[path] = {
            "nodes": [normal, slow],
            "targets": targets,
            "target": targets["module"],
            "postgres": True,
        }
    directory = root / "tools/bazel"
    directory.mkdir(parents=True)
    catalogue = root / "tests/selection_catalogue.json"
    catalogue.write_text("{}")
    workflow = root / ".github/workflows/tests.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs: {}")
    (directory / "test_suites.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_marker": "not slow",
                "modules": modules,
                "suites": suites,
                "aliases": {"cli": ["cli"], "migration_slow": ["slow"]},
                "collection": {"nodes": nodes},
                "catalogue_sha256": hashlib.sha256(catalogue.read_bytes()).hexdigest(),
                "workflow_sha256": hashlib.sha256(workflow.read_bytes()).hexdigest(),
            }
        )
    )
    monkeypatch.chdir(root)
    lock_dir = tmp_path / "slots"
    monkeypatch.setattr("src.resources.semaphore.default_lock_dir", lambda _config: lock_dir)
    monkeypatch.setattr("src.cli.test_runner._load_config", lambda: None)
    monkeypatch.setattr("src.cli.test_runner._caps", lambda _r: (2, 2, "not slow", 0.05, 0))
    monkeypatch.delenv("POSTGRES_TEST_DSN", raising=False)
    for name in ("AQ_BAZEL_DB_SLOTS", "AQ_BAZEL_TEST_MEMORY_MB", "AQ_BAZEL_MEMORY_MB"):
        monkeypatch.delenv(name, raising=False)
    report = tmp_path / "report.jsonl"
    monkeypatch.setenv("AQ_TEST_SLOT_REPORT", str(report))
    calls = []

    def child(argv, *, env):
        calls.append((argv, env))
        assert SlotSemaphore(lock_dir, 2).snapshot()["free"] == 1
        assert BoxLock(lock_dir, 2).snapshot()["box"]["mode"] == "shared"
        return 0

    monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", child)
    return SimpleNamespace(
        root=root, locks=lock_dir, calls=calls, report=report, runner=CliRunner()
    )


def invoke(setup, *args):
    return setup.runner.invoke(cli, ["test", "--aq-backend", "bazel", *args])


@pytest.mark.parametrize("path", ["tests/test_m0.py", "./tests/test_m0.py"])
def test_module_selection_runs_admitted_without_host_postgres(setup, path):
    result = invoke(setup, path)
    assert result.exit_code == 0, result.output
    argv, env = setup.calls[0]
    assert argv[-1] == "//tests:m0__module"
    assert "--jobs=2" in argv and "--local_test_jobs=0" in argv
    assert "--default_test_resources=aq_pg=1" in argv
    assert "--local_resources=aq_pg=2" in argv
    assert "--default_test_resources=memory=1024" in argv
    assert "--local_resources=memory=2048" in argv
    assert "-n" not in argv
    assert env["HOME"] == os.environ["HOME"]  # launcher isolation occurs after host admission
    assert env["AQ_TEST_RUN_ID"]
    holder = json.loads(setup.report.read_text().splitlines()[0])
    assert holder["event"] == "acquired"
    assert SlotSemaphore(setup.locks, 2).snapshot()["free"] == 2


def test_explicit_marker_and_all_markers_map_exact_node_sets(setup):
    result = invoke(setup, "tests/test_m0.py", "-m", "slow")
    assert result.exit_code == 0, result.output
    assert setup.calls[-1][0][-1] == "//tests:m0__slow"
    result = invoke(setup, "--aq-all-markers", "tests/test_m0.py")
    assert result.exit_code == 0, result.output
    assert setup.calls[-1][0][-1] == "//tests:m0__all_nodes"


def version_two_document(setup):
    path = setup.root / "tools/bazel/test_suites.json"
    document = json.loads(path.read_text())
    inputs = ["src/a.py", "src/café.py", "src/a.py"]
    digest = hashlib.sha256(
        json.dumps(inputs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    for info in document["modules"].values():
        info["source_files"] = {"input_set": digest}
    document.update(schema_version=2, dependency_sets={digest: inputs})
    return document


@pytest.mark.parametrize(
    ("args", "all_markers"),
    [
        (("tests/test_m0.py",), False),
        (("tests/test_m0.py", "-m", "slow"), False),
        (("tests/test_m0.py",), True),
        (("tests/",), False),
        (("cli",), False),
        (("migration_slow",), False),
    ],
)
def test_version_two_manifest_preserves_exact_selection_and_input_values(setup, args, all_markers):
    expected = _bazel_plan(args, markers="not slow", all_markers=all_markers)
    document = version_two_document(setup)
    expanded = hydrate_manifest(document)
    assert expanded["schema_version"] == 1
    assert "dependency_sets" not in expanded
    for info in expanded["modules"].values():
        assert info["source_files"] == ["src/a.py", "src/café.py", "src/a.py"]
    # Decoding does not mutate the versioned document or its shared list table.
    assert document["schema_version"] == 2
    assert "input_set" in document["modules"]["tests/test_m0.py"]["source_files"]
    (setup.root / "tools/bazel/test_suites.json").write_text(json.dumps(document))

    assert _bazel_plan(args, markers="not slow", all_markers=all_markers) == expected


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_table", "unknown_reference", "missing_reference", "extra_reference_key",
        "inline_list", "nonstring_path", "wrong_digest", "invalid_digest",
        "invalid_module", "unknown_version",
    ],
)
def test_malformed_version_two_manifest_refuses_before_admission(setup, corruption):
    document = version_two_document(setup)
    digest = next(iter(document["dependency_sets"]))
    # Corrupt an unselected module too: all references must be validated before admission.
    entry = document["modules"]["tests/test_m5.py"]
    if corruption == "missing_table":
        del document["dependency_sets"]
    elif corruption == "unknown_reference":
        entry["source_files"] = {"input_set": "0" * 64}
    elif corruption == "missing_reference":
        entry["source_files"] = {}
    elif corruption == "extra_reference_key":
        entry["source_files"]["extra"] = digest
    elif corruption == "inline_list":
        entry["source_files"] = ["src/a.py"]
    elif corruption == "nonstring_path":
        document["dependency_sets"][digest] = [1]
    elif corruption == "wrong_digest":
        document["dependency_sets"][digest] = ["src/changed.py"]
    elif corruption == "invalid_digest":
        document["dependency_sets"]["not-a-sha256"] = []
    elif corruption == "invalid_module":
        document["modules"]["tests/test_m5.py"] = []
    elif corruption == "unknown_version":
        document["schema_version"] = 3
    (setup.root / "tools/bazel/test_suites.json").write_text(json.dumps(document))

    result = invoke(setup, "tests/test_m0.py")

    assert result.exit_code == 2, result.output
    assert "cannot read Bazel suite manifest" in result.output
    assert "Nothing was run" in result.output
    assert not setup.calls
    assert not setup.report.exists()


def test_version_one_manifest_remains_accepted_without_rewriting(setup):
    document = json.loads((setup.root / "tools/bazel/test_suites.json").read_text())
    assert hydrate_manifest(document) is document
    assert invoke(setup, "tests/test_m0.py").exit_code == 0


@pytest.mark.parametrize("name", ["cli", "//checks:cli"])
def test_manifest_suite_names_use_their_explicit_obligations(setup, name):
    assert invoke(setup, name).exit_code == 0
    assert setup.calls[0][0][-1] == "//tests:m0__cli"


@pytest.mark.parametrize(
    "args",
    [
        ("tests/test_m0.py", "-k", "foo"),
        ("tests/test_m0.py::test_x",),
        ("tests/", "--ignore=tests/test_m0.py"),
        ("tests/", "--deselect", "x"),
        ("tests/", "--lf"),
        ("tests/", "-n", "8"),
        ("tests/", "--jobs=100"),
        ("tests/", "-x"),
        ("tests/", "-q"),
        ("tests/", "-m", "unknown(foo=1)"),
        ("tests/", "--aq-smart=enforce"),
        ("tests/", "--aq-smart-enforce"),
        ("cli", "-m", "slow"),
        ("cli", "--aq-all-markers"),
        ("cli", "tests/test_m0.py"),
        ("unknown-suite",),
        ("tests/nope.py",),
        (),
    ],
)
def test_unsupported_selections_refuse_before_admission_with_legacy_command(setup, args):
    result = invoke(setup, *args)
    assert result.exit_code == 2, result.output
    assert "Nothing was run" in result.output
    assert "Use the legacy backend: aq test --aq-backend pytest" in result.output
    assert not setup.calls
    assert not setup.report.exists()


def test_unknown_marker_never_turns_empty_selection_green(setup):
    result = invoke(setup, "tests/", "-m", "unknown")
    assert result.exit_code == 2
    assert "nothing was verified" in result.output


@pytest.mark.parametrize(
    "changed",
    ["tests/test_new.py", "tests/selection_catalogue.json", ".github/workflows/tests.yml"],
)
def test_stale_manifest_cannot_silently_omit_new_obligations(setup, changed):
    (setup.root / changed).write_text("changed")
    result = invoke(setup, "cli")
    assert result.exit_code == 2, result.output
    assert "stale manifest" in result.output
    assert not setup.calls


def test_marker_with_no_exact_manifest_target_is_refused(setup):
    manifest = setup.root / "tools/bazel/test_suites.json"
    body = json.loads(manifest.read_text())
    del body["modules"]["tests/test_m0.py"]["targets"]["slow"]
    manifest.write_text(json.dumps(body))
    assert invoke(setup, "tests/test_m0.py", "-m", "slow").exit_code == 2
    assert not setup.calls


def test_worker_db_and_memory_limits_remain_bounded(setup, monkeypatch):
    monkeypatch.setenv("AQ_BAZEL_DB_SLOTS", "1")
    monkeypatch.setenv("AQ_BAZEL_TEST_MEMORY_MB", "2048")
    monkeypatch.setenv("AQ_BAZEL_MEMORY_MB", "3072")
    result = invoke(setup, "--aq-workers", "100", "tests/test_m0.py")
    assert result.exit_code == 0, result.output
    argv = setup.calls[0][0]
    assert "--jobs=2" in argv
    assert "--local_resources=aq_pg=1" in argv
    assert "--default_test_resources=memory=2048" in argv
    assert "--local_resources=memory=3072" in argv
    monkeypatch.setenv("AQ_BAZEL_MEMORY_MB", "100")
    assert invoke(setup, "tests/test_m0.py").exit_code == 2


@pytest.mark.parametrize(
    ("allocated", "requested", "expected"),
    [(8, None, 2), (8, 100, 2), (8, 1, 1), (1, 100, 1)],
)
def test_cold_build_caps_loading_and_execution_within_admission(
    setup, monkeypatch, allocated, requested, expected
):
    monkeypatch.setattr(
        "src.cli.test_runner._caps", lambda _r: (2, allocated, "not slow", 0.05, 0)
    )
    args = [] if requested is None else ["--aq-workers", str(requested)]
    result = invoke(setup, *args, "tests/test_m0.py")
    assert result.exit_code == 0, result.output
    argv = setup.calls[0][0]
    assert f"--jobs={expected}" in argv
    assert "--local_test_jobs=0" in argv  # Resource reservations remain active.
    assert f"--loading_phase_threads={expected}" in argv
    assert f"--local_resources=cpu={expected}" in argv
    assert f"--local_resources=aq_pg={expected}" in argv
    assert f"--local_resources=memory={expected * 1024}" in argv
    assert f"Bazel jobs {expected}" in result.output


@pytest.mark.parametrize("full", [False, True])
def test_slot_contention_and_full_suite_lock_refuse_without_spawning(setup, full):
    sem = (
        SlotSemaphore(full_suite_lock_dir(setup.locks), 1)
        if full
        else SlotSemaphore(setup.locks, 2)
    )
    with sem.acquire(timeout=0):
        if full:
            result = invoke(setup, "--aq-no-wait", "tests/")
            assert SlotSemaphore(setup.locks, 2).snapshot()["free"] == 2
        else:
            with sem.acquire(timeout=0):
                result = invoke(setup, "--aq-no-wait", "tests/test_m0.py")
    assert result.exit_code == 75, result.output
    assert not setup.calls
    assert json.loads(setup.report.read_text().splitlines()[-1])["event"] == "slot_timeout"


def test_full_suite_acquires_both_locks_and_releases_on_launch_failure(setup, monkeypatch):
    def fail(argv, **_kwargs):
        assert SlotSemaphore(full_suite_lock_dir(setup.locks), 1).snapshot()["free"] == 0
        assert SlotSemaphore(setup.locks, 2).snapshot()["free"] == 1
        raise FileNotFoundError("no bazel binary")

    monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", fail)
    result = invoke(setup, "tests/")
    assert result.exit_code == 127, result.output
    assert SlotSemaphore(full_suite_lock_dir(setup.locks), 1).snapshot()["free"] == 1
    assert SlotSemaphore(setup.locks, 2).snapshot()["free"] == 2
    assert json.loads(setup.report.read_text().splitlines()[-1])["event"] == "released"


def test_signal_cancellation_forwards_to_child_and_releases_admission(setup, monkeypatch, tmp_path):
    marker = tmp_path / "terminated"
    script = tmp_path / "fake-bazel"
    script.write_text(
        f"#!{sys.executable}\nimport os, signal\nfrom pathlib import Path\n"
        f'def stop(*_args):\n Path({str(marker)!r}).write_text("terminated")\n raise SystemExit(143)\n'
        "signal.signal(signal.SIGTERM, stop)\nos.kill(os.getppid(), signal.SIGTERM)\nsignal.pause()\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("AQ_BAZEL_EXECUTABLE", str(script))
    monkeypatch.setattr("src.cli.test_runner._run_forwarding_signals", _run_forwarding_signals)
    previous = signal.getsignal(signal.SIGTERM)
    result = invoke(setup, "tests/")
    assert result.exit_code == 143, result.output
    assert marker.read_text() == "terminated"
    assert signal.getsignal(signal.SIGTERM) == previous
    assert SlotSemaphore(setup.locks, 2).snapshot()["free"] == 2
    assert SlotSemaphore(full_suite_lock_dir(setup.locks), 1).snapshot()["free"] == 1
    assert json.loads(setup.report.read_text().splitlines()[-1])["event"] == "released"


def test_smart_shadow_keeps_selection_observational_and_records_backend(setup, monkeypatch):
    calls = []

    def execute(command, args):
        calls.append((command, args))
        if command == "test_select":
            return {"selection_id": "selection", "recorded": True, "ordered": ["tests/test_m5.py"]}
        return {"success": True, "stale": False}

    monkeypatch.setattr("src.cli.test_runner._selection_execute", execute)
    result = invoke(setup, "--aq-smart", "cli")
    assert result.exit_code == 0, result.output
    assert setup.calls[0][0][-1] == "//tests:m0__cli"
    assert calls[0][1]["targets"] == ["tests/test_m0.py"]
    assert calls[-1][1]["payload"] == {"backend": "bazel"}
    assert calls[-1][1]["executed_modules"] == ["tests/test_m0.py"]


def test_detached_bazel_is_refused_instead_of_submitting_pytest(setup):
    result = invoke(setup, "--aq-detach", "tests/test_m0.py")
    assert result.exit_code == 2
    assert "requires local resource admission" in result.output


def test_repository_manifest_maps_a_real_module():
    root = Path(__file__).resolve().parents[1]
    # This test deliberately uses the committed manifest, independent of the fixture above.
    assert Path.cwd() == root
    plan = _bazel_plan(
        ("tests/test_env_scrub.py",),
        markers="not perf and not migration and not slow and not tmux and not integration",
        all_markers=False,
    )
    assert plan.targets == ("//tests:test_env_scrub",)
    assert not plan.full_suite


def test_shared_postgres_diagnostic_does_not_import_cli():
    import subprocess

    script = """
import sys
from src.test_dsn import _POSTGRES_SETUP, postgres_test_dsn_error
assert postgres_test_dsn_error({}) == _POSTGRES_SETUP
assert postgres_test_dsn_error({"POSTGRES_TEST_DSN": "  "}) == _POSTGRES_SETUP
assert postgres_test_dsn_error({"POSTGRES_TEST_DSN": "postgresql://host/base"}) is None
assert not any(name == "src.cli" or name.startswith("src.cli.") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr
