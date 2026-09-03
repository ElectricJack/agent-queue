from enum import Enum


class StaleArtifactDTOChange(str, Enum):
    CHANGED = "changed"
    REMOVED = "removed"

    def __str__(self) -> str:
        return str(self.value)
