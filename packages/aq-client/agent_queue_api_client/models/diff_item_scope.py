from enum import Enum


class DiffItemScope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"
    SKIP = "skip"

    def __str__(self) -> str:
        return str(self.value)
