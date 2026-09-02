from enum import Enum


class StepDiffDTOChange(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    UNCHANGED = "unchanged"

    def __str__(self) -> str:
        return str(self.value)
