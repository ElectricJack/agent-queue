"""The squashed baseline seeds built-in workspace kinds idempotently."""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select

from src.database import Database
from src.database.tables import workspace_kinds
from tests.db_fixtures import lease_dsn


async def test_baseline_seeds_system_workspace_kinds_idempotently():
    database = Database(lease_dsn("workspace-kinds"))
    await database.initialize()
    try:
        async with database._engine.begin() as conn:
            baseline = (
                ScriptDirectory.from_config(Config("alembic.ini"))
                .get_revision("a00000000001")
                .module
            )
            await conn.run_sync(baseline._seed_system_workspace_kinds)
            rows = (
                (
                    await conn.execute(
                        select(workspace_kinds).where(workspace_kinds.c.project_id == "__system__")
                    )
                )
                .mappings()
                .all()
            )
        assert {row["id"] for row in rows} == {"project-repo", "vault", "readonly-dir"}
        assert len(rows) == 3
        kinds = {row["id"]: row for row in rows}
        assert kinds["project-repo"]["lockable"] is True
        assert kinds["project-repo"]["default_lock_mode"] == "exclusive"
        assert kinds["vault"]["auto_attach"] is True
        assert kinds["vault"]["lockable"] is False
        assert kinds["readonly-dir"]["writable"] is False
    finally:
        await database.close()
