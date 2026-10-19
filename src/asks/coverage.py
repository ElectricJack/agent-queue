"""Accepted external prerequisites over existing review waiters, on the source transaction."""

from sqlalchemy import select, text

from src.asks.models import AskError
from src.commands import task_changes
from src.database.tables import task_gates, tasks


async def cover_review_waiters(db, decision, obligations, *, conn):
    required = [o for o in obligations if o["required_before_release"]
                and o["disposition"] == "follow_up"]
    if not required:
        return []
    domain = decision["packet"]["domain"]
    project_id = domain["review"]["project_id"]
    await db.lock_hierarchy_project(conn, project_id)
    await conn.execute(text("LOCK TABLE tasks, task_dependencies IN SHARE ROW EXCLUSIVE MODE"))
    waiters = set((await conn.execute(select(task_gates.c.task_id).where(
        task_gates.c.gate_id == domain["review"]["gate_id"],
    ))).scalars())
    edges = []
    frozen = {item["feedback_key"]: item for item in domain["feedback"]}
    for item in required:
        choice = frozen[item["feedback_key"]]["accepted_by"]["choice"]
        if not choice.get("external_prerequisite"):
            raise AskError("ask.application_unknown", "Document prerequisite needs a returned revision")
        target = item["task_id"]
        task = (await conn.execute(select(tasks).where(tasks.c.id == target))).mappings().first()
        if (not task or task["project_id"] != project_id or task["status"] == "FAILED"
                or await db.is_container(target, conn=conn)):
            raise AskError("ask.application_unknown", "Required follow-up is missing or unrepresentable")
        selected = choice.get("coverage", {}).get("waiter_ids")
        if selected is not None and not set(selected) <= waiters:
            raise AskError("ask.stale_source", "Accepted waiter coverage no longer matches")
        for waiter in sorted(waiters if selected is None else selected):
            if waiter == target:
                continue
            if await db.is_container(waiter, conn=conn):
                raise AskError("ask.application_unknown", "Container waiter coverage needs fold-in")
            edge = {"from": waiter, "to": target, "dep_type": "blocks"}
            if edge not in edges:
                edges.append(edge)
    if edges:
        payload = task_changes.normalize({"edges": edges})
        expected, rows, _ = await task_changes.snapshot(conn, project_id, payload)
        task_changes.final_graph(payload, rows, expected)
        for edge in edges:
            await db.add_dependency(edge["from"], edge["to"], conn=conn,
                                    description=f"Accepted prerequisite from {decision['id']}")
    return edges
