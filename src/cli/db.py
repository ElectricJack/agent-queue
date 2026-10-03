"""Database schema CLI (``aq db ...``) — the operator's migration door.

Migrations against the production database are daemon-only by policy
(:mod:`src.database.migration_guard`).  This module is the one exception that
policy names: an operator who types ``aq db upgrade`` is declaring intent, so
the command claims :data:`~src.database.migration_guard.OPERATOR` scope for
the duration of the upgrade — and nothing else in the CLI ever does.

``aq db current`` is the read-only companion.  It answers "is my schema
behind?" without touching anything, which is the question a worker who just
hit ``schema behind code; ask the operator to upgrade`` actually has.

``aq db retire`` reports what still uses each legacy integration table family
and, with ``--apply``, is the only path that drops one: no migration does
(:mod:`src.integration.table_retirement`).  ``aq db restore-retired`` replays
a retired table from its archive.
"""

from __future__ import annotations

import asyncio
import os

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
    """Database schema — inspect and upgrade the daemon's database."""
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


def _refuse_worker(command: str) -> None:
    from src.database.migration_guard import WORKER, current_scope

    if current_scope() == WORKER:
        console.print(
            f"[red]Refused:[/] this is a worker session (AQ_DB_SCOPE=worker). `aq db {command}` "
            "changes the production database; only the operator runs it, outside a worktree slot."
        )
        raise SystemExit(2)


def _operator() -> str:
    import getpass

    return f"operator:{getpass.getuser()}"


@db_group.command("retire")
@click.argument("family_names", metavar="[FAMILY]...", nargs=-1)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Archive and drop FAMILY's tables (operator only). Without it, report only.",
)
@click.option(
    "--backup",
    type=click.Path(dir_okay=False),
    help="A `pg_dump --format=custom` of this database from the last 24 hours.",
)
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
def db_retire(family_names: tuple[str, ...], apply_: bool, backup: str | None, yes: bool) -> None:
    """Report, or retire, families of legacy integration tables.

    Without --apply this is read-only: for every family (or each FAMILY), each
    table's rows, the source files that still name it and the foreign keys
    that reach it from outside the named families.  A family is ready when
    nothing names or references any of its tables.

    With --apply, at least one FAMILY and --backup are required.  In one
    transaction the command locks the tables, re-checks readiness, copies every
    row into integration_retired_rows, records a receipt whose count and digest
    the archive reproduces, then drops the tables.  Any blocker refuses the
    whole set and changes nothing.  Families that reference each other are
    named together.  Readiness reads this checkout's source, so restart the
    daemon onto the code that removed the family first.
    """
    from src.integration import table_retirement as retirement

    try:
        families = [retirement.family(name) for name in dict.fromkeys(family_names)]
    except KeyError as exc:
        names = ", ".join(f.name for f in retirement.FAMILIES)
        raise click.BadParameter(
            f"unknown family {exc.args[0]!r}; one of {names}", param_hint="FAMILY"
        ) from None
    named = bool(families)
    families = families or list(retirement.FAMILIES)

    if apply_:
        if not named or backup is None:
            raise click.UsageError("--apply needs FAMILY and --backup")
        _refuse_worker("retire --apply")
        try:
            evidence = retirement.verify_backup(backup)
        except retirement.RetirementRefused as exc:
            console.print(f"[red]Refused:[/] {exc}")
            raise SystemExit(1) from None

    async def _report() -> int:
        from rich.table import Table

        engine, url = _make_engine(_load_config())
        try:
            async with engine.connect() as conn:
                alongside = families if named else ()
                reports = [
                    await retirement.family_readiness(conn, f, alongside=alongside)
                    for f in families
                ]
        finally:
            await engine.dispose()
        console.print(f"database: [cyan]{_display_url(url)}[/]")
        table = Table("family", "table", "state", "rows", "source files", "outside FKs")
        blocked = 0
        for report in reports:
            for row in report:
                state = "retired" if row.retired else ("present" if row.present else "absent")
                blocked += bool(row.blockers)
                table.add_row(
                    row.family,
                    row.table,
                    state,
                    str(row.rows),
                    str(len(row.code_references)),
                    ", ".join(row.referenced_by) or "-",
                )
        console.print(table)
        console.print(
            f"[yellow]{blocked} table(s) still in use[/]" if blocked else "[green]ready[/]"
        )
        return 1 if blocked else 0

    async def _apply() -> int:
        engine, _url = _make_engine(_load_config())
        try:
            async with engine.begin() as conn:
                receipts = await retirement.retire_families(
                    conn, families, backup=evidence, retired_by=_operator()
                )
        except retirement.RetirementRefused as exc:
            console.print("[red]Refused; nothing changed:[/]")
            for reason in exc.reasons:
                console.print(f"  - {reason}")
            return 1
        finally:
            await engine.dispose()
        for receipt in receipts:
            console.print(
                f"retired [cyan]{receipt['table_name']}[/]: {receipt['row_count']} row(s) "
                f"archived, {receipt['rows_digest']}"
            )
        if not receipts:
            console.print("nothing to retire: every named table is already gone")
        return 0

    if not apply_:
        raise SystemExit(asyncio.run(_report()))
    tables = [table for retiring in families for table in retiring.tables]
    console.print(
        f"About to archive and drop {len(tables)} table(s) of "
        f"[cyan]{', '.join(f.name for f in families)}[/] in "
        f"[cyan]{_display_url(_load_config().database.url)}[/], backup {evidence.path}."
    )
    if not yes and not click.confirm("Continue?", default=False):
        raise SystemExit(1)
    raise SystemExit(asyncio.run(_apply()))


@db_group.command("restore-retired")
@click.argument("table_name", metavar="TABLE")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
def db_restore_retired(table_name: str, yes: bool) -> None:
    """Replay a retired table from its archive and check its receipt (operator only).

    An absent table is recreated with its recorded columns only; a present one
    must be empty.  The restored rows must reproduce the receipt's count and
    digest or nothing is written.
    """
    from src.integration import table_retirement as retirement

    _refuse_worker("restore-retired")
    if not yes and not click.confirm(f"Restore {table_name} from its archive?", default=False):
        raise SystemExit(1)

    async def _main() -> int:
        engine, _url = _make_engine(_load_config())
        try:
            async with engine.begin() as conn:
                result = await retirement.restore_retired_table(conn, table_name)
        except retirement.RetirementRefused as exc:
            console.print(f"[red]Refused; nothing changed:[/] {exc}")
            return 1
        finally:
            await engine.dispose()
        console.print(
            f"restored [cyan]{result['table']}[/]: {result['rows']} row(s), "
            f"{result['rows_digest']} matches its receipt"
        )
        return 0

    raise SystemExit(asyncio.run(_main()))


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
