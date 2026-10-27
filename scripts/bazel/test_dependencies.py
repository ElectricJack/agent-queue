"""Static, per-module pytest dependency closures for the Bazel generator.

Conftests remain intact runfiles. Only their import-time code, hooks and reachable
fixture/helper bodies contribute dependencies. Production modules are deliberately
analysed in full: runtime call-graph guessing must never omit a Python input.
"""
from __future__ import annotations

import ast
import json
import re
import textwrap
from collections import deque
from pathlib import Path

from scripts.bazel.dependency_paths import repository_data, repository_files

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DATA_ROOTS = {"src", "tests", "docs", "scripts", "tools", "dashboard", "packages",
               "migrations", "vault"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name(node.value) + "." + node.attr
    return ""


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _qualified(node: ast.AST, aliases: dict[str, str]) -> str:
    pieces = _name(node).split(".")
    return ".".join([aliases.get(pieces[0], pieces[0]), *pieces[1:]])


def _subprocess_snippet(
    node: ast.Call, aliases: dict[str, str], assignments: dict[str, list[ast.AST]],
) -> tuple[bool, str | None]:
    """Recognize Python -c argv without executing child code or resolving PATH."""
    if _qualified(node.func, aliases) not in {
        "subprocess.run", "subprocess.Popen", "subprocess.call",
        "subprocess.check_call", "subprocess.check_output",
    }:
        return False, None
    argv = node.args[0] if node.args else next(
        (keyword.value for keyword in node.keywords if keyword.arg == "args"), None
    )
    if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
        return False, None
    executable = _literal(argv.elts[0])
    python = _qualified(argv.elts[0], aliases) == "sys.executable" or bool(
        executable and re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable.replace("\\", "/").rsplit("/", 1)[-1]
        )
    )
    if not python:
        return False, None
    for index, argument in enumerate(argv.elts[1:], 1):
        if _literal(argument) != "-c":
            continue
        snippet = argv.elts[index + 1] if index + 1 < len(argv.elts) else None
        seen = set()
        while isinstance(snippet, ast.Name) and snippet.id not in seen:
            seen.add(snippet.id)
            values = assignments.get(snippet.id, [])
            snippet = values[0] if len(values) == 1 else None
        if isinstance(snippet, ast.Call) and _qualified(snippet.func, aliases) == "textwrap.dedent":
            source = _literal(snippet.args[0]) if snippet.args else None
            return True, textwrap.dedent(source) if source is not None else None
        return True, _literal(snippet)
    return False, None


def _composed_path(node: ast.AST) -> str:
    """Join a path expression's literal suffix without widening to each parent."""
    pieces = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if (piece := _literal(node.right)) is None:
            break
        pieces.append(piece)
        node = node.left
    if (isinstance(node, ast.Call) and _name(node.func).split(".")[-1] == "Path"
            and node.args and (piece := _literal(node.args[0])) is not None):
        pieces.append(piece)
    return "/".join(reversed(pieces))


def _data_literals(node: ast.AST):
    # Prefix/suffix predicates and normalization strings do not open files.
    if isinstance(node, ast.Compare):
        return
    if isinstance(node, ast.Call) and _name(node.func).split(".")[-1] in {
        "startswith", "endswith", "removeprefix", "removesuffix",
    }:
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    for child in ast.iter_child_nodes(node):
        yield from _data_literals(child)


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


def _definition_inputs(node):
    inputs = list(node.decorator_list)
    if isinstance(node, _FUNCTIONS):
        inputs.extend(node.args.defaults)
        inputs.extend(x for x in node.args.kw_defaults if x)
    elif isinstance(node, ast.ClassDef):
        inputs.extend(node.bases)
        inputs.extend(keyword.value for keyword in node.keywords)
        for child in node.body:
            if isinstance(child, (*_FUNCTIONS, ast.ClassDef)):
                inputs.extend(_definition_inputs(child))
            else:
                inputs.append(child)
    return inputs


class DependencyAnalyzer:
    """Reusable index; create a fresh instance after changing repository inputs."""

    def __init__(self, root: Path, *, trees: dict[str, ast.Module] | None = None):
        self.root = Path(root).resolve()
        self.repository_files = repository_files(self.root)
        self.files = sorted(
            path for path in self.repository_files
            if path.split("/", 1)[0] in {"src", "tests", "scripts", "tools", "packages", "migrations"}
            and path.endswith(".py")
        )
        self.known = {_module(path): path for path in self.files}
        self.migration_sources = {path for path in self.files if path.startswith("migrations/")}
        for path in self.files:
            if path.startswith("packages/aq-client/agent_queue_api_client/"):
                self.known[_module(path.split("packages/aq-client/", 1)[1])] = path
        self.trees = {} if trees is None else trees
        self.scan_contexts: dict[tuple, tuple] = {}
        self.snippet_contexts: dict[tuple, tuple] = {}
        self.path_contexts: dict = {}
        self.full_scans: dict[str, tuple] = {}
        self.scans: dict[tuple, tuple] = {}
        self.fixture_imports: dict[str, dict] = {}
        self.support_templates: dict[str, tuple[dict, tuple[ast.AST, ...]]] = {}
        self.lazy_scanning: set[str] = set()
        self.data_paths: dict[str, set[str]] = {}
        self.import_paths: dict[str, frozenset[str]] = {}
        self.relative_data_paths: dict[tuple[str, str], frozenset[str]] = {}
        self.parents: dict[str, dict[int, ast.AST]] = {}
        self.subprocess_trees: dict[str, ast.Module] = {}
        declarations = self.root / "scripts/bazel/test_dependency_inputs.json"
        self.declarations = (
            json.loads(declarations.read_text(encoding="utf-8"))
            if "scripts/bazel/test_dependency_inputs.json" in self.repository_files else {}
        )

    def tree(self, path: str) -> ast.Module:
        if path not in self.trees:
            self.trees[path] = ast.parse((self.root / path).read_text(encoding="utf-8"))
        if path not in self.parents:
            self.parents[path] = {
                id(child): parent for parent in ast.walk(self.trees[path])
                for child in ast.iter_child_nodes(parent)
            }
        return self.trees[path]

    def _resolve(self, name: str) -> frozenset[str]:
        if name not in self.import_paths:
            pieces = name.split(".")
            self.import_paths[name] = frozenset(
                self.known[prefix]
                for i in range(1, len(pieces) + 1)
                if (prefix := ".".join(pieces[:i])) in self.known
            )
        return self.import_paths[name]

    def _support_references(self, target: str, seen: set[str] | None = None) -> set[str]:
        """Follow a used test-helper binding through renamed reexports."""
        seen = set() if seen is None else seen
        if target in seen:
            return set()
        seen.add(target)
        module, _, binding = target.rpartition(".")
        path = self.known.get(module, "")
        if not path.startswith("tests/"):
            return set()
        names = {binding}
        package = module.split(".")
        if not path.endswith("/__init__.py"):
            package = package[:-1]
        for node in self.tree(path).body:
            if not isinstance(node, ast.ImportFrom):
                continue
            base = (
                ".".join(package[:len(package) - node.level + 1]) + "."
                if node.level else ""
            ) + (node.module or "")
            for alias in node.names:
                if (alias.asname or alias.name) == binding:
                    names.update(self._support_references(
                        base.rstrip(".") + "." + alias.name, seen,
                    ))
        return names

    def _data_files(self, value: str) -> set[str]:
        """Keep literal files; directory membership requires an explicit reader."""
        if value not in self.data_paths:
            candidate = (self.root / value).resolve()
            found = set()
            if candidate.is_relative_to(self.root):
                relative = candidate.relative_to(self.root)
                if relative.as_posix() in self.repository_files:
                    found.add(relative.as_posix())
            self.data_paths[value] = found
        return self.data_paths[value]

    def _path_data(self, path: str, value: str) -> frozenset[str]:
        key = path, value
        if key not in self.relative_data_paths:
            found = set()
            if value.split("/", 1)[0] in _DATA_ROOTS:
                found.update(self._data_files(value))
            # __file__-relative fixtures may be rooted at any enclosing test package.
            # Keep every existing interpretation rather than guess the parent depth.
            elif value and not Path(value).is_absolute():
                for parent in Path(path).parents:
                    candidate = (self.root / parent / value).resolve()
                    if candidate.is_relative_to(self.root) and candidate != self.root:
                        found.update(self._data_files(candidate.relative_to(self.root).as_posix()))
            self.relative_data_paths[key] = frozenset(found)
        return self.relative_data_paths[key]

    def _scan_context(self, path: str, tree: ast.AST) -> tuple:
        """Index aliases once in ast.walk order; scan batches never mutate this index."""
        key = path, id(tree)
        if key in self.scan_contexts:
            return self.scan_contexts[key]
        package = _module(path).split(".")
        if not path.endswith("/__init__.py"):
            package = package[:-1]
        aliases = {}
        alias_candidates: dict[str, set[str]] = {}
        parents = self.parents[path] if self.trees.get(path) is tree else {
            id(child): parent for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    base = (
                        ".".join(package[:len(package) - node.level + 1]) + "."
                        if node.level else ""
                    ) + (node.module or "")
                    binding = alias.asname or alias.name
                    target = base.rstrip(".") + "." + alias.name
                    aliases[binding] = target
                    alias_candidates.setdefault(binding, set()).add(target)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    binding = alias.asname or alias.name
                    aliases[binding] = alias.name
                    alias_candidates.setdefault(binding, set()).add(alias.name)
        result = parents, aliases, alias_candidates
        self.scan_contexts[key] = result
        return result

    def _snippet_context(self, path: str, tree: ast.AST) -> tuple:
        """Build lexical bindings only for Python snippets that need name resolution."""
        key = path, id(tree)
        if key in self.snippet_contexts:
            return self.snippet_contexts[key]
        scope_nodes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        scopes, by_scope = {}, {}

        def bind(scope, name, value):
            by_scope.setdefault(id(scope), {}).setdefault(name, []).append(value)

        # Breadth-first order matches ast.walk. Carry the nearest enclosing
        # scope instead of walking each node's parent chain. Only calls and
        # scope owners need retained links.
        pending = deque([(tree, None)])
        while pending:
            node, scope = pending.popleft()
            owns_scope = isinstance(node, scope_nodes)
            if owns_scope or isinstance(node, ast.Call):
                scopes[id(node)] = scope
            child_scope = node if owns_scope else scope
            pending.extend((child, child_scope) for child in ast.iter_child_nodes(node))
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            value = node.value if isinstance(target, ast.Name) and not isinstance(
                                node, ast.AugAssign
                            ) else None
                            bind(scope, name.id, value)
            elif isinstance(node, ast.arg):
                # Parameters shadow any enclosing constant with the same name.
                bind(scope, node.arg, None)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension, ast.NamedExpr)):
                for name in ast.walk(node.target):
                    if isinstance(name, ast.Name):
                        bind(scope, name.id, None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bind(scope, node.name, None)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bind(scope, alias.asname or alias.name.split(".")[0], None)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        for name in ast.walk(item.optional_vars):
                            if isinstance(name, ast.Name):
                                bind(scope, name.id, None)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(scope, node.name, None)
        by_scope = {
            owner: {name: tuple(values) for name, values in bindings.items()}
            for owner, bindings in by_scope.items()
        }
        result = scopes, by_scope
        self.snippet_contexts[key] = result
        return result

    def _scan(self, path: str, nodes: list[ast.AST], aliases_from: ast.AST | None = None) -> tuple:
        # Every output is a union over independent top nodes. Bindings come from
        # the complete fixed context, including assignments outside this batch.
        # Lazy-package recursion guards are part of the effective scan context.
        if not path.startswith("tests/"):
            # Production modules are consumed in full once. Batch their path
            # discovery, retaining node-level reuse for overlapping test helpers.
            key = path, tuple(id(node) for node in nodes), id(aliases_from), frozenset(
                self.lazy_scanning
            )
            if key not in self.scans:
                result = self._scan_nodes(path, nodes, aliases_from)
                self.scans[key] = (*map(frozenset, result[:5]), result[5])
            return self.scans[key]
        outputs = [set() for _ in range(5)]
        docs = False
        for top in nodes:
            key = path, id(top), id(aliases_from), frozenset(self.lazy_scanning)
            if key not in self.scans:
                result = self._scan_nodes(path, [top], aliases_from)
                self.scans[key] = (*map(frozenset, result[:5]), result[5])
            result = self.scans[key]
            for output, values in zip(outputs, result[:5]):
                output.update(values)
            docs |= result[5]
        return *outputs, docs

    def _scan_nodes(
        self, path: str, nodes: list[ast.AST], aliases_from: ast.AST | None = None,
    ) -> tuple:
        imports, names, fixture_names, reasons, data = set(), set(), set(), set(), set()
        docs = False
        package = _module(path).split(".")
        if not path.endswith("/__init__.py"):
            package = package[:-1]
        tree = aliases_from if aliases_from is not None else self.tree(path)
        parents, aliases, alias_candidates = self._scan_context(path, tree)
        context_tree = tree

        def snippet_assignments(call):
            scopes, bindings = self._snippet_context(path, context_tree)
            result = {}
            scope = scopes.get(id(call))
            while scope is not None:
                for name, values in bindings.get(id(scope), {}).items():
                    result.setdefault(name, values)
                scope = scopes.get(id(scope))
            return result
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
                    module = aliases.get(node.id)
                    for candidate in alias_candidates.get(node.id, ()):
                        names.update(self._support_references(candidate))
                    parent_node = parents.get(id(node))
                    if (module in self.known and self.known[module].startswith("tests/")
                            and not isinstance(parent_node, ast.Attribute)
                            and not (isinstance(parent_node, ast.Call)
                                     and _name(parent_node.func) == "getattr")):
                        reasons.add(f"{path}: unresolved dynamic import in escaped test helper module")
                if isinstance(node, ast.Attribute):
                    pieces = _name(node).split(".")
                    target = ".".join([aliases.get(pieces[0], pieces[0]), *pieces[1:]])
                    targets.append(target)
                    for candidate in alias_candidates.get(pieces[0], {pieces[0]}):
                        qualified = ".".join([candidate, *pieces[1:]])
                        targets.append(qualified)
                        names.update(self._support_references(qualified))
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = (
                        ".".join(package[:len(package) - node.level + 1]) + "."
                        if node.level else ""
                    ) + (node.module or "")
                    base = base.rstrip(".")
                    targets = [base] + [base + "." + alias.name for alias in node.names]
                    if (any(alias.name == "*" for alias in node.names)
                            and self.known.get(base, "").startswith("tests/")):
                        reasons.add(f"{path}: unresolved dynamic import in wildcard test helpers")
                elif isinstance(node, ast.Call):
                    function = _name(node.func)
                    function = aliases.get(function, function)
                    leaf = function.split(".")[-1]
                    if path.startswith("tests/") and leaf in {"globals", "locals", "eval", "exec"}:
                        reasons.add(f"{path}: unresolved dynamic import in test helper lookup")
                    if _qualified(node.func, aliases).startswith("alembic.command."):
                        # Treat migration Python as executable inputs, so imports
                        # in env.py and revision bodies join the closure too.
                        # Production bodies are intentionally overapproximated:
                        # importing an engine can declare this even if a pure
                        # fixture override prevents migrations in that test.
                        imports.update(self.migration_sources)
                    python_child, snippet = _subprocess_snippet(node, aliases, {})
                    if python_child and snippet is None:
                        python_child, snippet = _subprocess_snippet(
                            node, aliases, snippet_assignments(node),
                        )
                    if python_child:
                        if snippet is None:
                            reasons.add(f"{path}: unresolved dynamic import in subprocess Python execution")
                        else:
                            try:
                                if snippet not in self.subprocess_trees:
                                    self.subprocess_trees[snippet] = ast.parse(snippet)
                                tree = self.subprocess_trees[snippet]
                            except SyntaxError:
                                reasons.add(f"{path}: unresolved dynamic import in subprocess Python execution")
                            else:
                                extra = self._scan(path, tree.body, aliases_from=tree)
                                imports.update(extra[0])
                                names.update(extra[1])
                                fixture_names.update(extra[2])
                                reasons.update(extra[3])
                                data.update(extra[4])
                                docs |= extra[5]
                    if leaf == "getattr" and node.args:
                        module = aliases.get(_name(node.args[0]), _name(node.args[0]))
                        attribute = _literal(node.args[1]) if len(node.args) > 1 else None
                        receiver = node.args[0]
                        if (isinstance(receiver, ast.Subscript)
                                and _qualified(receiver.value, aliases) == "sys.modules"):
                            if isinstance(receiver.slice, ast.Name) and receiver.slice.id == "__name__":
                                module = _module(path)
                            elif (literal := _literal(receiver.slice)) in self.known:
                                module = literal
                        if module in self.known:
                            targets.append(module + "." + (attribute or "*"))
                            if self.known[module].startswith("tests/"):
                                if attribute is not None:
                                    names.update(self._support_references(module + "." + attribute))
                                else:
                                    reasons.add(f"{path}: unresolved dynamic import in test helper lookup")
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
                        if literal and literal in self.repository_files:
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
                # Literal repository paths are data, even when they are subsequently
                # read by a helper. Docstrings and bare messages do not count.
                if isinstance(node, (ast.BinOp, ast.Call, ast.Assign, ast.AnnAssign)):
                    for value in _data_literals(node):
                        if value == "docs" or value.startswith("docs/"):
                            docs = True
                        if "/" in value and value.split("/", 1)[0] in _DATA_ROOTS | {"fixtures"}:
                            data.update(self._path_data(path, value))
                if isinstance(node, (ast.BinOp, ast.Call)):
                    parent = parents.get(id(node))
                    if not (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div)
                            and _composed_path(parent).split("/", 1)[0] in _DATA_ROOTS | {"fixtures"}):
                        value = _composed_path(node)
                        data.update(self._path_data(path, value))
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
        data.update(repository_data(
            self.root, path, nodes,
            context=aliases_from if aliases_from is not None else self.tree(path),
            source_files=self.repository_files, context_cache=self.path_contexts,
        ))
        return imports, names, fixture_names, reasons, data, docs

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

    def _support_template(self, path: str) -> tuple[dict, tuple[ast.AST, ...]]:
        """Cache fixed definitions/startup syntax; discovery copies mutable queues."""
        if path not in self.support_templates:
            definitions, startup = {}, []
            for node in self.tree(path).body:
                if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
                    definitions[node.name] = node
                    startup.extend(_definition_inputs(node))
                    if node.name.startswith("pytest_"):
                        startup.append(node)
                else:
                    startup.append(node)
            self.support_templates[path] = definitions, tuple(startup)
        return self.support_templates[path]

    def discover(self, test_path: str | Path) -> dict:
        test_path = Path(test_path)
        if test_path.is_absolute():
            test_path = test_path.relative_to(self.root)
        path = test_path.as_posix()
        if path not in self.repository_files:
            raise ValueError(f"test module is not a repository source input: {path}")
        reached, reasons, data = set(), set(), set()
        docs = False
        # Audited inputs which Python syntax alone cannot express, such as code
        # executed in a subprocess and repository directory-membership checks.
        # These declarations only add dependencies; they never replace discovery.
        declared = self.declarations.get(path, {})
        extra_imports = set()
        for name in declared.get("imports", []):
            if name not in self.known:
                raise ValueError(f"{path}: unknown declared import {name}")
            extra_imports.update(self._resolve(name))
        for pattern in declared.get("data", []):
            matches = {
                p.relative_to(self.root).as_posix()
                for p in self.root.glob(pattern)
                if p.is_file() and p.name not in {"BUILD", "BUILD.bazel"}
                and p.relative_to(self.root).as_posix() in self.repository_files
            }
            if not matches:
                raise ValueError(f"{path}: declared data matches no files: {pattern}")
            data.update(matches)
        scopes = []
        for directory in reversed(test_path.parent.parents):
            candidate = (directory / "conftest.py").as_posix()
            if candidate in self.repository_files:
                scopes.append(candidate)
        candidate = (test_path.parent / "conftest.py").as_posix()
        if candidate in self.repository_files:
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
        requested, referenced, references_seen = set(), set(), set()
        pending_reference_batches = []
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
        definition_owners = {}
        for scope, defs in definitions.items():
            for name, node in defs.items():
                definition_owners.setdefault(name, []).append((scope, node))
        requested.update(name for name, (_, _, autouse) in fixtures.items() if autouse)
        pending = list(dict.fromkeys(scopes + sorted(self._resolve(_module(path)) | extra_imports)))

        def register_support(scope):
            # Test support follows the same named-helper mechanism as
            # conftests. Importing lease_dsn must not execute every unrelated
            # helper body; decorators, defaults, class initialization and hooks run.
            defs, startup = self._support_template(scope)
            definitions[scope] = defs
            special[scope] = list(startup)
            for name, node in definitions[scope].items():
                definition_owners.setdefault(name, []).append((scope, node))
            # Only new support definitions need the complete history. Current
            # owners are activated immediately below after every scan. Defer the
            # large history unions until this reader actually needs them.
            for names in pending_reference_batches:
                references_seen.update(names)
            pending_reference_batches.clear()
            referenced.update(references_seen & definitions[scope].keys())

        while pending or requested or referenced:
            while requested:
                name = requested.pop()
                fixture = fixtures.get(name)
                if fixture and (key := (fixture[0], fixture[1].name)) not in active:
                    scope, node, _ = fixture
                    active.add(key)
                    requested.update(arg.arg for arg in node.args.args + node.args.kwonlyargs)
                    if scope not in special:
                        register_support(scope)
                    special[scope].append(node)
                    pending.append(scope)
            while referenced:
                name = referenced.pop()
                for scope, node in definition_owners.get(name, ()):
                    if (scope, name) not in active:
                        active.add((scope, name))
                        special.setdefault(scope, []).append(node)
                        pending.append(scope)
            if not pending:
                continue
            current = pending.pop()
            if current.startswith("tests/") and current not in special:
                register_support(current)
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
            pending_reference_batches.append(names)
            referenced.update(names & definition_owners.keys())
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
