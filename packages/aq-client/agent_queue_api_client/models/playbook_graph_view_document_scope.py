from enum import Enum


class PlaybookGraphViewDocumentScope(str, Enum):
    USER = "user"
    WORKSPACE = "workspace"

    def __str__(self) -> str:
        return str(self.value)
