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


@pytest.mark.parametrize("revision_name,column_name", [
    ("a00000000093_pool_claim_refusal", "claim_refusal"),
    ("a00000000096_failed_preparation_identity", "failed_preparation"),
])
async def test_upgrade_downgrade_preserve_sessions_and_replay(revision_name, column_name):
    revision = importlib.import_module(f"migrations.versions.{revision_name}")
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
            await conn.exec_driver_sql(f"ALTER TABLE sessions DROP COLUMN {column_name}")

            def verify(bind):
                with Operations.context(MigrationContext.configure(bind)):
                    for _ in range(2):
                        revision.upgrade()
                        revision.upgrade()
                        column = next(col for col in sa.inspect(bind).get_columns("sessions")
                                      if col["name"] == column_name)
                        assert column["nullable"]
                        assert isinstance(column["type"], JSONB)
                        assert bind.execute(sa.text(
                            f"SELECT id, instance_token, {column_name} FROM sessions"
                        )).one() == ("worker", "token", None)
                        assert not compare_metadata(MigrationContext.configure(bind), metadata)
                        revision.downgrade()
                        revision.downgrade()
                        assert column_name not in {
                            col["name"] for col in sa.inspect(bind).get_columns("sessions")
                        }
                        assert bind.execute(sa.text(
                            "SELECT id, instance_token FROM sessions"
                        )).one() == ("worker", "token")
                    revision.upgrade()

            await conn.run_sync(verify)
        assert getattr(await db.get_session("worker"), column_name) is None
    finally:
        await db.close()
