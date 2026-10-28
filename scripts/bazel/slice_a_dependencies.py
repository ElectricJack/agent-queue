#!/usr/bin/env python3
"""Audit slice A dependency retention and selection; never build or execute tests.

Run after generation, supplying independently retained baseline/collection files::

    python scripts/bazel/slice_a_dependencies.py --baseline baseline.json \
        --collection collection.json --output .aq/tmp/slice-a-audit

Add --bazel /absolute/path/to/bazel-8.3.1 for read-only reverse-dependency queries.
Legacy M is computed from complete synthetic single-file modifications, without
shadow fallback S. Containment is checked within the slice, not the whole suite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.bazel.dependency_paths import repository_files  # noqa: E402
from scripts.bazel.slice_d_llm_perf import (  # noqa: E402
    declared,
    direct_imports,
    load_manifest,
    preserve_obligations,
)
from src.test_selection.catalogue import load_catalogue, load_rules  # noqa: E402
from src.test_selection.mandatory import mandatory_set  # noqa: E402
from src.test_selection.snapshot import ChangedPath, ChangeSnapshot  # noqa: E402

PROBES = (
    ("graph", "src/task_graph/layout/order_key.py", "//src/task_graph:layout/order_key.py"),
    ("graph", "src/task_graph/creator.py", "//src/task_graph:creator.py"),
    ("graph", "src/task_graph/parser.py", "//src/task_graph:parser.py"),
    ("integration", "src/integration/models.py", "//src/integration:models.py"),
    ("wait", "src/waits/adapters/timer.py", "//src/waits:adapters/timer.py"),
)


def group_for(path: str) -> str | None:
    if path.startswith("tests/task_graph/"):
        return "graph"
    if Path(path).parent == Path("tests"):
        if Path(path).name.startswith(("test_integration_", "test_train", "test_stack")):
            return "integration"
        if Path(path).name.startswith(("test_wait", "test_delivery")):
            return "wait"
    return None


def digest(path: Path) -> str:
    if path.is_dir():
        hasher = hashlib.sha256()
        for member in sorted(path.rglob("*.json")):
            hasher.update(str(member.relative_to(path)).encode("utf-8"))
            hasher.update(member.read_bytes())
        return hasher.hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ordered_nodes(nodes: list[dict], modules: set[str]) -> list[str]:
    return [node["nodeid"] for node in sorted(nodes, key=lambda n: n["collection_index"])
            if node["nodeid"].split("::", 1)[0] in modules]


def collection_identity(manifest: dict, collection: dict, modules: set[str]) -> dict:
    def identities(nodes):
        return sorted((node["nodeid"], tuple(sorted(node["markers"]))) for node in nodes
                      if node["nodeid"].split("::", 1)[0] in modules)

    generated = manifest["collection"]["nodes"]
    independent = collection["nodes"]
    return {
        "exit_code": collection.get("exit_code"),
        "nodes": len(identities(independent)),
        "node_marker_identity": identities(generated) == identities(independent),
        "collection_order": ordered_nodes(generated, modules) == ordered_nodes(independent, modules),
        "failed_reports": [r for r in collection.get("collection_reports", [])
                           if r["outcome"] == "failed"],
    }


def audit(args: argparse.Namespace) -> dict:
    manifest = load_manifest(args.manifest)
    baseline = load_manifest(args.baseline) if args.baseline else None
    catalogue_path = ROOT / "tests/selection_catalogue.json"
    rules_path = ROOT / "tests/selection_rules.yaml"
    catalogue = load_catalogue(catalogue_path)
    rules = load_rules(rules_path, catalogue)
    modules = {p: info for p, info in manifest["modules"].items() if group_for(p)}
    groups = {group: sorted(p for p in modules if group_for(p) == group)
              for group in ("graph", "integration", "wait")}
    errors, records = [], {}
    expected = {p for p in catalogue.modules if group_for(p)}
    if set(modules) != expected:
        errors.append(f"slice module mismatch: {sorted(set(modules) ^ expected)}")
    inputs = {p: declared(info) for p, info in modules.items()}
    for path, info in modules.items():
        if not info.get("fine_grained"):
            errors.append(f"slice not enabled: {path}")
        missing = sorted(direct_imports(path) - inputs[path])
        if missing:
            errors.append(f"missing direct imports: {path}: {missing}")
        if baseline:
            try:
                preserve_obligations(manifest, baseline, path)
                assert info["postgres"] == baseline["modules"][path]["postgres"], path
                old = ordered_nodes(baseline["collection"]["nodes"], {path})
                live = ordered_nodes(manifest["collection"]["nodes"], {path})
                assert [n for n in live if n in set(old)] == old, f"ordering changed: {path}"
            except (AssertionError, KeyError) as error:
                errors.append(f"baseline obligation: {path}: {error}")
        records[path] = {
            "source_files": len(info.get("source_files", [])),
            "data_files": len(info.get("data_files", [])),
            "conservative": info.get("conservative", False),
            "conservative_reasons": info.get("conservative_reasons", []),
            "missing_direct_imports": missing,
            "nodes": len(info["nodes"]),
        }
    commands = {}
    for name, suite in manifest["suites"].items():
        if not (set(suite.get("modules", {})) & set(modules)):
            continue
        commands[name] = {key: suite.get(key) for key in (
            "command", "marker", "perf_strict", "split_version",
        )}
        if baseline:
            old = baseline["suites"].get(name, {})
            if commands[name] != {key: old.get(key) for key in commands[name]}:
                errors.append(f"named command metadata changed: {name}")
    source_checks, declarations = {}, {p: {"data": []} for p in modules}
    for source in sorted(repository_files(ROOT)):
        if not source.startswith("src/") or not source.endswith(".py"):
            continue
        snapshot = ChangeSnapshot(
            str(ROOT), "synthetic-single-modification", "base", "head",
            (ChangedPath(source, "modified"),), "synthetic", True, None, None, 0.0,
        )
        result = mandatory_set(snapshot, catalogue, rules)
        mandatory = sorted(result.modules & modules.keys())
        missing = sorted(p for p in mandatory if source not in inputs[p])
        source_checks[source] = {
            "mandatory_total": len(result.modules), "mandatory_slice": mandatory,
            "full_required": result.full_required, "missing": missing,
        }
        for path in mandatory:
            declarations[path]["data"].append(source)
        if missing:
            errors.append(f"mandatory source coverage: {source}: {missing}")
    if args.declarations_output:
        write_json(args.declarations_output, declarations)
    parity = None
    if args.collection:
        collection = json.loads(args.collection.read_text(encoding="utf-8"))
        parity = collection_identity(manifest, collection, set(modules))
        if (parity["exit_code"] != 0 or parity["failed_reports"]
                or not parity["node_marker_identity"] or not parity["collection_order"]):
            errors.append("independent collection identity/order failed")
    proofs = []
    for group, source, label in PROBES:
        paths = groups[group]
        mandatory = sorted(set(source_checks[source]["mandatory_slice"]) & set(paths))
        expected_selected = sorted(p for p in paths if source in inputs[p])
        targets = {modules[p]["target"]: p for p in paths}
        universe = "set(" + " ".join(sorted(targets)) + ")"
        query = f"kind('py_test rule', rdeps(deps({universe}), {label}))"
        proof = {"group": group, "source": source, "mandatory": mandatory,
                 "manifest_selected": expected_selected, "query_executed": bool(args.bazel)}
        if args.bazel:
            command = [args.bazel, "--host_jvm_args=-Xmx2g",
                       f"--output_base={args.output_base.resolve()}", "query", query,
                       "--output=label", "--noshow_progress", "--lockfile_mode=error"]
            stem = f"{group}-{Path(source).stem}"
            try:
                run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=600)
                (args.output / f"{stem}.stdout").write_text(run.stdout, encoding="utf-8")
                (args.output / f"{stem}.stderr").write_text(run.stderr, encoding="utf-8")
                selected = sorted(targets[line] for line in run.stdout.splitlines() if line in targets)
                missing = sorted(set(mandatory) - set(selected))
                proof.update(command=command, returncode=run.returncode, selected=selected,
                             missing=missing, extras=sorted(set(selected) - set(mandatory)),
                             narrow=0 < len(selected) < len(paths))
                if run.returncode or selected != expected_selected or missing:
                    errors.append(f"query/manifest/M disagreement: {source}")
            except subprocess.TimeoutExpired:
                proof.update(command=command, timeout_seconds=600)
                errors.append(f"query timeout: {source}")
        proofs.append(proof)
    fingerprints = {str(p): digest(p) for p in (
        args.manifest, catalogue_path, rules_path, ROOT / "tests/BUILD.bazel",
        ROOT / "scripts/bazel/test_dependency_inputs",
        ROOT / "scripts/bazel/test_dependencies.py",
        ROOT / "scripts/bazel/dependency_paths.py", ROOT / "scripts/bazel/generate_targets.py",
        Path(__file__),
    )}
    for path in (args.baseline, args.collection):
        if path:
            fingerprints[str(path)] = digest(path)
    return {
        "fingerprints": fingerprints, "groups": groups, "modules": records,
        "baseline_checked": baseline is not None, "collection": parity, "commands": commands,
        "source_checks": source_checks, "proofs": proofs, "errors": errors,
        "scope": "M containment within slice A for existing tracked src Python files; no fallback S",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "tools/bazel/test_suites.json")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--collection", type=Path)
    parser.add_argument("--declarations-output", type=Path,
                        help="Write additive legacy-M source policy inputs; no discovered inputs removed")
    parser.add_argument("--bazel", help="Pinned executable; read-only queries, never builds/tests")
    parser.add_argument("--output-base", type=Path, default=Path("/tmp/aq-slice-a-audit-bazel"))
    parser.add_argument("--output", type=Path, default=ROOT / ".aq/tmp/slice-a-audit")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result = audit(args)
    write_json(args.output / "audit.json", result)
    print(json.dumps({"modules": len(result["modules"]), "sources": len(result["source_checks"]),
                      "errors": result["errors"], "output": str(args.output / "audit.json")}, indent=2))
    raise SystemExit(bool(result["errors"]))


if __name__ == "__main__":
    main()
