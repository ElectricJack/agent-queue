#!/usr/bin/env python3
"""Audit the LLM/performance dependency slice without executing Bazel tests.

Run after generate_targets.py opts in tests/llm/* and tests/perf/*. Optional
--bazel performs read-only rdeps queries in a private output root. --baseline
compares independently retained pre-migration node and suite obligations.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {
    "tests/llm/test_gemini_adapter.py": {"tests/llm/fake_genai.py"},
    "tests/llm/test_provider_requests.py": {"tests/llm/fake_genai.py"},
    "tests/llm/test_fallback.py": {"tests/test_llm_executor.py"},
    "tests/perf/test_claim_statements.py": {"tests/perf/test_hierarchy_statements.py"},
    "tests/perf/test_formula_statements.py": {"tests/perf/test_hierarchy_statements.py"},
    "tests/perf/test_layout_statements.py": {"scripts/seed_layout_perf.py"},
    "tests/perf/test_layout_api_statements.py": {"scripts/seed_layout_perf.py"},
    "tests/perf/test_train_selection.py": {"tests/test_integration_train_sources.py"},
}
PROBES = (
    ("src/llm/fake.py", "//src/llm:fake.py"),
    ("src/llm/providers/adapters/gemini_adapter.py",
     "//src/llm:providers/adapters/gemini_adapter.py"),
    ("tests/llm/fake_genai.py", "//tests:llm/fake_genai.py"),
)


def load_manifest(path: Path) -> dict:
    """Decode either wire version through the shared, validated manifest reader."""
    spec = importlib.util.spec_from_file_location(
        "aq_bazel_manifest", ROOT / "src/test_selection/bazel_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.hydrate_manifest(json.loads(path.read_text(encoding="utf-8")))


def declared(info: dict) -> set[str]:
    return set().union(*(info.get(field, []) for field in (
        "source_files", "test_sources", "other_sources", "data_files",
    )))


def direct_imports(path: str, workspace: Path = ROOT) -> set[str]:
    """Independent positive checks for repository imports in each test body."""
    result = set()
    for node in ast.walk(ast.parse((workspace / path).read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
            names.extend((node.module or "") + "." + alias.name for alias in node.names)
        for name in names:
            pieces = name.split(".")
            for end in range(1, len(pieces) + 1):
                base = "/".join(pieces[:end])
                for candidate in (base + ".py", base + "/__init__.py"):
                    if candidate.startswith(("src/", "tests/", "scripts/")) and (
                        workspace / candidate
                    ).is_file():
                        result.add(candidate)
    return result


def obligations(manifest: dict, path: str) -> dict:
    info = manifest["modules"][path]
    nodes = manifest["collection"]["nodes"]
    return {
        "nodes": {nodes[index]["nodeid"]: nodes[index]["markers"]
                  for index in info["nodes"]},
        "targets": info["targets"],
        "suites": sorted(name for name, suite in manifest["suites"].items()
                         if path in suite.get("modules", [])),
        "arms": {name: Counter(nodes[index]["nodeid"] for index in
                               suite.get("modules", {}).get(path, []))
                 for name, suite in manifest["suites"].items()},
    }


def preserve_obligations(manifest: dict, baseline: dict, path: str) -> None:
    current, previous = obligations(manifest, path), obligations(baseline, path)
    assert previous["nodes"].items() <= current["nodes"].items(), (
        f"existing node/marker obligations changed: {path}"
    )
    def is_default(name: str) -> bool:
        return re.fullmatch(r"default_[1-8]", name) is not None

    for arm, label in current["targets"].items():
        if is_default(arm):
            expected = manifest["modules"][path]["target"] + "__" + arm
            assert label == expected, f"noncanonical default target: {path}: {arm}"
            if arm in previous["targets"]:
                assert label == previous["targets"][arm], f"target label changed: {path}: {arm}"
    assert {k: v for k, v in current["targets"].items() if not is_default(k)} == {
        k: v for k, v in previous["targets"].items() if not is_default(k)
    }, f"nondefault targets changed: {path}"
    old_nodes = set(previous["nodes"])
    nondefault = {arm for arm in previous["arms"] | current["arms"] if not is_default(arm)}
    for arm in nondefault:
        before = previous["arms"].get(arm, Counter())
        after = Counter({node: count for node, count in current["arms"].get(arm, {}).items()
                         if node in old_nodes})
        assert before == after, f"existing nondefault arm membership changed: {path}: {arm}"
    before = sum((nodes for arm, nodes in previous["arms"].items() if is_default(arm)), Counter())
    after = sum((nodes for arm, nodes in current["arms"].items() if is_default(arm)), Counter())
    after = Counter({node: count for node, count in after.items() if node in old_nodes})
    assert before == after, f"existing default shard union changed: {path}"


def audit(manifest: dict, baseline: dict | None = None, workspace: Path = ROOT) -> dict:
    paths = sorted(p.relative_to(workspace).as_posix()
                   for directory in ("tests/llm", "tests/perf")
                   for p in (workspace / directory).glob("test_*.py"))
    records = {}
    for path in paths:
        info = manifest["modules"][path]
        assert info.get("fine_grained"), f"slice not enabled: {path}"
        required = direct_imports(path, workspace) | REQUIRED.get(path, set()) | {
            path, "tests/conftest.py", "tests/db_fixtures.py", "tests/pg_dsn.py",
        }
        if path.startswith("tests/perf/"):
            required.add("tests/perf/conftest.py")
        missing = required - declared(info)
        assert not missing, f"{path}: missing inputs {sorted(missing)}"
        if baseline:
            preserve_obligations(manifest, baseline, path)
        records[path] = {
            "sources": len(info["source_files"]),
            "test_support": len(info["test_sources"]),
            "required_inputs": sorted(required),
            "conservative_reasons": info.get("conservative_reasons", []),
            "nodes": len(info["nodes"]),
            "suite_obligations_preserved": baseline is not None,
        }
    return records


def query_proofs(
    manifest: dict, records: dict, bazel: str, output: Path, workspace: Path = ROOT,
) -> list[dict]:
    targets = {manifest["modules"][path]["target"]: path for path in records}
    universe = "set(" + " ".join(sorted(targets)) + ")"
    proofs = []
    for source, label in PROBES:
        query = f"kind('py_test rule', rdeps({universe}, {label}))"
        command = [bazel, f"--output_user_root={output / 'bazel-root'}", "query",
                   query, "--output=label", "--noshow_progress", "--lockfile_mode=error"]
        completed = subprocess.run(command, cwd=workspace, text=True, capture_output=True, check=False)
        log = output / (Path(source).name + ".query.log")
        log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"Bazel query failed ({completed.returncode}); see {log}")
        actual_labels = sorted(line for line in completed.stdout.splitlines() if line)
        actual = sorted(targets[label] for label in actual_labels)
        expected = sorted(path for path in records
                          if source in declared(manifest["modules"][path]))
        assert actual == expected, f"{source}: query/manifest mismatch {actual} != {expected}"
        proofs.append({"source": source, "query": query, "command": command, "modules": actual,
                       "selected": len(actual), "universe": len(records),
                       "narrow": 0 < len(actual) < len(records)})
    return proofs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "tools/bazel/test_suites.json")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--workspace", type=Path, default=ROOT,
                        help="Repository for source checks and Bazel query cwd")
    parser.add_argument("--bazel", help="Pinned executable; performs queries only")
    parser.add_argument("--output", type=Path, default=ROOT / ".aq/tmp/slice-d-llm-perf")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    baseline = load_manifest(args.baseline) if args.baseline else None
    workspace = args.workspace.resolve()
    records = audit(manifest, baseline, workspace)
    args.output.mkdir(parents=True, exist_ok=True)
    result = {"modules": records, "proofs": []}
    if args.bazel:
        result["proofs"] = query_proofs(manifest, records, args.bazel, args.output, workspace)
    (args.output / "audit.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Audited {len(records)} modules; {len(result['proofs'])} Bazel query proofs")


if __name__ == "__main__":
    main()
