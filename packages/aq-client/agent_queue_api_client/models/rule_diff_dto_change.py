from enum import Enum


class RuleDiffDTOChange(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    UNCHANGED = "unchanged"

    def __str__(self) -> str:
        return str(self.value)
