"""Revision a00000000040: ``tasks.route`` json -> jsonb, on owned disposable PostgreSQL."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.database.engine import create_postgres_engine
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration
ROOT = Path(__file__).resolve().parent.parent
REVISION = "a00000000040"
PRECEDING_REVISION = previous_revision(REVISION)
ROUTE = {"reason": "router", "candidates": [{"profile_id": "worker", "score": 0.5}]}
# The scheduler's stuck-DEFINED query as it stood during the outage.
WHOLE_ROW_DISTINCT = sa.text(
    "SELECT DISTINCT tasks.* FROM tasks "
    "JOIN task_dependencies ON task_dependencies.task_id = tasks.id "
    "JOIN tasks AS dep ON dep.id = task_dependencies.depends_on_task_id "
    "WHERE tasks.status = 'DEFINED' AND dep.status IN ('BLOCKED', 'FAILED')"
)


async def migrate(engine, direction, revision):
    def run(conn):
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = conn
        getattr(command, direction)(config, revision)

    async with engine.begin() as conn:
        await conn.run_sync(run)


def route_type(conn) -> str:
    return conn.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'tasks' "
            "AND column_name = 'route'"
        )
    ).scalar_one()


def relfilenode(conn) -> int:
    """Changes whenever ALTER ... TYPE rewrites the table."""
    return conn.execute(
        sa.text("SELECT relfilenode FROM pg_class WHERE oid = 'tasks'::regclass")
    ).scalar_one()


def routes(conn) -> dict[str, object]:
    rows = conn.execute(sa.text("SELECT id, route::text AS route FROM tasks")).all()
    return {row.id: None if row.route is None else json.loads(row.route) for row in rows}


@pytest.mark.parametrize("existing", ["json", "jsonb"])
async def test_upgrade_retypes_route_idempotently_and_downgrade_keeps_jsonb(existing):
    engine = create_postgres_engine(await create_scratch_database("routejsonbmigration"))
    revision = import_module("migrations.versions.a00000000040_task_route_jsonb")
    try:
        await migrate(engine, "upgrade", PRECEDING_REVISION)

        def prepare(conn):
            # The baseline reads live metadata (jsonb); "json" rebuilds an
            # install that ran a00000000039 before this fix, "jsonb" is a fresh
            # or hand-patched one.
            if existing == "json":
                conn.execute(sa.text("ALTER TABLE tasks ALTER COLUMN route TYPE json"))
            conn.execute(
                sa.text("INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', 1)")
            )
            for task_id, status, route in (
                ("failed", "FAILED", None),
                ("stuck", "DEFINED", json.dumps(ROUTE)),
                ("unrouted", "DEFINED", None),
            ):
                conn.execute(
                    sa.text(
                        "INSERT INTO tasks (id, project_id, title, description, status, "
                        "route, created_at, updated_at) "
                        "VALUES (:id, 'p', 'T', 'D', :status, CAST(:route AS json), 1, 1)"
                    ),
                    {"id": task_id, "status": status, "route": route},
                )
            for task_id in ("stuck", "unrouted"):
                conn.execute(
                    sa.text(
                        "INSERT INTO task_dependencies (task_id, depends_on_task_id) "
                        "VALUES (:id, 'failed')"
                    ),
                    {"id": task_id},
                )
            assert route_type(conn) == existing
            if existing == "json":
                # The outage, reproduced: plan-time failure on a whole-row DISTINCT.
                with (
                    pytest.raises(sa.exc.ProgrammingError, match="equality operator"),
                    conn.begin_nested(),
                ):
                    conn.execute(WHOLE_ROW_DISTINCT)

        async with engine.begin() as conn:
            await conn.run_sync(prepare)
        await migrate(engine, "upgrade", REVISION)

        expected = {"failed": None, "stuck": ROUTE, "unrouted": None}

        def verify_and_repeat(conn):
            assert route_type(conn) == "jsonb"
            assert routes(conn) == expected
            # SQL NULL stays NULL, not a jsonb 'null'.
            assert conn.execute(
                sa.text("SELECT count(*) FROM tasks WHERE route IS NULL")
            ).scalar_one() == 2
            assert {row.id for row in conn.execute(WHOLE_ROW_DISTINCT)} == {"stuck", "unrouted"}
            node = relfilenode(conn)
            with Operations.context(MigrationContext.configure(conn)):
                revision.upgrade()
                revision.upgrade()
            assert relfilenode(conn) == node  # Already jsonb: no rewrite.
            assert routes(conn) == expected

        async with engine.begin() as conn:
            await conn.run_sync(verify_and_repeat)
        await migrate(engine, "downgrade", PRECEDING_REVISION)

        def verify_downgrade(conn):
            assert route_type(conn) == "jsonb"
            assert routes(conn) == expected

        async with engine.begin() as conn:
            await conn.run_sync(verify_downgrade)
    finally:
        await engine.dispose()
