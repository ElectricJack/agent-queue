from enum import Enum


class CollaborationThreadRecordState(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"

    def __str__(self) -> str:
        return str(self.value)
