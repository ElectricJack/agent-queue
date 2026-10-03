from enum import Enum


class EpicDeliveryStatusEvidence(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNTRACKED = "untracked"

    def __str__(self) -> str:
        return str(self.value)
