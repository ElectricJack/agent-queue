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


async def _empty_pg_adapter():
    """The worker's Postgres database, truncated to the empty state the
    migration requires of its target.  Caller closes it."""
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

    adapter = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    await adapter.initialize()
    await adapter.reset_for_tests()
    return adapter


async def _seeded_source(tmp_path) -> str:
    """A SQLite source at head with rows across the deferred-FK tables:
    a self-FK parent pointer (tasks) and the agents⇄tasks circular FK."""
    from sqlalchemy import text

    from src.database import Database

    path = str(tmp_path / "source.db")
    source = Database(path)
    await source.initialize()
    async with source._engine.begin() as conn:
        await conn.execute(text("INSERT INTO projects (id, name, created_at) VALUES ('x','x',0)"))
        await conn.execute(
            text("INSERT INTO agent_profiles (id, name, created_at, updated_at) "
                 "VALUES ('worker','Worker',0,0)")
        )
        await conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
                "status, created_at, updated_at) VALUES "
                "('p','x',NULL,'p','p','IN_PROGRESS',0,0), ('c','x','p','c','c','READY',0,0)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, profile_id, current_task_id, created_at) "
                "VALUES ('a','a','worker','p',0)"
            )
        )
        for i in (1, 2, 3):
            await conn.execute(
                text(
                    "INSERT INTO hierarchy_migration_rejects "
                    "(id, run_id, task_id, source, reason, detail, created_at) "
                    f"VALUES ({i}, 'run', 't{i}', 'edge', 'cycle', '', 0)"
                )
            )
    await source.close()
    return path


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_copies_rows_and_restores_deferred_fks(tmp_path) -> None:
    """Rows land, and the NULLed-on-insert deferred columns are restored.

    ``tasks.parent_task_id`` (self-FK) and ``agents.current_task_id``
    (agents⇄tasks circular FK) are inserted as NULL in pass one; pass two
    must put the source values back.
    """
    from sqlalchemy import text

    from src.database.migrate_sqlite_to_pg import migrate_sqlite_to_postgres

    path = await _seeded_source(tmp_path)
    target = await _empty_pg_adapter()
    try:
        counts = await migrate_sqlite_to_postgres(path, POSTGRES_DSN)
        assert set(counts) == {table.name for table in _ORDERED_TABLES}
        assert counts["tasks"] == 2 and counts["agents"] == 1
        async with target._engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT parent_task_id FROM tasks WHERE id='c'"))
            ).scalar() == "p"
            assert (
                await conn.execute(text("SELECT current_task_id FROM agents WHERE id='a'"))
            ).scalar() == "p"
        await target.reset_for_tests()
    finally:
        await target.close()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_resets_postgres_sequences(tmp_path) -> None:
    """After copying explicit integer PKs, a plain insert must not collide.

    The copied rows carry ids 1..3; without ``setval`` the sequence would
    hand out 1 again and the next default-id insert would violate the PK.
    """
    from sqlalchemy import text

    from src.database.migrate_sqlite_to_pg import migrate_sqlite_to_postgres

    path = await _seeded_source(tmp_path)
    target = await _empty_pg_adapter()
    try:
        await migrate_sqlite_to_postgres(path, POSTGRES_DSN)
        async with target._engine.begin() as conn:
            new_id = (
                await conn.execute(
                    text(
                        "INSERT INTO hierarchy_migration_rejects "
                        "(run_id, task_id, source, reason, detail, created_at) "
                        "VALUES ('run', 't4', 'edge', 'cycle', '', 0) RETURNING id"
                    )
                )
            ).scalar()
        assert new_id == 4
        await target.reset_for_tests()
    finally:
        await target.close()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_rejects_nonempty_target_without_copying(
    tmp_path,
) -> None:
    from sqlalchemy import text

    from src.database.migrate_sqlite_to_pg import migrate_sqlite_to_postgres

    path = await _seeded_source(tmp_path)
    target = await _empty_pg_adapter()
    try:
        async with target._engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO projects (id, name, created_at) VALUES ('existing','e',0)")
            )
        with pytest.raises(RuntimeError, match="already contains data"):
            await migrate_sqlite_to_postgres(path, POSTGRES_DSN)
        async with target._engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id FROM projects"))).scalars().all()
            assert rows == ["existing"]  # nothing was copied
            assert (
                await conn.execute(text("SELECT COUNT(*) FROM tasks"))
            ).scalar() == 0
        await target.reset_for_tests()
    finally:
        await target.close()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_reports_per_table_progress_in_order(tmp_path) -> None:
    from src.database.migrate_sqlite_to_pg import migrate_sqlite_to_postgres

    path = await _seeded_source(tmp_path)
    target = await _empty_pg_adapter()
    try:
        calls: list[tuple[str, int]] = []
        counts = await migrate_sqlite_to_postgres(
            path, POSTGRES_DSN, progress_cb=lambda name, n: calls.append((name, n))
        )
        assert [name for name, _ in calls] == [table.name for table in _ORDERED_TABLES]
        assert dict(calls) == counts
        await target.reset_for_tests()
    finally:
        await target.close()
