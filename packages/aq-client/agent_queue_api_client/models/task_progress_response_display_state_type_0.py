from enum import Enum


class TaskProgressResponseDisplayStateType0(str, Enum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    WAITING = "waiting"

    def __str__(self) -> str:
        return str(self.value)
