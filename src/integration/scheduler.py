"""Durable per-project integration sweep scheduling."""

from __future__ import annotations

from typing import Any, Literal

from src.integration.outbox import enqueue_integration_event


ScheduleTrigger = Literal["periodic", "manual"]


class IntegrationScheduler:
    """Coalesce periodic and manual triggers into one durable sweep request."""

    DEFAULT_INTERVAL_SECONDS = 300

    def __init__(self, db: Any):
        self.db = db

    async def configure(
        self,
        *,
        project_id: str,
        now: float,
        enabled: bool,
        interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Persist scheduling controls while retaining any outstanding request."""
        if interval_seconds is not None and interval_seconds <= 0:
            raise ValueError("integration schedule interval must be positive")
        async with self.db.immediate() as conn:
            schedule = await self.db.lock_integration_schedule_on(
                conn,
                project_id=project_id,
                now=now,
                default_interval_seconds=self.DEFAULT_INTERVAL_SECONDS,
            )
            values: dict[str, Any] = {"enabled": enabled, "updated_at": now}
            if (
                interval_seconds is not None
                and interval_seconds != schedule["interval_seconds"]
            ):
                values["interval_seconds"] = interval_seconds
                values["next_due_at"] = now + interval_seconds
            return await self.db.update_integration_schedule_on(
                conn, project_id=project_id, values=values
            )

    async def mark_due(
        self, project_id: str, now: float, trigger: str
    ) -> dict[str, Any]:
        """Mark one sweep due, or return the durable request already in flight."""
        if trigger not in {"periodic", "manual"}:
            raise ValueError("integration schedule trigger must be periodic or manual")

        async with self.db.immediate() as conn:
            schedule = await self.db.lock_integration_schedule_on(
                conn,
                project_id=project_id,
                now=now,
                default_interval_seconds=self.DEFAULT_INTERVAL_SECONDS,
            )
            if trigger == "periodic" and not schedule["enabled"]:
                return self._result("disabled", project_id, schedule)

            periodic_due = trigger == "periodic" and now >= schedule["next_due_at"]
            if periodic_due:
                interval = int(schedule["interval_seconds"])
                elapsed_boundaries = int((now - schedule["next_due_at"]) // interval) + 1
                next_due_at = schedule["next_due_at"] + elapsed_boundaries * interval
                schedule = await self.db.update_integration_schedule_on(
                    conn,
                    project_id=project_id,
                    values={
                        "next_due_at": next_due_at,
                        "last_observed_window": next_due_at - interval,
                        "updated_at": now,
                    },
                )

            if schedule["outstanding_request_id"] is not None:
                return self._result("coalesced", project_id, schedule)
            if trigger == "periodic" and not periodic_due:
                return self._result("not_due", project_id, schedule)

            sequence = int(schedule["request_sequence"]) + 1
            request_id = f"integration-sweep:{project_id}:{sequence}"
            schedule = await self.db.update_integration_schedule_on(
                conn,
                project_id=project_id,
                values={
                    "request_sequence": sequence,
                    "outstanding_request_id": request_id,
                    "outstanding_trigger": trigger,
                    "outstanding_requested_at": now,
                    "updated_at": now,
                },
            )
            await enqueue_integration_event(
                conn,
                event_id=request_id,
                dedup_key=request_id,
                project_id=project_id,
                event_type="integration.sweep_due",
                payload={"project_id": project_id, "operation_id": request_id},
                available_at=now,
            )
            return self._result("due", project_id, schedule)

    @staticmethod
    def _result(outcome: str, project_id: str, schedule: dict[str, Any]) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "project_id": project_id,
            "request_id": schedule["outstanding_request_id"],
            "trigger": schedule["outstanding_trigger"],
            "requested_at": schedule["outstanding_requested_at"],
            "request_sequence": int(schedule["request_sequence"]),
            "next_due_at": float(schedule["next_due_at"]),
        }
