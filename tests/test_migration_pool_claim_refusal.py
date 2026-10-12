"""Pool claim cooldown migration is additive and safe to replay on PostgreSQL."""

import importlib

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects.postgresql import JSONB

from src.database import Database
from src.database.tables import metadata
from src.models import Project, SessionRecord
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


async def test_upgrade_downgrade_preserve_sessions_and_replay():
    revision = importlib.import_module("migrations.versions.a00000000091_pool_claim_refusal")
    db = Database(lease_dsn("pool_claim_refusal_migration"))
    await db.initialize()
    try:
        await db.create_project(Project(id="p", name="Project"))
        await db.create_session(SessionRecord(
            id="worker", project_id="p", profile_id="worker", harness="codex", provider="fake",
            name="worker", lifecycle="pool", work_dir="/tmp/worker", epoch="epoch",
            instance_token="token", started_at=100,
        ))
        async with db.immediate() as conn:
            await conn.exec_driver_sql("ALTER TABLE sessions DROP COLUMN claim_refusal")

            def verify(bind):
                with Operations.context(MigrationContext.configure(bind)):
                    for _ in range(2):
                        revision.upgrade()
                        revision.upgrade()
                        column = next(col for col in sa.inspect(bind).get_columns("sessions")
                                      if col["name"] == "claim_refusal")
                        assert column["nullable"]
                        assert isinstance(column["type"], JSONB)
                        assert bind.execute(sa.text(
                            "SELECT id, instance_token, claim_refusal FROM sessions"
                        )).one() == ("worker", "token", None)
                        assert not compare_metadata(MigrationContext.configure(bind), metadata)
                        revision.downgrade()
                        revision.downgrade()
                        assert "claim_refusal" not in {
                            col["name"] for col in sa.inspect(bind).get_columns("sessions")
                        }
                        assert bind.execute(sa.text(
                            "SELECT id, instance_token FROM sessions"
                        )).one() == ("worker", "token")
                    revision.upgrade()

            await conn.run_sync(verify)
        assert (await db.get_session("worker")).claim_refusal is None
    finally:
        await db.close()
