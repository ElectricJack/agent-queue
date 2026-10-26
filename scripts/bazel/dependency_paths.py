"""Discover files read through statically composed repository paths."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

_SKIP = {
    "__pycache__", ".git", ".aq", "node_modules", ".venv",
    ".codex", ".pytest_cache", ".aq-worktree.json",
}

SOURCE_INVENTORY = ".aq/bazel-source-files.json"


def repository_files(root: Path) -> frozenset[str]:
    """Snapshot tracked inputs, or an archive's explicit source inventory.

    Only a Git marker at this root is considered: temporary fixtures must not
    inherit the enclosing checkout's index. Archives may supply
    ``.aq/bazel-source-files.json`` as ``{"schema_version": 1, "files": [...]}``.
    Small non-Git fixtures retain filtered filesystem discovery.
    """
    root = root.resolve()
    paths = None
    if (root / ".git").exists():
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root,
            capture_output=True, text=True, check=False,
        )
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root:
            tracked = subprocess.run(
                ["git", "ls-files", "--cached", "-z"], cwd=root,
                capture_output=True, check=False,
            )
            if tracked.returncode:
                raise ValueError("cannot read tracked Bazel source inventory")
            paths = tracked.stdout.decode("utf-8").split("\0")[:-1]
    inventory = root / SOURCE_INVENTORY
    if paths is None and inventory.is_file():
        document = json.loads(inventory.read_text(encoding="utf-8"))
        if (not isinstance(document, dict) or document.get("schema_version") != 1
                or not isinstance(document.get("files"), list)):
            raise ValueError("invalid Bazel archive source inventory")
        paths = document["files"]
        if any(not isinstance(path, str) or not path or Path(path).is_absolute()
               or ".." in Path(path).parts or Path(path).as_posix() != path for path in paths):
            raise ValueError("invalid path in Bazel archive source inventory")
    if paths is None:
        if (root / "scripts/bazel/generate_targets.py").is_file():
            raise ValueError(f"Bazel source archive requires {SOURCE_INVENTORY}")
        paths = (path.relative_to(root).as_posix() for path in root.rglob("*"))
    return frozenset(
        path for path in paths
        if not any(part in _SKIP for part in Path(path).parts)
        and (root / path).is_file() and (root / path).resolve().is_relative_to(root)
        and not path.startswith("dashboard/dist/")
        and not path.endswith((".pyc", ".tsbuildinfo"))
    )


def repository_data(
    root: Path, path: str, nodes: list[ast.AST], *, context: ast.AST | None = None,
    source_files: frozenset[str] | None = None,
) -> set[str]:
    """Resolve literal Path expressions without executing repository code.

    A directory assignment alone is not a request for its entire contents.
    Only existing files and explicit directory reads contribute inputs. ``context``
    supplies global bindings when ``nodes`` contains a selected fixture body; its
    unselected expressions never contribute inputs.
    """
    root = root.resolve()
    source_files = repository_files(root) if source_files is None else source_files
    walked = [node for top in nodes for node in ast.walk(top)]
    explicit_reads = set()
    for node in walked:
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "read_text", "read_bytes", "open",
        }:
            explicit_reads.add(id(node.func.value))
        elif isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            explicit_reads.add(id(node.args[0]))
    scopes: dict[int, dict[str, list[ast.AST | str | None]]] = {0: {}}
    parents: dict[int, int] = {}
    node_scopes: dict[int, int] = {}

    def bind(scope: int, name: str, value: ast.AST | str | None):
        scopes[scope].setdefault(name, []).append(value)

    def index(node: ast.AST, scope: int):
        node_scopes[id(node)] = scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            if hasattr(node, "name"):
                bind(scope, node.name, None)
            child_scope = id(node)
            scopes[child_scope] = {}
            parents[child_scope] = scope
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for arg in ast.walk(node.args):
                    if isinstance(arg, ast.arg):
                        bind(child_scope, arg.arg, None)
            for field, value in ast.iter_fields(node):
                children = value if isinstance(value, list) else [value]
                for child in children:
                    if isinstance(child, ast.AST):
                        index(child, child_scope if field == "body" else scope)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                bind(scope, alias.asname or alias.name.split(".")[0],
                     alias.name if alias.asname else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bind(scope, alias.asname or alias.name, f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bind(scope, target.id, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            if isinstance(node.target, ast.Name):
                bind(scope, node.target.id, node.value)
        elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)):
            for target in ast.walk(node.target):
                if isinstance(target, ast.Name):
                    bind(scope, target.id, None)
        for child in ast.iter_child_nodes(node):
            index(child, scope)

    for top in [context] if context is not None else nodes:
        index(top, 0)

    def bindings(node: ast.Name):
        scope = node_scopes.get(id(node), 0)
        while node.id not in scopes[scope] and scope in parents:
            scope = parents[scope]
        return scopes[scope].get(node.id, [])

    def qualified(node: ast.AST, seen: frozenset[int] = frozenset()) -> str:
        if isinstance(node, ast.Name):
            if id(node) in seen:
                return ""
            definitions = bindings(node)
            values = {value if isinstance(value, str) else
                      qualified(value, seen | {id(node)}) if value is not None else ""
                      for value in definitions}
            return values.pop() if len(values) == 1 else "" if definitions else node.id
        if isinstance(node, ast.Attribute):
            return qualified(node.value, seen) + "." + node.attr
        return ""

    def resolve(node: ast.AST, seen: frozenset[int] = frozenset()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return root / path
            if id(node) in seen:
                return None
            values = {
                resolve(value, seen | {id(node)}) if isinstance(value, ast.AST) else None
                for value in bindings(node)
            }
            # Conflicting or dynamic bindings must never become a guessed path.
            return values.pop() if len(values) == 1 else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base, child = resolve(node.left, seen), resolve(node.right, seen)
            if isinstance(base, Path) and isinstance(child, (str, Path)):
                return base / child
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = resolve(node.value, seen)
            if isinstance(base, Path):
                return base.parent
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents" and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)):
            base = resolve(node.value.value, seen)
            if isinstance(base, Path):
                try:
                    return base.parents[node.slice.value]
                except IndexError:
                    return None
        if isinstance(node, ast.Call):
            function = node.func
            name = function.id if isinstance(function, ast.Name) else (
                function.attr if isinstance(function, ast.Attribute) else ""
            )
            if qualified(function) in {
                "Path", "PurePath", "PurePosixPath", "pathlib.Path",
                "pathlib.PurePath", "pathlib.PurePosixPath",
            } and len(node.args) == 1 and not node.keywords:
                argument = resolve(node.args[0], seen)
                if isinstance(argument, (str, Path)):
                    return root / argument
            if qualified(function) in {"str", "os.fspath"} and len(node.args) == 1:
                return resolve(node.args[0], seen)
            if (isinstance(function, ast.Attribute) and not node.args and not node.keywords
                    and name in {"resolve", "absolute"}):
                return resolve(function.value, seen)
        return None

    result = set()

    def add(candidate: Path, *, explicit: bool = False):
        try:
            candidate = candidate.resolve()
            valid = candidate.is_relative_to(root) and candidate.is_file()
        except (OSError, ValueError, RuntimeError):
            return
        if not valid:
            return
        relative = candidate.relative_to(root)
        if relative.as_posix() == path and not explicit:
            return
        if (relative.as_posix() in source_files
                and not any(part in _SKIP for part in relative.parts)
                and candidate.name not in {"BUILD", "BUILD.bazel"}
                and not relative.as_posix().startswith("dashboard/dist/")
                and not candidate.name.endswith((".pyc", ".tsbuildinfo"))):
            result.add(relative.as_posix())

    for node in walked:
        candidate = resolve(node)
        if isinstance(candidate, Path):
            add(candidate, explicit=id(node) in explicit_reads)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"glob", "rglob"} and len(node.args) == 1
                and not node.keywords):
            base, pattern = resolve(node.func.value), resolve(node.args[0])
            if isinstance(base, Path) and isinstance(pattern, str):
                if not base.resolve().is_relative_to(root) or Path(pattern).is_absolute():
                    continue
                try:
                    for candidate in getattr(base, node.func.attr)(pattern):
                        add(candidate, explicit=True)
                except (OSError, ValueError, NotImplementedError):
                    # Unsupported/dynamic paths remain unresolved, never executed.
                    continue
        if not isinstance(node, ast.Call):
            continue
        base = None
        if (isinstance(node.func, ast.Attribute)
                and node.func.attr in {"iterdir", "walk", "is_dir"}
                and not node.args and not node.keywords):
            base = resolve(node.func.value)
        elif qualified(node.func) in {"os.listdir", "os.walk"}:
            argument = node.args[0] if node.args else next(
                (keyword.value for keyword in node.keywords if keyword.arg in {"path", "top"}),
                None,
            )
            if argument is not None:
                base = resolve(argument)
                if isinstance(base, str):
                    base = root / base
        if isinstance(base, Path):
            try:
                if not base.resolve().is_relative_to(root):
                    continue
                # Retain descendants for explicit directory observations so
                # runfiles preserve existence and recursive traversal shape.
                for candidate in base.rglob("*"):
                    add(candidate, explicit=True)
            except (OSError, ValueError, RuntimeError):
                continue
    return result
