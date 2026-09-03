from enum import Enum


class MigrationInventoryEntryDTODisposition(str, Enum):
    DISABLED = "disabled"
    INVALID = "invalid"
    QUESTION_REQUIRED = "question_required"
    READY = "ready"

    def __str__(self) -> str:
        return str(self.value)
