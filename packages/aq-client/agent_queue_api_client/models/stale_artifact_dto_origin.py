from enum import Enum


class StaleArtifactDTOOrigin(str, Enum):
    ACTIVATION = "activation"
    FIXTURE = "fixture"

    def __str__(self) -> str:
        return str(self.value)
