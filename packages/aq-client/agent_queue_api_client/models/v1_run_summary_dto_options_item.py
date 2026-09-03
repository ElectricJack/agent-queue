from enum import Enum


class V1RunSummaryDTOOptionsItem(str, Enum):
    CANCEL = "cancel"
    RESOLVE = "resolve"
    WAIT = "wait"

    def __str__(self) -> str:
        return str(self.value)
