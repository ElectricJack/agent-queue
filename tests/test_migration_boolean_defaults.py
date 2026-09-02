# tests/test_migration_boolean_defaults.py
"""No boolean column may take an integer literal as its server default.

SQLite stores booleans as integers and accepts ``BOOLEAN DEFAULT 0``
without complaint; PostgreSQL — the production backend — rejects it:

    DatatypeMismatchError: column "..." is of type boolean but default
    expression is of type integer

So the mistake is invisible on a dev machine and fatal in production.
:mod:`tests.test_migration_postgres_upgrade_head` catches it by actually
running the chain, but only where a Postgres server is available; this
check is a pure source scan, so it runs in every suite on every box and
names the offending file and line directly.

Use ``sa.false()`` / ``sa.true()`` (or ``sa.text("false")``) instead.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Files whose ``Column(...)`` declarations reach real DDL.
SOURCES = sorted((ROOT / "migrations" / "versions").glob("*.py")) + [
    ROOT / "src" / "database" / "tables.py"
]

_BOOLEAN_NAMES = {"Boolean", "BOOLEAN"}
_INTEGERISH_LITERALS = {"0", "1"}


def _name_of(node: ast.AST) -> str | None:
    """Trailing identifier of a Name/Attribute/Call, e.g. ``sa.Boolean()`` -> Boolean."""
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_boolean_column(call: ast.Call) -> bool:
    args = list(call.args) + [kw.value for kw in call.keywords if kw.arg == "type_"]
    return any(_name_of(arg) in _BOOLEAN_NAMES for arg in args)


def _integer_literal_default(node: ast.AST) -> str | None:
    """Return the offending literal when ``node`` is an integer-ish default."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return None  # a real Python bool renders per-dialect correctly
        if isinstance(node.value, int):
            return repr(node.value)
        if isinstance(node.value, str) and node.value.strip() in _INTEGERISH_LITERALS:
            return repr(node.value)
        return None
    # sa.text("0") / text('1')
    if isinstance(node, ast.Call) and _name_of(node) == "text" and node.args:
        inner = node.args[0]
        if isinstance(inner, ast.Constant) and str(inner.value).strip() in _INTEGERISH_LITERALS:
            return f"text({inner.value!r})"
    return None


def test_no_boolean_column_uses_an_integer_server_default():
    offenders: list[str] = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or _name_of(call) != "Column":
                continue
            if not _is_boolean_column(call):
                continue
            for kw in call.keywords:
                if kw.arg != "server_default":
                    continue
                literal = _integer_literal_default(kw.value)
                if literal:
                    rel = path.relative_to(ROOT)
                    offenders.append(f"{rel}:{kw.value.lineno}: server_default={literal}")

    assert not offenders, (
        "Boolean columns with an integer server default — PostgreSQL rejects these "
        "with DatatypeMismatchError. Use sa.false()/sa.true():\n  "
        + "\n  ".join(offenders)
    )
