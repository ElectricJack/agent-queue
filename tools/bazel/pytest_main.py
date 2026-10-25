"""Bazel module-test entry point; no ambient runtime or network fallback.

The parent owns H2's namespace helper and records H5's complete lifecycle receipt.
The isolated child invokes pytest serially with explicit plugins and H3's private
PostgreSQL context when declared by the generated module obligation.
"""

from __future__ import annotations

# ruff: noqa: E402 -- entry timestamp must precede launcher imports

import time

ENTRY_TIME = time.monotonic()  # includes imports, environment construction and cleanup

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import tomllib
from pathlib import Path

from tools.bazel import runtime as budgets
from tools.bazel import test_environment as isolation


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


class ObligationPlugin:
    """Validate the whole module before applying a workflow split's node set."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.collected = []

    def pytest_itemcollected(self, item):
        self.collected.append(budgets.normalize_nodeid(item.nodeid))

    def pytest_collection_modifyitems(self, session, config, items):
        import pytest

        if sorted(self.collected) != sorted(self.spec["module_nodes"]):
            raise pytest.UsageError("Bazel module collection drift; regenerate the suite manifest")
        expected = set(self.spec["nodes"])
        actual = {budgets.normalize_nodeid(item.nodeid) for item in items}
        if not expected or not expected <= actual:
            raise pytest.UsageError(
                "Bazel marker/node obligation is empty or missing collected nodes"
            )
        rejected = [item for item in items if budgets.normalize_nodeid(item.nodeid) not in expected]
        items[:] = [item for item in items if budgets.normalize_nodeid(item.nodeid) in expected]
        config.hook.pytest_deselected(items=rejected)


def pytest_child(spec: dict, output: Path) -> int:
    import pytest
    from tools.bazel.pytest_plugin import RuntimePlugin

    plugin = RuntimePlugin(
        output=output / "pytest.json",
        env=os.environ,
        target_tags=spec["tags"],
        shard_isolation=spec["shard_isolation"],
    )
    arguments = [
        spec["module"],
        "-o",
        "addopts=",
        "-o",
        "cache_dir=" + str(Path(os.environ["TMPDIR"]) / "pytest-cache"),
        "-m",
        spec["marker"],
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "anyio.pytest_plugin",
        "-p",
        "hypothesis.extra.pytestplugin",
        "--junitxml=" + os.environ["XML_OUTPUT_FILE"],
    ]
    code = int(pytest.main(arguments, plugins=[ObligationPlugin(spec), plugin]))
    sidecar = output / "pytest.json"
    if not sidecar.exists():
        return code or 1
    report = json.loads(sidecar.read_text())
    errors = budgets.validate_shards(spec["nodes"], [report])
    if errors:
        print("Bazel obligation verification failed: " + "; ".join(errors), file=sys.stderr)
        return code or 1
    return code


async def isolated_child(plan: dict) -> int:
    from tools.bazel.postgres_cluster import PostgresBinaries, postgres_cluster

    spec, root = plan["spec"], Path(plan["root"])
    os.chdir(plan["workspace"])
    # These are internal child settings, never copied from the worker's env.
    os.environ["AQ_TEST_PG_MODE"] = "pure"
    if spec["perf_strict"]:
        os.environ["AQ_PERF_STRICT"] = "1"
    if not spec["postgres"]:
        process = await asyncio.create_subprocess_exec(
            *plan["pytest_command"], env=dict(os.environ)
        )
        return await process.wait()
    runtime = isolation.Runtime(
        Path(plan["python"]),
        tuple(map(Path, plan["read_only"])),
        tuple(map(Path, plan["bin_dirs"])),
    )
    env = isolation.TestEnvironment(root, runtime, dict(os.environ), (root,))
    native = Path(plan["native"]) / "bin"
    binaries = PostgresBinaries(
        *(native / name for name in ("initdb", "postgres", "pg_ctl", "psql"))
    )
    async with postgres_cluster(env, binaries, tcp=True, socket_alias_root=Path("/tmp")) as cluster:
        os.environ.update(cluster.environ)
        # Pytest needs its own event loop for async fixtures; execute in a child,
        # keeping cluster lifecycle on this responsive event loop.
        process = await asyncio.create_subprocess_exec(*plan["pytest_command"], env=cluster.environ)
        try:
            return await process.wait()
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 1)
                except TimeoutError:
                    process.kill()
                    await process.wait()


def runfile(workspace: Path, relative: str) -> Path:
    # short_path uses ../<canonical-repository> for external artifacts.
    path = Path(os.path.normpath(workspace / relative))
    if not path.is_relative_to(workspace.parent):
        raise isolation.IsolationError("runtime file escapes the declared runfiles tree")
    if not path.is_file():
        raise isolation.IsolationError("declared runtime file is absent from runfiles")
    return path


def install_project_runtime(python: Path, workspace: Path, inputs: Path) -> Path:
    """Prepare a declared, target-private installation for Python subprocesses."""
    site = python.parent.parent / "lib/python3.12/site-packages"
    if not site.parent.is_dir():
        raise isolation.IsolationError(
            "declared interpreter lacks its Python 3.12 standard library"
        )
    site.mkdir(exist_ok=True)
    paths = Launch.import_paths(workspace, inputs)
    (site / "aq-test-inputs.pth").write_text("\n".join(paths) + "\n")
    project = tomllib.loads((workspace / "pyproject.toml").read_text())["project"]
    distribution = site / (
        project["name"].replace("-", "_") + "-" + project["version"] + ".dist-info"
    )
    distribution.mkdir(exist_ok=True)
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: "
        + project["name"]
        + "\nVersion: "
        + project["version"]
        + "\n"
    )
    entries = project.get("scripts", {})
    (distribution / "entry_points.txt").write_text(
        "[console_scripts]\n"
        + "".join(name + " = " + value + "\n" for name, value in sorted(entries.items()))
    )
    scripts = inputs / "aq-project-bin"
    scripts.mkdir()
    for name, value in entries.items():
        module, function = value.split(":", 1)
        script = scripts / name
        script.write_text(
            "#!"
            + str(python)
            + "\nfrom "
            + module
            + " import "
            + function
            + "\nraise SystemExit("
            + function
            + "())\n"
        )
        script.chmod(0o755)
    return scripts


class Launch:
    def __init__(self, spec: dict, runtime: dict, *, started: float, output: Path):
        self.spec, self.description = spec, runtime
        self.started, self.output = started, output
        self.temporary = None
        self.context = None
        self.environment = None
        self.process = None

    async def bootstrap(self):
        directory = Path(os.environ["TEST_TMPDIR"]).absolute()
        self.temporary = tempfile.TemporaryDirectory(prefix="aq-module-", dir=directory)
        base = Path(self.temporary.name)
        # Materialize only the declared runfiles. Following the symlinks into a
        # read-only mount would otherwise expose undeclared checkout/cache paths.
        source = Path(os.environ["TEST_SRCDIR"]).absolute()
        inputs = base / "inputs"
        inputs.mkdir()
        copied = 0
        for current, directories, files in os.walk(source, followlinks=True):
            directories[:] = sorted(d for d in directories if d != "__pycache__")
            destination = inputs / Path(current).relative_to(source)
            destination.mkdir(parents=True, exist_ok=True)
            for name in sorted(files):
                if name == "MANIFEST" or name.endswith(".pyc"):
                    continue
                shutil.copy2(Path(current) / name, destination / name)
                copied += 1
                if copied % 64 == 0:
                    await asyncio.sleep(0)
        workspace = inputs / os.environ["TEST_WORKSPACE"]
        python = runfile(workspace, self.description["python"])
        native = runfile(workspace, self.description["native_git"]).parent.parent
        node = runfile(workspace, self.description["node"])
        project_bin = install_project_runtime(python, workspace, inputs)
        # H1 must supply an ELF loader/libc closure and H2 must mount its ABI
        # locations. Never substitute ambient /lib or a host interpreter here.
        sysroot = native / "x86_64-conda-linux-gnu/sysroot"
        if not (sysroot / "lib64/ld-linux-x86-64.so.2").is_file():
            raise isolation.IsolationError(
                "locked native toolchain lacks declared ELF loader/libc sysroot"
            )
        read_only = (inputs,)
        runtime = isolation.Runtime(
            python,
            read_only,
            (project_bin, python.parent, native / "bin", node.parent),
            library_dirs=(native / "lib",),
            git_exec_path=native / "libexec/git-core",
            shell=native / "bin/bash",
            sysroot=sysroot,
            read_only_mappings=tuple(
                isolation.ReadOnlyMapping(sysroot / directory, Path("/") / directory)
                for directory in ("lib", "lib64", "usr/lib", "usr/lib64")
            ),
        )
        state = base / "state"
        state.mkdir()
        bazel = {
            name: os.environ[name]
            for name in isolation.BAZEL_VARIABLES
            if name in os.environ
            and name
            not in {
                "RUNFILES_MANIFEST_FILE",
                "JAVA_RUNFILES",
                "PYTHON_RUNFILES",
                "RUNFILES_DIR",
                "TEST_SRCDIR",
            }
        }
        bazel.update({"TEST_SRCDIR": str(inputs), "RUNFILES_DIR": str(inputs)})
        output_roots = {self.output}
        for name in isolation._OUTPUTS:
            if name in bazel:
                path = Path(bazel[name])
                output_roots.add(path if path.is_dir() else path.parent)
        # Collapse nested private Bazel output roots to disjoint mounts.
        roots = tuple(
            p
            for p in sorted(output_roots)
            if not any(p != other and p.is_relative_to(other) for other in output_roots)
        )
        self.context = isolation.test_environment(
            test_tmpdir=state, runtime=runtime, bazel=bazel, output_roots=roots
        )
        self.environment = self.context.__enter__()
        env = self.environment.child_env()
        env.update(
            {
                "GIT_AUTHOR_NAME": "AQ disposable test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "AQ disposable test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
        )
        plan = {
            "root": str(self.environment.root),
            "python": str(python),
            "read_only": list(map(str, read_only)),
            "bin_dirs": list(map(str, runtime.bin_dirs)),
            "workspace": str(workspace),
            "spec": self.spec,
            "native": str(native),
            "output": str(self.output),
        }
        child_plan = self.environment.root / "pytest-plan.json"
        child_plan.write_text(json.dumps(plan))
        startup = (
            f"import sys; sys.path[:0] = {self.import_paths(workspace, inputs)!r}; "
            "from tools.bazel.pytest_main import main; sys.exit(main())"
        )
        child_command = [str(python), "-I", "-c", startup, "--pytest-child", str(child_plan)]
        plan["pytest_command"] = child_command
        child_plan.write_text(json.dumps(plan))
        jail = base / "jail"
        jail.mkdir()
        namespace = {
            "jail": str(jail),
            "cwd": str(workspace),
            "env": env,
            "grace": 1,
            "read_only": list(map(str, read_only)),
            "writable": list(map(str, self.environment.writable)),
            "shell": str(runtime.shell),
            "read_only_mappings": [
                [str(mapping.source), str(mapping.target)]
                for mapping in runtime.read_only_mappings
            ],
            "command": [str(python), "-I", "-c", startup, "--isolated-child", str(child_plan)],
        }
        self.namespace_plan = base / "namespace.json"
        self.namespace_plan.write_text(json.dumps(namespace))
        self.helper_command = [
            str(python),
            "-I",
            str(workspace / "tools/bazel/test_environment.py"),
            "--namespace-init",
            str(self.namespace_plan),
        ]
        self.env = env

    @staticmethod
    def import_paths(workspace: Path, inputs: Path) -> list[str]:
        paths = [str(workspace), str(workspace / "packages/aq-client")]
        paths += [str(p) for p in sorted(inputs.glob("*/site-packages"))]
        return paths

    async def execute(self) -> int:
        self.process = await asyncio.create_subprocess_exec(
            *self.helper_command,
            env=self.env,
            start_new_session=True,
            close_fds=True,
            stdin=asyncio.subprocess.DEVNULL,
        )
        return await self.process.wait()

    async def cleanup(self):
        if self.process is not None and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1)
            except TimeoutError:
                self.kill()
                await self.process.wait()
        if self.context is not None:
            self.context.__exit__(None, None, None)
            self.context = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    def kill(self):
        # H2's helper contains private PID 1 via PDEATHSIG. Its death kills all
        # descendants, including detached daemon/PG processes in that namespace.
        if self.process is not None and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def run(spec: dict, description: dict, output: Path) -> int:
    launch = Launch(spec, description, started=ENTRY_TIME, output=output)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    receipt = await budgets.run_target(
        launch.bootstrap, launch.execute, launch.cleanup, launch.kill, started=ENTRY_TIME
    )
    write_json(output / "target.json", receipt)
    if receipt["errors"]:
        print("Bazel target failed: " + "; ".join(receipt["errors"]), file=sys.stderr)
    return receipt["exit_status"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--module-spec", type=Path)
    parser.add_argument("--isolated-child", type=Path)
    parser.add_argument("--pytest-child", type=Path)
    args = parser.parse_args()
    if args.pytest_child:
        plan = json.loads(args.pytest_child.read_text())
        os.chdir(plan["workspace"])
        if plan["spec"]["perf_strict"]:
            os.environ["AQ_PERF_STRICT"] = "1"
        return pytest_child(plan["spec"], Path(plan["output"]))
    if args.isolated_child:
        return asyncio.run(isolated_child(json.loads(args.isolated_child.read_text())))
    if args.spec is None or args.runtime is None:
        parser.error("--spec and --runtime are required")
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]).absolute()
    output.mkdir(parents=True, exist_ok=True)
    spec = json.loads(args.spec.read_text())
    spec["module_nodes"] = json.loads(args.module_spec.read_text())["nodes"]
    spec["nodes"] = [spec["module_nodes"][index] for index in spec["nodes"]]
    return asyncio.run(run(spec, json.loads(args.runtime.read_text()), output))


if __name__ == "__main__":
    sys.exit(main())
