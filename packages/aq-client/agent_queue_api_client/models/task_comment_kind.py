from enum import Enum


class TaskCommentKind(str, Enum):
    NOTE = "note"
    PROGRESS = "progress"

    def __str__(self) -> str:
        return str(self.value)
