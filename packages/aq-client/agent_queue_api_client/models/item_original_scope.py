from enum import Enum


class ItemOriginalScope(str, Enum):
    PROJECT = "project"
    SYSTEM = "system"

    def __str__(self) -> str:
        return str(self.value)
