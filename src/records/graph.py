"""Read-only edges for the separate record graph, never task graph inputs.

Each read returns at most 100 edges from each domain. Unreadable endpoints are
omitted, including their identity and relation. Pins resolve exactly or carry
explicit unavailability; they never fall back to the current revision.
"""

from sqlalchemy import select

from src.database.tables import task_dependencies
from src.records.models import RecordError

EXECUTION_TYPES = {"blocks", "parent-child", "waits-for", "conditional-blocks"}


async def record_edges_on(service, record, access, *, revision_id, conn):
    if record["kind"] == "knowledge":
        revision = await service._revision(record, revision_id, conn=conn)
        links = revision["snapshot"]["outgoing_links"]
    else:
        links = [
            service._link_snapshot(link)
            for link in await service.db.current_record_links_on(record["record_id"], conn=conn)
            if not link["removed"]
        ]
    edges = [
        dict(
            edge_id=f"link:{link['link_id']}",
            source_record_id=str(record["record_id"]),
            target_record_id=str(link["target_record_id"]),
            domain="informational",
            type=link["link_type"],
            target_revision_id=link["target_revision_id"],
            availability=link["availability"],
        )
        for link in await service._visible_links(links[:100], access, conn=conn)
    ]
    if record["kind"] == "task":
        dependencies = (
            (
                await conn.execute(
                    select(task_dependencies)
                    .where(task_dependencies.c.depends_on_task_id == record["task_id"])
                    .order_by(task_dependencies.c.task_id, task_dependencies.c.dep_type)
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
        for dependency in dependencies:
            try:
                target = await service.resolve_on(
                    f"task:{dependency['task_id']}", access, conn=conn
                )
            except RecordError:
                continue
            edges.append(
                dict(
                    edge_id=f"dependency:{record['task_id']}:{dependency['task_id']}:{dependency['dep_type']}",
                    source_record_id=str(record["record_id"]),
                    target_record_id=str(target["record_id"]),
                    domain="execution"
                    if dependency["dep_type"] in EXECUTION_TYPES
                    else "informational",
                    type=dependency["dep_type"],
                    target_revision_id=None,
                    availability="available",
                )
            )
    return edges
