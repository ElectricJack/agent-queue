from enum import Enum


class RightSurfaceKindType0(str, Enum):
    DRAWER = "drawer"
    PANE = "pane"

    def __str__(self) -> str:
        return str(self.value)
