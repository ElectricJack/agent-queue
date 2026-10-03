"""Which project owns an integration operation.

A repair operation names its target indirectly — ``target_kind`` decides
whether the owner is the parent task or the batch — so every caller that has
to answer "is this operation mine?" reads the same two-hop join.  Both call
sites live here rather than in each caller: the command handler authorising an
operator control (``_integration_operator_for_operation``) and the scope layer
resolving a per-project supervisor's target
(:mod:`src.api.target_scope`).

``None`` means *no project could be resolved*: the operation is gone, its
target row is gone, or its ``target_kind`` is neither ``parent`` nor ``batch``.
Callers must fail closed on it.
"""

from __future__ import annotations


async def operation_project_id(db, operation: dict | None) -> str | None:
    """Return the project that owns *operation*, else ``None``."""
    if not operation:
        return None
    if operation["target_kind"] == "parent":
        from src.database.queries.task_identity import resolve_task_identity_on

        async with db._engine.connect() as conn:
            identity = await resolve_task_identity_on(
                conn, operation.get("parent_task_id") or ""
            )
        return identity.project_id if identity is not None else None
    if operation["target_kind"] == "batch":
        batch = await db.get_integration_batch(operation.get("batch_id") or "")
        return str(batch["project_id"]) if batch is not None else None
    return None


async def operation_project_id_for(db, operation_id: str) -> str | None:
    """Resolve *operation_id* and return the project that owns it."""
    return await operation_project_id(db, await db.get_integration_operation(operation_id))