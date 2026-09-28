"""Task route schema revisions on owned disposable PostgreSQL databases."""

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
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.postgresql import JSONB

from src.database.engine import create_postgres_engine
from src.database.tables import tasks
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration
ROOT = Path(__file__).resolve().parent.parent
REVISION = "a00000000039"
HEAD_REVISION = "a00000000040"
PRECEDING_REVISION = previous_revision(REVISION)
CHECK = "ck_tasks_route_source"
COLUMNS = ("route_source", "class_hint", "route")

# (task id, profile, class) -> (route_source, class_hint) after the upgrade.
ROWS = {
    ("worker-task", "worker", "standard-high"): ("legacy", "standard-high"),
    ("triage-task", "triage", "fast-high"): ("role", "fast-high"),
    ("spec-task", "spec-ingest", None): ("role", None),
    ("review-task", "reviewer", "deep-high"): ("role", "deep-high"),
    ("final-task", "final-reviewer", None): ("role", None),
    ("unrouted-task", None, "deep-high"): ("unrouted", "deep-high"),
    ("bare-task", None, None): ("unrouted", None),
}


async def migrate(engine, direction, revision):
    def run(conn):
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = conn
        getattr(command, direction)(config, revision)

    async with engine.begin() as conn:
        await conn.run_sync(run)


def task_columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("tasks")}


def check_oid(conn):
    return conn.execute(
        sa.text(
            "SELECT oid FROM pg_constraint WHERE conname = :name "
            "AND conrelid = 'tasks'::regclass"
        ),
        {"name": CHECK},
    ).scalar_one_or_none()


def routes(conn) -> dict[str, tuple[str, str | None]]:
    rows = conn.execute(sa.text("SELECT id, route_source, class_hint FROM tasks")).all()
    return {row.id: (row.route_source, row.class_hint) for row in rows}


def bindings(conn) -> dict[str, str | None]:
    rows = conn.execute(sa.text("SELECT id, assignment_playbook_id FROM projects")).all()
    return dict(rows)


def test_single_head():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == [HEAD_REVISION]


def test_route_metadata_uses_jsonb():
    assert isinstance(tasks.c.route.type, JSONB)
    assert tasks.c.route.type.none_as_null


@pytest.mark.parametrize("existing_type", ["json", "jsonb"])
async def test_route_jsonb_upgrade_preserves_records_and_is_idempotent(existing_type):
    engine = create_postgres_engine(await create_scratch_database("routejsonbmigration"))
    revision = import_module("migrations.versions.a00000000040_task_route_jsonb")
    payload = {"rule": "balanced", "candidates": ["worker-a", "worker-b"]}
    column_type = sa.text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'tasks' AND column_name = 'route'"
    )
    relfilenode = sa.text("SELECT pg_relation_filenode('tasks'::regclass)")
    try:
        await migrate(engine, "upgrade", REVISION)
        async with engine.begin() as conn:
            if existing_type == "json":
                await conn.execute(sa.text(
                    "ALTER TABLE tasks ALTER COLUMN route TYPE json USING route::json"
                ))
            await conn.execute(sa.text(
                "INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', 1)"
            ))
            await conn.execute(sa.text(
                "INSERT INTO tasks (id, project_id, title, description, created_at, "
                "updated_at) VALUES ('t', 'p', 'T', 'D', 1, 1), "
                "('empty', 'p', 'Empty', 'D', 1, 1)"
            ))
            await conn.execute(
                sa.text("UPDATE tasks SET route = CAST(:payload AS json) WHERE id = 't'"),
                {"payload": json.dumps(payload)},
            )
            assert await conn.scalar(column_type) == existing_type
            before = await conn.scalar(relfilenode)

        await migrate(engine, "upgrade", HEAD_REVISION)
        async with engine.begin() as conn:
            assert await conn.scalar(column_type) == "jsonb"
            if existing_type == "jsonb":
                assert await conn.scalar(relfilenode) == before
            records = (await conn.execute(sa.text(
                "SELECT id, route::text FROM tasks ORDER BY id"
            ))).all()
            assert [
                (task_id, json.loads(route) if route else None) for task_id, route in records
            ] == [("empty", None), ("t", payload)]
            # The original failure was SELECT DISTINCT over a json route.
            distinct = await conn.execute(sa.text("SELECT DISTINCT route FROM tasks"))
            assert len(distinct.all()) == 2
            converted = await conn.scalar(relfilenode)

            def repeat_upgrade(sync_conn):
                with Operations.context(MigrationContext.configure(sync_conn)):
                    revision.upgrade()

            await conn.run_sync(repeat_upgrade)
            assert await conn.scalar(relfilenode) == converted

        await migrate(engine, "downgrade", REVISION)
        async with engine.begin() as conn:
            assert await conn.scalar(column_type) == "json"

            def repeat_downgrade(sync_conn):
                with Operations.context(MigrationContext.configure(sync_conn)):
                    revision.downgrade()

            await conn.run_sync(repeat_downgrade)
            assert await conn.scalar(column_type) == "json"
        await migrate(engine, "upgrade", HEAD_REVISION)
        async with engine.begin() as conn:
            assert await conn.scalar(column_type) == "jsonb"
            assert json.loads(await conn.scalar(sa.text(
                "SELECT route::text FROM tasks WHERE id = 't'"
            ))) == payload
    finally:
        await engine.dispose()


@pytest.mark.parametrize("existing", ["legacy", "fresh"])
async def test_upgrade_backfills_is_idempotent_and_downgrade_reverses(existing):
    engine = create_postgres_engine(await create_scratch_database("routesourcemigration"))
    revision = import_module("migrations.versions.a00000000039_task_route_source")
    try:
        await migrate(engine, "upgrade", PRECEDING_REVISION)

        def prepare(conn):
            # The baseline reads live metadata; "legacy" rebuilds an install
            # from before this revision, "fresh" keeps the baseline's columns.
            if existing == "legacy":
                op = Operations(MigrationContext.configure(conn))
                op.drop_constraint(CHECK, "tasks", type_="check")
                for name in COLUMNS:
                    op.drop_column("tasks", name)
            for profile in ("worker", "triage", "spec-ingest", "reviewer", "final-reviewer"):
                conn.execute(
                    sa.text(
                        "INSERT INTO agent_profiles (id, name, created_at, updated_at) "
                        "VALUES (:id, :id, 1, 1)"
                    ),
                    {"id": profile},
                )
            conn.execute(sa.text(
                "INSERT INTO projects (id, name, created_at, assignment_playbook_id) VALUES "
                "('unbound', 'U', 1, NULL), ('bound', 'B', 1, 'project-router')"
            ))
            for task_id, profile, cls in ROWS:
                conn.execute(
                    sa.text(
                        "INSERT INTO tasks (id, project_id, title, description, profile_id, "
                        "intelligence_class, created_at, updated_at) "
                        "VALUES (:id, 'unbound', 'T', 'D', :profile, :cls, 1, 1)"
                    ),
                    {"id": task_id, "profile": profile, "cls": cls},
                )

        async with engine.begin() as conn:
            await conn.run_sync(prepare)
        await migrate(engine, "upgrade", REVISION)

        expected = {task_id: after for (task_id, _, _), after in ROWS.items()}

        def verify_and_repeat(conn):
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("tasks")}
            assert not columns["route_source"]["nullable"]
            assert "unrouted" in columns["route_source"]["default"]
            assert columns["class_hint"]["nullable"] and columns["route"]["nullable"]
            assert routes(conn) == expected
            assert bindings(conn) == {
                "unbound": "default-assignment-routing",
                "bound": "project-router",
            }
            oid = check_oid(conn)
            assert oid is not None
            # A router route written after the upgrade survives a re-run.
            conn.execute(sa.text(
                "UPDATE tasks SET route_source = 'router', class_hint = 'fast-low' "
                "WHERE id = 'worker-task'"
            ))
            with Operations.context(MigrationContext.configure(conn)):
                revision.upgrade()
                revision.upgrade()
            assert check_oid(conn) == oid  # The existing CHECK is not recreated.
            assert routes(conn) == {**expected, "worker-task": ("router", "fast-low")}
            conn.execute(
                sa.text("INSERT INTO tasks (id, project_id, title, description, created_at, "
                        "updated_at) VALUES ('new', 'bound', 'T', 'D', 2, 2)")
            )
            assert routes(conn)["new"] == ("unrouted", None)
            with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
                conn.execute(sa.text("UPDATE tasks SET route_source = 'explicit'"))

        async with engine.begin() as conn:
            await conn.run_sync(verify_and_repeat)
        await migrate(engine, "downgrade", PRECEDING_REVISION)

        def verify_downgrade_and_repeat(conn):
            assert not set(COLUMNS) & task_columns(conn)
            assert check_oid(conn) is None
            with Operations.context(MigrationContext.configure(conn)):
                revision.downgrade()
            assert not set(COLUMNS) & task_columns(conn)
            # Bindings predate the revision and stay.
            assert bindings(conn)["unbound"] == "default-assignment-routing"

        async with engine.begin() as conn:
            await conn.run_sync(verify_downgrade_and_repeat)
    finally:
        await engine.dispose()
