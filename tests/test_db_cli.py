"""Operator recovery refusals and a real PostgreSQL custom-archive round trip."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from sqlalchemy import text

import src.cli.db as db_cli
import src.database.backup as backup
from src.cli.app import cli
from src.database.engine import create_postgres_engine
from src.database.schema_key import alembic_head_revisions
from tests.pg_dsn import create_scratch_database


@pytest.fixture(autouse=True)
def _pg_backend():
    """Only the round-trip tests below allocate databases, explicitly."""


@pytest.fixture
def operator(tmp_path, monkeypatch):
    # Only test-owned configuration is addressable. Keep worker DB sentinels intact.
    monkeypatch.setattr(db_cli, "_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setattr("src.database.migration_guard.current_scope", lambda: "cli")
    monkeypatch.delenv("AQ_API_TOKEN", raising=False)
    monkeypatch.setattr("src.daemon_state.find_daemon_pid", lambda *args: None)


@pytest.fixture
def preflight(operator, tmp_path, monkeypatch):
    dump = tmp_path / "source.dump"
    dump.write_bytes(b"PGDMPfake archive")
    info = backup.ArchiveInfo(datetime(2026, 10, 6, tzinfo=UTC), (alembic_head_revisions()[0],))
    config = SimpleNamespace(
        database=SimpleNamespace(url="postgresql://fixture@localhost/fixture"),
        sessions=SimpleNamespace(tmux_socket="fixture"),
    )
    monkeypatch.setattr(db_cli, "_load_config", lambda: config)
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(db_cli, "_make_engine", lambda config: (engine, config.database.url))
    monkeypatch.setattr(backup, "postgres_clients", AsyncMock(return_value=object()))
    monkeypatch.setattr(backup, "inspect_archive", AsyncMock(return_value=info))
    monkeypatch.setattr(backup, "assert_sessions_ended", AsyncMock())
    monkeypatch.setattr(backup, "loss_bound", AsyncMock(return_value={"tasks": 3, "messages": 2}))
    monkeypatch.setattr(backup, "backup_database", AsyncMock(return_value=info))
    monkeypatch.setattr(backup, "restore_database", AsyncMock())
    return dump, info


def test_worker_restore_refuses_before_loading_config(tmp_path, monkeypatch):
    dump = tmp_path / "source.dump"
    dump.write_bytes(b"PGDMP")
    monkeypatch.setattr("src.database.migration_guard.current_scope", lambda: "worker")
    monkeypatch.setattr(db_cli, "_load_config", lambda: pytest.fail("worker must not load config"))
    result = CliRunner().invoke(cli, ["db", "restore", str(dump), "--force"])
    assert result.exit_code != 0
    assert "restore_operator_only" in result.output


def test_session_token_restore_cannot_bypass_worker_guard(operator, tmp_path, monkeypatch):
    dump = tmp_path / "source.dump"
    dump.write_bytes(b"PGDMP")
    monkeypatch.setenv("AQ_API_TOKEN", "test-session-token")
    result = CliRunner().invoke(cli, ["db", "restore", str(dump), "--force"])
    assert "restore_operator_only" in result.output


def test_live_daemon_refuses_without_force(preflight, monkeypatch):
    dump, _ = preflight
    monkeypatch.setattr("src.daemon_state.find_daemon_pid", lambda *args: 123)
    result = CliRunner().invoke(cli, ["db", "restore", str(dump)])
    assert result.exit_code != 0
    assert "restore_daemon_live" in result.output
    backup.inspect_archive.assert_not_awaited()
    backup.backup_database.assert_not_awaited()
    backup.restore_database.assert_not_awaited()


@pytest.mark.parametrize("accepted", [None, "wrong-timestamp"])
def test_missing_or_wrong_data_loss_acceptance_does_not_write(preflight, accepted):
    dump, info = preflight
    args = ["db", "restore", str(dump)]
    if accepted:
        args += ["--accept-data-loss", accepted]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    assert "restore_data_loss_unaccepted" in result.output
    assert info.timestamp in result.output
    assert "tasks: 3" in result.output and "messages: 2" in result.output
    backup.backup_database.assert_not_awaited()
    backup.restore_database.assert_not_awaited()
    assert not (dump.parent / "daemon.lock").exists()


def test_plain_sql_refuses_even_without_postgres_clients(preflight):
    dump, _ = preflight
    dump.write_text("-- legacy plain SQL")
    result = CliRunner().invoke(cli, ["db", "restore", str(dump)])
    assert result.exit_code != 0
    assert "restore_format_unsupported" in result.output and "psql" in result.output
    backup.postgres_clients.assert_not_awaited()
    backup.restore_database.assert_not_awaited()


@pytest.mark.parametrize("revisions", [(), ("future_revision",)])
def test_unstamped_or_unknown_schema_refuses_before_target_connection(
    preflight, monkeypatch, revisions
):
    dump, info = preflight
    backup.inspect_archive.return_value = backup.ArchiveInfo(info.created_at, revisions)
    monkeypatch.setattr(
        db_cli, "_make_engine", lambda _: pytest.fail("schema must be checked first")
    )
    result = CliRunner().invoke(cli, ["db", "restore", str(dump)])
    assert result.exit_code != 0
    assert "restore_schema_mismatch" in result.output
    backup.backup_database.assert_not_awaited()
    backup.restore_database.assert_not_awaited()


def test_force_does_not_override_live_agent_sessions(preflight):
    dump, info = preflight
    backup.assert_sessions_ended.side_effect = backup.BackupError("restore_sessions_live: worker")
    result = CliRunner().invoke(
        cli,
        [
            "db",
            "restore",
            str(dump),
            "--force",
            "--accept-data-loss",
            info.timestamp,
        ],
    )
    assert result.exit_code != 0
    assert "restore_sessions_live" in result.output
    backup.backup_database.assert_not_awaited()
    backup.restore_database.assert_not_awaited()


def test_recovery_backup_precedes_restore_and_force_only_overrides_daemon(preflight, monkeypatch):
    dump, info = preflight
    calls = []
    monkeypatch.setattr("src.daemon_state.find_daemon_pid", lambda *args: 123)

    async def recovery(*args):
        assert args[1].name.startswith("pre-restore-")
        calls.append("backup")

    async def restore(*args):
        calls.append("restore")

    backup.backup_database.side_effect = recovery
    backup.restore_database.side_effect = restore
    result = CliRunner().invoke(
        cli,
        [
            "db",
            "restore",
            str(dump),
            "--force",
            "--accept-data-loss",
            info.timestamp,
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["backup", "restore"]
    assert backup.assert_sessions_ended.await_count == 2
    assert not (dump.parent / "daemon.lock").exists()


def test_failed_recovery_backup_preserves_target(preflight):
    dump, info = preflight
    backup.backup_database.side_effect = backup.BackupError("disk full")
    result = CliRunner().invoke(
        cli,
        [
            "db",
            "restore",
            str(dump),
            "--accept-data-loss",
            info.timestamp,
        ],
    )
    assert result.exit_code != 0 and "disk full" in result.output
    backup.restore_database.assert_not_awaited()


def test_daemon_start_lock_refuses_even_with_force(preflight):
    dump, info = preflight
    (dump.parent / "daemon.lock").mkdir()
    result = CliRunner().invoke(
        cli,
        [
            "db",
            "restore",
            str(dump),
            "--force",
            "--accept-data-loss",
            info.timestamp,
        ],
    )
    assert result.exit_code != 0 and "restore_start_in_progress" in result.output
    backup.restore_database.assert_not_awaited()


async def test_local_clients_use_configured_endpoint_and_keep_password_out_of_argv(
    tmp_path, monkeypatch
):
    dump = tmp_path / "pg_dump"
    restore = tmp_path / "pg_restore"
    restore.touch()
    (tmp_path / "psql").touch()
    monkeypatch.setattr("src.install.update.find_pg_dump", lambda: str(dump))
    clients = await backup.postgres_clients("postgresql+asyncpg://custom:secret@db.example:6543/aq")
    assert clients.prefix == ()
    assert clients.env["PGPASSWORD"] == "secret"
    assert clients.connection == ("--dbname", "postgresql://custom@db.example:6543/aq")
    assert "secret" not in " ".join(clients.connection)


async def test_docker_fallback_only_uses_container_serving_configured_port(monkeypatch):
    monkeypatch.setattr("src.install.update.find_pg_dump", lambda: None)
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/docker")
    run = AsyncMock(side_effect=[b"test-container\n", b'{"5432/tcp":[{"HostPort":"5534"}]}'])
    monkeypatch.setattr(backup, "_run", run)
    clients = await backup.postgres_clients("postgresql://test:secret@127.0.0.1:5534/fixture")
    assert clients.prefix[-1] == "test-container"
    assert clients.connection[-1] == "fixture"
    assert "secret" not in " ".join(clients.prefix + clients.connection)
    assert all("compose" not in call.args[0] for call in run.call_args_list)
    assert "publish=5534" in run.call_args_list[0].args[0]


@pytest.mark.parametrize("local_major, expected_prefix", [(16, "docker"), (18, None), (19, None)])
async def test_clients_use_serving_container_when_local_tools_are_older(
    tmp_path, monkeypatch, local_major, expected_prefix
):
    (tmp_path / "pg_restore").touch()
    (tmp_path / "psql").touch()
    monkeypatch.setattr("src.install.update.find_pg_dump", lambda: str(tmp_path / "pg_dump"))
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/docker")
    run = AsyncMock(
        side_effect=[
            b"test-container\n",
            b'{"5432/tcp":[{"HostPort":"5534"}]}',
            f"pg_dump (PostgreSQL) {local_major}.0\n".encode(),
            b"pg_dump (PostgreSQL) 18.0\n",
        ]
    )
    monkeypatch.setattr(backup, "_run", run)
    clients = await backup.postgres_clients("postgresql://test:secret@localhost:5534/fixture")
    assert (clients.prefix[0] if clients.prefix else None) == expected_prefix
    assert all("--version" in call.args[0] for call in run.call_args_list[2:])


async def test_local_clients_remain_available_when_docker_is_unavailable(tmp_path, monkeypatch):
    (tmp_path / "pg_restore").touch()
    (tmp_path / "psql").touch()
    monkeypatch.setattr("src.install.update.find_pg_dump", lambda: str(tmp_path / "pg_dump"))
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        backup, "_run", AsyncMock(side_effect=backup.BackupError("docker permission denied"))
    )
    clients = await backup.postgres_clients("postgresql://test:secret@localhost:5534/fixture")
    assert clients.prefix == ()


async def test_failed_backup_removes_partial_archive_and_never_overwrites(tmp_path):
    target = tmp_path / "database.dump"
    clients = SimpleNamespace(
        run=AsyncMock(side_effect=backup.BackupError("dump failed")), connection=()
    )
    with pytest.raises(backup.BackupError, match="dump failed"):
        await backup.backup_database(clients, target)
    assert not target.exists()
    target.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        await backup.backup_database(clients, target)
    assert target.read_bytes() == b"keep"


@pytest.fixture
async def fixture_db(operator, monkeypatch):
    url = await create_scratch_database("db_restore")
    engine = create_postgres_engine(url)
    config = SimpleNamespace(
        database=SimpleNamespace(url=url),
        sessions=SimpleNamespace(tmux_socket="aq-test-db-restore"),
    )
    monkeypatch.setattr(db_cli, "_load_config", lambda: config)
    monkeypatch.setattr(
        db_cli,
        "_make_engine",
        lambda cfg: (create_postgres_engine(cfg.database.url), cfg.database.url),
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        )
        await conn.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": alembic_head_revisions()[0]},
        )
        await conn.execute(
            text("CREATE TABLE tasks (id text PRIMARY KEY, created_at float, updated_at float)")
        )
        await conn.execute(text("INSERT INTO tasks VALUES ('original', 1, 1)"))
        await conn.execute(text("CREATE TABLE events (id int PRIMARY KEY, timestamp float)"))
        await conn.execute(
            text(
                "CREATE TABLE agent_waits (id text PRIMARY KEY, created_at float, resolved_at float)"
            )
        )
        await conn.execute(text("CREATE TABLE gates (id text PRIMARY KEY, created_at float)"))
        await conn.execute(text("CREATE TABLE messages (id text PRIMARY KEY, created_at float)"))
        await conn.execute(text("CREATE TABLE sessions (name text, state text, ended_at float)"))
    try:
        yield engine, config
    finally:
        await engine.dispose()


async def invoke(*args):
    return await asyncio.to_thread(CliRunner().invoke, cli, list(args))


async def test_backup_restore_round_trip_with_recovery_and_loss_counts(fixture_db, tmp_path):
    engine, config = fixture_db
    dump = tmp_path / "database.dump"
    result = await invoke("db", "backup", str(dump))
    assert result.exit_code == 0, result.output
    assert dump.read_bytes().startswith(b"PGDMP")
    assert dump.stat().st_mode & 0o777 == 0o600
    clients = await backup.postgres_clients(config.database.url)
    info = await backup.inspect_archive(clients, dump)
    assert info.revisions == tuple(alembic_head_revisions())
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tasks VALUES ('new', :now, :now)"),
            {"now": info.created_at.timestamp() + 10},
        )
        for table, column in (
            ("events", "timestamp"),
            ("agent_waits", "created_at"),
            ("gates", "created_at"),
            ("messages", "created_at"),
        ):
            await conn.execute(
                text(f"INSERT INTO {table} (id, {column}) VALUES (1, :now)"),
                {"now": info.created_at.timestamp() + 10},
            )
    result = await invoke("db", "restore", str(dump), "--accept-data-loss", info.timestamp)
    assert result.exit_code == 0, result.output
    assert all(
        f"{table}: 1" in result.output
        for table in ("tasks", "events", "agent_waits", "gates", "messages")
    )
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT id FROM tasks"))).scalars().all() == ["original"]
        assert (
            await conn.scalar(text("SELECT version_num FROM alembic_version")) == info.revisions[0]
        )
    (recovery,) = (tmp_path / "backups").glob("pre-restore-*.dump")
    await backup.restore_database(clients, recovery)
    async with engine.connect() as conn:
        assert set((await conn.execute(text("SELECT id FROM tasks"))).scalars()) == {
            "original",
            "new",
        }


async def test_unverified_database_session_blocks_restore_with_force(
    fixture_db, tmp_path, monkeypatch
):
    engine, _ = fixture_db
    dump = tmp_path / "database.dump"
    result = await invoke("db", "backup", str(dump))
    assert result.exit_code == 0, result.output
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO sessions VALUES ('worker', 'sleeping', NULL)"))
    real_which = backup.shutil.which
    monkeypatch.setattr(
        backup.shutil, "which", lambda name: None if name == "tmux" else real_which(name)
    )
    result = await invoke("db", "restore", str(dump), "--force")
    assert result.exit_code != 0 and "restore_sessions_unverified" in result.output
    assert not list((tmp_path / "backups").glob("pre-restore-*.dump"))


async def test_archive_schema_is_checked_and_unknown_revision_preserves_target(
    fixture_db, tmp_path
):
    engine, config = fixture_db
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = 'future_revision'"))
    dump = tmp_path / "future.dump"
    result = await invoke("db", "backup", str(dump))
    assert result.exit_code == 0, result.output
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": alembic_head_revisions()[0]},
        )
    clients = await backup.postgres_clients(config.database.url)
    info = await backup.inspect_archive(clients, dump)
    result = await invoke("db", "restore", str(dump), "--accept-data-loss", info.timestamp)
    assert result.exit_code != 0 and "restore_schema_mismatch" in result.output
    async with engine.connect() as conn:
        assert (
            await conn.scalar(text("SELECT version_num FROM alembic_version"))
            == alembic_head_revisions()[0]
        )
    assert not list((tmp_path / "backups").glob("pre-restore-*.dump"))


async def test_failed_restore_rolls_back_schema_replacement_and_keeps_recovery_archive(
    fixture_db, tmp_path, monkeypatch
):
    engine, config = fixture_db
    dump = tmp_path / "database.dump"
    result = await invoke("db", "backup", str(dump))
    assert result.exit_code == 0, result.output
    clients = await backup.postgres_clients(config.database.url)
    info = await backup.inspect_archive(clients, dump)
    async with engine.begin() as conn:
        # Keep a target-only table and newer data to prove replacement rolls
        # back when the archived SQL fails after DROP SCHEMA.
        await conn.execute(text("CREATE TABLE blocker (id text REFERENCES tasks(id))"))
        await conn.execute(
            text("INSERT INTO messages VALUES ('keep', :now)"),
            {"now": info.created_at.timestamp() + 10},
        )
    real_run = backup.PostgresClients.run

    async def broken_sql(self, tool, *args, **kwargs):
        output = await real_run(self, tool, *args, **kwargs)
        if tool == "pg_restore" and kwargs.get("destination"):
            with kwargs["destination"].open("ab") as handle:
                handle.write(b"\nSELECT * FROM restore_failure_fixture;\n")
        return output

    monkeypatch.setattr(backup.PostgresClients, "run", broken_sql)
    result = await invoke("db", "restore", str(dump), "--accept-data-loss", info.timestamp)
    assert result.exit_code != 0 and "backup_client_failed" in result.output
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT id FROM tasks")) == "original"
        assert await conn.scalar(text("SELECT id FROM messages")) == "keep"
        assert await conn.scalar(text("SELECT to_regclass('public.blocker')")) == "blocker"
    assert len(list((tmp_path / "backups").glob("pre-restore-*.dump"))) == 1


@pytest.mark.parametrize("name", ["n-supervisor--fixture", "s-task", "p-worker--fixture"])
async def test_surviving_tmux_session_blocks_before_database_probe(monkeypatch, name):
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(backup, "_run", AsyncMock(return_value=(name + "\n").encode()))
    config = SimpleNamespace(sessions=SimpleNamespace(tmux_socket="test-socket"))
    with pytest.raises(backup.BackupError, match="restore_sessions_live"):
        await backup.assert_sessions_ended(config, None)


@pytest.mark.parametrize(
    "error", ["permission denied", "backup_client_unavailable: No such file or directory"]
)
async def test_tmux_probe_failure_does_not_authorize_restore(monkeypatch, error):
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(backup, "_run", AsyncMock(side_effect=backup.BackupError(error)))
    config = SimpleNamespace(sessions=SimpleNamespace(tmux_socket="test-socket"))
    with pytest.raises(backup.BackupError, match=error):
        await backup.assert_sessions_ended(config, None)


async def test_restore_older_schema_removes_newer_tables_and_resets_stamp(fixture_db, tmp_path):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from src.database.engine import _ALEMBIC_INI

    engine, config = fixture_db
    script = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI)))
    # Pin the archive's schema to the migration exercised below. A later
    # head must not stamp this pre-promotion-flow fixture as a newer schema.
    older = script.get_revision("a00000000085").down_revision
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = :rev"), {"rev": older})
        await conn.execute(text("CREATE TABLE integration_batches (id text PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO integration_batches VALUES ('original')"))
        # Revision 85 adds promotion_flow; the archive predates that column.
        await conn.execute(text("CREATE TABLE projects (id text PRIMARY KEY)"))
    dump = tmp_path / "older.dump"
    assert (await invoke("db", "backup", str(dump))).exit_code == 0
    clients = await backup.postgres_clients(config.database.url)
    info = await backup.inspect_archive(clients, dump)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE later_table (id text REFERENCES tasks(id))"))
        await conn.execute(text("INSERT INTO later_table VALUES ('original')"))
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN later_column text"))
        await conn.execute(
            text("UPDATE alembic_version SET version_num = :rev"),
            {"rev": alembic_head_revisions()[0]},
        )
    result = await invoke("db", "restore", str(dump), "--accept-data-loss", info.timestamp)
    assert result.exit_code == 0, result.output
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT to_regclass('public.later_table')")) is None
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == older
        assert await conn.scalar(text("SELECT id FROM tasks")) == "original"
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='tasks' AND column_name='later_column'"
                )
            )
            == 0
        )
    # The next upgrade applies the actual newer migration to the restored
    # scratch schema, rather than finding leftover post-deployment objects.
    from src.database.engine import run_schema_setup

    await run_schema_setup(engine)
    async with engine.begin() as conn:
        assert (
            await conn.scalar(text("SELECT version_num FROM alembic_version"))
            == alembic_head_revisions()[0]
        )
        assert await conn.scalar(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='projects' AND column_name='promotion_flow'"
        )) == 1
        assert await conn.scalar(text("SELECT to_regclass('public.agent_cron')")) == "agent_cron"
        assert await conn.scalar(text("SELECT id FROM tasks")) == "original"


@pytest.mark.parametrize("no_server", [False, True])
async def test_aq_stop_stale_session_rows_allow_restore_after_tmux_proof(
    fixture_db, tmp_path, monkeypatch, no_server
):
    engine, config = fixture_db
    dump = tmp_path / "stopped.dump"
    assert (await invoke("db", "backup", str(dump))).exit_code == 0
    clients = await backup.postgres_clients(config.database.url)
    info = await backup.inspect_archive(clients, dump)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO sessions VALUES ('p-worker', 'sleeping', NULL)"))
    real_run, real_which = backup._run, backup.shutil.which

    async def probe(argv, **kwargs):
        if argv[0] == "tmux":
            if no_server:
                raise backup.BackupError("no server running on fixture socket")
            return b"personal-shell\n"
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(
        backup.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else real_which(name)
    )
    monkeypatch.setattr(backup, "_run", probe)
    result = await invoke("db", "restore", str(dump), "--accept-data-loss", info.timestamp)
    assert result.exit_code == 0, result.output


async def test_missing_restore_target_reports_a_clear_error(fixture_db, tmp_path, monkeypatch):
    from sqlalchemy.engine import make_url

    _, config = fixture_db
    clients = await backup.postgres_clients(config.database.url)
    dump = tmp_path / "source.dump"
    info = await backup.backup_database(clients, dump)
    missing = (
        make_url(config.database.url)
        .set(database="aq_test_restore_missing_fixture")
        .render_as_string(hide_password=False)
    )
    config.database.url = missing
    result = await invoke("db", "restore", str(dump), "--accept-data-loss", info.timestamp)
    assert result.exit_code != 0
    assert "restore_target_unavailable" in result.output
    assert "target database exists" in result.output
    assert "Traceback" not in result.output
    assert not list((tmp_path / "backups").glob("pre-restore-*.dump"))


async def test_backup_and_restore_clients_share_update_timeout(monkeypatch):
    from src.install.update import BACKUP_TIMEOUT

    proc = SimpleNamespace(communicate=AsyncMock(return_value=(b"ok", b"")), returncode=0)
    monkeypatch.setattr(backup.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    wait_for = backup.asyncio.wait_for
    observed = []

    async def capture(awaitable, timeout):
        observed.append(timeout)
        return await wait_for(awaitable, timeout)

    monkeypatch.setattr(backup.asyncio, "wait_for", capture)
    clients = backup.PostgresClients((), "pg_dump", "pg_restore", (), {})
    for tool in ("pg_dump", "pg_restore", "psql"):
        await clients.run(tool, "--version")
    assert observed == [BACKUP_TIMEOUT] * 3
