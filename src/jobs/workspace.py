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
