from enum import Enum


class AgentWaitRecordOwnerKind(str, Enum):
    SUPERVISOR = "supervisor"
    TASK = "task"

    def __str__(self) -> str:
        return str(self.value)
