from enum import Enum


class ObjectLoopReconcileResponseOutcome(str, Enum):
    BRIEF_HELD = "brief_held"
    CHECKPOINT_APPROVED = "checkpoint_approved"
    CHECKPOINT_HELD = "checkpoint_held"
    RECONCILED = "reconciled"
    STOPPED = "stopped"
    WAITING_FOR_SCORE = "waiting_for_score"
    WAITING_FOR_WAVE = "waiting_for_wave"

    def __str__(self) -> str:
        return str(self.value)
