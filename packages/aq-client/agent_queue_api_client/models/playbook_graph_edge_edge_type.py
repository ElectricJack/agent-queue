from enum import Enum


class PlaybookGraphEdgeEdgeType(str, Enum):
    CONDITION = "condition"
    FAILURE = "failure"
    GOTO = "goto"
    OTHERWISE = "otherwise"
    SUCCESS = "success"
    TIMEOUT = "timeout"

    def __str__(self) -> str:
        return str(self.value)
