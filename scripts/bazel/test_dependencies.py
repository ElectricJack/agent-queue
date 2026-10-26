"""Static, per-module pytest dependency closures for the Bazel generator.

Conftests remain intact runfiles. Only their import-time code, hooks and reachable
fixture/helper bodies contribute dependencies. Production modules are deliberately
analysed in full: runtime call-graph guessing must never omit a Python input.
"""
from __future__ import annotations

import ast
from pathlib import Path

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SKIP = {"__pycache__", ".git", ".aq", "node_modules", ".venv"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name(node.value) + "." + node.attr
    return ""


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _fixture(node: ast.AST) -> tuple[str, bool] | None:
    if not isinstance(node, _FUNCTIONS):
        return None
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        if _name(call.func if call else decorator).split(".")[-1] not in {
            "fixture", "yield_fixture",
        }:
            continue
        keywords = {kw.arg: kw.value for kw in call.keywords} if call else {}
        alias = _literal(keywords.get("name")) or node.name
        autouse = keywords.get("autouse")
        return alias, isinstance(autouse, ast.Constant) and autouse.value is True
    return None


def _module(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def _walk(node: ast.AST):
    """TYPE_CHECKING branches cannot execute in the test interpreter."""
    yield node
    if isinstance(node, ast.If) and _name(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
        children = node.orelse
    else:
        children = ast.iter_child_nodes(node)
    for child in children:
        yield from _walk(child)


def _data_literals(nodes: list[ast.AST]) -> set[str]:
    """Find literals below resource expressions without rewalking every ancestor.

    Once an expression owns a subtree, retain all its literals, matching ast.walk.
    Outside those expressions, TYPE_CHECKING branches follow _walk's exclusions.
    """
    values = set()
    pending = [(node, False) for node in nodes]
    while pending:
        node, covered = pending.pop()
        covered |= isinstance(node, (ast.BinOp, ast.Call, ast.Assign, ast.AnnAssign))
        if covered and isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.add(node.value)
        if (not covered and isinstance(node, ast.If)
                and _name(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}):
            children = node.orelse
        else:
            children = ast.iter_child_nodes(node)
        pending.extend((child, covered) for child in children)
    return values


class DependencyAnalyzer:
    """Reusable index; create a fresh instance after changing repository inputs."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.files = sorted(
            p.relative_to(self.root).as_posix()
            for folder in ("src", "tests", "scripts", "tools", "packages", "migrations")
            for p in (self.root / folder).rglob("*.py")
            if not any(part in _SKIP for part in p.relative_to(self.root).parts)
        )
        self.known = {_module(path): path for path in self.files}
        for path in self.files:
            if path.startswith("packages/aq-client/agent_queue_api_client/"):
                self.known[_module(path.split("packages/aq-client/", 1)[1])] = path
        self.trees: dict[str, ast.Module] = {}
        self.full_scans: dict[str, tuple] = {}
        self.scans: dict[tuple, tuple] = {}
        self.fixture_imports: dict[str, dict] = {}
        self.lazy_scanning: set[str] = set()

    def tree(self, path: str) -> ast.Module:
        if path not in self.trees:
            self.trees[path] = ast.parse((self.root / path).read_text(encoding="utf-8"))
        return self.trees[path]

    def _resolve(self, name: str) -> set[str]:
        pieces = name.split(".")
        return {
            self.known[prefix]
            for i in range(1, len(pieces) + 1)
            if (prefix := ".".join(pieces[:i])) in self.known
        }

    def _scan(self, path: str, nodes: list[ast.AST]) -> tuple:
        key = (path, tuple(id(node) for node in nodes))
        if key in self.scans:
            return self.scans[key]
        imports, names, fixture_names, reasons, data = set(), set(), set(), set(), set()
        docs = False
        package = _module(path).split(".")
        if not path.endswith("/__init__.py"):
            package = package[:-1]
        aliases = {}
        for node in ast.walk(self.tree(path)):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    base = (
                        ".".join(package[:len(package) - node.level + 1]) + "."
                        if node.level else ""
                    ) + (node.module or "")
                    aliases[alias.asname or alias.name] = base.rstrip(".") + "." + alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
        for top in nodes:
            if (isinstance(top, _FUNCTIONS) and top.name == "__getattr__"
                    and path.endswith("/__init__.py")):
                continue
            for node in _walk(top):
                targets = []
                if _fixture(node):
                    # Classes are retained in full, so their fixture bodies form a
                    # safe union across class scopes. Follow every body's fixture
                    # parameters too, including imported aliases and autouse fixtures.
                    fixture_names.update(arg.arg for arg in node.args.args + node.args.kwonlyargs)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    names.add(node.id)
                if isinstance(node, ast.Attribute):
                    pieces = _name(node).split(".")
                    targets.append(".".join([aliases.get(pieces[0], pieces[0]), *pieces[1:]]))
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = (
                        ".".join(package[:len(package) - node.level + 1]) + "."
                        if node.level else ""
                    ) + (node.module or "")
                    base = base.rstrip(".")
                    targets = [base] + [base + "." + alias.name for alias in node.names]
                    # Explicitly imported conftest helpers execute when callers use them.
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Call):
                    function = _name(node.func)
                    function = aliases.get(function, function)
                    leaf = function.split(".")[-1]
                    if leaf == "getattr" and node.args:
                        module = aliases.get(_name(node.args[0]), _name(node.args[0]))
                        if module in self.known:
                            attribute = _literal(node.args[1]) if len(node.args) > 1 else None
                            targets.append(module + "." + (attribute or "*"))
                    if leaf in {"import_module", "__import__"}:
                        argument = node.args[0] if node.args else None
                        literal = _literal(argument)
                        if literal is not None:
                            if literal.startswith("."):
                                pkg = _literal(node.args[1]) if len(node.args) > 1 else None
                                pkg = pkg or next((
                                    _literal(k.value) for k in node.keywords if k.arg == "package"
                                ), None)
                                if pkg:
                                    levels = len(literal) - len(literal.lstrip("."))
                                    parts = pkg.split(".")
                                    literal = ".".join(parts[:len(parts) - levels + 1]) + literal[levels - 1:]
                                else:
                                    reasons.add(f"{path}: unresolved relative dynamic import")
                            targets.extend([literal, literal + ".*"])
                        elif isinstance(argument, ast.Name) and argument.id == "__name__":
                            targets.append(_module(path))
                        elif isinstance(argument, ast.JoinedStr) and argument.values and (
                            prefix := _literal(argument.values[0])
                        ) and "." in prefix:
                            prefix = prefix.rsplit(".", 1)[0]
                            targets.extend(name for name in self.known if name.startswith(prefix + "."))
                        else:
                            reasons.add(f"{path}: unresolved dynamic import")
                    elif leaf == "spec_from_file_location":
                        argument = node.args[1] if len(node.args) > 1 else None
                        literal = _literal(argument)
                        if literal and (self.root / literal).is_file():
                            imports.add(literal)
                        else:
                            reasons.add(f"{path}: unresolved file import")
                    elif leaf == "parametrize":
                        keywords = {kw.arg: kw.value for kw in node.keywords}
                        indirect = keywords.get("indirect")
                        if isinstance(indirect, ast.Constant) and indirect.value is False:
                            indirect = None
                        if indirect is not None:
                            if isinstance(indirect, ast.Constant) and indirect.value is True:
                                argnames = node.args[0] if node.args else keywords.get("argnames")
                                if (literal := _literal(argnames)) is not None:
                                    fixture_names.update(name.strip() for name in literal.split(","))
                                    arguments = []
                                elif isinstance(argnames, (ast.List, ast.Tuple)):
                                    arguments = argnames.elts
                                else:
                                    arguments = [argnames]
                            elif isinstance(indirect, (ast.List, ast.Tuple)):
                                arguments = indirect.elts
                            else:
                                arguments = [indirect]
                            for argument in arguments:
                                if (literal := _literal(argument)) is not None:
                                    fixture_names.add(literal)
                                else:
                                    reasons.add(f"{path}: unresolved fixture request in parametrize")
                    elif leaf in {"usefixtures", "getfixturevalue"}:
                        for argument in node.args:
                            if (literal := _literal(argument)) is not None:
                                fixture_names.add(literal)
                            else:
                                reasons.add(f"{path}: unresolved fixture request")
                for target in targets:
                    imports.update(self._resolve(target))
                    parent = self.known.get(target.rpartition(".")[0])
                    if target not in self.known and parent and parent.endswith("/__init__.py"):
                        lazy = next((n for n in self.tree(parent).body
                                     if isinstance(n, _FUNCTIONS) and n.name == "__getattr__"), None)
                        if lazy is not None and parent not in self.lazy_scanning:
                            self.lazy_scanning.add(parent)
                            extra = self._scan(parent, lazy.body)
                            self.lazy_scanning.remove(parent)
                            imports.update(extra[0])
                            names.update(extra[1])
                            fixture_names.update(extra[2])
                            reasons.update(extra[3])
                            data.update(extra[4])
                            docs |= extra[5]
        # Literal repository paths are data even when a helper reads them later.
        # Inspect each expression subtree once; docstrings and bare messages stay out.
        for value in _data_literals([
            node for node in nodes
            if not (isinstance(node, _FUNCTIONS) and node.name == "__getattr__"
                    and path.endswith("/__init__.py"))
        ]):
            if value == "docs" or value.startswith("docs/"):
                docs = True
            if value.startswith(("src/", "tests/", "docs/", "scripts/", "tools/")):
                candidate = self.root / value
                if candidate.is_file():
                    data.add(value)
                elif candidate.is_dir():
                    data.update(p.relative_to(self.root).as_posix()
                                for p in candidate.rglob("*") if p.is_file()
                                and p.name not in {"BUILD", "BUILD.bazel"}
                                and not any(part in _SKIP for part in p.parts))
        self.scans[key] = imports, names, fixture_names, reasons, data, docs
        return self.scans[key]

    def _imported_fixtures(self, path: str, seen: set[str] | None = None) -> dict:
        """Pytest exposes imported fixture functions under their local binding."""
        cacheable = seen is None
        if cacheable and path in self.fixture_imports:
            return self.fixture_imports[path]
        seen = set() if seen is None else seen
        if path in seen:
            return {}
        seen.add(path)
        found = {}
        package = _module(path).split(".")[:-1]
        for node in self.tree(path).body:
            if isinstance(node, ast.ImportFrom):
                base = (
                    ".".join(package[:len(package) - node.level + 1]) + "."
                    if node.level else ""
                ) + (node.module or "")
                imported_path = self.known.get(base.rstrip("."))
                if not imported_path or not imported_path.startswith("tests/"):
                    continue
                available = self._imported_fixtures(imported_path, seen.copy())
                for alias in node.names:
                    if alias.name in available:
                        found[alias.asname or alias.name] = available[alias.name]
            fixture = _fixture(node)
            if fixture:
                found[node.name] = (path, node, fixture[1])
        if cacheable:
            self.fixture_imports[path] = found
        return found

    def discover(self, test_path: str | Path) -> dict:
        test_path = Path(test_path)
        if test_path.is_absolute():
            test_path = test_path.relative_to(self.root)
        path = test_path.as_posix()
        reached, reasons, data = set(), set(), set()
        docs = False
        scopes = []
        for directory in reversed(test_path.parent.parents):
            candidate = (directory / "conftest.py").as_posix()
            if (self.root / candidate).is_file():
                scopes.append(candidate)
        candidate = (test_path.parent / "conftest.py").as_posix()
        if (self.root / candidate).is_file():
            scopes.append(candidate)
        scopes.append(path)
        plugins = []
        for scope in scopes:
            for node in self.tree(scope).body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "pytest_plugins"
                    for target in node.targets
                ):
                    values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                    for value in values:
                        name = _literal(value)
                        if name is None:
                            reasons.add(f"{scope}: unresolved dynamic import in pytest_plugins")
                        elif name in self.known:
                            plugins.append(self.known[name])
        scopes = list(dict.fromkeys(plugins + scopes))
        fixtures, definitions, special, active = {}, {}, {}, set()
        requested, referenced = set(), set()
        for scope in scopes:
            tree = self.tree(scope)
            for binding, definition in self._imported_fixtures(scope).items():
                fixture_node = definition[1]
                explicit_name = next((
                    _literal(keyword.value)
                    for decorator in fixture_node.decorator_list
                    if isinstance(decorator, ast.Call)
                    for keyword in decorator.keywords if keyword.arg == "name"
                ), None)
                fixtures[explicit_name or binding] = definition
            definitions[scope] = {}
            startup = []
            for node in tree.body:
                fixture = _fixture(node)
                if fixture:
                    name, autouse = fixture
                    fixtures[name] = (scope, node, autouse)
                elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
                    definitions[scope][node.name] = node
                    if scope == path or node.name.startswith("pytest_"):
                        startup.append(node)
                else:
                    startup.append(node)
                if isinstance(node, _FUNCTIONS):
                    # Defaults and decorators run even for fixtures never requested.
                    startup.extend(node.decorator_list)
                    startup.extend(node.args.defaults)
                    startup.extend(x for x in node.args.kw_defaults if x)
            special[scope] = startup
            if scope == path:
                for node in ast.walk(tree):
                    if isinstance(node, _FUNCTIONS) and node.name.startswith("test"):
                        requested.update(arg.arg for arg in node.args.args + node.args.kwonlyargs)
        requested.update(name for name, (_, _, autouse) in fixtures.items() if autouse)
        pending = list(dict.fromkeys(scopes + sorted(self._resolve(_module(path)))))
        while pending or requested or referenced:
            while requested:
                name = requested.pop()
                fixture = fixtures.get(name)
                if fixture and (key := (fixture[0], fixture[1].name)) not in active:
                    scope, node, _ = fixture
                    active.add(key)
                    requested.update(arg.arg for arg in node.args.args + node.args.kwonlyargs)
                    if scope not in special:
                        special[scope] = [n for n in self.tree(scope).body if not _fixture(n)]
                    special[scope].append(node)
                    pending.append(scope)
            while referenced:
                name = referenced.pop()
                for scope, defs in definitions.items():
                    if name in defs and (scope, name) not in active:
                        active.add((scope, name))
                        special.setdefault(scope, []).append(defs[name])
                        pending.append(scope)
            if not pending:
                continue
            current = pending.pop()
            if current.startswith("tests/") and current not in special:
                tree = self.tree(current)
                if any(_fixture(node) for node in tree.body):
                    special[current] = [node for node in tree.body if not _fixture(node)]
                    for node in tree.body:
                        if _fixture(node):
                            special[current].extend(node.decorator_list)
                            special[current].extend(node.args.defaults)
                            special[current].extend(x for x in node.args.kw_defaults if x)
            if current in special:
                nodes = special[current]
                special[current] = []
                if not nodes:
                    reached.add(current)
                    continue
                scan = self._scan(current, nodes)
            else:
                if current in reached:
                    continue
                if current not in self.full_scans:
                    self.full_scans[current] = self._scan(current, self.tree(current).body)
                scan = self.full_scans[current]
            reached.add(current)
            imports, names, fixture_names, unknown, resources, reads_docs = scan
            pending.extend(imports - reached)
            referenced.update(names)
            requested.update(fixture_names)
            reasons.update(unknown)
            data.update(resources)
            docs |= reads_docs
            if any("unresolved fixture request" in reason for reason in unknown):
                requested.update(fixtures)
        if any("dynamic import" in reason or "file import" in reason for reason in reasons):
            # Unknown Python execution can reach any repository module. Preserve a
            # visible reason rather than silently claiming a narrow complete graph.
            reached.update(self.files)
            docs = True
        return {
            "source_files": sorted(p for p in reached if p.startswith("src/")),
            "test_sources": sorted(p for p in reached if p.startswith("tests/")),
            "other_sources": sorted(p for p in reached if not p.startswith(("src/", "tests/"))),
            "data_files": sorted(data),
            "docs": docs,
            "conservative": bool(reasons),
            "conservative_reasons": sorted(reasons),
        }


def discover(root: Path, test_path: str | Path) -> dict:
    """Discover one module; reuse DependencyAnalyzer for an entire inventory."""
    return DependencyAnalyzer(root).discover(test_path)
