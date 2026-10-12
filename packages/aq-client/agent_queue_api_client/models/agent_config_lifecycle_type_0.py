from enum import Enum


class AgentConfigLifecycleType0(str, Enum):
    NAMED = "named"
    POOL = "pool"
    TASK = "task"

    def __str__(self) -> str:
        return str(self.value)
