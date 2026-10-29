from enum import Enum


class TaskClaimAbortResponseOutcomeType0(str, Enum):
    CLEANUP_PENDING = "cleanup_pending"
    SETTLED = "settled"

    def __str__(self) -> str:
        return str(self.value)
