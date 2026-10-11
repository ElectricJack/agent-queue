from enum import Enum


class EpicDeliveryStatusDisplayStateType0(str, Enum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    COLLECTING = "collecting"
    COMPLETED = "completed"
    DELIVERING = "delivering"
    WAITING = "waiting"

    def __str__(self) -> str:
        return str(self.value)
