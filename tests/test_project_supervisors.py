"""Project supervisors opt in; messages and event notices retain their context."""

from importlib import import_module
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.database.tables import messages, projects
from src.messages.delivery import MessageDeliveryEngine
from src.models import Project, SessionRecord
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("project-supervisors"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    await database.create_project(Project(id="sender", name="Sender"))
    yield database
    await database.close()


def handler(db):
    orch = MagicMock()
    orch.db = db
    orch.bus.emit = AsyncMock()
    return CommandHandler(orch, AppConfig())


@pytest.mark.parametrize("kind,address", [
    ("session", "supervisor-p1"), ("session", "n-supervisor--p1"),
    ("profile", "supervisor"),
])
async def test_default_send_routes_to_global_and_retains_project(db, kind, address):
    command = handler(db)
    sent = await command._cmd_message_send({
        "project_id": "p1", "from_kind": "user", "from_id": "dashboard",
        "to_kind": kind, "to_id": address, "body": "need a decision", "thread_id": "t1",
    })
    msg = await db.get_message(sent["message_id"])
    assert (msg.to_kind, msg.to_id, msg.project_id) == ("session", "supervisor-global", "p1")
    assert msg.thread_id == "t1"
    inbox = await command._cmd_message_inbox({"to_kind": "session", "to_id": "supervisor-global"})
    assert [item["id"] for item in inbox["messages"]] == [msg.id]
    event = command.orchestrator.bus.emit.await_args.args[1]
    assert (event["to_id"], event["project_id"]) == ("supervisor-global", "p1")


async def test_recipient_project_controls_routing_and_preserves_sender_context(db):
    for enabled in (True, False):
        await db.update_project("p1", supervisor_enabled=enabled)
        msg = await db.create_message(
            project_id="sender", from_kind="system", from_id="event", to_kind="session",
            to_id="supervisor-p1", body="report",
        )
        assert msg.to_id == ("supervisor-p1" if enabled else "supervisor-global")
        assert msg.project_id == "sender"


async def test_immutable_supervisor_address_redirects_new_and_queued_messages(db):
    await db.update_project("p1", supervisor_enabled=True)
    row = SessionRecord(
        id="project-supervisor-id", name="n-supervisor--p1", project_id="p1",
        profile_id="supervisor", harness="codex", provider="fake", lifecycle="named",
        work_dir="/tmp", epoch="e", instance_token="token", started_at=1, state="running",
    )
    await db.create_session(row)
    queued = await db.create_message(
        project_id="sender", from_kind="system", from_id="worker", to_kind="session",
        to_id=row.id, body="blocker", thread_id="worker-question", priority=90,
    )
    assert queued.to_id == row.id  # enabled immutable recipients remain pinned
    await db.mark_delivered(queued.id, via="nudge")
    await db.update_project("p1", supervisor_enabled=False)

    recovered = await db.get_message(queued.id)
    assert (recovered.to_kind, recovered.to_id) == ("session", "supervisor-global")
    assert recovered.delivered_at is None
    assert (recovered.project_id, recovered.thread_id, recovered.priority) == (
        "sender", "worker-question", 90,
    )
    assert (recovered.from_kind, recovered.from_id, recovered.body, recovered.id) == (
        queued.from_kind, queued.from_id, queued.body, queued.id,
    )
    # The native guidance command resolves its target to this immutable ID.
    command = handler(db)
    sent = await command._cmd_agent_message({"target": row.id, "body": "follow-up"})
    assert (await db.get_message(sent["message_id"])).to_id == "supervisor-global"
    cross_project = await db.create_message(
        project_id="sender", from_kind="system", from_id="worker", to_kind="session",
        to_id=row.id, body="another blocker",
    )
    assert (cross_project.to_id, cross_project.project_id) == ("supervisor-global", "sender")


@pytest.mark.parametrize("lifecycle,name", [
    ("pool", "p-worker"), ("task", "s-worker"), ("named", "n-other--p1"),
])
async def test_disabled_project_keeps_other_session_recipients(db, lifecycle, name):
    row = SessionRecord(
        id="worker-id", name=name, project_id="p1", profile_id="worker-codex",
        harness="codex", provider="fake", lifecycle=lifecycle,
        work_dir="/tmp", epoch="e", instance_token="token", started_at=1, state="running",
    )
    await db.create_session(row)
    msg = await db.create_message(
        project_id="p1", from_kind="system", from_id="supervisor", to_kind="session",
        to_id=row.id, body="guidance",
    )
    await db.redirect_disabled_supervisor_messages()
    assert (await db.get_message(msg.id)).to_id == row.id


async def test_global_session_id_inbox_recovers_internal_project_notices(db):
    await db.create_session(SessionRecord(
        id="global-session-id", name="n-supervisor--global", project_id=None,
        profile_id="supervisor", harness="codex", provider="fake", lifecycle="named",
        work_dir="/tmp", epoch="e", instance_token="token", started_at=1, state="running",
    ))
    async with db._engine.begin() as conn:
        await conn.execute(sa.insert(messages).values(
            id="internal-backlog", project_id="p1", from_kind="system", from_id="event",
            to_kind="session", to_id="supervisor-p1", body="blocker", created_at=1,
        ))
    inbox = await handler(db)._cmd_message_inbox({
        "to_kind": "session", "to_id": "global-session-id",
    })
    assert [msg["id"] for msg in inbox["messages"]] == ["internal-backlog"]
    assert inbox["messages"][0]["project_id"] == "p1"


async def test_edit_defaults_and_disable_records_stop_intent_and_recovers_unread(db):
    command = handler(db)
    assert (await command._cmd_get_project({"project_id": "p1"}))["supervisor_enabled"] is False
    assert (await command._cmd_list_projects({}))["projects"][0]["supervisor_enabled"] is False
    enabled = await command._cmd_edit_project({"project_id": "p1", "supervisor_enabled": True})
    assert enabled["fields"] == ["supervisor_enabled"]
    assert await db.list_sessions(project_id="p1") == []  # enabling is lazy
    row = SessionRecord(
        id="project-sup", name="n-supervisor--p1", project_id="p1", profile_id="supervisor",
        harness="codex", provider="fake", lifecycle="named", work_dir="/tmp",
        epoch="e", instance_token="token", started_at=1, state="running",
    )
    await db.create_session(row)
    unread = await db.create_message(
        project_id="p1", from_kind="system", from_id="worker", to_kind="session",
        to_id="supervisor-p1", body="blocker", thread_id="worker-question",
    )
    await db.mark_delivered(unread.id, via="nudge")
    await command._cmd_edit_project({"project_id": "p1", "supervisor_enabled": False})
    assert (await db.get_session(row.id)).desired_state == "stopped"
    recovered = await db.get_message(unread.id)
    assert recovered.to_id == "supervisor-global"
    assert recovered.delivered_at is None
    assert recovered.thread_id == "worker-question"
    assert recovered.project_id == "p1"
    bad = await command._cmd_edit_project({"project_id": "p1", "supervisor_enabled": "false"})
    assert bad["success"] is False


@pytest.mark.parametrize("kind,address", [
    ("session", "supervisor-p1"), ("session", "n-supervisor--p1"),
    ("profile", "supervisor"), ("session", "project-supervisor-id"),
])
async def test_internal_event_insert_redirects_before_delivery(db, kind, address):
    if address == "project-supervisor-id":
        await db.create_session(SessionRecord(
            id=address, name="n-supervisor--p1", project_id="p1", profile_id="supervisor",
            harness="codex", provider="fake", lifecycle="named", work_dir="/tmp",
            epoch="e", instance_token="token", started_at=1, state="running",
        ))
    async with db._engine.begin() as conn:
        await conn.execute(sa.insert(messages).values(
            id="internal-notice", project_id="p1", from_kind="system", from_id="event",
            to_kind=kind, to_id=address, body="worker blocker", created_at=1,
        ))
    sessions = MagicMock()
    sessions.activity = AsyncMock(return_value="idle")
    sessions.nudge = AsyncMock(return_value=True)
    sessions.ensure_started = AsyncMock()
    result = await MessageDeliveryEngine(db, sessions, AppConfig()).run_delivery_pass()
    # A terminal nudge notifies; only recipient inbox injection consumes mail.
    assert result["delivered"] == 0
    assert sessions.nudge.await_args.kwargs["target_id"] == "supervisor-global"
    sessions.ensure_started.assert_not_awaited()
    msg = await db.get_message("internal-notice")
    assert (msg.to_kind, msg.to_id, msg.project_id) == ("session", "supervisor-global", "p1")
    assert msg.notification_attempts == 1
    assert msg.last_notified_at is not None
    assert msg.delivered_at is None


async def test_reroute_leaves_read_history_enabled_projects_and_global_addresses(db):
    await db.update_project("p1", supervisor_enabled=True)
    history = await db.create_message(
        project_id="p1", from_kind="user", from_id="dashboard", to_kind="session",
        to_id="supervisor-p1", body="handled",
    )
    await db.mark_read(history.id)
    await db.update_project("p1", supervisor_enabled=False)
    assert (await db.get_message(history.id)).to_id == "supervisor-p1"
    await db.create_project(Project(id="global", name="Global"))
    for address in ("supervisor-global", "n-supervisor--global"):
        msg = await db.create_message(
            project_id="p1", from_kind="system", from_id="event", to_kind="session",
            to_id=address, body="global notice",
        )
        await db.redirect_disabled_supervisor_messages()
        assert (await db.get_message(msg.id)).to_id == address


async def test_migration_backfills_false_is_idempotent_and_defaults_new_rows(db):
    revision = import_module("migrations.versions.a00000000094_project_supervisor_opt_in")

    def migrate_and_check(conn):
        with Operations.context(MigrationContext.configure(conn)):
            # Emulate an old install; this database belongs to this test only.
            revision.downgrade()
            revision.upgrade()
            assert conn.execute(sa.select(projects.c.supervisor_enabled)).scalars().all() == [
                False, False,
            ]
            conn.execute(sa.update(projects).where(projects.c.id == "p1").values(
                supervisor_enabled=True,
            ))
            revision.upgrade()
            conn.execute(sa.insert(projects).values(id="new", name="New", created_at=1))
            assert conn.execute(sa.select(projects.c.supervisor_enabled).where(
                projects.c.id == "new",
            )).scalar_one() is False
            assert conn.execute(sa.select(projects.c.supervisor_enabled).where(
                projects.c.id == "p1",
            )).scalar_one() is True
            column = next(col for col in sa.inspect(conn).get_columns("projects")
                          if col["name"] == "supervisor_enabled")
            assert column["nullable"] is False
            assert column["default"] == "false"

    async with db._engine.begin() as conn:
        await conn.run_sync(migrate_and_check)
