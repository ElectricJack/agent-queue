"""Boolean defaults remain valid in metadata and future migrations."""

import ast
from pathlib import Path


def _numeric_boolean_defaults(path: Path) -> list[str]:
    """Return ``file:line`` for every Boolean Column with a numeric server_default.

    Handles both the migration spelling (``sa.Column(..., sa.Boolean(), ...)``)
    and the ``tables.py`` spelling (``Column(..., Boolean, ...)``).
    """

    def _is_named(node: ast.expr, name: str) -> bool:
        if isinstance(node, ast.Name):
            return node.id == name
        if isinstance(node, ast.Attribute):
            return node.attr == name
        if isinstance(node, ast.Call):
            return _is_named(node.func, name)
        return False

    offenders: list[str] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_named(node.func, "Column")):
            continue
        if not any(_is_named(arg, "Boolean") for arg in node.args):
            continue
        for kw in node.keywords:
            if kw.arg != "server_default":
                continue
            default = kw.value
            literal = None
            if isinstance(default, ast.Constant):
                literal = default.value
            elif (
                isinstance(default, ast.Call)
                and _is_named(default.func, "text")
                and default.args
                and isinstance(default.args[0], ast.Constant)
            ):
                literal = default.args[0].value
            if isinstance(literal, str) and literal.strip().strip("'\"") in {"0", "1"}:
                offenders.append(f"{path.name}:{default.lineno}")
    return offenders


def test_no_migration_gives_a_boolean_column_a_numeric_default():
    """The same mistake anywhere else would break PostgreSQL the same way."""
    offenders: list[str] = []
    for path in sorted(Path("migrations/versions").glob("*.py")):
        offenders.extend(_numeric_boolean_defaults(path))

    assert offenders == [], (
        "Boolean server_default must be sa.true()/sa.false(), not a numeric "
        f"literal (PostgreSQL rejects it): {offenders}"
    )


def test_tables_metadata_has_no_numeric_boolean_defaults():
    """``tables.py`` is what autogenerate diffs against.

    A numeric default there re-introduces the bug into the *next* generated
    migration even after the current one is fixed, so the metadata must carry
    the same ``false()``/``true()`` the migrations emit.
    """
    offenders = _numeric_boolean_defaults(Path("src/database/tables.py"))

    assert offenders == [], (
        "Boolean server_default in tables.py must be false()/true(), not a "
        f"numeric literal: {offenders}"
    )
