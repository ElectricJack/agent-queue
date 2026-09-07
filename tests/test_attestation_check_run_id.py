"""GitHub check-run identifiers exceed PostgreSQL's signed 32-bit range."""
from sqlalchemy import update
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

from src.database.tables import integration_attestation_publications


from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()
GITHUB_CHECK_RUN_ID = 101845954535


def test_publication_update_binds_check_run_id_as_bigint():
    statement = update(integration_attestation_publications).values(
        check_run_id=GITHUB_CHECK_RUN_ID
    )
    compiled = statement.compile(dialect=PGDialect_asyncpg())
    assert "::BIGINT" in str(compiled)
    assert compiled.params["check_run_id"] == GITHUB_CHECK_RUN_ID


def _assert_migration_round_trip(connection):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import BigInteger, Column, Integer, MetaData, Table, insert, select

    migration = importlib.import_module(
        "migrations.versions.9d3895228e7b_widen_attestation_check_run_ids_to_"
    )
    legacy = Table(
        "integration_attestation_publications", MetaData(),
        Column("id", Integer, primary_key=True),
        Column("check_run_id", Integer, nullable=True),
    )
    legacy.create(connection)
    connection.execute(insert(legacy).values(id=1, check_run_id=12))
    connection.execute(insert(legacy).values(id=2, check_run_id=None))
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    widened = Table(legacy.name, MetaData(), autoload_with=connection)
    expected_type = Integer if connection.dialect.name == "sqlite" else BigInteger
    assert isinstance(widened.c.check_run_id.type, expected_type)
    assert connection.execute(select(widened.c.check_run_id).order_by(widened.c.id)).all() == [
        (12,), (None,),
    ]
    connection.execute(update(widened).where(widened.c.id == 1).values(
        check_run_id=GITHUB_CHECK_RUN_ID
    ))
    assert connection.execute(select(widened.c.check_run_id).where(widened.c.id == 1)).scalar_one() == GITHUB_CHECK_RUN_ID
    connection.execute(update(widened).where(widened.c.id == 1).values(check_run_id=12))
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        migration.upgrade()
    assert connection.execute(select(widened.c.check_run_id).order_by(widened.c.id)).all() == [
        (12,), (None,),
    ]


def test_sqlite_migration_preserves_and_widens_ids(tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'check-run.db'}")
    try:
        with engine.begin() as connection:
            _assert_migration_round_trip(connection)
    finally:
        engine.dispose()


async def test_postgres_migration_preserves_and_widens_ids():
    import pytest
    from tests.pg_dsn import create_scratch_database
    from src.database.engine import create_postgres_engine

    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    dsn = await create_scratch_database("check_run_bigint")
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_assert_migration_round_trip)
    finally:
        await engine.dispose()
