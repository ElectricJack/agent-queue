from enum import Enum


class AgentWaitRecordKind(str, Enum):
    JOB = "job"
    MESSAGE = "message"
    TASK = "task"
    TIMER = "timer"

    def __str__(self) -> str:
        return str(self.value)
