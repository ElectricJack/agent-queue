"""H1 lock/ABI boundaries and opt-in, admitted clean-root toolchain verification."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bazel_lock", ROOT / "tools/bazel/lock.py")
lock_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock_tool)


def test_committed_dependency_intent_and_lock():
    lock_tool.validate(ROOT)
    from packaging.tags import compatible_tags, cpython_tags

    module = ast.parse((ROOT / "MODULE.bazel").read_text())
    parse = next(
        node for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "pip"
        and node.func.attr == "parse"
    )
    args = {kw.arg: ast.literal_eval(kw.value) for kw in parse.keywords}
    assert args["download_only"] is True  # pip wheel does not accept --platform.
    assert {"--require-hashes", "--only-binary=:all:"} <= set(args["extra_pip_args"])
    platforms = [arg.removeprefix("--platform=") for arg in args["extra_pip_args"]
                 if arg.startswith("--platform=")]
    tags = {str(tag) for tag in cpython_tags((3, 12), platforms=platforms)}
    tags.update(str(tag) for tag in compatible_tags((3, 12), platforms=platforms))
    # The failed cryptography wheel uses cp311-abi3-manylinux_2_34_x86_64.
    assert "cp311-abi3-manylinux_2_34_x86_64" not in tags
    assert "cp311-abi3-manylinux_2_28_x86_64" in tags
    assert "cp311-abi3-manylinux_2_17_x86_64" in tags
    assert "cp312-cp312-manylinux2014_x86_64" in tags
    assert "py3-none-any" in tags
    assert "cp312-cp312-linux_x86_64" not in tags


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
    # Root BUILD evaluates these globs even for the isolated H1 smoke targets.
    for directory in ("migrations", "scripts", "docs/reference/playbook-commands"):
        shutil.copytree(
            ROOT / directory, destination / directory,
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

    path = tmp_path / lock_tool.LOCK_PATH
    lock = json.loads(path.read_text())
    lock["pip"]["python_platform"] = "x86_64-manylinux_2_34"
    path.write_text(json.dumps(lock))
    (tmp_path / "MODULE.bazel").write_text(lock_tool.module_source(tmp_path, lock))
    with pytest.raises(ValueError, match="resolver platform must match"):
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
    return targets.hydrate_manifest(json.loads((ROOT / "tools/bazel/test_suites.json").read_text()))


# azure-cascade-11: full-graph regeneration measured ~178s under load; allow headroom.
@pytest.mark.timeout(300)
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


def test_dependency_areas_match_catalogue_names_and_module_globs():
    modules = {
        "tests/test_knowledge_budget.py": {"areas": ["knowledge"]},
        "tests/test_knowledge_context.py": {"areas": ["knowledge", "sessions"]},
        "tests/test_sessions.py": {"areas": ["sessions"]},
    }
    expected = set(modules) - {"tests/test_sessions.py"}
    assert targets.select_areas(modules, ["knowledge"]) == expected
    assert targets.select_areas(modules, ["tests/test_knowledge_*.py"]) == expected
    assert targets.select_areas(modules, ["knowledge", "sessions"]) == set(modules)
    with pytest.raises(ValueError, match="matches no test modules"):
        targets.select_areas(modules, ["unknown"])


def _fine_graph_fixture(root):
    contents = {
        "BUILD.bazel": "",
        "tools/bazel/BUILD.bazel": "",
        "src/__init__.py": "",
        "src/used.py": "VALUE = 1\n",
        "src/unrelated.py": "VALUE = 2\n",
        "tests/conftest.py": (
            "import pytest\n"
            "@pytest.fixture\n"
            "def selected():\n"
            "    from src.used import VALUE\n"
            "    return VALUE\n"
            "@pytest.fixture\n"
            "def unused():\n"
            "    from src.unrelated import VALUE\n"
            "    return VALUE\n"
        ),
        "tests/test_behavior.py": "def test_behavior(selected): assert selected == 1\n",
    }
    for relative, content in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return {"tests/test_behavior.py": {}}


def _render_graph(root, modules, fine_grained):
    libraries, discovered = targets.graph(root, modules, fine_grained=fine_grained)
    entries = {
        path: {**info, "target": targets.target(path, "module"), "nodes": [], "targets": {}}
        for path, info in discovered.items()
    }
    return targets.render(root, {
        "schema_version": 1, "libraries": libraries, "modules": entries, "collection": {"nodes": []},
        "suites": {}, "aliases": {},
    })


def test_tracked_inputs_ignore_local_artifacts_and_survive_archive(tmp_path):
    modules = _fine_graph_fixture(tmp_path)
    tracked = {
        ".gitignore": "packages/aq-ts-client/src/index.ts\ntests/test_ignored.py\n",
        "packages/aq-ts-client/src/tracked.ts": "export const value = 1;\n",
        "tools/lookup/data.json": "{}\n",
        "scripts/bazel/test_dependency_inputs.json": json.dumps({
            "tests/test_behavior.py": {"data": ["packages/aq-ts-client/src/*.ts"]},
        }),
    }
    for relative, content in tracked.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (tmp_path / "tests/test_behavior.py").write_text(
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        "def test_behavior(selected):\n"
        "    assert selected == 1\n"
        "    (ROOT / 'tools' / 'lookup' / 'data.json').read_text()\n"
        "    list((ROOT / 'packages' / 'aq-ts-client' / 'src').glob('*.ts'))\n"
        "    open('packages/aq-ts-client/src/index.ts')\n"
    )
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    subprocess.check_call(["git", "add", "."], cwd=tmp_path)
    baseline = _render_graph(tmp_path, modules, set(modules))
    graph = targets.graph(tmp_path, modules, fine_grained=set(modules))
    fingerprints = targets.collection_inputs(tmp_path)
    inventory = targets.load_inventory(ROOT)
    baseline_sources = inventory.Collector(tmp_path).source_files
    def collect():
        script = (
            "import json, sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "import pytest; from scripts.bazel.inventory import Collector; "
            "root = Path(sys.argv[2]); plugin = Collector(root); "
            "code = pytest.main([str(root / 'tests'), '--collect-only', '-q', "
            "'-o', 'addopts=', '--confcutdir', str(root)], plugins=[plugin]); "
            "print('COLLECTION_JSON=' + json.dumps(plugin.document, sort_keys=True)); "
            "raise SystemExit(code)"
        )
        output = subprocess.check_output(
            [sys.executable, "-c", script, str(ROOT), str(tmp_path)], cwd=tmp_path,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, text=True,
        )
        return json.loads(next(line.removeprefix("COLLECTION_JSON=") for line in output.splitlines()
                               if line.startswith("COLLECTION_JSON=")))

    collected = collect()
    assert [node["nodeid"] for node in collected["nodes"]] == [
        "tests/test_behavior.py::test_behavior",
    ]
    assert "packages/aq-ts-client/src/tracked.ts" in graph[1]["tests/test_behavior.py"]["data_files"]
    for relative in (
        "packages/aq-ts-client/src/index.ts", "src/local.py", "tests/test_local.py",
        "tests/test_ignored.py", "tests/local/test_new.py", "tools/lookup/BUILD.bazel",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise RuntimeError('local artifact must not be read')\n")
    assert _render_graph(tmp_path, modules, set(modules)) == baseline
    assert targets.graph(tmp_path, modules, fine_grained=set(modules)) == graph
    assert targets.collection_inputs(tmp_path) == fingerprints
    plugin = inventory.Collector(tmp_path)
    assert plugin.source_files == baseline_sources
    assert plugin.pytest_ignore_collect(tmp_path / "tests/test_local.py", None) is True
    assert plugin.pytest_ignore_collect(tmp_path / "tests/test_ignored.py", None) is True
    assert plugin.pytest_ignore_collect(tmp_path / "tests/local", None) is True
    assert plugin.pytest_ignore_collect(tmp_path / "tests/test_behavior.py", None) is None
    assert collect() == collected
    assert targets.file_label("tools/lookup/data.json", {}, tmp_path) == "//:tools/lookup/data.json"
    # Fresh invocations observe index changes; no process-global snapshot cache.
    subprocess.check_call(["git", "add", "src/local.py"], cwd=tmp_path)
    assert "src/local.py" in targets.source_files(tmp_path)
    subprocess.check_call(["git", "rm", "--cached", "-q", "src/local.py"], cwd=tmp_path)
    shutil.rmtree(tmp_path / ".git")
    snapshot = tmp_path / ".aq/bazel-source-files.json"
    snapshot.parent.mkdir()
    snapshot.write_text(json.dumps({"schema_version": 1, "files": sorted(baseline_sources)}))
    assert _render_graph(tmp_path, modules, set(modules)) == baseline
    assert targets.collection_inputs(tmp_path) == fingerprints
    assert inventory.Collector(tmp_path).source_files == baseline_sources


def test_fine_graph_only_carries_used_fixture_sources(tmp_path):
    modules = _fine_graph_fixture(tmp_path)
    _, graph = targets.graph(tmp_path, modules, fine_grained=set(modules))
    info = graph["tests/test_behavior.py"]
    assert info["source_files"] == ["src/__init__.py", "src/used.py"]
    assert "tests/conftest.py" in info["test_sources"]
    assert info["libraries"] == []
    assert not info["conservative"]
    rendered = _render_graph(tmp_path, modules, set(modules))
    build = rendered["tests/BUILD.bazel"]
    assert '"//src:used.py"' in build
    assert '"//src:unrelated.py"' not in build
    assert '"//src:library"' not in build
    assert '":fixtures"' not in build
    assert '"//:python_documentation_data"' not in build


def test_shared_inputs_preserve_expanded_module_vectors_and_obligations(tmp_path):
    modules = _fine_graph_fixture(tmp_path)
    for name, body in {
        "second": "def test_behavior(selected): assert selected == 1\n",
        "third": "from src.unrelated import VALUE\ndef test_behavior(): assert VALUE == 2\n",
    }.items():
        path = f"tests/test_{name}.py"
        (tmp_path / path).write_text(body)
        modules[path] = {}
    libraries, discovered = targets.graph(tmp_path, modules, fine_grained=set(modules))
    nodes = []
    entries = {}
    suites = {
        "module": {"modules": {}, "marker": "", "perf_strict": False},
        "default": {"modules": {}, "marker": "not slow", "perf_strict": False},
        "perf": {"modules": {}, "marker": "perf", "perf_strict": True},
    }
    for path, info in discovered.items():
        indices = list(range(len(nodes), len(nodes) + 2))
        nodes.extend({"nodeid": path + suffix} for suffix in ("::test_a", "::test_b[p]"))
        entries[path] = {
            **info, "target": targets.target(path, "module"), "nodes": indices,
            "targets": {suite: targets.target(path, suite) for suite in suites},
        }
        for suite, selected in (("module", indices), ("default", indices[:1]), ("perf", [])):
            suites[suite]["modules"][path] = selected
    manifest = {
        "schema_version": 1, "libraries": libraries, "modules": entries, "collection": {"nodes": nodes},
        "suites": suites, "aliases": {},
    }
    original = targets.render(tmp_path, manifest, factor_inputs=False)
    factored = targets.render(tmp_path, manifest)
    assert factored == targets.render(tmp_path, manifest)
    assert {k: v for k, v in original.items() if k != "tests/BUILD.bazel"} == {
        k: v for k, v in factored.items() if k != "tests/BUILD.bazel"
    }

    def invocations(build):
        groups, consumers = {}, {}
        for expression in ast.parse(build).body:
            if not isinstance(expression, ast.Expr) or not isinstance(expression.value, ast.Call):
                continue
            call = expression.value
            if not isinstance(call.func, ast.Name):
                continue
            if call.func.id == "aq_module_tests":
                values = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
                consumers[values["module"]] = values
            elif call.func.id == "filegroup":
                name = next(kw.value.value for kw in call.keywords if kw.arg == "name")
                if name.startswith("shared_inputs_"):
                    groups[":" + name] = ast.literal_eval(next(
                        kw.value for kw in call.keywords if kw.arg == "srcs"
                    ))
        return groups, consumers

    _, before = invocations(original["tests/BUILD.bazel"])
    groups, after = invocations(factored["tests/BUILD.bazel"])
    assert groups
    assert sum(len(info["data"]) for info in after.values()) < sum(
        len(info["data"]) for info in before.values()
    )
    for info in after.values():
        info["data"] = sorted({leaf for value in info["data"] for leaf in groups.get(value, [value])})
    # Compare complete invocation metadata as well as every expanded input vector:
    # parametrized node IDs, arm selection, markers, manual tags and perf activation.
    assert before == after
    reordered = dict(reversed(list(entries.items())))
    assert targets.shared_module_inputs({path: info["data"] for path, info in after.items()}) == (
        targets.shared_module_inputs({path: after[path]["data"] for path in reordered})
    )


def test_generated_exports_preserve_authored_public_exports(tmp_path):
    modules = _fine_graph_fixture(tmp_path)
    authored = 'exports_files(["openapi.json"], visibility = ["//visibility:public"])\n'
    (tmp_path / "BUILD.bazel").write_text(
        authored + '# BEGIN B1 PYTHON INPUT EXPORTS\n'
        'exports_files(["openapi.json", "obsolete.txt"])\n'
        '# END B1 PYTHON INPUT EXPORTS\n'
    )
    (tmp_path / "openapi.json").write_text("{}")
    (tmp_path / "tests/test_behavior.py").write_text(
        "from pathlib import Path\ndef test_behavior(): assert Path('openapi.json').read_text()\n"
    )
    rendered = _render_graph(tmp_path, modules, set(modules))
    assert '"//:openapi.json"' in rendered["tests/BUILD.bazel"]
    assert authored in rendered["BUILD.bazel"]
    assert 'load("//tools/bazel:python_data.bzl", "python_repository_data")' in rendered["BUILD.bazel"]
    assert "obsolete.txt" not in rendered["BUILD.bazel"]
    assert rendered["BUILD.bazel"].count('exports_files(["openapi.json"]') == 1
    assert targets.authored_exports(rendered["BUILD.bazel"]) == {"openapi.json"}


def test_interned_manifest_hashes_each_distinct_input_list_once(monkeypatch):
    shared = ["src/café.py", "src/a.py", "src/a.py"]
    manifest = {"schema_version": 1, "modules": {
        "tests/test_a.py": {"source_files": shared, "data_files": list(shared)},
        "tests/test_b.py": {"source_files": list(shared), "test_sources": []},
    }}
    expected = targets.intern_manifest(manifest)
    encoded = []
    original = targets.json.dumps

    def dumps(value, *args, **kwargs):
        if isinstance(value, list) and kwargs.get("separators") == (",", ":"):
            encoded.append(tuple(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(targets.json, "dumps", dumps)
    assert targets.intern_manifest(manifest) == expected
    assert encoded == [tuple(shared), ()]
    assert targets.hydrate_manifest(expected) == manifest


def test_generate_reads_existing_manifest_once_and_validates_references(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/selection_catalogue.json").write_text('{"modules": {}}')
    manifest_path = tmp_path / targets.MANIFEST
    manifest_path.parent.mkdir(parents=True)
    encoded = targets.intern_manifest({
        "schema_version": 1, "collection": {"source_files": {}},
        "modules": {"tests/test_a.py": {"source_files": ["src/a.py"]}},
    })
    manifest_path.write_text(json.dumps(encoded))
    sentinel = {"rendered": "unchanged"}
    monkeypatch.setattr(targets, "collection_inputs", lambda root: {})
    monkeypatch.setattr(targets, "graph", lambda *args: ({}, {}))
    monkeypatch.setattr(targets, "make_manifest", lambda *args: {})
    monkeypatch.setattr(targets, "render", lambda *args: sentinel)
    reads = []
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == manifest_path:
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert targets.generate(tmp_path) is sentinel
    assert reads == [manifest_path]
    encoded["modules"]["tests/test_a.py"]["source_files"] = {"input_set": "missing"}
    manifest_path.write_text(json.dumps(encoded))
    with pytest.raises(ValueError, match="unknown dependency set reference"):
        targets.generate(tmp_path)


def test_interned_manifest_dependency_arrays_round_trip_exactly():
    shared = ["src/a.py", "src/b.py"]
    manifest = {
        "schema_version": 1,
        "collection": {"nodes": [{
            "nodeid": "tests/test_x.py::test_a[p]",
            "keywords": ["__AQ_B1_NODE_TABLE__", "__AQ_B1_INPUT_TABLE_0__"],
        }]},
        "example_metadata": {"__AQ_B1_INPUT_TABLE_1__": "__AQ_B1_NODE_TABLE_1__"},
        "modules": {"tests/test_x.py": {
            "source_files": shared, "test_sources": [],
            "other_sources": ['scripts/quoted"name.py', "scripts/café.py"], "data_files": shared,
        }},
    }
    serialized = targets.manifest_json(manifest)
    encoded = json.loads(serialized)
    assert encoded["schema_version"] == 2
    info = encoded["modules"]["tests/test_x.py"]
    assert info["source_files"] == info["data_files"]
    assert len(encoded["dependency_sets"]) == 3
    assert targets.hydrate_manifest(encoded) == manifest
    assert targets.hydrate_manifest(manifest) == manifest
    assert '["src/a.py","src/b.py"]' in serialized
    assert targets.manifest_json(encoded) == serialized


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_graph_restores_garbage_collection_policy(tmp_path, monkeypatch, enabled, fails):
    gc = targets.gc
    original = gc.isenabled()
    modules = {"tests/test_behavior.py": {}}
    selected = set(modules)

    def build(root, received_modules, fine_grained):
        assert (root, received_modules, fine_grained) == (tmp_path, modules, selected)
        assert not gc.isenabled()
        if fails:
            raise ValueError("invalid input")
        return {}, {}

    monkeypatch.setattr(targets, "_graph", build)
    try:
        (gc.enable if enabled else gc.disable)()
        if fails:
            with pytest.raises(ValueError, match="invalid input"):
                targets.graph(tmp_path, modules, fine_grained=selected)
        else:
            assert targets.graph(tmp_path, modules, fine_grained=selected) == ({}, {})
        assert gc.isenabled() == enabled
    finally:
        (gc.enable if original else gc.disable)()


def test_directory_data_is_independent_of_worker_root(tmp_path):
    outputs = []
    for root in (tmp_path / "plain", tmp_path / ".aq/worktrees/proof"):
        modules = _fine_graph_fixture(root)
        (root / "tests/test_behavior.py").write_text(
            "from pathlib import Path\n"
            "def test_behavior(): assert Path('src/').is_dir()\n"
        )
        (root / "src/prompt.md").write_text("prompt bytes\n")
        (root / "src/BUILD.bazel").write_text("")
        for directory in (".aq", ".git", ".venv", "__pycache__", "node_modules"):
            skipped = root / "src" / directory
            skipped.mkdir()
            (skipped / "ignored.txt").write_text("excluded\n")
        _, graph = targets.graph(root, modules, fine_grained=set(modules))
        data = graph["tests/test_behavior.py"]["data_files"]
        assert "src/prompt.md" in data
        assert not any(path.endswith("ignored.txt") for path in data)
        assert "src/BUILD.bazel" not in data
        outputs.append(_render_graph(root, modules, set(modules)))
        assert '"//src:prompt.md"' in outputs[-1]["tests/BUILD.bazel"]
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("fine", [False, True])
def test_documentation_read_by_shared_helper_reaches_consumers(tmp_path, fine):
    modules = _fine_graph_fixture(tmp_path)
    (tmp_path / "tests/doc_helper.py").write_text(
        "from pathlib import Path\n"
        "def text(): return Path('docs/guide.md').read_text()\n"
    )
    (tmp_path / "tests/test_behavior.py").write_text(
        "from tests.doc_helper import text\ndef test_behavior(): assert text()\n"
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("instructions\n")
    selected = set(modules) if fine else set()
    _, graph = targets.graph(tmp_path, modules, fine_grained=selected)
    assert graph["tests/test_behavior.py"]["docs"]
    output = _render_graph(tmp_path, modules, selected)
    assert '"//:python_documentation_data"' in output["tests/BUILD.bazel"]
    repository_data, documentation_data = output["tools/bazel/python_data.bzl"].split(
        'name = "python_documentation_data"', 1
    )
    assert '"docs/**"' not in repository_data
    assert '"docs/**"' in documentation_data


def test_documentation_mentions_do_not_make_a_reader(tmp_path):
    modules = _fine_graph_fixture(tmp_path)
    (tmp_path / "tests/test_behavior.py").write_text(
        '\"\"\"Design reference: docs/guide.md.\"\"\"\n'
        "def test_behavior(selected): assert selected == 1\n"
    )
    _, graph = targets.graph(tmp_path, modules, fine_grained=set(modules))
    assert not graph["tests/test_behavior.py"]["docs"]


@pytest.mark.parametrize("build_name", ["BUILD", "BUILD.bazel"])
def test_fine_file_labels_respect_nested_bazel_packages(tmp_path, build_name):
    modules = _fine_graph_fixture(tmp_path)
    for folder in ("tools/bazel/smoke", "scripts/helpers"):
        directory = tmp_path / folder
        directory.mkdir(parents=True)
        (directory / build_name).write_text("")
        (directory / "helper.py").write_text("VALUE = 1\n")
    (tmp_path / "scripts/root_helper.py").write_text("VALUE = 1\n")
    (tmp_path / "tests/test_behavior.py").write_text(
        "from tools.bazel.smoke.helper import VALUE\n"
        "from scripts.helpers import helper\n"
        "from scripts import root_helper\n"
        "def test_behavior(): assert VALUE == helper.VALUE == root_helper.VALUE\n"
    )
    output = _render_graph(tmp_path, modules, set(modules))
    build = output["tests/BUILD.bazel"]
    assert '"//tools/bazel/smoke:helper.py"' in build
    assert '"//scripts/helpers:helper.py"' in build
    assert '"//:scripts/root_helper.py"' in build
    for package in ("tools/bazel/smoke", "scripts/helpers"):
        assert '"helper.py"' in output[f"{package}/{build_name}"].split("exports_files(")[1]
    assert '"scripts/root_helper.py"' in output["BUILD.bazel"].split("exports_files(")[1]


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
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800, check=False)
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


@pytest.mark.parametrize("use_upstream", [False, True])
def test_dashboard_generation_preserves_declared_openapi_provenance(
    tmp_path, monkeypatch, use_upstream,
):
    generate = _generator_module("generate")
    committed = tmp_path / "openapi.json"
    committed.write_text('{"info": {"version": "committed"}}')
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "openapi.json").write_text('{"info": {"version": "regenerated"}}')
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps([[str(committed), "openapi.json"]]))
    commands = []

    def run(command, root, env):
        commands.append(command)
        # The existing npm --from-file path must read the declared spec, even
        # when a developer checkout contains a different generated client.
        spec = json.loads((root / "openapi.json").read_text())
        generated = root / "packages/aq-ts-client/src"
        generated.mkdir(parents=True)
        (generated / "index.ts").write_text(spec["info"]["version"])
        return ""

    monkeypatch.setattr(generate, "run", run)
    output = tmp_path / "output"
    generate.execute(generate.argparse.Namespace(
        inputs=inputs, native=tmp_path / "native", node=tmp_path / "node",
        npm=tmp_path / "npm", ruff=tmp_path / "ruff", kind="typescript",
        upstream=upstream if use_upstream else None, output=output, manifest=None,
    ))
    assert (output / "packages/aq-ts-client/src/index.ts").read_text() == (
        "regenerated" if use_upstream else "committed"
    )
    assert len(commands) == 1
    assert commands[0][1:] == ["scripts/regenerate-ts-client.sh", "--from-file"]


def test_dashboard_generation_refuses_absent_declared_openapi(tmp_path):
    generate = _generator_module("generate")
    inputs = tmp_path / "inputs.json"
    inputs.write_text("[]")
    with pytest.raises(ValueError, match="declared OpenAPI"):
        generate.execute(generate.argparse.Namespace(
            inputs=inputs, native=tmp_path / "native", node=tmp_path / "node",
            npm=tmp_path / "npm", ruff=tmp_path / "ruff", kind="typescript",
            upstream=None, output=tmp_path / "output", manifest=None,
        ))


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
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800, check=False)
            (base / f"{command[0]}.log").write_text(result.stdout)
            assert result.returncode == 0, result.stdout
        binary = subprocess.check_output(prefix + ["info", "bazel-bin"], cwd=checkout,
                                         env=env, text=True).strip()
        binary = Path(binary)
        actual = json.loads((binary / "tools/bazel/dashboard_typescript.manifest.json").read_text())
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
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600, check=False)
                (base / f"npm-{index}.log").write_text(result.stdout)
                assert result.returncode == 0, result.stdout
            ts = manifest(reference / "packages/aq-ts-client/src")
            normalized = {p.removeprefix("packages/aq-ts-client/src/"): h for p, h in actual.items()}
            assert ts == normalized
            assert manifest(reference / "dashboard/dist") == build
    assert manifests[0] and manifests[0] == manifests[1]
    assert builds[0] and builds[0] == builds[1]


_provision_spec = importlib.util.spec_from_file_location(
    "bazel_provision", ROOT / "tools/bazel/provision.py"
)
provision_tool = importlib.util.module_from_spec(_provision_spec)
_provision_spec.loader.exec_module(provision_tool)


def _fake_bazel_lock(tmp_path, payload: bytes) -> Path:
    import hashlib

    lock = tmp_path / "toolchains.lock.json"
    lock.write_text(json.dumps({"bazel": {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "url": "https://releases.example/8.3.1/release/bazel-8.3.1-linux-x86_64",
        "version": "8.3.1",
    }}))
    return lock


def test_provisioned_bazel_pin_matches_bazelversion():
    pin = provision_tool.pinned_bazel()
    assert pin["version"] == (ROOT / ".bazelversion").read_text().strip()
    assert re.fullmatch(r"[a-f0-9]{64}", pin["sha256"])


@pytest.mark.skipif(
    (sys.platform, platform.machine()) != ("linux", "x86_64"), reason="Linux x86_64 only"
)
def test_provision_installs_verified_read_only_bazel_once(tmp_path):
    payload = b"#!/bin/sh\necho bazel 8.3.1\n"
    lock = _fake_bazel_lock(tmp_path, payload)
    fetched = []

    def fetch(url, destination):
        fetched.append(url)
        destination.write_bytes(payload)

    data_dir = tmp_path / "data"
    executable = provision_tool.provision(data_dir, fetch=fetch, lock=lock)
    root = data_dir / "toolchain/bazel-8.3.1-linux-x86_64"
    assert executable == root / "bin/bazel"
    assert executable.read_bytes() == payload
    for path in (root, root / "bin", executable):
        assert not path.stat().st_mode & 0o222, path
    assert os.access(executable, os.X_OK)
    # A verified install is reused without another download.
    assert provision_tool.provision(data_dir, fetch=fetch, lock=lock) == executable
    assert len(fetched) == 1


@pytest.mark.skipif(
    (sys.platform, platform.machine()) != ("linux", "x86_64"), reason="Linux x86_64 only"
)
def test_provision_refuses_checksum_mismatch_and_altered_install(tmp_path):
    lock = _fake_bazel_lock(tmp_path, b"approved bytes")
    data_dir = tmp_path / "data"
    with pytest.raises(provision_tool.ProvisionError, match="nothing was installed"):
        provision_tool.provision(
            data_dir, fetch=lambda _url, dest: dest.write_bytes(b"substituted"), lock=lock
        )
    assert not (data_dir / "toolchain/bazel-8.3.1-linux-x86_64").exists()

    root = provision_tool.provision(
        data_dir, fetch=lambda _url, dest: dest.write_bytes(b"approved bytes"), lock=lock
    ).parents[1]
    binary = root / "bin/bazel"
    root.chmod(0o755)
    (root / "bin").chmod(0o755)
    binary.chmod(0o755)
    binary.write_bytes(b"tampered")
    with pytest.raises(provision_tool.ProvisionError, match="does not hold the pinned"):
        provision_tool.provision(data_dir, fetch=lambda *_: None, lock=lock)
