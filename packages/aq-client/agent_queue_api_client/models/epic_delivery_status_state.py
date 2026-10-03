from enum import Enum


class EpicDeliveryStatusState(str, Enum):
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    DELIVERED = "delivered"
    IMPLEMENTING = "implementing"
    INTEGRATING = "integrating"
    NOT_TRACKED = "not_tracked"
    PAUSED = "paused"
    QUEUED = "queued"
    UNKNOWN = "unknown"
    VERIFYING = "verifying"

    def __str__(self) -> str:
        return str(self.value)
