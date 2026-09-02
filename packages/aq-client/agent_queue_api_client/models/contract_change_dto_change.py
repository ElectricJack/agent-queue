from enum import Enum


class ContractChangeDTOChange(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    UNCHANGED = "unchanged"

    def __str__(self) -> str:
        return str(self.value)
