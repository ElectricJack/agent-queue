from enum import Enum


class StaleArtifactDTOKind(str, Enum):
    COMMAND = "command"
    PROFILE = "profile"

    def __str__(self) -> str:
        return str(self.value)
