from enum import Enum


class LoopNodeDetailDTOFailurePolicy(str, Enum):
    COLLECT = "collect"
    CONTINUE = "continue"
    HALT = "halt"

    def __str__(self) -> str:
        return str(self.value)
