from enum import Enum


class DiffItemOriginalScope(str, Enum):
    PROJECT = "project"
    SYSTEM = "system"

    def __str__(self) -> str:
        return str(self.value)
