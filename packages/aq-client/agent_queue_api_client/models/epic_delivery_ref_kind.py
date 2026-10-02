from enum import Enum


class EpicDeliveryRefKind(str, Enum):
    BATCH = "batch"
    GATE = "gate"
    OPERATION = "operation"
    OPERATOR = "operator"
    SESSION = "session"
    SUPERVISOR = "supervisor"
    SYSTEM = "system"
    TASK = "task"

    def __str__(self) -> str:
        return str(self.value)
