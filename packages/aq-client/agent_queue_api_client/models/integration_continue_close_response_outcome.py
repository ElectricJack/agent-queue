from enum import Enum


class IntegrationContinueCloseResponseOutcome(str, Enum):
    COMPLETED = "completed"
    PENDING = "pending"
    REFUSED = "refused"

    def __str__(self) -> str:
        return str(self.value)
