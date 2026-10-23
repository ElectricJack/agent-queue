"""Task settlement, fenced by claim generation and status revision."""

from src.database.tables import tasks
from src.models import TaskStatus
from src.waits.adapters.base import Adapter, fields, invalid, row, string
from src.waits.conditions import Condition, NodeKey, Observation, Snapshot, SourceKey

# CANCELLED in the design is not a supported TaskStatus. Wait cancellation is
# a separate wait_records outcome, not a task settlement value.
SETTLEMENT = frozenset({"COMPLETED", "FAILED", "BLOCKED"})


class TaskAdapter(Adapter):
    kind = "task"

    def normalise(self, raw):
        fields(raw, required={"task_id"}, optional={"target_statuses"})
        statuses = raw.get("target_statuses", sorted(SETTLEMENT))
        if (
            not isinstance(statuses, (list, tuple))
            or not statuses
            or any(not isinstance(s, str) for s in statuses)
            or set(statuses) - {s.value for s in TaskStatus}
        ):
            invalid("target_statuses must name task statuses")
        return Condition(
            self.kind,
            {
                "task_id": string(raw["task_id"]),
                "target_statuses": sorted(set(statuses)),
            },
            raw.get("generation"),
        )

    async def snapshot(self, conn, cond):
        task = await row(conn, tasks, tasks.c.id == cond.parameters["task_id"])
        if task is None:
            return None
        return Snapshot(
            task["project_id"],
            task["claim_epoch"],
            Observation(
                f"{task['updated_at']}:{task['status']}",
                "satisfied"
                if task["status"] in cond.parameters["target_statuses"]
                else "unsatisfied",
                f"task:{task['id']}@{task['claim_epoch']}:{task['updated_at']}",
                {"status": task["status"]},
            ),
            f"task:{task['task_type']}",
            (NodeKey(f"task:{task['id']}", task["project_id"]),),
        )

    def source_key(self, cond):
        return SourceKey(self.kind, cond.parameters["task_id"], cond.generation)
