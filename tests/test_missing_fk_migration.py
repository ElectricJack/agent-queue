"""The PostgreSQL baseline must include both circular use_alter foreign keys.

Historical orphan repair belonged to a removed pre-baseline revision. Fresh
installs must have named constraints and enforce their ON DELETE SET NULL policy.
"""

import pytest
import sqlalchemy as sa

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


@pytest.mark.parametrize(
    "constraint,table,column,target",
    [
        ("fk_agents_current_task", "agents", "current_task_id", "tasks"),
        ("fk_tasks_preferred_workspace", "tasks", "preferred_workspace_id", "workspaces"),
    ],
)
async def test_named_foreign_keys_exist_postgres(constraint, table, column, target):
    database = Database(lease_dsn("missing-fks"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            foreign_keys = await conn.run_sync(
                lambda sync: sa.inspect(sync).get_foreign_keys(table)
            )
        fk = next(fk for fk in foreign_keys if fk["name"] == constraint)
        assert fk["constrained_columns"] == [column]
        assert fk["referred_table"] == target
        assert fk["referred_columns"] == ["id"]
        assert fk["options"]["ondelete"] == "SET NULL"
    finally:
        await database.close()
