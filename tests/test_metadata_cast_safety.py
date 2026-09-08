"""Ratchet: no raw SQL cast of free-form metadata text to a numeric type.

``task_metadata.value`` holds whatever ``json.dumps`` produced for whatever a
caller passed, so the same key can legitimately hold ``0`` (the number) on one
row and ``"0"`` (the string, quotes included) on the next.  PostgreSQL reads
the first and raises ``invalid input syntax for type double precision`` on the
second -- and because these casts live inside correlated ``EXISTS`` clauses
over every candidate task, the error takes down the *entire statement*, not
the offending row.

On 2026-09-07 exactly that happened: two rows holding ``"0"`` under
``claim_prepare_backoff_until`` made every ``select_ready_for_profile`` call in
one project raise for roughly 18 hours.  No pool worker could claim anything,
and since the symptom was a database error rather than an empty result set,
every operator surface reported an idle queue rather than a broken one.

The fix is :func:`src.database.queries.claim_queries.numeric_meta_value`, which
puts a regex guard *inside* the cast via ``CASE`` -- placement that matters,
because PostgreSQL does not guarantee evaluation order between a guard and a
cast sitting side by side in the same ``AND``.

This module is the ratchet that keeps the raw form from coming back, in the
spirit of ``test_v1_removal.py`` and ``test_sqlite_removal.py``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

#: Columns whose contents are free-form JSON text written by ``json.dumps``.
#: A numeric cast over any of these is unsafe unless it is guarded.
FREE_FORM_TEXT_COLUMNS = {"task_metadata", "agent_metadata", "session_metadata"}

#: SQLAlchemy type names that raise on unparseable text rather than returning
#: NULL.  ``String``/``Text`` casts are always safe and are not listed.
NUMERIC_SQL_TYPES = {"Float", "Integer", "BigInteger", "Numeric", "DateTime", "Boolean"}


def _python_files():
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _is_free_form_value(node: ast.AST) -> bool:
    """True for ``<table>.c.value`` where ``<table>`` holds JSON text."""
    if not isinstance(node, ast.Attribute) or node.attr != "value":
        return False
    inner = node.value
    if not isinstance(inner, ast.Attribute) or inner.attr != "c":
        return False
    table = inner.value
    return isinstance(table, ast.Name) and table.id in FREE_FORM_TEXT_COLUMNS


def _unguarded_casts(path: pathlib.Path):
    """``(lineno, table)`` for each ``cast(<table>.c.value, <numeric>)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "cast" or len(node.args) != 2:
            continue
        target, sql_type = node.args
        type_name = (
            sql_type.id
            if isinstance(sql_type, ast.Name)
            else getattr(sql_type, "attr", None)
        )
        if type_name not in NUMERIC_SQL_TYPES:
            continue
        if _is_free_form_value(target):
            found.append((node.lineno, ast.unparse(target)))
    return found


def test_no_unguarded_numeric_cast_of_free_form_metadata():
    """Cast a metadata value through ``numeric_meta_value``, never directly.

    A hit here is not a style violation: it is a query that one malformed row
    -- written by any caller, in any release, possibly years ago -- can make
    raise for an entire project.
    """
    offenders = []
    for path in _python_files():
        for lineno, expr in _unguarded_casts(path):
            offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}: cast({expr}, ...)")
    assert not offenders, (
        "Unguarded numeric cast of free-form metadata text:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse src.database.queries.claim_queries.numeric_meta_value(column), "
        "which guards the cast with a regex inside a CASE so a malformed row "
        "reads as the default instead of raising for the whole statement."
    )


@pytest.fixture
async def db():
    from src.database import Database
    from tests.db_fixtures import lease_dsn

    database = Database(lease_dsn("test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0", 0.0),
        ("-1", -1.0),
        ("1788823522.8", 1788823522.8),
        ('"0"', 0.0),  # the JSON string that caused the 2026-09-07 outage
        ('""', 0.0),
        ("null", 0.0),
        ("not-a-number", 0.0),
        ("1e9", 0.0),  # exponent form is deliberately out of scope
    ],
)
async def test_numeric_meta_value_reads_every_shape_without_raising(db, text, expected):
    """The guard's contract, exercised against a real PostgreSQL backend.

    Evaluated over a literal rather than a stored row: what is under test is
    the SQL expression's behaviour in PostgreSQL, and going through
    ``task_metadata`` would drag in its ``tasks`` foreign key without testing
    anything more.

    Parsing rules are asserted here rather than left implicit:
    ``_NUMERIC_TEXT`` is deliberately narrower than what ``float8`` accepts,
    so ``1e9`` reads as the default.  That is fine -- every writer of these
    keys stores a plain timestamp -- but it is a decision, so it is pinned.
    """
    from sqlalchemy import literal, select

    from src.database.queries.claim_queries import numeric_meta_value

    async with db.immediate() as conn:
        got = await conn.scalar(select(numeric_meta_value(literal(text))))
    assert got == pytest.approx(expected)
