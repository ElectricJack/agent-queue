from enum import Enum


class PlaybookV1AdmissionResponseAdmissionType0(str, Enum):
    CLOSED = "closed"
    OPEN = "open"

    def __str__(self) -> str:
        return str(self.value)
