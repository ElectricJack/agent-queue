"""GitHub check-run identifiers exceed PostgreSQL's signed 32-bit range."""

import pytest
from sqlalchemy import BigInteger, insert, inspect, select, update
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

from src.database.tables import integration_attestation_publications
from tests.test_integration_attestation import SHA
from tests.test_integration_attestation import attestation_db as attestation_db  # noqa: PLC0414

GITHUB_CHECK_RUN_ID = 101845954535


def test_publication_update_binds_check_run_id_as_bigint():
    statement = update(integration_attestation_publications).values(
        check_run_id=GITHUB_CHECK_RUN_ID
    )
    compiled = statement.compile(dialect=PGDialect_asyncpg())
    assert "::BIGINT" in str(compiled)
    assert compiled.params["check_run_id"] == GITHUB_CHECK_RUN_ID


@pytest.mark.parametrize("check_run_id", [12, GITHUB_CHECK_RUN_ID])
async def test_baseline_persists_small_and_large_check_run_ids(attestation_db, check_run_id):
    async with attestation_db._engine.begin() as conn:
        columns = await conn.run_sync(
            lambda sync: inspect(sync).get_columns("integration_attestation_publications")
        )
        assert isinstance(
            next(column["type"] for column in columns if column["name"] == "check_run_id"),
            BigInteger,
        )
        await conn.execute(
            insert(integration_attestation_publications).values(
                id="publication",
                project_id="p",
                batch_id="batch",
                revision=0,
                operation_id="root-op",
                head_sha=SHA,
                ci_evidence_id="ci-aggregate",
                external_id="external",
                execution_nonce="nonce",
                state="reserved",
                expires_at=100,
                created_at=1,
                updated_at=1,
            )
        )
        assert (
            await conn.execute(select(integration_attestation_publications.c.check_run_id))
        ).scalar_one() is None
        await conn.execute(
            update(integration_attestation_publications)
            .where(integration_attestation_publications.c.id == "publication")
            .values(state="published", prewrite_at=2, check_run_id=check_run_id, updated_at=2)
        )
    # Read through another connection after commit, not merely the bound parameter.
    async with attestation_db._engine.connect() as conn:
        assert (
            await conn.execute(select(integration_attestation_publications.c.check_run_id))
        ).scalar_one() == check_run_id
