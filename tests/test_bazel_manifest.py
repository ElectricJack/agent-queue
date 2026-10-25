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
    ):
        shutil.copy2(ROOT / path, destination / path)
    shutil.copytree(ROOT / "tools/bazel", destination / "tools/bazel")
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
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
            )
            assert result.returncode != 0 and "checksum" in result.stdout.lower(), result.stdout
            print("Bazel missing-lock and corrupted-archive negative checks: OK")


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
