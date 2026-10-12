#!/usr/bin/env python3
"""Exercise previous -> current -> previous schema startup on an empty scratch DB.

No daemon is launched and no Alembic downgrade is run. Release trees are exported
from pinned commits; each probe imports that tree's database startup code.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_additive_migrations import git, inspect_range  # noqa: E402
from src.config import is_postgres_url  # noqa: E402
from src.database.migration_guard import assert_not_production_database  # noqa: E402

_PROBE = """
import asyncio, json, os
from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
from src.database.migration_guard import SchemaAheadPolicy
async def main():
    database = PostgreSQLDatabaseAdapter(
        os.environ['AQ_ROLLBACK_DRILL_DSN'], schema_ahead_policy=SchemaAheadPolicy(
            os.environ.get('AQ_ROLLBACK_DRILL_MIGRATIONS', ''),
            json.loads(os.environ['AQ_ROLLBACK_DRILL_MAX_REVISIONS']),
        ),
    )
    try:
        await database.initialize()
    finally:
        await database.close()
asyncio.run(main())
"""


async def snapshot(connection) -> dict[str, dict]:
    """Hash every public table's rows in a stable order, including Alembic state."""
    result = {}
    async with connection.transaction(isolation="repeatable_read", readonly=True):
        tables = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
        for table in tables:
            name = table["tablename"]
            quoted = '"' + name.replace('"', '""') + '"'
            rows = sorted(
                row[0]
                for row in await connection.fetch(
                    f"SELECT row_to_json(t)::text FROM public.{quoted} t"
                )
            )
            digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
            result[name] = {"rows": len(rows), "sha256": digest}
    return result


async def probe(checkout: Path, dsn: str, evidence: Path | None, maximum: int | None) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(checkout),
            "AQ_ROLLBACK_DRILL_DSN": dsn,
            "AQ_ROLLBACK_DRILL_MIGRATIONS": str(evidence) if evidence else "",
            "AQ_ROLLBACK_DRILL_MAX_REVISIONS": json.dumps(maximum),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PROBE,
        cwd=checkout,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        # Never print the DSN or a child exception containing its credentials.
        from src.database import redact_dsn

        detail = redact_dsn((stdout + stderr).decode()).replace(dsn, "<scratch database>")
        raise ValueError(f"schema-start probe failed for {checkout.name}: {detail}")


async def drill(
    repository: Path,
    previous: str,
    current: str,
    dsn: str,
    *,
    seed_sql: Path | None = None,
    max_revisions: int | None = None,
) -> dict:
    import asyncpg

    if not is_postgres_url(dsn):
        raise ValueError("rollback drill requires an explicit scratch PostgreSQL URL")
    assert_not_production_database(dsn, actor="rollback drill")
    report = await inspect_range(repository, previous, current)
    if not report["success"]:
        raise ValueError("rollback unsafe: " + "; ".join(report["findings"]))
    connection = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        if await snapshot(connection):
            raise ValueError("rollback drill requires an empty scratch database")
        with tempfile.TemporaryDirectory(prefix="aq-rollback-drill-") as temporary:
            checkouts = []
            for label, oid in (("previous", report["previous"]), ("current", report["current"])):
                destination = Path(temporary) / label
                destination.mkdir()
                archive = await git(repository, "archive", "--format=tar", oid)
                with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                    bundle.extractall(destination, filter="data")
                checkouts.append(destination)
            older, newer = checkouts
            await probe(older, dsn, None, max_revisions)
            await probe(newer, dsn, None, max_revisions)
            if seed_sql:
                await connection.execute(seed_sql.read_text(encoding="utf-8"))
            before = await snapshot(connection)
            await probe(older, dsn, newer / "migrations" / "versions", max_revisions)
            after = await snapshot(connection)
            if before != after:
                raise ValueError("rollback changed database contents or Alembic state")
            return {
                "success": True,
                "previous": report["previous"],
                "current": report["current"],
                "tables": after,
                "database_preserved": True,
            }
    finally:
        await connection.close()


def main() -> int:
    import asyncpg

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--previous-tag", required=True)
    parser.add_argument("--current-tag", required=True)
    parser.add_argument("--database-url", default=os.environ.get("AQ_ROLLBACK_DRILL_DSN"))
    parser.add_argument(
        "--seed-sql", type=Path, help="scratch-only seed rows after current upgrade"
    )
    parser.add_argument("--max-revisions", type=int, default=None)
    args = parser.parse_args()
    try:
        report = asyncio.run(
            drill(
                args.repository,
                args.previous_tag,
                args.current_tag,
                args.database_url or "",
                seed_sql=args.seed_sql,
                max_revisions=args.max_revisions,
            )
        )
    except (ValueError, OSError, RuntimeError, asyncpg.PostgresError) as exc:
        from src.database import redact_dsn

        print(json.dumps({"success": False, "error": redact_dsn(str(exc))}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
