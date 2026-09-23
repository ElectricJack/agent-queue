"""Keep production GitHub commands on the startup-owned GhRunner."""

from __future__ import annotations

import ast
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
PROCESS_CALLS = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "Popen",
    "run",
    "call",
    "check_call",
    "check_output",
    "system",
    "_arun_subprocess",
}
SHARED_EXECUTABLE_DEFAULTS = {
    ("git/github.py", "from_config"),
    ("git/github_cli.py", "__init__"),
    ("projects/github.py", "__init__"),
}


def _starts_with_gh(node: ast.expr, assignments: dict[str, list[ast.expr]]) -> bool:
    if isinstance(node, ast.Name):
        return any(_starts_with_gh(assigned, {}) for assigned in assignments.get(node.id, ()))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "gh" or node.value.startswith("gh ")
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _starts_with_gh(node.elts[0], assignments)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _starts_with_gh(node.left, assignments)
    return False


def test_no_production_subprocess_launches_gh_outside_shared_runner():
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        relative_path = path.relative_to(SOURCE_ROOT).as_posix()
        assignments: dict[str, list[ast.expr]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = zip(node.args.args[-len(node.args.defaults) :], node.args.defaults)
                keyword_defaults = zip(node.args.kwonlyargs, node.args.kw_defaults)
                if any(
                    argument.arg == "executable"
                    and isinstance(default, ast.Constant)
                    and default.value == "gh"
                    for argument, default in (*defaults, *keyword_defaults)
                ) and (relative_path, node.name) not in SHARED_EXECUTABLE_DEFAULTS:
                    offenders.append(f"{relative_path}:{node.lineno}")
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else ""
            )
            if name in PROCESS_CALLS and _starts_with_gh(node.args[0], assignments):
                offenders.append(f"{relative_path}:{node.lineno}")
    assert not offenders, "direct gh launch outside GhRunner: " + ", ".join(offenders)
