from enum import Enum


class TaskCommentAuthorKind(str, Enum):
    AGENT = "agent"
    SUPERVISOR = "supervisor"
    USER = "user"

    def __str__(self) -> str:
        return str(self.value)
