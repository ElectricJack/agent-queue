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
