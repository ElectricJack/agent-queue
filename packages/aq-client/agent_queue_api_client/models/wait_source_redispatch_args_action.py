from enum import Enum


class WaitSourceRedispatchArgsAction(str, Enum):
    BLOCKERS = "blockers"
    REDISPATCH = "redispatch"
    RETIRE = "retire"

    def __str__(self) -> str:
        return str(self.value)
