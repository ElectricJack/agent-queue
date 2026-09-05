from enum import Enum


class GithubOwnerKind(str, Enum):
    ORGANIZATION = "organization"
    USER = "user"

    def __str__(self) -> str:
        return str(self.value)
