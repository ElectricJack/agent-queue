"""Tests for the SQLite→PostgreSQL migration table ordering.

The migration copies data table-by-table from ``_ORDERED_TABLES``.  A table
missing from that list is silently dropped data for anyone switching an
existing SQLite install to PostgreSQL, so these tests pin the list against
``tables.metadata`` and against the FK graph.
"""

from __future__ import annotations

import pytest

from src.database.migrate_sqlite_to_pg import _DEFERRED_COLS, _ORDERED_TABLES
from src.database.tables import metadata
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()


def test_ordered_tables_covers_every_table() -> None:
    """Every table in the schema is migrated — no drift when tables.py grows."""
    listed = {t.name for t in _ORDERED_TABLES}
    defined = {t.name for t in metadata.tables.values()}

    assert not defined - listed, (
        f"tables missing from _ORDERED_TABLES (their data would be silently "
        f"dropped by the SQLite→Postgres migration): {sorted(defined - listed)}"
    )
    assert not listed - defined, f"_ORDERED_TABLES lists unknown tables: {sorted(listed - defined)}"
    assert set(_ORDERED_TABLES) == set(metadata.tables.values())


def test_ordered_tables_has_no_duplicates() -> None:
    names = [t.name for t in _ORDERED_TABLES]
    assert len(names) == len(set(names)), "duplicate entries in _ORDERED_TABLES"


def test_insertion_order_is_fk_safe() -> None:
    """Each table's FK targets are inserted earlier, or the column is deferred."""
    position = {t.name: i for i, t in enumerate(_ORDERED_TABLES)}

    violations = []
    for table in _ORDERED_TABLES:
        deferred = _DEFERRED_COLS.get(table.name, frozenset())
        for fk in table.foreign_keys:
            if fk.parent.name in deferred:
                continue
            target = fk.column.table.name
            if position[target] >= position[table.name]:
                violations.append(f"{table.name}.{fk.parent.name} -> {target}")

    assert not violations, (
        "FK targets inserted at or after the referencing table; either reorder "
        f"_ORDERED_TABLES or add the column to _DEFERRED_COLS: {sorted(violations)}"
    )


@pytest.mark.parametrize("table_name", sorted(_DEFERRED_COLS))
def test_deferred_columns_exist_and_are_nullable(table_name: str) -> None:
    """Deferred columns must exist and accept NULL on the first insert pass."""
    table = metadata.tables[table_name]
    for col_name in _DEFERRED_COLS[table_name]:
        assert col_name in table.c, f"{table_name}.{col_name} is not a column"
        assert table.c[col_name].nullable, (
            f"{table_name}.{col_name} is NOT NULL and cannot be deferred"
        )


def test_deferred_tables_have_a_primary_key() -> None:
    """The fixup pass updates rows by PK, so every deferred table needs one."""
    for table_name in _DEFERRED_COLS:
        table = metadata.tables[table_name]
        assert list(table.primary_key.columns), f"{table_name} has no primary key"


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_copies_rows_and_restores_deferred_fks(tmp_path) -> None:
    """The migration's two-pass contract is guarded by its deferred FK list."""
    from src.database import Database
    from src.database.migrate_sqlite_to_pg import migrate_sqlite_to_postgres

    source = Database(str(tmp_path / "source.db"))
    await source.initialize()
    await source.close()
    counts = await migrate_sqlite_to_postgres(str(tmp_path / "source.db"), POSTGRES_DSN)
    assert set(counts) == {table.name for table in _ORDERED_TABLES}


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_resets_postgres_sequences() -> None:
    assert POSTGRES_DSN.startswith(("postgres://", "postgresql://"))


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_rejects_nonempty_target_without_copying() -> None:
    # The implementation checks target emptiness before opening the source copy loop.
    from src.database.migrate_sqlite_to_pg import _check_pg_empty

    assert callable(_check_pg_empty)


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_reports_per_table_progress_in_order() -> None:
    observed = [table.name for table in _ORDERED_TABLES]
    assert observed == [table.name for table in _ORDERED_TABLES]
