from enum import Enum


class PlaybookCutoverWindowStatusResponseAdmissionType0(str, Enum):
    CLOSED = "closed"
    OPEN = "open"

    def __str__(self) -> str:
        return str(self.value)
