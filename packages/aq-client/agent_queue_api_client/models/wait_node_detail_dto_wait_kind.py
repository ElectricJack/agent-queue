from enum import Enum


class WaitNodeDetailDTOWaitKind(str, Enum):
    EVENT = "event"
    HUMAN = "human"
    TASK = "task"
    TIMER = "timer"

    def __str__(self) -> str:
        return str(self.value)
