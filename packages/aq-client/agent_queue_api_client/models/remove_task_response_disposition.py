from enum import Enum


class RemoveTaskResponseDisposition(str, Enum):
    ARCHIVED = "archived"
    DELETED = "deleted"
    PREVIEW = "preview"

    def __str__(self) -> str:
        return str(self.value)
