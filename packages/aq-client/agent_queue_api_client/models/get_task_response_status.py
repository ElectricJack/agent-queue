from enum import Enum


class GetTaskResponseStatus(str, Enum):
    ASSIGNED = "ASSIGNED"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    DEFINED = "DEFINED"
    FAILED = "FAILED"
    HIBERNATED = "HIBERNATED"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    READY = "READY"
    RESUME_PENDING = "RESUME_PENDING"
    VALUE_0 = ""
    WAITING = "WAITING"
    WAITING_INPUT = "WAITING_INPUT"

    def __str__(self) -> str:
        return str(self.value)
