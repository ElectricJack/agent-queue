"""Revision a00000000039 (mandatory-routing §11 revision 1) on owned disposable PostgreSQL."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from src.database.engine import create_postgres_engine
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration
ROOT = Path(__file__).resolve().parent.parent
REVISION = "a00000000039"
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
    assert script.get_heads() == [REVISION]


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
