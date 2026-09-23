from enum import Enum


class PendingPullRequestState(str, Enum):
    OPEN = "open"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return str(self.value)
