"""Durable jobs: retries have a new job id, each id is one attempt."""

from src.database.tables import jobs, tasks
from src.jobs.policy import TERMINAL
from src.waits.adapters.base import Adapter, fields, foreign, integer, row, string, unresolved
from src.waits.conditions import Condition, NodeKey, Observation, Snapshot, SourceKey


class JobAdapter(Adapter):
    kind = "job"

    def normalise(self, raw):
        fields(raw, required={"job_id"}, optional={"attempt"})
        return Condition(
            self.kind,
            {
                "job_id": string(raw["job_id"]),
                "attempt": integer(raw.get("attempt", 1), 1),
            },
            raw.get("generation"),
        )

    async def authorise(self, conn, cond, scope):
        job = await row(conn, jobs, jobs.c.id == cond.parameters["job_id"])
        if job and (
            job["project_id"] != scope.project_id
            or (
                scope.owner_kind == "task"
                and (job["owner_kind"] != "task" or job["owner_id"] != scope.owner_id)
            )
        ):
            foreign()
        if cond.parameters["attempt"] != 1:
            unresolved("a job retry has a distinct job id")
        return await super().authorise(conn, cond, scope)

    async def snapshot(self, conn, cond):
        job = await row(conn, jobs, jobs.c.id == cond.parameters["job_id"])
        if job is None:
            return None
        blockers = ()
        if job["task_id"]:
            task = await row(conn, tasks, tasks.c.id == job["task_id"])
            if not task:
                return None
            # A job executed for this very waiter isn't a task waiting on itself.
            if task["id"] != cond.context.get("owner_id"):
                blockers = (NodeKey(f"task:{task['id']}", task["project_id"]),)
        return Snapshot(
            job["project_id"],
            1,
            Observation(
                str(job["state_version"]),
                "satisfied" if job["state"] in TERMINAL else "unsatisfied",
                job["result_ref"] or f"job:{job['id']}",
                {
                    "state": job["state"],
                    "exit_code": job["exit_code"],
                    "reason": job["infra_reason"],
                },
            ),
            f"job:{job['preset']}",
            blockers,
        )

    def source_key(self, cond):
        return SourceKey(self.kind, cond.parameters["job_id"], cond.generation)
