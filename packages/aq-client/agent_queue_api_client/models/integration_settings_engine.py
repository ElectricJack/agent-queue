from enum import Enum


class IntegrationSettingsEngine(str, Enum):
    ACTIVE = "active"
    SHADOW = "shadow"

    def __str__(self) -> str:
        return str(self.value)
