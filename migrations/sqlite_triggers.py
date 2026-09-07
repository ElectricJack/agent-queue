"""Keep SQLite triggers alive across ``batch_alter_table`` rebuilds.

Alembic's SQLite batch mode copies a table into a fresh one and drops the
original, and SQLite drops the original's triggers with it.  A revision that
rebuilds a table therefore silently retires every guard an earlier revision
attached to it: the invariant stops being enforced at head, and the earlier
revision's ``downgrade`` fails on its by-name ``DROP TRIGGER``.  PostgreSQL
alters in place and keeps its triggers, so nothing here touches it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import sqlalchemy as sa
from alembic import op

_SNAPSHOT = sa.text(
    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
    "AND tbl_name IN :tables ORDER BY rowid"
).bindparams(sa.bindparam("tables", expanding=True))

_NAMES = sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")


@contextlib.contextmanager
def preserve_sqlite_triggers(*tables: str) -> Iterator[None]:
    """Restore *tables*' SQLite triggers after the batch rebuild inside the block.

    The triggers' DDL is read from ``sqlite_master`` on entry and every one
    the rebuild dropped is re-executed verbatim on exit.  A trigger the
    revision means to replace or retire must be dropped *before* entering
    the block: the snapshot restores exactly what existed on entry, and
    SQLite only reports a column a restored trigger no longer has on the next
    ``UPDATE``, not at ``CREATE TRIGGER``.  On PostgreSQL the block is a no-op.
    """
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    saved = bind.execute(_SNAPSHOT, {"tables": list(tables)}).fetchall()
    yield
    present = {name for (name,) in bind.execute(_NAMES)}
    for name, ddl in saved:
        if name not in present:
            # ``exec_driver_sql`` — the stored DDL is not a bind-parameter template.
            bind.exec_driver_sql(ddl)
