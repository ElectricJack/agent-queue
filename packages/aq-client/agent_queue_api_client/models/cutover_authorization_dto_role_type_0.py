from enum import Enum


class CutoverAuthorizationDTORoleType0(str, Enum):
    AUTHOR = "author"
    RELEASE_OPERATOR = "release_operator"

    def __str__(self) -> str:
        return str(self.value)
