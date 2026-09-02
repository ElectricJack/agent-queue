"""Seed a project shaped for layout perf tests (spec §9): 100 epics with
nested packages, one 1,000-task epic, one hub with 50 dependents."""

from __future__ import annotations

import asyncio
import sys

from src.models import Project, Task, TaskStatus


async def seed_project(db, project_id: str, *, epics: int = 100, per_epic: int = 40,
                       big_epic: int = 1000, hub_dependents: int = 50) -> None:
    await db.create_project(Project(id=project_id, name=project_id))

    async def make(tid: str, parent: str | None, status=TaskStatus.DEFINED):
        await db.create_task(Task(id=tid, project_id=project_id, title=tid, description="", status=status))
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)

    for e in range(epics):
        eid = f"epic{e}"
        await make(eid, None)
        n = big_epic if e == 0 else per_epic
        for p in range(max(1, n // 10)):
            pid = f"{eid}-pkg{p}"
            await make(pid, eid)
            for t in range(10 if n >= 10 else n):
                tid = f"{pid}-t{t}"
                await make(tid, pid, TaskStatus.COMPLETED if (t % 2 == 0 and e > 0) else TaskStatus.DEFINED)
                if t > 0:
                    await db.add_dependency(tid, f"{pid}-t{t-1}")
    await make("hub", None)
    for i in range(hub_dependents):
        await make(f"hubdep{i}", None)
        await db.add_dependency(f"hubdep{i}", "hub")


if __name__ == "__main__":

    async def main():
        dsn = sys.argv[1]
        if dsn.startswith("postgresql"):
            from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

            db = PostgreSQLDatabaseAdapter(dsn)
        else:
            from src.database import Database

            db = Database(dsn)
        await db.initialize()
        await seed_project(db, sys.argv[2] if len(sys.argv) > 2 else "perf")
        await db.close()

    asyncio.run(main())
