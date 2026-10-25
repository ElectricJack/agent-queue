"""H1 lock/ABI boundaries and opt-in, admitted clean-root toolchain verification."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bazel_lock", ROOT / "tools/bazel/lock.py")
lock_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock_tool)


def test_committed_dependency_intent_and_lock():
    lock_tool.validate(ROOT)


@pytest.mark.parametrize(
    "system,machine,libc",
    [
        ("Darwin", "x86_64", ("glibc", "2.39")),
        ("Linux", "aarch64", ("glibc", "2.39")),
        ("Linux", "x86_64", ("musl", "1.2")),
        ("Linux", "x86_64", ("glibc", "2.27")),
        ("Linux", "x86_64", ("glibc", "3.0")),
    ],
)
def test_unsupported_abi_refused(system, machine, libc):
    lock = json.loads((ROOT / lock_tool.LOCK_PATH).read_text())
    with pytest.raises(ValueError, match="unsupported"):
        lock_tool.check_platform(lock, system, machine, libc)


def test_checksum_corruption_refused(tmp_path):
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"approved bytes")
    digest = lock_tool.sha256(archive)
    lock_tool.verify_archive(archive, digest)
    archive.write_bytes(b"substituted bytes")
    with pytest.raises(ValueError, match="checksum mismatch"):
        lock_tool.verify_archive(archive, digest)


def _copy_inputs(destination):
    for path in (
        "MODULE.bazel",
        "MODULE.bazel.lock",
        ".bazelversion",
        ".bazelrc",
        "BUILD.bazel",
        "pyproject.toml",
        "package-lock.json",
        "alembic.ini",
    ):
        shutil.copy2(ROOT / path, destination / path)
    shutil.copytree(ROOT / "tools/bazel", destination / "tools/bazel")
    shutil.copytree(
        ROOT / "migrations", destination / "migrations",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        ROOT / "packages/aq-client",
        destination / "packages/aq-client",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def test_dependency_change_requires_regeneration(tmp_path):
    _copy_inputs(tmp_path)
    path = tmp_path / "pyproject.toml"
    path.write_text(path.read_text() + "\n# dependency intent changed\n")
    with pytest.raises(ValueError, match="dependency intent drift"):
        lock_tool.validate(tmp_path)


def test_module_pin_drift_refused(tmp_path):
    _copy_inputs(tmp_path)
    module = tmp_path / "MODULE.bazel"
    module.write_text(module.read_text().replace('version = "1.8.2"', 'version = "0.0.0"'))
    with pytest.raises(ValueError, match="MODULE.bazel drift"):
        lock_tool.validate(tmp_path)


def test_npm_integrity_drift_refused(tmp_path):
    _copy_inputs(tmp_path)
    path = tmp_path / "package-lock.json"
    npm = json.loads(path.read_text())
    npm["packages"]["node_modules/typescript"]["integrity"] = "sha512-corrupt"
    path.write_text(json.dumps(npm))
    with pytest.raises(ValueError, match="TypeScript smoke pin drift"):
        lock_tool.validate(tmp_path)


def test_native_transitive_library_omission_refused(tmp_path):
    _copy_inputs(tmp_path)
    path = tmp_path / lock_tool.LOCK_PATH
    lock = json.loads(path.read_text())
    lock["native"]["packages"] = [p for p in lock["native"]["packages"] if p["name"] != "libgcc"]
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="native runtime is incomplete"):
        lock_tool.validate(tmp_path)


def test_native_sysroot_omission_refused(tmp_path):
    _copy_inputs(tmp_path)
    path = tmp_path / lock_tool.LOCK_PATH
    lock = json.loads(path.read_text())
    del lock["native"]["sysroot"]
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="locked glibc sysroot"):
        lock_tool.validate(tmp_path)


@pytest.mark.parametrize("root_number", [1, 2])
def test_clean_root_toolchain_smoke(root_number):
    bazel = os.environ.get("AQ_BAZEL_SMOKE")
    if not bazel:
        pytest.skip("opt-in toolchain acceptance: set AQ_BAZEL_SMOKE to locked Bazel executable")
    lock = lock_tool.validate(ROOT)
    lock_tool.verify_archive(Path(bazel), lock["bazel"]["sha256"])
    lock_tool.check_platform(lock, platform.system(), platform.machine(), platform.libc_ver())
    # No repository, action cache or Bazel server is shared between the two roots.
    with tempfile.TemporaryDirectory(prefix=f"aq-h1-{root_number}-") as temporary:
        base = Path(temporary)
        checkout = base / "checkout"
        checkout.mkdir()
        _copy_inputs(checkout)
        env = {"HOME": str(base / "home"), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        (base / "home").mkdir()
        commands = [
            ["mod", "deps", "--lockfile_mode=error"],
            [
                "test",
                "--config=local",
                "--jobs=1",
                "//tools/bazel/smoke:python_smoke",
                "//tools/bazel/smoke:typescript_smoke",
                "//tools/bazel/smoke:native_smoke",
            ],
        ]
        for command in commands:
            result = subprocess.run(
                [bazel, "--batch", f"--output_user_root={base / 'output'}", *command],
                cwd=checkout,
                check=False,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=900,
            )
            assert result.returncode == 0, result.stdout
            print(f"Clean root {root_number}: {' '.join(command)}\n{result.stdout}")
        if root_number == 1:
            # Exercise Bazel's real lock and checksum enforcement, beyond the
            # fast unit preflight. Each mutation exists only in this scratch root.
            module_lock = checkout / "MODULE.bazel.lock"
            saved = module_lock.read_bytes()
            module_lock.unlink()
            result = subprocess.run(
                [
                    bazel,
                    "--batch",
                    f"--output_user_root={base / 'output'}",
                    "mod",
                    "deps",
                    "--lockfile_mode=error",
                ],
                cwd=checkout,
                check=False,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
            )
            assert result.returncode != 0 and "lockfile" in result.stdout.lower(), result.stdout
            module_lock.write_bytes(saved)
            native_lock = checkout / lock_tool.LOCK_PATH
            poisoned = json.loads(native_lock.read_text())
            poisoned["micromamba"]["sha256"] = "0" * 64
            native_lock.write_text(json.dumps(poisoned))
            result = subprocess.run(
                [
                    bazel,
                    "--batch",
                    f"--output_user_root={base / 'output'}",
                    "fetch",
                    "@aq_native//:runtime",
                ],
                cwd=checkout,
                check=False,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
            )
            assert result.returncode != 0 and "checksum" in result.stdout.lower(), result.stdout
            print("Bazel missing-lock and corrupted-archive negative checks: OK")


# B1 graph and launcher boundaries; H1 lock checks above remain independent.
_spec_graph = importlib.util.spec_from_file_location(
    "bazel_targets", ROOT / "scripts/bazel/generate_targets.py"
)
targets = importlib.util.module_from_spec(_spec_graph)
_spec_graph.loader.exec_module(targets)


def _manifest():
    return json.loads((ROOT / "tools/bazel/test_suites.json").read_text())


def test_graph_is_reproducible_and_matches_catalogue():
    manifest = _manifest()
    catalogue = json.loads((ROOT / "tests/selection_catalogue.json").read_text())
    assert set(manifest["modules"]) == set(catalogue["modules"])
    assert len(manifest["modules"]) == len({m["target"] for m in manifest["modules"].values()})
    for path, entry in catalogue["modules"].items():
        assert manifest["modules"][path]["areas"] == entry["areas"]
        assert manifest["modules"][path]["markers"] == entry["markers"]
    for relative, rendered in targets.generate(ROOT).items():
        assert (ROOT / relative).read_text() == rendered, relative


def test_workflow_obligations_preserve_splits_overlap_and_performance():
    manifest = _manifest()
    suites = manifest["suites"]
    arms = ["cli", "migration_slow", "postgres", *[f"default_{i}" for i in range(1, 9)]]
    assert set(arms) <= suites.keys()
    assert len(arms) == 11
    assert suites["migration_slow"]["marker"] == "migration or slow"
    assert suites["postgres"]["marker"] == "(integration or perf) and not migration and not slow"
    assert "--ignore=tests/test_e2e_cli_stateful.py" in suites["postgres"]["command"]
    assert suites["cli_startup"]["perf_strict"] is True
    assert all(not suite["perf_strict"] for key, suite in suites.items() if key != "cli_startup")
    default = [
        node
        for i in range(1, 9)
        for nodes in suites[f"default_{i}"]["modules"].values()
        for node in nodes
    ]
    default = [manifest["collection"]["nodes"][i]["nodeid"] for i in default]
    assert len(default) == len(set(default))
    assert not any(
        node.startswith(("tests/test_cli_inventory.py::", "tests/test_cli_conformance.py::"))
        for node in default
    )
    startup = suites["cli_startup"]["modules"]["tests/test_cli_startup_offline.py"]
    assert startup
    # Startup has an independent obligation even where perf nodes also run in PG.
    assert set(startup) <= set(suites["postgres"]["modules"]["tests/test_cli_startup_offline.py"])
    assert {key for key in suites if key.startswith("e2e_")} == {
        "e2e_claims",
        "e2e_cli",
        "e2e_graphs",
        "e2e_failover",
    }


def test_node_obligations_are_backed_by_independent_collection():
    manifest = _manifest()
    collected = set(range(len(manifest["collection"]["nodes"])))
    assert collected
    for suite in manifest["suites"].values():
        for path, nodes in suite["modules"].items():
            assert len(nodes) == len(set(nodes))
            assert set(nodes) <= collected
            assert all(
                manifest["collection"]["nodes"][i]["nodeid"].startswith(path + "::") for i in nodes
            )
    assert sum(len(m["nodes"]) for m in manifest["modules"].values()) == len(collected)


def _graph_fixture(tmp_path):
    for relative, text in {
        "src/__init__.py": "",
        "src/alpha/__init__.py": "",
        "src/alpha/use.py": "from src.beta import service\n",
        "src/alpha/prompt.md": "prompt bytes",
        "src/beta/__init__.py": "",
        "src/beta/service.py": "from src.alpha import use\n",
        "src/unused/__init__.py": "",
        "tests/conftest.py": "from src.alpha import use\n",
        "tests/test_behavior.py": "def test_behavior(): pass\n",
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return {"tests/test_behavior.py": {}}


def test_cycle_closure_and_prompt_resource_are_declared_per_owner(tmp_path):
    modules = _graph_fixture(tmp_path)
    libraries, tests = targets.graph(tmp_path, modules)
    assert "src/alpha/prompt.md" in libraries["src/alpha"]["files"]
    assert libraries["src/alpha"]["source_packages"] == ["src", "src/alpha", "src/beta"]
    assert set(tests["tests/test_behavior.py"]["libraries"]) == {
        "//src:library",
        "//src/alpha:library",
        "//src/beta:library",
    }
    # A package resource addition changes the consuming package inventory.
    (tmp_path / "src/alpha/new_prompt.md").write_text("new resource")
    changed, _ = targets.graph(tmp_path, modules)
    assert changed["src/alpha"]["files"] != libraries["src/alpha"]["files"]
    assert changed["src/unused"] == libraries["src/unused"]


def test_unknown_dynamic_load_and_conftest_change_widen_consumers(tmp_path):
    modules = _graph_fixture(tmp_path)
    _, initial = targets.graph(tmp_path, modules)
    assert "//src/unused:library" not in initial["tests/test_behavior.py"]["libraries"]
    (tmp_path / "tests/conftest.py").write_text(
        "import importlib\nmodule = importlib.import_module(configured_name)\n"
    )
    _, changed = targets.graph(tmp_path, modules)
    assert changed["tests/test_behavior.py"]["conservative"]
    assert "//src/unused:library" in changed["tests/test_behavior.py"]["libraries"]
    # Refuse to reuse parametrized IDs when fixture source changes.
    old = targets.collection_inputs(tmp_path)
    (tmp_path / "tests/conftest.py").write_text("fixture_has_changed = True\n")
    assert targets.collection_inputs(tmp_path) != old


def test_deleted_renamed_and_initializer_inputs_change_the_graph(tmp_path):
    modules = _graph_fixture(tmp_path)
    before, _ = targets.graph(tmp_path, modules)
    path = tmp_path / "src/alpha/prompt.md"
    path.rename(path.with_name("renamed.md"))
    renamed, _ = targets.graph(tmp_path, modules)
    assert renamed["src/alpha"]["files"] != before["src/alpha"]["files"]
    path.with_name("renamed.md").unlink()
    deleted, _ = targets.graph(tmp_path, modules)
    assert deleted["src/alpha"]["files"] != renamed["src/alpha"]["files"]
    # Importing any child includes its package initializer as an action input.
    assert "src/alpha/__init__.py" in deleted["src/alpha"]["files"]


def test_launcher_rejects_node_drift_before_selecting_split():
    from types import SimpleNamespace

    from tools.bazel.pytest_main import ObligationPlugin

    spec = {
        "module_nodes": ["tests/test_x.py::test_a", "tests/test_x.py::test_b"],
        "nodes": ["tests/test_x.py::test_a"],
    }
    plugin = ObligationPlugin(spec)
    items = [SimpleNamespace(nodeid=node) for node in spec["module_nodes"]]
    for item in items:
        plugin.pytest_itemcollected(item)
    rejected = []
    config = SimpleNamespace(
        hook=SimpleNamespace(pytest_deselected=lambda items: rejected.extend(items))
    )
    plugin.pytest_collection_modifyitems(None, config, items)
    assert [item.nodeid for item in items] == spec["nodes"]
    assert [item.nodeid for item in rejected] == ["tests/test_x.py::test_b"]
    plugin.collected.append("tests/test_x.py::test_added")
    with pytest.raises(pytest.UsageError, match="collection drift"):
        plugin.pytest_collection_modifyitems(None, config, items)


async def test_launcher_lifecycle_receipt_includes_failure_and_partial_cleanup(
    tmp_path, monkeypatch
):
    from tools.bazel import pytest_main as launcher

    calls = []

    class FakeLaunch:
        def __init__(self, *args, **kwargs):
            pass

        async def bootstrap(self):
            calls.append("bootstrap")
            raise RuntimeError("partial bootstrap")

        async def execute(self):
            pytest.fail("must not execute after failed bootstrap")

        async def cleanup(self):
            calls.append("cleanup")

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(launcher, "Launch", FakeLaunch)
    monkeypatch.setattr(launcher, "ENTRY_TIME", launcher.time.monotonic())
    assert await launcher.run({}, {}, tmp_path) == 1
    receipt = json.loads((tmp_path / "target.json").read_text())
    assert receipt["exit_status"] == 1
    assert receipt["errors"] == ["RuntimeError: partial bootstrap"]
    assert calls == ["bootstrap", "cleanup", "kill"]


def test_declared_runtime_cannot_escape_runfiles(tmp_path):
    from tools.bazel.pytest_main import native_runtime_inputs, runfile
    from tools.bazel.test_environment import IsolationError

    workspace = tmp_path / "_main"
    workspace.mkdir()
    declared = tmp_path / "python/bin/python"
    declared.parent.mkdir(parents=True)
    declared.touch()
    assert runfile(workspace, "../python/bin/python") == declared
    with pytest.raises(IsolationError, match="escapes"):
        runfile(workspace, "../../operator/python")

    installed = tmp_path / "installation"
    (installed / "bin").mkdir(parents=True)
    (installed / "bin/git").touch()
    (installed / "conda-meta").mkdir()
    (installed / "conda-meta/private.json").touch()
    native = tmp_path / "native/runtime"
    (native / "bin").mkdir(parents=True)
    (native / "bin/git").symlink_to(installed / "bin/git")
    prefix, roots = native_runtime_inputs(workspace, "../native/runtime/bin/git")
    assert prefix == installed
    assert roots == (installed / "bin",)
    # A directory bind must never admit an undeclared sibling or symlink target.
    (installed / "bin/undeclared").touch()
    with pytest.raises(IsolationError, match="undeclared"):
        native_runtime_inputs(workspace, "../native/runtime/bin/git")
    (installed / "bin/undeclared").unlink()
    (installed / "bin/escape").symlink_to(declared)
    (native / "bin/escape").symlink_to(declared)
    with pytest.raises(IsolationError, match="escapes"):
        native_runtime_inputs(workspace, "../native/runtime/bin/git")


@pytest.mark.parametrize("module", ["test_env_scrub", "test_resolve_repo_path"])
def test_admitted_bazel_module_acceptance(module, tmp_path):
    """Runs under aq test's existing slot; never nests xdist inside Bazel."""
    executable = os.environ.get("AQ_BAZEL_EXECUTABLE")
    if not executable:
        pytest.skip("set AQ_BAZEL_EXECUTABLE for admitted real module acceptance")
    lock = lock_tool.validate(ROOT)
    lock_tool.verify_archive(Path(executable), lock["bazel"]["sha256"])
    result = subprocess.run(
        [
            executable,
            "--batch",
            f"--output_user_root={tmp_path / 'bazel'}",
            "test",
            "--config=local",
            "--jobs=1",
            "--test_output=errors",
            f"//tests:{module}",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=900,
    )
    (tmp_path / "bazel-module.log").write_text(result.stdout)
    assert result.returncode == 0, result.stdout
