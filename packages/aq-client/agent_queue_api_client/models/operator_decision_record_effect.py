from enum import Enum


class OperatorDecisionRecordEffect(str, Enum):
    HOLD = "hold"
    NOTE = "note"
    RELEASE = "release"

    def __str__(self) -> str:
        return str(self.value)
