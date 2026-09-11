from enum import Enum


class NavOrganizationDocumentScope(str, Enum):
    USER = "user"
    WORKSPACE = "workspace"

    def __str__(self) -> str:
        return str(self.value)
