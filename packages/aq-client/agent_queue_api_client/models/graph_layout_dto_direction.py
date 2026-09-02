from enum import Enum


class GraphLayoutDTODirection(str, Enum):
    LR = "LR"
    TD = "TD"

    def __str__(self) -> str:
        return str(self.value)
