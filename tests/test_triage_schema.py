"""Routing audit and control ownership survive persistence and archival."""

import json
from dataclasses import replace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.database import Database
from src.database.tables import metadata
from src.models import Agent, PlaybookRun, Project, SessionRecord, Task, TaskStatus
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgresql"] if POSTGRES_DSN else ["sqlite"])
async def db(request, tmp_path):
    if request.param == "postgresql":
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "triage.db"))
        await database.initialize()
    try:
        yield database
    finally:
        await database.close()


def decision_values():
    return dict(
        id="decision",
        project_id="p",
        task_id="t",
        routing_revision=1,
        playbook_run_id="run",
        playbook_id="triage",
        playbook_version=1,
        execution_type_key="a" * 64,
        execution_snapshot='{"model":"fixture-model"}',
        profile_id="worker",
        intelligence_class="standard",
        reason="Inspected",
        decided_at=5,
    )


async def test_new_task_defaults_do_not_fabricate_triage(db):
    await db.create_project(Project("p", "P"))
    await db.create_task(Task("t", "p", "Task", "Description"))
    task = await db.get_task("t")
    assert task.routing_revision == 1
    assert task.routing_request == {}
    assert task.routing_decision_id is None and task.control_origin is None


async def test_routing_audit_survives_archival_and_permanent_task_deletion(db):
    await db.create_project(Project("p", "P"))
    task = Task(
        "t",
        "p",
        "Task",
        "Description",
        status=TaskStatus.COMPLETED,
        routing_request={"model": "fixture-model"},
        control_origin={"operation": "compile_playbook", "source": "fixture"},
    )
    await db.create_task(task)
    await db.create_playbook_run(PlaybookRun("run", "triage", 1))
    decisions = metadata.tables["task_routing_decisions"]
    async with db._engine.begin() as conn:
        await conn.execute(sa.insert(decisions).values(**decision_values()))
    await db.update_task("t", routing_decision_id="decision")
    assert await db.archive_task("t")
    archived = await db.get_archived_task("t")
    assert archived["routing_revision"] == 1
    assert archived["routing_request"] == {"model": "fixture-model"}
    assert archived["control_origin"] == task.control_origin
    assert archived["routing_decision_id"] == "decision"
    await db.delete_archived_task("t")
    async with db._engine.begin() as conn:
        row = (await conn.execute(sa.select(decisions))).mappings().one()
    assert (
        row["task_id"] == "t" and json.loads(row["execution_snapshot"])["model"] == "fixture-model"
    )


async def test_one_decision_per_task_revision(db):
    await db.create_project(Project("p", "P"))
    decisions = metadata.tables["task_routing_decisions"]
    async with db._engine.begin() as conn:
        await conn.execute(sa.insert(decisions).values(**decision_values()))
    with pytest.raises(sa.exc.IntegrityError):
        async with db._engine.begin() as conn:
            await conn.execute(
                sa.insert(decisions).values(**(decision_values() | {"id": "duplicate"}))
            )
    async with db._engine.begin() as conn:
        await conn.execute(
            sa.insert(decisions).values(
                **(
                    decision_values()
                    | {
                        "id": "new-revision",
                        "routing_revision": 2,
                    }
                )
            )
        )


async def test_control_ownership_roundtrips_and_only_one_live_triage_run(db):
    await db.create_project(Project("p", "P", triage_playbook_id="custom"))
    await db.create_project(Project("q", "Q"))
    await db.create_agent(Agent("a", "A", "triage", role="triage", last_assigned_at=7))
    run = PlaybookRun("run", "custom", 1, project_id="p", role="triage")
    await db.create_playbook_run(run)
    session = SessionRecord(
        id="s",
        project_id="p",
        profile_id="triage",
        harness="fixture",
        provider="fake",
        name="s",
        lifecycle="playbook",
        work_dir="/tmp",
        epoch="e",
        instance_token="token",
        started_at=1,
        agent_id="a",
        playbook_run_id="run",
        playbook_node_id="inspect",
    )
    await db.create_session(session)
    await db.update_playbook_run("run", owner_session_id="s", status="paused")
    assert (await db.get_session("s")).playbook_node_id == "inspect"
    assert (await db.get_session("s")).playbook_run_id == "run"
    assert (await db.get_playbook_run("run")).owner_session_id == "s"
    assert (await db.get_playbook_run("run")).role == "triage"
    assert (await db.get_project("p")).triage_playbook_id == "custom"
    assert (await db.get_agent("a")).last_assigned_at == 7
    with pytest.raises(sa.exc.IntegrityError):
        await db.create_playbook_run(replace(run, run_id="conflict", playbook_id="different"))
    await db.create_playbook_run(replace(run, run_id="other-project", project_id="q"))
    await db.create_playbook_run(replace(run, run_id="ordinary", role=None))
    await db.update_playbook_run("run", status="completed")
    await db.create_playbook_run(replace(run, run_id="successor"))


def test_playbook_read_model_preserves_control_ownership():
    from src.playbooks.models import PlaybookRun as ReadRun

    payload = dict(
        run_id="r", playbook_id="p", project_id="project", role="triage", owner_session_id="session"
    )
    result = ReadRun.from_dict(payload).to_dict()
    assert {key: result[key] for key in payload} == payload


def migrate(engine, target, *, downgrade=False):
    with engine.connect() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        (command.downgrade if downgrade else command.upgrade)(cfg, target)
        conn.commit()


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_migration_preserves_legacy_rows_and_refuses_audit_loss(tmp_path, foreign_keys):
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_revision(head)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    if foreign_keys:

        @sa.event.listens_for(engine, "connect")
        def enable_fks(conn, _record):
            conn.execute("PRAGMA foreign_keys=ON")

    try:
        migrate(engine, revision.down_revision)
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO projects(id,name,created_at) VALUES ('p','P',1)"))
            conn.execute(
                sa.text(
                    "INSERT INTO tasks(id,project_id,title,description,created_at,updated_at) VALUES ('t','p','T','D',1,1)"
                )
            )
        migrate(engine, "head")
        with engine.begin() as conn:
            row = conn.execute(sa.text("SELECT * FROM tasks WHERE id='t'")).mappings().one()
            assert row["routing_revision"] == 1 and row["routing_decision_id"] is None
            assert row["routing_request"] == "{}" and row["control_origin"] is None
            conn.execute(
                sa.insert(metadata.tables["task_routing_decisions"]).values(**decision_values())
            )
        with pytest.raises(RuntimeError, match="audit|export"):
            migrate(engine, revision.down_revision, downgrade=True)
        with engine.begin() as conn:
            assert (
                conn.execute(sa.text("SELECT count(*) FROM task_routing_decisions")).scalar() == 1
            )
            conn.execute(sa.text("DELETE FROM task_routing_decisions"))
        migrate(engine, revision.down_revision, downgrade=True)
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT title FROM tasks WHERE id='t'")).scalar() == "T"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == int(foreign_keys)
    finally:
        engine.dispose()
