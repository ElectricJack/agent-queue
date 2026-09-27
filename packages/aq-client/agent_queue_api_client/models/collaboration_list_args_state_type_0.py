from enum import Enum


class CollaborationListArgsStateType0(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"

    def __str__(self) -> str:
        return str(self.value)
