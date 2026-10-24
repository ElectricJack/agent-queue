"""Exact parity evidence, real pytest sidecars and deliberately incomplete pairs."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "bazel_parity", ROOT / "scripts/bazel/compare_results.py"
)
parity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parity)


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure artifact/plugin checks allocate no test databases."""


def write_json(root, name, payload):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    return {"path": name, "sha256": parity.digest(path.read_bytes())}


def phase(result="pass", *, strict=None, reason=None):
    raw = {
        "pass": "passed",
        "fail": "failed",
        "error": "failed",
        "skip": "skipped",
        "xfail": "skipped",
        "xpass": "failed" if strict else "passed",
    }[result]
    return {
        "outcome": raw,
        "result": result,
        "xfail_strict": strict,
        "reason": reason,
        "duration": 0.001,
    }


def node(name="test_one", result="pass", *, strict=None, reason=None, parameter=None):
    nodeid = "tests/test_example.py::" + name + (f"[{parameter}]" if parameter is not None else "")
    return {
        "nodeid": nodeid,
        "parameter_id": parameter,
        "outcome": result,
        "phases": {
            "setup": phase(),
            "call": phase(result, strict=strict, reason=reason),
            "teardown": phase(),
        },
    }


def junit(root, name, nodes):
    xml = ET.Element("testsuites")
    suite = ET.SubElement(xml, "testsuite")
    for (nodeid, parameter, tags), count in parity.junit_expected(nodes).items():
        for _ in range(count):
            case = ET.SubElement(suite, "testcase", name=nodeid, classname="tests.test_example")
            props = ET.SubElement(case, "properties")
            ET.SubElement(props, "property", name="aq_nodeid", value=nodeid)
            ET.SubElement(props, "property", name="aq_parameter_id", value=json.dumps(parameter))
            for tag in tags:
                ET.SubElement(case, tag, message="reason retained in sidecar")
    path = root / name
    path.write_bytes(ET.tostring(xml))
    return {"path": name, "sha256": parity.digest(path.read_bytes())}


def evidence(root, *, nodes=None, count=1, dashboard=False):
    root.mkdir(parents=True, exist_ok=True)
    nodes = nodes if nodes is not None else [node(parameter="opaque/a::b\\c")]
    expected = [n["nodeid"] for n in nodes]
    plan_suite = {"marker": "not slow", "expected_nodes": expected}
    target = {"id": "example", "expected_nodes": expected, "shard_count": count, "shards": []}
    for index in range(count):
        selected = parity.Shard(index, count).select(expected)
        members = [n for n in nodes if n["nodeid"] in selected]
        report = {
            "schema_version": 1,
            "shard": {"index": index, "count": count},
            "collected": expected,
            "selected": selected,
            "nodes": members,
            "errors": [],
            "collection_errors": [],
            "exit_status": int(
                any(p["outcome"] == "failed" for n in members for p in n["phases"].values())
            ),
        }
        target["shards"].append(
            {
                "index": index,
                "sidecar": write_json(root, f"outcomes-{index}.json", report),
                "junit": junit(root, f"junit-{index}.xml", members),
                "lifecycle": write_json(
                    root,
                    f"target-{index}.json",
                    {
                        "schema_version": 1,
                        "budget_seconds": 300.0,
                        "elapsed": 1.0,
                        "phases": {"bootstrap": 0.1, "pytest": 0.5, "cleanup": 0.1},
                        "errors": [],
                        "exit_status": report["exit_status"],
                    },
                ),
            }
        )
    manifest = {
        "schema_version": 1,
        "context": {
            "sha": "a" * 40,
            "platform": "linux-x86_64-glibc-2.39",
            **{field: "b" * 64 for field in parity.CONTEXT_FIELDS[2:]},
        },
        "required_suites": ["default_1"],
        "suites": {"default_1": {"marker": "not slow", "targets": [target]}},
        "invocation": {"id": root.name, "timestamp": "reporting only"},
    }
    plan = {"schema_version": 1, "suites": {"default_1": plan_suite}}
    if dashboard:
        manifest["required_suites"].append("dashboard_build")
        manifest["suites"]["dashboard_build"] = {
            "marker": "",
            "exit_status": 0,
            "artifacts": write_json(
                root, "artifacts.json", [{"path": "dist/main.js", "sha256": "c" * 64}]
            ),
        }
        plan["suites"]["dashboard_build"] = {"kind": "dashboard", "marker": ""}
    manifest["suite_manifest"] = write_json(root, "plan.json", plan)
    write_json(root, "manifest.json", manifest)
    return manifest


@pytest.fixture
def pair(tmp_path):
    legacy, bazel = tmp_path / "legacy-results", tmp_path / "bazel-results"
    evidence(legacy)
    evidence(bazel)
    return legacy, bazel


def mutate(root, name, change, *, shard=0):
    manifest = json.loads((root / "manifest.json").read_text())
    if name == "manifest":
        change(manifest)
    else:
        target = manifest["suites"]["default_1"]["targets"][0]
        ref = target["shards"][shard][name]
        value = json.loads((root / ref["path"]).read_text())
        change(value)
        target["shards"][shard][name] = write_json(root, ref["path"], value)
    write_json(root, "manifest.json", manifest)


def test_complete_pair_and_cache_digest_ignores_invocation(pair):
    legacy, bazel = pair
    before = parity.compare(legacy, bazel)
    assert before["success"], before
    mutate(bazel, "manifest", lambda m: m.update(invocation={"timestamp": "new", "cache": "disk"}))
    after = parity.compare(legacy, bazel)
    assert after["success"] and after["result_digests"] == before["result_digests"]
    assert after["invocations"] != before["invocations"]


@pytest.mark.parametrize("field", parity.CONTEXT_FIELDS)
def test_every_exact_candidate_binding_is_required_and_compared(pair, field):
    legacy, bazel = pair
    mutate(
        bazel, "manifest", lambda m: m["context"].update({field: "c" * len(m["context"][field])})
    )
    result = parity.compare(legacy, bazel)
    assert not result["success"] and any(field in error for error in result["errors"])
    mutate(bazel, "manifest", lambda m: m["context"].pop(field))
    assert not parity.compare(legacy, bazel)["success"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["nodes"].clear(),
        lambda r: r["nodes"].append(copy.deepcopy(r["nodes"][0])),
        lambda r: r["nodes"][0].update(nodeid="tests/test_example.py::test_stale"),
        lambda r: r["nodes"][0].update(parameter_id="changed"),
        lambda r: r["nodes"][0]["phases"].pop("teardown"),
        lambda r: r["nodes"][0]["phases"].pop("call"),
        lambda r: r["nodes"][0]["phases"]["call"].pop("xfail_strict"),
        lambda r: r["collected"].append("tests/test_example.py::omitted"),
        lambda r: r["selected"].clear(),
        lambda r: r.update(exit_status=2),
        lambda r: r["errors"].append("interrupted before teardown"),
        lambda r: r["collection_errors"].append(
            {"nodeid": "tests/test_bad.py", "reason": "bad import"}
        ),
    ],
)
def test_incomplete_duplicate_and_stale_outcomes_never_pass(pair, mutation):
    legacy, bazel = pair
    mutate(bazel, "sidecar", mutation)
    result = parity.compare(legacy, bazel)
    assert not result["success"], result


def test_multishard_parity_with_empty_shards_and_different_target_partition(tmp_path):
    legacy, bazel = tmp_path / "legacy", tmp_path / "bazel"
    evidence(legacy)
    manifest = evidence(bazel, count=4)
    assert parity.compare(legacy, bazel)["success"]
    manifest["suites"]["default_1"]["targets"][0]["shards"].pop()
    write_json(bazel, "manifest.json", manifest)
    result = parity.compare(legacy, bazel)
    assert not result["success"] and "missing or duplicate shards" in str(result["errors"])


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m["suites"].clear(),
        lambda m: m["suites"]["default_1"].update(marker=""),
        lambda m: m["suites"]["default_1"]["targets"].append(
            copy.deepcopy(m["suites"]["default_1"]["targets"][0])
        ),
        lambda m: m["suites"]["default_1"]["targets"][0]["shards"].append(
            copy.deepcopy(m["suites"]["default_1"]["targets"][0]["shards"][0])
        ),
    ],
)
def test_missing_suite_marker_drift_and_duplicate_receipts(pair, change):
    legacy, bazel = pair
    mutate(bazel, "manifest", change)
    assert not parity.compare(legacy, bazel)["success"]


def test_bazel_cleanup_failure_and_missing_lifecycle_cannot_be_hidden(pair):
    legacy, bazel = pair
    mutate(bazel, "lifecycle", lambda r: r.update(exit_status=1, errors=["cleanup failed"]))
    result = parity.compare(legacy, bazel)
    assert not result["success"] and "cleanup failed" in str(result["failures"])
    mutate(
        bazel,
        "manifest",
        lambda m: m["suites"]["default_1"]["targets"][0]["shards"][0].pop("lifecycle"),
    )
    assert not parity.compare(legacy, bazel)["success"]


def test_junit_omission_corruption_and_identity_drift(pair):
    legacy, bazel = pair
    xml = bazel / "junit-0.xml"
    original = xml.read_bytes()
    xml.write_bytes(b"corrupt cached bytes")
    assert "digest mismatch" in str(parity.compare(legacy, bazel)["errors"])
    xml.write_bytes(original.replace(b"aq_nodeid", b"wrong_key"))
    manifest = json.loads((bazel / "manifest.json").read_text())
    manifest["suites"]["default_1"]["targets"][0]["shards"][0]["junit"]["sha256"] = parity.digest(
        xml.read_bytes()
    )
    write_json(bazel, "manifest.json", manifest)
    assert "canonical identity" in str(parity.compare(legacy, bazel)["errors"])
    xml.unlink()
    assert not parity.compare(legacy, bazel)["success"]


@pytest.mark.parametrize(
    "outcome,strict",
    [
        ("pass", None),
        ("skip", None),
        ("xfail", False),
        ("xfail", True),
        ("xpass", False),
        ("xpass", True),
        ("fail", None),
    ],
)
def test_exact_outcomes_strictness_and_reason_changes(tmp_path, outcome, strict):
    legacy, bazel = tmp_path / "legacy", tmp_path / "bazel"
    nodes = [
        node(result=outcome, strict=strict, reason="known issue" if outcome != "pass" else None)
    ]
    evidence(legacy, nodes=nodes)
    evidence(bazel, nodes=nodes)
    result = parity.compare(legacy, bazel)
    assert result["parity"], result
    assert result["success"] == (outcome != "fail" and not (outcome == "xpass" and strict))
    mutate(
        bazel, "sidecar", lambda r: r["nodes"][0]["phases"]["call"].update(reason="changed reason")
    )
    assert not parity.compare(legacy, bazel)["success"]


def test_same_total_different_outcome_or_strictness_is_a_difference(tmp_path):
    legacy, bazel = tmp_path / "legacy", tmp_path / "bazel"
    evidence(legacy, nodes=[node(result="xfail", strict=False, reason="known")])
    evidence(bazel, nodes=[node(result="xfail", strict=True, reason="known")])
    assert parity.compare(legacy, bazel)["differences"]
    evidence(bazel, nodes=[node(result="skip", reason="known")])
    assert parity.compare(legacy, bazel)["differences"]


def test_dashboard_artifact_identity_and_failure_are_independent_obligations(tmp_path):
    legacy, bazel = tmp_path / "legacy", tmp_path / "bazel"
    evidence(legacy, dashboard=True)
    manifest = evidence(bazel, dashboard=True)
    assert parity.compare(legacy, bazel)["success"]
    manifest["suites"]["dashboard_build"]["artifacts"] = write_json(
        bazel, "artifacts.json", [{"path": "dist/main.js", "sha256": "d" * 64}]
    )
    write_json(bazel, "manifest.json", manifest)
    assert parity.compare(legacy, bazel)["differences"]
    for root in (legacy, bazel):
        evidence(root, dashboard=True)
        mutate(root, "manifest", lambda m: m["suites"]["dashboard_build"].update(exit_status=1))
    result = parity.compare(legacy, bazel)
    assert result["parity"] and not result["success"] and result["failures"]


def test_b1_indexed_suite_manifest_is_consumed(pair):
    legacy, bazel = pair
    for root in pair:
        manifest = json.loads((root / "manifest.json").read_text())
        nodes = manifest["suites"]["default_1"]["targets"][0]["expected_nodes"]
        manifest["suite_manifest"] = write_json(
            root,
            "plan.json",
            {
                "schema_version": 1,
                "collection": {"nodes": [{"nodeid": n} for n in nodes]},
                "suites": {
                    "default_1": {"marker": "not slow", "modules": {"tests/test_example.py": [0]}}
                },
            },
        )
        write_json(root, "manifest.json", manifest)
    assert parity.compare(legacy, bazel)["success"]


def run_recorder(directory, source, *, runtime=False, xdist=False, args=()):
    directory.mkdir()
    (directory / "test_sample.py").write_text(textwrap.dedent(source))
    (directory / "pytest.ini").write_text("[pytest]\n")
    common = ["-c", "pytest.ini", "--rootdir=.", "-q", "--junitxml=junit.xml", *args]
    if runtime:
        script = (
            "import pytest; from pathlib import Path; "
            "from tools.bazel.pytest_plugin import RuntimePlugin; "
            f"raise SystemExit(pytest.main({common!r}, plugins=[RuntimePlugin("
            "output=Path('outcomes.json'), env={})]))"
        )
        command = [sys.executable, "-c", script]
    else:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tools.bazel.pytest_plugin",
            "--aq-outcomes=outcomes.json",
            *common,
        ]
        if xdist:
            command.extend(["-p", "xdist.plugin", "-n", "1"])
    result = subprocess.run(
        command,
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=40,
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    output = directory / "outcomes.json"
    assert output.exists(), result.stdout + result.stderr
    return result, json.loads(output.read_text())


@pytest.mark.parametrize("runtime,xdist", [(False, False), (True, False), (False, True)])
def test_real_recorders_preserve_full_phase_outcomes_and_junit_binding(tmp_path, runtime, xdist):
    directory = tmp_path / "run"
    result, report = run_recorder(
        directory,
        """
        import pytest
        @pytest.mark.parametrize('value', [1], ids=['opaque/a::b'])
        def test_pass(value): pass
        @pytest.mark.skip(reason='unsupported platform')
        def test_skip(): pass
        @pytest.mark.xfail(reason='known defect', strict=True)
        def test_xfail(): assert False
        @pytest.mark.xfail(reason='known defect', strict=False)
        def test_xpass(): pass
        @pytest.mark.xfail(reason='known defect', strict=True)
        def test_strict_xpass(): pass
        def test_imperative(): pytest.xfail('dynamic reason')
        @pytest.fixture
        def broken(): raise RuntimeError('setup failed')
        def test_setup(broken): pass
        @pytest.fixture
        def cleanup():
            yield
            raise RuntimeError('teardown failed')
        def test_teardown(cleanup): pass
        def test_double_failure(cleanup): assert False
    """,
        runtime=runtime,
        xdist=xdist,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not report["errors"], report
    nodes = [parity.semantic_node(n) for n in report["nodes"]]
    parity.check_junit((directory / "junit.xml").read_bytes(), nodes)
    by_name = {n["nodeid"].split("::", 1)[1]: n for n in nodes}
    assert by_name["test_pass[opaque/a::b]"]["parameter_id"] == "opaque/a::b"
    assert by_name["test_skip"]["phases"]["setup"]["reason"] == "Skipped: unsupported platform"
    assert by_name["test_strict_xpass"]["phases"]["call"]["xfail_strict"] is True
    assert by_name["test_setup"]["outcome"] == "error"
    assert by_name["test_teardown"]["outcome"] == "error"


@pytest.mark.parametrize("runtime", [False, True])
def test_real_collection_error_and_interrupted_runs_retain_evidence(tmp_path, runtime):
    _, report = run_recorder(
        tmp_path / "collection", "raise RuntimeError('bad import')", runtime=runtime
    )
    assert report["collection_errors"] and "bad import" in str(report["collection_errors"])
    _, report = run_recorder(
        tmp_path / "early",
        "def test_fail(): assert False\ndef test_later(): pass",
        runtime=runtime,
        args=("-x",),
    )
    assert "selected nodes did not execute exactly once" in report["errors"]


def test_cli_passing_and_deliberately_incomplete_fixture_pairs(pair):
    legacy, bazel = pair
    command = [
        sys.executable,
        str(ROOT / "scripts/bazel/compare_results.py"),
        "--legacy",
        str(legacy),
        "--bazel",
        str(bazel),
        "--require-exact-sha",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and json.loads(result.stdout)["success"], (
        result.stdout + result.stderr
    )
    (bazel / "outcomes-0.json").unlink()
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1 and json.loads(result.stdout)["errors"], (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize("bad", ["../escape.json", "/tmp/escape.json", "a/../escape.json"])
def test_artifact_traversal_never_reads_outside_evidence(pair, bad):
    legacy, bazel = pair
    mutate(bazel, "manifest", lambda m: m["suite_manifest"].update(path=bad))
    assert not parity.compare(legacy, bazel)["success"]
