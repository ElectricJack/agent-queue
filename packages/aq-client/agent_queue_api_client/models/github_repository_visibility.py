from enum import Enum


class GithubRepositoryVisibility(str, Enum):
    INTERNAL = "internal"
    PRIVATE = "private"
    PUBLIC = "public"

    def __str__(self) -> str:
        return str(self.value)
