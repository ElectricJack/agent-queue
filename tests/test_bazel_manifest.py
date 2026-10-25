"""H1 lock/ABI boundaries and opt-in, admitted clean-root toolchain verification."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from src.test_selection.static_impact import FixedStaticImpact

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


def _generator_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/bazel/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_inventory_and_locked_npm_closure():
    inputs = _generator_module("generator_inputs")
    assert (ROOT / "tools/bazel/generator_inputs.bzl").read_text() == inputs.render_inputs(ROOT)
    assert json.loads((ROOT / "tools/bazel/generator_npm.lock.json").read_text()) == (
        inputs.npm_closure(ROOT)
    )


def test_generator_npm_closure_follows_nested_lock_and_rejects_drift(tmp_path):
    inputs = _generator_module("generator_inputs")
    shutil.copyfile(ROOT / "package-lock.json", tmp_path / "package-lock.json")
    (tmp_path / "packages/aq-ts-client").mkdir(parents=True)
    shutil.copyfile(ROOT / "packages/aq-ts-client/package.json",
                    tmp_path / "packages/aq-ts-client/package.json")
    lock = json.loads((tmp_path / "package-lock.json").read_text())
    location = "node_modules/@hey-api/openapi-ts"
    # Resolution must retain a nested version rather than hoisting a new one.
    nested = location + "/node_modules/commander"
    lock["packages"][nested] = dict(lock["packages"]["node_modules/commander"], version="13.0.0")
    (tmp_path / "package-lock.json").write_text(json.dumps(lock))
    assert nested in inputs.npm_closure(tmp_path)["packages"]
    lock["packages"][location]["version"] = "0.0.0"
    (tmp_path / "package-lock.json").write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="pin differs"):
        inputs.npm_closure(tmp_path)


def test_generator_import_closure_follows_functions_and_package_initializers(tmp_path):
    inputs = _generator_module("generator_inputs")
    (tmp_path / "src/sub").mkdir(parents=True)
    (tmp_path / "src/__init__.py").write_text("")
    (tmp_path / "src/sub/__init__.py").write_text("from . import resource\n")
    (tmp_path / "src/sub/resource.py").write_text("DATA = 'declared'\n")
    (tmp_path / "src/entry.py").write_text("def build():\n    from .sub import resource\n")
    assert inputs.python_closure(tmp_path, ["src/entry.py"]) == [
        "src/__init__.py", "src/entry.py", "src/sub/__init__.py", "src/sub/resource.py",
    ]
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/unimported.py").write_text("")
    (tmp_path / "tests/test_example.py").write_text(
        "def test_child():\n    from src import unimported, missing\n"
    )
    assert inputs.selection_module_inputs(tmp_path) == {"src/unimported.py"}


def test_generator_environment_discards_worker_credentials_and_host_paths(tmp_path, monkeypatch):
    generate = _generator_module("generate")
    monkeypatch.setenv("AQ_API_TOKEN", "poison")
    monkeypatch.setenv("GITHUB_TOKEN", "poison")
    monkeypatch.setenv("HOME", "/operator")
    monkeypatch.setenv("PATH", "/ambient/tools")
    env = generate.environment(tmp_path, Path("/declared/native"), None, Path("/declared/ruff"))
    assert "AQ_API_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["PATH"] == f"{tmp_path / 'bin'}:/declared/native/bin"
    assert "operator" not in json.dumps(env)


def test_generator_manifests_detect_removed_files_and_root_dependent_bytes(tmp_path):
    generate = _generator_module("generate")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "client.ts").write_bytes(b"stable bytes\n")
    (second / "client.ts").write_bytes(b"stable bytes\n")
    generate.compare(first, second)
    (second / "client.ts").unlink()
    with pytest.raises(ValueError, match="client.ts"):
        generate.compare(first, second)
    (second / "client.ts").write_text(str(tmp_path))
    with pytest.raises(ValueError, match="temporary/execroot"):
        generate.assert_portable(second, [tmp_path])
    (second / "client.ts").unlink()
    (second / "client.ts").symlink_to(first / "client.ts")
    with pytest.raises(ValueError, match="symlink"):
        generate.manifest(second)
    generate.compare(first, second, allow_symlinks=True)


def test_clean_root_generator_acceptance():
    """Admitted Bazel verification, twice, with independent scratch/output roots."""
    bazel = os.environ.get("AQ_BAZEL_GENERATORS")
    if not bazel:
        pytest.skip("set AQ_BAZEL_GENERATORS to the locked Bazel executable for acceptance")
    lock_tool.verify_archive(Path(bazel), lock_tool.validate(ROOT)["bazel"]["sha256"])
    evidence = Path(os.environ["AQ_BAZEL_GENERATOR_EVIDENCE"])
    evidence.mkdir(parents=True, exist_ok=True)
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode().split("\0")
    # Both clean roots consume the same immutable snapshot, even if a worker
    # changes its checkout while this admitted verification is running.
    snapshot = {relative: ((ROOT / relative).read_bytes(),
                           (ROOT / relative).stat().st_mode & 0o777)
                for relative in paths if relative and (ROOT / relative).is_file()}
    manifests = []
    for number in (1, 2):
        with tempfile.TemporaryDirectory(prefix=f"aq-b2-{number}-") as temporary:
            base = Path(temporary)
            checkout = base / "checkout"
            checkout.mkdir()
            for relative, (content, mode) in snapshot.items():
                output = checkout / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
                output.chmod(mode)
            (base / "home").mkdir()
            env = {"HOME": str(base / "home"), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
            prefix = [bazel, "--batch", f"--output_user_root={base / 'output'}"]
            for command in (
                ["mod", "deps", "--lockfile_mode=error"],
                ["test", "--config=ci", "--jobs=1", "//checks:generated",
                 "--test_output=all",
                 f"--build_event_json_file={evidence / f'root-{number}.bep.jsonl'}"],
            ):
                result = subprocess.run(prefix + command, cwd=checkout, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800)
                (evidence / f"root-{number}-{command[0]}.log").write_text(result.stdout)
                assert result.returncode == 0, result.stdout
            binary = subprocess.check_output(prefix + ["info", "bazel-bin"], cwd=checkout,
                                             env=env, text=True).strip()
            actual = json.loads((Path(binary) / "tools/bazel/typescript.manifest.json").read_text())
            manifests.append(actual)
            (evidence / f"root-{number}-typescript.json").write_text(json.dumps(actual, indent=2))
    assert manifests[0] and manifests[0] == manifests[1]


def _npm_import_fixture(root):
    packages = {
        "": {"workspaces": ["dashboard", "packages/aq-ts-client"]},
        "dashboard": {"dependencies": {"@aq/ts-client": "*", "a": "^1"}},
        "packages/aq-ts-client": {"name": "@aq/ts-client", "version": "0.0.1"},
        "node_modules/@aq/ts-client": {"link": True, "resolved": "packages/aq-ts-client"},
        "node_modules/a": {"version": "1.0.0", "dependencies": {"alias": "npm:b@2"}},
        "node_modules/a/node_modules/alias": {"name": "b", "version": "2.0.0"},
        "node_modules/alias": {"name": "b", "version": "3.0.0"},
    }
    for location, package in packages.items():
        if "node_modules/" in location and not package.get("link"):
            name = package.get("name", location.rsplit("node_modules/", 1)[-1])
            package["resolved"] = f"https://registry.npmjs.org/{name}/-/{name}-{package['version']}.tgz"
            package["integrity"] = "sha512-" + package["version"]
    for workspace in ("", "dashboard", "packages/aq-ts-client"):
        path = root / workspace / "package.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(packages[workspace]))
    (root / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))
    return packages


def test_npm_projection_preserves_alias_nested_version_workspace_and_peer(tmp_path):
    importer = _generator_module("import_npm")
    packages = _npm_import_fixture(tmp_path)
    packages["node_modules/a"]["peerDependencies"] = {"alias": "*", "absent": "*"}
    packages["node_modules/a"]["peerDependenciesMeta"] = {"absent": {"optional": True}}
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))
    result = importer.derive(tmp_path)
    assert set(result["packages"]) == {"a@1.0.0", "b@2.0.0", "b@3.0.0"}
    assert result["snapshots"]["a@1.0.0"]["dependencies"] == {"alias": "b@2.0.0"}
    assert result["importers"]["../../dashboard"]["dependencies"]["@aq/ts-client"]["version"] == (
        "link:../packages/aq-ts-client"
    )


def test_committed_npm_projection_is_exact_and_drift_fails(tmp_path):
    importer = _generator_module("import_npm")
    importer.check(ROOT)
    npm_packages = json.loads((ROOT / "package-lock.json").read_text())["packages"]
    expected = {}
    for location, package in npm_packages.items():
        if "node_modules/" in location and not package.get("link"):
            name = package.get("name", location.rsplit("node_modules/", 1)[-1])
            expected[f"{name}@{package['version']}"] = {
                "integrity": package["integrity"], "tarball": package["resolved"],
            }
    projected = json.loads((ROOT / importer.LOCK).read_text())["packages"]
    assert {key: package["resolution"] for key, package in projected.items()} == expected
    locations = json.loads((ROOT / importer.LOCK).read_text())["aqImport"]["locations"]
    assert set(locations) == {p for p in npm_packages if "node_modules/" in p}
    _npm_import_fixture(tmp_path)
    (tmp_path / "tools/bazel").mkdir(parents=True)
    path = tmp_path / importer.LOCK
    path.write_text(importer.render(tmp_path))
    importer.check(tmp_path)
    lock = json.loads(path.read_text())
    lock["packages"]["b@2.0.0"]["resolution"]["integrity"] = "sha512-corrupt"
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="npm/pnpm graph drift"):
        importer.check(tmp_path)
    path.write_text(importer.render(tmp_path))
    npm = tmp_path / "package-lock.json"
    npm.write_text(npm.read_text() + "\n")
    with pytest.raises(ValueError, match="npm/pnpm graph drift"):
        importer.check(tmp_path)


def test_npm_projection_refuses_unresolved_dependency_and_intent_drift(tmp_path):
    importer = _generator_module("import_npm")
    packages = _npm_import_fixture(tmp_path)
    packages["node_modules/a"]["dependencies"]["missing"] = "*"
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))
    with pytest.raises(ValueError, match="unresolved npm dependency"):
        importer.derive(tmp_path)
    _npm_import_fixture(tmp_path)
    (tmp_path / "dashboard/package.json").write_text('{"dependencies": {}}')
    with pytest.raises(ValueError, match="npm workspace intent drift"):
        importer.derive(tmp_path)


def test_npm_projection_refuses_conflicting_same_version_contexts(tmp_path):
    importer = _generator_module("import_npm")
    packages = _npm_import_fixture(tmp_path)
    packages["node_modules/alias"] = {
        **packages["node_modules/a/node_modules/alias"], "dependencies": {"a": "^1"},
    }
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))
    with pytest.raises(ValueError, match="ambiguous npm dependency context"):
        importer.derive(tmp_path)


def test_dashboard_environment_ignores_ambient_node_and_credentials(tmp_path, monkeypatch):
    dashboard = _generator_module("dashboard")
    monkeypatch.setenv("NODE_PATH", "/poison/node_modules")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/poison/steal.js")
    monkeypatch.setenv("AQ_API_TOKEN", "poison")
    monkeypatch.setenv("HOME", "/poison/home")
    node = tmp_path / "locked-node"
    node.write_text("locked")
    env = dashboard.environment(tmp_path, tmp_path / "native", node)
    assert set(env).isdisjoint({"NODE_PATH", "NODE_OPTIONS", "AQ_API_TOKEN"})
    assert env["HOME"] == str(tmp_path / "home")
    assert env["PATH"] == f"{tmp_path / 'bin'}:{tmp_path / 'native/bin'}"
    assert (tmp_path / "bin/node").resolve() == node


def test_dashboard_ts_package_consumes_generated_artifact(tmp_path):
    dashboard = _generator_module("dashboard")
    source = tmp_path / "package.json"
    source.write_text('{"name":"@aq/ts-client"}')
    generated = tmp_path / "generated/packages/aq-ts-client/src"
    generated.mkdir(parents=True)
    (generated / "index.ts").write_text("export const fromB2 = true;\n")
    output = tmp_path / "package"
    output.mkdir()  # Bazel precreates declared directory outputs.
    dashboard.execute("package", {"srcs": [[str(source), "packages/aq-ts-client/package.json"]]},
                      output, tmp_path / "node", tmp_path / "native", tmp_path / "generated")
    assert (output / "src/index.ts").read_bytes() == (generated / "index.ts").read_bytes()
    assert (output / "package.json").read_bytes() == source.read_bytes()


def test_dashboard_reconstructs_npm_hoisting_aliases_and_generated_workspace(tmp_path):
    dashboard = _generator_module("dashboard")
    root = tmp_path / "checkout"
    root.mkdir()
    _npm_import_fixture(root)
    inputs = []
    for name, version in (("a", "1.0.0"), ("b", "2.0.0"), ("b", "3.0.0"),
                          ("@aq/ts-client", "0.0.1")):
        package = tmp_path / "archives" / (name.replace("/", "+") + version)
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": name, "version": version}))
        (package / "index.ts").write_text(f"export const version = '{version}';\n")
        if name == "@aq/ts-client":
            generated = tmp_path / "generated-package.json"
            (package / "package.json").rename(generated)
            (package / "package.json").symlink_to(generated)
        inputs.append([str(package), "node_modules/" + name])
    dashboard.materialize_npm(inputs, root)
    nested = root / "node_modules/a/node_modules/alias"
    assert json.loads((nested / "package.json").read_text())["version"] == "2.0.0"
    assert not nested.is_symlink()  # Real paths retain npm's ancestor @types lookup.
    assert json.loads((root / "node_modules/alias/package.json").read_text())["version"] == "3.0.0"
    assert (root / "node_modules/@aq/ts-client/index.ts").read_bytes() == (
        root / "packages/aq-ts-client/index.ts"
    ).read_bytes()
    assert not (root / "packages/aq-ts-client/package.json").is_symlink()


def test_clean_root_dashboard_acceptance():
    """Two isolated Bazel roots, independent TS manifests and Node24/npm reference."""
    bazel = os.environ.get("AQ_BAZEL_DASHBOARD")
    if not bazel:
        pytest.skip("set AQ_BAZEL_DASHBOARD to the locked Bazel executable for acceptance")
    lock_tool.verify_archive(Path(bazel), lock_tool.validate(ROOT)["bazel"]["sha256"])
    evidence = Path(os.environ["AQ_BAZEL_DASHBOARD_EVIDENCE"])
    evidence.mkdir(parents=True, exist_ok=True)
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode().split("\0")
    snapshot = {relative: ((ROOT / relative).read_bytes(),
                           (ROOT / relative).stat().st_mode & 0o777)
                for relative in paths if relative and (ROOT / relative).is_file()}
    manifests = []
    builds = []
    from tools.bazel.generate import manifest

    for number in (1, 2):
        # Retain roots as evidence for inspecting actions and independent outputs.
        base = evidence / f"root-{number}"
        checkout = base / "checkout"
        checkout.mkdir(parents=True)
        for relative, (content, mode) in snapshot.items():
            output = checkout / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            output.chmod(mode)
        home = base / "home"
        home.mkdir()
        poison = base / "poison"
        poison.mkdir()
        for name in ("node", "npm", "tsc", "vite"):
            (poison / name).write_text("#!/bin/sh\necho AMBIENT_NODE_USED >&2\nexit 99\n")
            (poison / name).chmod(0o755)
        # An incomplete declared dependency graph must not find ancestor packages.
        for name in ("typescript", "vite", "react", "@aq/ts-client"):
            package = base / "node_modules" / name
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({
                "name": name, "version": "0.0.0-poison", "main": "index.js",
            }))
            (package / "index.js").write_text("throw new Error('AMBIENT_PACKAGE_USED');\n")
        env = {"HOME": str(home), "PATH": f"{poison}:/usr/bin:/bin", "LANG": "C.UTF-8",
               "NODE_PATH": str(poison), "NODE_OPTIONS": "--require=/poison/ambient.cjs"}
        prefix = [bazel, "--batch", f"--output_user_root={base / 'output'}"]
        commands = [
            ["mod", "deps", "--lockfile_mode=error"],
            ["test", "--config=shadow", "--jobs=1", "//checks:dashboard",
             "--test_output=all", f"--build_event_json_file={base / 'bep.jsonl'}"],
        ]
        for command in commands:
            result = subprocess.run(prefix + command, cwd=checkout, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800)
            (base / f"{command[0]}.log").write_text(result.stdout)
            assert result.returncode == 0, result.stdout
        binary = subprocess.check_output(prefix + ["info", "bazel-bin"], cwd=checkout,
                                         env=env, text=True).strip()
        binary = Path(binary)
        actual = json.loads((binary / "tools/bazel/typescript.manifest.json").read_text())
        manifests.append(actual)
        (base / "typescript.json").write_text(json.dumps(actual, indent=2))
        build = manifest(binary / "dashboard/build.tree/dist")
        builds.append(build)
        (base / "dist.json").write_text(json.dumps(build, indent=2))
        if number == 1:
            # The reference installs from npm, independently of the pnpm graph.
            # Use the exact Node/npm archive Bazel fetched, never ambient PATH.
            nodes = list((base / "output").glob("*/external/*nodejs*linux_amd64/bin/node"))
            assert len(nodes) == 1, nodes
            archive = nodes[0].parent / "nodejs"
            node = archive / "bin/node"
            npm = archive / "lib/node_modules/npm/bin/npm-cli.js"
            reference = base / "npm-reference"
            shutil.copytree(checkout, reference)
            reference_env = {"HOME": str(home), "PATH": f"{node.parent}:/usr/bin:/bin",
                             "LANG": "C.UTF-8", "TZ": "UTC"}
            for index, command in enumerate((
                [str(node), str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                [str(node), str(npm), "--prefix", "dashboard", "run", "typecheck"],
                [str(node), str(npm), "--prefix", "dashboard", "run", "build"],
            )):
                result = subprocess.run(command, cwd=reference, env=reference_env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
                (base / f"npm-{index}.log").write_text(result.stdout)
                assert result.returncode == 0, result.stdout
            ts = manifest(reference / "packages/aq-ts-client/src")
            normalized = {p.removeprefix("packages/aq-ts-client/src/"): h for p, h in actual.items()}
            assert ts == normalized
            assert manifest(reference / "dashboard/dist") == build
    assert manifests[0] and manifests[0] == manifests[1]
    assert builds[0] and builds[0] == builds[1]


# V1: exact-head observational graph evidence and conservative cross-checks.
_spec_impact = importlib.util.spec_from_file_location(
    "bazel_impact",
    ROOT / "scripts/bazel/impact.py",
)
impact = importlib.util.module_from_spec(_spec_impact)
# dataclasses resolves annotations through the module registry.
sys.modules[_spec_impact.name] = impact
_spec_impact.loader.exec_module(impact)


def _impact_repo(tmp_path):
    from tests.selection_fixture_repo import build_fixture_repo, git

    repo = build_fixture_repo(tmp_path / "repo")
    for relative in (*impact.LOCKS, *impact.REGISTRY):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    (repo / "tools/bazel/toolchains.lock.json").write_bytes(
        (ROOT / "tools/bazel/toolchains.lock.json").read_bytes()
    )
    (repo / "MODULE.bazel.lock").write_bytes((ROOT / "MODULE.bazel.lock").read_bytes())
    (repo / "MODULE.bazel").write_text('module(name = "impact_fixture")\n')
    # No external modules: this graph tests invalidation without registry I/O.
    (repo / ".bazelrc").write_text("common --noenable_bzlmod\ncommon --enable_workspace\n")
    (repo / ".gitignore").write_text("bazel-*\n__pycache__/\n*.pyc\n")
    (repo / "probe.bzl").write_text("""
def _impl(ctx):
    ctx.actions.write(ctx.outputs.executable, "#!/bin/sh\\nexit 0\\n", is_executable = True)
probe_test = rule(implementation = _impl, test = True, attrs = {
    "srcs": attr.label_list(allow_files = True),
    "deps": attr.label_list(),
})
""")
    # The fixture exercises real query edges, including an intermediate generator
    # output. The unrelated c/docs tests establish that successful queries narrow.
    (repo / "BUILD.bazel").write_text("""
load(":probe.bzl", "probe_test")
filegroup(name = "inputs", srcs = glob(["src/**", "fixtures/**", "migrations/**", "generator/**"]))
genrule(name = "generated", srcs = [":inputs"], outs = ["generated.txt"], cmd = "touch $@")
probe_test(name = "a", srcs = ["tests/test_a.py", ":generated.txt"])
probe_test(name = "b", srcs = ["tests/sub/test_b.py"], deps = [":inputs"])
probe_test(name = "c", srcs = ["tests/test_c.py"])
probe_test(name = "docs", srcs = ["tests/test_docs_scan.py"])
probe_test(name = "a_slow", srcs = ["tests/test_a.py", ":generated.txt"])
""")
    modules = {
        path: {"targets": {"module": "//:" + label}}
        for path, label in (
            ("tests/test_a.py", "a"),
            ("tests/sub/test_b.py", "b"),
            ("tests/test_c.py", "c"),
            ("tests/test_docs_scan.py", "docs"),
        )
    }
    modules["tests/test_a.py"]["targets"]["slow"] = "//:a_slow"
    manifest = {
        "suites": {
            "module": {
                "marker": "not slow",
                "modules": {p: [] for p in modules},
                "targets": [m["targets"]["module"] for m in modules.values()],
            },
            "slow": {
                "marker": "slow",
                "modules": {"tests/test_a.py": []},
                "targets": ["//:a_slow"],
            },
        },
        "modules": modules,
    }
    (repo / impact.MANIFEST).write_text(json.dumps(manifest))
    for relative in (
        "fixtures/value.json",
        "src/pkg/prompt.md",
        "migrations/001.sql",
        "generator/input.json",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original fixture content sufficiently long for rename detection\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "pinned graph fixture")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def _impact_graph(*, omit_inputs=False):
    inputs = {
        "//:src/pkg/a.py",
        "//:src/pkg/__init__.py",
        "//:src/pkg/prompt.md",
        "//:fixtures/value.json",
        "//:migrations/001.sql",
        "//:generator/input.json",
    }
    return impact.Graph(
        {
            "//:inputs": set() if omit_inputs else inputs,
            "//:generated": {"//:inputs"},
            "//:generated.txt": {"//:generated"},
            "//:a": {"//:generated.txt"},
            "//:b": {"//:inputs"},
            "//:c": set(),
            "//:docs": set(),
            "//:a_slow": {"//:generated.txt"},
        },
        {"//:a", "//:b", "//:c", "//:docs", "//:a_slow"},
    )


def _impact_adapter(*, complete=True, modules=("tests/test_a.py", "tests/sub/test_b.py")):
    return FixedStaticImpact(modules, complete=complete)


async def test_impact_uses_pinned_heads_ignores_dirty_caller_and_binds_versions(tmp_path):
    from tests.selection_fixture_repo import git

    repo, base = _impact_repo(tmp_path)
    (repo / "src/pkg/a.py").write_text("def alpha(): return 4\n")
    git(repo, "commit", "-qam", "changed source")
    head = git(repo, "rev-parse", "HEAD").strip()
    (repo / "BUILD.bazel").write_text("poison caller BUILD\n")
    seen = []

    async def query(root, bazel, output_root):
        seen.append(await impact.pin(root, "HEAD"))
        assert "poison" not in (root / "BUILD.bazel").read_text()
        return _impact_graph()

    first = await impact.audit(
        repo,
        base,
        head,
        Path("unused"),
        ["module", "slow"],
        query_graph=query,
        static_adapter=_impact_adapter(),
    )
    second = await impact.audit(
        repo,
        base,
        head,
        Path("unused"),
        ["slow", "module"],
        query_graph=query,
        static_adapter=_impact_adapter(),
    )
    assert seen == [base, head, base, head]
    assert first == second
    assert first["observational"] and not first["authoritative"]
    assert not first["promotion_blocked"]
    assert first["suites"]["module"]["required_targets"] == ["//:a", "//:b"]
    assert first["suites"]["slow"]["required_targets"] == ["//:a_slow"]
    assert first["suites"]["slow"]["marker"] == "slow"
    assert first["identities"]["head"]["generator_registry"]["version"] == 1
    for field in ("graph_inputs_sha256", "graph_sha256", "lock_sha256", "selection_sha256"):
        assert len(first["identities"]["head"][field]) == 64
    assert first["base"] == base and first["head"] == head
    for body, reason in (
        ("import importlib\nimportlib.import_module(configured_name)\n", "unresolved_dynamic_load"),
        ("from importlib import import_module as load\nload(name)\n", "unresolved_dynamic_load"),
        ("from pathlib import Path\nPath(resource).read_text()\n", "unresolved_resource"),
        ("import src.missing_module\n", "unresolved_import"),
        (
            "from importlib.metadata import entry_points\nentry_points()\n",
            "unresolved_plugin_discovery",
        ),
        ("pytest_plugins = ['src.plugin']\n", "unresolved_plugin_discovery"),
    ):
        (repo / "BUILD.bazel").write_text(git(repo, "show", head + ":BUILD.bazel"))
        (repo / "src/pkg/a.py").write_text(body)
        git(repo, "commit", "-qam", "unresolved loader")
        changed = git(repo, "rev-parse", "HEAD").strip()
        unresolved = await impact.audit(
            repo,
            base,
            changed,
            Path("unused"),
            ["module"],
            query_graph=query,
            static_adapter=_impact_adapter(),
        )
        suite = unresolved["suites"]["module"]
        assert any(code.startswith(reason) for code in suite["blockers"])
        assert suite["required_targets"] == suite["eligible_targets"]
    for path, field in (
        (impact.LOCKS[0], "lock_sha256"),
        (impact.REGISTRY[0], "generator_registry"),
        (impact.MANIFEST, "manifest_sha256"),
    ):
        with (repo / path).open("a") as stream:
            stream.write("\n")
        git(repo, "commit", "-qam", "changed bound input")
        changed = git(repo, "rev-parse", "HEAD").strip()
        bound = await impact.audit(
            repo,
            base,
            changed,
            Path("unused"),
            ["module"],
            query_graph=query,
            static_adapter=_impact_adapter(),
        )
        assert bound["identities"]["head"][field] != first["identities"]["head"][field]
        assert bound["signature_sha256"] != first["signature_sha256"]


@pytest.mark.parametrize("failure", ["underdeclared", "unavailable", "static", "dirty_query"])
async def test_impact_failures_widen_whole_marker_suite(tmp_path, failure):
    from tests.selection_fixture_repo import git

    repo, base = _impact_repo(tmp_path)
    (repo / "src/pkg/a.py").write_text("def alpha(): return 4\n")
    git(repo, "commit", "-qam", "changed source")
    head = git(repo, "rev-parse", "HEAD").strip()

    async def query(root, bazel, output_root):
        if failure == "unavailable":
            raise ValueError("missing query evidence")
        if failure == "dirty_query":
            (root / "BUILD.bazel").write_text("changed during query")
        return _impact_graph(omit_inputs=failure == "underdeclared")

    if failure == "dirty_query":
        with pytest.raises(ValueError, match="snapshot identity changed"):
            await impact.audit(
                repo,
                base,
                head,
                Path("unused"),
                ["module"],
                query_graph=query,
                static_adapter=_impact_adapter(),
            )
        return
    report = await impact.audit(
        repo,
        base,
        head,
        Path("unused"),
        ["module"],
        query_graph=query,
        static_adapter=_impact_adapter(complete=failure != "static"),
    )
    suite = report["suites"]["module"]
    assert report["promotion_blocked"] and suite["widened"]
    assert suite["required_targets"] == suite["eligible_targets"]
    if failure == "underdeclared":
        assert "under_declared" in suite["blockers"]
        assert suite["missing_targets"] == ["//:a", "//:b"]


@pytest.mark.parametrize(
    "mutation,path",
    [
        ("edit", "fixtures/value.json"),
        ("edit", "src/pkg/prompt.md"),
        ("edit", "migrations/001.sql"),
        ("edit", "src/pkg/__init__.py"),
        ("edit", "generator/input.json"),
        ("delete", "src/pkg/a.py"),
        ("rename", "src/pkg/a.py"),
    ],
)
async def test_impact_real_query_mutation_matrix_on_pinned_heads(tmp_path, mutation, path):
    from tests.selection_fixture_repo import git

    executable = os.environ.get("AQ_BAZEL_IMPACT")
    if not executable:
        pytest.skip("set AQ_BAZEL_IMPACT to locked Bazel for the pinned-head mutation matrix")
    repo, base = _impact_repo(tmp_path)
    if mutation == "delete":
        (repo / path).unlink()
    elif mutation == "rename":
        (repo / path).rename(repo / "src/pkg/renamed.py")
    else:
        with (repo / path).open("a") as stream:
            stream.write("\n# mutation\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "mutation " + mutation)
    head = git(repo, "rev-parse", "HEAD").strip()
    report = await impact.audit(repo, base, head, Path(executable), ["module"])
    evidence = os.environ.get("AQ_BAZEL_IMPACT_EVIDENCE")
    if evidence:
        output = Path(evidence)
        output.mkdir(parents=True, exist_ok=True)
        (output / (mutation + "-" + path.replace("/", "_") + ".json")).write_text(
            json.dumps(report, indent=2)
        )
    suite = report["suites"]["module"]
    assert suite["graph_targets"] == ["//:a", "//:b"], report["diagnostics"] or suite
    assert {"//:a", "//:b"} <= set(suite["required_targets"])
    assert report["identities"]["base"]["head"] == base
    assert report["identities"]["head"]["head"] == head
    if mutation == "rename":
        assert suite["per_path"]["src/pkg/renamed.py"]["old_path"] == path
