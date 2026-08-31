"""Global supervisor persistence migrates without losing history or real projects."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

PRIOR_REVISION = "d4e5f6a7b8c9"


def _migrate(engine, target="head", *, downgrade=False):
    cfg = Config("alembic.ini")
    # Supplying the connection takes precedence over config and environment URLs.
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        if downgrade:
            command.downgrade(cfg, target)
        else:
            command.upgrade(cfg, target)
        conn.commit()


@pytest.fixture
def legacy_db(tmp_path: Path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'projectless.db'}")
    _migrate(engine, PRIOR_REVISION)

    # Exercise the actual startup setting, including messages' self-reference.
    @sa.event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    engine.dispose()
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO projects (id, name, created_at) VALUES ('global', 'Global', 1.0)")
        )
        conn.execute(
            sa.text(
                "INSERT INTO projects (id, name, created_at) VALUES ('p1', 'Real project', 2.0)"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _session(conn, sid="global-session", project_id="global", name="n-supervisor--global"):
    conn.execute(
        sa.text(
            "INSERT INTO sessions (id, project_id, profile_id, harness, provider, name, "
            "lifecycle, state, desired_state, work_dir, epoch, instance_token, started_at, "
            "session_key, last_activity) VALUES (:id, :project_id, 'supervisor', 'claude', "
            "'runtime', :name, 'named', 'stopped', 'stopped', '/vault', 'epoch-1', "
            "'token-1', 10.0, 'resume-key', 12.0)"
        ),
        {"id": sid, "project_id": project_id, "name": name},
    )


def _message(
    conn,
    mid="m1",
    project_id="global",
    from_id="operator",
    to_id="supervisor-global",
    thread_id=None,
    reply_to_id=None,
):
    conn.execute(
        sa.text(
            "INSERT INTO messages (id, project_id, from_kind, from_id, to_kind, to_id, "
            "thread_id, body, created_at, delivered_at, read_at, reply_to_id, subject) "
            "VALUES (:id, :project_id, 'user', :from_id, 'session', :to_id, :thread_id, "
            "'Keep this exact history', 10.0, 11.0, 12.0, :reply_to_id, 'Original subject')"
        ),
        {
            "id": mid,
            "project_id": project_id,
            "from_id": from_id,
            "to_id": to_id,
            "thread_id": thread_id,
            "reply_to_id": reply_to_id,
        },
    )


def _rows(conn, table):
    return {
        row["id"]: dict(row) for row in conn.execute(sa.text(f"SELECT * FROM {table}")).mappings()
    }


def test_upgrade_preserves_global_history_and_removes_placeholder(legacy_db):
    with legacy_db.begin() as conn:
        _session(conn)
        _session(conn, "older-session")
        _session(conn, "other-project-session", "p1")
        _message(conn)
        _message(conn, "m2", from_id="supervisor-global", to_id="operator", reply_to_id="m1")
        _message(conn, "m3", from_id="n-supervisor--global", to_id="operator", reply_to_id="m2")
        _message(conn, "m4", to_id="operator", thread_id="dashboard:global", reply_to_id="m3")
        _message(conn, "other-project-message", "p1")
        before = {table: _rows(conn, table) for table in ("sessions", "messages")}

    _migrate(legacy_db)

    with legacy_db.connect() as conn:
        assert conn.execute(sa.text("SELECT id FROM projects ORDER BY id")).scalars().all() == [
            "p1"
        ]
        for table in ("sessions", "messages"):
            after = _rows(conn, table)
            assert set(after) == set(before[table])
            for row_id, original in before[table].items():
                expected_project = "p1" if row_id.startswith("other-project-") else None
                # Later migrations may add columns; compare the columns that
                # existed at the revision under test.
                migrated = {k: after[row_id][k] for k in original}
                assert migrated == {**original, "project_id": expected_project}
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_upgrade_removes_unused_placeholder(legacy_db):
    _migrate(legacy_db)
    with legacy_db.connect() as conn:
        assert conn.execute(sa.text("SELECT id FROM projects WHERE id='global'")).first() is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "Global product"),
        ("credit_weight", 2.0),
        ("max_concurrent_agents", 4),
        ("status", "PAUSED"),
        ("total_tokens_used", 30),
        ("budget_limit", 100),
        ("workspace_path", "/real/repo"),
        ("discord_channel_id", "channel"),
        ("discord_control_channel_id", "control"),
        ("repo_url", "https://example.com/repo"),
        ("repo_default_branch", "develop"),
    ],
)
def test_upgrade_preserves_configured_global_project(legacy_db, field, value):
    with legacy_db.begin() as conn:
        conn.execute(
            sa.text(f"UPDATE projects SET {field}=:value WHERE id='global'"), {"value": value}
        )
        _session(conn)
        _message(conn)
        original = _rows(conn, "projects")["global"]
    _migrate(legacy_db)
    with legacy_db.connect() as conn:
        assert _rows(conn, "projects")["global"] == original
        assert _rows(conn, "sessions")["global-session"]["project_id"] is None
        assert _rows(conn, "messages")["m1"]["project_id"] is None


@pytest.mark.parametrize(
    "content", ["message", "session", "task", "workspace", "constraint", "event", "kind"]
)
def test_upgrade_preserves_global_project_with_user_data(legacy_db, content):
    with legacy_db.begin() as conn:
        _session(conn)
        _message(conn)
        if content == "message":
            _message(conn, "work-message", to_id="worker")
        elif content == "session":
            _session(conn, "worker-session", name="n-worker--global")
        else:
            sql = {
                "task": "INSERT INTO tasks (id, project_id, title, description, status, created_at, updated_at) "
                "VALUES ('t1', 'global', 'Real task', 'Keep', 'DEFINED', 1, 1)",
                "workspace": "INSERT INTO workspaces (id, project_id, workspace_path, created_at) "
                "VALUES ('w1', 'global', '/repo', 1)",
                "constraint": "INSERT INTO project_constraints (project_id, created_at) VALUES ('global', 1)",
                "event": "INSERT INTO events (event_type, project_id, payload, timestamp) "
                "VALUES ('user_action', 'global', '{}', 1)",
                "kind": "INSERT INTO workspace_kinds (project_id, id, created_at, updated_at) "
                "VALUES ('global', 'custom-kind', 1, 1)",
            }[content]
            conn.execute(sa.text(sql))
        snapshots = {
            table: conn.execute(sa.text(f"SELECT * FROM {table}")).all()
            for table in (
                "projects",
                "tasks",
                "workspaces",
                "project_constraints",
                "events",
                "workspace_kinds",
            )
        }
    _migrate(legacy_db)
    with legacy_db.connect() as conn:
        for table, rows in snapshots.items():
            assert conn.execute(sa.text(f"SELECT * FROM {table}")).all() == rows
        assert _rows(conn, "sessions")["global-session"]["project_id"] is None
        assert _rows(conn, "messages")["m1"]["project_id"] is None
        if content == "message":
            assert _rows(conn, "messages")["work-message"]["project_id"] == "global"
        if content == "session":
            assert _rows(conn, "sessions")["worker-session"]["project_id"] == "global"


def test_fresh_schema_accepts_projectless_history_without_placeholder(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        _migrate(engine)
        with engine.begin() as conn:
            _session(conn, project_id=None)
            _message(conn, project_id=None)
            assert conn.execute(sa.text("SELECT count(*) FROM projects")).scalar_one() == 0
        _migrate(engine, PRIOR_REVISION, downgrade=True)
        with engine.connect() as conn:
            assert _rows(conn, "sessions")["global-session"]["project_id"] == "global"
            assert _rows(conn, "messages")["m1"]["project_id"] == "global"
            assert _rows(conn, "projects")["global"]["name"] == "Global"
            for table in ("sessions", "messages"):
                assert not next(
                    c for c in sa.inspect(conn).get_columns(table) if c["name"] == "project_id"
                )["nullable"]
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_downgrade_preserves_history_and_existing_real_project(legacy_db):
    with legacy_db.begin() as conn:
        conn.execute(
            sa.text("UPDATE projects SET repo_url='https://example.com/real' WHERE id='global'")
        )
        _session(conn)
        _message(conn)
        _message(conn, "m2", from_id="supervisor-global", reply_to_id="m1")
        original = {table: _rows(conn, table) for table in ("projects", "sessions", "messages")}
    _migrate(legacy_db)
    with legacy_db.connect() as conn:
        assert _rows(conn, "messages")["m1"]["project_id"] is None
    _migrate(legacy_db, PRIOR_REVISION, downgrade=True)
    with legacy_db.connect() as conn:
        for table, rows in original.items():
            assert _rows(conn, table) == rows
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    _migrate(legacy_db)
    with legacy_db.connect() as conn:
        assert _rows(conn, "messages")["m1"]["project_id"] is None


@pytest.mark.parametrize("unrelated_ledger", [False, True])
def test_upgrade_preserves_supervisor_token_history(legacy_db, unrelated_ledger):
    with legacy_db.begin() as conn:
        _session(conn)
        conn.execute(
            sa.text(
                "INSERT INTO token_ledger (id, project_id, agent_id, task_id, tokens_used, "
                "model, input_tokens, output_tokens, timestamp) VALUES "
                "('supervisor-cost', 'global', 'global-session', '', 150, 'model', 100, 50, 12)"
            )
        )
        if unrelated_ledger:
            conn.execute(
                sa.text(
                    "INSERT INTO token_ledger (id, project_id, agent_id, task_id, tokens_used, timestamp) "
                    "VALUES ('work-cost', 'global', 'worker-session', 't1', 200, 13)"
                )
            )
        before = _rows(conn, "token_ledger")
    _migrate(legacy_db)
    with legacy_db.connect() as conn:
        after = _rows(conn, "token_ledger")
        assert after["supervisor-cost"] == {**before["supervisor-cost"], "project_id": None}
        if unrelated_ledger:
            assert after["work-cost"] == before["work-cost"]
        has_global = conn.execute(sa.text("SELECT id FROM projects WHERE id='global'")).first()
        assert (has_global is not None) is unrelated_ledger
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    _migrate(legacy_db, PRIOR_REVISION, downgrade=True)
    with legacy_db.connect() as conn:
        assert _rows(conn, "token_ledger") == before
        assert not next(
            c for c in sa.inspect(conn).get_columns("token_ledger") if c["name"] == "project_id"
        )["nullable"]
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_downgrade_preserves_projectless_ledger_without_sessions(legacy_db):
    _migrate(legacy_db)
    with legacy_db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO token_ledger (id, project_id, agent_id, task_id, tokens_used, timestamp) "
                "VALUES ('new-cost', NULL, 'new-global-session', '', 200, 13)"
            )
        )
    _migrate(legacy_db, PRIOR_REVISION, downgrade=True)
    with legacy_db.connect() as conn:
        assert _rows(conn, "token_ledger")["new-cost"]["project_id"] == "global"
        assert _rows(conn, "projects")["global"]["name"] == "Global"
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_detaches_complete_global_reply_chain(legacy_db):
    with legacy_db.begin() as conn:
        conn.execute(sa.text("UPDATE projects SET name='Real global project' WHERE id='global'"))
        _message(conn, "parent", to_id="session-uuid")
        _message(
            conn, "anchor", from_id="supervisor-global", to_id="operator", reply_to_id="parent"
        )
        _message(conn, "reply", from_id="runtime-uuid", to_id="operator", reply_to_id="anchor")
        _message(conn, "nested", from_id="runtime-uuid", to_id="operator", reply_to_id="reply")
        _message(conn, "unrelated", to_id="worker")
        _message(conn, "other-project", "p1", to_id="worker", reply_to_id="anchor")
        _message(conn, "external-link", to_id="worker", reply_to_id="other-project")
        before = _rows(conn, "messages")
    _migrate(legacy_db)
    with legacy_db.connect() as conn:
        after = _rows(conn, "messages")
        for mid in ("parent", "anchor", "reply", "nested"):
            assert after[mid] == {**before[mid], "project_id": None}
        for mid in ("unrelated", "other-project", "external-link"):
            assert after[mid] == before[mid]
        assert _rows(conn, "projects")["global"]["name"] == "Real global project"
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
