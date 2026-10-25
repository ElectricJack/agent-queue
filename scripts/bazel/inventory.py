#!/usr/bin/env python3
"""Reproducible verification inventory; collection never executes tests.

Run from the checkout, with its normal disposable POSTGRES_TEST_DSN configured::

    python scripts/bazel/inventory.py collect --output collection.json
    python scripts/bazel/inventory.py report --collection collection.json --output inventory.json

Collection overrides the default marker deselection, preserving every node and
the *resolved* fixture closure (including overrides). Static resource evidence
is conservative: absence of evidence is unknown, never a declaration of purity.
Reports include all workflow commands, even non-test setup/generator/gate steps.
Historical pytest-split totals are not per-phase or controlled measurements.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import itertools
import json
import math
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = {
    "postgres": r"\b(Database|lease_dsn|LeasePool|asyncpg|create_async_engine|postgresql)\b",
    "git": r"\b(GitManager|git|git_repo)\b",
    "tmux": r"\b(tmux|TmuxManager|TmuxSessionProvider)\b",
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        # Installed plugin identity must not depend on a workstation's venv path.
        parts = path.as_posix().split("site-packages/")
        return "site-packages/" + parts[-1] if len(parts) > 1 else path.name


@lru_cache(maxsize=None)
def function_info(func, root: Path) -> dict:
    func = inspect.unwrap(func)
    try:
        path = Path(inspect.getfile(func))
        lines, line = inspect.getsourcelines(func)
        source = "".join(lines)
    except (TypeError, OSError):
        return {"source": None, "line": None, "resources": {}, "dynamic_fixtures": []}
    # Search executable source, excluding comments/docstrings and string prose.
    # A method's multiline string may start at column zero, defeating dedent.
    # Wrapping the original indented block preserves those string contents.
    tree = ast.parse("if True:\n" + source if source[0].isspace() else source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    executable = "\n".join(ast.unparse(n.func) for n in calls)
    commands = []
    for call in calls:
        if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)):
            args = call.args[0].elts
            if args and isinstance(args[0], ast.Constant) and args[0].value in {"git", "tmux"}:
                commands.append(args[0].value)
    executable += "\n" + "\n".join(commands)
    return {
        "source": relative(path, root), "line": line,
        "resources": {key: "source_call" for key, pattern in RESOURCES.items()
                      if re.search(pattern, executable)},
        "dynamic_fixtures": sorted({ast.unparse(n) for n in ast.walk(tree)
                                    if isinstance(n, ast.Call)
                                    and isinstance(n.func, ast.Attribute)
                                    and n.func.attr == "getfixturevalue"}),
    }


class Collector:
    """pytest plugin using pytest's resolved FixtureDefs, not filename guesses."""

    def __init__(self, root: Path):
        self.root = root
        self.nodes = []
        self.fixtures = {}
        self.collection_reports = []

    def fixture(self, definition) -> str:
        info = function_info(definition.func, self.root)
        identity = f"{info['source']}:{info['line']}:{definition.argname}"
        self.fixtures[identity] = {
            "id": identity, "name": definition.argname, "scope": definition.scope,
            "baseid": definition.baseid, "dependencies": list(definition.argnames), **info,
        }
        return identity

    def pytest_collection_modifyitems(self, session, config, items):
        for index, item in enumerate(items):
            info = function_info(item.obj, self.root)
            closure = {}
            for name in item.fixturenames:
                definitions = item._fixtureinfo.name2fixturedefs.get(name, ())
                # Keep the chain: an override can request the fixture it overrides.
                closure[name] = [self.fixture(d) for d in definitions or ()]
            evidence = {key: [] for key in RESOURCES}
            for origin, data in [("test", info)] + [
                (identity, self.fixtures[identity])
                for name, chain in closure.items() for identity in self.effective_chain(name, chain)
            ]:
                for resource, kind in data["resources"].items():
                    evidence[resource].append({"origin": origin, "kind": kind})
            self.nodes.append({
                "nodeid": item.nodeid, "module": item.nodeid.split("::")[0],
                "collection_index": index, "split_sort_name": str(item),
                "markers": sorted({m.name for m in item.iter_markers()}),
                "keywords": sorted(str(k) for k in item.keywords),
                "parameter_id": getattr(getattr(item, "callspec", None), "id", None),
                "fixtures": closure, "source": info,
                "resources": {key: {"status": "evidence" if value else "unknown",
                                     "evidence": value} for key, value in evidence.items()},
            })

    def effective_chain(self, name: str, chain: list[str]) -> list[str]:
        result = []
        for identity in reversed(chain):
            result.append(identity)
            if name not in self.fixtures[identity]["dependencies"]:
                break
        return result

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection_reports.append({"nodeid": report.nodeid, "outcome": report.outcome})

    def result(self, session) -> dict:
        # Include definitions with no consumer too; unused does not mean removable.
        for definitions in session._fixturemanager._arg2fixturedefs.values():
            for definition in definitions:
                self.fixture(definition)
        return {
            "schema_version": 1, "kind": "pytest_collection",
            "nodes": sorted(self.nodes, key=lambda n: n["nodeid"]),
            "fixtures": sorted(self.fixtures.values(), key=lambda f: f["id"]),
            "collection_reports": self.collection_reports,
            "analysis": "static source calls plus resolved fixtures; unknown is not pure",
        }

    def pytest_sessionfinish(self, session, exitstatus):
        self.document = self.result(session)
        self.document["exit_code"] = int(exitstatus)


def expand_matrix(matrix: dict) -> list[dict]:
    """Expand static GitHub axes, exclusions and includes without evaluating code."""
    axes = {k: v for k, v in matrix.items() if k not in {"include", "exclude"}}
    if any(not isinstance(v, list) for v in axes.values()):
        return [{"unresolved_matrix": matrix}]
    original = [dict(zip(axes, values, strict=True))
                for values in itertools.product(*axes.values())] if axes else []
    original = [row for row in original if not any(
        all(row.get(k) == v for k, v in exclusion.items())
        for exclusion in matrix.get("exclude", []))]
    rows = [dict(row) for row in original]
    extra = []
    for addition in matrix.get("include", []):
        matched = False
        for base, row in zip(original, rows, strict=True):
            if all(k not in base or base[k] == v for k, v in addition.items()):
                row.update(addition)
                matched = True
        if not matched:
            extra.append(dict(addition))
    rows.extend(extra)
    return rows or ([{}] if not matrix else [])


def render_matrix(value: str, matrix: dict) -> str:
    def replace(match):
        result = matrix
        for part in match[1].split("."):
            if not isinstance(result, dict) or part not in result:
                return match[0]
            result = result[part]
        return str(result)
    return re.sub(r"\$\{\{\s*matrix\.([\w.]+)\s*\}\}", replace, value)


def matrix_step_enabled(condition, matrix: dict) -> bool | None:
    """Resolve a static matrix equality; leave runtime GitHub conditions unknown."""
    if condition is None:
        return True
    condition = str(condition).removeprefix("${{").removesuffix("}}").strip()
    match = re.fullmatch(r"matrix\.([\w.]+)\s*==\s*'([^']*)'", condition)
    if match:
        resolved = render_matrix("${{ matrix." + match[1] + " }}", matrix)
        return resolved == match[2]
    return None


def workflow_inventory(root: Path) -> list[dict]:
    rows = []
    for path in sorted((root / ".github/workflows").glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in workflow.get("jobs", {}).items():
            matrix = job.get("strategy", {}).get("matrix", {})
            instances = expand_matrix(matrix) if isinstance(matrix, dict) else [
                {"unresolved_matrix": matrix}]
            for instance in instances:
                identity = json.dumps(instance, sort_keys=True, separators=(",", ":"))
                rows.append({
                    "id": f"{path.name}:{job_id}:{identity}",
                    "workflow": relative(path, root), "workflow_sha256": fingerprint(path),
                    "job": job_id, "name": render_matrix(job.get("name", job_id), instance),
                    "matrix": instance, "condition": job.get("if"),
                    "reusable_workflow": job.get("uses"),
                    "steps": [{
                        "name": step.get("name"), "condition": step.get("if"),
                        "command": render_matrix(step["run"], instance),
                        "working_directory": step.get("working-directory", "."),
                        "timeout_minutes": step.get("timeout-minutes"),
                        "matrix_enabled": matrix_step_enabled(step.get("if"), instance),
                    } for step in job.get("steps", []) if "run" in step],
                })
    return rows


def option(tokens: list[str], name: str, default=None):
    for i, token in enumerate(tokens):
        if token == name:
            return tokens[i + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return default


@dataclass
class SplitItem:
    nodeid: str
    sort_name: str

    def __str__(self):
        return self.sort_name


def partition(nodes: list[dict], selected: list[str], tokens: list[str], durations: dict):
    """Use the installed reference algorithm, including its missing-timing estimate."""
    import importlib.metadata

    from pytest_split.algorithms import LeastDurationAlgorithm

    if option(tokens, "--splitting-algorithm") != "least_duration":
        raise ValueError("unsupported split algorithm; collect its actual membership instead")
    selected_set = set(selected)
    ordered = sorted((n for n in nodes if n["nodeid"] in selected_set),
                     key=lambda n: n["collection_index"])
    items = [SplitItem(n["nodeid"], n["split_sort_name"]) for n in ordered]
    groups = LeastDurationAlgorithm()(int(option(tokens, "--splits")), items, durations)
    group = groups[int(option(tokens, "--group")) - 1]
    return [n.nodeid for n in group.selected], importlib.metadata.version("pytest-split")


def select_nodes(nodes: list[dict], command: str, default_marker: str) -> list[str]:
    """Marker/path/keyword membership before timing-based split partitioning."""
    from _pytest.mark.expression import Expression

    tokens = shlex.split(command)
    marker_text = option(tokens, "-m", default_marker)
    keyword_text = option(tokens, "-k", "")
    marker = Expression.compile(marker_text)
    keyword = Expression.compile(keyword_text)
    paths = [t.rstrip("/") for t in tokens if t.startswith("tests/")]
    ignored = [tokens[i + 1] if t == "--ignore" else t.split("=", 1)[1]
               for i, t in enumerate(tokens) if t == "--ignore" or t.startswith("--ignore=")]
    return [n["nodeid"] for n in nodes
            if (not paths or any(n["nodeid"].startswith(p) for p in paths))
            and not any(n["module"].startswith(p) for p in ignored)
            and (not marker_text or marker.evaluate(lambda name: name in n["markers"]))
            and (not keyword_text or keyword.evaluate(lambda name: any(
                name.lower() in k.lower() for k in n["keywords"])))]


def build_inventory(root: Path, collection: dict, durations: dict,
                    phase_rows: list[dict], e2e_rows: list[dict]) -> dict:
    import tomllib

    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    ini = config["tool"]["pytest"]["ini_options"]
    default_marker = option(shlex.split(ini.get("addopts", "")), "-m", "")
    nodes = collection["nodes"]
    for value in durations.values():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError("historical durations must be finite and nonnegative")
    ids = {n["nodeid"] for n in nodes}
    workflows = workflow_inventory(root)
    obligations = []
    for job in workflows:
        for index, step in enumerate(job["steps"]):
            if step["matrix_enabled"] is False:
                continue
            for line in step["command"].splitlines():
                if not re.match(r"\s*(pytest\b|aq test\b|python -m pytest\b)", line):
                    continue
                selected = select_nodes(nodes, line, default_marker)
                tokens = shlex.split(line)
                split_version = None
                if option(tokens, "--splits"):
                    selected, split_version = partition(nodes, selected, tokens, durations)
                obligations.append({
                    "id": f"{job['id']}:{index}", "job": job["id"], "command": line,
                    "step_condition": step["condition"],
                    "marker_expression": option(tokens, "-m", default_marker),
                    "split_count": option(tokens, "--splits"),
                    "split_group": option(tokens, "--group"),
                    "membership": "selected", "split_reference_version": split_version,
                    "nodeids": selected, "node_count": len(selected),
                })
    phase_map = {}
    for row in phase_rows:
        if row["phase"] not in {"setup", "call", "teardown"}:
            raise ValueError("phase must be setup, call or teardown")
        key = (row["observation_id"], row["nodeid"], row["phase"])
        if key in phase_map:
            raise ValueError(f"duplicate phase observation {key}")
        value = row["seconds"]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError("phase seconds must be finite and nonnegative")
        phase_map[key] = {k: row[k] for k in ("observation_id", "nodeid", "phase", "seconds",
                                             "shared_fixture") if k in row}
    # No unobserved phase is filled with zero; pytest-split stores aggregate totals.
    timings = [{"nodeid": n["nodeid"], "historical_total_seconds": durations.get(n["nodeid"]),
                "phase_observations": [row for key, row in phase_map.items()
                                       if key[1] == n["nodeid"]]} for n in nodes]
    covered = {n for arm in obligations for n in arm["nodeids"]}
    modules = len({n["module"] for n in nodes})
    e2e_groups = len([j for j in workflows if j["workflow"] == ".github/workflows/tests.yml"
                      and j["job"] == "e2e-cli"])
    dashboard_commands = sum(s["command"] in {"npm run typecheck", "npm run build"}
                             for j in workflows if j["job"] == "dashboard"
                             for s in j["steps"])
    e2e_definitions = {}
    e2e_source = root / "tests/test_e2e_cli_stateful.py"
    if e2e_source.exists():
        for statement in ast.parse(e2e_source.read_text(encoding="utf-8")).body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id in {"SCENARIO_GROUPS", "PREREQUISITES"}:
                        e2e_definitions[target.id] = ast.literal_eval(statement.value)
    return {
        "schema_version": 1, "kind": "verification_inventory",
        "collection": collection, "workflows": workflows, "python_obligations": obligations,
        "dashboard_scripts": read_json(root / "dashboard/package.json")["scripts"],
        "e2e_scenarios": e2e_definitions,
        "pytest_markers": ini.get("markers", []), "default_marker_expression": default_marker,
        "timings": timings,
        "shared_fixture_setup_observations": [r for r in phase_map.values()
                                               if r["phase"] == "setup" and r.get("shared_fixture")],
        "e2e_historical_timings": [{k: r[k] for k in ("group", "stage", "seconds", "item",
                                                    "item_seconds") if k in r} for r in e2e_rows],
        "timing_provenance": "historical only; not controlled or a live delivery baseline",
        "stale_timing_nodeids": sorted(set(durations) - ids),
        "unmapped_phase_rows": [r for r in phase_map.values() if r["nodeid"] not in ids],
        "not_selected_by_workflow": sorted(ids - covered),
        "counts": {
            "modules": modules, "nodes": len(nodes),
            "fixtures": len(collection["fixtures"]),
            "python_module_target_estimate": modules,
            "e2e_group_target_estimate": e2e_groups,
            "total_target_estimate": modules + max(0, e2e_groups - 1) + dashboard_commands,
            "python_obligations": len(obligations), "timed_nodes": len(ids & durations.keys()),
            "nodes_missing_phase_timings": sum(not t["phase_observations"] for t in timings),
            "historical_nodes_over_60_seconds": sum(
                t["historical_total_seconds"] is not None
                and t["historical_total_seconds"] > 60 for t in timings),
            "markers": dict(sorted(Counter(m for n in nodes for m in n["markers"]).items())),
            "resource_analysis": {
                resource: dict(Counter(n.get("resources", {}).get(resource, {}).get("status", "unknown")
                                       for n in nodes)) for resource in RESOURCES},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--output", type=Path, required=True)
    report = sub.add_parser("report")
    report.add_argument("--collection", type=Path, required=True)
    report.add_argument("--durations", type=Path, default=ROOT / ".test_durations")
    report.add_argument("--phases", type=Path, help="JSON list: observation_id,nodeid,phase,seconds")
    report.add_argument("--e2e-timings", type=Path, help="AQ_E2E_TIMINGS_DIR JSONL directory")
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "collect":
        import pytest

        sys.path.insert(0, str(ROOT))
        plugin = Collector(ROOT)
        code = pytest.main([str(ROOT / "tests"), "--collect-only", "-q", "-m", "",
                            "-o", "addopts=-p no:impacted", "-p", "no:xdist"], plugins=[plugin])
        if hasattr(plugin, "document"):
            plugin.document["source_head"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            plugin.document["source_files"] = {
                relative(p, ROOT): fingerprint(p)
                for p in sorted((ROOT / "tests").rglob("*.py"))
            }
            plugin.document["pytest_version"] = pytest.__version__
            plugin.document["python_version"] = sys.version
            plugin.document["configuration_sha256"] = fingerprint(ROOT / "pyproject.toml")
            plugin.document["tool_sha256"] = fingerprint(Path(__file__))
            write_json(args.output, plugin.document)
        return int(code)
    e2e = []
    if args.e2e_timings:
        for path in sorted(args.e2e_timings.glob("*.jsonl")):
            e2e.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    result = build_inventory(ROOT, read_json(args.collection), read_json(args.durations),
                             read_json(args.phases) if args.phases else [], e2e)
    result["input_sha256"] = {str(p): fingerprint(p) for p in
                              [args.collection, args.durations, *([args.phases] if args.phases else [])]}
    result["tool_sha256"] = fingerprint(Path(__file__))
    result["timing_sources"] = {
        "historical_totals": str(args.durations),
        "phases": str(args.phases) if args.phases else None,
        "e2e_directory": str(args.e2e_timings) if args.e2e_timings else None,
        "missing": (["phase_timings"] if not args.phases else [])
                   + (["e2e_timings"] if not args.e2e_timings else []),
    }
    write_json(args.output, result)
    print(json.dumps(result["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
