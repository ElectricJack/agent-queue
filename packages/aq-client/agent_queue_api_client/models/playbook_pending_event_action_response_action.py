from enum import Enum


class PlaybookPendingEventActionResponseAction(str, Enum):
    DISCARD = "discard"
    DISPATCH = "dispatch"

    def __str__(self) -> str:
        return str(self.value)
