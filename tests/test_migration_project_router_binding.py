"""a00000000043: every project bound to a router, ``default_profile_id`` dropped.

Revision 3 of the mandatory-task-routing spec (2026-09-28 §8, §11), on owned
disposable PostgreSQL databases.
"""

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
from src.database.tables import projects
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration
ROOT = Path(__file__).resolve().parent.parent
REVISION = "a00000000043"
PRECEDING_REVISION = previous_revision(REVISION)
ROUTER = "default-assignment-routing"


async def migrate(engine, direction, revision):
    def run(conn):
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = conn
        getattr(command, direction)(config, revision)

    async with engine.begin() as conn:
        await conn.run_sync(run)


def project_columns(conn) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(conn).get_columns("projects")}


def bindings(conn) -> dict[str, str | None]:
    rows = conn.execute(sa.text("SELECT id, assignment_playbook_id FROM projects")).all()
    return dict(rows)


def test_revision_chains_onto_revision_2():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_revision("a00000000044").down_revision == REVISION
    assert PRECEDING_REVISION == "a00000000042"


def test_metadata_binds_every_project_and_has_no_default_profile():
    assert "default_profile_id" not in projects.c
    binding = projects.c.assignment_playbook_id
    assert binding.nullable is False
    assert binding.server_default.arg == ROUTER


@pytest.mark.parametrize("existing", ["legacy", "fresh"])
async def test_upgrade_binds_drops_is_idempotent_and_downgrade_reverses(existing):
    engine = create_postgres_engine(await create_scratch_database("routerbindingmigration"))
    revision = import_module("migrations.versions.a00000000043_project_router_binding")
    try:
        await migrate(engine, "upgrade", PRECEDING_REVISION)

        def prepare(conn):
            # The baseline reads live metadata; "legacy" rebuilds an install
            # from before this revision, "fresh" keeps the baseline's shape.
            if existing == "legacy":
                op = Operations(MigrationContext.configure(conn))
                op.alter_column(
                    "projects", "assignment_playbook_id",
                    existing_type=sa.Text(), nullable=True, server_default=None,
                )
                op.add_column("projects", sa.Column(
                    "default_profile_id", sa.Text(), sa.ForeignKey("agent_profiles.id"),
                    nullable=True,
                ))
                conn.execute(sa.text(
                    "INSERT INTO agent_profiles (id, name, created_at, updated_at) "
                    "VALUES ('worker', 'worker', 1, 1)"
                ))
                conn.execute(sa.text(
                    "INSERT INTO projects (id, name, created_at, assignment_playbook_id, "
                    "default_profile_id) VALUES "
                    "('unbound', 'U', 1, NULL, 'worker'), ('blank', 'B', 1, '', NULL), "
                    "('bound', 'P', 1, 'project-router', 'worker')"
                ))
            else:
                conn.execute(sa.text(
                    "INSERT INTO projects (id, name, created_at) VALUES ('unbound', 'U', 1)"
                ))
                conn.execute(sa.text(
                    "INSERT INTO projects (id, name, created_at, assignment_playbook_id) "
                    "VALUES ('bound', 'P', 1, 'project-router')"
                ))

        async with engine.begin() as conn:
            await conn.run_sync(prepare)
        await migrate(engine, "upgrade", REVISION)

        expected = {"unbound": ROUTER, "bound": "project-router"}
        if existing == "legacy":
            expected["blank"] = ROUTER

        def verify_and_repeat(conn):
            columns = project_columns(conn)
            assert "default_profile_id" not in columns
            assert not columns["assignment_playbook_id"]["nullable"]
            assert ROUTER in columns["assignment_playbook_id"]["default"]
            assert bindings(conn) == expected
            # A re-bound project survives a re-run.
            conn.execute(sa.text(
                "UPDATE projects SET assignment_playbook_id = 'other-router' "
                "WHERE id = 'unbound'"
            ))
            with Operations.context(MigrationContext.configure(conn)):
                revision.upgrade()
                revision.upgrade()
            assert bindings(conn) == {**expected, "unbound": "other-router"}
            conn.execute(sa.text(
                "INSERT INTO projects (id, name, created_at) VALUES ('new', 'N', 2)"
            ))
            assert bindings(conn)["new"] == ROUTER
            with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
                conn.execute(sa.text(
                    "UPDATE projects SET assignment_playbook_id = NULL WHERE id = 'new'"
                ))

        async with engine.begin() as conn:
            await conn.run_sync(verify_and_repeat)
        await migrate(engine, "downgrade", PRECEDING_REVISION)

        def verify_downgrade_and_repeat(conn):
            columns = project_columns(conn)
            assert columns["assignment_playbook_id"]["nullable"]
            assert columns["default_profile_id"]["nullable"]
            with Operations.context(MigrationContext.configure(conn)):
                revision.downgrade()
            assert set(project_columns(conn)) == set(columns)
            # Bindings stay; the dropped defaults do not come back.
            assert bindings(conn)["bound"] == "project-router"
            assert conn.execute(sa.text(
                "SELECT count(*) FROM projects WHERE default_profile_id IS NOT NULL"
            )).scalar_one() == 0

        async with engine.begin() as conn:
            await conn.run_sync(verify_downgrade_and_repeat)
        await migrate(engine, "upgrade", REVISION)
        async with engine.begin() as conn:
            columns = await conn.run_sync(project_columns)
            assert "default_profile_id" not in columns
    finally:
        await engine.dispose()
