"""Source-only integration inventory. No daemon, database, git history or Node needed.

The manifest is an ownership decision, never an automatically raised ceiling.
Python definitions use AST spans; shared schema/container entries are separate
units. TypeScript units are complete top-level declarations (including multiline
imports and component bodies). All measurements count newline bytes, like wc -l.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re


PINNED_REVISION = "2483f634aef2ece9fd3a6e5d92174cc145aa564a"
LANDING_REVISION = "8e73e088fa5050621a165b9d13f90ef88334572d"
BASELINE_ENGINE_LINES = 82_627
BASELINE_OWNED_LINES = 140_499
REPLACEMENT_ALLOWANCE = 15_000
FINAL_ENGINE_CAP = 11_999
FINAL_TOTAL_CAP = 19_999
FINAL_TABLE_CAP = 10
TARGET_TABLES = 5
EXTRA_TABLES = frozenset({
    "task_delivery_receipts", "task_branch_origins", "task_integration_checkpoints",
    "epic_dependencies", "project_integration_schedules", "project_integration_leases",
})
RETAINED_TABLES = frozenset({
    "integration_branch_owners", "integration_check_evidence", "integration_batches",
    "integration_batch_members", "integration_review_evidence",
})
MANIFEST_PATH = Path(__file__).with_name("integration_ownership.json")

# Direct semantic signals, independent of the file's location. Imported aliases
# and owned-symbol references extend this set below; comments alone are not proof.
SIGNAL = re.compile(
    r"integration[_.]|(?:^|\.)integration(?:\.|$)|Integration[A-Z_]|INTEGRATION_|hierarchical_integration|"
    r"EpicDelivery|epic_delivery|epicDelivery|delivery_status|"
    + "|".join(sorted(EXTRA_TABLES))
)
STORAGE_METHOD = re.compile(
    r"(?:set|upsert|append|write|record|log|save).*"
    r"(?:task_meta|metadata|op_event|operation_event|journal)|^log_event$"
)
STORAGE_TABLES = {"task_metadata", "op_events", "operation_events", "events",
                  "integration_subject_journal"}
SQL_STORAGE_WRITE = re.compile(
    r"\b(?:insert\s+into|update|delete\s+from)\s+[\"\w.]*"
    r"(?:task_metadata|op_events|operation_events|events|integration_subject_journal)\b",
    re.IGNORECASE,
)
WRITE_CALLS = {"insert", "update", "delete"}
TS_START = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?"
    r"(?:function|class|interface|type|const|let|var|import)\b"
)


def integration_table(name: str) -> bool:
    return name.startswith("integration_") or name in EXTRA_TABLES


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def tokens(node: ast.AST) -> set[str]:
    result = set()
    for part in ast.walk(node):
        if isinstance(part, ast.Name):
            result.add(part.id)
        elif isinstance(part, ast.Attribute):
            result.add(part.attr)
        elif isinstance(part, ast.Constant) and isinstance(part.value, str):
            result.add(part.value)
        elif isinstance(part, ast.alias):
            result.add(part.name)
            if part.asname:
                result.add(part.asname)
    return result


def fingerprint(node: ast.AST) -> str:
    return digest(ast.dump(node, include_attributes=False))


@dataclass(frozen=True)
class Unit:
    path: str
    name: str
    start: int
    end: int
    fingerprint: str
    references: frozenset[str]
    signal: bool
    kind: str = "text"

    @property
    def key(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass
class Source:
    path: str
    data: bytes
    units: list[Unit]
    sinks: dict[str, dict]
    tables: dict[str, tuple[int, int]]
    imports: dict[str, str]


def attached_start(lines: list[str], start: int) -> int:
    """Attach preceding comments/blanks, stopping at the previous code line."""
    while start > 1:
        previous = lines[start - 2].strip()
        if previous and not previous.startswith(("#", "//", "/*", "*", "*/")):
            break
        start -= 1
    return start


def table_declarations(tree: ast.AST, *, owned_only: bool = True) -> dict[str, tuple[int, int]]:
    """Count Table calls by their literal SQL name, including aliased imports."""
    aliases = {"Table"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy":
            aliases.update(alias.asname or alias.name for alias in node.names
                           if alias.name == "Table")
    result = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and call_name(node.func) in aliases
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            name = node.args[0].value
            if not owned_only or integration_table(name):
                if name in result:
                    raise AssertionError(f"duplicate integration table declaration: {name}")
                result[name] = (node.lineno, node.end_lineno)
    return result


def python_source(path: str, data: bytes) -> Source:
    text = data.decode()
    lines = text.splitlines()
    tree = ast.parse(text, filename=path)
    aliases = set()
    imports = {}
    storage_aliases = set(STORAGE_TABLES)
    write_aliases = set(WRITE_CALLS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and SIGNAL.search(node.module or ""):
            aliases.update(alias.asname or alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("sqlalchemy"):
                write_aliases.update(alias.asname or alias.name for alias in node.names
                                     if alias.name in WRITE_CALLS)
            if node.level:
                package = path.removesuffix(".py").split("/")[:-1]
                module = ".".join(package[:len(package) - node.level + 1] + [module]).rstrip(".")
            if module.startswith("src."):
                for alias in node.names:
                    imports[alias.asname or alias.name] = module.replace(".", "/") + ".py::" + alias.name
                    if alias.name in STORAGE_TABLES:
                        storage_aliases.add(alias.asname or alias.name)
    units = []
    sinks = {}

    def add(node: ast.AST, name: str, *, start=None, end=None, refs=None):
        references = tokens(node) if refs is None else refs
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            references = references | {node.name}
        first = node.lineno if start is None else start
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([first] + [d.lineno for d in node.decorator_list])
        units.append(Unit(
            path, name, attached_start(lines, first), end or node.end_lineno,
            fingerprint(node), frozenset(references),
            bool(any(SIGNAL.search(token) for token in references) or aliases & references),
            type(node).__name__,
        ))

    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(node, prefix + node.name)
            elif isinstance(node, ast.ClassDef):
                if SIGNAL.search(node.name):
                    add(node, prefix + node.name)
                    # Methods retain fingerprints if later extracted from a class.
                    visit(node.body, prefix + node.name + ".")
                else:
                    refs = {node.name} | set().union(*(tokens(b) for b in node.bases))
                    add(node, prefix + node.name + ".<header>",
                        end=node.body[0].lineno - 1, refs=refs)
                    visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                name = prefix + ast.unparse(target)
                value = node.value
                if isinstance(value, ast.Call) and call_name(value.func) == "Table":
                    sql_name = value.args[0].value
                    if integration_table(sql_name):
                        add(node, name)
                    else:
                        # A new table imported by owned code must receive an
                        # ownership decision even if its SQL name is innocuous.
                        add(node, name, refs={sql_name})
                        # Charge owned columns/constraints in ordinary tables;
                        # the generic task/project storage is not owned wholesale.
                        for column in value.args[2:]:
                            if isinstance(column, ast.Call) and column.args:
                                label = ast.unparse(column.args[0])
                                add(column, name + "[" + label + "]")
                elif isinstance(value, ast.Dict) and not SIGNAL.search(name):
                    for key, item in zip(value.keys, value.values, strict=True):
                        label = ast.unparse(key) if key is not None else "**" + ast.unparse(item)
                        refs = tokens(item) | (tokens(key) if key is not None else set())
                        add(item, name + "[" + label + "]",
                            start=key.lineno if key is not None else item.lineno, refs=refs)
                elif isinstance(value, (ast.List, ast.Tuple, ast.Set)) and not SIGNAL.search(name):
                    for item in value.elts:
                        if isinstance(item, ast.Dict):
                            label = next((str(v.value) for k, v in zip(item.keys, item.values)
                                          if isinstance(k, ast.Constant) and k.value == "name"
                                          and isinstance(v, ast.Constant)), fingerprint(item)[:16])
                        elif isinstance(item, ast.Constant):
                            label = repr(item.value)
                        else:
                            label = ast.unparse(item)
                        add(item, name + "[" + label + "]")
                else:
                    add(node, name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                add(node, "import " + ast.unparse(node))
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                # Definitions behind TYPE_CHECKING/platform guards still count.
                visit(node.body, prefix)
                visit(getattr(node, "orelse", []), prefix)
                for handler in getattr(node, "handlers", []):
                    visit(handler.body, prefix)
            elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                      and isinstance(node.value.value, str)):
                add(node, prefix + "statement:" + fingerprint(node)[:16])

    visit(tree.body)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        refs = tokens(node)
        if (STORAGE_METHOD.search(name) or
                name in write_aliases and storage_aliases & refs or
                name == "text" and any(SQL_STORAGE_WRITE.search(token) for token in refs)):
            enclosing = [u for u in units if u.start <= node.lineno <= u.end]
            owner = min(enclosing, key=lambda u: u.end - u.start, default=None)
            key = f"{path}::{owner.name if owner else '<module>'}::{fingerprint(node)}"
            sinks[key] = {
                "line": node.lineno, "owner": owner.name if owner else "<module>",
                "call": name,
                "owner_fingerprint": owner.fingerprint if owner else fingerprint(tree),
                "integration_signal": bool(owner and owner.signal or any(
                    SIGNAL.search(token) for token in refs
                )),
            }
    return Source(path, data, units, sinks, table_declarations(tree, owned_only=False), imports)


def text_source(path: str, data: bytes) -> Source:
    lines = data.decode().splitlines()
    if path.endswith(".md"):
        starts = [i + 1 for i, line in enumerate(lines) if line.startswith("#")]
    else:
        starts = [i + 1 for i, line in enumerate(lines) if TS_START.match(line)]
    if not starts or starts[0] != 1:
        starts.insert(0, 1)
    starts = [attached_start(lines, start) for start in starts]
    units = []
    names = defaultdict(int)
    for index, start in enumerate(starts):
        end = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines)
        if end < start:
            continue
        text = "\n".join(lines[start - 1:end])
        declaration = next((line for line in lines[start - 1:end]
                            if line.startswith("#") or TS_START.match(line)), "<header>")
        name = declaration.strip()
        names[name] += 1
        if names[name] > 1:
            name += f" #{names[name]}"
        units.append(Unit(path, name, start, end, digest(text),
                          frozenset(re.findall(r"[\w.]+", text)), bool(SIGNAL.search(text))))
    return Source(path, data, units, {}, {}, {})


@lru_cache(maxsize=4)
def sources(root: Path) -> dict[str, Source]:
    result = {}
    for directory, extensions in (("src", {".py", ".md"}),
                                  ("dashboard/src", {".ts", ".tsx"}),
                                  ("migrations", {".py"})):
        for path in sorted((root / directory).rglob("*")):
            rel = path.relative_to(root).as_posix()
            if (path.suffix not in extensions or
                    any(part in {"__pycache__", "__tests__", "testUtils", "versions"}
                        for part in path.parts) or ".test." in path.name):
                continue
            if directory == "migrations" and path.name != "integration_guards.py":
                continue
            if path.suffix == ".md" and not (
                "playbooks/" in rel or rel == "src/integration/development_policy_template.md"
            ):
                continue
            if path.name == "manifest.md":
                continue  # review attestations, not executable policy
            data = path.read_bytes()
            result[rel] = (python_source if path.suffix == ".py" else text_source)(rel, data)
    return result


@lru_cache(maxsize=4096)
def _dedicated_group(path: str, groups: tuple) -> str | None:
    for group, patterns in groups:
        for pattern in patterns:
            if Path(path).match(pattern):
                return group
    return None


def dedicated_group(path: str, manifest: dict) -> str | None:
    groups = tuple((group, tuple(patterns)) for group, patterns in manifest["dedicated"].items())
    return _dedicated_group(path, groups)


def inventory(root: Path, manifest: dict) -> dict:
    tree = sources(root)
    file_identities = set(manifest.get("owned_file_fingerprints", []))
    relocated_files = {path for path, source in tree.items()
                       if hashlib.sha256(source.data).hexdigest() in file_identities}
    owned = set(manifest["shared"])
    exclusions = manifest["exclusions"]
    identities = set(manifest["owned_fingerprints"])
    units = [unit for source in tree.values() for unit in source.units]
    candidates = {unit.key for unit in units if unit.signal or unit.key in owned
                  or unit.fingerprint in identities or unit.path in relocated_files
                  or dedicated_group(unit.path, manifest)}
    by_key = {unit.key: unit for unit in units}
    # An alias imported from a shared owned module is ownership evidence even
    # when neither the alias nor its caller contains "integration".
    owned_targets = {unit.key for unit in units if unit.key in owned
                     or unit.path in relocated_files
                     or dedicated_group(unit.path, manifest)}
    for unit in units:
        if any(alias in unit.references and target in owned_targets
               for alias, target in tree[unit.path].imports.items()):
            candidates.add(unit.key)
    generic = set(manifest.get("generic_dependencies", {}))
    # Follow imported pure helpers too: relocating/renaming a helper and importing
    # it back cannot hide ownership. Generic dependencies are explicitly reviewed.
    while True:
        added = set()
        for key in candidates:
            unit = by_key[key]
            if key in exclusions:
                continue
            for alias, target in tree[unit.path].imports.items():
                if alias in unit.references and target not in generic and target in by_key:
                    added.add(target)
        added -= candidates
        if not added:
            break
        candidates.update(added)
    unknown = []
    selected = defaultdict(set)
    for path, source in tree.items():
        group = dedicated_group(path, manifest)
        if group or path in relocated_files:
            selected[path].update(range(1, source.data.count(b"\n") + 1))
        for unit in source.units:
            relocated = unit.fingerprint in identities
            is_owned = unit.key in owned or relocated
            candidate = unit.key in candidates
            if candidate and unit.key not in exclusions:
                if not group and path not in relocated_files and not is_owned:
                    unknown.append(f"{unit.key} (lines {unit.start}-{unit.end})")
                selected[path].update(range(unit.start, unit.end + 1))
    groups = defaultdict(int)
    spans = {}
    for path, rows in selected.items():
        source = tree[path]
        # splitlines() would charge an unterminated final line: wc -l does not.
        newline_count = source.data.count(b"\n")
        charged = sorted(row for row in rows if row <= newline_count)
        spans[path] = charged
        groups[dedicated_group(path, manifest) or "shared"] += len(charged)
    tables = {}
    for path, source in tree.items():
        for name, span in source.tables.items():
            if not integration_table(name) and not any(
                unit.key in candidates and unit.key not in exclusions
                and unit.start <= span[0] and unit.end >= span[1]
                for unit in source.units
            ):
                continue
            if name in tables:
                raise AssertionError(f"duplicate table {name}: {tables[name][0]} and {path}")
            tables[name] = (path, *span)
    current_sinks = {key: sink for source in tree.values() for key, sink in source.sinks.items()}
    unknown_sinks = sorted(key for key, sink in current_sinks.items()
                           if key not in manifest["storage_sinks"] or
                           sink["owner_fingerprint"] != manifest["storage_sinks"][key].get(
                               "owner_fingerprint"))
    legacy_sinks = sorted(key for key in current_sinks
                          if manifest["storage_sinks"].get(key, {}).get("disposition") == "retire")
    return {"groups": dict(groups), "total": sum(groups.values()),
            "spans": spans, "tables": tables, "unknown": sorted(unknown),
            "unknown_sinks": unknown_sinks, "legacy_sinks": legacy_sinks}


def budget_errors(report: dict, manifest: dict) -> list[str]:
    phase = manifest["phase"]
    if phase not in {"transition", "s4_modules", "final"}:
        return [f"unknown budget phase: {phase}"]
    if phase == "transition":
        engine_cap = BASELINE_ENGINE_LINES + REPLACEMENT_ALLOWANCE
        total_cap = BASELINE_OWNED_LINES + REPLACEMENT_ALLOWANCE
        table_cap = 45  # replacement reshapes the existing five, adds no table
    else:
        engine_cap, total_cap, table_cap = FINAL_ENGINE_CAP, FINAL_TOTAL_CAP, FINAL_TABLE_CAP
    errors = []
    for name, count, cap in (
        ("engine", report["groups"].get("engine", 0), engine_cap),
        ("owned total", report["total"], total_cap),
        ("tables", len(report["tables"]), table_cap),
    ):
        if count > cap:
            errors.append(f"{name}: {count:,} exceeds {cap:,} in {phase}")
    if phase != "transition" and report["legacy_sinks"]:
        errors.append("legacy journal/duplicate metadata writers remain: "
                      + ", ".join(report["legacy_sinks"]))
    return errors


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    manifest = load_manifest()
    report = inventory(root, manifest)
    print(json.dumps({key: value for key, value in report.items() if key != "spans"}, indent=2))
