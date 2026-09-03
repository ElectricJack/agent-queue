from enum import Enum


class PlaybookV1DrainStatusResponseAdmission(str, Enum):
    CLOSED = "closed"
    OPEN = "open"

    def __str__(self) -> str:
        return str(self.value)
