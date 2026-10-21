"""Serialize workspace mutation with pin creation on the workspace row."""

from __future__ import annotations
from contextlib import asynccontextmanager
from functools import wraps
from inspect import signature
from sqlalchemy import select, func
from contextvars import ContextVar
from src.database.tables import workspaces, job_workspace_pins
from src.jobs.policy import JobError


_held = ContextVar("job_workspace_guards", default=frozenset())


@asynccontextmanager
async def mutation_guard(db, workspace):
    # Lightweight fake DBs used by unrelated area tests have no pin storage.
    # Concrete adapters participate even with admission off: existing pins
    # must survive rollback of the rollout flag.
    if not hasattr(type(db), "workspace_has_job_pin") or workspace is None:
        yield
        return
    async with db._engine.begin() as conn:
        if hasattr(workspace, "workspace_path"):
            predicate = workspaces.c.id == workspace.id
        else:
            predicate = workspaces.c.workspace_path == str(workspace)
        rows = (
            (await conn.execute(select(workspaces.c.id).where(predicate).order_by(workspaces.c.id)))
            .scalars()
            .all()
        )
        already = _held.get()
        for workspace_id in rows:
            if workspace_id not in already:
                await conn.execute(
                    select(func.pg_advisory_xact_lock(109795, func.hashtext(workspace_id)))
                )
        if rows and await conn.scalar(
            select(job_workspace_pins.c.job_id)
            .where(job_workspace_pins.c.workspace_id.in_(rows))
            .limit(1)
        ):
            raise JobError("jobs.workspace_busy")
        token = _held.set(already | frozenset(rows))
        try:
            yield
        finally:
            _held.reset(token)


def guard_workspace(argument):
    def decorate(function):
        sig = signature(function)

        @wraps(function)
        async def guarded(self, *args, **kwargs):
            bound = sig.bind(self, *args, **kwargs)
            workspace = bound.arguments.get(argument)
            if argument == "attachment":
                workspace = workspace.workspace
            async with mutation_guard(self.db, workspace):
                return await function(self, *args, **kwargs)

        return guarded

    return decorate


@asynccontextmanager
async def candidate_workspace(db, config, args, digest):
    """Reserve a warm snapshot through submission; jobs own the durable pin.

    Keep an attempt's ignored installs/builds until its batch is terminal.
    Ordinary worker slots and unknown/legacy operations are never reclaimed.
    """
    import time
    from pathlib import Path
    from sqlalchemy.dialects.postgresql import insert
    from src.database.tables import integration_batches, jobs, sessions
    from src.git.manager import GitManager
    from src.jobs.policy import TERMINAL
    from src.warm_workspaces import WarmWorkspacePool, primary_store

    git = GitManager()
    store = await primary_store(
        db, git, args["project_id"], args.get("repository_id"), args["store"], args["input_ref"],
    )
    pool = WarmWorkspacePool(git, store, Path(config.data_dir) / "job-snapshots")
    owner = {"project_id": args["project_id"], "operation_id": args["operation_id"],
             "attempt": args.get("snapshot_group") or digest, "oid": args["input_ref"],
             "batch_id": args.get("workspace_batch_id") or args["operation_id"]}
    # This lock covers reservation, filesystem reset, row creation and job pin
    # creation. A competing submission cannot slip between those steps.
    async with db._engine.begin() as fence:
        await fence.execute(select(func.pg_advisory_xact_lock(109797, func.hashtext(pool.key))))

        async def reserved(path):
            rows = (await fence.execute(select(workspaces).where(
                workspaces.c.workspace_path == str(path),
            ))).mappings().all()
            if any(row["enabled"] or row["locked_by_task_id"] or row["locked_by_agent_id"]
                   or row["kind_id"] != "job-snapshot" for row in rows):
                return True
            return bool(await fence.scalar(select(sessions.c.id).where(
                sessions.c.work_dir == str(path), sessions.c.state != "stopped",
            ).limit(1)))

        async def released(manifest, path, head):
            previous = manifest["owner"]
            workspace_id = "job-snapshot-" + path.name
            if (head != manifest["oid"] or previous.get("project_id") != args["project_id"]
                    or await reserved(path)):
                return False
            if await db.workspace_has_job_pin(workspace_id):
                return False
            batch = (await fence.execute(select(integration_batches).where(
                integration_batches.c.id == previous.get("batch_id"),
                integration_batches.c.project_id == args["project_id"],
            ))).mappings().first()
            if not batch or batch["lifecycle"] not in {"promoted", "aborted", "failed", "empty"}:
                return False
            busy = await fence.scalar(select(jobs.c.id).where(
                jobs.c.workspace_id == workspace_id,
                jobs.c.state.not_in(TERMINAL) | jobs.c.cleanup_blocked.is_(True),
            ).limit(1))
            return busy is None

        path = await pool.acquire(owner, args["input_ref"], released=released)
        workspace_id = "job-snapshot-" + path.name
        if await reserved(path) or await db.workspace_has_job_pin(workspace_id):
            raise JobError("jobs.workspace_busy")
        async with db._engine.begin() as conn:
            await conn.execute(insert(workspaces).values(
                id=workspace_id, project_id=args["project_id"], workspace_path=str(path),
                source_type="worktree", kind_id="job-snapshot", enabled=False,
                created_at=time.time(),
            ).on_conflict_do_nothing(index_elements=[workspaces.c.id]))
        yield workspace_id
