from enum import Enum


class OperatorDecisionModelObjectKind(str, Enum):
    BATCH = "batch"
    OPERATION = "operation"
    TASK = "task"

    def __str__(self) -> str:
        return str(self.value)
