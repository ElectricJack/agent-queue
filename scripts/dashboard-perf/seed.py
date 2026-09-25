#!/usr/bin/env python3
"""Seed the experiment's task fixture into an isolated database (spec §4.2).

Writes ``--tasks`` tasks (default 10,000) into one project of the database
named by ``--dsn`` — the isolated daemon's database or a test server's,
**never the operator's**.  Before connecting it compares endpoints with the
operator daemon's ``database.url`` (``~/.agent-queue/config.yaml``, or
``--refuse-dsn``) and refuses when host, port and database all match; a
config whose URL it cannot resolve is a refusal too, not permission.

The project is created PAUSED so the isolated daemon never dispatches the
fixture.  Unblocked DEFINED tasks are still promoted to READY by the
daemon's cascade once after seeding, so seed before the warm-up (or pass
``--status READY``).  Re-running is safe: task ids are deterministic and
existing ones are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_TASKS = 10_000
BATCH = 500


def _sibling(name: str):
    """A script beside this one, loaded by path (``scripts/`` is not a package)."""
    module_name = f"aqperf_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


pg_identity = _sibling("pg_identity")


class RefusedOperatorDatabase(RuntimeError):
    """The target is the daemon's own database (or cannot be told apart from it)."""


def _describe(ident: dict) -> str:
    return f"{ident['scheme']}://{ident['host']}:{ident['port']}/{ident['database'] or ''}"


def refuse_if_operator(dsn: str, operator_dsn: str | None) -> None:
    """Raise ``RefusedOperatorDatabase`` when ``dsn`` is the operator's database.

    Endpoints are compared, not spellings: ``localhost`` and ``127.0.0.1``
    are one host and an omitted port is 5432.  A different database on the
    same server is allowed (that is what an isolated daemon usually is).
    Messages carry endpoints only, never credentials.
    """
    result = pg_identity.compare(dsn, operator_dsn)
    if result["reason"] == "invalid_test_dsn":
        raise ValueError("the target is not a PostgreSQL DSN")
    if result["reason"] == "invalid_daemon_dsn":
        raise RefusedOperatorDatabase(
            "the daemon's database URL could not be parsed, so the target cannot be proved "
            "different from it")
    if result["same_database"]:
        raise RefusedOperatorDatabase(f"{_describe(result['test'])} is the daemon's database")


def _task_id(project_id: str, index: int) -> str:
    return f"{project_id}-seed-{index:06d}"


async def seed(dsn: str, project_id: str, count: int, *, status: str = "DEFINED") -> dict:
    """Create ``project_id`` (PAUSED) if missing and ``count`` tasks in it; ``created`` counts new rows.

    Callers refuse the operator's database first (``main`` does); this
    function trusts its ``dsn``.
    """
    from sqlalchemy import select

    from src.database import Database
    from src.database.tables import tasks
    from src.models import Project, ProjectStatus, Task, TaskStatus

    task_status = TaskStatus(status)
    db = Database(dsn)
    await db.initialize()
    created = 0
    try:
        if await db.get_project(project_id) is None:
            await db.create_project(
                Project(id=project_id, name=project_id, status=ProjectStatus.PAUSED))
        for start in range(0, count, BATCH):
            ids = [_task_id(project_id, index) for index in range(start, min(count, start + BATCH))]
            async with db._engine.begin() as conn:
                existing = set((await conn.execute(
                    select(tasks.c.id).where(tasks.c.id.in_(ids)))).scalars())
                for task_id in ids:
                    if task_id in existing:
                        continue
                    index = int(task_id.rsplit("-", 1)[1])
                    await db.create_task(Task(
                        id=task_id, project_id=project_id, title=f"seed-{index:06d}",
                        description=f"Dashboard performance fixture task {index}.",
                        status=task_status,
                    ), conn=conn)
                    created += 1
    finally:
        await db.close()
    return {"project_id": project_id, "created": created}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True, help="the isolated database to seed")
    parser.add_argument("--project", default="perf-fixture")
    parser.add_argument("--tasks", type=int, default=DEFAULT_TASKS)
    parser.add_argument("--status", default="DEFINED", choices=("DEFINED", "READY"))
    parser.add_argument("--refuse-dsn", default=None,
                        help="the daemon DSN to refuse (default: database.url from --operator-config)")
    parser.add_argument("--operator-config", type=Path, default=pg_identity.DEFAULT_CONFIG,
                        help="the operator daemon's config file (default %(default)s)")
    args = parser.parse_args(argv)
    refuse = args.refuse_dsn
    if refuse is None:
        refuse, reason = pg_identity.daemon_dsn_from_config(args.operator_config)
        if refuse is None and reason != "no_config":
            print(f"refusing to seed: the operator daemon's database is unknown ({reason}); "
                  "pass --refuse-dsn with its URL", file=sys.stderr)
            return 2
    try:
        refuse_if_operator(args.dsn, refuse)
    except (RefusedOperatorDatabase, ValueError) as exc:
        print(f"refusing to seed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asyncio.run(seed(args.dsn, args.project, args.tasks, status=args.status))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
