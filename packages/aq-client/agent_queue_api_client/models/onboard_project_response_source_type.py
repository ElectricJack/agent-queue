from enum import Enum


class OnboardProjectResponseSourceType(str, Enum):
    CLONE = "clone"
    INIT = "init"
    LINK = "link"

    def __str__(self) -> str:
        return str(self.value)
