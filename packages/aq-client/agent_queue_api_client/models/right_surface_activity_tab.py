from enum import Enum


class RightSurfaceActivityTab(str, Enum):
    EVENTS = "events"
    GATES = "gates"

    def __str__(self) -> str:
        return str(self.value)
