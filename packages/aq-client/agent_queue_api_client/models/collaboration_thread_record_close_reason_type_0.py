from enum import Enum


class CollaborationThreadRecordCloseReasonType0(str, Enum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    CLOSED = "closed"
    EXPIRED = "expired"
    MEMBERS_BELOW_TWO = "members_below_two"

    def __str__(self) -> str:
        return str(self.value)
