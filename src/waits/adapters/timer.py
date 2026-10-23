"""Bounded UTC instants; the reason text never affects verification."""

import math
import time
from datetime import datetime

from src.waits.adapters.base import Adapter, fields, invalid
from src.waits.conditions import Condition, DeadlinePolicy, Observation, Snapshot, SourceKey


class TimerAdapter(Adapter):
    kind = "timer"

    def __init__(self, clock=time.time):
        self.clock = clock

    def normalise(self, raw):
        fields(raw, required={"due_at"})
        due = raw["due_at"]
        if isinstance(due, str):
            try:
                instant = datetime.fromisoformat(due.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    invalid("due_at must include a UTC offset")
                due = instant.timestamp()
            except ValueError:
                invalid("due_at must be a UTC instant")
        if type(due) not in (int, float) or not math.isfinite(due):
            invalid("due_at must be a finite UTC instant")
        return Condition(self.kind, {"due_at": float(due)}, raw.get("generation"))

    async def authorise(self, conn, cond, scope):
        if not 1 <= cond.parameters["due_at"] - self.clock() <= 86400:
            invalid("due_at must be between one second and one day from now")
        return await super().authorise(conn, cond, scope)

    async def snapshot(self, conn, cond):
        due = cond.parameters["due_at"]
        return Snapshot(
            cond.context["project_id"],
            0,
            Observation(
                str(due),
                "satisfied" if due <= self.clock() else "unsatisfied",
                f"timer:{due}",
            ),
            "timer",
        )

    def source_key(self, cond):
        return SourceKey(self.kind, str(cond.parameters["due_at"]), 0)

    def deadline_policy(self, cond, policy):
        return DeadlinePolicy(policy.escalation_seconds(self.kind), cond.parameters["due_at"])
