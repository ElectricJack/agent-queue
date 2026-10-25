#!/usr/bin/env python3
"""Audit pinned base/head BUILD graphs against static and mandatory obligations.

    python scripts/bazel/impact.py --base BASE --head HEAD --bazel /locked/bazel \
        --suite default_1 --output impact.json

Queries run in private, clean local clones of each exact commit, never against the
caller's working tree. No tests execute. Reports are observational Phase 1
signatures: they cannot authorize selection, cutover or candidate publication.
An incomplete audit still writes evidence, widens the entire requested suite,
and exits 1. Supply individual manifest suites (including marker arms), or the
non-Python `dashboard`/`generated` obligations. No network fetch of Git refs occurs.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.git.manager import GitError, GitManager  # noqa: E402
from src.git.process import run_owned_process  # noqa: E402
from src.test_selection.catalogue import load_catalogue, load_rules  # noqa: E402
from src.test_selection.mandatory import mandatory_set  # noqa: E402
from src.test_selection.snapshot import ChangedPath, ChangeSnapshot  # noqa: E402
from src.test_selection.static_impact import PytestImpactedAdapter  # noqa: E402

VERSION = 1
MANIFEST = "tools/bazel/test_suites.json"
LOCKS = (
    "MODULE.bazel.lock",
    "tools/bazel/toolchains.lock.json",
    "tools/bazel/requirements_linux_x86_64.txt",
    "package-lock.json",
    "tools/bazel/pnpm-lock.yaml",
    "tools/bazel/generator_npm.lock.json",
)
REGISTRY = (
    "scripts/regenerate-generated.sh",
    "tools/bazel/generator_inputs.py",
    "tools/bazel/generator_inputs.bzl",
    "tests/fixtures/generated_sources/playbooks/index.json",
)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bound_files(root: Path, paths) -> dict[str, str]:
    # Missing inputs are errors, never silently omitted from an evidence identity.
    return {path: file_hash(root / path) for path in sorted(paths)}


async def git(root: Path, *args: str) -> str:
    result = await GitManager().arun_git_result(list(args), cwd=str(root), timeout=120)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.rstrip("\n")


async def pin(root: Path, ref: str) -> str:
    return await git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")


def changes_from_diff(output: str) -> tuple[ChangedPath, ...]:
    fields = output.split("\0")
    changes = []
    while fields and fields[0]:
        status = fields.pop(0)
        path = fields.pop(0)
        if status.startswith(("R", "C")):
            new = fields.pop(0)
            changes.append(ChangedPath(new, "renamed", old_path=path))
        else:
            changes.append(
                ChangedPath(path, {"A": "added", "D": "deleted"}.get(status, "modified"))
            )
    return tuple(changes)


@dataclass
class Graph:
    edges: dict[str, set[str]]
    tests: set[str]

    def reached(self, seeds: set[str], *, reverse: bool = False) -> set[str]:
        edges = self.edges
        if reverse:
            edges = defaultdict(set)
            for consumer, inputs in self.edges.items():
                for dependency in inputs:
                    edges[dependency].add(consumer)
        result, pending = set(seeds), list(seeds)
        while pending:
            for dependency in edges.get(pending.pop(), set()) - result:
                result.add(dependency)
                pending.append(dependency)
        return result

    def affected(self, path: str) -> tuple[set[str], set[str]]:
        # File labels are resolved by their actual query names, rather than by
        # guessing the package boundary (root filegroups can span directories).
        labels = {
            label
            for inputs in self.edges.values()
            for label in inputs
            if label.startswith("//") and label[2:].replace(":", "/").lstrip("/") == path
        }
        owners = {rule for rule, inputs in self.edges.items() if inputs & labels}
        return owners, self.reached(labels, reverse=True) & self.tests

    def identity(self) -> str:
        return digest(
            {"edges": {k: sorted(v) for k, v in self.edges.items()}, "tests": sorted(self.tests)}
        )


def parse_graph(xml: bytes) -> Graph:
    tree = ET.fromstring(xml)
    if tree.tag != "query":
        raise ValueError("invalid Bazel query document")
    edges, tests = {}, set()
    for rule in tree.findall("rule"):
        label = rule.attrib["name"]
        if not label.startswith("//"):
            continue
        edges[label] = {node.attrib["name"] for node in rule.findall("rule-input")}
        if rule.attrib.get("class", "").endswith("_test"):
            tests.add(label)
        for output in rule.findall("rule-output"):
            edges.setdefault(output.attrib["name"], set()).add(label)
    if not edges:
        raise ValueError("empty Bazel query graph")
    return Graph(edges, tests)


async def query(root: Path, bazel: Path, output_root: Path) -> Graph:
    lock = json.loads((root / "tools/bazel/toolchains.lock.json").read_text())
    if file_hash(bazel) != lock["bazel"]["sha256"]:
        raise ValueError("Bazel executable checksum differs from pinned head lock")
    # Query only declared local rules; rule-input edges retain source files and
    # external labels without traversing all external toolchain implementations.
    proc, stdout, stderr = await run_owned_process(
        str(bazel),
        "--batch",
        "--nosystem_rc",
        "--nohome_rc",
        "--host_jvm_args=-XX:ActiveProcessorCount=2",
        f"--output_user_root={output_root}",
        f"--install_base={output_root.parent / 'install'}",
        "query",
        "//...",
        "--output=xml",
        "--noshow_progress",
        "--loading_phase_threads=1",
        "--lockfile_mode=error",
        cwd=str(root),
        timeout=300,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"HOME": str(output_root.parent), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    if proc.returncode:
        raise ValueError(
            f"Bazel query failed (exit {proc.returncode}): {stderr.decode(errors='replace')[-4000:]}"
        )
    return parse_graph(stdout)


async def identity(root: Path, expected: str) -> dict:
    if await pin(root, "HEAD") != expected or await git(
        root, "status", "--porcelain", "--untracked-files=all"
    ):
        raise ValueError("snapshot identity changed: query requires clean exact-head BUILD files")
    tracked = (await git(root, "ls-files", "-z")).split("\0")
    graph_files = [
        p
        for p in tracked
        if p
        and (
            Path(p).name in {"BUILD", "BUILD.bazel", "MODULE.bazel", ".bazelrc", ".bazelversion"}
            or p.endswith(".bzl")
            or p == MANIFEST
        )
    ]
    return {
        "head": expected,
        "graph_inputs_sha256": digest(bound_files(root, graph_files)),
        "lock_sha256": digest(bound_files(root, LOCKS)),
        "generator_registry": {"version": VERSION, "sha256": digest(bound_files(root, REGISTRY))},
        "manifest_sha256": file_hash(root / MANIFEST),
        "selection_sha256": digest(
            bound_files(
                root,
                (
                    "tests/selection_catalogue.json",
                    "tests/selection_rules.yaml",
                    "tests/selection_areas.yaml",
                ),
            )
        ),
    }


def unresolved_loads(root: Path, changes) -> list[str]:
    """Changed dynamic loads/resources cannot be proved by an import graph.

    Literal local imports must exist at the queried head. Runtime-selected
    imports and resource discovery remain explicitly unresolved, even when a
    broad BUILD filegroup happens to include today's files.
    """
    problems = []
    for change in changes:
        if change.status == "deleted" or not change.path.endswith(".py"):
            continue
        try:
            tree = ast.parse((root / change.path).read_bytes())
        except (SyntaxError, ValueError):
            problems.append("unresolved_python:" + change.path)
            continue
        aliases = {
            alias.asname: alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.asname
        }
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Name) and node.id == "pytest_plugins":
                problems.append("unresolved_plugin_discovery:" + change.path)
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            elif isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                name = aliases.get(name, name)
                if name in {"import_module", "__import__", "spec_from_file_location"}:
                    problems.append("unresolved_dynamic_load:" + change.path)
                if name in {"entry_points", "iter_entry_points", "iter_modules", "walk_packages"}:
                    problems.append("unresolved_plugin_discovery:" + change.path)
                if name in {
                    "read_text",
                    "read_bytes",
                    "open",
                    "glob",
                    "rglob",
                    "walk",
                    "files",
                    "open_text",
                    "resource_filename",
                    "resource_stream",
                    "resource_string",
                    "read_binary",
                    "getfixturevalue",
                }:
                    problems.append("unresolved_resource:" + change.path)
            for name in names:
                if name.startswith(("src.", "tests.")):
                    path = root.joinpath(*name.split("."))
                    if not path.with_suffix(".py").is_file() and not path.is_dir():
                        problems.append("unresolved_import:" + change.path + ":" + name)
    return sorted(set(problems))


def compare(
    manifest: dict,
    suite: str,
    graphs: dict[str, Graph | None],
    changes,
    static,
    mandatory,
    problems: list[str],
) -> dict:
    blockers = list(problems)
    head_graph = graphs["head"]
    if suite in manifest["suites"]:
        obligation = manifest["suites"][suite]
        all_targets = set(obligation["targets"])
        module_targets = {
            p: manifest["modules"][p]["targets"][suite] for p in obligation["modules"]
        }
        marker = obligation["marker"]
    elif suite in {"dashboard", "generated"}:
        # Missing graph means even the non-Python target universe is unknown.
        all_targets = (
            head_graph.reached({f"//checks:{suite}"}) & head_graph.tests
            if head_graph
            else {f"//checks:{suite}"}
        )
        module_targets, marker = {}, None
        if not all_targets:
            blockers.append("missing_suite_obligations:" + suite)
    else:
        raise ValueError("unknown manifest suite: " + suite)
    if head_graph and all_targets - head_graph.tests:
        blockers.append("missing_graph_targets")
    affected, owners, per_path = set(), set(), {}
    for change in changes:
        found_owners, found_tests = set(), set()
        sides = [("base", change.old_path or change.path)] if change.status != "added" else []
        if change.status != "deleted":
            sides.append(("head", change.path))
        for side, path in sides:
            graph = graphs[side]
            if graph:
                own, tests = graph.affected(path)
                found_owners.update(own)
                found_tests.update(tests)
                if not own:
                    blockers.append("unresolved_path:" + side + ":" + path)
        owners.update(found_owners)
        affected.update(found_tests & all_targets)
        per_path[change.path] = {
            "status": change.status,
            "old_path": change.old_path,
            "owners": sorted(found_owners),
            "tests": sorted(found_tests),
        }
    expected_static = {module_targets[p] for p in static.modules if p in module_targets}
    expected_mandatory = {module_targets[p] for p in mandatory.modules if p in module_targets}
    if not static.complete or static.unknown_outputs:
        blockers.append("static_incomplete:" + (static.reason or "unknown_outputs"))
    missing = (expected_static | expected_mandatory) - affected
    if missing:
        blockers.append("under_declared")
    blockers = sorted(set(blockers))
    return {
        "marker": marker,
        "eligible_targets": sorted(all_targets),
        "owners": sorted(owners),
        "per_path": per_path,
        "graph_targets": sorted(affected),
        "static_targets": sorted(expected_static),
        "mandatory_targets": sorted(expected_mandatory),
        "missing_targets": sorted(missing),
        "graph_only_targets": sorted(affected - expected_static - expected_mandatory),
        "required_targets": sorted(
            all_targets if blockers else affected | expected_static | expected_mandatory
        ),
        "widened": bool(blockers),
        "promotion_blocked": bool(blockers),
        "blockers": blockers,
    }


async def audit(
    repo: Path,
    base: str,
    head: str,
    bazel: Path,
    suites: list[str],
    *,
    query_graph=query,
    static_adapter=None,
) -> dict:
    base, head = await pin(repo, base), await pin(repo, head)
    if await git(repo, "merge-base", base, head) != base:
        raise ValueError("base must be an ancestor of head for the pinned static adapter")
    changes = changes_from_diff(
        await git(repo, "diff", "--name-status", "-z", "--find-renames", base, head, "--")
    )
    with tempfile.TemporaryDirectory(prefix="aq-impact-") as temporary:
        scratch = Path(temporary)
        roots, identities, graphs, problems, diagnostics = {}, {}, {}, [], {}
        for side, sha in (("base", base), ("head", head)):
            root = scratch / side
            # Only local objects, no fetch, no changes to the caller's checkout.
            await git(repo, "clone", "--shared", "--no-checkout", "--", str(repo), str(root))
            await git(root, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", sha)
            roots[side] = root
            identities[side] = await identity(root, sha)
            try:
                graphs[side] = await query_graph(root, bazel, scratch / (side + "-output"))
            except (ValueError, OSError, TimeoutError, ET.ParseError) as exc:
                graphs[side] = None
                problems.append(f"{side}_query_unavailable:{type(exc).__name__}")
                diagnostics[side] = str(exc)
            if identities[side] != await identity(root, sha):
                raise ValueError("graph inputs changed during query")
            identities[side]["graph_sha256"] = graphs[side].identity() if graphs[side] else None
        root = roots["head"]
        problems.extend(unresolved_loads(root, changes))
        catalogue = load_catalogue(root / "tests/selection_catalogue.json")
        base_catalogue = load_catalogue(roots["base"] / "tests/selection_catalogue.json")
        rules = load_rules(root / "tests/selection_rules.yaml", catalogue)
        snapshot = ChangeSnapshot(str(root), base, base, head, changes, "", True, None, None, 0.0)
        static = await (static_adapter or PytestImpactedAdapter()).impacted(
            snapshot, catalogue=catalogue
        )
        mandatory = mandatory_set(snapshot, catalogue, rules, base_catalogue=base_catalogue)
        # Static analysis must not change the pinned checkout either.
        if await identity(root, head) != {
            k: v for k, v in identities["head"].items() if k != "graph_sha256"
        }:
            raise ValueError("snapshot changed during cross-check")
        manifest = json.loads((root / MANIFEST).read_text())
        reports = {
            suite: compare(manifest, suite, graphs, changes, static, mandatory, problems)
            for suite in sorted(set(suites))
        }
        payload = {
            "signature_version": VERSION,
            "observational": True,
            "authoritative": False,
            "base": base,
            "head": head,
            "identities": identities,
            "audit_tool_sha256": file_hash(Path(__file__)),
            "static_adapter_sha256": digest(
                bound_files(
                    ROOT,
                    (
                        "src/test_selection/static_impact.py",
                        "src/test_selection/load_closure.py",
                        "src/test_selection/mandatory.py",
                        "src/test_selection/catalogue.py",
                    ),
                )
            ),
            "static": {
                k: v for k, v in asdict(static).items() if k not in {"modules", "elapsed_ms"}
            },
            "mandatory": {
                "full_required": mandatory.full_required,
                "global_reasons": mandatory.global_reasons,
            },
            "suites": reports,
            "promotion_blocked": any(r["promotion_blocked"] for r in reports.values()),
        }
        return {**payload, "signature_sha256": digest(payload), "diagnostics": diagnostics}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--bazel", type=Path, required=True)
    parser.add_argument("--suite", action="append", default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(
            audit(
                args.repo.resolve(),
                args.base,
                args.head,
                args.bazel.resolve(),
                args.suite or ["module"],
            )
        )
        args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    except (ValueError, OSError, GitError) as exc:
        parser.exit(2, f"Impact audit refused: {exc}\n")
    print(
        f"Observational signature {report['signature_sha256']}; promotion_blocked={report['promotion_blocked']}"
    )
    return int(report["promotion_blocked"])


if __name__ == "__main__":
    raise SystemExit(main())
