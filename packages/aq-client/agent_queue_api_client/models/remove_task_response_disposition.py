from enum import Enum


class RemoveTaskResponseDisposition(str, Enum):
    DELETED = "deleted"
    PREVIEW = "preview"

    def __str__(self) -> str:
        return str(self.value)
