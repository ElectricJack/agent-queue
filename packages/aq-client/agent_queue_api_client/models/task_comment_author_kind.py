from enum import StrEnum


class TaskCommentAuthorKind(StrEnum):
    AGENT = "agent"
    SUPERVISOR = "supervisor"
    USER = "user"

    def __str__(self) -> str:
        return str(self.value)
