"""Whole-database custom archives and read-only restore preflight.

Clients run asynchronously. Docker is only a client transport to an already
running container publishing the configured PostgreSQL endpoint; this module
never starts containers or changes their volumes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url

from src.install.update import BACKUP_TIMEOUT


class BackupError(RuntimeError):
    """An archive or restore precondition failed, with an operator-facing reason."""


async def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    source: Path | None = None,
    destination: Path | None = None,
    timeout: float = BACKUP_TIMEOUT,
) -> bytes:
    with ExitStack() as stack:
        stdin = stack.enter_context(source.open("rb")) if source else asyncio.subprocess.DEVNULL
        stdout = (
            stack.enter_context(destination.open("wb")) if destination else asyncio.subprocess.PIPE
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=stdin, stdout=stdout, stderr=asyncio.subprocess.PIPE, env=env
            )
        except OSError as exc:
            raise BackupError(f"backup_client_unavailable: {exc}") from exc
        try:
            output, error = await asyncio.wait_for(proc.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
    if proc.returncode:
        detail = error.decode("utf-8", "replace").strip()
        if env and env.get("PGPASSWORD"):
            detail = detail.replace(env["PGPASSWORD"], "***")
        raise BackupError(f"backup_client_failed: {detail}")
    return output or b""


@dataclass
class PostgresClients:
    """Matched pg_dump, pg_restore and psql clients, local or on the serving container."""

    prefix: tuple[str, ...]
    dump: str
    restore: str
    connection: tuple[str, ...]
    env: dict[str, str]

    async def run(self, tool: str, *args: str, **kwargs) -> bytes:
        executable = {
            "pg_dump": self.dump,
            "pg_restore": self.restore,
            "psql": str(Path(self.restore).with_name("psql")),
        }[tool]
        return await _run([*self.prefix, executable, *args], env=self.env, **kwargs)


async def postgres_clients(database_url: str) -> PostgresClients:
    from src.database.migration_guard import WORKER, current_scope, is_production_database
    from src.install.update import find_pg_dump

    if current_scope() == WORKER and is_production_database(database_url):
        raise BackupError(
            "restore_operator_only: workers may not administer the production database"
        )
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise BackupError("backup_database_unsupported: PostgreSQL is required")
    env = dict(os.environ)
    # pg_restore renders the archive's epoch in its own timezone and locale.
    env.update(TZ="UTC", LC_ALL="C")
    env.pop("PGPASSWORD", None)
    if url.password is not None:
        env["PGPASSWORD"] = str(url.password)
    # Preserve libpq connection options (e.g. sslmode), without credentials in argv.
    env["PGDATABASE"] = URL.create(
        "postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)
    dump = find_pg_dump()
    restore = str(Path(dump).with_name("pg_restore")) if dump else None
    local = (
        PostgresClients((), dump, restore, ("--dbname", env["PGDATABASE"]), env)
        if dump
        and restore
        and Path(restore).is_file()
        and Path(restore).with_name("psql").is_file()
        else None
    )

    if url.host not in {None, "localhost", "127.0.0.1", "::1"} or not shutil.which("docker"):
        if local:
            return local
        raise BackupError("backup_client_unavailable: install matching PostgreSQL client tools")
    port = str(url.port or 5432)
    try:
        containers = await _run(
            ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.ID}}"], timeout=10
        )
        matches = containers.decode().splitlines()
        if len(matches) != 1:
            raise BackupError(
                "backup_client_unavailable: no unique running PostgreSQL container serves "
                f"the configured port {port}; install matching PostgreSQL client tools"
            )
        ports = json.loads(
            await _run(
                ["docker", "inspect", matches[0], "--format", "{{json .NetworkSettings.Ports}}"],
                timeout=10,
            )
        )
        if not any(p["HostPort"] == port for p in (ports.get("5432/tcp") or [])):
            raise BackupError("backup_client_unavailable: container does not publish PostgreSQL")
    except (BackupError, TimeoutError):
        if local:
            return local
        raise
    # docker forwards the environment values; no password is an argv element.
    prefix = ("docker", "exec", "-i", "-e", "PGPASSWORD", "-e", "TZ", "-e", "LC_ALL", matches[0])
    connection = (
        "--host",
        "localhost",
        "--port",
        "5432",
        "--username",
        url.username or "",
        "--dbname",
        url.database or "",
    )
    serving = PostgresClients(prefix, "pg_dump", "pg_restore", connection, env)
    if local:
        # A distro client can lag the serving container (e.g. PG16 vs PG18).
        # Select a compatible pair without connecting to the restore target.
        local_version = await local.run("pg_dump", "--version")
        serving_version = await serving.run("pg_dump", "--version")
        local_major = re.search(rb"PostgreSQL\) (\d+)", local_version)
        serving_major = re.search(rb"PostgreSQL\) (\d+)", serving_version)
        if local_major and serving_major and int(local_major[1]) >= int(serving_major[1]):
            return local
    return serving


@dataclass(frozen=True)
class ArchiveInfo:
    created_at: datetime
    revisions: tuple[str, ...]

    @property
    def timestamp(self) -> str:
        return self.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_custom_archive(path: Path) -> None:
    with path.open("rb") as handle:
        if handle.read(5) != b"PGDMP":
            raise BackupError(
                "restore_format_unsupported: expected a pg_dump custom archive (.dump); "
                "legacy plain SQL requires a manual psql restore"
            )


async def inspect_archive(clients: PostgresClients, path: Path) -> ArchiveInfo:
    assert_custom_archive(path)
    listing = (await clients.run("pg_restore", "--list", source=path)).decode()
    match = re.search(r"^; Archive created at (.+)$", listing, re.MULTILINE)
    if not match:
        raise BackupError("restore_archive_invalid: archive creation timestamp is missing")
    try:
        created_at = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=UTC)
    except ValueError as exc:
        raise BackupError("restore_archive_invalid: unreadable archive timestamp") from exc
    # Extract only the archived stamp as SQL text, never execute it in the target.
    sql = (
        await clients.run(
            "pg_restore", "--data-only", "--table=alembic_version", "--file=-", source=path
        )
    ).decode()
    copies = re.findall(
        r"^COPY (?:public\.)?alembic_version \(version_num\) FROM stdin;\n(.*?)\n\\\.",
        sql,
        re.MULTILINE | re.DOTALL,
    )
    revisions = [line for block in copies for line in block.splitlines() if line]
    # Custom archives made with pg_dump --inserts are also custom format.
    revisions.extend(
        re.findall(
            r"^INSERT INTO (?:public\.)?alembic_version(?: \(version_num\))? VALUES \('([^']+)'\);$",
            sql,
            re.MULTILINE,
        )
    )
    if any(not re.fullmatch(r"[a-zA-Z0-9_]+", revision) for revision in revisions):
        raise BackupError("restore_schema_invalid: unreadable archived Alembic stamp")
    return ArchiveInfo(created_at, tuple(sorted(set(revisions))))


def validate_revisions(info: ArchiveInfo) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from src.database.engine import _ALEMBIC_INI

    script = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI)))
    known = {revision.revision for revision in script.walk_revisions()}
    if not info.revisions or not set(info.revisions) <= known:
        raise BackupError(
            "restore_schema_mismatch: dump revision(s) "
            f"{', '.join(info.revisions) or 'unstamped'} are not in this checkout's "
            f"migration history (head: {', '.join(script.get_heads())})"
        )

    # An ancestor and its descendant cannot both be current Alembic heads.
    for revision in info.revisions:
        ancestors = {r.revision for r in script.iterate_revisions(revision, "base")} - {revision}
        if ancestors.intersection(info.revisions):
            raise BackupError("restore_schema_invalid: archived stamps are not independent heads")


async def backup_database(clients: PostgresClients, destination: Path) -> ArchiveInfo:
    """Write one private custom archive, refusing to overwrite an existing file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        await clients.run(
            "pg_dump",
            *clients.connection,
            "--format=custom",
            "--no-owner",
            "--no-acl",
            destination=destination,
        )
        return await inspect_archive(clients, destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


async def restore_database(clients: PostgresClients, source: Path) -> None:
    """Replace public, including objects absent from the older archive, atomically.

    pg_restore --clean only removes archived objects. Render the archive first
    and execute its SQL after schema replacement in the same psql transaction;
    an archive or SQL error leaves the target untouched. SQL stays on disk with
    private permissions rather than buffering a whole database in memory.
    """
    with tempfile.TemporaryDirectory(prefix="aq-restore-") as directory:
        sql = Path(directory) / "archive.sql"
        await clients.run(
            "pg_restore", "--no-owner", "--no-acl", "--file=-", source=source, destination=sql
        )
        script = Path(directory) / "restore.sql"

        def prepare() -> None:
            with script.open("wb") as target, sql.open("rb") as archive:
                target.write(b"DROP SCHEMA IF EXISTS public CASCADE;\nCREATE SCHEMA public;\n")
                shutil.copyfileobj(archive, target)

        await asyncio.to_thread(prepare)
        await clients.run(
            "psql",
            *clients.connection,
            "--no-psqlrc",
            "--quiet",
            "--single-transaction",
            "--set=ON_ERROR_STOP=1",
            "--file=-",
            source=script,
        )


async def assert_sessions_ended(config, engine) -> None:
    """Stored status alone is insufficient: sessions survive daemon restarts."""
    socket = config.sessions.tmux_socket or "aq"
    tmux_verified = False
    if shutil.which("tmux"):
        try:
            names = (
                (
                    await _run(
                        ["tmux", "-L", socket, "list-sessions", "-F", "#{session_name}"], timeout=10
                    )
                )
                .decode()
                .splitlines()
            )
        except BackupError as exc:
            detail = str(exc)
            if detail.startswith("backup_client_unavailable:") or not any(
                s in detail for s in ("no server running", "No such file or directory")
            ):
                raise
            names = []
        live = [name for name in names if name.startswith(("s-", "n-", "p-"))]
        if live:
            raise BackupError(
                f"restore_sessions_live: end every agent session first: {', '.join(live)}"
            )
        tmux_verified = True
    async with engine.connect() as conn:
        exists = await conn.scalar(text("SELECT to_regclass('public.sessions')"))
        if exists:
            live = (
                (
                    await conn.execute(
                        text(
                            "SELECT name FROM sessions WHERE ended_at IS NULL AND state <> 'stopped'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            if live and not tmux_verified:
                raise BackupError(
                    f"restore_sessions_unverified: sessions have not ended: {', '.join(live)}; "
                    "install tmux and verify the configured socket after aq stop"
                )
            # aq stop kills tmux after the daemon exits, so its records can
            # retain ended_at=NULL. Every agent is a tmux session; a successful
            # socket probe proving no live agents is sufficient without editing
            # those historical rows or restarting a daemon before recovery.


# The names in the design map to these persisted tables. Include changes to
# existing tasks/waits as well as newly created rows when timestamps permit it.
_LOSS_TABLES = {
    "tasks": ("created_at", "updated_at"),
    "events": ("timestamp",),
    "agent_waits": ("created_at", "resolved_at"),
    "gates": ("created_at",),
    "operator_decisions": ("created_at",),
    "messages": ("created_at",),
}


async def loss_bound(engine, created_at: datetime) -> dict[str, int]:
    counts = {}
    async with engine.connect() as conn:
        for table, candidates in _LOSS_TABLES.items():
            columns = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT column_name, data_type FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :table"
                        ),
                        {"table": table},
                    )
                ).all()
            )
            selected = [name for name in candidates if name in columns]
            if not selected:
                continue
            params = {}
            clauses = []
            for name in selected:
                params[name] = (
                    created_at if columns[name].startswith("timestamp") else created_at.timestamp()
                )
                clauses.append(f'"{name}" >= :{name}')
            counts[table] = await conn.scalar(
                text(f'SELECT count(*) FROM "{table}" WHERE ' + " OR ".join(clauses)), params
            )
    return counts
