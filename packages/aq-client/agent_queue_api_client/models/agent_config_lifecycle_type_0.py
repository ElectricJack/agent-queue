from enum import Enum


class AgentConfigLifecycleType0(str, Enum):
    POOL = "pool"
    TASK = "task"

    def __str__(self) -> str:
        return str(self.value)
