"""Conservative, non-executing checks for release migration compatibility.

Only upgrade bodies and their local helpers are inspected. SQL and imported
helpers cannot be proved additive by this checker and require a breaking release.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Revision:
    path: str
    source: str
    revision: str
    parents: tuple[str, ...]
    dependencies: tuple[str, ...] = ()


def parse_revision(path: str, source: str) -> Revision:
    values = {}
    for node in ast.parse(source, filename=path).body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        for name in names:
            if name in {"revision", "down_revision", "depends_on"}:
                values[name] = ast.literal_eval(node.value)
    revision = values.get("revision")
    if not isinstance(revision, str) or not revision or "down_revision" not in values:
        raise ValueError(f"{path}: revision/down_revision must be literal declarations")
    links = {}
    for key in ("down_revision", "depends_on"):
        value = values.get(key)
        sequence = () if value is None else (value,) if isinstance(value, str) else value
        if not isinstance(sequence, (tuple, list)) or any(
            not isinstance(parent, str) or not parent for parent in sequence
        ):
            raise ValueError(f"{path}: {key} must be a literal revision sequence")
        links[key] = tuple(dict.fromkeys(sequence))
    return Revision(path, source, revision, links["down_revision"], links["depends_on"])


class MigrationTree:
    """A validated revision graph; never import a migration to inspect it."""

    def __init__(self, sources: dict[str, str]):
        self.revisions: dict[str, Revision] = {}
        for path, source in sorted(sources.items()):
            if Path(path).name == "__init__.py":
                continue
            revision = parse_revision(path, source)
            if revision.revision in self.revisions:
                other = self.revisions[revision.revision].path
                raise ValueError(f"{path}: duplicate revision {revision.revision} (also {other})")
            self.revisions[revision.revision] = revision
        parents = {parent for revision in self.revisions.values() for parent in revision.parents}
        dependencies = {
            dependency
            for revision in self.revisions.values()
            for dependency in revision.dependencies
        }
        missing = (parents | dependencies) - self.revisions.keys()
        if missing:
            raise ValueError(f"missing parent revision(s): {sorted(missing)}")
        for revision in self.revisions:
            self.ancestors(revision)
        self.heads = tuple(sorted(self.revisions.keys() - parents))
        if len(self.heads) != 1:
            raise ValueError(
                f"expected one Alembic head; multiple or missing heads: {list(self.heads)}. "
                "Resolve duplicate ids or join sibling heads with an Alembic merge revision."
            )

    @classmethod
    def from_directory(cls, path: Path) -> MigrationTree:
        sources = {str(file): file.read_text(encoding="utf-8") for file in path.glob("*.py")}
        return cls(sources)

    def ancestors(self, revision: str) -> set[str]:
        visited: set[str] = set()
        active: set[str] = set()

        def visit(current: str) -> None:
            if current in active:
                raise ValueError(f"migration graph cycle at {current}")
            if current in visited:
                return
            if current not in self.revisions:
                raise ValueError(f"unknown revision {current}")
            active.add(current)
            node = self.revisions[current]
            for parent in (*node.parents, *node.dependencies):
                visit(parent)
            active.remove(current)
            visited.add(current)

        visit(revision)
        return visited

    def extra_revisions(self, previous: str, current: str) -> set[str]:
        newer = self.ancestors(current)
        if previous not in newer:
            raise ValueError(
                f"database revision {current} does not descend from code head {previous}"
            )
        return newer - self.ancestors(previous)


_ADDITIVE_OPS = {
    "create_table",
    "create_index",
    "create_foreign_key",
    "create_unique_constraint",
    "create_check_constraint",
    "create_primary_key",
    "add_column",
    "get_bind",
}
_PURE_CALLS = {"len", "set", "list", "tuple", "dict", "sorted", "isinstance", "any", "all"}
_DECLARATIVE_CALLS = {
    "Column",
    "Table",
    "MetaData",
    "Index",
    "ForeignKey",
    "ForeignKeyConstraint",
    "CheckConstraint",
    "UniqueConstraint",
    "PrimaryKeyConstraint",
    "Identity",
    "Computed",
    "Integer",
    "BigInteger",
    "SmallInteger",
    "String",
    "Text",
    "Unicode",
    "UnicodeText",
    "Boolean",
    "Float",
    "Double",
    "Numeric",
    "DECIMAL",
    "Date",
    "DateTime",
    "Time",
    "Interval",
    "LargeBinary",
    "JSON",
    "JSONB",
    "ARRAY",
    "UUID",
    "Uuid",
    "Enum",
    "VARCHAR",
    "CHAR",
    "TIMESTAMP",
    "TEXT",
    "BOOLEAN",
    "INTEGER",
    "BIGINT",
    "text",
    "literal",
    "literal_column",
    "false",
    "true",
    "inspect",
}
_READ_METHODS = {
    "has_table",
    "get_columns",
    "get_indexes",
    "get_table_names",
    "get_foreign_keys",
    "get_pk_constraint",
    "get_unique_constraints",
    "get_check_constraints",
    "get",
    "keys",
    "values",
    "items",
}


def check_upgrade(revision: Revision) -> list[Finding]:
    tree = ast.parse(revision.source, filename=revision.path)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({name.asname or name.name: name.name for name in node.names})
        elif isinstance(node, ast.ImportFrom):
            aliases.update(
                {name.asname or name.name: f"{node.module}.{name.name}" for name in node.names}
            )
    findings: list[Finding] = []
    visited: set[str] = set()

    def finding(node: ast.AST, message: str) -> None:
        findings.append(Finding(revision.path, node.lineno, message))

    def qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{qualified(node.value)}.{node.attr}"
        return "<dynamic>"

    def inspect_body(nodes: list[ast.AST]) -> None:
        for statement in nodes:
            for node in ast.walk(statement):
                if not isinstance(node, ast.Call):
                    continue
                name = qualified(node.func)
                if isinstance(node.func, ast.Name) and node.func.id in functions:
                    inspect_function(node.func.id)
                    continue
                if name.startswith("alembic.op."):
                    operation = name.removeprefix("alembic.op.")
                    if operation not in _ADDITIVE_OPS:
                        finding(node, f"non-additive or unproven operation: {operation}")
                    elif operation == "add_column":
                        column = (
                            node.args[1]
                            if len(node.args) > 1
                            else next(
                                (kw.value for kw in node.keywords if kw.arg == "column"), None
                            )
                        )
                        if not isinstance(column, ast.Call) or not qualified(column.func).endswith(
                            ".Column"
                        ):
                            finding(node, "add_column requires a literal Column declaration")
                            continue
                        options = {kw.arg: kw.value for kw in column.keywords}
                        nullable = options.get("nullable", ast.Constant(True))
                        primary = options.get("primary_key", ast.Constant(False))
                        default = options.get("server_default", ast.Constant(None))
                        proven_default = (
                            isinstance(default, ast.Constant) and default.value is not None
                        ) or (
                            isinstance(default, ast.Call)
                            and qualified(default.func).startswith("sqlalchemy.")
                        )
                        if (
                            not isinstance(nullable, ast.Constant)
                            or nullable.value is not True
                            or not isinstance(primary, ast.Constant)
                            or primary.value is not False
                        ):
                            if not proven_default:
                                finding(node, "added non-null column needs a server_default")
                        if any(kw.arg is None for kw in column.keywords):
                            finding(node, "dynamic Column options cannot prove additivity")
                elif name.startswith("sqlalchemy.") and (
                    name.split(".")[-1] in _DECLARATIVE_CALLS or name.startswith("sqlalchemy.func.")
                ):
                    continue  # declarative types, expressions and inspection
                elif name in _PURE_CALLS:
                    continue
                elif isinstance(node.func, ast.Attribute) and node.func.attr in _READ_METHODS:
                    continue
                else:
                    finding(node, f"call cannot be proved additive: {name}")

    def inspect_function(name: str) -> None:
        if name not in visited:
            visited.add(name)
            inspect_body(functions[name].body)

    if "upgrade" not in functions:
        findings.append(Finding(revision.path, 1, "missing upgrade()"))
    else:
        inspect_function("upgrade")
    # Defaults and decorators execute on module load, even for downgrade().
    top_level = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top_level.extend(node.decorator_list)
            top_level.extend(node.args.defaults)
            top_level.extend(default for default in node.args.kw_defaults if default is not None)
        elif not isinstance(node, (ast.Import, ast.ImportFrom)):
            top_level.append(node)
    inspect_body(top_level)
    return findings


def check_range(previous: dict[str, str], current: dict[str, str]) -> list[Finding]:
    """Validate the target graph and inspect only revisions added since previous."""
    graph = MigrationTree(current)
    old = MigrationTree(previous) if previous else None
    findings = []
    if old:
        for revision_id, revision in old.revisions.items():
            newer = graph.revisions.get(revision_id)
            if newer is None or newer.source != revision.source or newer.path != revision.path:
                findings.append(Finding(revision.path, 1, "existing migration removed or modified"))
        graph.extra_revisions(old.heads[0], graph.heads[0])
    for revision_id, revision in graph.revisions.items():
        if old is None or revision_id not in old.revisions:
            findings.extend(check_upgrade(revision))
    return findings
