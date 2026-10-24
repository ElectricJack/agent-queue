from enum import Enum


class HeldBatchSummaryState(str, Enum):
    HELD = "held"
    NEEDS_SCOPE = "needs_scope"
    RELEASED = "released"
    RETIRED = "retired"
    REVIEWING = "reviewing"

    def __str__(self) -> str:
        return str(self.value)
