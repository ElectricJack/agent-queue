from enum import Enum


class WaitFactsDTODeadlineSourceType0(str, Enum):
    RUN = "run"
    WAIT = "wait"

    def __str__(self) -> str:
        return str(self.value)
