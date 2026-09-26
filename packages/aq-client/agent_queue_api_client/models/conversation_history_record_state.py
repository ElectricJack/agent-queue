from enum import Enum


class ConversationHistoryRecordState(str, Enum):
    CLOSED = "closed"
    DELIVERY_BLOCKED = "delivery_blocked"
    OPEN = "open"
    OPENING = "opening"

    def __str__(self) -> str:
        return str(self.value)
