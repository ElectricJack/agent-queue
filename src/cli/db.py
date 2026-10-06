"""Database CLI (``aq db ...``) — the operator's migration and recovery door.

Migrations against the production database are daemon-only by policy
(:mod:`src.database.migration_guard`).  This module is the one exception that
policy names: an operator who types ``aq db upgrade`` is declaring intent, so
the command claims :data:`~src.database.migration_guard.OPERATOR` scope for
the duration of the upgrade — and nothing else in the CLI ever does.

``aq db current`` is the read-only companion.  It answers "is my schema
behind?" without touching anything, which is the question a worker who just
hit ``schema behind code; ask the operator to upgrade`` actually has.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import click

from .app import cli, console
from .envelope import reject_json_mode

_CONFIG_PATH = os.path.expanduser("~/.agent-queue/config.yaml")


def _load_config():
    from src.config import load_config

    return load_config(_CONFIG_PATH)


def _head_revisions() -> list[str]:
    """This checkout's Alembic head(s)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from src.database.engine import _ALEMBIC_INI

    return sorted(ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_heads())


def _make_engine(config):
    """An engine for *config*'s database that has not run any migration."""
    from src.database.engine import create_postgres_engine

    url = config.database.url
    return create_postgres_engine(url, 1, 2), url


def _display_url(url: str) -> str:
    from src.database import redact_dsn
    from src.database.migration_guard import normalize_database_url

    return redact_dsn(normalize_database_url(url) or url)


@cli.group("db")
@click.pass_context
def db_group(ctx: click.Context) -> None:
    """Database administration — schema, custom backups and disaster recovery."""
    reject_json_mode(
        ctx,
        "aq db",
        "database administration is a local operator workflow with interactive safeguards",
    )


@db_group.command("current")
def db_current() -> None:
    """Show the stamped revision(s) and this checkout's head. Read-only."""

    async def _main() -> int:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        config = _load_config()
        engine, url = _make_engine(config)
        try:
            async with engine.connect() as conn:
                try:
                    result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                    stamped = sorted(row[0] for row in result.fetchall())
                except SQLAlchemyError:
                    # No ``alembic_version`` table — an unstamped database.
                    stamped = []
        finally:
            await engine.dispose()

        head = _head_revisions()
        console.print(f"database: [cyan]{_display_url(url)}[/]")
        console.print(f"stamped:  {', '.join(stamped) or '[yellow]unstamped[/]'}")
        console.print(f"head:     {', '.join(head) or '[yellow]none[/]'}")
        if stamped == head:
            console.print("[green]schema is at head[/]")
            return 0
        console.print("[red]schema is not at head[/] — an operator must run `aq db upgrade`")
        return 1

    raise SystemExit(asyncio.run(_main()))


@db_group.command("upgrade")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
def db_upgrade(yes: bool) -> None:
    """Run Alembic migrations against the configured database (operator only)."""
    from src.database.migration_guard import (
        OPERATOR,
        WORKER,
        current_scope,
        process_scope,
        production_database_url,
    )

    if current_scope() == WORKER:
        console.print(
            "[red]Refused:[/] this is a worker session (AQ_DB_SCOPE=worker). Worker "
            "sessions must never migrate the production database — ask the operator to "
            "run `aq db upgrade` outside a worktree slot."
        )
        raise SystemExit(2)

    console.print(
        f"About to migrate [cyan]{_display_url(production_database_url())}[/] to "
        f"{', '.join(_head_revisions())}."
    )
    if not yes and not click.confirm("Continue?", default=False):
        raise SystemExit(1)

    async def _main() -> None:
        from src.database import create_database

        db = create_database(_load_config())
        await db.initialize()
        await db.close()

    with process_scope(OPERATOR):
        asyncio.run(_main())
    console.print(f"[green]schema at head[/] ({', '.join(_head_revisions())})")


def _require_operator() -> None:
    from src.database.migration_guard import WORKER, current_scope

    if current_scope() == WORKER or os.environ.get("AQ_API_TOKEN"):
        raise click.ClickException(
            "restore_operator_only: database backup/restore is a local operator workflow; "
            "worker sessions must use fixture databases through tests"
        )


def _backup_destination(prefix: str) -> Path:
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return Path(_CONFIG_PATH).parent / "backups" / f"{prefix}-{stamp}.dump"


@db_group.command("backup")
@click.argument("destination", required=False, type=click.Path(dir_okay=False, path_type=Path))
def db_backup(destination: Path | None) -> None:
    """Back up the configured database as one private pg_dump custom archive."""
    _require_operator()

    async def _main() -> None:
        from src.database.backup import backup_database, postgres_clients

        clients = await postgres_clients(_load_config().database.url)
        path = destination or _backup_destination("agent-queue")
        info = await backup_database(clients, path)
        console.print(f"Backup written: {path} ({path.stat().st_size:,} bytes)", markup=False)
        console.print(f"Dump timestamp: {info.timestamp}", markup=False)
        console.print(f"Schema: {', '.join(info.revisions) or 'unstamped'}", markup=False)

    try:
        asyncio.run(_main())
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise click.ClickException(str(exc)) from exc


@db_group.command("restore")
@click.argument("dump", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--accept-data-loss",
    metavar="DUMP_TIMESTAMP",
    help="Accept data loss by repeating the UTC dump timestamp printed by preflight.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Override the live-daemon refusal; agent sessions must still have ended.",
)
def db_restore(dump: Path, accept_data_loss: str | None, force: bool) -> None:
    """Replace the configured database from a custom archive (operator only).

    Checks the archived schema before writing, prints the loss bound, and takes
    a recovery backup before pg_restore. Known older revisions are accepted;
    upgrade the restored schema with `aq db upgrade` before restarting.
    """
    _require_operator()
    from src.daemon_state import acquire_start_lock, find_daemon_pid, release_start_lock

    lock = str(Path(_CONFIG_PATH).parent / "daemon.lock")
    if not acquire_start_lock(lock):
        raise click.ClickException("restore_start_in_progress: daemon start/restore lock is held")

    async def _main() -> None:
        from src.database.backup import (
            BackupError,
            assert_custom_archive,
            assert_sessions_ended,
            backup_database,
            inspect_archive,
            loss_bound,
            postgres_clients,
            restore_database,
            validate_revisions,
        )

        pid_file = str(Path(_CONFIG_PATH).parent / "daemon.pid")
        if not force and await asyncio.to_thread(find_daemon_pid, pid_file, _CONFIG_PATH):
            raise BackupError(
                "restore_daemon_live: stop the daemon first, or explicitly use --force"
            )
        config = _load_config()
        assert_custom_archive(dump)
        clients = await postgres_clients(config.database.url)
        info = await inspect_archive(clients, dump)
        validate_revisions(info)
        console.print(f"Restore target: {_display_url(config.database.url)}", markup=False)
        console.print(f"Dump timestamp: {info.timestamp}", markup=False)
        console.print(f"Archived schema: {', '.join(info.revisions)}", markup=False)
        engine, _ = _make_engine(config)
        try:
            await assert_sessions_ended(config, engine)
            counts = await loss_bound(engine, info.created_at)
            console.print("Data-loss bound: rows created/changed since the dump:")
            for table, count in counts.items():
                console.print(f"  {table}: {count}", markup=False)
            console.print("Deleted rows and changes without timestamps cannot be counted.")
            if accept_data_loss != info.timestamp:
                raise BackupError(
                    "restore_data_loss_unaccepted: repeat --accept-data-loss " + info.timestamp
                )
            recovery = _backup_destination("pre-restore")
            await backup_database(clients, recovery)
            console.print(f"Recovery backup: {recovery}", markup=False)
            # Check again after a potentially lengthy backup; the start lock is
            # still held so aq start and its watchdog cannot race the restore.
            if not force and await asyncio.to_thread(find_daemon_pid, pid_file, _CONFIG_PATH):
                raise BackupError("restore_daemon_live: daemon became live during preflight")
            await assert_sessions_ended(config, engine)
        finally:
            await engine.dispose()
        await restore_database(clients, dump)
        console.print("Restore complete. Run `aq db current` before restarting.")

    try:
        asyncio.run(_main())
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        release_start_lock(lock, owner=os.getpid())


@db_group.command("import-sqlite")
@click.argument("sqlite_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
def db_import_sqlite(sqlite_path: str, yes: bool) -> None:
    """Copy a pre-PostgreSQL SQLite database into the configured PostgreSQL one.

    One-way and one-time: SQLite is no longer a backend, this only carries old
    data across.  The target must be empty — importing over a live database
    would interleave two histories.

    Needs the optional reader: ``pip install "agent-queue[sqlite-import]"``.
    """
    config = _load_config()
    target = config.database.url
    console.print(f"[bold]Import[/] {sqlite_path}\n[bold]Into[/]   {_display_url(target)}")
    if not yes and not click.confirm("Proceed?", default=False):
        console.print("Aborted.")
        return

    from src.database.legacy_sqlite_import import migrate_sqlite_to_postgres

    try:
        asyncio.run(migrate_sqlite_to_postgres(sqlite_path, target))
    except ImportError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        raise SystemExit(1) from None
    console.print("[bold green]Import complete.[/]")
