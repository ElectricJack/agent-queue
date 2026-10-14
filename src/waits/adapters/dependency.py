"""Dependency waits use the actual readiness predicate, including fan-in."""

from sqlalchemy import select

from src.database.queries.blocked_state import unmet_dependency_predicate
from src.database.tables import task_dependencies, tasks
from src.models import BLOCKING_DEP_TYPES
from src.waits.adapters.base import Adapter, fields, foreign, invalid, row, string
from src.waits.conditions import Condition, NodeKey, Observation, Snapshot, SourceKey


class DependencyAdapter(Adapter):
    kind = "dependency"

    def normalise(self, raw):
        fields(raw, required={"task_id", "depends_on_task_id"}, optional={"dep_type"})
        dep_type = raw.get("dep_type", "blocks")
        if not isinstance(dep_type, str) or dep_type not in BLOCKING_DEP_TYPES:
            invalid("dependency must be a blocking edge type")
        return Condition(
            self.kind,
            {
                "task_id": string(raw["task_id"]),
                "depends_on_task_id": string(raw["depends_on_task_id"]),
                "dep_type": dep_type,
            },
            raw.get("generation"),
        )

    async def snapshot(self, conn, cond):
        p = cond.parameters
        owner = await row(conn, tasks, tasks.c.id == p["task_id"])
        target = await row(conn, tasks, tasks.c.id == p["depends_on_task_id"])
        if owner is None or target is None:
            return None
        if owner["project_id"] != target["project_id"]:
            foreign()
        edge = task_dependencies
        unmet = await conn.scalar(
            select(unmet_dependency_predicate(edge, tasks))
            .select_from(edge.join(tasks, tasks.c.id == edge.c.depends_on_task_id))
            .where(
                edge.c.task_id == p["task_id"],
                edge.c.depends_on_task_id == target["id"],
                edge.c.dep_type == p["dep_type"],
            )
        )
        if unmet is None:
            return None
        return Snapshot(
            owner["project_id"],
            target["claim_epoch"],
            Observation(
                f"{target['updated_at']}:{unmet}",
                "unsatisfied" if unmet else "satisfied",
                f"dependency:{owner['id']}:{target['id']}:{p['dep_type']}",
                {"dep_type": p["dep_type"], "status": target["status"]},
            ),
            f"dependency:{p['dep_type']}",
            (NodeKey(f"task:{target['id']}", target["project_id"]),),
        )

    def source_key(self, cond):
        p = cond.parameters
        return SourceKey(
            self.kind, f"{p['task_id']}:{p['depends_on_task_id']}:{p['dep_type']}", cond.generation
        )
