"""Small decision data for the Git train, embedded in a V2 policy artifact."""

from typing import Literal

from pydantic import Field

from src.integration.models import RequiredCheckSet
from src.playbooks.expressions import V2Base


class TrainPolicy(V2Base):
    # Mechanism enforces source ancestry, exact CI, human intent and writer fences.
    # Policy chooses whether to dispatch/repair now or revisit after a finite wait.
    idle: Literal["merge", "wait"] = "merge"
    red: Literal["repair", "wait"] = "repair"
    conflict: Literal["repair", "wait"] = "repair"
    wait_seconds: int = Field(default=30, gt=0, le=3600)
    required_checks: RequiredCheckSet | None = None

    def action(self, state: str) -> str:
        if state == "green":
            return "publish"
        return getattr(self, state, "wait") if state in {"idle", "red", "conflict"} else "wait"
