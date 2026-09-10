from enum import Enum


class CommandCenterPreferencesDensity(str, Enum):
    COMFORTABLE = "comfortable"
    COMPACT = "compact"
    SPACIOUS = "spacious"

    def __str__(self) -> str:
        return str(self.value)
