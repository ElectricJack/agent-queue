from enum import Enum


class ActivationStateDTOHealth(str, Enum):
    DISABLED = "disabled"
    INVALID = "invalid"
    QUESTION_REQUIRED = "question_required"
    READY = "ready"
    STALE_CONTRACT = "stale_contract"
    UNAVAILABLE = "unavailable"

    def __str__(self) -> str:
        return str(self.value)
