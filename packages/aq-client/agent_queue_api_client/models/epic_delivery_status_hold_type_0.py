from enum import Enum


class EpicDeliveryStatusHoldType0(str, Enum):
    BACKOFF = "backoff"
    INTEGRATION = "integration"
    OPERATOR = "operator"

    def __str__(self) -> str:
        return str(self.value)
