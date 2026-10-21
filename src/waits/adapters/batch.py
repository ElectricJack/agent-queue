"""Integration ledger settlement; failure, abort and promotion remain distinct."""

from sqlalchemy import select

from src.database.tables import integration_batch_members as members
from src.database.tables import integration_batches as batches
from src.waits.adapters.base import Adapter, fields, invalid, row, string
from src.waits.conditions import Condition, NodeKey, Observation, Snapshot, SourceKey


class BatchAdapter(Adapter):
    kind = "batch"

    def normalise(self, raw):
        fields(raw, required={"batch_id"}, optional={"predicate"})
        if raw.get("predicate", "settled") != "settled":
            invalid("batch predicate must be settled")
        return Condition(
            self.kind,
            {"batch_id": string(raw["batch_id"]), "predicate": "settled"},
            raw.get("generation"),
        )

    async def snapshot(self, conn, cond):
        batch = await row(conn, batches, batches.c.id == cond.parameters["batch_id"])
        if batch is None:
            return None
        blockers = tuple(
            NodeKey(f"task:{task_id}", batch["project_id"])
            for task_id in (
                await conn.execute(
                    select(members.c.task_id).where(members.c.batch_id == batch["id"])
                )
            ).scalars()
        )
        return Snapshot(
            batch["project_id"],
            batch["baseline_generation"],
            Observation(
                f"{batch['updated_at']}:{batch['lifecycle']}",
                "satisfied"
                if batch["lifecycle"]
                in {
                    "promoted",
                    "aborted",
                    "failed",
                    "empty",
                }
                else "unsatisfied",
                f"batch:{batch['id']}@{batch['current_revision']}",
                {
                    "lifecycle": batch["lifecycle"],
                    "ejection": batch["ejection_record"],
                    "repair_stage_ordinal": batch["repair_stage_ordinal"],
                },
            ),
            f"batch:{batch['repository_id']}",
            blockers,
        )

    def source_key(self, cond):
        return SourceKey(self.kind, cond.parameters["batch_id"], cond.generation)
