"""Owner-scoped cancellation, sharing outcome dispatch and graph cleanup."""

from sqlalchemy import select

from src.database.queries.wait_queries import verify_owner
from src.database.tables import tasks, wait_edges, wait_records
from src.waits.conditions import Scope, WaitError
from src.waits.explain import owner_key
from src.waits.graph import lock_component_tasks, lock_graph
from src.waits.ownership import fence_owner


async def cancel(service, args, *, principal, scope):
    now = service.lifecycle.clock()
    async with service.orch.db._engine.begin() as conn:
        await lock_graph(conn)
        candidate = (await conn.execute(select(wait_records).where(
            (wait_records.c.id == args["wait_id"])
            | (wait_records.c.legacy_id == args["wait_id"]),
        ))).mappings().first()
        if candidate is None:
            raise WaitError("wait.source_unresolved", "wait not found")
        if isinstance(scope, Scope):
            scope = await verify_owner(conn, scope, lock_owner=False)
            if any(candidate[key] != getattr(scope, key) for key in (
                "project_id", "owner_kind", "owner_id",
            )):
                raise WaitError("wait.out_of_scope", "wait belongs to another owner")
        else:
            if scope.get("project_id") not in {None, candidate["project_id"]}:
                raise WaitError("wait.out_of_scope", "wait belongs to another project")
            await service.decisions.authorize(conn, principal, candidate["project_id"])
        endpoints = (await conn.execute(select(
            wait_edges.c.waiter_key, wait_edges.c.blocker_key,
        ).where(wait_edges.c.wait_id == candidate["id"],
                wait_edges.c.active.is_(True)))).all()
        keys = {owner_key(candidate), *(key for edge in endpoints for key in edge)}
        await lock_component_tasks(conn, keys)
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == candidate["owner_id"],
        ).with_for_update())).mappings().first() if candidate["owner_kind"] == "task" else None
        wait = dict((await conn.execute(select(wait_records).where(
            wait_records.c.id == candidate["id"],
        ).with_for_update())).mappings().one())
        if isinstance(scope, Scope) and scope.principal != "service":
            if (wait["session_id"] != scope.session_id
                    or wait["claim_epoch"] != scope.claim_epoch):
                raise WaitError("stale_claim", "wait holder changed")
        if wait["outcome"] != "pending":
            return {"success": True, "wait_id": wait["id"], "changed": False}
        _, continuation = await fence_owner(conn, wait, task, parked_required=True)
        if continuation:
            await service.decisions.hold(conn, wait, task, continuation, now)
        await service.dispatcher.finish(
            conn, wait, outcome="cancelled", result_ref=f"wait:{wait['id']}:cancelled",
            event={}, task=task, continuation=continuation, changed_keys=keys,
            deliver=continuation is None,
        )
        return {"success": True, "wait_id": wait["id"], "changed": True}
