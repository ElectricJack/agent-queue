from enum import Enum


class DeleteCleanupResultOutcome(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"

    def __str__(self) -> str:
        return str(self.value)
