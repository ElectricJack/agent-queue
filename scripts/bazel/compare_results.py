#!/usr/bin/env python3
"""Compare complete, exact-candidate legacy/Bazel evidence (never totals alone).

Each directory contains manifest.json, schema_version=1, with:
  context: sha (full Git OID), platform, toolchain_lock_sha256,
           dependency_lock_sha256, environment_sha256, graph_sha256
  suite_manifest: {path, sha256} of B1's test_suites.json
  required_suites: nonempty list of suite IDs for this comparison
  suites: {ID: {marker, targets: [{id, expected_nodes, shard_count,
             shards: [{index, sidecar: {path, sha256}, junit: {path, sha256},
                       lifecycle: {path, sha256}}]}]}}
  invocation: optional reporting-only metadata (timestamps, cache source, run ID)

The suite manifest supplies the independent expected node set via B1's
collection.nodes and suites[ID].modules indices. A small external plan may use
suites[ID].expected_nodes instead. Dashboard plans declare kind="dashboard";
their evidence supplies exit_status and artifacts: {path, sha256}, a JSON list
of {path, sha256} generated files (an explicit empty list is valid for typecheck).
Markers always come from the plan. All artifact paths are relative and confined
to the evidence directory. References hash the original cached bytes. Result
digests exclude context and invocation metadata; copying a cached result into a
new invocation preserves its digest. This command compares only required_suites;
it does not issue an attestation, promote a suite or implement the 20-sample gate.

Legacy pytest: -p tools.bazel.pytest_plugin --aq-outcomes outcomes.json
              --junitxml=junit.xml (supports serial and xdist).
Bazel: retain pytest.json, test.xml and target.json from each H5 target/shard.
The lifecycle reference is mandatory for Bazel and optional for legacy pytest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath

# Permit both direct script execution and imports without an installed AQ package.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bazel.runtime import Shard, normalize_nodeid, validate_target  # noqa: E402

CONTEXT_FIELDS = (
    "sha",
    "platform",
    "toolchain_lock_sha256",
    "dependency_lock_sha256",
    "environment_sha256",
    "graph_sha256",
)
OUTCOMES = {"pass", "fail", "error", "skip", "xfail", "xpass"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=unique_object)


def relative_path(value):
    require(isinstance(value, str) and value, "missing artifact path")
    path = PurePosixPath(value)
    require(
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and ":" not in value
        and path.as_posix() == value
        and value != ".",
        f"artifact path must be canonical and relative: {value}",
    )
    return path


def artifact(root, reference):
    path = root / relative_path(reference["path"])
    require(path.resolve().is_relative_to(root.resolve()), "artifact escapes evidence directory")
    data = path.read_bytes()
    require(reference["sha256"] == digest(data), f"artifact digest mismatch: {reference['path']}")
    return data


def nodeids(values, *, nonempty=True):
    require(isinstance(values, list), "node identities must be a list")
    normalized = [normalize_nodeid(value) for value in values]
    require(not nonempty or normalized, "empty expected node set")
    require(len(normalized) == len(set(normalized)), "duplicate node identity")
    return normalized


def expected_nodes(plan, suite):
    if "expected_nodes" in suite:
        return nodeids(suite["expected_nodes"])
    table = plan["collection"]["nodes"]
    nodes = []
    for module, indices in suite["modules"].items():
        for index in indices:
            require(type(index) is int and 0 <= index < len(table), "invalid collection index")
            node = table[index]["nodeid"]
            require(node.partition("::")[0] == module, "suite module/node mismatch")
            nodes.append(node)
    return nodeids(nodes)


def semantic_node(node):
    nodeid = normalize_nodeid(node["nodeid"])
    parameter = node["parameter_id"]
    require(parameter is None or isinstance(parameter, str), f"invalid parameter ID: {nodeid}")
    if parameter is not None:
        require(nodeid.endswith("[" + parameter + "]"), f"parameter identity mismatch: {nodeid}")
    phases = node["phases"]
    require(
        {"setup", "teardown"} <= phases.keys() <= {"setup", "call", "teardown"},
        f"incomplete or unknown phases: {nodeid}",
    )
    require(
        phases["setup"]["outcome"] != "passed" or "call" in phases, f"missing call phase: {nodeid}"
    )
    require(
        phases["setup"]["outcome"] == "passed" or "call" not in phases,
        f"call after unsuccessful setup: {nodeid}",
    )
    normalized = {}
    for name, phase in phases.items():
        result, raw = phase["result"], phase["outcome"]
        strict, reason = phase["xfail_strict"], phase["reason"]
        require(
            result in OUTCOMES and raw in {"passed", "failed", "skipped"},
            f"invalid phase outcome: {nodeid}",
        )
        require(strict is None or type(strict) is bool, f"invalid xfail strictness: {nodeid}")
        require(reason is None or isinstance(reason, str), f"invalid reason: {nodeid}")
        expected_raw = {
            "pass": "passed",
            "fail": "failed",
            "error": "failed",
            "skip": "skipped",
            "xfail": "skipped",
            "xpass": "failed" if strict else "passed",
        }[result]
        require(raw == expected_raw, f"contradictory phase outcome: {nodeid}")
        require(
            result not in {"xfail", "xpass"} or type(strict) is bool,
            f"missing xfail strictness: {nodeid}",
        )
        require(
            result not in {"skip", "xfail", "xpass", "fail", "error"} or bool(reason),
            f"missing outcome reason: {nodeid}",
        )
        require(result != "error" or name != "call", f"call error must be fail: {nodeid}")
        require(result != "fail" or name == "call", f"fixture failure must be error: {nodeid}")
        normalized[name] = {
            "result": result,
            "outcome": raw,
            "xfail_strict": strict,
            "reason": reason,
        }
    results = {phase["result"] for phase in normalized.values()}
    outcome = next(
        value for value in ("error", "fail", "xpass", "xfail", "skip", "pass") if value in results
    )
    require(node["outcome"] == outcome, f"contradictory node outcome: {nodeid}")
    require(not node.get("budget_exceeded"), f"node budget exceeded: {nodeid}")
    return {"nodeid": nodeid, "parameter_id": parameter, "outcome": outcome, "phases": normalized}


def junit_expected(nodes):
    rows = Counter()
    for node in nodes:
        phases = node["phases"]
        tags = [
            "skipped" if phase["outcome"] == "skipped" else "failure" if name == "call" else "error"
            for name, phase in phases.items()
            if phase["outcome"] != "passed"
        ]
        # Pytest emits two cases for a failed call AND failed teardown.
        double = (
            phases.get("call", {}).get("outcome") == "failed"
            and phases["teardown"]["outcome"] == "failed"
        )
        groups = [("failure",), ("error",)] if double else [tuple(sorted(tags))]
        for tags in groups:
            rows[(node["nodeid"], node["parameter_id"], tags)] += 1
    return rows


def check_junit(data, nodes):
    require(
        b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(),
        "JUnit document declarations are forbidden",
    )
    xml = ET.fromstring(data)
    require(xml.tag in {"testsuites", "testsuite"}, "not a JUnit document")
    rows = Counter()
    for case in xml.iter("testcase"):
        properties = [
            (prop.get("name"), prop.get("value")) for prop in case.findall("./properties/property")
        ]
        values = dict(properties)
        require(
            sum(key == "aq_nodeid" for key, _ in properties) == 1
            and sum(key == "aq_parameter_id" for key, _ in properties) == 1,
            "JUnit missing or duplicate canonical identity properties",
        )
        nodeid = normalize_nodeid(values["aq_nodeid"])
        parameter = decode(values["aq_parameter_id"])
        require(parameter is None or isinstance(parameter, str), "invalid JUnit parameter ID")
        tags = tuple(
            sorted(child.tag for child in case if child.tag in {"failure", "error", "skipped"})
        )
        rows[(nodeid, parameter, tags)] += 1
    require(rows == junit_expected(nodes), "JUnit identities/outcomes differ from sidecar")
    require(
        not xml.findall("./error") and not xml.findall("./testsuite/error"),
        "JUnit suite-level error",
    )


def pytest_suite(root, suite, expected, *, require_lifecycle):
    require(isinstance(suite["targets"], list) and suite["targets"], "missing target evidence")
    all_nodes, target_nodes, target_ids, outputs, failures = [], [], set(), [], []
    for target in suite["targets"]:
        require(
            isinstance(target["id"], str) and target["id"] not in target_ids,
            "duplicate or invalid target ID",
        )
        target_ids.add(target["id"])
        expected_target = nodeids(target["expected_nodes"])
        target_nodes.extend(expected_target)
        count = target["shard_count"]
        require(type(count) is int and count > 0, "invalid shard count")
        shards = target["shards"]
        require(
            Counter(shard["index"] for shard in shards) == Counter(range(count)),
            f"missing or duplicate shards: {target['id']}",
        )
        for shard in shards:
            index = shard["index"]
            require(type(index) is int, "invalid shard index")
            report = decode(artifact(root, shard["sidecar"]))
            outputs.append(
                {"sidecar": shard["sidecar"]["sha256"], "junit": shard["junit"]["sha256"]}
            )
            if require_lifecycle or "lifecycle" in shard:
                receipt = decode(artifact(root, shard["lifecycle"]))
                outputs[-1]["lifecycle"] = shard["lifecycle"]["sha256"]
                failures.extend(
                    f"{target['id']} shard {index}: {error} ({canonical(receipt)})"
                    for error in validate_target(receipt)
                )
            require(report["schema_version"] == 1, "unknown outcome sidecar schema")
            require(
                not report["collection_errors"],
                f"collection errors/skips: {canonical(report['collection_errors'])}",
            )
            require(not report["errors"], f"runtime errors: {canonical(report['errors'])}")
            require(
                report["shard"] == {"index": index, "count": count},
                "sidecar shard contract mismatch",
            )
            require(
                Counter(nodeids(report["collected"])) == Counter(expected_target),
                "collected nodes differ from target plan",
            )
            selected = Shard(index, count).select(expected_target)
            require(
                Counter(nodeids(report["selected"], nonempty=False)) == Counter(selected),
                "selected shard nodes differ from target plan",
            )
            nodes = [semantic_node(node) for node in report["nodes"]]
            require(
                Counter(node["nodeid"] for node in nodes) == Counter(selected),
                "missing, duplicate or unexpected node outcomes",
            )
            check_junit(artifact(root, shard["junit"]), nodes)
            require(
                type(report["exit_status"]) is int and report["exit_status"] >= 0,
                "invalid pytest exit status",
            )
            if report["exit_status"] != 0:
                failures.append(
                    f"{target['id']} shard {index}: pytest exit {report['exit_status']}"
                )
            for node in nodes:
                if any(phase["outcome"] == "failed" for phase in node["phases"].values()):
                    failures.append(f"{node['nodeid']}: {node['outcome']}")
            all_nodes.extend(nodes)
    require(
        Counter(target_nodes) == Counter(expected), "target plan omits or duplicates suite nodes"
    )
    require(
        Counter(node["nodeid"] for node in all_nodes) == Counter(expected),
        "suite outcomes omit or duplicate expected nodes",
    )
    return {"nodes": sorted(all_nodes, key=lambda node: node["nodeid"])}, outputs, failures


def dashboard_suite(root, suite):
    code = suite["exit_status"]
    require(type(code) is int and code >= 0, "invalid dashboard exit status")
    files = decode(artifact(root, suite["artifacts"]))
    require(isinstance(files, list), "dashboard artifact manifest must be a list")
    seen = set()
    for entry in files:
        path = str(relative_path(entry["path"]))
        require(path not in seen, "duplicate dashboard artifact")
        seen.add(path)
        require(re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), "invalid dashboard digest")
        require(set(entry) == {"path", "sha256"}, "unknown dashboard artifact fields")
    return (
        {"exit_status": code, "artifacts": sorted(files, key=lambda entry: entry["path"])},
        [{"artifacts": suite["artifacts"]["sha256"], "exit_status": code}],
        [f"dashboard exit {code}"] if code else [],
    )


def load_backend(root, *, backend):
    manifest = decode((root / "manifest.json").read_bytes())
    require(manifest["schema_version"] == 1, "unknown evidence manifest schema")
    context = manifest["context"]
    require(set(context) == set(CONTEXT_FIELDS), "missing or unknown candidate context fields")
    require(re.fullmatch(r"[0-9a-f]{40}", context["sha"]), "candidate SHA must be a full Git OID")
    require(
        isinstance(context["platform"], str) and context["platform"].strip(), "missing platform"
    )
    for key in CONTEXT_FIELDS[2:]:
        require(re.fullmatch(r"[0-9a-f]{64}", context[key]), f"invalid {key}")
    plan = decode(artifact(root, manifest["suite_manifest"]))
    require(plan["schema_version"] == 1, "unknown suite manifest schema")
    required = manifest["required_suites"]
    require(
        isinstance(required, list)
        and required
        and all(isinstance(s, str) for s in required)
        and len(set(required)) == len(required),
        "invalid required suite set",
    )
    require(set(manifest["suites"]) == set(required), "missing or unexpected suite evidence")
    suites, digests, failures = {}, {}, []
    for name in required:
        evidence, obligation = manifest["suites"][name], plan["suites"][name]
        require(evidence["marker"] == obligation["marker"], f"marker differs from plan: {name}")
        kind = obligation.get("kind", "pytest")
        if kind == "pytest":
            result, outputs, failed = pytest_suite(
                root,
                evidence,
                expected_nodes(plan, obligation),
                require_lifecycle=backend == "bazel",
            )
        else:
            require(kind == "dashboard", f"unknown suite kind: {kind}")
            result, outputs, failed = dashboard_suite(root, evidence)
        suites[name] = {"kind": kind, "marker": evidence["marker"], **result}
        digests[name] = digest(canonical(sorted(outputs, key=canonical)).encode())
        failures.extend(f"{name}: {failure}" for failure in failed)
    return {
        "context": context,
        "suite_manifest_sha256": manifest["suite_manifest"]["sha256"],
        "suites": suites,
        "result_digests": digests,
        "failures": failures,
        "invocation": manifest.get("invocation", {}),
    }


def compare(legacy: Path, bazel: Path, *, require_exact_sha=True):
    # Exact comparison is unconditional; the CLI flag makes CI's intent explicit.
    backends, errors = {}, []
    for label, root in (("legacy", legacy), ("bazel", bazel)):
        try:
            backends[label] = load_backend(root, backend=label)
        except (
            ValueError,
            OSError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
            ET.ParseError,
        ) as exc:
            errors.append(f"{label}: {exc}")
    differences = []
    if len(backends) == 2:
        left, right = backends["legacy"], backends["bazel"]
        for key in CONTEXT_FIELDS:
            if left["context"][key] != right["context"][key]:
                errors.append(f"candidate context mismatch: {key}")
        if left["suite_manifest_sha256"] != right["suite_manifest_sha256"]:
            errors.append("suite manifest digest mismatch")
        for name in sorted(left["suites"].keys() | right["suites"].keys()):
            if left["suites"].get(name) != right["suites"].get(name):
                differences.append(
                    {
                        "suite": name,
                        "legacy": left["suites"].get(name),
                        "bazel": right["suites"].get(name),
                    }
                )
    failures = {label: value["failures"] for label, value in backends.items() if value["failures"]}
    parity = len(backends) == 2 and not errors and not differences
    return {
        "schema_version": 1,
        "success": parity and not failures,
        "parity": parity,
        "errors": errors,
        "differences": differences,
        "failures": failures,
        "result_digests": {label: value["result_digests"] for label, value in backends.items()},
        "invocations": {label: value["invocation"] for label, value in backends.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", required=True, type=Path)
    parser.add_argument("--bazel", required=True, type=Path)
    parser.add_argument("--require-exact-sha", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare(args.legacy, args.bazel, require_exact_sha=args.require_exact_sha)
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
