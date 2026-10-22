"""Author budget observes operation settlement; it never sleeps the author."""
from src.database.tables import integration_close_attempts
from src.waits.adapters.base import Adapter, fields, foreign, row, string, invalid
from src.waits.conditions import Condition, DeadlinePolicy, Observation, Snapshot, SourceKey


class CloseMergeAdapter(Adapter):
    kind = "close_merge"

    def normalise(self, raw):
        fields(raw, required={"operation_id", "budget_seconds"})
        budget = raw["budget_seconds"]
        if type(budget) not in (float, int) or not 0 < budget < float("inf"):
            invalid("author budget must be finite and positive")
        return Condition(self.kind, {"operation_id": string(raw["operation_id"]), "budget_seconds": budget},
                         raw.get("generation"))

    async def authorise(self, conn, cond, scope):
        if scope.principal != "service" or scope.owner_kind != "integration" or (
                scope.owner_id != cond.parameters["operation_id"]):
            foreign()
        return await super().authorise(conn, cond, scope)

    async def snapshot(self, conn, cond):
        op = await row(conn, integration_close_attempts,
                       integration_close_attempts.c.id == cond.parameters["operation_id"])
        if op is None:
            return None
        return Snapshot(op["project_id"], 0, Observation(
            str(op["version"]), "satisfied" if op["state"] in {
                "finished", "deferred", "cancelled"} else "unsatisfied",
            "close_merge:" + op["id"], {"state": op["state"], "task_id": op["task_id"]}),
            "integration:author_close")

    def source_key(self, cond):
        return SourceKey(self.kind, cond.parameters["operation_id"], 0)

    def deadline_policy(self, cond, policy):
        # The project close policy supplies the registration deadline override.
        return DeadlinePolicy(cond.parameters["budget_seconds"])
