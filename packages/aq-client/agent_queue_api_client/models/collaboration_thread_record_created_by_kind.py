from enum import Enum


class CollaborationThreadRecordCreatedByKind(str, Enum):
    OPERATOR = "operator"
    SUPERVISOR = "supervisor"

    def __str__(self) -> str:
        return str(self.value)
