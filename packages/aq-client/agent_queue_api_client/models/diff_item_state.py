from enum import Enum


class DiffItemState(str, Enum):
    CONFIGURE_INTEGRATION = "configure integration"
    COPY = "copy"
    PENDING_CONFIGURATION = "pending configuration"
    PENDING_REVIEW = "pending review"
    SYSTEM_TEMPLATE = "system template"

    def __str__(self) -> str:
        return str(self.value)
