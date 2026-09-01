from enum import StrEnum


class PlaybookGraphEdgeEdgeType(StrEnum):
    CONDITION = "condition"
    FAILURE = "failure"
    GOTO = "goto"
    OTHERWISE = "otherwise"
    SUCCESS = "success"
    TIMEOUT = "timeout"

    def __str__(self) -> str:
        return str(self.value)
