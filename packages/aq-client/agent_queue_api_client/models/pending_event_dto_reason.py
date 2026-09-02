from enum import Enum


class PendingEventDTOReason(str, Enum):
    DISABLED = "disabled"
    INVALID_ARTIFACT = "invalid_artifact"
    QUESTION_REQUIRED = "question_required"
    STALE_CONTRACT = "stale_contract"
    UNAVAILABLE = "unavailable"

    def __str__(self) -> str:
        return str(self.value)
