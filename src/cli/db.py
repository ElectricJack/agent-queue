"""Database schema CLI (``aq db ...``) — the operator's migration door.

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

import click

from .app import cli, console

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
    from src.database.engine import create_postgres_engine, create_sqlite_engine

    url = config.database.url or config.database_path
    if config.database.backend == "postgresql":
        return create_postgres_engine(url, 1, 2), url
    return create_sqlite_engine(url), url


def _display_url(url: str) -> str:
    from src.database import redact_dsn
    from src.database.migration_guard import normalize_database_url

    return redact_dsn(normalize_database_url(url) or url)


@cli.group("db")
def db_group() -> None:
    """Database schema — inspect and upgrade the daemon's database."""


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
