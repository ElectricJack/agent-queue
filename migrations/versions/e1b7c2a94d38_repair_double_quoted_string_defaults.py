"""repair double-quoted string server defaults on the playbook v2 tables

Revision ID: e1b7c2a94d38
Revises: a3f1c0de0001
Create Date: 2026-09-02

``src/database/tables.py`` and revision ``a3f1c0de0001`` originally declared
string defaults as ``server_default="'system'"``.  SQLAlchemy renders a plain
Python string as a SQL literal, so the source's own quotes were escaped and
the emitted DDL was::

    scope TEXT DEFAULT '''system''' NOT NULL

i.e. the stored default is the 8-character string ``'system'`` — quotes
included — not ``system``.  Any insert that omits the column either violates
its CHECK constraint (``ck_playbook_artifacts_scope``,
``ck_playbook_activations_health``) or silently stores quote characters.

Both source declarations are now corrected, so a database built from scratch
after this lands is already right.  This revision repairs the databases built
from the original form: it strips quote characters from any row that took a
malformed default, then rewrites the column defaults.

Scope is deliberately narrow.  ``tables.py`` used the quoted idiom in 59
places, but nothing calls ``metadata.create_all`` — every database is built
by alembic, and the baseline revision ``311e98c39ffa`` already used the
correct unquoted form.  Only the eight columns created by ``a3f1c0de0001``
ever reached real DDL.
"""

import sqlalchemy as sa
from alembic import op

revision = "e1b7c2a94d38"
down_revision = "a3f1c0de0001"
branch_labels = None
depends_on = None


# table -> column -> intended default value (the *unquoted* string)
TARGETS: dict[str, dict[str, str]] = {
    "playbook_artifacts": {
        "scope": "system",
        "scope_identifier": "",
        "profile_fingerprint": "",
        "validation": "{}",
    },
    "playbook_activations": {
        "scope": "system",
        "scope_identifier": "",
        "health": "disabled",
        "reasons": "[]",
    },
}


def _quote(value: str) -> str:
    """Render ``value`` as a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _is_malformed(reflected_default: str | None, value: str) -> bool:
    """True when ``reflected_default`` is the doubly-quoted form of ``value``.

    SQLite reflects the raw DDL text (``'''system'''``); PostgreSQL reflects a
    cast expression (``'''system'''::text``).  Comparing against the exact
    doubly-quoted literal keeps this a no-op on a database that is already
    correct, and on one whose default was changed to something else entirely.
    """
    if reflected_default is None:
        return False
    text = reflected_default.strip()
    if "::" in text:  # strip PostgreSQL's ``::text`` cast suffix
        text = text.split("::", 1)[0].strip()
    return text == _quote(_quote(value))


def _repair_rows(bind) -> None:
    """Strip the quote characters from rows that took a malformed default."""
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in TARGETS.items():
        if table not in tables:
            continue
        for column, value in columns.items():
            bind.execute(
                sa.text(
                    # Identifiers come from TARGETS, never from user input.
                    f"UPDATE {table} SET {column} = :good "
                    f"WHERE {column} = :bad"
                ),
                {"good": value, "bad": _quote(value)},
            )


def upgrade() -> None:
    bind = op.get_bind()
    _repair_rows(bind)

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    broken: dict[str, dict[str, str]] = {}
    for table, columns in TARGETS.items():
        if table not in tables:
            continue
        reflected = {c["name"]: c.get("default") for c in inspector.get_columns(table)}
        bad = {
            column: value
            for column, value in columns.items()
            if column in reflected and _is_malformed(reflected[column], value)
        }
        if bad:
            broken[table] = bad
    if not broken:
        return

    if bind.dialect.name == "sqlite":
        _fix_defaults_sqlite(bind, broken)
    else:
        for table, columns in broken.items():
            for column, value in columns.items():
                op.alter_column(
                    table,
                    column,
                    existing_type=sa.Text(),
                    existing_nullable=False,
                    server_default=value,
                )


def _fix_defaults_sqlite(bind, broken: dict[str, dict[str, str]]) -> None:
    """Rebuild the affected tables under SQLite, where DEFAULT cannot be altered.

    ``batch_alter_table`` recreates the table (create temp, copy, drop, rename).
    The daemon opens SQLite with ``PRAGMA foreign_keys=ON``, so dropping
    ``playbook_artifacts`` fails while ``playbook_activations`` rows point at
    it.  Detaching those references for the duration of the rebuild — the
    column is nullable — lets the drop through; the values are restored once
    the parent table is back.
    """
    saved: list[tuple[str, str]] = []
    detach = "playbook_artifacts" in broken
    if detach:
        saved = [
            (row[0], row[1])
            for row in bind.execute(
                sa.text(
                    "SELECT activation_id, active_artifact_sha256 FROM playbook_activations "
                    "WHERE active_artifact_sha256 IS NOT NULL"
                )
            ).fetchall()
        ]
        bind.execute(sa.text("UPDATE playbook_activations SET active_artifact_sha256 = NULL"))

    for table, columns in broken.items():
        with op.batch_alter_table(table) as batch_op:
            for column, value in columns.items():
                batch_op.alter_column(
                    column,
                    existing_type=sa.Text(),
                    existing_nullable=False,
                    server_default=value,
                )

    for activation_id, sha256 in saved:
        bind.execute(
            sa.text(
                "UPDATE playbook_activations SET active_artifact_sha256 = :sha "
                "WHERE activation_id = :aid"
            ),
            {"sha": sha256, "aid": activation_id},
        )


def downgrade() -> None:
    """No-op.

    The previous state was malformed DDL that broke CHECK constraints; there
    is nothing worth restoring, and reinstating it would leave a database that
    ``a3f1c0de0001`` no longer produces either.
    """
