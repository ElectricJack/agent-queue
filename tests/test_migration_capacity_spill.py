"""Capacity-spill schema upgrades and rollback on owned disposable PostgreSQL."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.database.engine import create_postgres_engine
from src.database.tables import task_reroutes
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration
ROOT = Path(__file__).resolve().parent.parent
REVISION = "a00000000037"
PRECEDING_REVISION = previous_revision(REVISION)
CHECK = "ck_task_reroutes_reason_code"
OLD_REASONS = "reason_code IN ('provider_unavailable','operator_forced','operator_undo')"


async def migrate(engine, direction, revision):
    def run(conn):
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = conn
        getattr(command, direction)(config, revision)

    async with engine.begin() as conn:
        await conn.run_sync(run)


def reason_check(conn):
    return next(
        check
        for check in sa.inspect(conn).get_check_constraints("task_reroutes")
        if check["name"] == CHECK
    )


def check_oid(conn):
    return conn.execute(
        sa.text(
            "SELECT oid FROM pg_constraint WHERE conname = :name "
            "AND conrelid = 'task_reroutes'::regclass"
        ),
        {"name": CHECK},
    ).scalar_one()


@pytest.mark.parametrize("existing", ["legacy", "fresh", "column_only", "check_only", "no_check"])
async def test_upgrade_preserves_rows_is_idempotent_and_downgrade_reverses(existing):
    engine = create_postgres_engine(await create_scratch_database("capacityspillmigration"))
    revision = import_module(
        "migrations.versions.a00000000037_capacity_spill_and_preferred_provider"
    )
    try:
        await migrate(engine, "upgrade", PRECEDING_REVISION)

        def prepare(conn):
            # The baseline reads live metadata: reconstruct old/partial installs.
            op = Operations(MigrationContext.configure(conn))
            if existing in {"legacy", "check_only"}:
                op.drop_column("projects", "preferred_provider")
            if existing in {"legacy", "column_only", "no_check"}:
                op.drop_constraint(CHECK, "task_reroutes", type_="check")
                if existing != "no_check":
                    op.create_check_constraint(CHECK, "task_reroutes", OLD_REASONS)
            conn.execute(
                sa.text("INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', 1)")
            )
            conn.execute(
                sa.text(
                    "INSERT INTO tasks (id, project_id, title, description, created_at, updated_at) "
                    "VALUES ('t', 'p', 'T', 'D', 1, 1)"
                )
            )
            conn.execute(
                sa.insert(task_reroutes).values(
                    task_id="t", project_id="p", reason_code="provider_unavailable", at=1.0
                )
            )

        async with engine.begin() as conn:
            await conn.run_sync(prepare)
        await migrate(engine, "upgrade", REVISION)

        def verify_and_repeat(conn):
            column = next(
                c
                for c in sa.inspect(conn).get_columns("projects")
                if c["name"] == "preferred_provider"
            )
            assert isinstance(column["type"], sa.Text)
            assert column["nullable"] and column["default"] is None
            assert "capacity_spill" in reason_check(conn)["sqltext"]
            oid = check_oid(conn)
            conn.execute(sa.text("UPDATE projects SET preferred_provider = 'codex' WHERE id = 'p'"))
            with Operations.context(MigrationContext.configure(conn)):
                revision.upgrade()
                revision.upgrade()
            assert check_oid(conn) == oid  # The existing CHECK is not recreated.
            assert (
                conn.execute(
                    sa.text("SELECT preferred_provider FROM projects WHERE id = 'p'")
                ).scalar_one()
                == "codex"
            )
            assert conn.execute(sa.select(task_reroutes.c.reason_code)).scalars().all() == [
                "provider_unavailable"
            ]
            # Prove the database accepts spill and still rejects unknown reasons.
            savepoint = conn.begin_nested()
            conn.execute(
                sa.insert(task_reroutes).values(
                    task_id="t", project_id="p", reason_code="capacity_spill", at=2.0
                )
            )
            # A rollback cannot silently discard the new audit reason's rows.
            with (
                pytest.raises(sa.exc.IntegrityError),
                conn.begin_nested(),
                Operations.context(MigrationContext.configure(conn)),
            ):
                revision.downgrade()
            assert "capacity_spill" in reason_check(conn)["sqltext"]
            assert (
                conn.execute(
                    sa.text("SELECT preferred_provider FROM projects WHERE id = 'p'")
                ).scalar_one()
                == "codex"
            )
            savepoint.rollback()
            with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
                conn.execute(
                    sa.insert(task_reroutes).values(
                        task_id="t", project_id="p", reason_code="invalid", at=3.0
                    )
                )

        async with engine.begin() as conn:
            await conn.run_sync(verify_and_repeat)
        await migrate(engine, "downgrade", PRECEDING_REVISION)

        def verify_downgrade_and_repeat(conn):
            assert "preferred_provider" not in {
                c["name"] for c in sa.inspect(conn).get_columns("projects")
            }
            assert "capacity_spill" not in reason_check(conn)["sqltext"]
            oid = check_oid(conn)
            with Operations.context(MigrationContext.configure(conn)):
                revision.downgrade()
            assert check_oid(conn) == oid
            assert conn.execute(sa.select(task_reroutes.c.reason_code)).scalars().all() == [
                "provider_unavailable"
            ]

        async with engine.begin() as conn:
            await conn.run_sync(verify_downgrade_and_repeat)
    finally:
        await engine.dispose()
