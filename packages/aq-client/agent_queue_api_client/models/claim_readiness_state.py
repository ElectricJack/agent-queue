from enum import Enum


class ClaimReadinessState(str, Enum):
    CLAIMABLE = "claimable"
    UNKNOWN = "unknown"
    WAITING_ON_DELIVERY = "waiting_on_delivery"
    WITHHELD = "withheld"

    def __str__(self) -> str:
        return str(self.value)
