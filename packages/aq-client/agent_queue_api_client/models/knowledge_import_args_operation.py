from enum import Enum


class KnowledgeImportArgsOperation(str, Enum):
    APPLY = "apply"
    CANCEL = "cancel"
    DRY_RUN = "dry-run"
    RESUME = "resume"

    def __str__(self) -> str:
        return str(self.value)
