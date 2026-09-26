from enum import Enum


class AgentWaitRecordState(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SATISFIED = "satisfied"

    def __str__(self) -> str:
        return str(self.value)
